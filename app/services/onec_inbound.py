"""
Входящая интеграция из 1С:УНФ (push через вебхук).

1С на проведении документа сама шлёт в TMS:
  • счёт (Document_СчетНаОплату) — метаданные + печатную форму PDF;
  • УПД (печатная форма Document_РасходнаяНакладная) — PDF.

TMS:
  • дублирует счёт в карточку (Invoice) с привязкой к заказу;
  • прикладывает печатную форму к заказу (AttachedFile, со сжатием).

Аутентификация вебхука — по токену onec_webhook_token (см. sync_1c.py).
Тяжёлой логики выгрузки тут нет: только приём и раскладка.
"""
import logging
import os
import uuid
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.models import AttachedFile, Counterparty, Invoice, InvoiceItem, Order, Product
from app.utils.file_compress import compress_file

logger = logging.getLogger(__name__)

UPLOAD_ROOT = "uploads"
# Тип документа из 1С → тип файла в карточке заказа (см. files.FILE_TYPES["order"])
_PDF_FILE_TYPE = {"invoice": "invoice", "upd": "upd", "tn": "tn"}


# ── Поиск связанных сущностей по Ref_Key из 1С ───────────────────────────────

def find_order_by_1c_ref(db: Session, order_ref: str | None) -> Order | None:
    if not order_ref:
        return None
    return db.query(Order).filter(Order.external_id_1c == order_ref).first()


def find_counterparty_by_1c_ref(db: Session, ref: str | None) -> Counterparty | None:
    if not ref:
        return None
    return db.query(Counterparty).filter(Counterparty.external_id_1c == ref).first()


def _digits(s) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())


# ── Счёт: 1С → TMS (дубль с привязкой к заказу) ──────────────────────────────

def upsert_invoice_from_1c(
    db: Session,
    *,
    doc_ref: str,
    number: str,
    date_iso: str | None,
    amount: float | None,
    order_ref: str | None = None,
    counterparty_ref: str | None = None,
    items: list[dict] | None = None,
) -> Invoice | None:
    """Создаёт/обновляет счёт в TMS по данным из 1С.

    Идемпотентность: ищем по external_id_1c == doc_ref, затем по номеру.
    Возвращает Invoice (или None, если нет ни заказа, ни контрагента для привязки).
    """
    order = find_order_by_1c_ref(db, order_ref)
    cp = find_counterparty_by_1c_ref(db, counterparty_ref)
    if order and not cp:
        cp = order.counterparty
    if not cp:
        logger.warning("upsert_invoice_from_1c: не найден контрагент (order_ref=%s, cp_ref=%s)",
                       order_ref, counterparty_ref)
        return None

    inv = db.query(Invoice).filter(Invoice.external_id_1c == doc_ref).first()
    if not inv and number:
        inv = db.query(Invoice).filter(Invoice.number == number).first()

    inv_date = _parse_date(date_iso) or date.today()
    total = round(float(amount or 0), 2)

    if not inv:
        inv = Invoice(number=number or doc_ref[:50], date=inv_date,
                      counterparty_id=cp.id, status="issued")
        db.add(inv)
    # Обновляем шапку (но не трогаем статус «paid» — его выставляет sync_payments)
    inv.number = number or inv.number
    inv.date = inv_date
    inv.counterparty_id = cp.id
    if order:
        inv.order_id = order.id
        if order.contract_id:
            inv.contract_id = order.contract_id
    inv.total_amount = total
    inv.external_id_1c = doc_ref
    inv.synced_to_1c_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if inv.status not in ("paid", "cancelled"):
        inv.status = "issued"

    # Позиции — только если 1С их прислала (иначе оставляем как есть)
    if items:
        db.flush()
        for old in list(inv.items):
            db.delete(old)
        db.flush()
        subtotal = vat_amount = 0.0
        for it in items:
            qty = float(it.get("quantity") or 0)
            price = float(it.get("price") or 0)
            disc = min(max(float(it.get("discount_pct") or 0), 0), 100)
            vat = float(it.get("vat_rate") or 0)
            line = round(qty * price * (1 - disc / 100), 2)
            subtotal += line
            vat_amount += line * vat / 100
            product = None
            pref = it.get("product_ref")
            if pref:
                product = db.query(Product).filter(Product.external_id_1c == pref).first()
            db.add(InvoiceItem(
                invoice_id=inv.id,
                product_id=product.id if product else None,
                name=(it.get("name") or "").strip()[:200] or "Товар",
                quantity=qty, unit=(it.get("unit") or "шт")[:20],
                price=price, vat_rate=vat, discount_pct=disc, amount=line,
            ))
        inv.subtotal = round(subtotal, 2)
        inv.vat_amount = round(vat_amount, 2)
        if not amount:
            inv.total_amount = round(subtotal + vat_amount, 2)

    db.commit()
    return inv


# ── Печатная форма (PDF): 1С → файл в карточке заказа ─────────────────────────

def attach_document_pdf(
    db: Session,
    *,
    order: Order,
    doc_type: str,
    doc_ref: str,
    filename: str,
    data: bytes,
    uploaded_by_id: int | None = None,
) -> AttachedFile | None:
    """Сохраняет PDF печатной формы как вложение заказа (со сжатием).

    Идемпотентно: повторная присылка того же документа (external_key) заменяет
    прежний файл, а не плодит дубли.
    """
    if not order or not data:
        return None
    file_type = _PDF_FILE_TYPE.get(doc_type, "other")
    external_key = f"{doc_ref}:{doc_type}"

    # Удаляем прежнюю версию того же документа из 1С
    prev = (db.query(AttachedFile)
            .filter(AttachedFile.entity_type == "order",
                    AttachedFile.entity_id == order.id,
                    AttachedFile.external_key == external_key)
            .all())
    for old in prev:
        try:
            if old.stored_path and os.path.exists(old.stored_path):
                os.remove(old.stored_path)
        except OSError:
            pass
        db.delete(old)

    dest_dir = os.path.join(UPLOAD_ROOT, "orders", str(order.id))
    os.makedirs(dest_dir, exist_ok=True)
    stored_path = os.path.join(dest_dir, f"{uuid.uuid4().hex}.pdf")
    with open(stored_path, "wb") as out:
        out.write(data)

    size_original = len(data)
    # Сжимаем синхронно (best-effort) — PDF из 1С обычно сжимается хорошо
    try:
        size_compressed = compress_file(stored_path, ".pdf")
    except Exception:  # noqa: BLE001
        size_compressed = size_original

    safe_name = _safe_filename(filename) or f"{doc_type}.pdf"
    af = AttachedFile(
        entity_type="order", entity_id=order.id, file_type=file_type,
        original_name=safe_name, stored_path=stored_path.replace("\\", "/"),
        size_original=size_original, size_compressed=size_compressed,
        source="1c", external_key=external_key, uploaded_by_id=uploaded_by_id,
    )
    db.add(af)
    db.commit()
    return af


# ── Утилиты ──────────────────────────────────────────────────────────────────

def _parse_date(s: str | None):
    if not s:
        return None
    s = str(s)[:10]
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _safe_filename(name: str) -> str:
    import re as _re
    return _re.sub(r'[\x00-\x1f\x7f\\/:"*?<>|]', "_", str(name or "")).strip()[:300]

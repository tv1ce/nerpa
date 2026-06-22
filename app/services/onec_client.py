"""
Клиент к OData API 1С:УНФ.

Базовый URL: {settings.onec_url}   пример: http://srv4.life-it.pro/grach_unf/odata/standard.odata
Аутентификация: HTTP Basic (onec_user / onec_password)
Формат: JSON (odata=nometadata — меньше трафика)
"""
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.models import CompanySettings, Product

logger = logging.getLogger(__name__)

TIMEOUT = 15


# ── Вспомогательные ──────────────────────────────────────────────────────────

def _get_settings(db: Session) -> CompanySettings | None:
    s = db.query(CompanySettings).first()
    if not s or not s.onec_url:
        return None
    return s


def _client(s: CompanySettings) -> httpx.Client:
    return httpx.Client(
        base_url=s.onec_url.rstrip("/") + "/",
        auth=(s.onec_user or "", s.onec_password or ""),
        headers={
            "Accept": "application/json;odata=nometadata",
            "Content-Type": "application/json",
        },
        timeout=TIMEOUT,
    )


def _save_external_id(db: Session, obj, ref_key: str) -> None:
    obj.external_id_1c = ref_key
    obj.synced_to_1c_at = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        db.commit()
    except Exception:
        db.rollback()


# ── Проверка подключения ──────────────────────────────────────────────────────

def test_connection(db: Session) -> dict:
    """
    Проверяет подключение к 1С:УНФ.
    Не требует onec_enabled=True — нужна только строка URL.
    Возвращает {"ok": bool, "message": str}.
    """
    s = _get_settings(db)
    if not s:
        return {"ok": False, "message": "URL 1С не задан в настройках"}
    try:
        with _client(s) as c:
            r = c.get("$metadata", timeout=10)
        if r.status_code == 200:
            return {"ok": True, "message": "Подключение успешно"}
        if r.status_code == 401:
            return {"ok": False, "message": "Неверный логин или пароль (HTTP 401)"}
        return {"ok": False, "message": f"HTTP {r.status_code}: {r.text[:300]}"}
    except httpx.ConnectError as e:
        return {"ok": False, "message": f"Не удалось подключиться: {e}"}
    except httpx.TimeoutException:
        return {"ok": False, "message": "Таймаут подключения (>10 с)"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


# ── Номенклатура: 1С → TMS ───────────────────────────────────────────────────

def sync_products_from_1c(db: Session) -> dict:
    """
    Читает Catalog_Номенклатура из 1С, создаёт/обновляет Products в TMS.
    Маппинг: Ref_Key→external_id_1c, Code→article, Description→name.
    Пропускает записи с ПометкаУдаления=true.
    Возвращает {"created": N, "updated": N, "errors": [...]}.
    """
    s = _get_settings(db)
    if not s or not s.onec_enabled:
        return {"created": 0, "updated": 0, "errors": ["Синхронизация отключена"]}

    errors: list[str] = []
    created = updated = 0

    try:
        with _client(s) as c:
            r = c.get(
                "Catalog_Номенклатура",
                params={
                    "$format": "json",
                    "$select": "Ref_Key,Code,Description,DeletionMark",
                    "$top": "5000",
                },
            )
        r.raise_for_status()
        # Фильтруем помеченные на удаление в Python — булевые фильтры в OData УНФ нестабильны
        items = [i for i in r.json().get("value", []) if not i.get("DeletionMark", False)]
    except Exception as e:
        logger.error("sync_products_from_1c: %s", e)
        return {"created": 0, "updated": 0, "errors": [str(e)]}

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for item in items:
        ref_key = item.get("Ref_Key")
        name = (item.get("Description") or "").strip()
        code = (item.get("Code") or "").strip()

        if not ref_key or not name:
            continue

        try:
            # Ищем по GUID 1С
            p = db.query(Product).filter(Product.external_id_1c == ref_key).first()
            if p:
                p.name = name
                if code:
                    p.article = code
                p.synced_from_1c_at = now
                updated += 1
            else:
                # Пытаемся связать по артикулу
                p = db.query(Product).filter(Product.article == code).first() if code else None
                if p:
                    p.external_id_1c = ref_key
                    p.synced_from_1c_at = now
                    updated += 1
                else:
                    # Пытаемся связать по имени (для продуктов созданных до интеграции)
                    p = db.query(Product).filter(Product.name == name, Product.external_id_1c == None).first()
                    if p:
                        p.external_id_1c = ref_key
                        if code:
                            p.article = code
                        p.synced_from_1c_at = now
                        updated += 1
                    else:
                        db.add(Product(
                            name=name,
                            article=code or None,
                            external_id_1c=ref_key,
                            synced_from_1c_at=now,
                            is_active=True,
                        ))
                        created += 1
        except Exception as e:
            errors.append(f"{name}: {e}")
            logger.warning("sync_products_from_1c item error: %s", e)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        errors.append(f"commit: {e}")
        created = updated = 0

    logger.info(
        "sync_products_from_1c: создано %d, обновлено %d, ошибок %d",
        created, updated, len(errors),
    )
    return {"created": created, "updated": updated, "errors": errors}


# ── Контрагенты: TMS → 1С ───────────────────────────────────────────────────

_ENTITY_TYPE_MAP = {
    "ooo":   "ЮрЛицо",
    "ip":    "ИндивидуальныйПредприниматель",
    "other": "ФизЛицо",
}


def push_counterparty(cp, db: Session) -> str | None:
    """
    Создаёт или обновляет контрагента в 1С.
    Поиск дубля по ИНН перед созданием.
    Возвращает Ref_Key (GUID) или None при ошибке.
    """
    s = _get_settings(db)
    if not s or not s.onec_enabled:
        return None

    # Минимальный payload — только поля гарантированно существующие в УНФ OData
    payload: dict = {"Description": cp.name}
    if cp.inn:
        payload["ИНН"] = cp.inn

    try:
        with _client(s) as c:
            # Если уже привязан — просто обновляем
            if cp.external_id_1c:
                c.patch(f"Catalog_Контрагенты(guid'{cp.external_id_1c}')", json=payload)
                _save_external_id(db, cp, cp.external_id_1c)
                return cp.external_id_1c

            # Ищем по ИНН: OData-фильтр не работает в УНФ — тянем всех и ищем в Python
            if cp.inn:
                r = c.get(
                    "Catalog_Контрагенты",
                    params={"$format": "json", "$select": "Ref_Key,ИНН", "$top": "2000"},
                )
                if r.is_success:
                    for item in r.json().get("value", []):
                        if item.get("ИНН") == cp.inn:
                            ref_key = item["Ref_Key"]
                            logger.info("push_counterparty %s: найден в 1С по ИНН → %s", cp.id, ref_key)
                            _save_external_id(db, cp, ref_key)
                            return ref_key

            # Создаём нового
            r = c.post("Catalog_Контрагенты", json=payload)
            r.raise_for_status()
            ref_key = r.json().get("Ref_Key")
            if ref_key:
                _save_external_id(db, cp, ref_key)
            return ref_key
    except Exception as e:
        logger.error("push_counterparty %s: %s", cp.id, e)
        return None


# ── Заказы: TMS → 1С ─────────────────────────────────────────────────────────

def push_order(order, db: Session) -> str | None:
    """
    Создаёт/обновляет Document_ЗаказПокупателя в 1С.
    Вызывать при status='confirmed'. При повторных сменах статуса — PATCH.
    """
    s = _get_settings(db)
    if not s or not s.onec_enabled:
        return None

    if not order.counterparty:
        logger.warning("push_order %s: нет контрагента", order.id)
        return None
    if not order.counterparty.external_id_1c:
        logger.info("push_order %s: контрагент без external_id_1c — пушим сначала", order.id)
        push_counterparty(order.counterparty, db)
    if not order.counterparty.external_id_1c:
        logger.warning("push_order %s: контрагент не удалось создать в 1С", order.id)
        return None

    # Табличная часть Запасы в УНФ OData не поддерживает запись через POST/PATCH —
    # передаём состав в Комментарий чтобы 1С-операторы видели позиции
    lines = [f"TMS заказ #{order.number}"]
    for item in order.items:
        name = item.product.name if item.product else "—"
        qty = item.quantity
        price = item.price
        lines.append(f"  {name}: {qty} шт × {price}")
    comment = "\n".join(lines)

    payload = {
        "Date": order.date.isoformat() if order.date else None,
        "Контрагент_Key": order.counterparty.external_id_1c,
        "Комментарий": comment,
    }

    try:
        with _client(s) as c:
            if order.external_id_1c:
                c.patch(f"Document_ЗаказПокупателя(guid'{order.external_id_1c}')", json=payload)
                _save_external_id(db, order, order.external_id_1c)
                return order.external_id_1c
            r = c.post("Document_ЗаказПокупателя", json=payload)
            r.raise_for_status()
            ref_key = r.json().get("Ref_Key")
            if ref_key:
                _save_external_id(db, order, ref_key)
            return ref_key
    except Exception as e:
        logger.error("push_order %s: %s", order.id, e)
        return None


# ── Счета: TMS → 1С ──────────────────────────────────────────────────────────

def push_invoice(invoice, db: Session) -> str | None:
    """
    Создаёт Document_СчётНаОплатуПокупателю в 1С при переводе в статус 'issued'.
    """
    s = _get_settings(db)
    if not s or not s.onec_enabled:
        return None

    if not invoice.counterparty:
        logger.warning("push_invoice %s: нет контрагента", invoice.id)
        return None
    if not invoice.counterparty.external_id_1c:
        logger.info("push_invoice %s: контрагент без external_id_1c — пушим сначала", invoice.id)
        push_counterparty(invoice.counterparty, db)
    if not invoice.counterparty.external_id_1c:
        logger.warning("push_invoice %s: контрагент не удалось создать в 1С", invoice.id)
        return None

    items_payload = [
        {
            "Наименование": item.name,
            "Количество": item.quantity,
            "Цена": item.price,
            "СтавкаНДС": "20%" if (item.vat_rate or 0) >= 20 else "Без НДС",
        }
        for item in invoice.items
    ]

    payload = {
        "Номер": invoice.number,
        "Date": invoice.date.isoformat() if invoice.date else None,
        "Контрагент_Key": invoice.counterparty.external_id_1c,
        "ДатаОплаты": invoice.due_date.isoformat() if invoice.due_date else None,
        "ТоварыУслуги": items_payload,
    }

    try:
        with _client(s) as c:
            if invoice.external_id_1c:
                c.patch(f"Document_СчётНаОплатуПокупателю(guid'{invoice.external_id_1c}')", json=payload)
                _save_external_id(db, invoice, invoice.external_id_1c)
                return invoice.external_id_1c
            r = c.post("Document_СчётНаОплатуПокупателю", json=payload)
            r.raise_for_status()
            ref_key = r.json().get("Ref_Key")
            if ref_key:
                _save_external_id(db, invoice, ref_key)
            return ref_key
    except Exception as e:
        logger.error("push_invoice %s: %s", invoice.id, e)
        return None


# ── Оплаты: 1С → TMS ─────────────────────────────────────────────────────────

def sync_payments_from_1c(db: Session) -> dict:
    """
    Читает Document_ПоступлениеДенежныхСредств из 1С за последние 30 дней.
    Обновляет Invoice.status='paid', paid_date по external_id_1c основания.
    """
    s = _get_settings(db)
    if not s or not s.onec_enabled:
        return {"updated": 0, "errors": ["Синхронизация отключена"]}

    from datetime import date, timedelta
    from app.models import Invoice

    errors: list[str] = []
    updated = 0
    horizon = (date.today() - timedelta(days=30)).isoformat() + "T00:00:00"

    try:
        with _client(s) as c:
            r = c.get(
                "Document_ПоступлениеДенежныхСредств",
                params={
                    "$format": "json",
                    "$filter": f"Date ge datetime'{horizon}' and Posted eq true",
                    "$select": "Ref_Key,Date,Основание_Key",
                    "$top": "500",
                },
            )
        r.raise_for_status()
        payments = r.json().get("value", [])
    except Exception as e:
        logger.error("sync_payments_from_1c: %s", e)
        return {"updated": 0, "errors": [str(e)]}

    for pay in payments:
        basis_key = pay.get("Основание_Key")
        if not basis_key:
            continue
        try:
            inv = db.query(Invoice).filter(Invoice.external_id_1c == basis_key).first()
            if inv and inv.status != "paid":
                inv.status = "paid"
                pay_date_str = pay.get("Date", "")[:10]
                try:
                    from datetime import date as _d
                    inv.paid_date = _d.fromisoformat(pay_date_str)
                except (ValueError, TypeError):
                    pass
                updated += 1
        except Exception as e:
            errors.append(str(e))

    if updated:
        try:
            db.commit()
        except Exception as e:
            db.rollback()
            errors.append(f"commit: {e}")
            updated = 0

    logger.info("sync_payments_from_1c: обновлено %d", updated)
    return {"updated": updated, "errors": errors}


# ── Склад: TMS → 1С ──────────────────────────────────────────────────────────

def push_stock_movement(movement, db: Session) -> str | None:
    """
    Пушит движения типа 'in' (Document_ПоступлениеТоваров)
    и 'adjustment' (Document_ИнвентаризацияТоваров).
    Движения 'out' с order_id пропускаются — 1С создаёт их сама через заказ.
    """
    if movement.movement_type == "out" and movement.order_id:
        return None

    s = _get_settings(db)
    if not s or not s.onec_enabled:
        return None

    if not movement.product or not movement.product.external_id_1c:
        return None

    doc_type = (
        "Document_ПоступлениеТоваров"
        if movement.movement_type == "in"
        else "Document_ИнвентаризацияТоваров"
    )

    payload = {
        "Date": movement.date.isoformat() if movement.date else None,
        "Комментарий": movement.notes or "",
        "Товары": [{
            "Номенклатура_Key": movement.product.external_id_1c,
            "Количество": movement.quantity,
        }],
    }

    try:
        with _client(s) as c:
            if movement.external_id_1c:
                c.patch(f"{doc_type}(guid'{movement.external_id_1c}')", json=payload)
                _save_external_id(db, movement, movement.external_id_1c)
                return movement.external_id_1c
            r = c.post(doc_type, json=payload)
            r.raise_for_status()
            ref_key = r.json().get("Ref_Key")
            if ref_key:
                _save_external_id(db, movement, ref_key)
            return ref_key
    except Exception as e:
        logger.error("push_stock_movement %s: %s", movement.id, e)
        return None

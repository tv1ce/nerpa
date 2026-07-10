"""
Эндпоинты СБИС ЭПД/ЭТРН.

ЭТРН в СБИС — это процесс из 4 «титулов», которые создают и подписывают
разные стороны (грузоотправитель → перевозчик → грузополучатель → перевозчик).
Точная схема полей для каждого титула не описана в публичной документации,
поэтому TMS только создаёт черновик документа с базовыми реквизитами —
остальное (титулы, подписание) оформляется вручную в личном кабинете СБИС.

POST /api/sbis/etran/{order_id}        — создать черновик ЭТРН
GET  /api/sbis/etran/{order_id}/status — опросить статус из СБИС
POST /api/sbis/webhook                 — приём уведомлений от СБИС
"""
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth import login_required, role_required
from app.database import get_db
from app.models import CompanySettings, Order, Invoice, AttachedFile
from app.services.sbis_client import SbisError, get_sbis_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sbis", tags=["sbis"])


def _json(ok: bool, **kw):
    return JSONResponse({"ok": ok, **kw})


def _doc_id(result: dict) -> str:
    return (result.get("Идентификатор") or result.get("id")
            or (result.get("Документ") or {}).get("Идентификатор") or "")


# ── Проверка подключения ────────────────────────────────────────────────────

@router.get("/test")
@role_required("admin")
async def test_connection(request: Request, db: Session = Depends(get_db)):
    company = db.query(CompanySettings).first()
    client = get_sbis_client(company)
    if not client:
        return _json(False, message="СБИС не настроен — укажите логин и пароль")
    try:
        with client:
            client.authenticate()
        return _json(True, message="Авторизация успешна")
    except SbisError as e:
        return _json(False, message=str(e))
    except Exception as e:
        return _json(False, message=f"Ошибка: {e}")


# ── Создать/отправить ЭТРН ──────────────────────────────────────────────────

@router.post("/etran/{order_id}")
@role_required("manager")
async def create_etran(
    request: Request,
    order_id: int,
    db: Session = Depends(get_db),
):
    company = db.query(CompanySettings).first()
    order   = db.query(Order).filter(Order.id == order_id).first()

    if not order:
        return _json(False, error="Заказ не найден")

    client = get_sbis_client(company)
    if not client:
        return _json(False, error="СБИС не настроен — укажите логин и пароль в Настройках → Интеграции")

    try:
        with client:
            result = client.create_etran(order, company)
            etran_id = result.get("id")
            if not etran_id:
                return _json(False, error="СБИС не вернул ID документа", raw=result.get("raw"))

        order.etran_id     = etran_id
        order.etran_status = "черновик"
        order.etran_url    = result.get("url") or f"https://online.sbis.ru/opendoc.html?guid={etran_id}"
        db.commit()

        return _json(
            True,
            etran_id=etran_id,
            url=order.etran_url,
            message="Черновик ЭТРН создан в СБИС. Титулы и подписание — в личном кабинете СБИС по ссылке.",
        )
    except SbisError as e:
        logger.error("СБИС ЭТРН ошибка для заказа #%s: %s", order.number, e)
        order.etran_status = "ошибка"
        db.commit()
        return _json(False, error=str(e))
    except Exception as e:
        logger.exception("ЭТРН unexpected error order_id=%s", order_id)
        return _json(False, error=f"Непредвиденная ошибка: {e}")


# ── Опросить статус ─────────────────────────────────────────────────────────

@router.get("/etran/{order_id}/status")
@login_required
async def etran_status(
    request: Request,
    order_id: int,
    db: Session = Depends(get_db),
):
    company = db.query(CompanySettings).first()
    order   = db.query(Order).filter(Order.id == order_id).first()

    if not order:
        return _json(False, error="Заказ не найден")
    if not order.etran_id:
        return _json(False, error="ЭТРН не создан")

    client = get_sbis_client(company)
    if not client:
        return _json(False, error="СБИС не настроен")

    try:
        with client:
            status = client.get_status(order.etran_id)
        order.etran_status = status
        db.commit()
        return _json(True, status=status, etran_id=order.etran_id, url=order.etran_url)
    except SbisError as e:
        return _json(False, error=str(e))


# ── ЭДО: счёт на оплату ──────────────────────────────────────────────────────

@router.post("/invoice/{invoice_id}")
@role_required("manager")
async def create_invoice_edo(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    """Создаёт документ-счёт в СБИС ЭДО. Вложение — файл счёта, ПРИКРЕПЛЁННЫЙ к
    заказу (из 1С), а не сгенерированный в TMS. Данные (контрагент, номер, сумма)
    передаются на уровне документа. Черновик — подпись и отправка в кабинете СБИС."""
    import base64, os
    company = db.query(CompanySettings).first()
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        return _json(False, error="Счёт не найден")
    if not invoice.order_id:
        return _json(False, error="Счёт не привязан к заказу — нет прикреплённого файла счёта")

    # Файл счёта, прикреплённый к заказу (из 1С), берём самый свежий
    inv_file = (
        db.query(AttachedFile)
        .filter(AttachedFile.entity_type == "order",
                AttachedFile.entity_id == invoice.order_id,
                AttachedFile.file_type == "invoice")
        .order_by(AttachedFile.uploaded_at.desc())
        .first()
    )
    if not inv_file or not os.path.exists(inv_file.stored_path):
        return _json(False, error="Нет прикреплённого к заказу файла счёта (из 1С). Сначала получите счёт из 1С.")

    client = get_sbis_client(company)
    if not client:
        return _json(False, error="СБИС не настроен — укажите логин и пароль в Настройках → Интеграции")

    try:
        with open(inv_file.stored_path, "rb") as f:
            file_bytes = f.read()
        b64 = base64.b64encode(file_bytes).decode("ascii")
        filename = inv_file.original_name or f"Счет № {invoice.number}.pdf"
        doc_fields = {
            "Номер": invoice.number or "",
            "Дата": invoice.date.strftime("%d.%m.%Y") if invoice.date else "",
            "Сумма": f"{invoice.total_amount:.2f}",
            "СуммаБезНДС": f"{invoice.subtotal:.2f}",
            "Примечание": f"Счёт № {invoice.number}",
            "Контрагент": client.kontragent_block(invoice.counterparty),
            "НашаОрганизация": client.nasha_org_block(company),
        }
        with client:
            result = client.write_edo_document("СчетИсх", "ЭДОСч", b64, filename, doc_fields)
        doc_id = _doc_id(result)
        if not doc_id:
            return _json(False, error="СБИС не вернул идентификатор документа", raw=result)
        invoice.sbis_doc_id = doc_id
        invoice.sbis_status = "черновик"
        invoice.sbis_url = client.doc_link(doc_id)
        db.commit()
        return _json(True, id=doc_id, url=invoice.sbis_url,
                     message="Счёт создан в СБИС. Подпишите и отправьте контрагенту в кабинете СБИС.")
    except SbisError as e:
        logger.error("СБИС счёт #%s: %s", invoice.number, e)
        invoice.sbis_status = "ошибка"
        db.commit()
        return _json(False, error=str(e))
    except Exception as e:
        logger.exception("СБИС счёт unexpected error invoice_id=%s", invoice_id)
        return _json(False, error=f"Непредвиденная ошибка: {e}")


# ── ЭДО: УПД (формализованный XML из 1С) ─────────────────────────────────────

@router.post("/upd/{order_id}")
@role_required("manager")
async def create_upd_edo(request: Request, order_id: int, db: Session = Depends(get_db)):
    """Создаёт документ-УПД в СБИС ЭДО из формализованного XML, полученного из 1С
    (файл заказа типа upd_xml). Черновик — подписание/отправка в кабинете СБИС."""
    import base64, os
    company = db.query(CompanySettings).first()
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return _json(False, error="Заказ не найден")

    xml_file = (
        db.query(AttachedFile)
        .filter(AttachedFile.entity_type == "order",
                AttachedFile.entity_id == order.id,
                AttachedFile.file_type == "upd_xml")
        .order_by(AttachedFile.uploaded_at.desc())
        .first()
    )
    if not xml_file or not os.path.exists(xml_file.stored_path):
        return _json(False, error="Нет файла УПД (XML) — он приходит из 1С. Сначала получите УПД из 1С.")

    client = get_sbis_client(company)
    if not client:
        return _json(False, error="СБИС не настроен — укажите логин и пароль в Настройках → Интеграции")

    try:
        with open(xml_file.stored_path, "rb") as f:
            xml_bytes = f.read()
        b64 = base64.b64encode(xml_bytes).decode("ascii")
        filename = xml_file.original_name or f"УПД {order.number}.xml"
        doc_fields = {
            "Номер": order.number or "",
            "Дата": order.date.strftime("%d.%m.%Y") if order.date else "",
            "Примечание": f"УПД по заказу № {order.number}",
            "Контрагент": client.kontragent_block(order.counterparty),
            "НашаОрганизация": client.nasha_org_block(company),
        }
        with client:
            result = client.write_edo_document("ДокОтгрИсх", "УпдСчфДоп", b64, filename, doc_fields)
        doc_id = _doc_id(result)
        if not doc_id:
            return _json(False, error="СБИС не вернул идентификатор документа", raw=result)
        order.upd_sbis_id = doc_id
        order.upd_sbis_status = "черновик"
        order.upd_sbis_url = client.doc_link(doc_id)
        db.commit()
        return _json(True, id=doc_id, url=order.upd_sbis_url,
                     message="УПД создан в СБИС. Подпишите и отправьте контрагенту в кабинете СБИС.")
    except SbisError as e:
        logger.error("СБИС УПД заказ #%s: %s", order.number, e)
        order.upd_sbis_status = "ошибка"
        db.commit()
        return _json(False, error=str(e))
    except Exception as e:
        logger.exception("СБИС УПД unexpected error order_id=%s", order_id)
        return _json(False, error=f"Непредвиденная ошибка: {e}")


# ── Вебхук от СБИС ─────────────────────────────────────────────────────────

@router.post("/webhook")
async def sbis_webhook(request: Request, db: Session = Depends(get_db)):
    """Принимает уведомления СБИС о смене статуса ЭТРН."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

    # Структура нотификации СБИС:  {"event": "...", "document": {"id": "...", "status": "..."}}
    doc = body.get("document") or body.get("Документ") or {}
    etran_id  = doc.get("id") or doc.get("Идентификатор")
    raw_status = (
        doc.get("status")
        or doc.get("Состояние")
        or body.get("event")
        or "unknown"
    )

    if not etran_id:
        logger.warning("СБИС вебхук: нет ID документа — %s", body)
        return JSONResponse({"ok": True})  # отвечаем 200, чтобы СБИС не ретраил

    from app.services.sbis_client import ETRAN_STATUS_MAP
    status = ETRAN_STATUS_MAP.get(raw_status.lower(), raw_status)

    order = db.query(Order).filter(Order.etran_id == etran_id).first()
    if order:
        order.etran_status = status
        db.commit()
        logger.info("СБИС вебхук: заказ #%s статус → %s", order.number, status)

    return JSONResponse({"ok": True})

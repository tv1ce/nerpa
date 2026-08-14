"""Приём документов из 1С (push, вариант A).

Внешняя обработка 1С (.epf) формирует печатную форму Счёта/УПД и шлёт её сюда.
Аутентификация — статический ключ в заголовке X-API-Key (значение из env
ONEC_PUSH_KEY). Тело запроса — сырые байты файла (PDF/XML), метаданные — в
query-параметрах: так на стороне 1С не нужно собирать multipart вручную.

Заказ в NERPA определяется по одному из GUID 1С (в порядке приоритета):
  order_ref     — Document_ЗаказПокупателя.Ref_Key  → Order.external_id_1c
  shipment_ref  — Document_РасходнаяНакладная.Ref_Key → Order.shipment_id_1c
  invoice_ref   — Document_СчетНаОплату.Ref_Key       → Invoice.external_id_1c → заказ

Файл сохраняется как вложение заказа через тот же _save_order_file(), что и
pull-конвейер: идемпотентно по external_key, PDF сжимается, source='1c'.
"""
import logging
import os
import secrets

from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Order, Invoice
from app.services.onec_client import _save_order_file
from app.utils import log_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/1c", tags=["api_1c"])

# Типы файлов заказа (синхронно с FILE_TYPES['order'] в routers/files.py)
_ALLOWED_TYPES = {"invoice", "upd", "upd_xml", "tn", "other"}
_MAX_BYTES = 25 * 1024 * 1024  # 25 МБ


def _check_key(request: Request) -> bool:
    """Сверяет X-API-Key с ONEC_PUSH_KEY из окружения (constant-time)."""
    expected = os.environ.get("ONEC_PUSH_KEY", "")
    if not expected:
        return False  # ключ не сконфигурирован — приём выключен
    got = request.headers.get("X-API-Key", "")
    return bool(got) and secrets.compare_digest(got, expected)


@router.post("/order-document")
async def receive_order_document(request: Request, db: Session = Depends(get_db)):
    """Принимает файл документа из 1С и прикладывает к заказу NERPA."""
    # 1. Аутентификация
    if not os.environ.get("ONEC_PUSH_KEY"):
        return JSONResponse({"ok": False, "error": "ONEC_PUSH_KEY не задан на сервере"},
                            status_code=503)
    if not _check_key(request):
        return JSONResponse({"ok": False, "error": "Неверный или отсутствует X-API-Key"},
                            status_code=401)

    qp = request.query_params
    file_type = (qp.get("file_type") or "other").strip()
    if file_type not in _ALLOWED_TYPES:
        file_type = "other"
    order_ref = (qp.get("order_ref") or "").strip() or None
    shipment_ref = (qp.get("shipment_ref") or "").strip() or None
    invoice_ref = (qp.get("invoice_ref") or "").strip() or None
    filename = (qp.get("filename") or "").strip()

    # 2. Тело — сырые байты файла
    data = await request.body()
    if not data:
        return JSONResponse({"ok": False, "error": "Пустое тело запроса (нет файла)"},
                            status_code=400)
    if len(data) > _MAX_BYTES:
        return JSONResponse({"ok": False, "error": "Файл больше 25 МБ"}, status_code=413)

    # 3. Определяем заказ по GUID 1С (приоритет: заказ → расходная → счёт)
    order = None
    key_ref = None
    if order_ref:
        order = db.query(Order).filter(Order.external_id_1c == order_ref).first()
        key_ref = order_ref
    if not order and shipment_ref:
        order = db.query(Order).filter(Order.shipment_id_1c == shipment_ref).first()
        key_ref = shipment_ref
    if not order and invoice_ref:
        inv = db.query(Invoice).filter(Invoice.external_id_1c == invoice_ref).first()
        if inv and inv.order_id:
            order = db.query(Order).filter(Order.id == inv.order_id).first()
        key_ref = invoice_ref

    if not order:
        # Заказ ещё не синхронизирован в NERPA — отдаём понятную 1С ошибку
        return JSONResponse(
            {"ok": False, "error": "Заказ не найден в NERPA по переданным GUID. "
             "Проверьте, что заказ выгружен из NERPA в 1С (есть external_id_1c)."},
            status_code=404,
        )

    # 4. Имя и расширение файла
    ext = ".xml" if file_type == "upd_xml" else (os.path.splitext(filename)[1].lower() or ".pdf")
    if ext not in (".pdf", ".xml"):
        ext = ".xml" if file_type == "upd_xml" else ".pdf"
    if not filename:
        labels = {"invoice": "Счет", "upd": "УПД", "upd_xml": "УПД", "tn": "ТН", "other": "Документ"}
        filename = f"{labels.get(file_type, 'Документ')} {order.number}{ext}"

    external_key = f"{key_ref}:{file_type}"

    # 5. Сохраняем вложение (идемпотентно, PDF сжимается внутри)
    try:
        _save_order_file(
            db, order,
            file_type=file_type, ext=ext, external_key=external_key,
            data=data, original_name=filename, source="1c",
        )
        log_action(db, "order", order.id, "updated", None,
                   f"Из 1С получен документ: {filename}")
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.error("receive_order_document: %s", e)
        return JSONResponse({"ok": False, "error": f"Ошибка сохранения: {e}"}, status_code=500)

    logger.info("1С→NERPA: заказ #%s получен %s (%s байт)", order.number, file_type, len(data))
    return JSONResponse({
        "ok": True,
        "order_id": order.id,
        "order_number": order.number,
        "file_type": file_type,
        "bytes": len(data),
    })

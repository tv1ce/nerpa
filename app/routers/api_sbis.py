"""
Эндпоинты СБИС ЭПД/ЭТРН.

POST /api/sbis/etran/{order_id}        — создать + отправить ЭТРН
GET  /api/sbis/etran/{order_id}/status — опросить статус из СБИС
POST /api/sbis/webhook                 — приём уведомлений от СБИС
"""
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth import login_required, role_required
from app.database import get_db
from app.models import CompanySettings, Order
from app.services.sbis_client import SbisError, get_sbis_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sbis", tags=["sbis"])


def _json(ok: bool, **kw):
    return JSONResponse({"ok": ok, **kw})


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

            # Отправляем на подписание сразу
            client.send_etran(etran_id)

        order.etran_id     = etran_id
        order.etran_status = "отправлен"
        order.etran_url    = result.get("url") or f"https://online.sbis.ru/opendoc.html?guid={etran_id}"
        db.commit()

        return _json(
            True,
            etran_id=etran_id,
            url=order.etran_url,
            message="ЭТРН создан и отправлен на подписание",
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

"""
Saby «Управление транспортом» — заказы на перевозку (ЭЗЗ) и ЭТрН через tms.saby.ru.

Пока — только проверка связи: подтверждаем, что сессия СБИС (логин/пароль из
Настроек) валидна против выделенного транспортного эндпоинта tms.saby.ru/service/.
Read-only, ничего не создаёт — безопасно на боевом аккаунте.

GET /api/saby-tms/test — авторизация + СБИС.СписокИзменений (последний месяц), admin
"""
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth import role_required
from app.database import get_db
from app.models import CompanySettings
from app.services.saby_tms_client import (
    SabyTmsError, get_saby_tms_client, DOC_TRANSPORT_ORDER,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/saby-tms", tags=["saby_tms"])


def _json(ok: bool, **kw):
    return JSONResponse({"ok": ok, **kw})


@router.get("/test")
@role_required("admin")
async def test_connection(request: Request, db: Session = Depends(get_db)):
    """Проверка связи с транспортным API Saby: авторизация + чтение списка заказов
    на перевозку за последний месяц. Ничего не создаёт и не отправляет."""
    company = db.query(CompanySettings).first()
    client = get_saby_tms_client(company)
    if not client:
        return _json(False, message="СБИС не настроен — укажите логин и пароль в Настройках → Интеграции")
    try:
        with client:
            client.authenticate()
            result = client.list_changes(DOC_TRANSPORT_ORDER, page_size=1)
        docs = (result or {}).get("Документ") or []
        return _json(
            True,
            message="Связь с Saby «Управление транспортом» есть, сессия принята транспортным эндпоинтом.",
            sample_count=len(docs),
        )
    except SabyTmsError as e:
        return _json(False, message=str(e))
    except Exception as e:
        logger.exception("Saby TMS test error")
        return _json(False, message=f"Ошибка: {e}")

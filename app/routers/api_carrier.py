"""Приём подтверждений доставки от «Помощника логиста» (внешнего Telegram-бота).

Зачем отдельный вход: Telegram не отдаёт боту сообщения, написанные другим
ботом, поэтому NERPA-бот не видит строки «✅ <адрес> | 🟢 <время>», которые
публикует бот-помощник в группе перевозчика. Помощник дублирует их сюда HTTP-
вызовом, а NERPA делает ровно то же, что делал бы по сообщению в чате.

Вызов (ключ — общий секрет из CARRIER_PUSH_KEY в .env):

    POST /api/carrier/delivery?key=<CARRIER_PUSH_KEY>
    {"chat_id": "-1003967668906", "text": "✅ пр. Космонавтов 14 | 🟢 До 12"}

Вместо `text` можно прислать готовый `address` — тогда разбор пропускается.
Вместо `chat_id` можно указать `carrier_id` (id контрагента-перевозчика в NERPA).

Ответ: {"ok": true, "order": "80", "reason": "ok", "message": "…"} либо
{"ok": false, "reason": "no_match|ambiguous|not_confirmation|unknown_carrier", …}.
Ответ всегда 200, кроме проблем с ключом и телом запроса, — помощнику важно не
падать, а показать причину.
"""
import logging
import os
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.carrier_delivery import (
    confirm_delivery, find_carrier_by_chat, parse_delivery_confirmation,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/carrier", tags=["api_carrier"])


def _check_key(request: Request) -> bool:
    """Общий секрет — в query `key` или заголовке X-TMS-Key (как удобнее помощнику)."""
    expected = os.environ.get("CARRIER_PUSH_KEY", "")
    if not expected:
        return False
    got = request.query_params.get("key", "") or request.headers.get("x-tms-key", "")
    return bool(got) and secrets.compare_digest(got, expected)


@router.post("/delivery")
async def delivery_confirm(request: Request, db: Session = Depends(get_db)):
    if not os.environ.get("CARRIER_PUSH_KEY"):
        return JSONResponse({"ok": False, "error": "CARRIER_PUSH_KEY не задан на сервере"},
                            status_code=503)
    if not _check_key(request):
        return JSONResponse({"ok": False, "error": "Неверный или отсутствующий key"},
                            status_code=401)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "Ожидается JSON-объект"}, status_code=400)

    chat_id = str(body.get("chat_id") or "").strip()
    carrier_id = body.get("carrier_id")
    text = (body.get("text") or "").strip()
    address = (body.get("address") or "").strip()

    logger.info("api_carrier: chat_id=%s carrier_id=%s text=%r address=%r",
                chat_id or "—", carrier_id or "—", text[:120], address)

    # Перевозчик: по чату (как в боте) либо по прямому id
    carrier = None
    if chat_id:
        carrier = find_carrier_by_chat(db, chat_id)
    elif carrier_id:
        from app.models import Counterparty
        carrier = db.query(Counterparty).filter(Counterparty.id == carrier_id).first()
    if not carrier:
        return JSONResponse({
            "ok": False, "reason": "unknown_carrier",
            "message": "Перевозчик не найден: chat_id не привязан ни к одному контрагенту "
                       "(поле «Telegram chat ID» в карточке) либо неверный carrier_id",
        })

    # Адрес: готовый или разобранный из текста сообщения
    if not address:
        address = parse_delivery_confirmation(text) or ""
    if not address:
        return JSONResponse({
            "ok": False, "reason": "not_confirmation",
            "message": "Это не подтверждение доставки: нужны ✅ и 🟢 в тексте "
                       "(🔴 — не доставлено) либо явное поле address",
        })

    result = confirm_delivery(db, carrier, address)
    return JSONResponse({
        "ok": result.ok,
        "reason": result.reason,
        "message": result.message,
        "order": result.order_number,
        "order_id": result.order_id,
        "address": address,
    })

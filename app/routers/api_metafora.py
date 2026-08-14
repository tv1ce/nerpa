"""Приём статусов перевозки от перевозчика (вебхук из Метафоры).

Перевозчик ведёт рейсы в Метафоре и на смене статуса дёргает этот эндпоинт —
NERPA находит заказ и переводит его в соответствующий статус. Спецификация для
перевозчика: docs/metafora_webhook.md.

    POST /api/metafora/status?key=<METAFORA_PUSH_KEY>
    {"order": "80", "status": "доставлено"}

Эндпоинт намеренно терпим к формату: у Метафоры/Glide поля называются
по-разному в зависимости от того, как перевозчик соберёт сценарий, поэтому
принимаются синонимы (`order` / `order_number` / `номер_заказа` / …), а сырое
тело всегда пишется в лог — по нему можно донастроить разбор, не дёргая
перевозчика второй раз.

Заказ ищется сначала по номеру NERPA (надёжно), затем по адресу доставки среди
активных заказов. Неоднозначность (несколько заказов по одному адресу) статус
не меняет — такое разбирает человек.
"""
import logging
import os
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.carrier_delivery import (
    apply_status, find_order_by_number, find_orders_by_address,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/metafora", tags=["api_metafora"])

# Синонимы полей: что бы перевозчик ни назвал в сценарии Метафоры — поймём
_F_ORDER = ("order", "order_number", "orderNumber", "number", "номер", "номер_заказа", "заказ")
_F_ADDR = ("address", "delivery_address", "deliveryAddress", "адрес", "адрес_доставки", "точка")
_F_STATUS = ("status", "state", "статус", "состояние")
_F_COMMENT = ("comment", "note", "комментарий", "примечание")
_F_CARRIER_INN = ("carrier_inn", "inn", "инн", "инн_перевозчика")

# Слово статуса → статус заказа NERPA. Сравнение по вхождению, регистр не важен.
_STATUS_MAP = {
    "delivered": (
        "доставлен", "доставлено", "доставили", "вручен", "вручено", "выполнен",
        "выполнено", "завершен", "завершён", "завершено", "сдан", "сдано",
        "delivered", "done", "completed", "complete", "finish",
    ),
    "handed": (
        "в пути", "впути", "забрал", "забрано", "принят", "принято", "отгружен",
        "отгружено", "погружен", "на доставке", "передан", "выехал", "в работе",
        "in_transit", "intransit", "in transit", "picked", "shipped", "transit",
    ),
}


def _check_key(request: Request) -> bool:
    """Общий секрет — в query `key` или заголовке X-TMS-Key (как удобнее перевозчику)."""
    expected = os.environ.get("METAFORA_PUSH_KEY", "")
    if not expected:
        return False
    got = request.query_params.get("key", "") or request.headers.get("x-tms-key", "")
    return bool(got) and secrets.compare_digest(got, expected)


def _pick(data: dict, names) -> str:
    """Первое непустое значение из синонимов поля (регистр ключа не важен)."""
    lowered = {str(k).strip().lower(): v for k, v in data.items()}
    for n in names:
        v = lowered.get(n.lower())
        if v not in (None, "", []):
            return str(v).strip()
    return ""


def _map_status(raw: str) -> str | None:
    """Слово перевозчика → статус NERPA. None, если статус нам неизвестен."""
    s = (raw or "").strip().lower().replace("ё", "е")
    if not s:
        return None
    for status, words in _STATUS_MAP.items():
        for w in words:
            if w.replace("ё", "е") in s:
                return status
    return None


@router.get("/ping")
async def ping(request: Request):
    """Проверка связи для перевозчика: ключ верный и эндпоинт жив."""
    if not os.environ.get("METAFORA_PUSH_KEY"):
        return JSONResponse({"ok": False, "error": "METAFORA_PUSH_KEY не задан на сервере"},
                            status_code=503)
    if not _check_key(request):
        return JSONResponse({"ok": False, "error": "Неверный или отсутствующий key"}, status_code=401)
    return JSONResponse({"ok": True, "message": "NERPA на связи, ключ верный"})


@router.post("/status")
async def status_webhook(request: Request, db: Session = Depends(get_db)):
    if not os.environ.get("METAFORA_PUSH_KEY"):
        return JSONResponse({"ok": False, "error": "METAFORA_PUSH_KEY не задан на сервере"},
                            status_code=503)
    if not _check_key(request):
        return JSONResponse({"ok": False, "error": "Неверный или отсутствующий key"}, status_code=401)

    # Тело: JSON-объект, JSON-массив (берём первый элемент) или форма
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        try:
            body = dict(await request.form())
        except Exception:  # noqa: BLE001
            body = {}
    if isinstance(body, list):
        body = body[0] if body and isinstance(body[0], dict) else {}
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "reason": "bad_body",
                             "message": "Ожидается JSON-объект"}, status_code=400)

    # Сырое тело в лог — по нему донастраиваем разбор, если Метафора шлёт иначе
    logger.info("metafora: входящий вебхук %s", body)

    number = _pick(body, _F_ORDER)
    address = _pick(body, _F_ADDR)
    raw_status = _pick(body, _F_STATUS)
    comment = _pick(body, _F_COMMENT)
    inn = _pick(body, _F_CARRIER_INN)

    status = _map_status(raw_status)
    if not status:
        return JSONResponse({
            "ok": False, "reason": "unknown_status",
            "message": f"Статус «{raw_status}» не распознан. Ожидаются «доставлено» "
                       f"(заказ закрывается) или «в пути» (заказ передан перевозчику). "
                       f"Отмену и возврат NERPA по вебхуку не проводит — это делает менеджер.",
        })

    # Перевозчик — только чтобы сузить поиск по адресу; по номеру заказа не нужен
    carrier = None
    if inn:
        from app.models import Counterparty
        carrier = db.query(Counterparty).filter(Counterparty.inn == inn).first()

    order = find_order_by_number(db, number) if number else None
    if not order and address:
        matches = find_orders_by_address(db, address, carrier=carrier)
        if len(matches) > 1:
            nums = ", ".join(f"№{o.number}" for o in matches)
            return JSONResponse({
                "ok": False, "reason": "ambiguous",
                "message": f"По адресу «{address}» несколько активных заказов ({nums}) — "
                           f"пришлите номер заказа в поле order, статус не изменён.",
            })
        order = matches[0] if matches else None

    if not order:
        return JSONResponse({
            "ok": False, "reason": "order_not_found",
            "message": "Заказ не найден: пришлите номер заказа NERPA в поле order "
                       "либо адрес доставки в поле address.",
            "got": {"order": number, "address": address},
        })

    source = "Метафора" + (f", {comment}" if comment else "")
    message = apply_status(db, order, status, source)
    return JSONResponse({
        "ok": True, "reason": "ok", "message": message,
        "order": order.number, "order_id": order.id, "status": status,
    })

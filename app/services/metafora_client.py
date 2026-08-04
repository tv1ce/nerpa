"""Клиент API «Метафоры» — создание заказа на доставку у перевозчика.

Спека перевозчика: docs/metafora_api.md. База: https://api.damasevich.ru,
авторизация — `Authorization: Bearer <токен>`, метод один: POST /orders.

Заказ отправляется структурными полями (форма 3б), а не текстом: у TMS все
поля уже разобраны, парсер Метафоры не нужен — быстрее и без риска, что он
что-то не так поймёт.

`external_id` — номер заказа TMS. Он же ключ идемпотентности (повтор с тем же
номером не задваивает заказ, приходит 409 — это успех) и он же возвращается
Метафорой в вебхуке статусов в поле `order`, по нему заказ и находится обратно
(см. app/routers/api_metafora.py).
"""
import logging
import os

import httpx
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

API_BASE = os.getenv("METAFORA_API_URL", "https://api.damasevich.ru").rstrip("/")
# Документация требует таймаут ≥60 с (текстовая форма идёт через парсер).
# Мы шлём структурными полями, но запас держим — сеть на стороне перевозчика.
TIMEOUT = 60


def get_token(db: Session) -> str:
    """Токен API Метафоры: настройки компании, затем .env."""
    from app.models import CompanySettings
    company = db.query(CompanySettings).first()
    tok = (getattr(company, "metafora_api_token", None) or "").strip() if company else ""
    return tok or os.getenv("METAFORA_API_TOKEN", "").strip()


def _phones(order) -> list[str]:
    """Телефоны из «телефон + имя получателя» («79119244416 Ольга» → 79119244416)."""
    import re
    raw = (order.delivery_contact or "")
    found = re.findall(r"\+?\d[\d\-\s()]{9,}", raw)
    return [re.sub(r"[^\d+]", "", f) for f in found][:2]


def _what_to_carry(order) -> str:
    """Что везём: явное имя груза, иначе состав заказа коротко."""
    if order.cargo_name:
        return order.cargo_name.strip()
    names = []
    for it in order.items:
        nm = it.product.name if it.product else None
        if nm:
            names.append(f"{nm} × {it.quantity:g}")
    text = "; ".join(names)
    return text[:300]


def build_order_payload(order) -> dict:
    """Заказ TMS → тело POST /orders (структурная форма)."""
    cp = order.counterparty
    payload = {
        "external_id": str(order.number),
        "service": "Доставка",
        "address": (order.delivery_address or "").strip(),
        "order_number": str(order.number),
    }
    if order.delivery_date:
        payload["date"] = order.delivery_date.isoformat()
    if order.delivery_time:
        payload["time"] = order.delivery_time.strip()

    phones = _phones(order)
    if phones:
        payload["phone_1"] = phones[0]
    if len(phones) > 1:
        payload["phone_2"] = phones[1]

    what = _what_to_carry(order)
    if what:
        payload["what_to_carry"] = what
    if order.cargo_places:
        payload["comment"] = f"Мест: {order.cargo_places}"
    if order.cargo_pallets:
        payload["comment"] = ((payload.get("comment", "") + ", ") if payload.get("comment") else "") \
                             + f"паллет: {order.cargo_pallets}"

    # Кому везём — логисту перевозчика это нужнее номера заказа
    if cp:
        payload["important"] = (cp.trade_name or cp.name or "").strip()[:200]
    # Имя получателя из «телефон + имя» — как найти на месте
    contact = (order.delivery_contact or "").strip()
    if contact:
        import re
        name_part = re.sub(r"\+?\d[\d\-\s()]{9,}", "", contact).strip(" ,;")
        if name_part:
            payload["how_to_find"] = name_part[:200]
    if order.notes:
        payload["comment"] = ((payload.get("comment", "") + ". ") if payload.get("comment") else "") \
                             + order.notes.strip()[:300]
    return payload


def push_order(payload: dict, token: str) -> dict:
    """Создаёт заказ в системе перевозчика. Возвращает {"ok", "message", ...}.

    Принимает готовое тело и токен (а не order/db), чтобы вызывающая сторона
    могла увести блокирующий HTTP в отдельный поток, не таща туда сессию БД.
    409 (duplicate) — успех: заказ с этим номером уже принят (см. спеку).
    """
    if not token:
        return {"ok": False, "error": "Токен API Метафоры не задан "
                                      "(Настройки → Интеграции или METAFORA_API_TOKEN в .env)"}

    number = payload.get("external_id", "?")
    if not payload.get("address"):
        return {"ok": False, "error": "В заказе не указан адрес доставки — Метафора его не примет"}

    try:
        r = httpx.post(
            f"{API_BASE}/orders",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("metafora push_order %s: %s", number, e)
        return {"ok": False, "error": f"Метафора недоступна: {e}"}

    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        data = {}

    logger.info("metafora push_order %s: HTTP %s %s", number, r.status_code, str(data)[:300])

    if r.status_code == 409:
        return {"ok": True, "duplicate": True,
                "message": f"Заказ №{number} уже был передан в Метафору"}
    if r.status_code == 200:
        status = (data.get("status") or "").lower()
        if status == "ok":
            warn = data.get("warnings") or []
            msg = f"Заказ №{number} создан в Метафоре"
            if warn:
                msg += " (замечания: " + "; ".join(str(w) for w in warn) + ")"
            return {"ok": True, "message": msg, "warnings": warn}
        # no_orders / parse_error — заказ НЕ создан
        return {"ok": False,
                "error": f"Метафора не приняла заказ ({status or 'без статуса'}): "
                         f"{data.get('note') or 'причина не указана'}"}
    if r.status_code == 401:
        return {"ok": False, "error": "Метафора: неверный или отсутствующий токен"}
    if r.status_code == 422:
        return {"ok": False, "error": f"Метафора: ошибка в данных заказа — {data.get('detail') or r.text[:200]}"}
    if r.status_code == 503:
        return {"ok": False, "retry": True,
                "error": "Метафора временно недоступна — повторите отправку позже"}
    return {"ok": False, "error": f"Метафора: HTTP {r.status_code} {r.text[:200]}"}

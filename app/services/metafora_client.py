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
    """Телефоны из поля «телефон + имя получателя».

    Режем по длине номера, а не «до конца цифр»: в поле рядом с телефоном часто
    стоят другие числа («+79117712372 81»), и без этого они приклеивались к
    номеру — курьер получал несуществующий телефон.
    """
    import re
    out: list[str] = []
    for chunk in re.findall(r"[\d\-\s()+]{10,}", order.delivery_contact or ""):
        digits = re.sub(r"\D", "", chunk)
        while len(digits) >= 10:
            take = 11 if digits[0] in "78" and len(digits) >= 11 else 10
            num, digits = digits[:take], digits[take:]
            if len(num) == 10:          # номер без кода страны
                num = "7" + num
            elif num.startswith("8"):   # 8XXXXXXXXXX → +7XXXXXXXXXX
                num = "7" + num[1:]
            out.append("+" + num)
    return out[:2]


def build_order_payload(order) -> dict:
    """Заказ TMS → тело POST /orders (структурная форма).

    Курьеру нужен минимум: куда, когда, к какому времени, кому звонить и как
    найти точку. Название заведения идёт в `how_to_find` — в карточке у
    перевозчика это поле под 🔍, именно там его ищет логист.

    Намеренно НЕ отправляем: `order_number` (наш номер курьеру не нужен,
    идемпотентность и так держится на `external_id`), `what_to_carry` (состав
    заказа) и `comment` (примечания) — карточка от них только разбухает.
    """
    cp = order.counterparty
    payload = {
        "external_id": str(order.number),
        "service": "Доставка",
        "address": (order.delivery_address or "").strip(),
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

    # Название заведения — под 🔍
    if cp:
        venue = (cp.trade_name or cp.name or "").strip()
        if venue:
            payload["how_to_find"] = venue[:200]
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

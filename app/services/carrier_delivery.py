"""Подтверждение доставки перевозчиком: разбор сообщения и перевод заказа в «Доставлено».

Формат подтверждения один и тот же — «✅ <адрес> | 🟢 <время>», а приходит он
двумя путями:

  • сообщением в группе перевозчика — его ловит TMS-бот (`bot/main.py`);
  • HTTP-вызовом из «Помощника логиста» (`app/routers/api_carrier.py`).

Второй путь обязателен: Telegram принципиально не отдаёт боту сообщения,
написанные другим ботом («bots will not be able to see messages from other bots
regardless of mode»), поэтому подтверждения, которые публикует бот-помощник,
TMS-бот не увидит никогда — их нужно присылать напрямую в API.

Логика разбора и сопоставления живёт здесь, чтобы оба входа вели себя одинаково.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Служебные сокращения адреса — в сравнении не участвуют
_ADDR_STOP_TOKENS = {
    "д", "дом", "ул", "улица", "г", "город", "пр", "пркт", "просп", "проспект",
    "наб", "набережная", "пер", "переулок", "ш", "шоссе", "лит", "литер",
    "к", "корп", "корпус", "стр", "строение", "оф", "офис", "пом", "помещение",
}


def norm_addr(s: str) -> str:
    """Нормализация адреса для нестрогого сравнения: нижний регистр, ё→е,
    пунктуация → пробел, схлопывание пробелов."""
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def addr_tokens(s: str) -> set[str]:
    """Значимые токены адреса: числа (номер дома) и слова от 3 букв,
    без служебных сокращений типа «ул», «д», «пр-кт»."""
    out = set()
    for tok in norm_addr(s).split(" "):
        if not tok:
            continue
        if tok.isdigit() or (len(tok) >= 3 and tok not in _ADDR_STOP_TOKENS):
            out.add(tok)
    return out


def parse_delivery_confirmation(text: str) -> str | None:
    """Возвращает адрес из сообщения «✅ <адрес> | 🟢 <...>», либо None,
    если в сообщении нет одновременно ✅ и 🟢 — тогда триггер не срабатывает.

    🔴 вместо 🟢 — это не доставка (заказ не отработан), такие пропускаем."""
    if not text or "✅" not in text or "🟢" not in text:
        return None
    address = text.split("✅", 1)[1].split("🟢", 1)[0]
    address = address.replace("|", " ").strip(" \t\n-")
    return address or None


def address_matches(msg_address: str, order_address: str) -> bool:
    """Совпадение «нестрогое»: все значимые токены адреса из сообщения
    должны присутствовать в адресе доставки заказа (порядок не важен,
    сокращения улиц/домов игнорируются)."""
    msg_t = addr_tokens(msg_address)
    if not msg_t:
        return False
    return msg_t.issubset(addr_tokens(order_address))


def find_carrier_by_chat(db: Session, chat_id) -> object | None:
    """Перевозчик, чья группа Telegram (Counterparty.tg_chat_id, задаётся в
    карточке контрагента) совпадает с чатом, откуда пришло подтверждение."""
    from app.models import Counterparty
    return db.query(Counterparty).filter(Counterparty.tg_chat_id == str(chat_id)).first()


@dataclass
class ConfirmResult:
    """Итог подтверждения. `message` готов к показу человеку (в чат или в ответ API)."""
    ok: bool
    reason: str            # ok / no_match / ambiguous
    message: str
    order_number: str | None = None
    order_id: int | None = None


def find_order_by_number(db: Session, number: str):
    """Заказ по номеру TMS. «№80», «80», «0080» — всё это заказ №80."""
    from app.models import Order
    num = (str(number or "")).strip().lstrip("#№ ").strip()
    if not num:
        return None
    order = db.query(Order).filter(Order.number == num).first()
    if order:
        return order
    # номер мог приехать с ведущими нулями или как число
    digits = re.sub(r"\D", "", num)
    if not digits:
        return None
    for o in db.query(Order).all():
        if re.sub(r"\D", "", o.number or "") == digits.lstrip("0").rjust(1, "0"):
            return o
    return None


def apply_status(db: Session, order, new_status: str, source: str) -> str:
    """Меняет статус заказа так же, как это делает карточка заказа в интерфейсе:
    проставляет отметки времени, пишет в журнал и толкает событие в Bitrix24.

    `source` — человекочитаемое «откуда» для журнала («перевозчик «…»», «Метафора»).
    Возвращает текст для ответа вызывающей стороне.
    """
    from app.routers.orders import ORDER_STATUSES
    from app.tz import now as msk_now
    from app.utils import log_action

    old_status = order.status
    if old_status == new_status:
        return f"Заказ №{order.number} уже в статусе «{ORDER_STATUSES.get(new_status, new_status)}»."

    order.status = new_status
    # Те же отметки времени, что и при ручной смене статуса: на них завязаны
    # табло цеха (assembled_at) и дата отгрузки в отчётах (handed_at).
    if new_status == "assembled" and order.assembled_at is None:
        order.assembled_at = msk_now()
    if new_status == "handed" and order.handed_at is None:
        order.handed_at = msk_now()

    log_action(
        db, "order", order.id, "status_changed", None,
        f"Статус: {ORDER_STATUSES.get(old_status, old_status)} → "
        f"{ORDER_STATUSES.get(new_status, new_status)} (авто, {source})",
        field="status", old_value=old_status, new_value=new_status,
    )
    db.commit()

    if new_status in ("assembled", "delivered") and order.bitrix_deal_id:
        from app.routers.orders import _push_bitrix_event_bg
        threading.Thread(target=_push_bitrix_event_bg, args=(order.id, new_status), daemon=True).start()

    logger.info("apply_status: заказ %s %s → %s (%s)", order.number, old_status, new_status, source)
    return (f"Заказ №{order.number}: {ORDER_STATUSES.get(old_status, old_status)} → "
            f"{ORDER_STATUSES.get(new_status, new_status)}.")


def confirm_delivery(db: Session, carrier, address: str) -> ConfirmResult:
    """Находит активный заказ перевозчика по адресу и переводит его в «Доставлено».

    Заказ должен быть единственным: если по адресу подходит несколько активных
    заказов — статус не меняем, это решает человек.
    """
    carrier_name = carrier.trade_name or carrier.name
    matches = find_orders_by_address(db, address, carrier=carrier)

    if not matches:
        return ConfirmResult(
            False, "no_match",
            f"⚠️ Не нашёл активный заказ «{carrier_name}» по адресу «{address}» — статус не изменён.",
        )
    if len(matches) > 1:
        nums = ", ".join(f"№{o.number}" for o in matches)
        return ConfirmResult(
            False, "ambiguous",
            f"⚠️ По адресу «{address}» нашлось несколько заказов ({nums}) — уточните статус вручную.",
        )

    order = matches[0]
    apply_status(db, order, "delivered", f"подтверждение перевозчика «{carrier_name}»")
    return ConfirmResult(
        True, "ok",
        f"✅ Заказ №{order.number} переведён в статус «Доставлено».",
        order_number=order.number, order_id=order.id,
    )


def find_orders_by_address(db: Session, address: str, carrier=None) -> list:
    """Активные заказы, чей адрес доставки совпадает с присланным.

    Если перевозчик известен — ищем только среди его заказов; иначе по всем
    активным (номер заказа надёжнее, адрес — фолбэк)."""
    from app.models import Order

    q = db.query(Order).filter(
        Order.status.notin_(["delivered", "cancelled"]),
        Order.delivery_address.isnot(None),
        Order.delivery_address != "",
    )
    if carrier is not None:
        q = q.filter(Order.carrier_id == carrier.id)
    candidates = q.all()
    matches = [o for o in candidates if address_matches(address, o.delivery_address)]
    logger.info("find_orders_by_address: перевозчик=%s адрес=%r активных=%d совпало=%d",
                (carrier.trade_name or carrier.name) if carrier else "любой",
                address, len(candidates), len(matches))
    return matches

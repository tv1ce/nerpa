"""Сети заведений: сводка по сети и автоподбор кандидатов на объединение.

Сетевой клиент по франчайзингу выглядит в базе как десяток независимых
контрагентов с одинаковой вывеской в trade_name («Кофе Хауз», ИП Иванов /
ИП Петров / ООО «Кофе-Юг»). Здесь — логика, которая сводит их в один объект:
считает общие деньги по сети и подсказывает, кого с кем объединить.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.models import Counterparty, Invoice, Network, Order

# Статусы заказа, отражающие фактическую выручку — тот же список, что в
# app/routers/counterparties.py (_compute_category), чтобы категории сети и
# точки считались по одному правилу.
REVENUE_STATUSES = ("paid", "assembled", "handed", "delivered")

NETWORK_KINDS = {
    "franchise": "Франшиза",
    "own": "Собственная сеть",
    "holding": "Холдинг",
}

# Организационно-правовые формы и мусорные слова, которые не отличают вывеску
_LEGAL_FORMS = (
    "ооо", "оао", "зао", "пао", "ао", "ип", "нко", "ано", "тд", "торговый дом",
    "индивидуальный предприниматель", "общество с ограниченной ответственностью",
)
_STOPWORDS = ("кафе", "ресторан", "бар", "кофейня", "пекарня", "магазин", "сеть")


def normalize_brand(value: str | None) -> str:
    """Приводит вывеску к сравнимому виду: «Кофейня "Кофе Хауз" №3» → «кофе хауз».

    Убирает кавычки, ОПФ, слова-родовые понятия («кафе», «кофейня») и номера
    точек — остаётся то, что реально отличает одну сеть от другой."""
    s = (value or "").lower().replace("ё", "е")
    s = re.sub(r"[«»\"'`]", " ", s)
    s = re.sub(r"[^0-9a-zа-я]+", " ", s)
    words = s.split()
    # ОПФ и родовые слова убираем только с краёв: «бар Дудки» → «дудки»,
    # но «Кофе и Кафе» в середине названия остаётся нетронутым.
    while words and (words[0] in _LEGAL_FORMS or words[0] in _STOPWORDS):
        words.pop(0)
    while words and (words[-1] in _LEGAL_FORMS or words[-1] in _STOPWORDS):
        words.pop()
    # Хвостовой номер точки: «додо пицца 12» → «додо пицца»
    if len(words) > 1 and words[-1].isdigit():
        words.pop()
    return " ".join(words)


def _cp_brand(cp: Counterparty) -> str:
    """Вывеска контрагента для сравнения: торговое название, иначе юр. наименование."""
    return normalize_brand(cp.trade_name) or normalize_brand(cp.name)


# ── Деньги по сети ───────────────────────────────────────────────────────────

def counterparty_stats(db: Session, cp_ids: list[int]) -> dict:
    """Сводка по группе контрагентов: выручка, заказы, дебиторка, просрочка.

    Одним запросом на сущность — карточка сети из 20 точек не должна
    превращаться в 60 обращений к базе."""
    empty = {
        "revenue_total": 0.0, "revenue_12m": 0.0, "orders_count": 0,
        "last_order_date": None, "debt": 0.0, "overdue": 0.0,
        "avg_check": 0.0, "invoices_open": 0,
    }
    if not cp_ids:
        return empty

    year_ago = date.today() - timedelta(days=365)
    orders = (
        db.query(Order)
        .filter(Order.counterparty_id.in_(cp_ids), Order.status.in_(REVENUE_STATUSES))
        .all()
    )
    revenue_total = sum(o.total_amount or 0 for o in orders)
    revenue_12m = sum(o.total_amount or 0 for o in orders if o.date and o.date >= year_ago)
    last_order = max((o.date for o in orders if o.date), default=None)

    invoices = (
        db.query(Invoice)
        .filter(Invoice.counterparty_id.in_(cp_ids),
                Invoice.status.in_(["issued", "partial", "overdue"]))
        .all()
    )
    debt = sum((inv.total_amount or 0) - (inv.paid_amount or 0) for inv in invoices)
    overdue = sum((inv.total_amount or 0) - (inv.paid_amount or 0)
                  for inv in invoices if inv.status == "overdue")

    return {
        "revenue_total": revenue_total,
        "revenue_12m": revenue_12m,
        "orders_count": len(orders),
        "last_order_date": last_order,
        "debt": debt,
        "overdue": overdue,
        "avg_check": (revenue_total / len(orders)) if orders else 0.0,
        "invoices_open": len(invoices),
    }


def network_stats(db: Session, network: Network) -> dict:
    """Сводка по сети + разбивка по точкам (для карточки сети)."""
    cps = [cp for cp in network.counterparties if cp.is_active]
    total = counterparty_stats(db, [cp.id for cp in cps])
    total["outlets"] = len(cps)
    total["per_cp"] = {cp.id: counterparty_stats(db, [cp.id]) for cp in cps}
    return total


def stats_for_networks(db: Session, networks: list[Network]) -> dict[int, dict]:
    """Сводки сразу по списку сетей — для таблицы «Сети»."""
    return {n.id: network_stats(db, n) for n in networks}


def compute_category(revenue: float) -> str | None:
    """Категория A/B/C по суммарной выручке сети (пороги — как у контрагента)."""
    if revenue >= 1_000_000:
        return "A"
    if revenue >= 200_000:
        return "B"
    if revenue > 0:
        return "C"
    return None


def recalc_categories(db: Session) -> int:
    """Пересчитывает A/B/C по сетям (кроме выставленных вручную). Без commit."""
    changed = 0
    networks = db.query(Network).filter(Network.is_active == True,  # noqa: E712
                                        Network.category_manual == False).all()  # noqa: E712
    for n in networks:
        cat = compute_category(network_stats(db, n)["revenue_total"])
        if n.category != cat:
            n.category = cat
            changed += 1
    return changed


# ── Автоподбор сетей ─────────────────────────────────────────────────────────

def suggest_groups(db: Session, min_size: int = 2) -> list[dict]:
    """Ищет контрагентов с одинаковой вывеской, ещё не собранных в сеть.

    Возвращает группы, отсортированные по размеру: [{brand, title, counterparties}].
    Ничего не меняет — решение об объединении принимает человек."""
    cps = (
        db.query(Counterparty)
        .filter(Counterparty.is_active == True,  # noqa: E712
                Counterparty.network_id.is_(None),
                Counterparty.type != "carrier")
        .all()
    )
    groups: dict[str, list[Counterparty]] = defaultdict(list)
    for cp in cps:
        brand = _cp_brand(cp)
        if brand:
            groups[brand].append(cp)

    # Вывеска уже заведена как сеть — предлагаем присоединиться к ней, а не плодить дубль
    existing = {normalize_brand(n.name): n
                for n in db.query(Network).filter(Network.is_active == True).all()}  # noqa: E712

    result = []
    for brand, members in groups.items():
        network = existing.get(brand)
        if len(members) < min_size and not network:
            continue
        # Заголовок берём из самой длинной исходной вывески — она читабельнее
        # нормализованной («Кофе Хауз» вместо «кофе хауз»)
        title = max((cp.trade_name or cp.name for cp in members), key=len)
        result.append({
            "brand": brand,
            "title": title.strip(),
            "counterparties": sorted(members, key=lambda c: c.name),
            "network": network,
        })
    result.sort(key=lambda g: -len(g["counterparties"]))
    return result


def apply_network_defaults(cp: Counterparty, network: Network | None) -> None:
    """Подставляет условия сети в точку — только в пустые/дефолтные поля."""
    if not network:
        return
    if not cp.default_discount_pct and network.default_discount_pct:
        cp.default_discount_pct = network.default_discount_pct
    if network.payment_delay_days is not None:
        cp.payment_delay_days = network.payment_delay_days
        cp.payment_delay_type = network.payment_delay_type or "banking"

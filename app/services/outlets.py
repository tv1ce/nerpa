"""Аналитика по точкам (адресам доставки).

Точка — это физический адрес, куда возят орешки, а не контрагент: один ИП может
держать две кофейни, а одна кофейня за год смениться собственником. Поэтому все
метрики здесь считаются по адресу доставки заказа.

Главная цифра — **средний расход орешков в день**. Считается по методике «сколько
съели между поставками»: объём поставки делится на число дней до следующей
поставки. Одна поставка на 168 шт, следующая через 14 дней → 12 шт/день. Дальше
из расхода выводится прогноз: когда точка «доест» последний завоз, кому звонить
сегодня, а у кого продажи просели относительно его же среднего.

Адреса в заказах пишутся вразнобой («г Санкт-Петербург, ул Гончарная, д 2» и
«Санкт-Петербург, гончарная 2» — одна и та же кофейня), поэтому адрес приводится
к ключу «улица + номер дома»; исходные написания сохраняются и показываются в
карточке точки, чтобы склейку можно было проверить глазами.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from statistics import median

from sqlalchemy.orm import Session

from app.models import Order, Product

# Номенклатура орешков: линейки П1 и П2 (см. справочник товаров — «П1.Орешки …»).
# Всё остальное (подставки, муляжи, полуфабрикаты) в расход точки не входит.
NUT_PREFIXES = ("п1.", "п2.")

# Заказ считается поставкой на точку, когда товар реально уехал к клиенту.
DELIVERED_STATUSES = ("paid", "assembled", "handed", "delivered")

DAYS_IN_MONTH = 30.44

# Статусы точки — «светофор» в списке
OUTLET_STATUSES = {
    "empty":    "Пусто",       # расчётный запас кончился — точка стоит без товара
    "soon":     "Заканчивается",
    "ok":       "В норме",
    "sleeping": "Спит",        # выпала из своего ритма — клиента можно вернуть
    "lost":     "Потерян",     # молчит больше трёх месяцев — уже не «спит»
    "new":      "Новая",       # одна поставка — расход считать не из чего
}

# Сколько дней тишины считать «сном». Одного «дольше 2.5 своих интервалов» мало:
# у точки с ритмом раз в неделю это всего 18 дней — она ещё не спит, а просто
# доедает запас. Поэтому к относительному порогу добавлен абсолютный.
SLEEP_MIN_DAYS = 30
SLEEP_INTERVAL_FACTOR = 2.5

# После трёх месяцев тишины точка перестаёт быть «спящей»: это уже не пауза,
# а потерянный клиент. В ежедневную сводку такие больше не попадают
# (см. services/outlets_digest.py — напоминаем 3 раза, раз в месяц).
LOST_DAYS = 90

# ── Нормализация адреса ──────────────────────────────────────────────────────

# Маркеры типа улицы: по ним ищем название (слово рядом с маркером)
_STREET_MARKERS = {
    "ул", "улица", "пр", "пркт", "прт", "просп", "проспект", "пер", "переулок",
    "наб", "набережная", "ш", "шоссе", "бр", "бул", "бульвар", "пл", "площадь",
    "лн", "линия", "аллея", "дорога", "тракт", "проезд", "туп",
}
# Мусор: города, регионы, литеры, этажи, ТЦ и прочее, что не отличает точку
_NOISE = {
    "россия", "рф", "г", "гор", "город", "санкт", "петербург", "санкт-петербург",
    "спб", "кронштадт", "во", "в", "о", "мск",
    "москва", "обл", "область", "р-н", "район", "дом", "д", "зд", "здание",
    "литера", "лит", "к", "корп", "корпус", "стр", "строение", "пом", "помещ",
    "помещение", "офис", "оф", "этаж", "эт", "вход", "тц", "трц", "трк", "тк",
    "бизнесцентр", "бц", "мкр", "деревня", "дер", "пос", "посёлок", "поселок",
    "п", "село", "с", "территория", "владение", "вл", "квартал",
}
_HOUSE_RE = re.compile(r"^\d+")
# «2-я Красноармейская», «7-ая линия» — это часть названия улицы, а не номер дома,
# в отличие от «47-А» (дом 47 литера А). Отличаем по окончанию: порядковое или нет.
_ORDINAL_RE = re.compile(r"^\d+-(?:я|й|е|ая|ой|ий|ья|ые|ое|го)$")


def _tokens(raw: str) -> list[str]:
    s = (raw or "").lower().replace("ё", "е")
    s = re.sub(r"\(.*?\)", " ", s)              # «(этаж 2)» — не часть адреса
    s = re.sub(r"\b\d{6}\b", " ", s)            # почтовый индекс
    s = re.sub(r"[^0-9a-zа-я/\-]+", " ", s)
    # «Мурманское шоссе 12-й км, стр 1» — адресом здесь является километр, а не
    # строение: «12-й км» сворачиваем в номер дома «12»
    s = re.sub(r"\b(\d+)-[а-я]{1,2}\s+км\b", r"\1", s)
    return [t for t in s.split() if t]


def _house_key(token: str) -> str:
    """Номер дома для ключа: «117в» → «117», «26б» → «26».

    Буква корпуса/литеры в базе ставится непоследовательно (одна и та же точка
    заведена и как «Ломоносова 117В», и как «Ломоносова 117»), поэтому в ключ
    идут только цифры номера. Полное написание остаётся в исходных адресах."""
    m = _HOUSE_RE.match(token)
    return m.group(0) if m else ""


def _is_name(token: str) -> bool:
    """Похоже ли слово на название улицы (а не на город, маркер типа или номер)."""
    return (token not in _NOISE
            and token.replace("-", "") not in _STREET_MARKERS
            and not _HOUSE_RE.match(token))


def normalize_address(raw: str | None) -> str:
    """Ключ точки: «улица:дом». Пустая строка — адрес не разобрать (самовывоз и т.п.)."""
    tokens = _tokens(raw)
    if not tokens:
        return ""
    if any(t.startswith("самовывоз") for t in tokens):
        return ""

    # Номер дома — первый токен, начинающийся с цифры (но не «2-я красноармейская»)
    house_idx = next(
        (i for i, t in enumerate(tokens)
         if _HOUSE_RE.match(t) and not _ORDINAL_RE.match(t)),
        None,
    )
    if house_idx is None:
        return ""
    house = _house_key(tokens[house_idx])

    # Название улицы: слово рядом с маркером типа («ул Гончарная», «Коломяжский
    # проспект»), иначе — последнее осмысленное слово перед номером дома
    street = ""
    for i, t in enumerate(tokens[:house_idx + 1]):
        # «пр-кт», «пр-т», «б-р» — тот же маркер, что «пркт»/«бр»: дефисы не значимы
        if t.replace("-", "") in _STREET_MARKERS:
            # После маркера ищем первое осмысленное слово: в «ул 2-я Красноармейская»
            # название — не порядковый номер, а слово за ним
            after = next((x for x in tokens[i + 1:house_idx + 1] if _is_name(x)), "")
            before = tokens[i - 1] if i and _is_name(tokens[i - 1]) else ""
            if after:
                street = after
                break
            if before:
                street = before
                break
    if not street:
        street = next((t for t in reversed(tokens[:house_idx]) if _is_name(t)), "")
    if not street:
        return ""
    return f"{street}:{house}"


def address_label(key: str) -> str:
    """Ключ «гончарная:2» → «Гончарная, 2» для заголовков."""
    if ":" not in key:
        return key
    street, house = key.split(":", 1)
    return f"{street.capitalize()}, {house}"


# ── Сбор поставок ────────────────────────────────────────────────────────────

def _nut_product_ids(db: Session) -> set[int]:
    """id номенклатуры орешков (П1/П2) — SQLite LOWER() не умеет кириллицу,
    поэтому фильтруем в Python, как это уже делается в отчёте по выручке."""
    return {
        p.id for p in db.query(Product.id, Product.name).all()
        if (p.name or "").lower().startswith(NUT_PREFIXES)
    }


def _flavor(name: str) -> str:
    """Вкус из названия номенклатуры: «П1.Орешки с кокосовой начинкой» → «кокос»."""
    n = (name or "").lower()
    for needle, label in (("сгущен", "классика"), ("классик", "классика"),
                          ("карамел", "карамель"), ("фисташ", "фисташка"),
                          ("кокос", "кокос")):
        if needle in n:
            return label
    return "прочее"


def collect_deliveries(db: Session) -> dict[str, dict]:
    """Группирует заказы по точкам. Возвращает {ключ адреса: сырые данные точки}."""
    nut_ids = _nut_product_ids(db)
    rows = (
        db.query(Order)
        .filter(Order.status.in_(DELIVERED_STATUSES))
        .order_by(Order.date)
        .all()
    )
    points: dict[str, dict] = {}
    for order in rows:
        key = normalize_address(order.delivery_address)
        if not key:
            continue
        qty = sum(i.quantity or 0 for i in order.items if i.product_id in nut_ids)
        amount = order.total_amount
        p = points.setdefault(key, {
            "key": key, "raw_addresses": set(), "deliveries": [],
            "counterparties": {}, "flavors": defaultdict(float), "revenue": 0.0,
        })
        p["raw_addresses"].add((order.delivery_address or "").strip())
        p["deliveries"].append({
            "date": order.date, "qty": qty, "order": order, "amount": amount,
            # Нулевая сумма при отгруженном товаре — это не дозаказ, а чаще всего
            # рекламация: везём замену брака бесплатно. Помечаем, чтобы такие
            # поставки не выдавались за признак роста продаж
            "free": qty > 0 and not amount,
        })
        p["revenue"] += amount
        cp = order.counterparty
        if cp:
            p["counterparties"][cp.id] = cp
        for item in order.items:
            if item.product_id in nut_ids and item.product:
                p["flavors"][_flavor(item.product.name)] += item.quantity or 0
    return points


# ── Метрики точки ────────────────────────────────────────────────────────────

def outlet_metrics(point: dict, today: date | None = None) -> dict:
    """Считает по точке расход в день, частоту заказов, прогноз и тренд."""
    today = today or date.today()
    deliveries = sorted(point["deliveries"], key=lambda d: d["date"])
    dates = [d["date"] for d in deliveries]
    first, last = dates[0], dates[-1]
    last_qty = deliveries[-1]["qty"]
    total_qty = sum(d["qty"] for d in deliveries)

    # Интервалы между поставками: расход = что привезли ÷ на сколько дней хватило
    intervals: list[tuple[int, float]] = []   # (дней до следующей, объём этой поставки)
    for prev, nxt in zip(deliveries, deliveries[1:]):
        days = (nxt["date"] - prev["date"]).days
        if days > 0:
            intervals.append((days, prev["qty"]))

    days_total = sum(d for d, _ in intervals)
    qty_consumed = sum(q for _, q in intervals)
    daily_rate = (qty_consumed / days_total) if days_total else None
    avg_interval = (days_total / len(intervals)) if intervals else None

    # Частота: сколько заказов в месяц по ритму поставок
    orders_per_month = (DAYS_IN_MONTH / avg_interval) if avg_interval else None

    days_since = (today - last).days

    # Прогноз: сколько от последнего завоза осталось при таком расходе
    if daily_rate and daily_rate > 0:
        stock_left = last_qty - daily_rate * days_since
        days_left = stock_left / daily_rate
        next_order_date = last + timedelta(days=round(last_qty / daily_rate))
    else:
        stock_left = days_left = None
        next_order_date = None

    # Тренд: последний интервал против среднего по точке
    trend_pct = None
    if len(intervals) >= 2 and daily_rate:
        last_days, last_prev_qty = intervals[-1]
        last_rate = last_prev_qty / last_days
        trend_pct = round((last_rate / daily_rate - 1) * 100)

    if len(deliveries) < 2:
        status = "new"
    elif days_since > LOST_DAYS:
        # Три месяца тишины — это уже не «спит»
        status = "lost"
    elif (avg_interval and days_since > avg_interval * SLEEP_INTERVAL_FACTOR
            and days_since >= SLEEP_MIN_DAYS):
        # Выпала из ритма — но только если тишина заметна и в абсолютных днях:
        # иначе точка с недельным ритмом «засыпала» через две с половиной недели,
        # хотя ей просто пора завозить (это «Пусто», а не «Спит»)
        status = "sleeping"
    elif days_left is None:
        status = "ok"
    elif days_left < 0:
        status = "empty"
    elif days_left <= 3:
        status = "soon"
    else:
        status = "ok"

    free_count = sum(1 for d in deliveries if d.get("free"))
    flavors = dict(point["flavors"])
    flavor_total = sum(flavors.values()) or 1
    flavor_mix = {k: round(v / flavor_total * 100) for k, v in
                  sorted(flavors.items(), key=lambda kv: -kv[1])}

    cps = list(point["counterparties"].values())
    networks = {cp.network.name for cp in cps if cp.network_id and cp.network}

    return {
        "key": point["key"],
        "label": address_label(point["key"]),
        "raw_addresses": sorted(a for a in point["raw_addresses"] if a),
        "counterparties": cps,
        "networks": sorted(networks),
        "deliveries": deliveries,
        "deliveries_count": len(deliveries),
        "free_count": free_count,
        "first_date": first,
        "last_date": last,
        "days_since": days_since,
        "total_qty": total_qty,
        "last_qty": last_qty,
        "avg_qty": total_qty / len(deliveries),
        "daily_rate": daily_rate,
        "avg_interval": avg_interval,
        "orders_per_month": orders_per_month,
        "days_left": days_left,
        "next_order_date": next_order_date,
        "trend_pct": trend_pct,
        "status": status,
        "revenue": point["revenue"],
        "flavor_mix": flavor_mix,
    }


def build_outlets(db: Session, today: date | None = None) -> list[dict]:
    """Все точки с метриками, отсортированные по срочности: где пусто — сверху."""
    order = {"empty": 0, "soon": 1, "sleeping": 2, "ok": 3, "new": 4, "lost": 5}
    outlets = [outlet_metrics(p, today) for p in collect_deliveries(db).values()]
    outlets.sort(key=lambda o: (order.get(o["status"], 9),
                                o["days_left"] if o["days_left"] is not None else 999))
    return outlets


def add_benchmarks(outlets: list[dict]) -> None:
    """Дописывает каждой точке отклонение её расхода от медианы по всем точкам.

    Абсолютные 8 шт/день ни о чём не говорят, пока не видно, что соседняя точка
    той же сети продаёт 15 — сравнение и есть повод для разговора."""
    rates = [o["daily_rate"] for o in outlets if o["daily_rate"]]
    med = median(rates) if rates else None
    for o in outlets:
        o["median_rate"] = med
        o["vs_median_pct"] = (
            round((o["daily_rate"] / med - 1) * 100) if med and o["daily_rate"] else None
        )


def summary(outlets: list[dict]) -> dict:
    """Шапка раздела: сколько точек, где пусто, общий расход, средний ритм."""
    rates = [o["daily_rate"] for o in outlets if o["daily_rate"]]
    intervals = [o["avg_interval"] for o in outlets if o["avg_interval"]]
    return {
        "count": len(outlets),
        "empty": sum(1 for o in outlets if o["status"] == "empty"),
        "soon": sum(1 for o in outlets if o["status"] == "soon"),
        "sleeping": sum(1 for o in outlets if o["status"] == "sleeping"),
        "lost": sum(1 for o in outlets if o["status"] == "lost"),
        "daily_total": sum(rates),
        "avg_rate": (sum(rates) / len(rates)) if rates else 0.0,
        "avg_interval": (sum(intervals) / len(intervals)) if intervals else 0.0,
        "revenue": sum(o["revenue"] for o in outlets),
    }


# ── Сравнение точек внутри сети ──────────────────────────────────────────────

def add_network_comparison(outlets: list[dict]) -> None:
    """Считает место точки среди точек её сети и отставание от лучшей.

    Медиана по всей базе смешивает кофейню в спальнике с точкой на Невском.
    Внутри сети формат, ассортимент и цены одинаковые, поэтому разрыв между
    точками одной вывески — это уже вопрос к конкретной точке, а не к рынку."""
    by_network: dict[str, list[dict]] = defaultdict(list)
    for o in outlets:
        for name in o["networks"]:
            by_network[name].append(o)

    for o in outlets:
        o["network_rank"] = None

    for name, members in by_network.items():
        rated = sorted((m for m in members if m["daily_rate"]),
                       key=lambda m: -m["daily_rate"])
        if len(rated) < 2:
            continue
        best = rated[0]
        rates = [m["daily_rate"] for m in rated]
        avg = sum(rates) / len(rates)
        for i, m in enumerate(rated, start=1):
            # Точка может входить в несколько сетей — оставляем сравнение с той,
            # где она выглядит хуже: именно там есть что чинить
            gap = round((m["daily_rate"] / best["daily_rate"] - 1) * 100)
            prev = m.get("network_rank")
            if prev and prev["gap_to_best_pct"] <= gap:
                continue
            m["network_rank"] = {
                "network": name,
                "place": i,
                "total": len(rated),
                "best_label": best["label"],
                "best_rate": best["daily_rate"],
                "avg_rate": avg,
                "gap_to_best_pct": gap,
                "vs_network_pct": round((m["daily_rate"] / avg - 1) * 100),
            }


def network_outlets(outlets: list[dict], network_name: str) -> list[dict]:
    """Точки одной сети, отсортированные по расходу — таблица сравнения в карточке сети."""
    members = [o for o in outlets if network_name in o["networks"]]
    return sorted(members, key=lambda o: -(o["daily_rate"] or 0))


# ── Запросы к геокодеру ──────────────────────────────────────────────────────

# Города, которые встречаются в адресах заказов. Геокодеру нужен город явно:
# «Гончарная 2» без города он ищет по всей стране и чаще всего не находит.
_CITY_HINTS = (
    ("санкт-петербург", "Санкт-Петербург"), ("спб", "Санкт-Петербург"),
    ("петербург", "Санкт-Петербург"), ("кронштадт", "Кронштадт"),
    ("москва", "Москва"), ("кудрово", "Кудрово"), ("мурино", "Мурино"),
    ("выборг", "Выборг"), ("кириши", "Кириши"), ("раменское", "Раменское"),
    ("парголово", "Санкт-Петербург"), ("отрадное", "Отрадное"),
)


def _city_of(raw_addresses: list[str]) -> str:
    """Город точки по исходным адресам; пусто — не определили."""
    joined = " ".join(raw_addresses).lower().replace("ё", "е")
    for needle, city in _CITY_HINTS:
        if needle in joined:
            return city
    return ""


def geocode_queries(key: str, raw_addresses: list[str]) -> list[str]:
    """Варианты запроса к геокодеру, от подробного к простому.

    Полный адрес из заказа («…лит.Б, ТРК "Академ-Парк", помещение F6») геокодер
    часто не понимает, зато уверенно находит «Гражданский проспект 41,
    Санкт-Петербург». Поэтому пробуем по очереди: как записано в заказе, затем
    очищенное «улица дом, город»."""
    variants = []
    if raw_addresses:
        variants.append(max(raw_addresses, key=len))
    street, _, house = key.partition(":")
    city = _city_of(raw_addresses)
    clean = f"{street} {house}".strip()
    if city:
        variants.append(f"{clean}, {city}")
    variants.append(clean)
    # Убираем дубли, сохраняя порядок
    seen, result = set(), []
    for v in variants:
        v = v.strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            result.append(v)
    return result

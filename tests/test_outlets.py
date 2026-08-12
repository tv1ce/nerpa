"""Аналитика точек: нормализация адресов, расход орешков, ритм, прогноз, роуты."""
from datetime import date, timedelta

import pytest

from app.services.outlets import normalize_address, outlet_metrics


# ── Нормализация адресов ─────────────────────────────────────────────────────
# Кейсы взяты с боевой базы: одну и ту же кофейню заводят по-разному.

@pytest.mark.parametrize("a,b", [
    ("г Санкт-Петербург, ул Гончарная, д 2", "Санкт-Петербург, гончарная 2"),
    ("ул. Ломоносова, 117В, посёлок Парголово",
     "улица Ломоносова, 117, Парголово, Санкт-Петербург, Россия, 194362"),
    ("Коломяжский проспект, 22 стр1, ТРЦ Голливуд",
     "Коломяжский 22, Санкт-Петербург, ТЦ Hollywood"),
    ("г.Санкт-Петербург, Гражданский пр-кт, д.41, корп.2, лит.Б, ТРК \"Академ-Парк\"",
     "г Санкт-Петербург, Гражданский пр-кт, д 41 к 2 литера Б"),
    ("Санкт-Петербург, пр. Космонавтов 14", "Санкт-Петербург, проспект Космонавтов, 14"),
])
def test_same_point_merges(a, b):
    key = normalize_address(a)
    assert key and key == normalize_address(b), f"{a!r} и {b!r} должны быть одной точкой"


@pytest.mark.parametrize("addr,expected", [
    ("г Санкт-Петербург, ул 2-я Красноармейская, д 3", "красноармейская:3"),
    ("проспект Стачек, 47-А, Санкт-Петербург", "стачек:47"),
    ("Мурманское шоссе 12-й км, стр 1", "мурманское:12"),
    ("Благодатная 33", "благодатная:33"),
])
def test_tricky_addresses(addr, expected):
    assert normalize_address(addr) == expected


@pytest.mark.parametrize("addr", ["", None, "Самовывоз", "   "])
def test_unparsable_addresses(addr):
    assert normalize_address(addr) == ""


def test_different_houses_stay_apart():
    assert normalize_address("Лиговский пр 33") != normalize_address("Лиговский пр 55")


# ── Методика расчёта ─────────────────────────────────────────────────────────

def _point(deliveries):
    """Синтетическая точка: [(дата, штук), …]."""
    from collections import defaultdict
    return {
        "key": "тестовая:1", "raw_addresses": {"Тестовая, 1"},
        "deliveries": [{"date": d, "qty": q, "order": None} for d, q in deliveries],
        "counterparties": {}, "flavors": defaultdict(float), "revenue": 0.0,
    }


def test_daily_rate_is_qty_divided_by_days_until_next():
    """168 шт, следующая поставка через 14 дней → 12 шт/день."""
    start = date(2026, 6, 1)
    m = outlet_metrics(_point([(start, 168), (start + timedelta(days=14), 168)]),
                       today=start + timedelta(days=14))
    assert m["daily_rate"] == pytest.approx(12.0)
    assert m["avg_interval"] == pytest.approx(14.0)


def test_daily_rate_weights_by_interval_length():
    """Несколько интервалов: расход = всё съеденное ÷ все дни, а не среднее средних."""
    start = date(2026, 6, 1)
    m = outlet_metrics(_point([
        (start, 100),                       # съели за 10 дней → 10/день
        (start + timedelta(days=10), 300),  # съели за 30 дней → 10/день
        (start + timedelta(days=40), 150),
    ]), today=start + timedelta(days=40))
    assert m["daily_rate"] == pytest.approx(400 / 40)
    assert m["deliveries_count"] == 3


def test_orders_per_month_from_rhythm():
    """Поставка раз в 15 дней → примерно 2 заказа в месяц."""
    start = date(2026, 1, 1)
    m = outlet_metrics(_point([(start + timedelta(days=15 * i), 150) for i in range(4)]),
                       today=start + timedelta(days=45))
    assert m["orders_per_month"] == pytest.approx(30.44 / 15, rel=0.01)


def test_forecast_marks_empty_point():
    """Завезли на 10 дней, прошло 20 — точка стоит без товара."""
    start = date(2026, 6, 1)
    m = outlet_metrics(_point([(start, 100), (start + timedelta(days=10), 100)]),
                       today=start + timedelta(days=30))
    assert m["daily_rate"] == pytest.approx(10.0)
    assert m["days_left"] < 0
    assert m["status"] == "empty"


def test_single_delivery_is_new_point():
    m = outlet_metrics(_point([(date(2026, 6, 1), 168)]), today=date(2026, 6, 5))
    assert m["status"] == "new"
    assert m["daily_rate"] is None
    assert m["orders_per_month"] is None


def test_trend_detects_slowdown():
    """Последний интервал вдвое длиннее при том же объёме — расход просел."""
    start = date(2026, 6, 1)
    m = outlet_metrics(_point([
        (start, 100),
        (start + timedelta(days=10), 100),
        (start + timedelta(days=20), 100),
        (start + timedelta(days=60), 100),   # тот же объём, но растянулся на 40 дней
    ]), today=start + timedelta(days=60))
    # средний расход 300/60 = 5 шт/день, последний интервал 100/40 = 2.5 → −50%
    assert m["trend_pct"] == -50


# ── Роуты ────────────────────────────────────────────────────────────────────

def test_outlets_page_renders(admin_client):
    r = admin_client.get("/analytics/outlets")
    assert r.status_code == 200
    assert "Аналитика по точкам" in r.text


def test_outlet_appears_with_metrics(admin_client):
    """Сквозной прогон: два заказа на один адрес → точка с расходом в списке и карточке."""
    from app.database import SessionLocal
    from app.models import Counterparty, Order, OrderItem, Product

    start = date.today() - timedelta(days=20)
    db = SessionLocal()
    try:
        product = Product(name="П1.Орешки со сгущенкой «Классика»", price=52.0)
        db.add(product)
        cp = Counterparty(name="ООО «Точка аналитики»", trade_name="Кофейня Аналитика",
                          inn="7899999901")
        db.add(cp)
        db.flush()
        # 140 шт, следующая поставка через 14 дней → 10 шт/день
        for i, (day, qty, addr) in enumerate([
            (start, 140, "г Санкт-Петербург, ул Тестовая, д 7"),
            (start + timedelta(days=14), 140, "Тестовая 7, Санкт-Петербург"),
        ]):
            order = Order(number=f"OUT-TEST-{i}", date=day, counterparty_id=cp.id,
                          status="delivered", delivery_address=addr)
            db.add(order)
            db.flush()
            db.add(OrderItem(order_id=order.id, product_id=product.id,
                             quantity=qty, price=52.0, amount=qty * 52.0))
        db.commit()
    finally:
        db.close()

    r = admin_client.get("/analytics/outlets", params={"q": "тестовая"})
    assert r.status_code == 200
    assert "Тестовая, 7" in r.text
    assert "10.0" in r.text, "должен считаться расход 10 шт/день"

    r = admin_client.get("/analytics/outlets/detail", params={"key": "тестовая:7"})
    assert r.status_code == 200
    assert "Кофейня Аналитика" in r.text
    # оба написания адреса склеены в одну точку и показаны в карточке
    assert "г Санкт-Петербург, ул Тестовая, д 7" in r.text
    assert "Тестовая 7, Санкт-Петербург" in r.text


def test_outlet_detail_unknown_key_redirects(admin_client):
    r = admin_client.get("/analytics/outlets/detail", params={"key": "нет:1"},
                         follow_redirects=False)
    assert r.status_code == 302


def test_ai_failure_does_not_break_page(admin_client, monkeypatch):
    """Без ключа OpenRouter кнопка ИИ не роняет страницу, а возвращает с ошибкой."""
    from app.services import openrouter_client

    def _boom(*a, **kw):
        raise RuntimeError("no api key")

    monkeypatch.setattr(openrouter_client, "chat", _boom)
    monkeypatch.setattr(openrouter_client, "chat_json", _boom)

    csrf = _csrf(admin_client, "/analytics/outlets")
    r = admin_client.post("/analytics/outlets/analyze",
                          data={"key": "тестовая:7", "csrf_token": csrf},
                          follow_redirects=False)
    assert r.status_code == 302
    assert "error=" in r.headers["location"]

    r = admin_client.post("/analytics/outlets/digest", data={"csrf_token": csrf},
                          follow_redirects=False)
    assert r.status_code == 302
    assert "error=" in r.headers["location"]


def _csrf(client, path):
    import re
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', client.get(path).text)
    return m.group(1) if m else ""


# ── Сравнение внутри сети ────────────────────────────────────────────────────

def test_network_comparison_ranks_outlets():
    """Точки одной сети выстраиваются по расходу с отставанием от лучшей."""
    from app.services.outlets import add_network_comparison

    def fake(label, rate, network):
        return {"key": label, "label": label, "daily_rate": rate,
                "networks": [network] if network else []}

    outlets = [fake("A", 20.0, "Сеть"), fake("B", 10.0, "Сеть"),
               fake("C", 5.0, "Сеть"), fake("D", 30.0, None)]
    add_network_comparison(outlets)

    a, b, c, d = outlets
    assert a["network_rank"]["place"] == 1
    assert b["network_rank"]["place"] == 2
    assert b["network_rank"]["gap_to_best_pct"] == -50      # 10 против 20
    assert c["network_rank"]["gap_to_best_pct"] == -75      # 5 против 20
    # средний расход сети (20+10+5)/3 ≈ 11.67 → лучшая точка на +71%
    assert a["network_rank"]["vs_network_pct"] == 71
    assert d["network_rank"] is None, "точку вне сети сравнивать не с чем"


def test_single_outlet_network_has_no_rank():
    from app.services.outlets import add_network_comparison
    outlets = [{"key": "A", "label": "A", "daily_rate": 12.0, "networks": ["Одна точка"]}]
    add_network_comparison(outlets)
    assert outlets[0]["network_rank"] is None


# ── Карта и сравнение сетей ──────────────────────────────────────────────────

def test_map_and_networks_pages_render(admin_client):
    for path in ("/analytics/outlets/map", "/analytics/networks"):
        r = admin_client.get(path)
        assert r.status_code == 200, f"{path} → {r.status_code}"


def test_map_data_returns_only_geocoded(admin_client):
    """В данные карты попадают только точки с координатами."""
    from app.database import SessionLocal
    from app.models import OutletGeo

    assert admin_client.get("/analytics/outlets/map/data").json() == []

    db = SessionLocal()
    try:
        db.add(OutletGeo(address_key="тестовая:7", lat=59.93, lng=30.33,
                         query="Тестовая 7"))
        db.commit()
    finally:
        db.close()

    rows = admin_client.get("/analytics/outlets/map/data").json()
    assert len(rows) == 1
    point = rows[0]
    assert point["label"] == "Тестовая, 7"
    assert point["lat"] == 59.93 and point["lng"] == 30.33
    assert point["daily_rate"] == 10.0
    assert "Кофейня Аналитика" in point["clients"]


def test_geocode_status_available(admin_client):
    st = admin_client.get("/analytics/outlets/geocode/status").json()
    assert set(st) >= {"running", "done", "total", "found"}


def test_zero_price_delivery_marked_as_claim(admin_client):
    """Отгрузка на 0 ₽ — рекламация: помечается в карточке и уходит в промпт ИИ."""
    from app.database import SessionLocal
    from app.models import Counterparty, Order, OrderItem, Product
    from app.routers.analytics import _outlet_facts, _outlets

    start = date.today() - timedelta(days=24)
    db = SessionLocal()
    try:
        product = Product(name="П1.Орешки с кокосовой начинкой", price=52.0)
        db.add(product)
        cp = Counterparty(name="ООО «Рекламационная»", trade_name="Кофейня Брак",
                          inn="7899999902")
        db.add(cp)
        db.flush()
        addr = "г Санкт-Петербург, ул Бракованная, д 5"
        # Вторая отгрузка — бесплатная замена брака (сумма 0)
        for i, (day, qty, price) in enumerate([
            (start, 120, 52.0),
            (start + timedelta(days=12), 60, 0.0),
            (start + timedelta(days=20), 120, 52.0),
        ]):
            order = Order(number=f"CLAIM-TEST-{i}", date=day, counterparty_id=cp.id,
                          status="delivered", delivery_address=addr)
            db.add(order)
            db.flush()
            db.add(OrderItem(order_id=order.id, product_id=product.id,
                             quantity=qty, price=price, amount=qty * price))
        db.commit()

        outlet = next(o for o in _outlets(db) if o["key"] == "бракованная:5")
        assert outlet["free_count"] == 1
        assert [d["free"] for d in outlet["deliveries"]] == [False, True, False]

        facts = _outlet_facts(outlet)
        assert "РЕКЛАМАЦИЯ" in facts
        assert "Из них рекламаций (нулевая сумма): 1" in facts
    finally:
        db.close()

    r = admin_client.get("/analytics/outlets/detail", params={"key": "бракованная:5"})
    assert r.status_code == 200
    assert "рекламация" in r.text


# ── Ежедневная сводка в Telegram ─────────────────────────────────────────────

def test_digest_message_format():
    """Задачи превращаются в сообщение с приоритетами, контрагентом и адресом."""
    from app.services.outlets_digest import format_message

    text = format_message([
        {"outlet": "Гончарная, 2", "client": "ИП Веннерхолм", "priority": "high",
         "action": "Позвонить и завезти 240 шт", "why": "стоит без товара 5 дней"},
    ])
    assert "Веннерхолм" in text
    assert "Гончарная" in text
    assert "🔴" in text


def test_digest_message_when_all_calm():
    from app.services.outlets_digest import format_message
    assert "Срочных задач нет" in format_message([])


def test_digest_due_now_respects_settings():
    """Расписание: время, будни и защита от повторной отправки за день."""
    from datetime import datetime

    from app.models import CompanySettings
    from app.services.outlets_digest import due_now

    monday_10 = datetime(2026, 8, 10, 10, 0)     # понедельник
    saturday_10 = datetime(2026, 8, 15, 10, 0)   # суббота

    off = CompanySettings(outlets_digest_enabled=False, outlets_digest_time="09:30")
    assert due_now(off, monday_10) is False

    on = CompanySettings(outlets_digest_enabled=True, outlets_digest_time="09:30",
                         outlets_digest_weekdays_only=True)
    assert due_now(on, monday_10) is True
    assert due_now(on, datetime(2026, 8, 10, 9, 0)) is False, "время ещё не наступило"
    assert due_now(on, saturday_10) is False, "по выходным не шлём"

    on.outlets_digest_last_sent = monday_10.date()
    assert due_now(on, monday_10) is False, "за день отправляем один раз"

    assert due_now(None, monday_10) is False


def test_digest_send_without_chat_is_reported(admin_client):
    """Без настроенного чата сводка не отправляется, но и не падает."""
    from app.database import SessionLocal
    from app.models import CompanySettings
    from app.services import outlets_digest

    db = SessionLocal()
    try:
        company = db.query(CompanySettings).first()
        company.outlets_digest_chat_ids = None
        company.tg_report_chat_ids = None
        db.commit()
        result = outlets_digest.send(db)
        assert result["ok"] is False and "чат" in result["error"].lower()
    finally:
        db.close()


def test_geocode_queries_fallback_to_clean_address():
    """Полный адрес из заказа дополняется очищенным «улица дом, город»."""
    from app.services.outlets import geocode_queries

    raw = ['г.Санкт-Петербург, Гражданский пр-кт, д.41, корп.2, лит.Б, ТРК "Академ-Парк", помещение F6']
    variants = geocode_queries("гражданский:41", raw)
    assert variants[0] == raw[0], "сначала пробуем как записано в заказе"
    assert "гражданский 41, Санкт-Петербург" in variants
    # Москву не подставляем в питерский адрес и наоборот
    assert geocode_queries("гвардейская:3", ["Москва, Гвардейская улица, 3к1"])[1] \
        == "гвардейская 3, Москва"


def test_geocode_queries_without_city():
    """Город не определился, а очищенный вариант совпал с исходным — дубль не плодим."""
    from app.services.outlets import geocode_queries
    assert geocode_queries("благодатная:33", ["Благодатная 33"]) == ["Благодатная 33"]


# ── «Спит» и «Потерян» ───────────────────────────────────────────────────────

def test_frequent_point_is_empty_not_sleeping():
    """Точка с недельным ритмом молчит 20 дней — это «пусто», а не «спит»:
    прежний порог 2.5 интервала засыпал её на 18-й день и прятал срочную задачу."""
    start = date.today() - timedelta(days=41)
    m = outlet_metrics(_point([
        (start, 70), (start + timedelta(days=7), 70), (start + timedelta(days=14), 70),
        (start + timedelta(days=21), 70),
    ]))
    assert m["days_since"] == 20
    assert m["status"] == "empty"


def test_point_sleeps_after_month_off_rhythm():
    """Тот же недельный ритм, но тишина уже 40 дней — точка выпала из графика."""
    start = date.today() - timedelta(days=61)
    m = outlet_metrics(_point([
        (start, 70), (start + timedelta(days=7), 70), (start + timedelta(days=14), 70),
        (start + timedelta(days=21), 70),
    ]))
    assert m["days_since"] == 40
    assert m["status"] == "sleeping"


def test_point_is_lost_after_three_months():
    start = date.today() - timedelta(days=200)
    m = outlet_metrics(_point([(start, 100), (start + timedelta(days=14), 100)]))
    assert m["status"] == "lost"


def test_rare_rhythm_point_not_sleeping_too_early():
    """Точка заказывает раз в 30 дней: 40 дней тишины — ещё не сон."""
    start = date.today() - timedelta(days=100)
    m = outlet_metrics(_point([
        (start, 200), (start + timedelta(days=30), 200), (start + timedelta(days=60), 200),
    ]))
    assert m["days_since"] == 40
    assert m["status"] != "sleeping"


# ── Напоминания по спящим в сводке ───────────────────────────────────────────

def _sleeping(key, days_ago=45):
    return {"key": key, "label": key, "status": "sleeping",
            "last_date": date.today() - timedelta(days=days_ago)}


def test_digest_reminds_sleeping_monthly_three_times():
    """Спящую точку напоминаем раз в месяц и максимум три раза, потом молчим."""
    from app.database import SessionLocal
    from app.models import OutletReminder
    from app.services.outlets_digest import mark_reminded, select_for_digest

    db = SessionLocal()
    try:
        db.query(OutletReminder).delete()
        db.commit()
        today = date.today()
        point = _sleeping("спящая:1")
        active = {"key": "живая:2", "label": "живая:2", "status": "empty",
                  "last_date": today}

        # 1-е напоминание — уходит
        rows, sleeping = select_for_digest(db, [point, active], today)
        assert point in rows and sleeping == [point]
        mark_reminded(db, sleeping, today)

        # На следующий день спящей в сводке уже нет, активная осталась
        rows, sleeping = select_for_digest(db, [point, active], today + timedelta(days=1))
        assert sleeping == [] and rows == [active]

        # Через месяц — второе напоминание, ещё через месяц — третье
        for month in (1, 2):
            when = today + timedelta(days=30 * month)
            rows, sleeping = select_for_digest(db, [point, active], when)
            assert sleeping == [point], f"напоминание {month + 1} должно уйти"
            mark_reminded(db, sleeping, when)

        # Четвёртого напоминания нет — три месяца прошли, клиент потерян
        rows, sleeping = select_for_digest(db, [point, active], today + timedelta(days=90))
        assert sleeping == [] and point not in rows
        assert db.query(OutletReminder).filter(
            OutletReminder.address_key == "спящая:1").first().sent_count == 3
    finally:
        db.close()


def test_digest_resets_counter_when_point_orders_again():
    """Точка заказала после напоминаний — счётчик обнуляется, цикл начинается заново."""
    from app.database import SessionLocal
    from app.models import OutletReminder
    from app.services.outlets_digest import mark_reminded, select_for_digest

    db = SessionLocal()
    try:
        db.query(OutletReminder).delete()
        db.commit()
        today = date.today()
        old = _sleeping("вернулась:3", days_ago=60)
        for i in range(3):
            when = today + timedelta(days=30 * i)
            _, sleeping = select_for_digest(db, [old], when)
            mark_reminded(db, sleeping, when)

        # Свежая поставка → точка снова может попасть в сводку
        revived = _sleeping("вернулась:3", days_ago=0)
        revived["last_date"] = today + timedelta(days=100)
        rows, sleeping = select_for_digest(db, [revived], today + timedelta(days=140))
        assert sleeping == [revived]
    finally:
        db.close()


def test_digest_skips_lost_points():
    """Потерянные точки в сводку не попадают вообще."""
    from app.database import SessionLocal
    from app.services.outlets_digest import select_for_digest

    db = SessionLocal()
    try:
        lost = {"key": "потерянная:9", "label": "потерянная:9", "status": "lost",
                "last_date": date.today() - timedelta(days=200)}
        rows, sleeping = select_for_digest(db, [lost])
        assert rows == [] and sleeping == []
    finally:
        db.close()

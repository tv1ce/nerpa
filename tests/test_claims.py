"""Рекламации по точкам: привязка к адресу, группировка, сроки, реестр.

Ключевая идея механизма — претензия живёт на точке (адресе доставки), а не на
контрагенте: у клиента адресов бывает десятки, и «рекламация по контрагенту»
не отвечает на вопрос, где именно проблема.
"""
from datetime import date, timedelta

import pytest

from app.services import claims as cl
from app.services.outlets import normalize_address


# ── Фикстура данных: один контрагент, две точки, поставки и рекламации ───────

@pytest.fixture(scope="module")
def sample(admin_client):
    from app.database import SessionLocal
    from app.models import Claim, Counterparty, Order, OrderItem, Product

    db = SessionLocal()
    try:
        product = Product(name="П1.Орешки с карамелью", price=50.0)
        db.add(product)
        cp = Counterparty(name="ООО «Сетевой клиент»", trade_name="Сеть Тест",
                          inn="7811110001")
        db.add_all([product, cp])
        db.flush()

        # Две точки одного клиента, адреса написаны вразнобой
        addresses = ["г Санкт-Петербург, ул Рекламная, д 5",
                     "Санкт-Петербург, Претензионный пр-кт 12"]
        orders = []
        for i, addr in enumerate(addresses):
            for n in range(2):
                o = Order(number=f"ТЕСТ-РЕК-{i}{n}", date=date.today() - timedelta(days=10 * n + 3),
                          counterparty_id=cp.id, status="delivered", delivery_address=addr)
                db.add(o)
                db.flush()
                db.add(OrderItem(order_id=o.id, product_id=product.id, quantity=100,
                                 price=50.0, amount=5000.0))
                orders.append(o)
        db.commit()

        key_a = normalize_address(addresses[0])
        key_b = normalize_address(addresses[1])

        # Две рекламации по первой точке (повтор) и одна по второй
        made = []
        from datetime import datetime
        for num, key, addr, sev, status, days in (
            ("РЕК-ТЕСТ-0001", key_a, addresses[0], "critical", "new", 5),
            ("РЕК-ТЕСТ-0002", key_a, addresses[0], "normal", "resolved", 30),
            ("РЕК-ТЕСТ-0003", key_b, addresses[1], "low", "in_progress", 2),
        ):
            opened = datetime.combine(date.today() - timedelta(days=days),
                                      datetime.min.time())
            c = Claim(number=num, date=opened.date(),
                      counterparty_id=cp.id, address_key=key, delivery_address=addr,
                      type="quality", severity=sev, status=status, amount=1000.0,
                      description="брак", created_at=opened,
                      # закрытая — с датой закрытия, как её ставит приложение
                      resolved_at=(opened + timedelta(days=4)) if status == "resolved" else None)
            db.add(c)
            made.append(c)
        db.commit()
        return {"cp_id": cp.id, "key_a": key_a, "key_b": key_b,
                "order_a": orders[0].id, "claim_ids": [c.id for c in made]}
    finally:
        db.close()


# ── Точки контрагента ────────────────────────────────────────────────────────

def test_counterparty_outlets_are_grouped_by_address(sample):
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        outlets = cl.counterparty_outlets(db, sample["cp_id"])
    finally:
        db.close()
    keys = [o["address_key"] for o in outlets]
    assert sample["key_a"] in keys and sample["key_b"] in keys
    by_key = {o["address_key"]: o for o in outlets}
    # Разные написания одного адреса склеились в одну точку с двумя поставками
    assert len(by_key[sample["key_a"]]["orders"]) == 2
    assert by_key[sample["key_a"]]["claims_total"] == 2
    assert by_key[sample["key_a"]]["claims_open"] == 1


def test_outlets_with_open_claims_come_first(sample):
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        outlets = cl.counterparty_outlets(db, sample["cp_id"])
    finally:
        db.close()
    assert outlets[0]["claims_open"] >= outlets[-1]["claims_open"]


# ── Сроки разбора ────────────────────────────────────────────────────────────

def test_critical_claim_overdue_after_a_day():
    from app.models import Claim
    c = Claim(number="X", date=date.today() - timedelta(days=3), severity="critical",
              status="new")
    s = cl.sla_state(c)
    assert s["limit_days"] == 1 and s["overdue"] and s["overdue_by"] == 2


def test_minor_claim_not_overdue_within_a_week():
    from app.models import Claim
    c = Claim(number="X", date=date.today() - timedelta(days=4), severity="low",
              status="in_progress")
    assert not cl.sla_state(c)["overdue"]


def test_closed_claim_is_never_overdue_and_reports_duration():
    from datetime import datetime
    from app.models import Claim
    c = Claim(number="X", date=date.today() - timedelta(days=10), severity="critical",
              status="resolved",
              resolved_at=datetime.combine(date.today() - timedelta(days=6),
                                           datetime.min.time()))
    s = cl.sla_state(c)
    assert not s["overdue"] and s["closed_in"] == 4


def test_mark_status_sets_and_clears_resolved_at():
    from app.models import Claim
    c = Claim(number="X", date=date.today(), status="new")
    cl.mark_status(c, "resolved")
    assert c.resolved_at is not None
    cl.mark_status(c, "in_progress")
    assert c.resolved_at is None


# ── Группировка ──────────────────────────────────────────────────────────────

def test_group_by_outlet_puts_painful_points_first(sample):
    from app.database import SessionLocal
    from app.models import Claim
    db = SessionLocal()
    try:
        claims = db.query(Claim).filter(Claim.counterparty_id == sample["cp_id"]).all()
        groups = cl.group_by_outlet(claims)
    finally:
        db.close()
    assert len(groups) == 2
    # Точка с просроченной критичной рекламацией — первая
    assert groups[0]["address_key"] == sample["key_a"]
    assert groups[0]["overdue"] == 1
    assert len(groups[0]["claims"]) == 2


def test_claims_without_address_form_their_own_group():
    from app.models import Claim
    items = [Claim(number="A", date=date.today(), status="new", address_key=None),
             Claim(number="B", date=date.today(), status="new", address_key="ул:1")]
    groups = cl.group_by_outlet(items)
    labels = {g["address_key"]: g for g in groups}
    assert None in labels and labels[None]["label"].startswith("Без точки")


def test_summary_counts_repeat_outlets(sample):
    from app.database import SessionLocal
    from app.models import Claim
    db = SessionLocal()
    try:
        claims = db.query(Claim).filter(Claim.counterparty_id == sample["cp_id"]).all()
        stats = cl.summary(claims)
    finally:
        db.close()
    assert stats["total"] == 3 and stats["open"] == 2
    assert stats["outlets"] == 2
    assert stats["repeat_outlets"] == 1, "повторная точка — только Рекламная, 5"
    assert stats["avg_close_days"] is not None


# ── Разделение для карточки заказа ───────────────────────────────────────────

def test_order_banner_splits_claims_by_outlet(sample):
    """В заказе на точку А её претензии — отдельно от претензий по точке Б."""
    from app.database import SessionLocal
    from app.models import Order
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == sample["order_a"]).first()
        split = cl.claims_for_order(db, order)
    finally:
        db.close()
    assert [c.address_key for c in split["same"]] == [sample["key_a"]]
    assert [c.address_key for c in split["other"]] == [sample["key_b"]]


# ── Роуты ────────────────────────────────────────────────────────────────────

def test_registry_renders_in_both_views(admin_client, sample):
    for view in ("clients", "kanban"):
        r = admin_client.get("/claims/", params={"view": view})
        assert r.status_code == 200, f"{view} → {r.status_code}"
        assert "Рекламная, 5" in r.text, "точка должна быть видна в реестре"


def test_registry_survives_empty_filter_params(admin_client, sample):
    """Пустой cp_id не должен ломать реестр.

    Селект «Все контрагенты» и ссылки плиток отправляют `cp_id=` без значения —
    со строгой аннотацией int роут отвечал на это 422, и канбан с фильтрами не
    открывались вовсе.
    """
    r = admin_client.get("/claims/", params={
        "cp_id": "", "status": "", "ctype": "", "severity": "",
        "address_key": "", "q": "", "only": "", "view": "kanban",
    })
    assert r.status_code == 200


def test_registry_links_are_all_alive(admin_client, sample):
    """Все ссылки реестра открываются — включая плитки и переключатель вида."""
    import re
    page = admin_client.get("/claims/").text
    links = {u.replace("&amp;", "&")
             for u in re.findall(r'href="(/claims/\?[^"]*)"', page)}
    assert links, "в реестре должны быть ссылки фильтров"
    broken = [(admin_client.get(u).status_code, u) for u in links
              if admin_client.get(u).status_code != 200]
    assert not broken, f"битые ссылки: {broken}"


def test_registry_filters_by_outlet(admin_client, sample):
    r = admin_client.get("/claims/", params={"address_key": sample["key_a"]})
    assert r.status_code == 200
    assert "РЕК-ТЕСТ-0001" in r.text and "РЕК-ТЕСТ-0003" not in r.text


def test_registry_overdue_filter(admin_client, sample):
    r = admin_client.get("/claims/", params={"only": "overdue"})
    assert r.status_code == 200 and "РЕК-ТЕСТ-0001" in r.text


def test_outlets_json_for_form(admin_client, sample):
    rows = admin_client.get("/claims/outlets", params={"cp_id": sample["cp_id"]}).json()
    assert len(rows) == 2
    row = next(r for r in rows if r["address_key"] == sample["key_a"])
    assert row["claims_total"] == 2 and row["orders_count"] == 2
    assert row["orders"], "поставки точки нужны для выбора в форме"


def test_open_json_splits_by_delivery_address(admin_client, sample):
    data = admin_client.get("/claims/open", params={
        "counterparty_id": sample["cp_id"],
        "delivery_address": "ул. Рекламная 5, Санкт-Петербург",
    }).json()
    assert [c["number"] for c in data["same"]] == ["РЕК-ТЕСТ-0001"]
    assert [c["number"] for c in data["other"]] == ["РЕК-ТЕСТ-0003"]
    assert data["items"], "плоский список остаётся для обратной совместимости"


def test_create_claim_takes_outlet_from_order(admin_client, sample):
    """Точка подставляется из заказа — менеджеру не нужно вводить адрес руками."""
    from tests.conftest import _get_csrf
    csrf = _get_csrf(admin_client, "/claims/new")
    r = admin_client.post("/claims/new", data={
        "csrf_token": csrf,
        "claim_date": date.today().isoformat(),
        "counterparty_id": sample["cp_id"],
        "order_id": sample["order_a"],
        "address_key": "",
        "claim_type": "quality",
        "severity": "critical",
        "description": "пересорт",
        "amount": "500",
    }, follow_redirects=False)
    assert r.status_code == 302

    from app.database import SessionLocal
    from app.models import Claim
    db = SessionLocal()
    try:
        claim = db.query(Claim).order_by(Claim.id.desc()).first()
        assert claim.address_key == sample["key_a"]
        assert claim.delivery_address, "исходный адрес сохраняется для сверки глазами"
        assert claim.severity == "critical"
    finally:
        db.close()


# ── Позиции: несколько номенклатур в одной рекламации ────────────────────────

def test_parse_items_drops_rows_without_product():
    items = cl.parse_items('[{"product_id": 3, "quantity": "12,5", "amount": "100"},'
                           ' {"product_id": "", "quantity": "5"},'
                           ' {"quantity": "1"}]')
    assert len(items) == 1
    assert items[0] == {"product_id": 3, "quantity": 12.5, "amount": 100.0, "note": None}


def test_parse_items_survives_garbage():
    assert cl.parse_items("не json") == []
    assert cl.parse_items("") == []


def test_create_claim_with_several_products(admin_client, sample):
    """Несколько вкусов в одной претензии; сумма складывается из позиций."""
    from tests.conftest import _get_csrf
    from app.database import SessionLocal
    from app.models import Claim, Product

    db = SessionLocal()
    try:
        products = db.query(Product).limit(2).all()
        if len(products) < 2:
            db.add(Product(name="П1.Орешки с кокосом", price=55.0))
            db.commit()
            products = db.query(Product).limit(2).all()
        pids = [p.id for p in products]
    finally:
        db.close()

    r = admin_client.post("/claims/new", data={
        "csrf_token": _get_csrf(admin_client, "/claims/new"),
        "claim_date": date.today().isoformat(),
        "counterparty_id": sample["cp_id"],
        "order_id": sample["order_a"],
        "claim_type": "quality",
        "severity": "normal",
        "description": "два вкуса",
        "amount": "10",
        "items_json": (f'[{{"product_id": {pids[0]}, "quantity": 30, "amount": 1500}},'
                       f' {{"product_id": {pids[1]}, "quantity": 12, "amount": 600,'
                       f'   "note": "мятая упаковка"}}]'),
    }, follow_redirects=False)
    assert r.status_code == 302

    db = SessionLocal()
    try:
        claim = db.query(Claim).order_by(Claim.id.desc()).first()
        assert len(claim.items) == 2
        assert claim.amount == 2100.0, "итог складывается из сумм позиций"
        assert claim.product_id == pids[0], "первая позиция дублируется в старое поле"
        assert claim.items[1].note == "мятая упаковка"
        assert "шт" in cl.items_label(claim)
    finally:
        db.close()


def test_items_can_be_edited_after_creation(admin_client, sample):
    from tests.conftest import _get_csrf
    from app.database import SessionLocal
    from app.models import Claim, Product

    db = SessionLocal()
    try:
        claim = db.query(Claim).order_by(Claim.id.desc()).first()
        claim_id = claim.id
        pid = db.query(Product).first().id
    finally:
        db.close()

    admin_client.post(f"/claims/{claim_id}/items", data={
        "csrf_token": _get_csrf(admin_client, f"/claims/{claim_id}"),
        "items_json": f'[{{"product_id": {pid}, "quantity": 5, "amount": 250}}]',
    }, follow_redirects=False)

    db = SessionLocal()
    try:
        claim = db.query(Claim).filter(Claim.id == claim_id).first()
        assert len(claim.items) == 1 and claim.amount == 250.0
        # Правка состава попадает в ленту разбора
        assert any("Позиции" in (e["text"] or "") for e in cl.timeline(db, claim))
    finally:
        db.close()


def test_legacy_single_product_shows_in_items_label(sample):
    """У старой рекламации номенклатура одна и лежит в самой рекламации."""
    from app.database import SessionLocal
    from app.models import Claim, Product
    db = SessionLocal()
    try:
        p = db.query(Product).first()
        legacy = Claim(number="РЕК-ТЕСТ-OLD", date=date.today(),
                       counterparty_id=sample["cp_id"], product_id=p.id, quantity=7,
                       type="quality", status="new")
        db.add(legacy)
        db.commit()
        assert p.name in cl.items_label(legacy)
    finally:
        db.close()


# ── Выбор контрагента ────────────────────────────────────────────────────────

def test_client_label_shows_trade_and_legal_name(admin_client, sample):
    """В списках видно и вывеску, и юрлицо: по одной вывеске сеть не различить."""
    from app.database import SessionLocal
    from app.models import Claim
    db = SessionLocal()
    try:
        claims = db.query(Claim).filter(Claim.counterparty_id == sample["cp_id"]).all()
        groups = cl.group_by_client(claims)
    finally:
        db.close()
    assert groups[0]["name"] == "Сеть Тест · ООО «Сетевой клиент»"


def test_form_ships_client_and_product_lists(admin_client, sample):
    """Форма отдаёт списки для поиска контрагента и редактора позиций."""
    import json
    import re
    r = admin_client.get("/claims/new")
    assert r.status_code == 200
    # tojson экранирует кириллицу (С…), поэтому сверяем распарсенный список
    clients = json.loads(re.search(r"const CLIENTS\s+= (\[.*?\]);", r.text).group(1))
    mine = next(c for c in clients if c["id"] == sample["cp_id"])
    assert mine["trade"] == "Сеть Тест" and mine["name"] == "ООО «Сетевой клиент»"
    assert mine["inn"] == "7811110001", "ИНН нужен для поиска"
    assert json.loads(re.search(r"const PRODUCTS = (\[.*?\]);", r.text).group(1))


def test_claim_detail_shows_repeat_warning(admin_client, sample):
    r = admin_client.get(f"/claims/{sample['claim_ids'][0]}")
    assert r.status_code == 200
    assert "не первый раз" in r.text


def test_status_change_records_timeline_and_close_date(admin_client, sample):
    claim_id = sample["claim_ids"][2]
    from tests.conftest import _get_csrf
    r = admin_client.post(f"/claims/{claim_id}/status",
                          data={"status": "resolved", "resolution": "заменили партию",
                                "csrf_token": _get_csrf(admin_client, f"/claims/{claim_id}")},
                          follow_redirects=False)
    assert r.status_code == 302

    from app.database import SessionLocal
    from app.models import Claim
    db = SessionLocal()
    try:
        claim = db.query(Claim).filter(Claim.id == claim_id).first()
        assert claim.status == "resolved" and claim.resolved_at is not None
        events = cl.timeline(db, claim)
        assert any(e["kind"] == "status" for e in events)
        assert events[0]["at"] >= events[-1]["at"], "лента свежими вперёд"
    finally:
        db.close()


def test_update_sets_outlet_for_legacy_claim(admin_client, sample):
    """Старой рекламации без точки адрес можно указать руками."""
    from app.database import SessionLocal
    from app.models import Claim
    db = SessionLocal()
    try:
        legacy = Claim(number="РЕК-ТЕСТ-LEGACY", date=date.today(),
                       counterparty_id=sample["cp_id"], type="other", status="new")
        db.add(legacy)
        db.commit()
        legacy_id = legacy.id
    finally:
        db.close()

    from tests.conftest import _get_csrf
    admin_client.post(f"/claims/{legacy_id}/update",
                      data={"address_key": sample["key_b"], "severity": "low",
                            "assignee_id": "0", "amount": "",
                            "csrf_token": _get_csrf(admin_client, f"/claims/{legacy_id}")},
                      follow_redirects=False)
    db = SessionLocal()
    try:
        claim = db.query(Claim).filter(Claim.id == legacy_id).first()
        assert claim.address_key == sample["key_b"]
        assert claim.severity == "low"
    finally:
        db.close()


def test_outlet_claim_stats_feed_analytics(admin_client, sample):
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        stats = cl.outlet_claim_stats(db)
    finally:
        db.close()
    assert stats[sample["key_a"]]["total"] >= 2
    assert stats[sample["key_b"]]["total"] >= 1


def test_counterparty_card_groups_claims_by_outlet(admin_client, sample):
    r = admin_client.get(f"/counterparties/{sample['cp_id']}")
    assert r.status_code == 200
    assert "Рекламная, 5" in r.text and "Претензионный, 12" in r.text

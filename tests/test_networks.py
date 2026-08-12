"""Сети заведений: нормализация вывески, автоподбор групп, сводка и роуты."""
import pytest

from app.services.networks import normalize_brand, suggest_groups


def _csrf(client, path="/networks/"):
    import re
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', client.get(path).text)
    return m.group(1) if m else ""


# ── Нормализация вывески ─────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ('Кофейня "Кофе Хауз"', "кофе хауз"),
    ("ООО «Кофе Хауз»", "кофе хауз"),
    ("кофе хауз 12", "кофе хауз"),
    ("КОФЕ ХАУЗ", "кофе хауз"),
    ("ИП Иванов", "иванов"),
])
def test_normalize_brand(raw, expected):
    assert normalize_brand(raw) == expected


def test_normalize_brand_keeps_distinct_names():
    assert normalize_brand("Додо Пицца") != normalize_brand("Тесто Пицца")


# ── Автоподбор ───────────────────────────────────────────────────────────────

def test_suggest_groups_finds_same_brand(admin_client):
    from app.database import SessionLocal
    from app.models import Counterparty

    db = SessionLocal()
    try:
        for i, (name, trade) in enumerate([
            ("ИП Иванов Иван Иванович", 'Кофейня "Тест Кофе"'),
            ("ИП Петров Пётр Петрович", "ТЕСТ КОФЕ"),
            ("ООО «Одиночка»", "Одиночная точка"),
        ]):
            db.add(Counterparty(name=name, trade_name=trade, inn=f"770000000{i}"))
        db.commit()

        groups = {g["brand"]: g for g in suggest_groups(db)}
        assert "тест кофе" in groups, "две точки с одной вывеской должны попасть в группу"
        assert len(groups["тест кофе"]["counterparties"]) == 2
        assert "одиночная точка" not in groups, "одиночка — не сеть"
    finally:
        db.close()


# ── Роуты ────────────────────────────────────────────────────────────────────

def test_networks_pages_render(admin_client):
    for path in ("/networks/", "/networks/suggest", "/networks/new"):
        r = admin_client.get(path)
        assert r.status_code == 200, f"{path} → {r.status_code}"


def test_create_network_and_attach(admin_client):
    from app.database import SessionLocal
    from app.models import Counterparty, Network

    db = SessionLocal()
    try:
        cp = Counterparty(name="ООО «Точка сети»", trade_name="Сетевая точка", inn="7712345678")
        db.add(cp)
        db.commit()
        cp_id = cp.id
    finally:
        db.close()

    r = admin_client.post("/networks/new", data={
        "name": "Тестовая сеть", "kind": "franchise",
        "default_discount_pct": "7", "payment_delay_days": "14",
        "payment_delay_type": "calendar", "csrf_token": _csrf(admin_client, "/networks/new"),
    }, follow_redirects=False)
    assert r.status_code == 302

    db = SessionLocal()
    try:
        net = db.query(Network).filter(Network.name == "Тестовая сеть").first()
        assert net is not None
        net_id = net.id
    finally:
        db.close()

    r = admin_client.post(f"/networks/{net_id}/attach", data={
        "counterparty_id": cp_id, "outlet_name": "на Тверской", "use_defaults": "1",
        "csrf_token": _csrf(admin_client, f"/networks/{net_id}"),
    }, follow_redirects=False)
    assert r.status_code == 302

    db = SessionLocal()
    try:
        cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first()
        assert cp.network_id == net_id
        assert cp.outlet_name == "на Тверской"
        # условия сети подставились в точку
        assert cp.default_discount_pct == 7
        assert cp.payment_delay_days == 14
        assert cp.payment_delay_type == "calendar"
    finally:
        db.close()

    r = admin_client.get(f"/networks/{net_id}")
    assert r.status_code == 200
    assert "Сетевая точка" in r.text, "точка должна быть видна в карточке сети"

    # Сеть видна в списке и карточке контрагента
    for path in (f"/counterparties/?network_id={net_id}",
                 f"/counterparties/{cp_id}",
                 f"/counterparties/{cp_id}/edit"):
        r = admin_client.get(path)
        assert r.status_code == 200, f"{path} → {r.status_code}"
        assert "Тестовая сеть" in r.text, f"{path}: не показана сеть"

    # Расформирование: сеть скрывается, контрагент остаётся
    r = admin_client.post(f"/networks/{net_id}/delete",
                          data={"csrf_token": _csrf(admin_client, f"/networks/{net_id}/edit")},
                          follow_redirects=False)
    assert r.status_code == 302
    db = SessionLocal()
    try:
        assert db.query(Network).filter(Network.id == net_id).first().is_active is False
        cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first()
        assert cp is not None and cp.network_id is None
    finally:
        db.close()


def test_match_brand_endpoint(admin_client):
    """Подсказка в форме контрагента: вывеска узнаётся и как сеть, и как «двойники»."""
    from app.database import SessionLocal
    from app.models import Counterparty, Network

    db = SessionLocal()
    try:
        net = Network(name="Матч Кофе")
        db.add(net)
        db.add(Counterparty(name="ООО «Двойник-1»", trade_name='Кафе "Двойники"', inn="7700000101"))
        db.add(Counterparty(name="ООО «Двойник-2»", trade_name="ДВОЙНИКИ", inn="7700000102"))
        db.commit()
        net_id = net.id
    finally:
        db.close()

    m = admin_client.get("/networks/match", params={"q": 'Кофейня "Матч Кофе"'}).json()["match"]
    assert m and m["kind"] == "network" and m["id"] == net_id

    m = admin_client.get("/networks/match", params={"q": "двойники"}).json()["match"]
    assert m and m["kind"] == "twins" and m["count"] == 2

    assert admin_client.get("/networks/match", params={"q": "не-встречалось-такого"}).json()["match"] is None
    assert admin_client.get("/networks/match", params={"q": "ы"}).json()["match"] is None


def test_revenue_report_consolidates_network(admin_client):
    """Две точки одной сети складываются в одну строку разбивки выручки."""
    from datetime import date

    from app.database import SessionLocal
    from app.models import Counterparty, Network, Order, OrderItem, Product

    db = SessionLocal()
    try:
        net = Network(name="Отчётная сеть")
        db.add(net)
        db.flush()
        net_id = net.id
        product = Product(name="Орешки тестовые", price=100.0)
        db.add(product)
        db.flush()
        for i in range(2):
            cp = Counterparty(name=f"ООО «Отчётная точка {i}»", trade_name="Отчётная сеть",
                              inn=f"770000020{i}", network_id=net.id)
            db.add(cp)
            db.flush()
            order = Order(number=f"NET-TEST-{i}", date=date.today(),
                          counterparty_id=cp.id, status="delivered",
                          delivery_address=f"Тверская, {i + 1}")
            db.add(order)
            db.flush()
            db.add(OrderItem(order_id=order.id, product_id=product.id,
                             quantity=10, price=100.0, amount=1000.0))
        db.commit()
    finally:
        db.close()

    r = admin_client.get("/reports/revenue")
    assert r.status_code == 200
    assert "Отчётная сеть" in r.text
    assert "2 юрлиц(а)" in r.text, "в разбивке по сетям должно быть видно число юрлиц"

    # В карточке сети у заказов виден адрес доставки — точки в разных местах
    r = admin_client.get(f"/networks/{net_id}")
    assert r.status_code == 200
    assert "Адрес доставки" in r.text
    assert "Тверская, 1" in r.text


def test_receivables_shows_network_debt(admin_client):
    """Долг двух точек одной сети сводится в блок «Долг по сетям»."""
    from datetime import date, timedelta

    from app.database import SessionLocal
    from app.models import Counterparty, Invoice, Network

    db = SessionLocal()
    try:
        net = Network(name="Должная сеть")
        db.add(net)
        db.flush()
        for i in range(2):
            cp = Counterparty(name=f"ООО «Должник {i}»", inn=f"770000030{i}",
                              network_id=net.id, payment_delay_days=0)
            db.add(cp)
            db.flush()
            db.add(Invoice(number=f"NET-DEBT-{i}", date=date.today(), counterparty_id=cp.id,
                           status="issued", total_amount=5000.0,
                           due_date=date.today() + timedelta(days=10)))
        db.commit()
    finally:
        db.close()

    r = admin_client.get("/receivables/")
    assert r.status_code == 200
    assert "Долг по сетям" in r.text
    # 5000 + 5000 по двум точкам одной вывески
    assert "10 000,00" in r.text


def test_counterparties_network_filter(admin_client):
    """Фильтр «Вне сетей» в списке контрагентов не ломает страницу."""
    r = admin_client.get("/counterparties/?network_id=none")
    assert r.status_code == 200

"""
Тесты на конкретные фичи, добавленные в процессе подготовки к релизу:
  - /health: DB-проверка и поля ответа
  - _mark_overdue_invoices: просроченные счета переводятся в overdue
  - default_discount_pct: скидка по умолчанию сохраняется у контрагента
"""
import os
from datetime import date, timedelta


# ── /health ──────────────────────────────────────────────────────────────────

def test_health_has_db_field(client):
    """Health-check должен включать поле db и uptime_s."""
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["db"] == "ok"
    assert "db_ms" in data
    assert data["uptime_s"] >= 0


def test_health_returns_200_with_working_db(client):
    """При рабочей БД HTTP-статус 200, а не 503."""
    r = client.get("/health")
    assert r.status_code == 200, "DB недоступна — /health вернул 503"


# ── _mark_overdue_invoices ────────────────────────────────────────────────────

def test_mark_overdue_invoices(admin_client):
    """Счёт со статусом issued и просроченной датой → переводится в overdue."""
    from app.database import SessionLocal
    from app.models import Invoice, Counterparty

    db = SessionLocal()
    try:
        # Берём любого существующего контрагента (создаётся seed'ом или предыдущими тестами)
        cp = db.query(Counterparty).first()
        assert cp is not None, "Нет контрагентов в тестовой БД"

        # Создаём счёт с просроченной датой оплаты
        inv = Invoice(
            number="TEST-OVERDUE-001",
            date=date.today() - timedelta(days=10),
            counterparty_id=cp.id,
            status="issued",
            due_date=date.today() - timedelta(days=3),  # просрочен 3 дня назад
            total_amount=1000.0,
        )
        db.add(inv)
        db.commit()
        inv_id = inv.id
    finally:
        db.close()

    # Вызываем функцию напрямую (она также запускается каждый час в фоне)
    from app.main import _mark_overdue_invoices
    updated = _mark_overdue_invoices()
    assert updated >= 1, "Функция должна была обновить хотя бы один счёт"

    # Проверяем статус в БД
    db = SessionLocal()
    try:
        inv = db.query(Invoice).filter(Invoice.id == inv_id).first()
        assert inv.status == "overdue", f"Ожидался overdue, получен: {inv.status}"
    finally:
        db.close()


def test_mark_overdue_does_not_touch_paid(admin_client):
    """Оплаченный счёт с прошедшей датой не должен меняться."""
    from app.database import SessionLocal
    from app.models import Invoice, Counterparty

    db = SessionLocal()
    try:
        cp = db.query(Counterparty).first()
        inv = Invoice(
            number="TEST-PAID-001",
            date=date.today() - timedelta(days=20),
            counterparty_id=cp.id,
            status="paid",
            due_date=date.today() - timedelta(days=15),
            total_amount=500.0,
        )
        db.add(inv)
        db.commit()
        inv_id = inv.id
    finally:
        db.close()

    from app.main import _mark_overdue_invoices
    _mark_overdue_invoices()

    db = SessionLocal()
    try:
        inv = db.query(Invoice).filter(Invoice.id == inv_id).first()
        assert inv.status == "paid", "Оплаченный счёт не должен меняться"
    finally:
        db.close()


# ── default_discount_pct ──────────────────────────────────────────────────────

def test_counterparty_default_discount_saved(admin_client):
    """Скидка по умолчанию сохраняется при создании контрагента."""
    from tests.conftest import _get_csrf
    csrf = _get_csrf(admin_client, "/counterparties/new")

    r = admin_client.post("/counterparties/new", data={
        "name": "Тест Скидка ООО",
        "type": "client",
        "entity_type": "ooo",
        "default_discount_pct": "15",
        "csrf_token": csrf,
    }, follow_redirects=False)
    assert r.status_code == 302

    from app.database import SessionLocal
    from app.models import Counterparty
    db = SessionLocal()
    try:
        cp = db.query(Counterparty).filter(
            Counterparty.name == "Тест Скидка ООО"
        ).first()
        assert cp is not None, "Контрагент не создан"
        assert cp.default_discount_pct == 15.0, (
            f"Ожидалась скидка 15%, получена: {cp.default_discount_pct}"
        )
    finally:
        db.close()


def test_counterparty_discount_zero_by_default(admin_client):
    """Если скидка не указана явно, по умолчанию 0."""
    from tests.conftest import _get_csrf
    csrf = _get_csrf(admin_client, "/counterparties/new")

    r = admin_client.post("/counterparties/new", data={
        "name": "Тест Без Скидки ООО",
        "type": "client",
        "entity_type": "ooo",
        # default_discount_pct не передаётся
        "csrf_token": csrf,
    }, follow_redirects=False)
    assert r.status_code == 302

    from app.database import SessionLocal
    from app.models import Counterparty
    db = SessionLocal()
    try:
        cp = db.query(Counterparty).filter(
            Counterparty.name == "Тест Без Скидки ООО"
        ).first()
        assert cp is not None
        assert (cp.default_discount_pct or 0.0) == 0.0
    finally:
        db.close()


def test_counterparty_discount_clamped(admin_client):
    """Скидка > 100% должна быть обрезана до 100."""
    from tests.conftest import _get_csrf
    csrf = _get_csrf(admin_client, "/counterparties/new")

    r = admin_client.post("/counterparties/new", data={
        "name": "Тест Большая Скидка",
        "type": "client",
        "entity_type": "ooo",
        "default_discount_pct": "150",
        "csrf_token": csrf,
    }, follow_redirects=False)
    assert r.status_code == 302

    from app.database import SessionLocal
    from app.models import Counterparty
    db = SessionLocal()
    try:
        cp = db.query(Counterparty).filter(
            Counterparty.name == "Тест Большая Скидка"
        ).first()
        assert cp is not None
        assert cp.default_discount_pct <= 100.0, "Скидка не обрезана до 100%"
    finally:
        db.close()


# ── PWA offline ───────────────────────────────────────────────────────────────

def test_offline_html_served(client):
    """/static/offline.html доступна без авторизации."""
    r = client.get("/static/offline.html")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "Нет связи" in r.text


def test_sw_version_bumped(client):
    """Service Worker содержит актуальную версию кеша."""
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "tms-wh-v3" in r.text, "Версия SW не обновлена после добавления offline.html"

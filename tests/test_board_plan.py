"""План производства на табло цеха считает ТОЛЬКО подтверждённые заказы.

Черновик из Bitrix и бронь кабинета подтверждением не являются: сделку могли
завести по ошибке или удалить, а цех уже увидел лишние орешки и напёк их.
"""
from datetime import date, datetime, timedelta

import pytest

from app.database import SessionLocal
from app.models import (Base, CompanySettings, Counterparty, Order, OrderItem,
                        Product, ShopBooking)
from app.routers.shop import production_plan


@pytest.fixture
def db():
    from app.database import engine
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def fixtures(db):
    """Контрагент + один орешек. Всё лишнее из БД убираем, чтобы соседние
    тесты не подмешивали свои заказы в план."""
    db.query(OrderItem).delete()
    db.query(Order).delete()
    db.query(ShopBooking).delete()
    cp = Counterparty(name="ООО Тест", inn="7700000001")
    nut = Product(name="П1.Орешки с варёнкой", unit="шт")
    db.add_all([cp, nut])
    db.commit()
    return cp, nut


def _order(db, cp, nut, status, day, qty, num):
    o = Order(number=num, date=day, counterparty_id=cp.id, status=status,
              dispatch_date=day)
    db.add(o)
    db.flush()
    db.add(OrderItem(order_id=o.id, product_id=nut.id, quantity=qty, price=0, amount=0))
    db.commit()
    return o


def test_draft_order_not_in_plan(db, fixtures):
    """Неподтверждённый заказ цех не видит."""
    cp, nut = fixtures
    day = date.today() + timedelta(days=1)
    _order(db, cp, nut, "draft", day, 300, "T-draft")

    assert production_plan(db, db.query(CompanySettings).first())["date"] is None


def test_confirmed_order_in_plan(db, fixtures):
    """Подтверждённый — видит, ровно на своё количество."""
    cp, nut = fixtures
    day = date.today() + timedelta(days=1)
    _order(db, cp, nut, "confirmed", day, 300, "T-conf")

    plan = production_plan(db, db.query(CompanySettings).first())
    assert plan["date_iso"] == day.isoformat()
    assert plan["total"] == 300


def test_booking_without_order_not_in_plan(db, fixtures):
    """Свежая бронь кабинета в цифры плана не попадает: заказ ещё не подтверждён."""
    cp, nut = fixtures
    day = date.today() + timedelta(days=1)
    db.add(ShopBooking(ship_date=day, counterparty_id=cp.id, qty=99,
                       items=f'[{{"id": {nut.id}, "qty": 99}}]',
                       bitrix_deal_id="99999", created_at=datetime.now()))
    db.commit()

    assert production_plan(db, db.query(CompanySettings).first())["date"] is None


def test_booking_does_not_inflate_confirmed_order(db, fixtures):
    """Бронь по той же дате не удваивает уже подтверждённый заказ."""
    cp, nut = fixtures
    day = date.today() + timedelta(days=1)
    _order(db, cp, nut, "confirmed", day, 300, "T-both")
    db.add(ShopBooking(ship_date=day, counterparty_id=cp.id, qty=99,
                       items=f'[{{"id": {nut.id}, "qty": 99}}]',
                       bitrix_deal_id="88888", created_at=datetime.now()))
    db.commit()

    assert production_plan(db, db.query(CompanySettings).first())["total"] == 300

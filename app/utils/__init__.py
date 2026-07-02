from datetime import date, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import func


def add_banking_days(start: date, n: int) -> date:
    """Прибавляет n банковских (рабочих, пн–пт) дней к дате."""
    d = start
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:   # 0=пн … 4=пт
            added += 1
    return d


def compute_invoice_due_date(inv_date: date, contract, counterparty) -> date | None:
    """Срок оплаты счёта — по отсрочке договора, иначе по условиям оплаты контрагента.

    Договор «с отсрочкой» (payment_type=deferred) даёт payment_days календарных дней
    от даты счёта. Договор «по предоплате» — оплата в день выставления счёта.
    Без договора (или без указанных дней отсрочки) — используем условия оплаты
    контрагента (payment_delay_days/payment_delay_type, банковские либо календарные дни).
    """
    if not inv_date:
        return None
    if contract and contract.payment_type == "deferred" and contract.payment_days:
        return inv_date + timedelta(days=contract.payment_days)
    if contract and contract.payment_type == "prepay":
        return inv_date
    delay = (counterparty.payment_delay_days or 0) if counterparty else 0
    if not delay:
        return inv_date
    dtype = (counterparty.payment_delay_type or "banking") if counterparty else "banking"
    if dtype == "banking":
        return add_banking_days(inv_date, delay)
    return inv_date + timedelta(days=delay)


def log_action(
    db: Session,
    entity_type: str,
    entity_id: int,
    action: str,
    user_id: int,
    note: str,
    field: str = None,
    old_value: str = None,
    new_value: str = None,
) -> None:
    from app.models import AuditLog
    db.add(AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        field=field,
        old_value=old_value,
        new_value=new_value,
        note=note,
        user_id=user_id,
    ))


def sync_order_paid_status(db: Session, order, user_id: int = None) -> bool:
    """Подтягивает статус заказа по оплате связанных счетов.

    Если заказ по предоплате и его привязанные счета полностью оплачены — переводит
    заказ в «Оплачен» (с этого статуса он падает кладовщику на сборку). Только
    продвигает вперёд: заказы в «Собран»/«Передан»/«Доставлен»/«Отменён» не трогаем.
    Для заказов с отсрочкой шага «Оплачен» в цикле нет — статус не меняем.
    Возвращает True, если статус был изменён.
    """
    if not order or not order.is_prepay:
        return False
    if order.status not in ("draft", "confirmed"):
        return False
    invoices = [i for i in order.invoices if i.status != "cancelled"]
    if not invoices:
        return False
    order_total = order.total_amount or 0
    paid_total = sum((i.total_amount or 0) for i in invoices if i.status == "paid")
    if order_total > 0:
        is_paid = paid_total + 0.01 >= order_total
    else:
        is_paid = any(i.status == "paid" for i in invoices)
    if not is_paid:
        return False
    old = order.status
    order.status = "paid"
    log_action(db, "order", order.id, "status_changed", user_id,
               "Заказ переведён в «Оплачен» по оплате счёта",
               field="status", old_value=old, new_value="paid")
    return True


def get_balance(db: Session, product_id: int) -> float:
    from app.models import Product, StockMovement
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        return 0.0
    in_qty = db.query(func.sum(StockMovement.quantity)).filter(
        StockMovement.product_id == product_id,
        StockMovement.movement_type == "in",
    ).scalar() or 0.0
    out_qty = db.query(func.sum(StockMovement.quantity)).filter(
        StockMovement.product_id == product_id,
        StockMovement.movement_type == "out",
    ).scalar() or 0.0
    # adjustment: quantity хранится со знаком (+ излишки, - недостача)
    adj_qty = db.query(func.sum(StockMovement.quantity)).filter(
        StockMovement.product_id == product_id,
        StockMovement.movement_type == "adjustment",
    ).scalar() or 0.0
    return round((p.initial_stock or 0) + in_qty - out_qty + adj_qty, 3)


def get_balances(db: Session) -> dict:
    """Остатки всех активных товаров одним запросом (без N+1).
    Логика adjustment: со знаком (+ излишки, - недостача)."""
    from app.models import Product, StockMovement
    # Агрегируем движения по товару и типу одним GROUP BY
    rows = db.query(
        StockMovement.product_id,
        StockMovement.movement_type,
        func.sum(StockMovement.quantity),
    ).group_by(StockMovement.product_id, StockMovement.movement_type).all()

    moves: dict[int, dict] = {}
    for pid, mtype, qty in rows:
        moves.setdefault(pid, {})[mtype] = qty or 0.0

    products = db.query(Product).filter(Product.is_active == True).all()
    result = {}
    for p in products:
        m = moves.get(p.id, {})
        bal = (p.initial_stock or 0) + m.get("in", 0.0) - m.get("out", 0.0) + m.get("adjustment", 0.0)
        result[p.id] = round(bal, 3)
    return result


def maybe_notify_low_stock(db: Session, product_id: int) -> None:
    from app.models import Product, Notification
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p or not p.min_stock or p.min_stock <= 0:
        return
    balance = get_balance(db, product_id)
    if balance <= p.min_stock:
        existing = db.query(Notification).filter(
            Notification.product_id == product_id,
            Notification.type == "low_stock",
            Notification.is_read == False,
        ).first()
        if not existing:
            db.add(Notification(
                type="low_stock",
                title=f"Низкий остаток: {p.name}",
                body=f"Текущий остаток {balance} {p.unit} ≤ минимум {p.min_stock} {p.unit}",
                product_id=product_id,
                link="/warehouse/",
            ))
    else:
        db.query(Notification).filter(
            Notification.product_id == product_id,
            Notification.type == "low_stock",
            Notification.is_read == False,
        ).update({"is_read": True})

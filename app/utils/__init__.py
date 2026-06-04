from sqlalchemy.orm import Session
from sqlalchemy import func


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
            ))
    else:
        db.query(Notification).filter(
            Notification.product_id == product_id,
            Notification.type == "low_stock",
            Notification.is_read == False,
        ).update({"is_read": True})

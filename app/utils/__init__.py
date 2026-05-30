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
        StockMovement.movement_type.in_(["out", "adjustment"]),
    ).scalar() or 0.0
    return round((p.initial_stock or 0) + in_qty - out_qty, 3)


def get_balances(db: Session) -> dict:
    from app.models import Product
    products = db.query(Product).filter(Product.is_active == True).all()
    return {p.id: get_balance(db, p.id) for p in products}


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

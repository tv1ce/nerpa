from datetime import date
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.auth import login_required, role_required
from app.models import Product, StockMovement, Order
from app.utils import maybe_notify_low_stock, log_action

# Статусы заказа, считающиеся «в работе» (не черновик и не завершён/отменён)
ACTIVE_ORDER_STATUSES = ["confirmed", "paid", "assembled", "handed", "delivered"]

router = APIRouter(prefix="/warehouse", tags=["warehouse"])


@router.post("/toggle-view")
@login_required
async def toggle_view(request: Request, next: str = Form(default="/warehouse/")):
    """Переключает вид склада между мобильным и десктопным для роли warehouse."""
    current = request.session.get("warehouse_view", "mobile")
    request.session["warehouse_view"] = "desktop" if current == "mobile" else "mobile"
    return RedirectResponse(url=next, status_code=302)
templates = Jinja2Templates(directory="app/templates")

MOVEMENT_TYPES = {
    "in": "Приход",
    "out": "Расход",
    "adjustment": "Корректировка",
}
REASONS = {
    "in":  ["Поставка", "Возврат от клиента", "Начальный остаток", "Другое"],
    "out": ["Продажа", "Списание", "Брак", "Другое"],
    "adjustment": ["Инвентаризация", "Исправление ошибки", "Другое"],
}


def _assembly_queue_count(db: Session) -> int:
    """Количество заказов, ожидающих сборки — для бейджа в таббаре."""
    candidates = (
        db.query(Order)
        .filter(Order.status.in_(["confirmed", "paid"]))
        .all()
    )
    return sum(1 for o in candidates if o.ready_for_assembly)


def _get_balances(db: Session) -> dict:
    """Возвращает словарь {product_id: current_balance}.

    Логика типов движений:
      - in:         приход — увеличивает остаток
      - out:        расход — уменьшает остаток
      - adjustment: корректировка с явным знаком quantity:
                    положительное значение → увеличивает (излишки),
                    отрицательное значение → уменьшает (недостача).
                    Quantity хранится как введённое (может быть < 0).
    """
    products = db.query(Product).filter(Product.is_active == True).all()
    result = {}
    for p in products:
        in_qty = db.query(func.sum(StockMovement.quantity)).filter(
            StockMovement.product_id == p.id,
            StockMovement.movement_type == "in",
        ).scalar() or 0.0
        out_qty = db.query(func.sum(StockMovement.quantity)).filter(
            StockMovement.product_id == p.id,
            StockMovement.movement_type == "out",
        ).scalar() or 0.0
        # adjustment: quantity хранится со знаком (+ излишки, - недостача)
        adj_qty = db.query(func.sum(StockMovement.quantity)).filter(
            StockMovement.product_id == p.id,
            StockMovement.movement_type == "adjustment",
        ).scalar() or 0.0
        result[p.id] = round((p.initial_stock or 0) + in_qty - out_qty + adj_qty, 3)
    return result


# ── Главная страница: остатки ─────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
@login_required
async def warehouse_index(request: Request, db: Session = Depends(get_db)):
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()
    balances = _get_balances(db)

    # Считаем низкий остаток
    low_stock = [p for p in products if balances.get(p.id, 0) <= (p.min_stock or 0) and (p.min_stock or 0) > 0]

    # Последние 10 движений
    recent = (
        db.query(StockMovement)
        .order_by(StockMovement.created_at.desc())
        .limit(10).all()
    )

    # Очередь сборки: заказы, «упавшие» кладовщику
    # (предоплата — после оплаты, отсрочка — после подтверждения)
    assembly_candidates = (
        db.query(Order)
        .filter(Order.status.in_(["confirmed", "paid"]))
        .order_by(Order.delivery_date.asc().nullslast(), Order.date.asc())
        .all()
    )
    assembly_queue = [o for o in assembly_candidates if o.ready_for_assembly]

    return templates.TemplateResponse(request, "warehouse/index.html", {
        "products": products,
        "balances": balances,
        "low_stock": low_stock,
        "recent": recent,
        "assembly_queue": assembly_queue,
        "movement_types": MOVEMENT_TYPES,
    })


# ── Очередь сборки: кладовщик отмечает заказ собранным ────────────────────────

@router.post("/orders/{order_id}/assemble")
@login_required
async def mark_assembled(request: Request, order_id: int, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if order and order.ready_for_assembly:
        old = order.status
        order.status = "assembled"
        log_action(db, "order", order_id, "status_changed",
                   request.session.get("user_id"),
                   "Заказ собран кладовщиком",
                   field="status", old_value=old, new_value="assembled")
        # Автосписание: создаём движение "out" по каждой позиции заказа
        user_id = request.session.get("user_id")
        for item in order.items:
            db.add(StockMovement(
                product_id=item.product_id,
                movement_type="out",
                quantity=abs(item.quantity),
                date=date.today(),
                reason="Продажа",
                order_id=order_id,
                created_by_id=user_id,
            ))
            maybe_notify_low_stock(db, item.product_id)
        db.commit()
    return RedirectResponse(url="/warehouse/", status_code=302)


# ── Печатная наклейка на коробку ──────────────────────────────────────────────

@router.get("/orders/{order_id}/label", response_class=HTMLResponse)
@login_required
async def order_label(request: Request, order_id: int, db: Session = Depends(get_db)):
    """Печатная наклейка на коробку: клиент, адрес, дата, состав заказа.
    Отдельная страница без меню — удобно печатать и клеить на коробку."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return HTMLResponse("<h2 style='font-family:sans-serif'>Заказ не найден</h2>", status_code=404)
    return templates.TemplateResponse(request, "warehouse/label.html", {"order": order})


# ── Журнал движений ───────────────────────────────────────────────────────────

@router.get("/journal", response_class=HTMLResponse)
@login_required
async def journal(
    request: Request,
    product_id: int = 0,
    mtype: str = "",
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
):
    q = db.query(StockMovement).order_by(StockMovement.date.desc(), StockMovement.id.desc())
    if product_id:
        q = q.filter(StockMovement.product_id == product_id)
    if mtype:
        q = q.filter(StockMovement.movement_type == mtype)
    if date_from:
        try:
            q = q.filter(StockMovement.date >= date.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            q = q.filter(StockMovement.date <= date.fromisoformat(date_to))
        except ValueError:
            pass
    movements = q.limit(200).all()
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()

    return templates.TemplateResponse(request, "warehouse/journal.html", {
        "movements": movements,
        "products": products,
        "movement_types": MOVEMENT_TYPES,
        "product_id": product_id,
        "mtype": mtype,
        "date_from": date_from,
        "date_to": date_to,
        "assembly_queue_count": _assembly_queue_count(db),
    })


# ── Форма новой операции ──────────────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
@login_required
async def new_movement(
    request: Request,
    product_id: int = 0,
    mtype: str = "in",
    db: Session = Depends(get_db),
):
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()
    orders = (
        db.query(Order)
        .filter(Order.status.in_(ACTIVE_ORDER_STATUSES))
        .order_by(Order.date.desc()).limit(50).all()
    )
    balances = _get_balances(db)
    return templates.TemplateResponse(request, "warehouse/movement_form.html", {
        "products": products,
        "orders": orders,
        "balances": balances,
        "movement_types": MOVEMENT_TYPES,
        "reasons": REASONS,
        "selected_product": product_id,
        "selected_type": mtype,
        "today": date.today().isoformat(),
        "assembly_queue_count": _assembly_queue_count(db),
    })


@router.post("/new")
@login_required
async def create_movement(
    request: Request,
    product_id: int = Form(...),
    movement_type: str = Form(...),
    quantity: float = Form(...),
    mov_date: str = Form(...),
    reason: str = Form(default=""),
    order_id: int = Form(default=0),
    notes: str = Form(default=""),
    db: Session = Depends(get_db),
):
    linked_order_id = order_id or None
    # Валидация типа движения
    if movement_type not in ("in", "out", "adjustment"):
        movement_type = "in"
    # Для прихода/расхода quantity всегда положительный
    # Для корректировки quantity хранится со знаком (+ излишки, - недостача)
    if movement_type in ("in", "out"):
        quantity = abs(quantity)
    if quantity == 0 and movement_type != "adjustment":
        return RedirectResponse(url="/warehouse/", status_code=302)
    try:
        parsed_date = date.fromisoformat(mov_date)
    except (ValueError, TypeError):
        parsed_date = date.today()
    mv = StockMovement(
        product_id=product_id,
        movement_type=movement_type,
        quantity=quantity,
        date=parsed_date,
        reason=reason,
        order_id=linked_order_id,
        notes=notes,
        created_by_id=request.session.get("user_id"),
    )
    db.add(mv)
    if linked_order_id and movement_type == "out":
        # Отгрузка со склада → заказ передан поставщику
        order = db.query(Order).filter(Order.id == linked_order_id).first()
        if order and order.status not in ("handed", "delivered", "cancelled"):
            order.status = "handed"
    maybe_notify_low_stock(db, product_id)  # добавляет Notification без commit
    db.commit()  # единый коммит — движение + уведомление атомарно
    return RedirectResponse(url="/warehouse/", status_code=302)


# ── Удаление записи журнала ───────────────────────────────────────────────────

@router.post("/journal/{movement_id}/delete")
@role_required("manager")
async def delete_movement(request: Request, movement_id: int, db: Session = Depends(get_db)):
    mv = db.query(StockMovement).filter(StockMovement.id == movement_id).first()
    if mv:
        db.delete(mv)
        db.commit()
    return RedirectResponse(url="/warehouse/journal", status_code=302)


# ── Редактирование мин. остатка и нач. остатка прямо со страницы склада ──────

@router.post("/product/{product_id}/stock-settings")
@role_required("manager")
async def update_stock_settings(
    request: Request,
    product_id: int,
    min_stock: float = Form(default=0.0),
    initial_stock: float = Form(default=0.0),
    db: Session = Depends(get_db),
):
    p = db.query(Product).filter(Product.id == product_id).first()
    if p:
        p.min_stock = min_stock
        p.initial_stock = initial_stock
        maybe_notify_low_stock(db, product_id)  # добавляет Notification без commit
        db.commit()  # единый коммит
    return RedirectResponse(url="/warehouse/", status_code=302)

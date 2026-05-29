import json
from datetime import date
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import login_required, role_required
from app.models import Order, OrderItem, Counterparty, Product, CompanySettings, Task, Comment, AuditLog, User
from app.utils import log_action

router = APIRouter(prefix="/orders", tags=["orders"])
templates = Jinja2Templates(directory="app/templates")

ORDER_STATUSES = {
    "draft": "Черновик",
    "confirmed": "Подтверждён",
    "shipped": "Отгружен",
    "delivered": "Доставлен",
    "cancelled": "Отменён",
}


def _next_order_number(db: Session) -> str:
    from sqlalchemy import func
    max_id = db.query(func.max(Order.id)).scalar() or 0
    return str(max_id + 1)


@router.get("/", response_class=HTMLResponse)
@login_required
async def list_orders(request: Request, q: str = "", status: str = "", db: Session = Depends(get_db)):
    query = db.query(Order).join(Counterparty)
    if q:
        query = query.filter(Order.number.ilike(f"%{q}%") | Counterparty.name.ilike(f"%{q}%"))
    if status:
        query = query.filter(Order.status == status)
    orders = query.order_by(Order.date.desc(), Order.id.desc()).all()
    return templates.TemplateResponse(request, "orders/list.html", {
        "orders": orders, "q": q, "status": status, "statuses": ORDER_STATUSES,
    })


@router.get("/new", response_class=HTMLResponse)
@login_required
async def new_order(request: Request, db: Session = Depends(get_db)):
    counterparties = db.query(Counterparty).filter(
        Counterparty.is_active == True, Counterparty.type.in_(["client", "both"])
    ).order_by(Counterparty.name).all()
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()
    return templates.TemplateResponse(request, "orders/form.html", {
        "order": None, "counterparties": counterparties, "products": products,
        "statuses": ORDER_STATUSES, "suggested_number": _next_order_number(db),
    })


@router.post("/new")
@role_required("manager")
async def create_order(
    request: Request,
    number: str = Form(...),
    order_date: str = Form(...),
    counterparty_id: int = Form(...),
    status: str = Form(default="draft"),
    delivery_date: str = Form(default=""),
    delivery_address: str = Form(default=""),
    notes: str = Form(default=""),
    items_json: str = Form(default="[]"),
    db: Session = Depends(get_db),
):
    order = Order(
        number=number,
        date=date.fromisoformat(order_date),
        counterparty_id=counterparty_id,
        status=status,
        delivery_date=date.fromisoformat(delivery_date) if delivery_date else None,
        delivery_address=delivery_address,
        notes=notes,
        created_by_id=request.session.get("user_id"),
    )
    db.add(order)
    db.flush()
    items_data = json.loads(items_json)
    # Фильтруем позиции без выбранного товара (защита от невалидных данных)
    items_data = [i for i in items_data if i.get("product_id")]
    for item in items_data:
        qty = float(item["quantity"])
        price = float(item["price"])
        disc = min(max(float(item.get("discount_pct", 0)), 0), 100)
        db.add(OrderItem(
            order_id=order.id,
            product_id=int(item["product_id"]),
            quantity=qty,
            price=price,
            discount_pct=disc,
            vat_rate=float(item.get("vat_rate", 20)),
            amount=round(qty * price * (1 - disc / 100), 2),
        ))
    db.commit()
    log_action(db, "order", order.id, "created",
               request.session.get("user_id"), f"Заказ {order.number} создан")
    db.commit()
    return RedirectResponse(url=f"/orders/{order.id}", status_code=302)


@router.get("/{order_id}", response_class=HTMLResponse)
@login_required
async def view_order(request: Request, order_id: int, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return RedirectResponse(url="/orders", status_code=302)
    tasks = db.query(Task).filter(
        Task.entity_type == "order", Task.entity_id == order_id
    ).order_by(Task.status, Task.created_at).all()
    comments = db.query(Comment).filter(
        Comment.entity_type == "order", Comment.entity_id == order_id
    ).order_by(Comment.created_at).all()
    activity = db.query(AuditLog).filter(
        AuditLog.entity_type == "order", AuditLog.entity_id == order_id
    ).order_by(AuditLog.created_at.desc()).limit(50).all()
    users = db.query(User).filter(User.is_active == True).order_by(User.full_name).all()
    return templates.TemplateResponse(request, "orders/detail.html", {
        "order": order, "statuses": ORDER_STATUSES,
        "tasks": tasks, "comments": comments, "activity": activity, "users": users,
        "priority_colors": {"low": "secondary", "normal": "primary", "high": "warning", "urgent": "danger"},
    })


@router.get("/{order_id}/edit", response_class=HTMLResponse)
@login_required
async def edit_order(request: Request, order_id: int, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return RedirectResponse(url="/orders", status_code=302)
    counterparties = db.query(Counterparty).filter(Counterparty.is_active == True).order_by(Counterparty.name).all()
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()
    return templates.TemplateResponse(request, "orders/form.html", {
        "order": order, "counterparties": counterparties, "products": products,
        "statuses": ORDER_STATUSES, "suggested_number": order.number,
    })


@router.post("/{order_id}/edit")
@role_required("manager")
async def update_order(
    request: Request, order_id: int,
    number: str = Form(...),
    order_date: str = Form(...),
    counterparty_id: int = Form(...),
    status: str = Form(default="draft"),
    delivery_date: str = Form(default=""),
    delivery_address: str = Form(default=""),
    notes: str = Form(default=""),
    items_json: str = Form(default="[]"),
    db: Session = Depends(get_db),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return RedirectResponse(url="/orders", status_code=302)
    order.number = number
    order.date = date.fromisoformat(order_date)
    order.counterparty_id = counterparty_id
    order.status = status
    order.delivery_date = date.fromisoformat(delivery_date) if delivery_date else None
    order.delivery_address = delivery_address
    order.notes = notes
    for item in order.items:
        db.delete(item)
    db.flush()
    items_data = json.loads(items_json)
    items_data = [i for i in items_data if i.get("product_id")]
    for item in items_data:
        qty = float(item["quantity"])
        price = float(item["price"])
        disc = min(max(float(item.get("discount_pct", 0)), 0), 100)
        db.add(OrderItem(
            order_id=order.id,
            product_id=int(item["product_id"]),
            quantity=qty,
            price=price,
            discount_pct=disc,
            vat_rate=float(item.get("vat_rate", 20)),
            amount=round(qty * price * (1 - disc / 100), 2),
        ))
    db.commit()
    log_action(db, "order", order_id, "updated",
               request.session.get("user_id"), "Заказ отредактирован")
    db.commit()
    return RedirectResponse(url=f"/orders/{order_id}", status_code=302)


@router.post("/{order_id}/status")
@role_required("manager")
async def change_status(request: Request, order_id: int,
                        status: str = Form(...),
                        redirect_url: str = Form(default=""),
                        db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if order:
        old_status = order.status
        order.status = status
        log_action(db, "order", order_id, "status_changed",
                   request.session.get("user_id"),
                   f"Статус: {ORDER_STATUSES.get(old_status, old_status)} → {ORDER_STATUSES.get(status, status)}",
                   field="status", old_value=old_status, new_value=status)
        db.commit()
    target = redirect_url if redirect_url else f"/orders/{order_id}"
    return RedirectResponse(url=target, status_code=302)


@router.get("/{order_id}/tn", response_class=HTMLResponse)
@login_required
async def tn_form(request: Request, order_id: int, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return RedirectResponse(url="/orders", status_code=302)
    company = db.query(CompanySettings).first()
    return templates.TemplateResponse(request, "orders/tn_form.html", {
        "order": order, "company": company,
    })


@router.post("/{order_id}/tn")
@login_required
async def generate_tn(
    request: Request, order_id: int,
    carrier_name:     str = Form(default=""),
    carrier_inn:      str = Form(default=""),
    driver_name:      str = Form(default=""),
    vehicle_type:     str = Form(default=""),
    vehicle_plate:    str = Form(default=""),
    pickup_address:   str = Form(default=""),
    pickup_date:      str = Form(default=""),
    cargo_name:       str = Form(default=""),
    cargo_places:     str = Form(default=""),
    cargo_weight:     str = Form(default=""),
    cargo_volume:     str = Form(default=""),
    cargo_value:      str = Form(default=""),
    docs:             str = Form(default=""),
    delivery_address: str = Form(default=""),
    delivery_date:    str = Form(default=""),
    shipping_cost:    str = Form(default=""),
    tn_number:        str = Form(default=""),
    db: Session = Depends(get_db),
):
    from datetime import date as _date
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return RedirectResponse(url="/orders", status_code=302)
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings(name="Моя компания")

    def _parse_date(s):
        try:
            return _date.fromisoformat(s) if s else None
        except Exception:
            return None

    from app.utils.pdf_tn import TnData, generate_tn_pdf
    tn = TnData(
        order=order, company=company,
        carrier_name=carrier_name, carrier_inn=carrier_inn,
        driver_name=driver_name, vehicle_type=vehicle_type, vehicle_plate=vehicle_plate,
        pickup_address=pickup_address or None,
        pickup_date=_parse_date(pickup_date) or order.date,
        cargo_name=cargo_name, cargo_places=cargo_places,
        cargo_weight=cargo_weight, cargo_volume=cargo_volume, cargo_value=cargo_value,
        docs=docs,
        delivery_address=delivery_address or None,
        delivery_date=_parse_date(delivery_date) or order.delivery_date,
        shipping_cost=shipping_cost,
        tn_number=tn_number or str(order_id),
    )
    from urllib.parse import quote
    pdf_bytes = generate_tn_pdf(tn)
    fn_ascii  = f"tn_{order_id}.pdf"
    tn_date_s = tn.pickup_date.strftime("%d.%m.%Y") if tn.pickup_date else ""
    fn_utf8   = f"Транспортная накладная № {tn.tn_number} от {tn_date_s}.pdf"
    cd = f"attachment; filename=\"{fn_ascii}\"; filename*=UTF-8''{quote(fn_utf8)}"
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": cd})


@router.post("/{order_id}/delete")
@role_required("admin")
async def delete_order(request: Request, order_id: int, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if order:
        db.delete(order)
        db.commit()
    return RedirectResponse(url="/orders", status_code=302)

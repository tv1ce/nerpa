from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date, timedelta
from app.database import get_db
from app.auth import login_required
from app.models import Order, OrderItem, Invoice, Counterparty, Contract, Product, CompanySettings
from app.routers.orders import ORDER_STATUSES

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
@login_required
async def dashboard(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    month_start = today.replace(day=1)

    company = db.query(CompanySettings).first()
    kpi_filter = (company.kpi_product_filter if company and company.kpi_product_filter else "орешк")

    total_orders = db.query(Order).count()
    active_orders = db.query(Order).filter(Order.status.in_(["confirmed", "paid", "assembled", "handed"])).count()
    orders_this_month = db.query(Order).filter(Order.date >= month_start).count()

    total_invoices = db.query(Invoice).count()
    unpaid_invoices = db.query(Invoice).filter(Invoice.status.in_(["issued", "overdue"])).count()
    revenue_this_month = db.query(func.sum(Invoice.total_amount)).filter(
        Invoice.date >= month_start, Invoice.status == "paid"
    ).scalar() or 0.0
    total_revenue = db.query(func.sum(Invoice.total_amount)).filter(
        Invoice.status == "paid"
    ).scalar() or 0.0

    total_counterparties = db.query(Counterparty).filter(Counterparty.is_active == True).count()

    expiring_contracts = db.query(Contract).filter(
        Contract.status == "active",
        Contract.end_date <= today + timedelta(days=30),
        Contract.end_date >= today,
    ).count()

    recent_orders = (
        db.query(Order).join(Counterparty, Order.counterparty_id == Counterparty.id)
        .order_by(Order.created_at.desc()).limit(5).all()
    )
    recent_invoices = (
        db.query(Invoice).join(Counterparty)
        .order_by(Invoice.created_at.desc()).limit(5).all()
    )

    months_data = []
    for i in range(5, -1, -1):
        # Корректный сдвиг на i месяцев назад (без приближения 28 дней)
        total = (today.year * 12 + (today.month - 1)) - i
        ms = date(total // 12, total % 12 + 1, 1)
        if ms.month == 12:
            me = ms.replace(year=ms.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            me = ms.replace(month=ms.month + 1, day=1) - timedelta(days=1)
        rev = db.query(func.sum(Invoice.total_amount)).filter(
            Invoice.date >= ms, Invoice.date <= me, Invoice.status == "paid"
        ).scalar() or 0.0
        months_data.append({"month": ms.strftime("%b %Y"), "revenue": round(rev, 2)})

    top_clients = (
        db.query(Counterparty.name, func.sum(Invoice.total_amount).label("total"))
        .join(Invoice, Invoice.counterparty_id == Counterparty.id)
        .filter(Invoice.status == "paid")
        .group_by(Counterparty.id)
        .order_by(func.sum(Invoice.total_amount).desc())
        .limit(5).all()
    )

    order_status_counts = {
        s: db.query(Order).filter(Order.status == s).count()
        for s in ["draft", "confirmed", "paid", "assembled", "handed", "delivered", "cancelled"]
    }

    _sold_orders = ["confirmed", "paid", "assembled", "handed", "delivered"]
    total_nuts_sold = db.query(func.sum(OrderItem.quantity)).join(
        Order, OrderItem.order_id == Order.id
    ).join(Product, OrderItem.product_id == Product.id).filter(
        Order.status.in_(_sold_orders),
        Product.name.ilike(f"%{kpi_filter}%"),
    ).scalar() or 0.0

    nuts_this_month = db.query(func.sum(OrderItem.quantity)).join(
        Order, OrderItem.order_id == Order.id
    ).join(Product, OrderItem.product_id == Product.id).filter(
        Order.status.in_(_sold_orders),
        Order.date >= month_start,
        Product.name.ilike(f"%{kpi_filter}%"),
    ).scalar() or 0.0

    top_products = db.query(
        Product.name,
        func.sum(OrderItem.quantity).label("qty"),
    ).join(OrderItem, OrderItem.product_id == Product.id).join(
        Order, OrderItem.order_id == Order.id
    ).filter(Order.status.in_(_sold_orders)).group_by(Product.id).order_by(
        func.sum(OrderItem.quantity).desc()
    ).limit(6).all()

    return templates.TemplateResponse(request, "dashboard/index.html", {
        "total_orders": total_orders,
        "active_orders": active_orders,
        "orders_this_month": orders_this_month,
        "total_invoices": total_invoices,
        "unpaid_invoices": unpaid_invoices,
        "revenue_this_month": revenue_this_month,
        "total_revenue": total_revenue,
        "total_counterparties": total_counterparties,
        "expiring_contracts": expiring_contracts,
        "recent_orders": recent_orders,
        "recent_invoices": recent_invoices,
        "months_data": months_data,
        "top_clients": top_clients,
        "order_status_counts": order_status_counts,
        "total_nuts_sold": total_nuts_sold,
        "nuts_this_month": nuts_this_month,
        "top_products": top_products,
        "order_statuses": ORDER_STATUSES,  # L-9: не дублировать в шаблоне
    })

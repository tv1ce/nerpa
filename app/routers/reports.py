from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date, timedelta
from app.database import get_db
from app.auth import login_required, role_required
from app.models import Order, OrderItem, Invoice, Counterparty, CompanySettings, MonthlyPlan, LogisticsCost

router = APIRouter(prefix="/reports", tags=["reports"])
templates = Jinja2Templates(directory="app/templates")


def _week_bounds(d: date):
    """Возвращает (начало, конец) недели по дате."""
    start = d - timedelta(days=d.weekday())
    end = start + timedelta(days=6)
    return start, end


@router.get("/revenue", response_class=HTMLResponse)
@login_required
async def revenue_report(
    request: Request,
    period: str = "month",   # week | month | year | custom
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
):
    today = date.today()
    week_start, week_end = _week_bounds(today)
    prev_week_start = week_start - timedelta(days=7)
    prev_week_end = week_end - timedelta(days=7)
    year_start = today.replace(month=1, day=1)
    month_start = today.replace(day=1)

    # Определяем диапазон для таблицы
    if period == "week":
        tbl_from, tbl_to = week_start, week_end
    elif period == "year":
        tbl_from, tbl_to = year_start, today
    elif period == "custom" and date_from and date_to:
        try:
            tbl_from = date.fromisoformat(date_from)
            tbl_to = date.fromisoformat(date_to)
        except ValueError:
            tbl_from, tbl_to = month_start, today
    else:  # month (default)
        tbl_from, tbl_to = month_start, today

    # ── Настройки компании (план + KPI-фильтр) ───────────────────────────────
    company = db.query(CompanySettings).first()
    monthly_plan = getattr(company, "monthly_plan", None) or 0.0
    kpi_filter = (company.kpi_product_filter if company and company.kpi_product_filter else "орешк")

    # ── Таблица отгрузок ──────────────────────────────────────────────────────
    # Берём заказы за период и подтягиваем связанный счёт
    rows_q = (
        db.query(Order)
        .join(Counterparty, Order.counterparty_id == Counterparty.id)
        .filter(
            Order.date >= tbl_from,
            Order.date <= tbl_to,
        )
        .order_by(Order.date.desc())
        .all()
    )

    # Формируем строки таблицы
    shipments = []
    for order in rows_q:
        # Считаем только целевые товары (фильтр задаётся в настройках компании)
        qty = sum(
            i.quantity for i in order.items
            if kpi_filter.lower() in (i.product.name if i.product else "").lower()
        ) if order.items else 0
        # Сумма берётся напрямую из заказа — единственный источник правды
        # (счёт может включать НДС или отличаться по составу)
        amount = order.total_amount
        # Ищем связанный оплаченный счёт (только для ссылки)
        inv = next((inv for inv in order.invoices if inv.status == "paid"), None)
        shipments.append({
            "id": order.id,
            "counterparty": order.counterparty.name if order.counterparty else "—",
            "amount": amount,
            "qty": qty,
            "date": order.date,
            "status": order.status,
            "invoice_id": inv.id if inv else None,
        })

    # ── KPI ───────────────────────────────────────────────────────────────────
    # Выручка за год (оплаченные счета)
    revenue_year = db.query(func.sum(Invoice.total_amount)).filter(
        Invoice.date >= year_start,
        Invoice.status == "paid",
    ).scalar() or 0.0

    # Орешков за год (фильтр "орешк" — единообразно с таблицей отгрузок)
    from app.models import Product as ProductModel
    nuts_year = db.query(func.sum(OrderItem.quantity)).join(Order).join(
        ProductModel, OrderItem.product_id == ProductModel.id
    ).filter(
        Order.date >= year_start,
        ProductModel.name.ilike(f"%{kpi_filter}%"),
    ).scalar() or 0.0

    # Выручка текущей недели
    revenue_week = db.query(func.sum(Invoice.total_amount)).filter(
        Invoice.date >= week_start,
        Invoice.date <= week_end,
        Invoice.status == "paid",
    ).scalar() or 0.0

    # Выручка прошлой недели
    revenue_prev_week = db.query(func.sum(Invoice.total_amount)).filter(
        Invoice.date >= prev_week_start,
        Invoice.date <= prev_week_end,
        Invoice.status == "paid",
    ).scalar() or 0.0

    # Динамика недели
    if revenue_prev_week > 0:
        week_delta_pct = round((revenue_week - revenue_prev_week) / revenue_prev_week * 100, 1)
    else:
        week_delta_pct = None

    # Выручка за месяц (для плана)
    revenue_month = db.query(func.sum(Invoice.total_amount)).filter(
        Invoice.date >= month_start,
        Invoice.status == "paid",
    ).scalar() or 0.0

    plan_pct = round(revenue_month / monthly_plan * 100, 1) if monthly_plan > 0 else 0

    # Затраты на логистику за месяц и год
    logistics_month = db.query(func.sum(LogisticsCost.amount)).filter(
        LogisticsCost.date >= month_start,
    ).scalar() or 0.0
    logistics_year = db.query(func.sum(LogisticsCost.amount)).filter(
        LogisticsCost.date >= year_start,
    ).scalar() or 0.0

    # Итого по таблице (за выбранный период)
    total_amount = sum(r["amount"] for r in shipments)
    total_qty = sum(r["qty"] for r in shipments)

    return templates.TemplateResponse(request, "reports/revenue.html", {
        "shipments": shipments,
        "total_amount": total_amount,
        "total_qty": total_qty,
        # KPI
        "revenue_year": revenue_year,
        "nuts_year": nuts_year,
        "week_start": week_start,
        "week_end": week_end,
        "revenue_week": revenue_week,
        "revenue_prev_week": revenue_prev_week,
        "week_delta_pct": week_delta_pct,
        "revenue_month": revenue_month,
        "monthly_plan": monthly_plan,
        "plan_pct": plan_pct,
        "logistics_month": logistics_month,
        "logistics_year": logistics_year,
        # Фильтр
        "period": period,
        "date_from": date_from,
        "date_to": date_to,
        "tbl_from": tbl_from,
        "tbl_to": tbl_to,
    })


# ── Планы по месяцам ──────────────────────────────────────────────────────────

@router.get("/plans", response_class=HTMLResponse)
@login_required
async def plans_page(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    months_list = []
    for i in range(11, -1, -1):
        # Идём назад 12 месяцев
        month = (today.month - i - 1) % 12 + 1
        year  = today.year + ((today.month - i - 1) // 12)
        ms = date(year, month, 1)
        if month == 12:
            me = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            me = date(year, month + 1, 1) - timedelta(days=1)

        actual = db.query(func.sum(Invoice.total_amount)).filter(
            Invoice.date >= ms,
            Invoice.date <= me,
            Invoice.status == "paid",
        ).scalar() or 0.0

        plan_row = db.query(MonthlyPlan).filter(
            MonthlyPlan.year == year,
            MonthlyPlan.month == month,
        ).first()
        plan_amount = plan_row.plan_amount if plan_row else None
        diff = round(actual - plan_amount, 2) if plan_amount else None
        pct  = round(actual / plan_amount * 100, 1) if plan_amount and plan_amount > 0 else None

        months_list.append({
            "year": year, "month": month,
            "label": ms.strftime("%B %Y"),
            "plan": plan_amount,
            "actual": actual,
            "diff": diff,
            "pct": pct,
            "is_current": (year == today.year and month == today.month),
            "plan_id": plan_row.id if plan_row else None,
        })

    return templates.TemplateResponse(request, "reports/plans.html", {
        "months_list": months_list,
        "today": today,
    })


@router.post("/plans/set")
@role_required("manager")
async def set_plan(
    request: Request,
    year: int = Form(...),
    month: int = Form(...),
    plan_amount: float = Form(...),
    notes: str = Form(default=""),
    db: Session = Depends(get_db),
):
    # Валидация диапазонов
    if not (1 <= month <= 12):
        return RedirectResponse(url="/reports/plans", status_code=302)
    if not (2000 <= year <= 2100):
        return RedirectResponse(url="/reports/plans", status_code=302)
    if plan_amount < 0:
        plan_amount = 0.0
    existing = db.query(MonthlyPlan).filter(
        MonthlyPlan.year == year,
        MonthlyPlan.month == month,
    ).first()
    if existing:
        existing.plan_amount = plan_amount
        existing.notes = notes
    else:
        db.add(MonthlyPlan(year=year, month=month, plan_amount=plan_amount, notes=notes))
    db.commit()
    return RedirectResponse(url="/reports/plans", status_code=302)

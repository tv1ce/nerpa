import io
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date, timedelta
from app.database import get_db
from app.auth import login_required, role_required
from app.models import Order, OrderItem, Invoice, Counterparty, CompanySettings, MonthlyPlan, LogisticsCost

router = APIRouter(prefix="/reports", tags=["reports"])
templates = Jinja2Templates(directory="app/templates")


_LEGAL_ABBR = [
    ("Индивидуальный предприниматель", "ИП"),
    ("Общество с ограниченной ответственностью", "ООО"),
    ("Публичное акционерное общество", "ПАО"),
    ("Открытое акционерное общество", "ОАО"),
    ("Закрытое акционерное общество", "ЗАО"),
    ("Акционерное общество", "АО"),
]


def _short_legal(name: str) -> str:
    """'Индивидуальный предприниматель Иванов И.И.' → 'ИП Иванов И.И.'"""
    n = name.strip()
    for full, abbr in _LEGAL_ABBR:
        if n.lower().startswith(full.lower()):
            return abbr + n[len(full):]
    return n

XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx_response(headers: list[str], rows: list[list], filename: str, widths: list[int] | None = None):
    """Собирает .xlsx из заголовков и строк, отдаёт StreamingResponse."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from urllib.parse import quote
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    fill = PatternFill("solid", fgColor="1E3A5F")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = fill
    for r in rows:
        ws.append(r)
    if widths:
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    cd = f"attachment; filename*=UTF-8''{quote(filename)}"
    return StreamingResponse(out, media_type=XLSX_MEDIA, headers={"Content-Disposition": cd})


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
        qty = sum(
            i.quantity for i in order.items
            if kpi_filter.lower() in (i.product.name if i.product else "").lower()
        ) if order.items else 0
        amount = order.total_amount
        # Оплаченные счета по заказу
        paid_invs = [inv for inv in order.invoices if inv.status == "paid"]
        any_inv   = next(iter(order.invoices), None)
        paid_amount = sum(inv.total_amount for inv in paid_invs)
        invoice_id  = paid_invs[0].id if paid_invs else (any_inv.id if any_inv else None)
        cp = order.counterparty
        cp_name  = cp.name if cp else "—"
        cp_trade = (cp.trade_name or "").strip() if cp else ""
        cp_short = _short_legal(cp_name)
        network = cp.network if cp and cp.network_id else None
        shipments.append({
            "id": order.id,
            "counterparty":       cp_name,
            "counterparty_short": cp_short,
            "counterparty_trade": cp_trade,
            "network_id":         network.id if network else None,
            "network_name":       network.name if network else None,
            "amount": amount,
            "paid_amount": paid_amount,
            "qty": qty,
            "date": order.date,
            "status": order.status,
            "invoice_id": invoice_id,
        })

    # ── Разбивка по клиентам за период ────────────────────────────────────────
    by_client_map = {}
    for s in shipments:
        key = s["counterparty"]
        if key not in by_client_map:
            by_client_map[key] = {
                "name":  key,
                "short": s["counterparty_short"],
                "trade": s["counterparty_trade"],
                "amount": 0.0, "qty": 0, "orders": 0,
            }
        by_client_map[key]["amount"] += s["amount"]
        by_client_map[key]["qty"]    += s["qty"]
        by_client_map[key]["orders"] += 1
    _period_total = sum(c["amount"] for c in by_client_map.values()) or 0.0
    by_client = sorted(by_client_map.values(), key=lambda c: c["amount"], reverse=True)
    for c in by_client:
        c["share_pct"] = round(c["amount"] / _period_total * 100, 1) if _period_total > 0 else 0

    # ── То же самое, но с укрупнением до сетей ────────────────────────────────
    # Франчайзи одной вывески («Кофе Хауз» = десяток разных ИП) по отдельности
    # выглядят мелочью, а вместе могут быть крупнейшим клиентом. Здесь их суммы
    # складываются; контрагенты вне сетей остаются сами по себе.
    by_network_map = {}
    for s in shipments:
        if s["network_id"]:
            key = ("net", s["network_id"])
            title, subtitle = s["network_name"], "сеть"
        else:
            key = ("cp", s["counterparty"])
            title = s["counterparty_trade"] or s["counterparty_short"]
            subtitle = ""
        row = by_network_map.setdefault(key, {
            "title": title, "subtitle": subtitle, "amount": 0.0, "qty": 0,
            "orders": 0, "outlets": set(),
            "network_id": s["network_id"],
        })
        row["amount"] += s["amount"]
        row["qty"]    += s["qty"]
        row["orders"] += 1
        row["outlets"].add(s["counterparty"])
    by_network = sorted(by_network_map.values(), key=lambda c: c["amount"], reverse=True)
    for c in by_network:
        c["outlets"] = len(c["outlets"])
        c["share_pct"] = round(c["amount"] / _period_total * 100, 1) if _period_total > 0 else 0

    # ── KPI ───────────────────────────────────────────────────────────────────
    # Выручка = оплаченные счета по ДАТЕ ОПЛАТЫ (кассовый метод). Для счетов без
    # paid_date (старые записи) откатываемся на дату счёта.
    _paid_dt = func.coalesce(Invoice.paid_date, Invoice.date)

    # Выручка за год (оплаченные счета, по дате оплаты)
    revenue_year = db.query(func.sum(Invoice.total_amount)).filter(
        _paid_dt >= year_start,
        Invoice.status == "paid",
    ).scalar() or 0.0

    # Выручка за выбранный период таблицы (оплаченные счета, по дате оплаты) —
    # это и есть «настоящая» выручка, включая счета без привязки к заказу.
    revenue_period = db.query(func.sum(Invoice.total_amount)).filter(
        _paid_dt >= tbl_from, _paid_dt <= tbl_to,
        Invoice.status == "paid",
    ).scalar() or 0.0

    # Орешков за год — SQLite LOWER() не работает с кириллицей, поэтому
    # фильтруем product_id через Python, агрегируем через SQL IN().
    from app.models import Product as ProductModel
    _kpi_ids = [
        p.id for p in db.query(ProductModel.id, ProductModel.name).all()
        if kpi_filter.lower() in p.name.lower()
    ]
    if _kpi_ids:
        nuts_year = db.query(func.sum(OrderItem.quantity)).join(
            Order, OrderItem.order_id == Order.id
        ).filter(
            Order.date >= year_start,
            OrderItem.product_id.in_(_kpi_ids),
        ).scalar() or 0.0
    else:
        nuts_year = 0.0

    # Заказы текущей недели (для консистентности с таблицей — оба по дате заказа)
    _orders_week = db.query(Order).filter(
        Order.date >= week_start,
        Order.date <= week_end,
    ).all()
    invoiced_week = sum(o.total_amount for o in _orders_week)
    revenue_week  = sum(
        sum(inv.total_amount for inv in o.invoices if inv.status == "paid")
        for o in _orders_week
    )

    # Орешков за текущую неделю
    nuts_week = 0.0
    if _kpi_ids:
        nuts_week = db.query(func.sum(OrderItem.quantity)).join(
            Order, OrderItem.order_id == Order.id
        ).filter(
            Order.date >= week_start,
            Order.date <= week_end,
            OrderItem.product_id.in_(_kpi_ids),
        ).scalar() or 0.0

    # Орешков за выбранный период
    nuts_period = 0.0
    if _kpi_ids:
        nuts_period = db.query(func.sum(OrderItem.quantity)).join(
            Order, OrderItem.order_id == Order.id
        ).filter(
            Order.date >= tbl_from,
            Order.date <= tbl_to,
            OrderItem.product_id.in_(_kpi_ids),
        ).scalar() or 0.0

    # Оплаченные счета прошлой недели (по дате заказа)
    _orders_prev_week = db.query(Order).filter(
        Order.date >= prev_week_start,
        Order.date <= prev_week_end,
    ).all()
    revenue_prev_week = sum(
        sum(inv.total_amount for inv in o.invoices if inv.status == "paid")
        for o in _orders_prev_week
    )

    # Динамика недели
    if revenue_prev_week > 0:
        week_delta_pct = round((revenue_week - revenue_prev_week) / revenue_prev_week * 100, 1)
    else:
        week_delta_pct = None

    # Выручка за месяц (для плана) — по дате оплаты
    revenue_month = db.query(func.sum(Invoice.total_amount)).filter(
        _paid_dt >= month_start,
        Invoice.status == "paid",
    ).scalar() or 0.0

    plan_pct = round(revenue_month / monthly_plan * 100, 1) if monthly_plan > 0 else 0

    # ── Прогноз выполнения плана (линейная экстраполяция по темпу) ────────────
    import calendar as _cal
    days_in_month = _cal.monthrange(today.year, today.month)[1]
    days_passed = today.day
    if days_passed > 0 and revenue_month > 0:
        projected_revenue = revenue_month / days_passed * days_in_month
        forecast_pct = round(projected_revenue / monthly_plan * 100, 1) if monthly_plan > 0 else 0
    else:
        projected_revenue = 0.0
        forecast_pct = 0

    # ── Сравнение: текущий месяц vs тот же месяц прошлого года ────────────────
    ly_month_start = month_start.replace(year=month_start.year - 1)
    ly_days_in_month = _cal.monthrange(ly_month_start.year, ly_month_start.month)[1]
    ly_month_end = ly_month_start.replace(day=ly_days_in_month)
    revenue_last_year_month = db.query(func.sum(Invoice.total_amount)).filter(
        _paid_dt >= ly_month_start,
        _paid_dt <= ly_month_end,
        Invoice.status == "paid",
    ).scalar() or 0.0
    if revenue_last_year_month > 0:
        yoy_delta_pct = round((revenue_month - revenue_last_year_month) / revenue_last_year_month * 100, 1)
    else:
        yoy_delta_pct = None

    # Затраты на логистику за месяц и год (налог — как в разделе «Логистика»,
    # вносится вручную по каждой строке через LogisticsCost.tax_rate)
    from app.utils.logistics_tax import sum_taxed
    logistics_month = sum_taxed(db, month_start, today)
    logistics_year = sum_taxed(db, year_start, today)

    # ── Динамика по дням: выручка (оплаченные счета) + количество заказов ────
    # Выручка — по дате оплаты (_paid_dt), как и все KPI выше, поэтому сумма
    # по графику совпадает с карточкой выручки за период.
    _rev_by_day = dict(
        db.query(func.date(_paid_dt), func.sum(Invoice.total_amount))
        .filter(
            _paid_dt >= tbl_from,
            _paid_dt <= tbl_to,
            Invoice.status == "paid",
        )
        .group_by(func.date(_paid_dt))
        .all()
    )
    _ord_by_day: dict[str, int] = {}
    _ord_amount_by_day: dict[str, float] = {}
    for _s in shipments:
        _k = _s["date"].isoformat() if _s["date"] else None
        if not _k:
            continue
        _ord_by_day[_k] = _ord_by_day.get(_k, 0) + 1
        _ord_amount_by_day[_k] = _ord_amount_by_day.get(_k, 0.0) + _s["amount"]

    daily = []
    _d = tbl_from
    while _d <= tbl_to:
        _k = _d.isoformat()
        daily.append({
            "date": _k,
            "label": _d.strftime("%d.%m"),
            "revenue": float(_rev_by_day.get(_k) or 0.0),
            "orders": _ord_by_day.get(_k, 0),
            "shipped": round(_ord_amount_by_day.get(_k, 0.0), 2),
        })
        _d += timedelta(days=1)

    # Итого по таблице (за выбранный период)
    total_amount       = sum(r["amount"]       for r in shipments)
    total_paid_invoices= sum(r["paid_amount"]  for r in shipments)
    total_qty          = sum(r["qty"]          for r in shipments)

    return templates.TemplateResponse(request, "reports/revenue.html", {
        "shipments": shipments,
        "by_client": by_client,
        "daily": daily,
        "by_network": by_network,
        "total_amount": total_amount,
        "total_paid_invoices": total_paid_invoices,
        "total_qty": total_qty,
        # KPI
        "revenue_year": revenue_year,
        "revenue_period": revenue_period,
        "nuts_year": nuts_year,
        "nuts_period": nuts_period,
        "week_start": week_start,
        "week_end": week_end,
        "revenue_week": revenue_week,
        "invoiced_week": invoiced_week,
        "nuts_week": nuts_week,
        "revenue_prev_week": revenue_prev_week,
        "week_delta_pct": week_delta_pct,
        "revenue_month": revenue_month,
        "monthly_plan": monthly_plan,
        "plan_pct": plan_pct,
        "projected_revenue": projected_revenue,
        "forecast_pct": forecast_pct,
        "days_passed": days_passed,
        "days_in_month": days_in_month,
        "revenue_last_year_month": revenue_last_year_month,
        "yoy_delta_pct": yoy_delta_pct,
        "logistics_month": logistics_month,
        "logistics_year": logistics_year,
        # Фильтр
        "period": period,
        "date_from": date_from,
        "date_to": date_to,
        "tbl_from": tbl_from,
        "tbl_to": tbl_to,
    })


@router.get("/revenue.xlsx")
@login_required
async def revenue_export(
    request: Request,
    period: str = "month", date_from: str = "", date_to: str = "",
    db: Session = Depends(get_db),
):
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    year_start = today.replace(month=1, day=1)
    month_start = today.replace(day=1)
    if period == "week":
        tbl_from, tbl_to = week_start, week_start + timedelta(days=6)
    elif period == "year":
        tbl_from, tbl_to = year_start, today
    elif period == "custom" and date_from and date_to:
        try:
            tbl_from, tbl_to = date.fromisoformat(date_from), date.fromisoformat(date_to)
        except ValueError:
            tbl_from, tbl_to = month_start, today
    else:
        tbl_from, tbl_to = month_start, today

    company = db.query(CompanySettings).first()
    kpi_filter = (company.kpi_product_filter if company and company.kpi_product_filter else "орешк")
    orders = (
        db.query(Order).join(Counterparty, Order.counterparty_id == Counterparty.id)
        .filter(Order.date >= tbl_from, Order.date <= tbl_to)
        .order_by(Order.date.desc()).all()
    )
    rows = []
    for o in orders:
        qty = sum(i.quantity for i in o.items
                  if kpi_filter.lower() in (i.product.name if i.product else "").lower()) if o.items else 0
        rows.append([
            o.number,
            o.counterparty.name if o.counterparty else "—",
            o.date.strftime("%d.%m.%Y") if o.date else "",
            round(o.total_amount, 2),
            int(qty),
            o.status,
        ])
    fn = f"Выручка {tbl_from.strftime('%d.%m.%Y')}-{tbl_to.strftime('%d.%m.%Y')}.xlsx"
    return _xlsx_response(
        ["№ заказа", "Клиент", "Дата", "Сумма ₽", "Орешков", "Статус"],
        rows, fn, widths=[14, 40, 14, 16, 12, 16],
    )


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

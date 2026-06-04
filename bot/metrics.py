"""
Функции сбора метрик из базы TMS.
Каждая функция принимает db-сессию и возвращает словарь с данными.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models import (
    Order, OrderItem, Invoice, Counterparty, Contract,
    LogisticsCost, Claim, MonthlyPlan, Product, StockMovement,
    SalesLead, User,
)


def get_callbacks_today(db: Session) -> list[dict]:
    """Точки прозвона, по которым перезвон назначен на сегодня или просрочен."""
    rows = (
        db.query(SalesLead)
        .filter(
            SalesLead.is_active == True,
            SalesLead.callback_at.isnot(None),
            SalesLead.callback_at <= date.today(),
            SalesLead.call_status.notin_(["deal", "refused", "invalid"]),
        )
        .order_by(SalesLead.callback_at, SalesLead.assigned_to_id)
        .all()
    )
    result = []
    for lead in rows:
        result.append({
            "name": lead.name,
            "phone": lead.phone or "",
            "manager": lead.assigned_to.full_name if lead.assigned_to else None,
            "overdue": lead.callback_at < date.today(),
            "date": lead.callback_at,
        })
    return result


def _month_bounds(d: date):
    ms = d.replace(day=1)
    last = calendar.monthrange(ms.year, ms.month)[1]
    me = ms.replace(day=last)
    return ms, me


def _week_bounds(d: date):
    start = d - timedelta(days=d.weekday())
    end = start + timedelta(days=6)
    return start, end


def _fmt(amount: float) -> str:
    return f"{amount:,.0f}".replace(",", " ")


# ─────────────────────────────────────────────────────────────────────────────
# DAILY
# ─────────────────────────────────────────────────────────────────────────────

def get_daily_metrics(db: Session, day: date | None = None) -> dict:
    """Метрики за один день (по умолчанию сегодня)."""
    today = day or date.today()

    orders_today = db.query(Order).filter(Order.date == today).count()
    orders_shipped = db.query(Order).filter(
        Order.date == today,
        Order.status.in_(["shipped", "delivered"]),
    ).count()

    revenue_today = db.query(func.sum(Invoice.total_amount)).filter(
        Invoice.date == today,
        Invoice.status == "paid",
    ).scalar() or 0.0

    issued_today = db.query(Invoice).filter(Invoice.date == today).count()

    qty_today = (
        db.query(func.sum(OrderItem.quantity))
        .join(Order)
        .filter(
            Order.date == today,
            Order.status.in_(["shipped", "delivered"]),
        )
        .scalar() or 0.0
    )

    # Просроченные счета на сегодня
    overdue_count = db.query(Invoice).filter(Invoice.status == "overdue").count()
    overdue_sum = db.query(func.sum(Invoice.total_amount)).filter(
        Invoice.status == "overdue"
    ).scalar() or 0.0

    # Активные задачи
    new_claims = db.query(Claim).filter(Claim.status == "new").count()

    return {
        "date": today,
        "orders_today": orders_today,
        "orders_shipped": orders_shipped,
        "revenue_today": revenue_today,
        "issued_today": issued_today,
        "qty_today": qty_today,
        "overdue_count": overdue_count,
        "overdue_sum": overdue_sum,
        "new_claims": new_claims,
    }


# ─────────────────────────────────────────────────────────────────────────────
# WEEKLY
# ─────────────────────────────────────────────────────────────────────────────

def get_weekly_metrics(db: Session, ref_date: date | None = None) -> dict:
    """Метрики за текущую неделю + сравнение с прошлой."""
    today = ref_date or date.today()
    ws, we = _week_bounds(today)
    pws, pwe = ws - timedelta(days=7), we - timedelta(days=7)

    def _rev(d_from, d_to):
        return db.query(func.sum(Invoice.total_amount)).filter(
            Invoice.date >= d_from,
            Invoice.date <= d_to,
            Invoice.status == "paid",
        ).scalar() or 0.0

    def _qty(d_from, d_to):
        return (
            db.query(func.sum(OrderItem.quantity))
            .join(Order)
            .filter(
                Order.date >= d_from,
                Order.date <= d_to,
                Order.status.in_(["shipped", "delivered"]),
            )
            .scalar() or 0.0
        )

    rev_week = _rev(ws, we)
    rev_prev = _rev(pws, pwe)
    qty_week = _qty(ws, we)
    qty_prev = _qty(pws, pwe)

    delta_rev = round((rev_week - rev_prev) / rev_prev * 100, 1) if rev_prev else None
    delta_qty = round((qty_week - qty_prev) / qty_prev * 100, 1) if qty_prev else None

    orders_week = db.query(Order).filter(
        Order.date >= ws, Order.date <= we
    ).count()

    new_clients = db.query(Counterparty).filter(
        func.date(Counterparty.created_at) >= ws,
        func.date(Counterparty.created_at) <= we,
        Counterparty.type.in_(["client", "both"]),
    ).count()

    logistics_week = db.query(func.sum(LogisticsCost.amount)).filter(
        LogisticsCost.date >= ws,
        LogisticsCost.date <= we,
    ).scalar() or 0.0

    # Топ-3 клиента за неделю
    top_clients = (
        db.query(Counterparty.name, func.sum(Invoice.total_amount).label("total"))
        .join(Invoice, Invoice.counterparty_id == Counterparty.id)
        .filter(
            Invoice.date >= ws, Invoice.date <= we,
            Invoice.status == "paid",
        )
        .group_by(Counterparty.id)
        .order_by(func.sum(Invoice.total_amount).desc())
        .limit(3)
        .all()
    )

    # Счета выставленные, но не оплаченные за неделю
    unpaid_issued = db.query(func.sum(Invoice.total_amount)).filter(
        Invoice.date >= ws, Invoice.date <= we,
        Invoice.status.in_(["issued", "overdue"]),
    ).scalar() or 0.0

    return {
        "week_start": ws,
        "week_end": we,
        "prev_week_start": pws,
        "prev_week_end": pwe,
        "revenue_week": rev_week,
        "revenue_prev": rev_prev,
        "delta_rev_pct": delta_rev,
        "qty_week": qty_week,
        "qty_prev": qty_prev,
        "delta_qty_pct": delta_qty,
        "orders_week": orders_week,
        "new_clients": new_clients,
        "logistics_week": logistics_week,
        "top_clients": top_clients,
        "unpaid_issued": unpaid_issued,
    }


# ─────────────────────────────────────────────────────────────────────────────
# MONTHLY
# ─────────────────────────────────────────────────────────────────────────────

def get_monthly_metrics(db: Session, ref_date: date | None = None) -> dict:
    """Полные метрики за месяц."""
    today = ref_date or date.today()
    ms, me = _month_bounds(today)
    year_start = today.replace(month=1, day=1)

    prev_ms, prev_me = _month_bounds(ms - timedelta(days=1))

    def _rev(d_from, d_to):
        return db.query(func.sum(Invoice.total_amount)).filter(
            Invoice.date >= d_from, Invoice.date <= d_to,
            Invoice.status == "paid",
        ).scalar() or 0.0

    revenue_month = _rev(ms, me)
    revenue_prev  = _rev(prev_ms, prev_me)
    revenue_year  = _rev(year_start, me)

    delta_month = round((revenue_month - revenue_prev) / revenue_prev * 100, 1) if revenue_prev else None

    # План
    plan_row = db.query(MonthlyPlan).filter(
        MonthlyPlan.year == today.year,
        MonthlyPlan.month == today.month,
    ).first()
    plan_amount = plan_row.plan_amount if plan_row else None
    plan_pct = round(revenue_month / plan_amount * 100, 1) if plan_amount else None

    # Орешки
    def _qty(d_from, d_to):
        return (
            db.query(func.sum(OrderItem.quantity))
            .join(Order)
            .filter(
                Order.date >= d_from, Order.date <= d_to,
                Order.status.in_(["shipped", "delivered"]),
            )
            .scalar() or 0.0
        )

    qty_month = _qty(ms, me)
    qty_prev  = _qty(prev_ms, prev_me)
    delta_qty = round((qty_month - qty_prev) / qty_prev * 100, 1) if qty_prev else None

    # Заказы
    orders_month = db.query(Order).filter(Order.date >= ms, Order.date <= me).count()
    orders_new_clients = (
        db.query(Order)
        .join(Counterparty, Order.counterparty_id == Counterparty.id)
        .filter(
            Order.date >= ms, Order.date <= me,
            func.date(Counterparty.created_at) >= ms,
            Counterparty.type.in_(["client", "both"]),
        )
        .count()
    )

    # Логистика
    logistics_month = db.query(func.sum(LogisticsCost.amount)).filter(
        LogisticsCost.date >= ms, LogisticsCost.date <= me,
    ).scalar() or 0.0
    logistics_year = db.query(func.sum(LogisticsCost.amount)).filter(
        LogisticsCost.date >= year_start,
    ).scalar() or 0.0

    margin_month = revenue_month - logistics_month

    # Дебиторка
    unpaid_total = db.query(func.sum(Invoice.total_amount)).filter(
        Invoice.status.in_(["issued", "overdue"]),
    ).scalar() or 0.0
    overdue_total = db.query(func.sum(Invoice.total_amount)).filter(
        Invoice.status == "overdue",
    ).scalar() or 0.0
    overdue_count = db.query(Invoice).filter(Invoice.status == "overdue").count()

    # Топ-5 клиентов
    top_clients = (
        db.query(Counterparty.name, func.sum(Invoice.total_amount).label("total"))
        .join(Invoice, Invoice.counterparty_id == Counterparty.id)
        .filter(
            Invoice.date >= ms, Invoice.date <= me,
            Invoice.status == "paid",
        )
        .group_by(Counterparty.id)
        .order_by(func.sum(Invoice.total_amount).desc())
        .limit(5)
        .all()
    )

    # Топ продукты за месяц
    top_products = (
        db.query(Product.name, func.sum(OrderItem.quantity).label("qty"))
        .join(OrderItem, OrderItem.product_id == Product.id)
        .join(Order, OrderItem.order_id == Order.id)
        .filter(
            Order.date >= ms, Order.date <= me,
            Order.status.in_(["shipped", "delivered"]),
        )
        .group_by(Product.id)
        .order_by(func.sum(OrderItem.quantity).desc())
        .limit(5)
        .all()
    )

    # Рекламации за месяц
    claims_new = db.query(Claim).filter(
        func.date(Claim.created_at) >= ms,
        func.date(Claim.created_at) <= me,
    ).count()
    claims_resolved = db.query(Claim).filter(
        func.date(Claim.updated_at) >= ms,
        func.date(Claim.updated_at) <= me,
        Claim.status == "resolved",
    ).count()

    # Договоры истекающие в следующие 30 дней
    expiring_contracts = db.query(Contract).filter(
        Contract.status == "active",
        Contract.end_date >= today,
        Contract.end_date <= today + timedelta(days=30),
    ).count()

    return {
        "month_start": ms,
        "month_end": me,
        "month_label": ms.strftime("%B %Y"),
        "prev_month_label": prev_ms.strftime("%B %Y"),
        "revenue_month": revenue_month,
        "revenue_prev": revenue_prev,
        "delta_month_pct": delta_month,
        "revenue_year": revenue_year,
        "plan_amount": plan_amount,
        "plan_pct": plan_pct,
        "qty_month": qty_month,
        "qty_prev": qty_prev,
        "delta_qty_pct": delta_qty,
        "orders_month": orders_month,
        "orders_new_clients": orders_new_clients,
        "logistics_month": logistics_month,
        "logistics_year": logistics_year,
        "margin_month": margin_month,
        "unpaid_total": unpaid_total,
        "overdue_total": overdue_total,
        "overdue_count": overdue_count,
        "top_clients": top_clients,
        "top_products": top_products,
        "claims_new": claims_new,
        "claims_resolved": claims_resolved,
        "expiring_contracts": expiring_contracts,
    }

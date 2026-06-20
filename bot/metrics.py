"""
Функции сбора метрик из базы TMS.
Каждая функция принимает db-сессию и возвращает словарь с данными.
"""
from __future__ import annotations

import calendar
import os
from datetime import date, timedelta
from zoneinfo import ZoneInfo

# Дата берётся в TZ бота, чтобы отчёт в 20:00 МСК был за сегодня, а не завтра
_TZ = ZoneInfo(os.getenv("TMS_TZ", "Europe/Moscow"))


def _today() -> date:
    """Текущая дата в часовом поясе бота (не UTC системы)."""
    from datetime import datetime
    return datetime.now(tz=_TZ).date()

from sqlalchemy import func
from sqlalchemy.orm import Session

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models import (
    Order, OrderItem, Invoice, Counterparty, Contract,
    LogisticsCost, Claim, MonthlyPlan, Product, StockMovement,
    SalesLead, User, CompanySettings,
)


def get_callbacks_today(db: Session) -> list[dict]:
    """Точки прозвона, по которым перезвон назначен на сегодня или просрочен."""
    rows = (
        db.query(SalesLead)
        .filter(
            SalesLead.is_active == True,
            SalesLead.callback_at.isnot(None),
            SalesLead.callback_at <= _today(),
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
            "overdue": lead.callback_at < _today(),
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


def _resolve_plan_amount(db: Session, year: int, month: int) -> float | None:
    """План месяца: сначала помесячная строка `monthly_plans`, иначе — глобальная
    настройка `CompanySettings.monthly_plan` (так же, как дашборд /reports).
    0 трактуется как «план не задан» → None."""
    plan_row = (
        db.query(MonthlyPlan)
        .filter(MonthlyPlan.year == year, MonthlyPlan.month == month)
        .first()
    )
    if plan_row and plan_row.plan_amount:
        return plan_row.plan_amount
    company = db.query(CompanySettings).first()
    amount = getattr(company, "monthly_plan", None) if company else None
    return amount or None


def _fmt(amount: float) -> str:
    return f"{amount:,.0f}".replace(",", " ")


# Статусы, считающиеся «отгружено» (собрано/передано/доставлено)
_SHIPPED = ["assembled", "handed", "delivered"]


# ─────────────────────────────────────────────────────────────────────────────
# DAILY
# ─────────────────────────────────────────────────────────────────────────────

def get_daily_metrics(db: Session, day: date | None = None) -> dict:
    """Метрики за один день (по умолчанию сегодня)."""
    today = day or _today()

    orders_today = db.query(Order).filter(Order.date == today).count()
    orders_shipped = db.query(Order).filter(
        Order.date == today,
        Order.status.in_(_SHIPPED),
    ).count()

    # Сумма отгрузок = итоги строк заказов, отгруженных сегодня
    shipped_amount_today = (
        db.query(func.sum(OrderItem.amount))
        .join(Order, OrderItem.order_id == Order.id)
        .filter(
            Order.date == today,
            Order.status.in_(_SHIPPED),
        )
        .scalar() or 0.0
    )

    # Оплаты — по дате поступления денег (paid_date), не по дате выставления счёта
    paid_today = db.query(func.sum(Invoice.total_amount)).filter(
        Invoice.paid_date == today,
        Invoice.status == "paid",
    ).scalar() or 0.0

    issued_today = db.query(Invoice).filter(Invoice.date == today).count()

    qty_today = (
        db.query(func.sum(OrderItem.quantity))
        .join(Order)
        .filter(
            Order.date == today,
            Order.status.in_(_SHIPPED),
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
        "shipped_amount_today": shipped_amount_today,
        "paid_today": paid_today,
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
    today = ref_date or _today()
    ws, we = _week_bounds(today)
    pws, pwe = ws - timedelta(days=7), we - timedelta(days=7)

    # Оплаты: по дате поступления денег (paid_date)
    def _paid(d_from, d_to):
        return db.query(func.sum(Invoice.total_amount)).filter(
            Invoice.paid_date >= d_from,
            Invoice.paid_date <= d_to,
            Invoice.status == "paid",
        ).scalar() or 0.0

    # Отгрузки: сумма позиций отгруженных заказов по дате заказа
    def _shipped_amt(d_from, d_to):
        return (
            db.query(func.sum(OrderItem.amount))
            .join(Order, OrderItem.order_id == Order.id)
            .filter(
                Order.date >= d_from, Order.date <= d_to,
                Order.status.in_(_SHIPPED),
            )
            .scalar() or 0.0
        )

    def _qty(d_from, d_to):
        return (
            db.query(func.sum(OrderItem.quantity))
            .join(Order)
            .filter(
                Order.date >= d_from,
                Order.date <= d_to,
                Order.status.in_(_SHIPPED),
            )
            .scalar() or 0.0
        )

    paid_week      = _paid(ws, we)
    paid_prev      = _paid(pws, pwe)
    shipped_week   = _shipped_amt(ws, we)
    shipped_prev   = _shipped_amt(pws, pwe)
    qty_week       = _qty(ws, we)
    qty_prev       = _qty(pws, pwe)

    delta_paid     = round((paid_week - paid_prev) / paid_prev * 100, 1) if paid_prev else None
    delta_shipped  = round((shipped_week - shipped_prev) / shipped_prev * 100, 1) if shipped_prev else None
    delta_qty      = round((qty_week - qty_prev) / qty_prev * 100, 1) if qty_prev else None

    orders_week = db.query(Order).filter(
        Order.date >= ws, Order.date <= we
    ).count()

    new_clients = db.query(Counterparty).filter(
        func.date(Counterparty.created_at) >= ws,
        func.date(Counterparty.created_at) <= we,
        Counterparty.type.in_(["client", "both"]),
    ).count()

    _logi_raw_week = db.query(func.sum(LogisticsCost.amount)).filter(
        LogisticsCost.date >= ws,
        LogisticsCost.date <= we,
    ).scalar() or 0.0
    _TAX = 1.06
    logistics_week = round(_logi_raw_week * _TAX, 2)

    logistics_per_order_week = round(logistics_week / orders_week, 2) if orders_week else 0.0

    # Топ-3 клиента по отгрузкам за неделю
    top_clients = (
        db.query(Counterparty.name, func.sum(OrderItem.amount).label("total"))
        .join(Order, Order.counterparty_id == Counterparty.id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .filter(
            Order.date >= ws, Order.date <= we,
            Order.status.in_(_SHIPPED),
        )
        .group_by(Counterparty.id)
        .order_by(func.sum(OrderItem.amount).desc())
        .limit(3)
        .all()
    )

    # Счета выставленные, но не оплаченные за неделю
    unpaid_issued = db.query(func.sum(Invoice.total_amount)).filter(
        Invoice.date >= ws, Invoice.date <= we,
        Invoice.status.in_(["issued", "overdue"]),
    ).scalar() or 0.0

    # Статус месяца: оплаты с начала месяца vs план
    ms, _me = _month_bounds(today)
    paid_month_so_far = db.query(func.sum(Invoice.total_amount)).filter(
        Invoice.paid_date >= ms, Invoice.paid_date <= today,
        Invoice.status == "paid",
    ).scalar() or 0.0
    shipped_month_so_far = _shipped_amt(ms, today)

    plan_amount = _resolve_plan_amount(db, today.year, today.month)
    plan_pct = round(paid_month_so_far / plan_amount * 100, 1) if plan_amount else None
    plan_remaining = max(plan_amount - paid_month_so_far, 0) if plan_amount else None

    # Орешки за всё время
    qty_all_time = (
        db.query(func.sum(OrderItem.quantity))
        .join(Order)
        .filter(Order.status.in_(_SHIPPED))
        .scalar() or 0.0
    )

    # Оплаты за текущий год
    year_start = today.replace(month=1, day=1)
    paid_year = db.query(func.sum(Invoice.total_amount)).filter(
        Invoice.paid_date >= year_start, Invoice.paid_date <= today,
        Invoice.status == "paid",
    ).scalar() or 0.0

    return {
        "week_start": ws,
        "week_end": we,
        "prev_week_start": pws,
        "prev_week_end": pwe,
        # отгрузки
        "shipped_week": shipped_week,
        "shipped_prev": shipped_prev,
        "delta_shipped_pct": delta_shipped,
        # оплаты
        "paid_week": paid_week,
        "paid_prev": paid_prev,
        "delta_paid_pct": delta_paid,
        # орешки
        "qty_week": qty_week,
        "qty_prev": qty_prev,
        "delta_qty_pct": delta_qty,
        # прочее
        "orders_week": orders_week,
        "new_clients": new_clients,
        "logistics_week": logistics_week,
        "logistics_per_order_week": logistics_per_order_week,
        "top_clients": top_clients,
        "unpaid_issued": unpaid_issued,
        # статус месяца
        "paid_month_so_far": paid_month_so_far,
        "shipped_month_so_far": shipped_month_so_far,
        "plan_amount": plan_amount,
        "plan_pct": plan_pct,
        "plan_remaining": plan_remaining,
        # сводные
        "qty_all_time": qty_all_time,
        "paid_year": paid_year,
    }


# ─────────────────────────────────────────────────────────────────────────────
# MONTHLY
# ─────────────────────────────────────────────────────────────────────────────

def get_monthly_metrics(db: Session, ref_date: date | None = None) -> dict:
    """Полные метрики за месяц."""
    today = ref_date or _today()
    ms, me = _month_bounds(today)
    year_start = today.replace(month=1, day=1)

    prev_ms, prev_me = _month_bounds(ms - timedelta(days=1))

    # Оплаты: по дате поступления денег (paid_date)
    def _paid(d_from, d_to):
        return db.query(func.sum(Invoice.total_amount)).filter(
            Invoice.paid_date >= d_from, Invoice.paid_date <= d_to,
            Invoice.status == "paid",
        ).scalar() or 0.0

    # Отгрузки: сумма позиций отгруженных заказов по дате заказа
    def _shipped_amt(d_from, d_to):
        return (
            db.query(func.sum(OrderItem.amount))
            .join(Order, OrderItem.order_id == Order.id)
            .filter(
                Order.date >= d_from, Order.date <= d_to,
                Order.status.in_(_SHIPPED),
            )
            .scalar() or 0.0
        )

    paid_month   = _paid(ms, me)
    paid_prev    = _paid(prev_ms, prev_me)
    paid_year    = _paid(year_start, me)
    shipped_month = _shipped_amt(ms, me)
    shipped_prev  = _shipped_amt(prev_ms, prev_me)

    delta_paid    = round((paid_month - paid_prev) / paid_prev * 100, 1) if paid_prev else None
    delta_shipped = round((shipped_month - shipped_prev) / shipped_prev * 100, 1) if shipped_prev else None

    # План vs оплаты
    plan_amount = _resolve_plan_amount(db, today.year, today.month)
    plan_pct = round(paid_month / plan_amount * 100, 1) if plan_amount else None

    # Орешки
    def _qty(d_from, d_to):
        return (
            db.query(func.sum(OrderItem.quantity))
            .join(Order)
            .filter(
                Order.date >= d_from, Order.date <= d_to,
                Order.status.in_(_SHIPPED),
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
    _TAX = 1.06
    _logi_raw_month = db.query(func.sum(LogisticsCost.amount)).filter(
        LogisticsCost.date >= ms, LogisticsCost.date <= me,
    ).scalar() or 0.0
    logistics_month = round(_logi_raw_month * _TAX, 2)
    logistics_year = round((db.query(func.sum(LogisticsCost.amount)).filter(
        LogisticsCost.date >= year_start,
    ).scalar() or 0.0) * _TAX, 2)

    _carrier_name = os.getenv("LOGI_CARRIER_NAME", "Гоголев Николай Николаевич")
    from app.models import Counterparty as _CP
    _carrier = db.query(_CP).filter(_CP.name.ilike(f"%{_carrier_name}%")).first()
    _carrier_id = _carrier.id if _carrier else None
    _carrier_orders_month = (
        db.query(func.count(Order.id)).filter(
            Order.date >= ms,
            Order.date <= me,
            Order.carrier_id == _carrier_id,
        ).scalar() or 0
    ) if _carrier_id else 0
    logistics_per_order_month = round(logistics_month / _carrier_orders_month, 2) if _carrier_orders_month else 0.0

    # Маржа = отгрузки − логистика (отгрузки точнее отражают реальный объём)
    margin_month = shipped_month - logistics_month

    # Дебиторка
    unpaid_total = db.query(func.sum(Invoice.total_amount)).filter(
        Invoice.status.in_(["issued", "overdue"]),
    ).scalar() or 0.0
    overdue_total = db.query(func.sum(Invoice.total_amount)).filter(
        Invoice.status == "overdue",
    ).scalar() or 0.0
    overdue_count = db.query(Invoice).filter(Invoice.status == "overdue").count()

    # Топ-5 клиентов по отгрузкам за месяц
    top_clients = (
        db.query(Counterparty.name, func.sum(OrderItem.amount).label("total"))
        .join(Order, Order.counterparty_id == Counterparty.id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .filter(
            Order.date >= ms, Order.date <= me,
            Order.status.in_(_SHIPPED),
        )
        .group_by(Counterparty.id)
        .order_by(func.sum(OrderItem.amount).desc())
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
            Order.status.in_(_SHIPPED),
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
        # отгрузки
        "shipped_month": shipped_month,
        "shipped_prev": shipped_prev,
        "delta_shipped_pct": delta_shipped,
        # оплаты
        "paid_month": paid_month,
        "paid_prev": paid_prev,
        "delta_paid_pct": delta_paid,
        "paid_year": paid_year,
        # план
        "plan_amount": plan_amount,
        "plan_pct": plan_pct,
        # орешки
        "qty_month": qty_month,
        "qty_prev": qty_prev,
        "delta_qty_pct": delta_qty,
        # заказы
        "orders_month": orders_month,
        "orders_new_clients": orders_new_clients,
        # логистика и маржа
        "logistics_month": logistics_month,
        "logistics_per_order_month": logistics_per_order_month,
        "logistics_year": logistics_year,
        "margin_month": margin_month,
        # дебиторка
        "unpaid_total": unpaid_total,
        "overdue_total": overdue_total,
        "overdue_count": overdue_count,
        # топы
        "top_clients": top_clients,
        "top_products": top_products,
        # прочее
        "claims_new": claims_new,
        "claims_resolved": claims_resolved,
        "expiring_contracts": expiring_contracts,
    }

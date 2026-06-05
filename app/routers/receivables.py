from datetime import date, timedelta
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import login_required
from app.models import Invoice

router = APIRouter(prefix="/receivables", tags=["receivables"])
templates = Jinja2Templates(directory="app/templates")


def add_banking_days(start: date, n: int) -> date:
    """Прибавляет n банковских (рабочих, пн–пт) дней к дате."""
    d = start
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:   # 0=пн … 4=пт
            added += 1
    return d


def overdue_deadline(inv: Invoice) -> date | None:
    """Возвращает дату, ПОСЛЕ которой счёт считается просроченным."""
    if not inv.due_date:
        return None
    cp = inv.counterparty
    delay = (cp.payment_delay_days or 0) if cp else 0
    dtype = (cp.payment_delay_type or "banking") if cp else "banking"
    if delay == 0:
        return inv.due_date
    if dtype == "banking":
        return add_banking_days(inv.due_date, delay)
    else:
        return inv.due_date + timedelta(days=delay)


def refresh_overdue(db: Session) -> None:
    """Автоматически переводит просроченные счета в статус 'overdue'."""
    today = date.today()
    candidates = (
        db.query(Invoice)
        .filter(Invoice.status == "issued", Invoice.due_date.isnot(None))
        .all()
    )
    changed = False
    for inv in candidates:
        deadline = overdue_deadline(inv)
        if deadline and today > deadline:
            inv.status = "overdue"
            changed = True
    if changed:
        db.commit()


@router.get("/", response_class=HTMLResponse)
@login_required
async def receivables_list(request: Request, db: Session = Depends(get_db)):
    refresh_overdue(db)

    today = date.today()
    invoices = (
        db.query(Invoice)
        .filter(Invoice.status.in_(["issued", "overdue"]))
        .order_by(Invoice.due_date.asc().nullslast())
        .all()
    )

    rows = []
    for inv in invoices:
        deadline = overdue_deadline(inv)
        if deadline:
            delta = (today - deadline).days
            is_overdue = delta > 0
            days_overdue = delta if is_overdue else 0
            days_left = (deadline - today).days if not is_overdue else 0
        else:
            is_overdue = False
            days_overdue = 0
            days_left = None

        cp = inv.counterparty
        delay_days = (cp.payment_delay_days or 0) if cp else 0
        delay_type = (cp.payment_delay_type or "banking") if cp else "banking"

        rows.append({
            "invoice": inv,
            "deadline": deadline,
            "is_overdue": is_overdue,
            "days_overdue": days_overdue,
            "days_left": days_left,
            "delay_days": delay_days,
            "delay_type": delay_type,
        })

    total_amount = sum(r["invoice"].total_amount for r in rows)
    overdue_amount = sum(r["invoice"].total_amount for r in rows if r["is_overdue"])
    overdue_count = sum(1 for r in rows if r["is_overdue"])
    max_overdue_days = max((r["days_overdue"] for r in rows if r["is_overdue"]), default=0)

    # ── Группировка по контрагентам ──────────────────────────────────────────
    from collections import OrderedDict
    grouped: "OrderedDict[int, dict]" = OrderedDict()
    for r in rows:
        inv = r["invoice"]
        cp = inv.counterparty
        cp_id = inv.counterparty_id or 0
        if cp_id not in grouped:
            grouped[cp_id] = {
                "cp_id": cp_id,
                "cp_name": (cp.short_name or cp.name) if cp else "—",
                "rows": [],
                "total": 0.0,
                "overdue_total": 0.0,
                "overdue_count": 0,
                "max_overdue": 0,
            }
        g = grouped[cp_id]
        g["rows"].append(r)
        g["total"] += inv.total_amount
        if r["is_overdue"]:
            g["overdue_total"] += inv.total_amount
            g["overdue_count"] += 1
            g["max_overdue"] = max(g["max_overdue"], r["days_overdue"])
    # Сортируем: сначала с просрочкой, потом по сумме долга
    groups = sorted(
        grouped.values(),
        key=lambda g: (g["overdue_count"] > 0, g["total"]),
        reverse=True,
    )

    return templates.TemplateResponse(request, "receivables/index.html", {
        "rows": rows,
        "groups": groups,
        "total_amount": total_amount,
        "overdue_amount": overdue_amount,
        "overdue_count": overdue_count,
        "max_overdue_days": max_overdue_days,
        "today": today,
    })

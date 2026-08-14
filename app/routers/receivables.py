from collections import OrderedDict
from datetime import date, timedelta
from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from app.database import get_db
from app.auth import login_required
from app.models import Invoice
from app.utils import add_banking_days

router = APIRouter(prefix="/receivables", tags=["receivables"])
templates = Jinja2Templates(directory="app/templates")

# Открытый долг — всё, что выставлено и не закрыто деньгами. «partial» тоже долг:
# частично оплаченный счёт гасится на остаток, а не исчезает из дебиторки.
OPEN_STATUSES = ("issued", "overdue", "partial")

# Горизонты прогноза, доступные в интерфейсе (недель)
HORIZONS = (4, 8, 12, 26)

# Корзины старения просрочки: до скольких дней включительно + подпись
AGING_BUCKETS = (
    (7,    "1–7 дн."),
    (30,   "8–30 дн."),
    (60,   "31–60 дн."),
    (None, "60+ дн."),
)


def overdue_deadline(inv: Invoice) -> date | None:
    """Возвращает дату, ПОСЛЕ которой счёт считается просроченным.

    Это срок оплаты плюс индивидуальная отсрочка контрагента (в банковских или
    календарных днях). Единая точка правды: этой же датой пользуются фоновая
    авто-простановка статуса overdue и напоминания в app/main.py.
    """
    if not inv.due_date:
        return None
    cp = inv.counterparty
    delay = (cp.payment_delay_days or 0) if cp else 0
    dtype = (cp.payment_delay_type or "banking") if cp else "banking"
    if delay == 0:
        return inv.due_date
    if dtype == "banking":
        return add_banking_days(inv.due_date, delay)
    return inv.due_date + timedelta(days=delay)


def outstanding(inv: Invoice) -> float:
    """Непогашенный остаток счёта: итог минус уже полученные деньги."""
    return round((inv.total_amount or 0.0) - (inv.paid_amount or 0.0), 2)


def week_start(d: date) -> date:
    """Понедельник недели, в которую попадает дата."""
    return d - timedelta(days=d.weekday())


def month_end(d: date) -> date:
    """Последний день месяца, в который попадает дата."""
    if d.month == 12:
        return d.replace(day=31)
    return d.replace(month=d.month + 1, day=1) - timedelta(days=1)


def aging_label(days: int) -> str:
    """Подпись корзины старения для просрочки в N дней."""
    for limit, label in AGING_BUCKETS:
        if limit is None or days <= limit:
            return label
    return AGING_BUCKETS[-1][1]


def open_invoices(db: Session) -> list[Invoice]:
    """Все открытые счета с подтянутым контрагентом (без N+1 на отсрочку и сеть)."""
    return (
        db.query(Invoice)
        .options(joinedload(Invoice.counterparty))
        .filter(Invoice.status.in_(OPEN_STATUSES))
        .all()
    )


def build_rows(invoices: list[Invoice], today: date) -> list[dict]:
    """Строки реестра: остаток долга, срок с отсрочкой, просрочка и её возраст.

    Просрочка считается по дате, а не по статусу счёта: частично оплаченный счёт
    в БД лежит как «partial» и статуса «overdue» уже не получит, но долг по нему
    просрочен ровно так же.
    """
    rows = []
    for inv in invoices:
        debt = outstanding(inv)
        if debt <= 0.01:
            continue  # закрыт деньгами, статус ещё не пересчитан — не долг
        deadline = overdue_deadline(inv)
        is_overdue = bool(deadline) and deadline < today
        days_overdue = (today - deadline).days if is_overdue else 0
        cp = inv.counterparty
        rows.append({
            "invoice": inv,
            "outstanding": debt,
            "is_partial": (inv.paid_amount or 0) > 0,
            "deadline": deadline,
            "is_overdue": is_overdue,
            "days_overdue": days_overdue,
            "days_left": (deadline - today).days if deadline and not is_overdue else None,
            "aging": aging_label(days_overdue) if is_overdue else None,
            "delay_days": (cp.payment_delay_days or 0) if cp else 0,
            "delay_type": (cp.payment_delay_type or "banking") if cp else "banking",
        })
    return rows


def build_forecast(rows: list[dict], today: date, n_weeks: int) -> dict:
    """Прогноз поступлений: остатки долга, разложенные по неделям вперёд.

    Каждый открытый счёт ждём к дате overdue_deadline. Раскладка:
      — срок уже прошёл  → корзина «Просрочено» (деньги ждём, но срок сорван);
      — в горизонте      → своя неделя;
      — за горизонтом    → «Позже»;
      — счёт без срока   → «Без срока» — это не ноль, а неизвестность, и прятать
                           её нельзя: иначе прогноз молча занижен.

    Накопительный итог считается ТОЛЬКО по недельным корзинам, без просрочки:
    смешивать сорванный срок с плановым — значит выдавать желаемое за прогноз.
    Просрочку показываем отдельной строкой, а в интерфейсе даём переключатель.
    """
    cur_ws = week_start(today)
    horizon_end = cur_ws + timedelta(weeks=n_weeks)  # exclusive

    weeks = [{
        "start": cur_ws + timedelta(weeks=i),
        "end": cur_ws + timedelta(weeks=i, days=6),
        "index": i,
        "total": 0.0, "count": 0, "rows": [], "cumulative": 0.0, "is_gap": False,
    } for i in range(n_weeks)]

    overdue = {"total": 0.0, "count": 0, "rows": []}
    later = {"total": 0.0, "count": 0, "rows": []}
    no_date = {"total": 0.0, "count": 0, "rows": []}

    for r in rows:
        deadline = r["deadline"]
        if not deadline:
            bucket = no_date
        elif r["is_overdue"]:
            bucket = overdue
        elif deadline >= horizon_end:
            bucket = later
        else:
            bucket = weeks[(deadline - cur_ws).days // 7]
        bucket["total"] += r["outstanding"]
        bucket["count"] += 1
        bucket["rows"].append(r)

    running = 0.0
    for w in weeks:
        running += w["total"]
        w["cumulative"] = running
        w["is_gap"] = w["total"] == 0
        w["rows"].sort(key=lambda r: r["deadline"])
    for b in (overdue, later, no_date):
        b["rows"].sort(key=lambda r: (r["deadline"] or date.max, -r["outstanding"]))

    first_gap = next((w for w in weeks if w["is_gap"]), None)
    # Ровно то, что показывает график: недели горизонта, без просрочки и без
    # денег за горизонтом — иначе подпись и линия накопления расходятся.
    planned_total = sum(w["total"] for w in weeks)

    return {
        "weeks": weeks,
        "overdue": overdue,
        "later": later,
        "no_date": no_date,
        "n_weeks": n_weeks,
        "planned_total": planned_total,           # в горизонте, по сроку, без просрочки
        "expected_total": planned_total + later["total"] + overdue["total"] + no_date["total"],
        "max_week": max((w["total"] for w in weeks), default=0.0),
        "first_gap": first_gap,
    }


def build_aging(rows: list[dict]) -> list[dict]:
    """Старение просрочки: сколько денег висит в каждой корзине по возрасту."""
    buckets = OrderedDict((label, {"label": label, "total": 0.0, "count": 0})
                          for _, label in AGING_BUCKETS)
    for r in rows:
        if r["is_overdue"]:
            b = buckets[r["aging"]]
            b["total"] += r["outstanding"]
            b["count"] += 1
    total = sum(b["total"] for b in buckets.values())
    for b in buckets.values():
        b["pct"] = (b["total"] / total * 100) if total else 0
    return list(buckets.values())


def build_groups(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Реестр, свёрнутый по контрагентам, и сводка долга по сетям.

    Точки одной вывески по отдельности выглядят мелочью, поэтому держим их рядом
    и взвешиваем по долгу всей сети: переговоры идут с УК, а не с точкой.
    """
    grouped: "OrderedDict[int, dict]" = OrderedDict()
    for r in rows:
        inv = r["invoice"]
        cp = inv.counterparty
        cp_id = inv.counterparty_id or 0
        g = grouped.get(cp_id)
        if g is None:
            g = grouped[cp_id] = {
                "cp_id": cp_id,
                "cp_name": (cp.short_name or cp.name) if cp else "—",
                "network_id": cp.network_id if cp else None,
                "network_name": cp.network.name if cp and cp.network_id else None,
                "outlet_name": cp.outlet_name if cp else None,
                "rows": [], "total": 0.0, "overdue_total": 0.0,
                "overdue_count": 0, "max_overdue": 0, "nearest": None,
            }
        g["rows"].append(r)
        g["total"] += r["outstanding"]
        if r["is_overdue"]:
            g["overdue_total"] += r["outstanding"]
            g["overdue_count"] += 1
            g["max_overdue"] = max(g["max_overdue"], r["days_overdue"])
        elif r["deadline"] and (g["nearest"] is None or r["deadline"] < g["nearest"]):
            g["nearest"] = r["deadline"]

    net_totals: dict[int, dict] = {}
    for g in grouped.values():
        g["rows"].sort(key=lambda r: (r["deadline"] or date.max))
        if not g["network_id"]:
            continue
        t = net_totals.setdefault(g["network_id"], {
            "name": g["network_name"], "total": 0.0, "overdue_total": 0.0, "outlets": 0,
        })
        t["total"] += g["total"]
        t["overdue_total"] += g["overdue_total"]
        t["outlets"] += 1
    for g in grouped.values():
        t = net_totals.get(g["network_id"]) or {}
        g["network_total"] = t.get("total")
        g["network_outlets"] = t.get("outlets", 0)

    groups = sorted(
        grouped.values(),
        key=lambda g: (g["overdue_count"] > 0,
                       g["network_total"] or g["total"],
                       g["network_id"] or 0,
                       g["total"]),
        reverse=True,
    )
    networks_debt = sorted(
        ({"id": nid, **t} for nid, t in net_totals.items() if t["outlets"] > 1),
        key=lambda t: t["total"], reverse=True,
    )
    return groups, networks_debt


@router.get("/", response_class=HTMLResponse)
@login_required
async def receivables_list(
    request: Request,
    weeks: int = Query(8, description="горизонт прогноза, недель"),
    db: Session = Depends(get_db),
):
    # Статусы счетов проставляет фоновый проход (_overdue_loop раз в час) —
    # открытие страницы больше ничего не пишет в БД. Просрочку для показа
    # считаем от даты, поэтому реестр верен и между проходами.
    today = date.today()
    n_weeks = weeks if weeks in HORIZONS else 8

    rows = build_rows(open_invoices(db), today)
    forecast = build_forecast(rows, today, n_weeks)
    aging = build_aging(rows)
    groups, networks_debt = build_groups(rows)

    horizon_7 = today + timedelta(days=7)
    m_end = month_end(today)

    return templates.TemplateResponse(request, "receivables/index.html", {
        "rows": rows,
        "groups": groups,
        "networks_debt": networks_debt,
        "forecast": forecast,
        "aging": aging,
        "horizons": HORIZONS,
        "n_weeks": n_weeks,
        "total_amount": sum(r["outstanding"] for r in rows),
        "overdue_amount": forecast["overdue"]["total"],
        "overdue_count": forecast["overdue"]["count"],
        "max_overdue_days": max((r["days_overdue"] for r in rows if r["is_overdue"]), default=0),
        # Ближайшие деньги: что должно прийти за 7 дней и до конца месяца —
        # без просрочки, чтобы цифра означала «план», а не «надежда».
        "due_7d": sum(r["outstanding"] for r in rows
                      if r["deadline"] and not r["is_overdue"] and r["deadline"] <= horizon_7),
        "due_month": sum(r["outstanding"] for r in rows
                         if r["deadline"] and not r["is_overdue"] and r["deadline"] <= m_end),
        "month_end": m_end,
        "today": today,
    })

"""Полноэкранное табло для цеха (digital signage через liqvid.ru).

Страница доступна без логина — её показывает Android-TV приставка, которая
не умеет авторизовываться. Поэтому доступ защищён токеном в URL: ?key=...
Токен задаётся переменной окружения TMS_BOARD_KEY (по умолчанию — для разработки).

URL для liqvid:  https://<сервер>/board?key=<токен>
"""
import os
import time
from datetime import date, timedelta
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models import Order, OrderItem, Claim, CompanySettings, Product, User

router = APIRouter(prefix="/board", tags=["board"])
templates = Jinja2Templates(directory="app/templates")

# Токен доступа к табло. На проде задать переменную окружения TMS_BOARD_KEY.
BOARD_KEY = os.getenv("TMS_BOARD_KEY", "tseh2026")

# Метка запуска сервера — клиенты следят за ней и перезагружают страницу при изменении
SERVER_START = int(time.time())

# Статусы заказов, считающиеся отгрузкой.
# Орешки и выручка попадают в табло только ПОСЛЕ того, как кладовщик нажал
# «Собрано» (статус assembled). До сборки (confirmed/paid) заказ не учитываем.
_SOLD = ["assembled", "handed", "delivered"]

# Значения по умолчанию, если в настройках пусто
DEFAULT_QUOTES = [
    "Каждый орешек — это улыбка клиента 🥜",
    "Качество — наша подпись на каждой упаковке",
    "Сегодня работаем на рекорд!",
    "Маленькие орешки — большая ответственность",
    "Сделано с душой — отгружено вовремя",
    "Команда, которая отгружает — команда, которая побеждает 💪",
    "Чисто на участке — порядок в голове",
    "Твой вклад виден в каждой цифре на табло",
]

# Примеры станций (проверь/замени в настройках). Потоки Radio Record стабильны.
DEFAULT_STATIONS = [
    {"name": "Радио Рекорд", "url": "https://radiorecord.hostingradio.ru/rr_main96.aacp"},
    {"name": "Record Chill House", "url": "https://radiorecord.hostingradio.ru/chillhouse96.aacp"},
    {"name": "Европа Плюс", "url": "https://ep128.hostingradio.ru/ep128"},
    {"name": "Ретро FM", "url": "https://retro128.hostingradio.ru/retro128"},
    {"name": "DFM", "url": "https://dfm.hostingradio.ru/dfm96.aacp"},
]


def _check_key(request: Request) -> bool:
    return request.query_params.get("key") == BOARD_KEY


def parse_quotes(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return DEFAULT_QUOTES
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


def parse_stations(raw: str | None) -> list[dict]:
    """Каждая строка вида 'Название | URL'."""
    if not raw or not raw.strip():
        return DEFAULT_STATIONS
    out = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln or "|" not in ln:
            continue
        name, url = ln.split("|", 1)
        name, url = name.strip(), url.strip()
        if name and url:
            out.append({"name": name, "url": url})
    return out or DEFAULT_STATIONS


def _collect_metrics(db: Session) -> dict:
    today = date.today()
    month_start = today.replace(day=1)
    year_start  = today.replace(month=1, day=1)

    # Прошлый месяц
    last_month_end   = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)

    # KPI-фильтр берём из настроек компании (так же как в dashboard и reports).
    # SQLite lower() не работает с Кириллицей, поэтому фильтруем на Python-стороне
    # и передаём в SQL готовый список product_id — тот же подход, что в dashboard.
    _company_pre = db.query(CompanySettings).first()
    _kpi = (_company_pre.kpi_product_filter if _company_pre and _company_pre.kpi_product_filter else "орешк")
    _kpi_ids = [
        p.id for p in db.query(Product.id, Product.name).all()
        if _kpi.lower() in p.name.lower()
    ]

    # Дата отгрузки = дата сборки (нажатие «Собрано»). Для старых заказов,
    # собранных до появления assembled_at, откатываемся на дату заказа.
    ship_date = func.coalesce(func.date(Order.assembled_at), func.date(Order.date))

    def _nuts(*filters):
        if not _kpi_ids:
            return 0
        q = db.query(func.sum(OrderItem.quantity)).join(
            Order, OrderItem.order_id == Order.id
        ).filter(
            Order.status.in_(_SOLD),
            OrderItem.product_id.in_(_kpi_ids),
        )
        for f in filters:
            q = q.filter(f)
        return int(q.scalar() or 0)

    def _revenue(*filters):
        q = db.query(func.sum(OrderItem.amount)).join(
            Order, OrderItem.order_id == Order.id
        ).filter(Order.status.in_(_SOLD))
        for f in filters:
            q = q.filter(f)
        return int(q.scalar() or 0)

    shipped_total = _nuts()
    shipped_month = _nuts(ship_date >= month_start)
    shipped_today = _nuts(ship_date == today)
    shipped_year  = _nuts(ship_date >= year_start)

    last_month_nuts    = _nuts(ship_date >= last_month_start, ship_date <= last_month_end)
    last_month_revenue = _revenue(ship_date >= last_month_start, ship_date <= last_month_end)

    revenue_month = _revenue(ship_date >= month_start)

    company      = db.query(CompanySettings).first()
    plan         = int(company.board_nuts_plan or 0) if company else 0
    revenue_plan = float(company.monthly_plan or 0.0) if company else 0.0
    company_name = (company.brand_name or company.short_name or company.name) if company else "Производство"
    logo_path    = company.logo_path if company and company.logo_path else None
    # Превращаем путь хранения "app/static/uploads/logo.png" → "/static/uploads/logo.png"
    if logo_path:
        logo_path = "/" + logo_path.lstrip("/").replace("app/static", "static", 1) if logo_path.startswith("app/static") else logo_path
    quotes       = parse_quotes(company.board_quotes if company else None)
    stations     = parse_stations(company.board_stations if company else None)
    active       = (company.board_active_station or 0) if company else 0
    if active >= len(stations):
        active = 0

    nut_price        = float(company.board_nut_price or 52.0)    if company else 52.0
    cost_pct         = float(company.board_cost_pct or 0.0)      if company else 0.0
    cost_norm_pct    = float(company.board_cost_norm_pct or 48.0) if company else 48.0
    cost_deviation   = float(company.board_cost_deviation or 5.0) if company else 5.0
    shift_start      = (company.board_shift_start or "09:00")    if company else "09:00"
    shift_end        = (company.board_shift_end   or "17:00")    if company else "17:00"

    plan_pct     = round(shipped_month / plan * 100) if plan > 0 else 0
    revenue_pct  = round(revenue_month / revenue_plan * 100) if revenue_plan > 0 else 0

    shipped_month_money = int(shipped_month * nut_price)
    shipped_today_money = int(shipped_today * nut_price)
    shipped_year_money  = int(shipped_year  * nut_price)
    shipped_total_money = int(shipped_total * nut_price)

    last_claim_date    = db.query(func.max(Claim.date)).scalar()
    days_without_claims = (today - last_claim_date).days if last_claim_date else None

    # ── Дни рождения сотрудников ──────────────────────────────────────────────
    birthdays_today = []
    birthdays_upcoming = []
    users_with_bd = db.query(User).filter(
        User.is_active == True, User.birthday.isnot(None)
    ).all()
    for u in users_with_bd:
        bd = u.birthday
        if bd.month == today.month and bd.day == today.day:
            birthdays_today.append(u.full_name)
            continue
        # Ближайшие ДР в пределах 7 дней вперёд (без учёта года)
        for delta in range(1, 8):
            d = today + timedelta(days=delta)
            if bd.month == d.month and bd.day == d.day:
                birthdays_upcoming.append({
                    "name": u.full_name,
                    "date": d.strftime("%d.%m"),
                    "in_days": delta,
                })
                break
    birthdays_upcoming.sort(key=lambda x: x["in_days"])

    MONTHS_RU = ["январь","февраль","март","апрель","май","июнь",
                 "июль","август","сентябрь","октябрь","ноябрь","декабрь"]
    last_month_name = f"{MONTHS_RU[last_month_end.month - 1]} {last_month_end.year}"

    # Рекорд дня текущего месяца (только KPI-товары), по дате отгрузки (сборки)
    best_day_row = (
        db.query(
            ship_date.label("day"),
            func.sum(OrderItem.quantity).label("total"),
        )
        .join(Order, OrderItem.order_id == Order.id)
        .filter(
            Order.status.in_(_SOLD),
            ship_date >= month_start,
            OrderItem.product_id.in_(_kpi_ids) if _kpi_ids else False,
        )
        .group_by(ship_date)
        .order_by(func.sum(OrderItem.quantity).desc())
        .first()
    )
    record_day = int(best_day_row.total) if best_day_row else 0
    is_record_today = bool(
        best_day_row
        and str(best_day_row.day) == str(today)
        and shipped_today > 0
    )

    return {
        "company_name":        company_name,
        "logo_path":           logo_path,
        "shipped_total":       shipped_total,
        "shipped_total_money": shipped_total_money,
        "shipped_month":       shipped_month,
        "shipped_month_money": shipped_month_money,
        "shipped_today":       shipped_today,
        "shipped_today_money": shipped_today_money,
        "shipped_year":        shipped_year,
        "shipped_year_money":  shipped_year_money,
        "plan":                plan,
        "plan_pct":            plan_pct,
        "revenue_month":       revenue_month,
        "revenue_plan":        int(revenue_plan),
        "revenue_pct":         revenue_pct,
        "days_without_claims": days_without_claims,
        "record_day":          record_day,
        "is_record_today":     is_record_today,
        "last_month_nuts":     last_month_nuts,
        "last_month_revenue":  last_month_revenue,
        "last_month_name":     last_month_name,
        "nut_price":           nut_price,
        "cost_pct":            cost_pct,
        "cost_norm_pct":       cost_norm_pct,
        "cost_deviation":      cost_deviation,
        "shift_start":         shift_start,
        "shift_end":           shift_end,
        "quotes":              quotes,
        "stations":            stations,
        "active_station":      active,
        "birthdays_today":     birthdays_today,
        "birthdays_upcoming":  birthdays_upcoming,
        "server_version":      SERVER_START,
    }


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def board_page(request: Request, db: Session = Depends(get_db)):
    if not _check_key(request):
        return HTMLResponse("403 — неверный ключ доступа", status_code=403)
    data = _collect_metrics(db)
    return templates.TemplateResponse(request, "board/index.html", {
        "data": data,
        "key": BOARD_KEY,
    })


@router.get("/data")
async def board_data(request: Request, db: Session = Depends(get_db)):
    if not _check_key(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse(_collect_metrics(db))


@router.post("/station")
async def set_station(request: Request, db: Session = Depends(get_db)):
    """Переключение активной станции (с табло по клику или из настроек)."""
    if not _check_key(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        idx = int(request.query_params.get("i", 0))
    except (TypeError, ValueError):
        idx = 0
    company = db.query(CompanySettings).first()
    if company:
        company.board_active_station = idx
        db.commit()
    return JSONResponse({"active_station": idx})

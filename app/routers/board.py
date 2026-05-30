"""Полноэкранное табло для цеха (digital signage через liqvid.ru).

Страница доступна без логина — её показывает Android-TV приставка, которая
не умеет авторизовываться. Поэтому доступ защищён токеном в URL: ?key=...
Токен задаётся переменной окружения TMS_BOARD_KEY (по умолчанию — для разработки).

URL для liqvid:  https://<сервер>/board?key=<токен>
"""
import os
from datetime import date
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models import Order, OrderItem, Claim, CompanySettings

router = APIRouter(prefix="/board", tags=["board"])
templates = Jinja2Templates(directory="app/templates")

# Токен доступа к табло. На проде задать переменную окружения TMS_BOARD_KEY.
BOARD_KEY = os.getenv("TMS_BOARD_KEY", "tseh2026")

# Статусы заказов, считающиеся отгрузкой
_SOLD = ["confirmed", "shipped", "delivered"]

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
    year_start = today.replace(month=1, day=1)

    def _nuts(*filters):
        q = db.query(func.sum(OrderItem.quantity)).join(
            Order, OrderItem.order_id == Order.id
        ).filter(Order.status.in_(_SOLD))
        for f in filters:
            q = q.filter(f)
        return int(q.scalar() or 0)

    shipped_total = _nuts()
    shipped_month = _nuts(Order.date >= month_start)
    shipped_today = _nuts(Order.date == today)
    shipped_year = _nuts(Order.date >= year_start)

    company = db.query(CompanySettings).first()
    plan = int(company.board_nuts_plan or 0) if company else 0
    company_name = (company.short_name or company.name) if company else "Производство"
    quotes = parse_quotes(company.board_quotes if company else None)
    stations = parse_stations(company.board_stations if company else None)
    active = (company.board_active_station or 0) if company else 0
    if active >= len(stations):
        active = 0

    plan_pct = round(shipped_month / plan * 100) if plan > 0 else 0

    last_claim_date = db.query(func.max(Claim.date)).scalar()
    days_without_claims = (today - last_claim_date).days if last_claim_date else None

    return {
        "company_name": company_name,
        "shipped_total": shipped_total,
        "shipped_month": shipped_month,
        "shipped_today": shipped_today,
        "shipped_year": shipped_year,
        "plan": plan,
        "plan_pct": plan_pct,
        "days_without_claims": days_without_claims,
        "quotes": quotes,
        "stations": stations,
        "active_station": active,
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

"""Полноэкранное табло для цеха (digital signage через liqvid.ru).

Страница доступна без логина — её показывает Android-TV приставка, которая
не умеет авторизовываться. Поэтому доступ защищён токеном в URL: ?key=...
Токен задаётся переменной окружения NERPA_BOARD_KEY (по умолчанию — для разработки).

URL для liqvid:  https://<сервер>/board?key=<токен>
"""
import re
import ssl
import socket
import time
import threading
import logging
from urllib.parse import urlparse
from datetime import date, timedelta
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models import Order, OrderItem, Invoice, Claim, CompanySettings, Product, User
from app.env import getenv as env_get

router = APIRouter(prefix="/board", tags=["board"])
templates = Jinja2Templates(directory="app/templates")

# Токен доступа к табло. Задать переменную окружения NERPA_BOARD_KEY (обязательно).
_board_key_raw = env_get("NERPA_BOARD_KEY", "")
if not _board_key_raw:
    import sys
    print(
        "FATAL: переменная окружения NERPA_BOARD_KEY не задана. "
        "Задайте её в .env (например: NERPA_BOARD_KEY=<случайная строка>). "
        "Табло будет недоступно без этого ключа.",
        file=sys.stderr,
    )
BOARD_KEY = _board_key_raw or "BOARD_KEY_NOT_SET"

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
        # Разрешаем только http/https URL (защита от javascript: и data: схем)
        if name and url and url.startswith(("http://", "https://")):
            out.append({"name": name, "url": url})
    return out or DEFAULT_STATIONS


_log = logging.getLogger(__name__)

# ── «Сейчас играет»: чтение названия трека из ICY-метаданных потока ───────────
# Браузер не отдаёт метаданные радиопотока в JS, поэтому название трека
# добывает сервер: подключается к потоку с заголовком Icy-MetaData:1 и читает
# StreamTitle. Опрашиваем только активную станцию в фоне раз в ~20с.
_now_playing = {"idx": -1, "title": None, "at": 0.0}
_np_lock = threading.Lock()
_poller_started = False


class _StreamReader:
    """Минимальный буферизованный читатель поверх сокета."""
    def __init__(self, sock, initial=b""):
        self.sock = sock
        self.buf = initial

    def read(self, n):
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def _fetch_icy_title(url: str, timeout: float = 8.0) -> str | None:
    """Подключается к Icecast/Shoutcast-потоку и возвращает StreamTitle
    (обычно «Исполнитель - Трек») либо None, если метаданных нет/ошибка."""
    sock = None
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return None
        host = u.hostname
        port = u.port or (443 if u.scheme == "https" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        raw = socket.create_connection((host, port), timeout=timeout)
        sock = raw
        if u.scheme == "https":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(raw, server_hostname=host)
        sock.settimeout(timeout)

        req = (
            f"GET {path} HTTP/1.0\r\n"
            f"Host: {host}\r\n"
            "Icy-MetaData: 1\r\n"
            "User-Agent: NERPA-Board/1.0\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        sock.sendall(req.encode("ascii"))

        # Читаем заголовки ответа до пустой строки
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 16384:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            buf += chunk
        header_blob, _, rest = buf.partition(b"\r\n\r\n")
        headers = header_blob.decode("latin-1", "ignore").lower()

        metaint = None
        for line in headers.split("\r\n"):
            if line.startswith("icy-metaint:"):
                try:
                    metaint = int(line.split(":", 1)[1].strip())
                except ValueError:
                    metaint = None
        if not metaint or metaint <= 0:
            return None  # станция не отдаёт метаданные

        reader = _StreamReader(sock, rest)
        # Читаем несколько метаблоков, пока не встретим непустой StreamTitle
        for _ in range(6):
            reader.read(metaint)              # пропускаем аудио-данные
            lenb = reader.read(1)
            if not lenb:
                break
            meta_len = lenb[0] * 16
            if meta_len == 0:
                continue                       # в этом блоке метаданные не менялись
            meta = reader.read(meta_len)
            text = meta.decode("utf-8", "ignore")
            m = re.search(r"StreamTitle='(.*?)';", text)
            if m:
                title = m.group(1).strip()
                if title:
                    return title
        return None
    except (OSError, ValueError):
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _active_station_url():
    """Возвращает (idx, url) активной станции из настроек, либо (0, None)."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        company = db.query(CompanySettings).first()
        stations = parse_stations(company.board_stations if company else None)
        idx = (company.board_active_station or 0) if company else 0
        if idx >= len(stations):
            idx = 0
        url = stations[idx]["url"] if stations else None
        return idx, url
    finally:
        db.close()


def _refresh_now_playing():
    """Один цикл: узнать активную станцию и обновить кэш названия трека."""
    try:
        idx, url = _active_station_url()
        title = _fetch_icy_title(url) if url else None
        with _np_lock:
            _now_playing["idx"] = idx
            _now_playing["title"] = title
            _now_playing["at"] = time.time()
    except Exception as e:  # noqa: BLE001 — фоновый поток не должен падать
        _log.debug("now-playing refresh failed: %s", e)


def _now_playing_loop():
    while True:
        _refresh_now_playing()
        time.sleep(20)


def start_now_playing():
    """Запускает фоновый поллер «сейчас играет» (idempotent)."""
    global _poller_started
    if _poller_started:
        return
    _poller_started = True
    threading.Thread(target=_now_playing_loop, daemon=True,
                     name="now-playing").start()
    _log.info("Поллер «сейчас играет» запущен")


def _current_now_playing(active_idx: int) -> str | None:
    """Название трека, только если кэш относится к текущей активной станции."""
    with _np_lock:
        if _now_playing["idx"] == active_idx:
            return _now_playing["title"]
    return None


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

    # Дата выручки = дата ОПЛАТЫ счёта (когда пришли деньги). Для старых
    # оплаченных счетов без paid_date откатываемся на дату счёта.
    pay_date = func.coalesce(func.date(Invoice.paid_date), func.date(Invoice.date))

    def _revenue(*filters):
        # Выручка = сумма ОПЛАЧЕННЫХ счетов (Invoice.status == 'paid').
        # Период считается по дате оплаты (pay_date), а не по дате счёта,
        # иначе «выручка за день» не видит счета, оплаченные позже выставления.
        q = db.query(func.sum(Invoice.total_amount)).filter(Invoice.status == "paid")
        for f in filters:
            q = q.filter(f)
        return int(q.scalar() or 0)

    shipped_total = _nuts()
    shipped_month = _nuts(ship_date >= month_start)
    shipped_today = _nuts(ship_date == today)
    shipped_year  = _nuts(ship_date >= year_start)

    last_month_nuts    = _nuts(ship_date >= last_month_start, ship_date <= last_month_end)
    last_month_revenue = _revenue(pay_date >= last_month_start, pay_date <= last_month_end)

    revenue_month = _revenue(pay_date >= month_start)

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

    cost_pct         = float(company.board_cost_pct or 0.0)      if company else 0.0
    cost_norm_pct    = float(company.board_cost_norm_pct or 48.0) if company else 48.0
    cost_deviation   = float(company.board_cost_deviation or 5.0) if company else 5.0

    plan_pct     = round(shipped_month / plan * 100) if plan > 0 else 0
    revenue_pct  = round(revenue_month / revenue_plan * 100) if revenue_plan > 0 else 0

    # Выручка на всех карточках = сумма оплаченных счетов за период (по дате оплаты).
    # Орешки считаются отдельно — по дате сборки (см. _nuts выше).
    shipped_month_money = revenue_month  # = _revenue(pay_date >= month_start)
    shipped_today_money = _revenue(pay_date == today)
    shipped_year_money  = _revenue(pay_date >= year_start)
    shipped_total_money = _revenue()

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

    # Рекорд выручки за день в текущем месяце (оплаченные счета, по дате оплаты)
    best_money_row = (
        db.query(
            pay_date.label("day"),
            func.sum(Invoice.total_amount).label("total"),
        )
        .filter(
            Invoice.status == "paid",
            pay_date >= month_start,
        )
        .group_by(pay_date)
        .order_by(func.sum(Invoice.total_amount).desc())
        .first()
    )
    record_day_money = int(best_money_row.total or 0) if best_money_row else 0
    record_day_money_date = str(best_money_row.day) if best_money_row else None
    is_record_money_today = bool(
        best_money_row
        and record_day_money_date == str(today)
        and shipped_today_money > 0
    )

    # План на ближайшую отгрузку — цех видит, что печь, ещё до того как
    # менеджер что-то подтвердил (см. shop.production_plan).
    try:
        from app.routers.shop import production_plan
        production = production_plan(db, company)
    except Exception as e:                       # табло не должно падать из-за панели
        _log.warning("Табло: не удалось собрать план производства: %s", e)
        production = {"date": None}

    return {
        "production":          production,
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
        "record_day_money":       record_day_money,
        "record_day_money_date":  record_day_money_date,
        "is_record_money_today":  is_record_money_today,
        "last_month_nuts":     last_month_nuts,
        "last_month_revenue":  last_month_revenue,
        "last_month_name":     last_month_name,
        "cost_pct":            cost_pct,
        "cost_norm_pct":       cost_norm_pct,
        "cost_deviation":      cost_deviation,
        "quotes":              quotes,
        "stations":            stations,
        "active_station":      active,
        "now_playing":         _current_now_playing(active),
        "birthdays_today":     birthdays_today,
        "birthdays_upcoming":  birthdays_upcoming,
        "server_version":      SERVER_START,
    }


@router.get("/tv")
async def board_tv_redirect():
    """Короткий редирект для ввода на ТВ вместо длинного URL с ключом."""
    return RedirectResponse(url=f"/board?key={BOARD_KEY}", status_code=302)


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
    # Сбрасываем кэш трека и обновляем его в фоне, чтобы название сменилось быстро
    with _np_lock:
        _now_playing["idx"] = idx
        _now_playing["title"] = None
    threading.Thread(target=_refresh_now_playing, daemon=True).start()
    return JSONResponse({"active_station": idx})

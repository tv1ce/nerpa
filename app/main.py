import asyncio
import logging
import os
import time as _time
from contextlib import asynccontextmanager
from datetime import date as _date
from dotenv import load_dotenv
load_dotenv()  # загружаем .env до инициализации всего остального

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from app.routers import auth, dashboard, counterparties, products, orders, invoices, contracts, settings, reports, warehouse, receivables, notifications, claims, activity, audit_log, board, logistics, leads, recon
from app.database import init_db

logger = logging.getLogger(__name__)

# Миграции запускаются при каждом старте (в т.ч. при --reload)
init_db()

app = FastAPI(
    title="TMS — Управление поставками",
    lifespan=lifespan,
    # /docs и /redoc закрыты в production — схема API не должна быть публичной
    docs_url=None,
    redoc_url=None,
)

# Фиксируем момент старта для /health → uptime
_APP_START = _time.monotonic()


# ── Авто-перевод просроченных счетов в статус overdue ────────────────────────

def _mark_overdue_invoices() -> int:
    """Переводит счета issued→overdue если due_date < сегодня.
    Возвращает количество обновлённых записей."""
    from app.database import SessionLocal
    from app.models import Invoice
    from sqlalchemy import and_

    today = _date.today()
    db = SessionLocal()
    try:
        updated = (
            db.query(Invoice)
            .filter(
                Invoice.status == "issued",
                Invoice.due_date != None,
                Invoice.due_date < today,
            )
            .all()
        )
        for inv in updated:
            inv.status = "overdue"
        if updated:
            db.commit()
            logger.info("Авто-просрочка: %d счетов → overdue", len(updated))
        return len(updated)
    except Exception as e:
        logger.error("Ошибка авто-просрочки счетов: %s", e)
        db.rollback()
        return 0
    finally:
        db.close()


async def _overdue_loop():
    """Фоновая задача: проверяет просрочку каждый час."""
    while True:
        try:
            _mark_overdue_invoices()
        except Exception as e:
            logger.error("overdue_loop: %s", e)
        await asyncio.sleep(3600)  # раз в час


def _rotate_generated(max_age_days: int = 90) -> int:
    """Удаляет файлы из generated/ старше max_age_days дней.
    Возвращает количество удалённых файлов."""
    import glob as _glob
    from pathlib import Path

    cutoff = _time.time() - max_age_days * 86400
    deleted = 0
    for path in Path("generated").glob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        except Exception as e:
            logger.warning("Не удалось удалить %s: %s", path, e)
    if deleted:
        logger.info("Ротация generated/: удалено %d файлов старше %d дней", deleted, max_age_days)
    return deleted


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """FastAPI lifespan: заменяет устаревший @app.on_event('startup')."""
    # ── startup ──────────────────────────────────────────────────────────────
    _mark_overdue_invoices()          # перевести просроченные счета
    _rotate_generated(max_age_days=90)  # удалить старые docx
    asyncio.create_task(_overdue_loop())  # фоновый цикл каждый час
    yield
    # ── shutdown (ничего освобождать не нужно) ────────────────────────────────


# Сессия живёт 30 дней — чтобы мобильное приложение/браузер «помнили» пользователя
_session_secret = os.environ.get("SECRET_KEY")
if not _session_secret:
    import secrets
    _session_secret = secrets.token_hex(32)
    logger.warning(
        "SECRET_KEY не задан в .env — используется временный ключ. "
        "Все сессии сбросятся при перезапуске. Добавьте SECRET_KEY в .env"
    )

app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    max_age=60 * 60 * 24 * 30,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Папка с дистрибутивом Android-приложения (APK + version.json)
APP_DIST_DIR = "app_dist"


# ── PWA: манифест и service worker (нужны на корне, без авторизации) ──────────

@app.get("/manifest.webmanifest", include_in_schema=False)
async def pwa_manifest():
    return FileResponse(
        "app/static/manifest.webmanifest",
        media_type="application/manifest+json",
    )


@app.get("/sw.js", include_in_schema=False)
async def pwa_service_worker():
    # SW обязан отдаваться с корня, чтобы его scope покрывал весь сайт ('/')
    return FileResponse(
        "app/static/js/sw.js",
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )


# ── Автообновление Android-приложения ────────────────────────────────────────
# Приложение при запуске запрашивает /app/version.json и сравнивает versionCode
# с установленным. Если на сервере новее — скачивает APK с /app/download.

@app.get("/health", include_in_schema=False)
async def health():
    """
    Расширенный health-check.

    Возвращает HTTP 200 когда всё OK, HTTP 503 если БД недоступна.
    Используется nginx upstream_check, Docker HEALTHCHECK, мониторингом.

    Поля ответа:
      status   — "ok" | "degraded"
      db       — "ok" | "error"
      db_ms    — время ответа БД в мс
      uptime_s — секунд с момента запуска процесса
    """
    from sqlalchemy import text
    from app.database import SessionLocal

    uptime_s = int(_time.monotonic() - _APP_START)

    db_ok = False
    db_ms = 0.0
    try:
        t0 = _time.monotonic()
        _db = SessionLocal()
        _db.execute(text("SELECT 1"))
        _db.close()
        db_ok = True
        db_ms = round((_time.monotonic() - t0) * 1000, 1)
    except Exception:
        pass

    payload = {
        "status":   "ok" if db_ok else "degraded",
        "db":       "ok" if db_ok else "error",
        "db_ms":    db_ms,
        "uptime_s": uptime_s,
    }
    return JSONResponse(content=payload, status_code=200 if db_ok else 503)


@app.get("/app/version.json", include_in_schema=False)
async def app_version():
    path = os.path.join(APP_DIST_DIR, "version.json")
    if os.path.exists(path):
        return FileResponse(path, media_type="application/json",
                            headers={"Cache-Control": "no-cache"})
    # Нет опубликованной версии — обновлений нет
    return JSONResponse({"versionCode": 0, "versionName": "", "notes": ""})


@app.get("/app/download", include_in_schema=False)
async def app_download(request: Request):
    # APK только для авторизованных пользователей
    if not request.session.get("user_id"):
        from fastapi.responses import RedirectResponse as _R
        return _R(url="/auth/login", status_code=302)
    path = os.path.join(APP_DIST_DIR, "tms-sklad.apk")
    if os.path.exists(path):
        return FileResponse(
            path,
            media_type="application/vnd.android.package-archive",
            filename="tms-sklad.apk",
        )
    return JSONResponse({"error": "apk not found"}, status_code=404)

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(counterparties.router)
app.include_router(products.router)
app.include_router(orders.router)
app.include_router(invoices.router)
app.include_router(contracts.router)
app.include_router(settings.router)
app.include_router(reports.router)
app.include_router(warehouse.router)
app.include_router(receivables.router)
app.include_router(notifications.router)
app.include_router(claims.router)
app.include_router(activity.router)
app.include_router(audit_log.router)
app.include_router(board.router)
app.include_router(logistics.router)
app.include_router(leads.router)
app.include_router(recon.router)


# ── Jinja2 фильтры ───────────────────────────────────────────────────────────

def _fmt_money(value):
    if value is None:
        return "0,00"
    return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")


def _fmt_date(value):
    if not value:
        return "—"
    from datetime import date, datetime
    if isinstance(value, (date, datetime)):
        return value.strftime("%d.%m.%Y")
    return str(value)


# Регистрируем фильтры во всех шаблонах через Jinja2Templates
from datetime import date as _date
import secrets as _secrets

def _get_csrf_token(request) -> str:
    """Возвращает CSRF-токен из сессии, при необходимости создаёт новый."""
    token = request.session.get("csrf_token")
    if not token:
        token = _secrets.token_hex(32)
        request.session["csrf_token"] = token
    return token

_templates = Jinja2Templates(directory="app/templates")
_templates.env.filters["money"] = _fmt_money
_templates.env.filters["date_fmt"] = _fmt_date
_templates.env.filters["format_number"] = lambda v: f"{int(v):,}".replace(",", " ")
# today — прокси-объект, который всегда возвращает ТЕКУЩУЮ дату.
# Шаблоны используют его без скобок: {{ today }}, today <= date, today.isoformat() —
# всё работает как с обычным date-объектом, но значение свежее при каждом рендере.
class _TodayProxy:
    """Прокси вокруг date.today() — обновляется при каждом обращении к атрибуту."""
    def __getattr__(self, name):
        return getattr(_date.today(), name)
    def __str__(self):   return str(_date.today())
    def __repr__(self):  return repr(_date.today())
    def __format__(self, fmt): return format(_date.today(), fmt)
    def __eq__(self, other):   return _date.today() == other
    def __lt__(self, other):   return _date.today() <  other
    def __le__(self, other):   return _date.today() <= other
    def __gt__(self, other):   return _date.today() >  other
    def __ge__(self, other):   return _date.today() >= other
    def __hash__(self):        return hash(_date.today())

_templates.env.globals["today"] = _TodayProxy()
_templates.env.globals["csrf_token"] = _get_csrf_token


def _safe_url(v):
    """Безопасный href: рабочий URL или '#'. Не-URL текст (мусор в полях
    соцсетей/сайта) не превращается в относительную ссылку — иначе клик уводит
    на /recon/<текст> и ломает роут."""
    if not v:
        return "#"
    v = str(v).strip()
    if v.startswith(("http://", "https://", "mailto:", "tel:")):
        return v
    if "." in v and " " not in v and "@" not in v:
        return "https://" + v.lstrip("/")
    return "#"


_templates.env.filters["safe_url"] = _safe_url

# Патчим все роутеры, чтобы они использовали тот же env
import app.routers.auth as _r_auth
import app.routers.dashboard as _r_dash
import app.routers.counterparties as _r_cp
import app.routers.products as _r_prod
import app.routers.orders as _r_ord
import app.routers.invoices as _r_inv
import app.routers.contracts as _r_con
import app.routers.settings as _r_set
import app.routers.reports as _r_rep
import app.routers.warehouse as _r_wh
import app.routers.receivables as _r_rec
import app.routers.notifications as _r_notif
import app.routers.claims as _r_claims
import app.routers.activity as _r_act
import app.routers.audit_log as _r_audit
import app.routers.board as _r_board
import app.routers.logistics as _r_logistics
import app.routers.leads as _r_leads
import app.routers.recon as _r_recon

for _mod in [_r_auth, _r_dash, _r_cp, _r_prod, _r_ord, _r_inv, _r_con, _r_set, _r_rep, _r_wh, _r_rec, _r_notif, _r_claims, _r_act, _r_audit, _r_board, _r_logistics, _r_leads, _r_recon]:
    _mod.templates = _templates

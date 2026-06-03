import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from app.routers import auth, dashboard, counterparties, products, orders, invoices, contracts, settings, reports, warehouse, receivables, notifications, claims, activity, audit_log, board, logistics, leads, recon
from app.database import init_db

# Миграции запускаются при каждом старте (в т.ч. при --reload)
init_db()

app = FastAPI(title="TMS — Управление поставками")

# Сессия живёт 30 дней — чтобы мобильное приложение/браузер «помнили» пользователя
app.add_middleware(
    SessionMiddleware,
    secret_key="tms-secret-change-in-production-2024",
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

@app.get("/app/version.json", include_in_schema=False)
async def app_version():
    path = os.path.join(APP_DIST_DIR, "version.json")
    if os.path.exists(path):
        return FileResponse(path, media_type="application/json",
                            headers={"Cache-Control": "no-cache"})
    # Нет опубликованной версии — обновлений нет
    return JSONResponse({"versionCode": 0, "versionName": "", "notes": ""})


@app.get("/app/download", include_in_schema=False)
async def app_download():
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
_templates = Jinja2Templates(directory="app/templates")
_templates.env.filters["money"] = _fmt_money
_templates.env.filters["date_fmt"] = _fmt_date
_templates.env.filters["format_number"] = lambda v: f"{int(v):,}".replace(",", " ")
# Глобальная переменная today доступна в каждом шаблоне
_templates.env.globals["today"] = _date.today()


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

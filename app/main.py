from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from app.routers import auth, dashboard, counterparties, products, orders, invoices, contracts, settings, reports, warehouse, receivables, notifications, claims, activity, audit_log, board

app = FastAPI(title="TMS — Управление поставками")

app.add_middleware(SessionMiddleware, secret_key="tms-secret-change-in-production-2024", max_age=86400)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

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

for _mod in [_r_auth, _r_dash, _r_cp, _r_prod, _r_ord, _r_inv, _r_con, _r_set, _r_rep, _r_wh, _r_rec, _r_notif, _r_claims, _r_act, _r_audit, _r_board]:
    _mod.templates = _templates

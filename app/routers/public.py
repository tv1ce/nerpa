"""Публичный клиентский трекинг заказа по ссылке /track/{token}.

Открывается БЕЗ авторизации — клиент видит статус своего заказа, состав и
контакт менеджера. Внутренние данные (цены закупки, маржа, заметки) не светятся.
Токен длинный и неугадываемый; генерируется лениво в карточке заказа.
"""
import secrets
import time
from collections import defaultdict
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Order, CompanySettings, ORDER_FLOW_PREPAY, ORDER_FLOW_DEFERRED

router = APIRouter(tags=["public"])
templates = Jinja2Templates(directory="app/templates")

# Клиентские (дружелюбные) подписи статусов — без внутренней кухни.
PUBLIC_STATUS_LABELS = {
    "draft":     "Оформляется",
    "confirmed": "Подтверждён",
    "paid":      "Оплачен",
    "assembled": "Собран",
    "handed":    "Передан в доставку",
    "delivered": "Доставлен",
    "cancelled": "Отменён",
}

# ── Примитивный rate-limit по IP (в памяти процесса) ─────────────────────────
# Защищает от перебора токенов: не более N запросов за окно с одного IP.
_RATE_WINDOW = 60          # секунд
_RATE_MAX = 60             # запросов за окно
_hits: dict[str, list] = defaultdict(list)


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    bucket = _hits[ip]
    # выкидываем устаревшие отметки
    cutoff = now - _RATE_WINDOW
    bucket[:] = [t for t in bucket if t > cutoff]
    if len(bucket) >= _RATE_MAX:
        return True
    bucket.append(now)
    # лёгкая уборка, чтобы словарь не разрастался
    if len(_hits) > 2048:
        for k in [k for k, v in list(_hits.items()) if not v]:
            _hits.pop(k, None)
    return False


def ensure_public_token(db: Session, order: Order) -> str:
    """Возвращает публичный токен заказа, создавая его при первом обращении."""
    if not order.public_token:
        order.public_token = secrets.token_urlsafe(24)
        db.commit()
    return order.public_token


def _public_timeline(order: Order) -> list[dict]:
    """Шаги статусов для клиентского таймлайна (без 'draft' и 'cancelled')."""
    flow = ORDER_FLOW_PREPAY if order.is_prepay else ORDER_FLOW_DEFERRED
    steps = [s for s in flow if s != "draft"]
    # индекс текущего статуса в полном flow (для отметки пройденных)
    try:
        cur_idx = flow.index(order.status)
    except ValueError:
        cur_idx = -1
    result = []
    for s in steps:
        idx = flow.index(s)
        result.append({
            "code": s,
            "label": PUBLIC_STATUS_LABELS.get(s, s),
            "done": idx <= cur_idx and cur_idx >= 0,
            "current": s == order.status,
        })
    return result


@router.get("/track/{token}", response_class=HTMLResponse)
async def track_order(request: Request, token: str, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "?"
    if _rate_limited(ip):
        return HTMLResponse(
            "<h2 style='font-family:sans-serif;text-align:center;padding:3rem'>"
            "Слишком много запросов. Попробуйте через минуту.</h2>",
            status_code=429,
        )

    # Токен короткий/мусорный — не делаем запрос в БД
    order = None
    if token and len(token) >= 16:
        order = db.query(Order).filter(Order.public_token == token).first()

    company = db.query(CompanySettings).first()

    if not order or order.status == "cancelled":
        # Единый «не найдено» и для несуществующих, и для отменённых —
        # чтобы по статусу нельзя было что-то выведать.
        return templates.TemplateResponse(
            request, "public/track_notfound.html",
            {"company": company}, status_code=404,
        )

    manager = order.created_by
    cp = order.counterparty
    return templates.TemplateResponse(request, "public/track.html", {
        "order": order,
        "company": company,
        "manager": manager,
        "cp": cp,
        "timeline": _public_timeline(order),
        "status_label": PUBLIC_STATUS_LABELS.get(order.status, order.status),
    })

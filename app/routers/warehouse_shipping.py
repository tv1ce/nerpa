"""Раздел «Отгрузка заказов» кабинета кладовщика.

После сборки (Order.status == 'assembled') заказ попадает сюда. Кладовщик видит
дату отгрузки, контрагента (с названием заведения), номер заказа и количество
мест (Order.cargo_places), и нажатием «Отгружено» переводит заказ в статус
«Передан поставщику» (handed) — конечная точка ответственности склада.

Не используем общий /orders/{id}/status: та ручка разрешает роли warehouse
только переход в 'assembled' (см. orders.py:change_status), а до 'handed'
раньше можно было дойти только побочным эффектом создания движения 'out'
(warehouse.py:create_movement). Здесь — явная и понятная кнопка для той же
операции, с тем же переходом статуса.
"""
from datetime import datetime

from fastapi import APIRouter, Request, Depends, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import login_required
from app.models import Order, User
from app.utils import log_action
from app.services.telegram_send import notify_warehouse_group_bg

router = APIRouter(prefix="/warehouse/shipping", tags=["warehouse_shipping"])
templates = Jinja2Templates(directory="app/templates")


def _shipping_queue_count(db: Session) -> int:
    return db.query(Order).filter(Order.status == "assembled").count()


@router.get("/", response_class=HTMLResponse)
@login_required
async def shipping_list(request: Request, db: Session = Depends(get_db)):
    orders = (
        db.query(Order)
        .filter(Order.status == "assembled")
        .order_by(Order.dispatch_date.asc().nullslast(), Order.delivery_date.asc().nullslast())
        .all()
    )
    return templates.TemplateResponse(request, "warehouse/shipping.html", {
        "orders": orders,
        "shipping_queue_count": len(orders),
    })


@router.post("/{order_id}/ship")
@login_required
def mark_shipped(request: Request, order_id: int, background: BackgroundTasks,
                 db: Session = Depends(get_db)):
    # Обычный def — см. комментарий в warehouse.mark_assembled: запись в SQLite
    # не должна занимать event loop.
    order = db.query(Order).filter(Order.id == order_id).with_for_update().first()
    if order and order.status == "assembled":
        old_status = order.status
        order.status = "handed"
        if order.handed_at is None:
            order.handed_at = datetime.now()
        log_action(db, "order", order_id, "status_changed",
                   request.session.get("user_id"),
                   "Заказ отгружен кладовщиком",
                   field="status", old_value=old_status, new_value="handed")
        db.commit()

        user = db.query(User).filter(User.id == request.session.get("user_id")).first()
        cp = order.counterparty
        background.add_task(
            notify_warehouse_group_bg, "shipped",
            f"🚚 Заказ №{order.number} передан поставщику\n"
            f"Клиент: {(cp.trade_name or cp.name) if cp else '—'}\n"
            f"Мест: {order.cargo_places if order.cargo_places else len(order.items)}\n"
            f"Кладовщик: {user.full_name if user else '—'}"
        )
    return RedirectResponse(url="/warehouse/shipping/", status_code=302)

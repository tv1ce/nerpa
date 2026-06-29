import json
import os
import uuid as _uuid
from datetime import date, datetime
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
import httpx
from app.database import get_db
from app.auth import login_required, role_required
from app.models import Order, OrderItem, Counterparty, Product, CompanySettings, Task, Comment, AuditLog, User, Contract
from app.utils import log_action
import logging
import threading

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["orders"])


def _push_order_bg(order_id: int) -> None:
    """Push заказа в 1С в фоновом потоке."""
    from app.database import SessionLocal
    from app.services.onec_client import push_order
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        if order:
            push_order(order, db)
    except Exception as e:
        logger.error("push_order bg %s: %s", order_id, e)
    finally:
        db.close()
templates = Jinja2Templates(directory="app/templates")

ORDER_STATUSES = {
    "draft": "Черновик",
    "confirmed": "Подтверждён",
    "paid": "Оплачен",
    "assembled": "Собран",
    "handed": "Передан поставщику",
    "delivered": "Доставлено",
    "cancelled": "Отменён",
}

PAYMENT_TYPES = {
    "prepay": "Предоплата",
    "deferred": "Отсрочка платежа",
}


def _statuses_for(order: Order) -> dict:
    """Статусы, применимые к конкретному заказу (зависят от типа оплаты),
    плюс «Отменён». Для предоплаты доступен шаг «Оплачен», для отсрочки — нет."""
    allowed = list(order.workflow) + ["cancelled"]
    return {k: v for k, v in ORDER_STATUSES.items() if k in allowed}


def _next_order_number(db: Session) -> str:
    """Следующий номер заказа — max по числовой части поля number."""
    rows = db.query(Order.number).all()
    nums = []
    for (n,) in rows:
        try:
            nums.append(int(str(n).split("/")[0].strip()))
        except (ValueError, TypeError):
            pass
    return str((max(nums) + 1) if nums else 1)


def _assembly_queue_count(db: Session) -> int:
    """Кол-во заказов на сборку — для бейджа в мобильном таббаре.
    ready_for_assembly = (prepay AND paid) OR (deferred AND confirmed).
    Считаем прямо в SQL без загрузки объектов в память."""
    from sqlalchemy import case, and_
    count = db.query(func.count(Order.id)).filter(
        Order.status.in_(["confirmed", "paid"]),
        # предоплата → ждём статуса paid; отсрочка → ждём confirmed
        case(
            (and_(Order.payment_type == "prepay",    Order.status == "paid"),      1),
            (and_(Order.payment_type == "deferred",  Order.status == "confirmed"), 1),
            else_=0,
        ) == 1,
    ).scalar() or 0
    return count


def _resolve_payment_type(db: Session, contract_id: int, fallback: str) -> str:
    """Тип оплаты заказа определяется выбранным договором; если договор
    не выбран — берётся значение из формы. Допустимые: prepay / deferred."""
    if contract_id:
        contract = db.query(Contract).filter(Contract.id == contract_id).first()
        if contract and contract.payment_type:
            return contract.payment_type
    return fallback if fallback in ("prepay", "deferred") else "prepay"


@router.get("/", response_class=HTMLResponse)
@login_required
async def list_orders(
    request: Request,
    q: str = "",
    status: str = "",
    date_from: str = "",
    date_to: str = "",
    counterparty_id: int = 0,
    carrier_id: int = 0,
    payment_type: str = "",
    overdue: str = "",
    db: Session = Depends(get_db),
):
    today = date.today()
    query = db.query(Order).join(Counterparty, Order.counterparty_id == Counterparty.id)
    if q:
        query = query.filter(Order.number.ilike(f"%{q}%") | Counterparty.name.ilike(f"%{q}%"))
    if status:
        query = query.filter(Order.status == status)
    if date_from:
        try:
            query = query.filter(Order.date >= date.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(Order.date <= date.fromisoformat(date_to))
        except ValueError:
            pass
    if counterparty_id:
        query = query.filter(Order.counterparty_id == counterparty_id)
    if carrier_id:
        query = query.filter(Order.carrier_id == carrier_id)
    if payment_type:
        query = query.filter(Order.payment_type == payment_type)
    if overdue:
        query = query.filter(
            Order.delivery_date < today,
            Order.status.notin_(["delivered", "cancelled"]),
            Order.delivery_date.isnot(None),
        )
    orders = query.order_by(Order.date.desc(), Order.id.desc()).all()
    counterparties = db.query(Counterparty).filter(
        Counterparty.is_active == True, Counterparty.type.in_(["client", "both"])
    ).order_by(Counterparty.name).all()
    carriers = db.query(Counterparty).filter(
        Counterparty.is_active == True, Counterparty.type == "carrier"
    ).order_by(Counterparty.name).all()
    return templates.TemplateResponse(request, "orders/list.html", {
        "orders": orders, "q": q, "status": status, "statuses": ORDER_STATUSES,
        "date_from": date_from, "date_to": date_to,
        "counterparty_id": counterparty_id, "carrier_id": carrier_id,
        "payment_type": payment_type, "overdue": overdue,
        "counterparties": counterparties, "carriers": carriers,
        "payment_types": PAYMENT_TYPES, "today": today,
        "assembly_queue_count": _assembly_queue_count(db),
    })


@router.get("/new", response_class=HTMLResponse)
@login_required
async def new_order(request: Request, db: Session = Depends(get_db)):
    counterparties = db.query(Counterparty).filter(
        Counterparty.is_active == True, Counterparty.type.in_(["client", "both"])
    ).order_by(Counterparty.name).all()
    suppliers = db.query(Counterparty).filter(
        Counterparty.is_active == True, Counterparty.type.in_(["supplier", "both"])
    ).order_by(Counterparty.name).all()
    carriers = db.query(Counterparty).filter(
        Counterparty.is_active == True, Counterparty.type == "carrier"
    ).order_by(Counterparty.name).all()
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()
    contracts = db.query(Contract).order_by(Contract.date.desc()).all()
    company = db.query(CompanySettings).first()
    managers = db.query(User).filter(User.is_active == True).order_by(User.full_name).all()
    return templates.TemplateResponse(request, "orders/form.html", {
        "order": None, "counterparties": counterparties, "suppliers": suppliers,
        "carriers": carriers, "products": products, "contracts": contracts,
        "statuses": ORDER_STATUSES, "payment_types": PAYMENT_TYPES,
        "suggested_number": _next_order_number(db),
        "company": company, "managers": managers,
        "current_user_id": request.session.get("user_id"),
    })


@router.post("/new")
@role_required("manager")
async def create_order(
    request: Request,
    number: str = Form(...),
    order_date: str = Form(...),
    counterparty_id: int = Form(...),
    supplier_id: int = Form(default=0),
    carrier_id: int = Form(default=0),
    contract_id: int = Form(default=0),
    payment_type: str = Form(default="prepay"),
    status: str = Form(default="draft"),
    delivery_date: str = Form(default=""),
    delivery_address: str = Form(default=""),
    notes: str = Form(default=""),
    pickup_city: str = Form(default=""),
    pickup_address: str = Form(default=""),
    delivery_contact: str = Form(default=""),
    delivery_time: str = Form(default=""),
    delivery_cost: float = Form(default=0),
    sales_manager_id: int = Form(default=0),
    items_json: str = Form(default="[]"),
    db: Session = Depends(get_db),
):
    payment_type = _resolve_payment_type(db, contract_id, payment_type)
    if status not in ORDER_STATUSES:
        status = "draft"
    order = Order(
        number=f"~{_uuid.uuid4().hex[:12]}",  # уникальный temp-номер до получения ID
        date=date.fromisoformat(order_date),
        counterparty_id=counterparty_id,
        supplier_id=supplier_id or None,
        carrier_id=carrier_id or None,
        contract_id=contract_id or None,
        payment_type=payment_type,
        status=status,
        delivery_date=date.fromisoformat(delivery_date) if delivery_date else None,
        delivery_address=delivery_address,
        notes=notes,
        pickup_city=pickup_city or None,
        pickup_address=pickup_address or None,
        delivery_contact=delivery_contact or None,
        delivery_time=delivery_time or None,
        created_by_id=request.session.get("user_id"),
        sales_manager_id=sales_manager_id or request.session.get("user_id"),
    )
    db.add(order)
    db.flush()  # получаем order.id, гарантированно уникальный

    # Разрешаем финальный номер: предпочитаем пользовательский, fallback — ID
    desired_number = (number or "").strip() or str(order.id)
    conflict = db.query(Order.id).filter(
        Order.number == desired_number, Order.id != order.id
    ).scalar()
    order.number = desired_number if not conflict else str(order.id)

    try:
        items_data = json.loads(items_json)
    except (ValueError, TypeError):
        items_data = []
    # Фильтруем позиции без выбранного товара (защита от невалидных данных)
    # Проверяем что product_id есть и валиден (не пустой и не 0)
    items_data = [i for i in items_data if i.get("product_id") and int(i.get("product_id", 0)) > 0]
    for item in items_data:
        qty = float(item["quantity"])
        price = float(item["price"])
        disc = min(max(float(item.get("discount_pct", 0)), 0), 100)
        db.add(OrderItem(
            order_id=order.id,
            product_id=int(item["product_id"]),
            quantity=qty,
            price=price,
            discount_pct=disc,
            vat_rate=float(item.get("vat_rate", 20)),
            amount=round(qty * price * (1 - disc / 100), 2),
        ))
    from app.routers.logistics import upsert_order_delivery_cost
    upsert_order_delivery_cost(db, order, delivery_cost)
    db.commit()
    log_action(db, "order", order.id, "created",
               request.session.get("user_id"), f"Заказ {order.number} создан")
    db.commit()
    if status == "confirmed":
        threading.Thread(target=_push_order_bg, args=(order.id,), daemon=True).start()
    return RedirectResponse(url=f"/orders/{order.id}", status_code=302)


@router.get("/{order_id}", response_class=HTMLResponse)
@login_required
async def view_order(request: Request, order_id: int, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return RedirectResponse(url="/orders", status_code=302)
    tasks = db.query(Task).filter(
        Task.entity_type == "order", Task.entity_id == order_id
    ).order_by(Task.status, Task.created_at).all()
    comments = db.query(Comment).filter(
        Comment.entity_type == "order", Comment.entity_id == order_id
    ).order_by(Comment.created_at).all()
    activity = db.query(AuditLog).filter(
        AuditLog.entity_type == "order", AuditLog.entity_id == order_id
    ).order_by(AuditLog.created_at.desc()).limit(50).all()
    users = db.query(User).filter(User.is_active == True).order_by(User.full_name).all()
    from app.routers.files import files_for, FILE_TYPES
    files = files_for(db, "order", order_id)
    # Публичная ссылка для клиента (токен создаётся лениво при первом открытии карточки)
    from app.routers.public import ensure_public_token
    token = ensure_public_token(db, order)
    track_url = str(request.base_url).rstrip("/") + f"/track/{token}"

    # Мини-статистика клиента
    cp_all_orders = [o for o in order.counterparty.orders if o.status != "cancelled"]
    cp_revenue = sum(o.total_amount for o in cp_all_orders if o.status in ("paid", "assembled", "handed", "delivered"))
    cp_last_date = max((o.date for o in cp_all_orders if o.date), default=None)
    cp_avg_check = round(cp_revenue / len(cp_all_orders), 2) if cp_all_orders and cp_revenue else 0

    return templates.TemplateResponse(request, "orders/detail.html", {
        "order": order, "statuses": ORDER_STATUSES,
        "order_statuses": _statuses_for(order), "payment_types": PAYMENT_TYPES,
        "tasks": tasks, "comments": comments, "activity": activity, "users": users,
        "files": files, "file_types": FILE_TYPES["order"],
        "track_url": track_url,
        "priority_colors": {"low": "secondary", "normal": "primary", "high": "warning", "urgent": "danger"},
        "assembly_queue_count": _assembly_queue_count(db),
        "cp_orders_count": len(cp_all_orders),
        "cp_revenue": cp_revenue,
        "cp_last_date": cp_last_date,
        "cp_avg_check": cp_avg_check,
    })


@router.get("/{order_id}/edit", response_class=HTMLResponse)
@login_required
async def edit_order(request: Request, order_id: int, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return RedirectResponse(url="/orders", status_code=302)
    counterparties = db.query(Counterparty).filter(Counterparty.is_active == True).order_by(Counterparty.name).all()
    suppliers = db.query(Counterparty).filter(
        Counterparty.is_active == True, Counterparty.type.in_(["supplier", "both"])
    ).order_by(Counterparty.name).all()
    carriers = db.query(Counterparty).filter(
        Counterparty.is_active == True, Counterparty.type == "carrier"
    ).order_by(Counterparty.name).all()
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()
    contracts = db.query(Contract).order_by(Contract.date.desc()).all()
    company = db.query(CompanySettings).first()
    managers = db.query(User).filter(User.is_active == True).order_by(User.full_name).all()
    return templates.TemplateResponse(request, "orders/form.html", {
        "order": order, "counterparties": counterparties, "suppliers": suppliers,
        "carriers": carriers, "products": products, "contracts": contracts,
        "statuses": ORDER_STATUSES, "payment_types": PAYMENT_TYPES,
        "suggested_number": order.number,
        "company": company, "managers": managers,
        "current_user_id": request.session.get("user_id"),
    })


@router.post("/{order_id}/edit")
@role_required("manager")
async def update_order(
    request: Request, order_id: int,
    number: str = Form(...),
    order_date: str = Form(...),
    counterparty_id: int = Form(...),
    supplier_id: int = Form(default=0),
    carrier_id: int = Form(default=0),
    contract_id: int = Form(default=0),
    payment_type: str = Form(default="prepay"),
    status: str = Form(default="draft"),
    delivery_date: str = Form(default=""),
    delivery_address: str = Form(default=""),
    notes: str = Form(default=""),
    pickup_city: str = Form(default=""),
    pickup_address: str = Form(default=""),
    delivery_contact: str = Form(default=""),
    delivery_time: str = Form(default=""),
    delivery_cost: float = Form(default=0),
    sales_manager_id: int = Form(default=0),
    items_json: str = Form(default="[]"),
    db: Session = Depends(get_db),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return RedirectResponse(url="/orders", status_code=302)
    order.number = number
    order.date = date.fromisoformat(order_date)
    order.counterparty_id = counterparty_id
    order.supplier_id = supplier_id or None
    order.carrier_id = carrier_id or None
    order.contract_id = contract_id or None
    order.payment_type = _resolve_payment_type(db, contract_id, payment_type)
    if status in ORDER_STATUSES:
        order.status = status
    order.delivery_date = date.fromisoformat(delivery_date) if delivery_date else None
    order.delivery_address = delivery_address
    order.notes = notes
    order.pickup_city = pickup_city or None
    order.pickup_address = pickup_address or None
    order.delivery_contact = delivery_contact or None
    order.delivery_time = delivery_time or None
    if sales_manager_id:
        order.sales_manager_id = sales_manager_id
    for item in order.items:
        db.delete(item)
    db.flush()
    try:
        items_data = json.loads(items_json)
    except (ValueError, TypeError):
        items_data = []
    # Фильтруем позиции без выбранного товара (защита от невалидных данных)
    # Проверяем что product_id есть и валиден (не пустой и не 0)
    items_data = [i for i in items_data if i.get("product_id") and int(i.get("product_id", 0)) > 0]
    for item in items_data:
        qty = float(item["quantity"])
        price = float(item["price"])
        disc = min(max(float(item.get("discount_pct", 0)), 0), 100)
        db.add(OrderItem(
            order_id=order.id,
            product_id=int(item["product_id"]),
            quantity=qty,
            price=price,
            discount_pct=disc,
            vat_rate=float(item.get("vat_rate", 20)),
            amount=round(qty * price * (1 - disc / 100), 2),
        ))
    from app.routers.logistics import upsert_order_delivery_cost
    upsert_order_delivery_cost(db, order, delivery_cost)
    db.commit()
    log_action(db, "order", order_id, "updated",
               request.session.get("user_id"), "Заказ отредактирован")
    db.commit()
    return RedirectResponse(url=f"/orders/{order_id}", status_code=302)


@router.post("/{order_id}/status")
@login_required
async def change_status(request: Request, order_id: int,
                        status: str = Form(...),
                        redirect_url: str = Form(default=""),
                        db: Session = Depends(get_db)):
    from app.auth import ROLE_LEVELS
    role = request.session.get("user_role", "viewer")
    # with_for_update блокирует строку до commit — защита от гонки
    # одновременных смен статуса (на SQLite сводится к сериализации записи)
    order = db.query(Order).filter(Order.id == order_id).with_for_update().first()

    # Кладовщик может переводить заказ в «Собран» только когда он готов к сборке
    if role == "warehouse":
        allowed = (status == "assembled" and order is not None and order.ready_for_assembly)
    else:
        allowed = (ROLE_LEVELS.get(role, 0) >= ROLE_LEVELS.get("manager", 0)
                   and order is not None and status in _statuses_for(order))

    if allowed:
        old_status = order.status
        order.status = status
        # Фиксируем момент сборки при первом переходе в «Собран» — табло цеха
        # считает заказ отгруженным именно с этого времени.
        if status == "assembled" and order.assembled_at is None:
            order.assembled_at = datetime.now()
        log_action(db, "order", order_id, "status_changed",
                   request.session.get("user_id"),
                   f"Статус: {ORDER_STATUSES.get(old_status, old_status)} → {ORDER_STATUSES.get(status, status)}",
                   field="status", old_value=old_status, new_value=status)
        db.commit()
        if status == "confirmed":
            threading.Thread(target=_push_order_bg, args=(order_id,), daemon=True).start()

    target = redirect_url if redirect_url else f"/orders/{order_id}"
    return RedirectResponse(url=target, status_code=302)


@router.get("/{order_id}/tn", response_class=HTMLResponse)
@login_required
async def tn_form(request: Request, order_id: int, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return RedirectResponse(url="/orders", status_code=302)
    company = db.query(CompanySettings).first()
    # Автоподстановка данных перевозчика из заказа
    carrier = order.carrier if order.carrier_id else None
    return templates.TemplateResponse(request, "orders/tn_form.html", {
        "order": order, "company": company, "carrier": carrier,
    })


@router.post("/{order_id}/tn")
@login_required
async def generate_tn(
    request: Request, order_id: int,
    carrier_name:     str = Form(default=""),
    carrier_inn:      str = Form(default=""),
    driver_name:      str = Form(default=""),
    vehicle_type:     str = Form(default=""),
    vehicle_plate:    str = Form(default=""),
    pickup_address:   str = Form(default=""),
    pickup_date:      str = Form(default=""),
    cargo_name:       str = Form(default=""),
    cargo_places:     str = Form(default=""),
    cargo_weight:     str = Form(default=""),
    cargo_volume:     str = Form(default=""),
    cargo_value:      str = Form(default=""),
    docs:             str = Form(default=""),
    delivery_address: str = Form(default=""),
    delivery_date:    str = Form(default=""),
    shipping_cost:    str = Form(default=""),
    tn_number:        str = Form(default=""),
    db: Session = Depends(get_db),
):
    from datetime import date as _date
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return RedirectResponse(url="/orders", status_code=302)
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings(name="Моя компания")

    def _parse_date(s):
        try:
            return _date.fromisoformat(s) if s else None
        except Exception:
            return None

    from app.utils.pdf_tn import TnData, generate_tn_pdf
    tn = TnData(
        order=order, company=company,
        carrier_name=carrier_name, carrier_inn=carrier_inn,
        driver_name=driver_name, vehicle_type=vehicle_type, vehicle_plate=vehicle_plate,
        pickup_address=pickup_address or None,
        pickup_date=_parse_date(pickup_date) or order.date,
        cargo_name=cargo_name, cargo_places=cargo_places,
        cargo_weight=cargo_weight, cargo_volume=cargo_volume, cargo_value=cargo_value,
        docs=docs,
        delivery_address=delivery_address or None,
        delivery_date=_parse_date(delivery_date) or order.delivery_date,
        shipping_cost=shipping_cost,
        tn_number=tn_number or str(order_id),
    )
    from urllib.parse import quote
    pdf_bytes = generate_tn_pdf(tn)
    fn_ascii  = f"tn_{order_id}.pdf"
    tn_date_s = tn.pickup_date.strftime("%d.%m.%Y") if tn.pickup_date else ""
    fn_utf8   = f"Транспортная накладная № {tn.tn_number} от {tn_date_s}.pdf"
    cd = f"attachment; filename=\"{fn_ascii}\"; filename*=UTF-8''{quote(fn_utf8)}"
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": cd})


@router.post("/{order_id}/notify-carrier")
@role_required("manager")
async def notify_carrier(request: Request, order_id: int, db: Session = Depends(get_db)):
    """Отправить заказ перевозчику в Telegram."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return JSONResponse({"ok": False, "error": "Заказ не найден"}, status_code=404)

    carrier = order.carrier
    if not carrier:
        return JSONResponse({"ok": False, "error": "Перевозчик не указан в заказе"}, status_code=400)
    if not carrier.tg_notify_enabled:
        return JSONResponse({"ok": False, "error": "У перевозчика отключены Telegram-уведомления"}, status_code=400)
    if not carrier.tg_chat_id:
        return JSONResponse({"ok": False, "error": "У перевозчика не указан Telegram chat ID"}, status_code=400)

    # Токен: сначала из настроек компании, затем из .env
    from app.models import CompanySettings
    company = db.query(CompanySettings).first()
    bot_token = (company.tg_bot_token or "").strip() if company else ""
    if not bot_token:
        bot_token = os.getenv("TMS_BOT_TOKEN", "").strip()
    if not bot_token:
        return JSONResponse({"ok": False, "error": "Токен Telegram-бота не настроен. Укажите его в Настройки → Telegram-бот"}, status_code=500)

    # Собираем текст сообщения
    cp = order.counterparty
    cp_name = (cp.trade_name or cp.name) if cp else None

    lines = []

    # Дата
    lines.append("Дата")
    lines.append("")
    lines.append(order.delivery_date.strftime("%d.%m.%Y") if order.delivery_date else "—")

    # Адрес доставки + название заведения
    lines.append("")
    if order.delivery_address:
        lines.append(order.delivery_address)
    if cp_name:
        lines.append(cp_name)

    # Телефон / контактное лицо
    if order.delivery_contact:
        lines += ["", "Телефон", "", order.delivery_contact]

    # Время
    if order.delivery_time:
        lines += ["", "Время", "", order.delivery_time]

    text = "\n".join(lines)

    proxy_url = os.getenv("TMS_PROXY", "socks5://127.0.0.1:1080") or None
    try:
        async with httpx.AsyncClient(timeout=10.0, proxy=proxy_url) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": carrier.tg_chat_id, "text": text},
            )
        data = resp.json()
        if not data.get("ok"):
            return JSONResponse({"ok": False, "error": data.get("description", "Ошибка Telegram")}, status_code=502)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)

    log_action(db, "order", order_id, "notified_carrier",
               request.session.get("user_id"),
               f"Заказ отправлен перевозчику {carrier.trade_name or carrier.name} в Telegram")
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/{order_id}/duplicate")
@role_required("manager")
async def duplicate_order(request: Request, order_id: int, db: Session = Depends(get_db)):
    src = db.query(Order).filter(Order.id == order_id).first()
    if not src:
        return RedirectResponse(url="/orders", status_code=302)
    new_order = Order(
        number=f"~{_uuid.uuid4().hex[:12]}",
        date=date.today(),
        counterparty_id=src.counterparty_id,
        supplier_id=src.supplier_id,
        carrier_id=src.carrier_id,
        contract_id=src.contract_id,
        payment_type=src.payment_type,
        status="draft",
        delivery_address=src.delivery_address,
        notes=src.notes,
        pickup_city=src.pickup_city,
        pickup_address=src.pickup_address,
        delivery_contact=src.delivery_contact,
        delivery_time=src.delivery_time,
        created_by_id=request.session.get("user_id"),
    )
    db.add(new_order)
    db.flush()
    new_order.number = _next_order_number(db)
    for item in src.items:
        db.add(OrderItem(
            order_id=new_order.id,
            product_id=item.product_id,
            quantity=item.quantity,
            price=item.price,
            discount_pct=item.discount_pct,
            vat_rate=item.vat_rate,
            amount=item.amount,
        ))
    db.commit()
    log_action(db, "order", new_order.id, "created",
               request.session.get("user_id"),
               f"Заказ {new_order.number} создан как копия #{src.number}")
    db.commit()
    return RedirectResponse(url=f"/orders/{new_order.id}/edit", status_code=302)


@router.post("/{order_id}/delete")
@role_required("admin")
async def delete_order(request: Request, order_id: int, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if order:
        db.delete(order)
        db.commit()
    return RedirectResponse(url="/orders", status_code=302)

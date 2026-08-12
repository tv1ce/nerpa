"""Клиентский кабинет заказа — /shop/{token}, без авторизации.

Менеджер один раз отправляет клиенту магическую ссылку в мессенджер, дальше
товаровед точки собирает заказ сам с телефона. Пароля нет намеренно: в B2B
любой логин означает, что ссылкой не будут пользоваться.

Заказ приземляется в TMS ЧЕРНОВИКОМ (status='draft', source='client_portal') —
подтверждает его менеджер в TMS, он же и запускает штатную цепочку «подтверждён
→ 1С → сборка». Параллельно в Bitrix24 создаётся сделка на компанию клиента, в
то же направление и стадию, куда падают сделки менеджеров.

Безопасность: единственный секрет — токен, поэтому он длинный, страница отдаётся
с noindex, а на IP висит тот же примитивный rate-limit, что и на /track.
"""
import logging
import secrets
import threading
from datetime import date, timedelta

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (Counterparty, CompanySettings, Product, Order, OrderItem,
                        Notification)
from app.routers.public import _rate_limited, ensure_public_token
from app.utils import log_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shop", tags=["shop"])
templates = Jinja2Templates(directory="app/templates")

# Сколько последних заказов клиента считаем «моими товарами» (частотный топ).
_HISTORY_ORDERS = 20
# Максимум позиций в одном заказе — защита от кривого/злого запроса.
_MAX_ITEMS = 200


def ensure_shop_token(db: Session, cp: Counterparty) -> str:
    """Возвращает токен кабинета контрагента, создавая его при первом обращении."""
    if not cp.shop_token:
        cp.shop_token = secrets.token_urlsafe(24)
        db.commit()
    return cp.shop_token


def _find_counterparty(db: Session, token: str) -> Counterparty | None:
    if not token or len(token) < 16:
        return None
    cp = db.query(Counterparty).filter(Counterparty.shop_token == token).first()
    if not cp or not cp.shop_enabled or not cp.is_active:
        return None
    return cp


def _stock_label(qty: float, min_stock: float) -> str:
    """Светофор остатка. Точные цифры склада клиенту не показываем намеренно:
    это внутренняя информация, а «осталось 3 шт» ещё и провоцирует панику."""
    if qty <= 0:
        return "order"      # под заказ
    if qty <= max(min_stock or 0, 1):
        return "low"        # мало
    return "in"             # есть


def _catalog(db: Session, cp: Counterparty) -> tuple[list, list, list]:
    """(каталог, «мои товары» по частоте, позиции последнего заказа)."""
    from app.services.onec_client import get_1c_balances

    balances = get_1c_balances(db)
    discount = cp.default_discount_pct or 0.0

    products = (db.query(Product)
                .filter(Product.is_active == True)  # noqa: E712
                .order_by(Product.category, Product.name)
                .all())

    catalog = []
    for p in products:
        # Товар без цены клиенту показывать нечего — он не сможет понять, во
        # сколько ему обойдётся заказ, а «цена по запросу» ломает всю идею.
        if not p.price:
            continue
        price = round(p.price * (1 - discount / 100), 2)
        catalog.append({
            "id": p.id,
            "name": p.name,
            "article": p.article or "",
            "category": p.category or "Прочее",
            "unit": p.sale_unit or p.unit or "шт",
            "per_box": p.units_per_box or 1,
            "price": price,
            "base_price": round(p.price, 2),
            "stock": _stock_label(balances.get(p.id, 0.0), p.min_stock),
        })

    known = {c["id"] for c in catalog}

    # ── «Мои товары»: что этот клиент реально берёт, по частоте заказов ──────
    hist_orders = (db.query(Order.id)
                   .filter(Order.counterparty_id == cp.id,
                           Order.status != "cancelled")
                   .order_by(Order.date.desc(), Order.id.desc())
                   .limit(_HISTORY_ORDERS)
                   .all())
    hist_ids = [o.id for o in hist_orders]

    freq: dict[int, int] = {}
    if hist_ids:
        for (pid,) in db.query(OrderItem.product_id).filter(
                OrderItem.order_id.in_(hist_ids)).all():
            if pid in known:
                freq[pid] = freq.get(pid, 0) + 1
    mine = [pid for pid, _ in sorted(freq.items(), key=lambda kv: -kv[1])]

    # ── Последний заказ целиком — для кнопки «Повторить» ────────────────────
    last = []
    if hist_ids:
        for item in db.query(OrderItem).filter(OrderItem.order_id == hist_ids[0]).all():
            if item.product_id in known:
                last.append({"id": item.product_id, "qty": item.quantity})

    return catalog, mine, last


def _delivery_slots() -> list[dict]:
    """3 ближайших рабочих дня — вместо свободного календаря, куда клиент
    обязательно вобьёт воскресенье или вчерашнее число."""
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    slots, d = [], date.today()
    while len(slots) < 3:
        d += timedelta(days=1)
        if d.weekday() >= 5:      # выходные машины не ходят
            continue
        if len(slots) == 0:
            label = "Завтра" if (d - date.today()).days == 1 else f"{d.day:02d}.{d.month:02d}"
        else:
            label = f"{days[d.weekday()][:2]}, {d.day:02d}.{d.month:02d}"
        slots.append({"value": d.isoformat(), "label": label})
    return slots


@router.get("/{token}", response_class=HTMLResponse)
async def shop_page(request: Request, token: str, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "?"
    if _rate_limited(ip):
        return HTMLResponse("Слишком много запросов, попробуйте через минуту", status_code=429)

    cp = _find_counterparty(db, token)
    if not cp:
        return templates.TemplateResponse(request, "public/shop_notfound.html",
                                          {}, status_code=404)

    catalog, mine, last = _catalog(db, cp)
    company = db.query(CompanySettings).first()
    return templates.TemplateResponse(request, "public/shop.html", {
        "cp": cp,
        "company": company,
        "token": token,
        "catalog": catalog,
        "mine_ids": mine,
        "last_items": last,
        "slots": _delivery_slots(),
        "discount": cp.default_discount_pct or 0.0,
        "default_address": cp.actual_address or cp.legal_address or "",
    })


def _push_bitrix_bg(order_id: int) -> None:
    """Создаёт сделку в Bitrix24 в фоновом потоке — клиент не должен ждать CRM."""
    from app.database import SessionLocal
    from app.services.bitrix_client import push_shop_order_to_bitrix
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        company = db.query(CompanySettings).first()
        if order:
            push_shop_order_to_bitrix(order, company, db)
    except Exception as e:
        logger.error("push_shop_order_to_bitrix bg %s: %s", order_id, e)
    finally:
        db.close()


@router.post("/{token}/submit")
async def submit_order(request: Request, token: str, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "?"
    if _rate_limited(ip):
        return JSONResponse({"ok": False, "error": "Слишком много запросов"}, status_code=429)

    cp = _find_counterparty(db, token)
    if not cp:
        return JSONResponse({"ok": False, "error": "Ссылка недействительна"}, status_code=404)

    payload = await request.json()
    raw_items = payload.get("items") or []
    if not raw_items or len(raw_items) > _MAX_ITEMS:
        return JSONResponse({"ok": False, "error": "Корзина пуста"}, status_code=400)

    # Цены берём из БД, а не из тела запроса: всё, что пришло с клиента, кроме
    # id товара и количества, доверия не заслуживает.
    discount = cp.default_discount_pct or 0.0
    wanted = {}
    for row in raw_items:
        try:
            pid, qty = int(row.get("id")), float(row.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty > 0:
            wanted[pid] = wanted.get(pid, 0.0) + qty
    if not wanted:
        return JSONResponse({"ok": False, "error": "Корзина пуста"}, status_code=400)

    products = {p.id: p for p in db.query(Product).filter(
        Product.id.in_(wanted.keys()), Product.is_active == True).all()}  # noqa: E712
    if not products:
        return JSONResponse({"ok": False, "error": "Товары не найдены"}, status_code=400)

    delivery_date = None
    raw_date = (payload.get("delivery_date") or "").strip()
    if raw_date:
        try:
            parsed = date.fromisoformat(raw_date)
            if parsed >= date.today():
                delivery_date = parsed
        except ValueError:
            pass

    comment = (payload.get("comment") or "").strip()[:1000]
    address = (payload.get("address") or "").strip()[:500]
    contact = (payload.get("contact") or "").strip()[:200]

    from app.routers.orders import _next_order_number
    order = Order(
        number=_next_order_number(db),
        date=date.today(),
        counterparty_id=cp.id,
        status="draft",
        source="client_portal",
        # Свой менеджер клиента ведёт заказ и становится ответственным по
        # сделке в Bitrix24 — иначе заказ уйдёт на общего ответственного.
        sales_manager_id=cp.manager_id,
        payment_type="prepay",
        delivery_date=delivery_date,
        delivery_address=address or cp.actual_address or cp.legal_address,
        delivery_contact=contact or cp.contact_person or cp.phone,
        notes=comment or None,
    )
    db.add(order)
    db.flush()

    total = 0.0
    for pid, qty in wanted.items():
        product = products.get(pid)
        if not product:
            continue
        price = round(product.price or 0, 2)
        amount = round(qty * price * (1 - discount / 100), 2)
        total += amount
        db.add(OrderItem(order_id=order.id, product_id=pid, quantity=qty, price=price,
                         discount_pct=discount, vat_rate=product.vat_rate, amount=amount))

    log_action(db, "order", order.id, "created", None,
               f"Заказ собран клиентом в кабинете ({cp.trade_name or cp.name})")

    notif_title = f"🛒 Заказ из кабинета — {cp.trade_name or cp.name}"
    body = (f"Заказ №{order.number} на {round(total):,} ₽".replace(",", " ") +
            f", позиций: {len(wanted)}.\nЛежит черновиком — проверьте и подтвердите.")
    db.add(Notification(type="shop_order", title=notif_title, body=body,
                        link=f"/orders/{order.id}"))

    ensure_public_token(db, order)
    db.commit()

    threading.Thread(target=_push_bitrix_bg, args=(order.id,), daemon=True).start()
    _notify_telegram(db, order, cp, total)

    return JSONResponse({"ok": True, "order_number": order.number,
                         "track_url": f"/track/{order.public_token}"})


def _notify_telegram(db: Session, order: Order, cp: Counterparty, total: float) -> None:
    """Громкий алерт менеджерам в Telegram — теми же чатами, что и заказы из
    Bitrix24 (bitrix_alert_chat_ids в Настройках). Отдельный список заводить
    незачем: адресат тот же."""
    import os
    company = db.query(CompanySettings).first()
    if not company:
        return
    chat_ids = [c.strip() for c in (company.bitrix_alert_chat_ids or "").split(",") if c.strip()]
    bot_token = (company.tg_bot_token or "").strip() or os.getenv("TMS_BOT_TOKEN", "").strip()
    if not chat_ids or not bot_token:
        return
    text = (f"🛒 ЗАКАЗ ИЗ КАБИНЕТА КЛИЕНТА\n"
            f"№{order.number} — {cp.trade_name or cp.name}\n"
            f"Сумма: {round(total):,} ₽".replace(",", " ") +
            "\nСтатус: черновик, нужно подтвердить в TMS")
    import httpx
    try:
        with httpx.Client(timeout=10.0) as client:
            for chat_id in chat_ids:
                client.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.warning("Кабинет клиента: не удалось отправить Telegram-алерт: %s", e)

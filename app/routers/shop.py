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
import json
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
                        Notification, ShopCart)
from app.routers.public import _rate_limited, ensure_public_token
from app.utils import log_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shop", tags=["shop"])
templates = Jinja2Templates(directory="app/templates")

# Максимум позиций в одном заказе — защита от кривого/злого запроса.
_MAX_ITEMS = 200

# ── Правила заказа орешков ───────────────────────────────────────────────────
# В кабинете клиента продаются ТОЛЬКО орешки — линейки П1 (на сливочном масле)
# и П2 (на маргарине). Подставки, упаковка, сырьё и прочая номенклатура из
# справочника клиенту не показываются: он их не заказывает.
NUT_PREFIXES = ("п1.", "п2.")
LINE_LABELS = {"п1.": "на сливочном масле", "п2.": "на маргарине"}

# Коробка — 42 орешка, она же минимальный заказ по каждому виду: меньше
# коробки цех не собирает. Шаг — 3 штуки (орешки идут тройками на лотке),
# поэтому любое количество кратно 3 и не меньше 42.
BOX_SIZE = 42
MIN_QTY = 42
QTY_STEP = 3


def nut_display_name(name: str) -> str:
    """«П1.Орешки с кокосовой начинкой» → «Орешки с кокосовой начинкой на
    сливочном масле». Клиенту не нужно знать про внутренние коды линеек, но
    разница между маслом и маргарином для него важна — выносим её словами.

    Внутри TMS, в 1С и в сделке Bitrix остаётся исходное название номенклатуры:
    склад и бухгалтерия работают со справочником, а не с витриной."""
    raw = (name or "").strip()
    low = raw.lower()
    for prefix, suffix in LINE_LABELS.items():
        if low.startswith(prefix):
            return f"{raw[len(prefix):].strip()} {suffix}"
    return raw


def nut_line(name: str) -> str:
    """Линейка товара для группировки витрины: 'п1.' / 'п2.' / '' (не орешек)."""
    low = (name or "").lower()
    for prefix in NUT_PREFIXES:
        if low.startswith(prefix):
            return prefix
    return ""


def normalize_qty(qty: float) -> int:
    """Приводит количество к правилам: не меньше коробки и кратно 3.

    Витрина сама не даёт ввести другое, но запрос можно подделать руками —
    поэтому те же правила пересчитываются на сервере. Округляем ВВЕРХ: клиент
    получит не меньше, чем просил."""
    q = int(qty)
    if q < MIN_QTY:
        return MIN_QTY
    if q % QTY_STEP:
        q += QTY_STEP - (q % QTY_STEP)
    return q


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


def _catalog(db: Session, cp: Counterparty) -> tuple[list, list]:
    """(витрина орешков, позиции последнего заказа для кнопки «Повторить»).

    Остатки склада здесь намеренно НЕ участвуют: орешки печём под заказ, и
    позиция, которой сейчас нет на складе, всё равно доступна к заказу — цех
    успевает к дате доставки. Показывать клиенту «нет в наличии» значило бы
    отговаривать его от покупки там, где отговаривать не нужно."""
    discount = cp.default_discount_pct or 0.0

    products = (db.query(Product)
                .filter(Product.is_active == True)  # noqa: E712
                .order_by(Product.name)
                .all())

    catalog = []
    for p in products:
        line = nut_line(p.name)
        if not line:
            continue
        catalog.append({
            "id": p.id,
            "name": nut_display_name(p.name),
            "line": "butter" if line == "п1." else "margarine",
            "article": p.article or "",
            "unit": p.sale_unit or p.unit or "шт",
            "price": round((p.price or 0) * (1 - discount / 100), 2),
        })
    # Внутри линейки — по названию, линейки — масло, потом маргарин
    catalog.sort(key=lambda c: (c["line"] != "butter", c["name"]))

    known = {c["id"] for c in catalog}

    # ── Последний заказ клиента — для кнопки «Повторить» ────────────────────
    last_order = (db.query(Order)
                  .filter(Order.counterparty_id == cp.id,
                          Order.status != "cancelled")
                  .order_by(Order.date.desc(), Order.id.desc())
                  .first())
    last = []
    if last_order:
        for item in last_order.items:
            if item.product_id in known:
                last.append({"id": item.product_id, "qty": normalize_qty(item.quantity)})

    return catalog, last


def _load_cart(db: Session, cp: Counterparty, known: set[int]) -> list[dict]:
    """Сохранённая корзина контрагента, очищенная от товаров, которых больше
    нет на витрине (номенклатуру могли выключить, пока клиент думал)."""
    row = db.query(ShopCart).filter(ShopCart.counterparty_id == cp.id).first()
    if not row or not row.items:
        return []
    try:
        raw = json.loads(row.items)
    except (ValueError, TypeError):
        return []
    out = []
    for it in raw if isinstance(raw, list) else []:
        try:
            pid, qty = int(it.get("id")), float(it.get("qty") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if pid in known and qty > 0:
            out.append({"id": pid, "qty": normalize_qty(qty)})
    return out


def _save_cart(db: Session, cp: Counterparty, items: list[dict]) -> None:
    row = db.query(ShopCart).filter(ShopCart.counterparty_id == cp.id).first()
    if not row:
        row = ShopCart(counterparty_id=cp.id)
        db.add(row)
    row.items = json.dumps(items, ensure_ascii=False)
    db.commit()


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

    catalog, last = _catalog(db, cp)
    cart = _load_cart(db, cp, {c["id"] for c in catalog})
    company = db.query(CompanySettings).first()
    return templates.TemplateResponse(request, "public/shop.html", {
        "cp": cp,
        "company": company,
        "token": token,
        "catalog": catalog,
        "last_items": last,
        "cart_items": cart,
        "slots": _delivery_slots(),
        "discount": cp.default_discount_pct or 0.0,
        "default_address": cp.actual_address or cp.legal_address or "",
        "box_size": BOX_SIZE,
        "min_qty": MIN_QTY,
        "qty_step": QTY_STEP,
    })


@router.post("/{token}/cart")
async def save_cart(request: Request, token: str, db: Session = Depends(get_db)):
    """Автосохранение корзины — витрина дёргает при каждом изменении состава.

    Отвечает коротко и не валидирует состав строго: это черновик, а не заказ.
    Всё, что не проходит правила, отфильтруется при загрузке и при отправке."""
    cp = _find_counterparty(db, token)
    if not cp:
        return JSONResponse({"ok": False}, status_code=404)

    payload = await request.json()
    items = []
    for row in (payload.get("items") or [])[:_MAX_ITEMS]:
        try:
            pid, qty = int(row.get("id")), float(row.get("qty") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if qty > 0:
            items.append({"id": pid, "qty": normalize_qty(qty)})
    _save_cart(db, cp, items)
    return JSONResponse({"ok": True})


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

    # Кабинет торгует только орешками — чужую номенклатуру, даже если её id
    # подставили в запрос руками, в заказ не пускаем.
    products = {p.id: p for p in db.query(Product).filter(
        Product.id.in_(wanted.keys()), Product.is_active == True).all()  # noqa: E712
        if nut_line(p.name)}
    if not products:
        return JSONResponse({"ok": False, "error": "Товары не найдены"}, status_code=400)
    wanted = {pid: normalize_qty(qty) for pid, qty in wanted.items() if pid in products}

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
    # Корзина «переехала» в заказ — очищаем, иначе клиент вернётся в кабинет
    # и увидит только что отправленный состав как незаконченный черновик.
    _save_cart(db, cp, [])
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

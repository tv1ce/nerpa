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
import asyncio
import json
import logging
import secrets
from datetime import date, timedelta

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (Counterparty, CompanySettings, Product, Order, OrderItem,
                        Notification, ShopCart, ShopBooking)
from app.routers.public import _rate_limited
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

# В коробку влезает 51 орешек — это ТАРА, а не минимальный заказ. Минимальная
# партия по каждому вкусу — 42 шт, шаг — 3 штуки (орешки идут тройками на
# лотке). Поэтому количество всегда кратно 3 и не меньше 42, а число коробок
# считается как «сколько тары понадобится»: до 51 включительно — одна.
BOX_CAPACITY = 51
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


def client_outlets(db: Session, cp: Counterparty) -> list[dict]:
    """Точки клиента — по адресам его прошлых заказов.

    У одного юрлица бывает несколько кофеен, и заказывают на них по-разному.
    Отдельные ссылки на точку заводить не стали: товаровед один, ссылка у него
    одна, а точку он выбирает внутри. Список берём из истории заказов, а не из
    Bitrix24 — это локально, быстро и не ломается, когда CRM недоступна.

    Ключ точки — «улица:дом» (та же нормализация, что в аналитике точек):
    один и тот же адрес пишут по-разному, и без нормализации «Гончарная 2» и
    «г Санкт-Петербург, ул Гончарная, д 2» стали бы двумя точками."""
    from app.services.outlets import normalize_address, address_label

    rows = (db.query(Order.delivery_address)
            .filter(Order.counterparty_id == cp.id,
                    Order.status != "cancelled",
                    Order.delivery_address.isnot(None))
            .order_by(Order.date.desc(), Order.id.desc())
            .all())

    found: dict[str, dict] = {}
    for (addr,) in rows:
        key = normalize_address(addr)
        if not key:
            continue
        if key not in found:
            # Первым идёт самое свежее написание адреса — его и показываем
            found[key] = {"key": key, "address": (addr or "").strip(),
                          "label": address_label(key), "orders": 0}
        found[key]["orders"] += 1

    fallback = (cp.actual_address or cp.legal_address or "").strip()
    if fallback:
        key = normalize_address(fallback)
        if key and key not in found:
            found[key] = {"key": key, "address": fallback,
                          "label": address_label(key), "orders": 0}

    return sorted(found.values(), key=lambda o: (-o["orders"], o["label"]))


def pick_outlet(outlets: list[dict], key: str | None) -> dict | None:
    """Выбранная точка: из ссылки, иначе самая ходовая."""
    if not outlets:
        return None
    for o in outlets:
        if o["key"] == key:
            return o
    return outlets[0]


def _catalog(db: Session, cp: Counterparty, outlet: dict | None = None) -> tuple[list, list]:
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

    # ── Последний заказ ЭТОЙ точки — для кнопки «Повторить» ─────────────────
    # Точки заказывают по-разному, и повтор заказа с соседней кофейни сбивал бы
    # с толку сильнее, чем помогал.
    orders = (db.query(Order)
              .filter(Order.counterparty_id == cp.id, Order.status != "cancelled")
              .order_by(Order.date.desc(), Order.id.desc())
              .limit(50).all())
    last_order = _first_of_outlet(orders, outlet)
    last = []
    if last_order:
        for item in last_order.items:
            if item.product_id in known:
                last.append({"id": item.product_id, "qty": normalize_qty(item.quantity)})

    return catalog, last


def _first_of_outlet(orders: list, outlet: dict | None):
    """Первый заказ, относящийся к точке. Без точки — просто первый."""
    if not outlet:
        return orders[0] if orders else None
    from app.services.outlets import normalize_address
    for o in orders:
        if normalize_address(o.delivery_address) == outlet["key"]:
            return o
    return None


def _cart_map(row: ShopCart | None) -> dict:
    """Корзины контрагента как {ключ точки: [позиции]}.

    Исторически в поле лежал плоский список — это корзина клиента до появления
    точек. Читаем оба формата, пишем всегда новый."""
    if not row or not row.items:
        return {}
    try:
        raw = json.loads(row.items)
    except (ValueError, TypeError):
        return {}
    if isinstance(raw, list):
        return {"": raw}
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items() if isinstance(v, list)}
    return {}


def _clean_items(raw: list, known: set[int]) -> list[dict]:
    """Отбрасывает товары, которых больше нет на витрине (номенклатуру могли
    выключить, пока клиент думал), и приводит количества к правилам."""
    out = []
    for it in raw or []:
        try:
            pid, qty = int(it.get("id")), float(it.get("qty") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if pid in known and qty > 0:
            out.append({"id": pid, "qty": normalize_qty(qty)})
    return out


def _load_cart(db: Session, cp: Counterparty, known: set[int],
               outlet_key: str = "") -> list[dict]:
    """Корзина выбранной точки. Наследует старую «безточечную», если своей ещё нет."""
    carts = _cart_map(db.query(ShopCart).filter(ShopCart.counterparty_id == cp.id).first())
    raw = carts.get(outlet_key)
    if raw is None and outlet_key and "" in carts:
        raw = carts[""]          # корзина, набранная до появления точек
    return _clean_items(raw or [], known)


def _save_cart(db: Session, cp: Counterparty, items: list[dict],
               outlet_key: str = "") -> None:
    row = db.query(ShopCart).filter(ShopCart.counterparty_id == cp.id).first()
    if not row:
        row = ShopCart(counterparty_id=cp.id)
        db.add(row)
    carts = _cart_map(row)
    carts.pop("", None)          # старый формат больше не поддерживаем на запись
    carts[outlet_key] = items
    row.items = json.dumps(carts, ensure_ascii=False)
    db.commit()


WEEKDAY_NAMES = ["понедельник", "вторник", "среда", "четверг",
                 "пятница", "суббота", "воскресенье"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]

# Сколько ближайших дат отгрузки предлагать кнопками.
_SLOT_COUNT = 3
# Как далеко вперёд имеет смысл искать: при одном дне отгрузки в неделю трёх
# дат хватает на месяц, дальше искать нечего.
_SLOT_HORIZON_DAYS = 60


def shipping_weekdays(company) -> list[int]:
    """Дни недели, по которым мы отгружаем: [0, 3] — понедельник и четверг.

    Настраивается в Настройках → Bitrix24 → «Кабинет клиента»: график машин
    меняется, и захардкоженные «будни» заставляли бы клиента выбирать дату, в
    которую никто никуда не едет."""
    raw = (getattr(company, "shipping_weekdays", None) or "").strip()
    days = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 6 and int(part) not in days:
            days.append(int(part))
    if not days:
        days = [0, 1, 2, 3, 4]      # настройка пуста — возим по будням
    return sorted(days)


def format_slot_date(d: date) -> str:
    """«Понедельник, 18 августа» — день недели словом и целиком.

    Сокращения вида «Пн, 18.08» экономят место, но читаются как код: клиент
    выбирает день, когда ему привезут товар, и должен видеть его без расшифровки."""
    return f"{WEEKDAY_NAMES[d.weekday()].capitalize()}, {d.day} {MONTHS_GEN[d.month - 1]}"


def daily_capacity(company) -> int:
    """Сколько орешков цех вывозит одной датой. 0 — без ограничения."""
    try:
        return max(0, int(getattr(company, "daily_nut_capacity", None) or 0))
    except (TypeError, ValueError):
        return 0


def date_load(db: Session, d: date) -> int:
    """Сколько орешков уже обещано на эту дату — по всем клиентам.

    Складываем два источника, которые не пересекаются по построению:
      * заказы в TMS с этой датой доставки (они приехали из Bitrix24);
      * брони кабинета, по сделкам которых заказ в TMS ещё не появился —
        робот довозит его не мгновенно, и в этом окне дата выглядела бы
        свободной.
    Отменённые заказы не считаем: их мощность освободилась."""
    nut_ids = {p.id for p in db.query(Product.id, Product.name).all() if nut_line(p.name)}
    if not nut_ids:
        return 0

    booked = 0
    rows = (db.query(OrderItem.quantity)
            .join(Order, Order.id == OrderItem.order_id)
            .filter(Order.delivery_date == d,
                    Order.status != "cancelled",
                    OrderItem.product_id.in_(nut_ids))
            .all())
    booked += int(sum(q or 0 for (q,) in rows))

    known_deals = {str(x) for (x,) in db.query(Order.bitrix_deal_id)
                   .filter(Order.bitrix_deal_id.isnot(None)).all()}
    for b in db.query(ShopBooking).filter(ShopBooking.delivery_date == d).all():
        if str(b.bitrix_deal_id or "") not in known_deals:
            booked += int(b.qty or 0)
    return booked


def date_free(db: Session, company, d: date) -> int | None:
    """Свободная мощность на дату. None — ограничение не задано."""
    cap = daily_capacity(company)
    if not cap:
        return None
    return max(0, cap - date_load(db, d))


def _record_booking(db: Session, cp: Counterparty, d: date, qty: int, deal_id: str,
                    items: list | None = None) -> None:
    """Фиксирует бронь и подчищает старые: держать их дольше месяца незачем."""
    if not d:
        return
    detail = json.dumps([{"id": p.id, "qty": int(q)} for p, q, _ in (items or [])],
                        ensure_ascii=False)
    db.add(ShopBooking(delivery_date=d, counterparty_id=cp.id, qty=int(qty),
                       items=detail, bitrix_deal_id=str(deal_id) if deal_id else None))
    db.query(ShopBooking).filter(
        ShopBooking.delivery_date < date.today() - timedelta(days=30)).delete()


def production_plan(db: Session, company) -> dict:
    """Что цеху печь к ближайшей отгрузке — в разрезе вкусов.

    Собирается из трёх источников, каждый со своей ролью:
      * заказы в TMS с этой датой доставки — подтверждённые, приехали из Bitrix24;
      * брони кабинета, по сделкам которых заказ ещё не вернулся — клиент их уже
        отправил, печь надо, а в TMS они появятся с задержкой;
      * корзины, которые клиенты набирают ПРЯМО СЕЙЧАС, — отдельной строкой и в
        план не входят: заказ ещё не отправлен и может не отправиться вовсе.
        Но цех должен видеть, что на него надвигается.

    Дата берётся ближайшая из тех, на которые вообще что-то заказано, а не
    «завтра»: при отгрузке два раза в неделю завтра обычно пусто."""
    from app.services.outlets import _flavor

    nut_names = {p.id: p.name for p in db.query(Product.id, Product.name).all()
                 if nut_line(p.name)}
    if not nut_names:
        return {"date": None}

    # ── Ближайшая дата, на которую что-то есть ──────────────────────────────
    today = date.today()
    dates = [d for (d,) in db.query(Order.delivery_date)
             .filter(Order.delivery_date >= today, Order.status != "cancelled").distinct().all() if d]
    dates += [d for (d,) in db.query(ShopBooking.delivery_date)
              .filter(ShopBooking.delivery_date >= today).distinct().all() if d]
    if not dates:
        return {"date": None}
    day = min(dates)

    by_product: dict[int, int] = {}

    rows = (db.query(OrderItem.product_id, OrderItem.quantity)
            .join(Order, Order.id == OrderItem.order_id)
            .filter(Order.delivery_date == day, Order.status != "cancelled",
                    OrderItem.product_id.in_(nut_names.keys()))
            .all())
    for pid, qty in rows:
        by_product[pid] = by_product.get(pid, 0) + int(qty or 0)

    known_deals = {str(x) for (x,) in db.query(Order.bitrix_deal_id)
                   .filter(Order.bitrix_deal_id.isnot(None)).all()}
    for b in db.query(ShopBooking).filter(ShopBooking.delivery_date == day).all():
        if str(b.bitrix_deal_id or "") in known_deals:
            continue                      # заказ уже доехал — посчитан выше
        try:
            detail = json.loads(b.items or "[]")
        except (ValueError, TypeError):
            detail = []
        for it in detail:
            pid = int(it.get("id", 0))
            if pid in nut_names:
                by_product[pid] = by_product.get(pid, 0) + int(it.get("qty") or 0)

    # ── Строки плана: вкус + линейка, крупно и коротко ──────────────────────
    lines = []
    for pid, qty in by_product.items():
        if qty <= 0:
            continue
        name = nut_names[pid]
        lines.append({
            "name": f"{_flavor(name).capitalize()} · "
                    f"{'масло' if nut_line(name) == 'п1.' else 'маргарин'}",
            "qty": qty,
            "boxes": -(-qty // BOX_CAPACITY),
        })
    lines.sort(key=lambda r: -r["qty"])

    total = sum(r["qty"] for r in lines)
    cap = daily_capacity(company)

    # ── Что набирают в корзинах прямо сейчас ────────────────────────────────
    in_carts = 0
    for row in db.query(ShopCart).all():
        try:
            for it in json.loads(row.items or "[]"):
                if int(it.get("id", 0)) in nut_names:
                    in_carts += int(it.get("qty") or 0)
        except (ValueError, TypeError):
            continue

    return {
        "date": format_slot_date(day),
        "date_iso": day.isoformat(),
        "days_left": (day - today).days,
        "lines": lines,
        "total": total,
        "boxes": sum(r["boxes"] for r in lines),
        "capacity": cap,
        "load_pct": round(total / cap * 100) if cap else 0,
        "in_carts": in_carts,
    }


def _delivery_slots(db: Session, company) -> list[dict]:
    """Ближайшие даты отгрузки — кнопками, вместо пустого календаря.

    У каждой даты показываем остаток мощности цеха: клиент должен видеть, что
    день забит, ДО того как соберёт корзину, а не узнавать это при отправке.
    Календарь остаётся рядом отдельной опцией: если нужна дата вне графика,
    клиент просит её сам, а не звонит менеджеру."""
    days = shipping_weekdays(company)
    slots, d = [], date.today()
    for _ in range(_SLOT_HORIZON_DAYS):
        d += timedelta(days=1)
        if d.weekday() in days:
            slots.append({"value": d.isoformat(), "label": format_slot_date(d),
                          "free": date_free(db, company, d)})
            if len(slots) >= _SLOT_COUNT:
                break
    return slots


def _order_history(db: Session, cp: Counterparty, outlet: dict | None = None,
                   limit: int = 12) -> list[dict]:
    """История заказов клиента — то, что он уже у нас заказывал.

    Берём заказы из TMS: они приезжают туда из Bitrix24 после согласования, то
    есть в истории клиент видит именно подтверждённые заказы, а не свои
    неотправленные черновики. Отменённые не показываем — это не история
    покупок, а шум."""
    from app.routers.public import PUBLIC_STATUS_LABELS

    orders = (db.query(Order)
              .filter(Order.counterparty_id == cp.id, Order.status != "cancelled")
              .order_by(Order.date.desc(), Order.id.desc())
              .limit(limit * 4 if outlet else limit)
              .all())
    if outlet:
        from app.services.outlets import normalize_address
        orders = [o for o in orders
                  if normalize_address(o.delivery_address) == outlet["key"]][:limit]
    out = []
    for o in orders:
        # Ключ намеренно не "items": в Jinja `o.items` разрешается в метод
        # словаря dict.items, а не в наши строки заказа.
        lines = [{"name": nut_display_name(i.product.name) if i.product else "Товар",
                  "qty": i.quantity,
                  "amount": i.amount or 0}
                 for i in o.items]
        out.append({
            "number": o.number,
            "date": f"{o.date.day} {MONTHS_GEN[o.date.month - 1]} {o.date.year}" if o.date else "",
            "status": PUBLIC_STATUS_LABELS.get(o.status, o.status),
            "status_code": o.status,
            "delivery_date": format_slot_date(o.delivery_date) if o.delivery_date else "",
            "lines": lines,
            "total": round(sum(i["amount"] for i in lines), 2),
            "track_url": f"/track/{o.public_token}" if o.public_token else "",
        })
    return out


@router.get("/{token}", response_class=HTMLResponse)
async def shop_page(request: Request, token: str, p: str = "", db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "?"
    if _rate_limited(ip):
        return HTMLResponse("Слишком много запросов, попробуйте через минуту", status_code=429)

    cp = _find_counterparty(db, token)
    if not cp:
        return templates.TemplateResponse(request, "public/shop_notfound.html",
                                          {}, status_code=404)

    outlets = client_outlets(db, cp)
    outlet = pick_outlet(outlets, p)
    outlet_key = outlet["key"] if outlet else ""

    catalog, last = _catalog(db, cp, outlet)
    cart = _load_cart(db, cp, {c["id"] for c in catalog}, outlet_key)
    company = db.query(CompanySettings).first()
    history = _order_history(db, cp, outlet)
    return templates.TemplateResponse(request, "public/shop.html", {
        "cp": cp,
        "company": company,
        "token": token,
        "catalog": catalog,
        "last_items": last,
        "cart_items": cart,
        "slots": _delivery_slots(db, company),
        "history": history,
        "min_date": (date.today() + timedelta(days=1)).isoformat(),
        "capacity": daily_capacity(company),
        "discount": cp.default_discount_pct or 0.0,
        "default_address": (outlet["address"] if outlet
                            else (cp.actual_address or cp.legal_address or "")),
        "outlets": outlets,
        "outlet": outlet,
        "outlet_key": outlet_key,
        "box_capacity": BOX_CAPACITY,
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
    _save_cart(db, cp, items, str(payload.get("outlet") or ""))
    return JSONResponse({"ok": True})


@router.post("/{token}/submit")
async def submit_order(request: Request, token: str, db: Session = Depends(get_db)):
    """Заказ из кабинета → карточка клиента в Bitrix24, стадия «Заказ согласован».

    Заказ в TMS здесь НЕ создаётся намеренно: он приедет обратно роботом с этой
    стадии через /api/bitrix/webhook/deal-approved — тем же путём, что и заказы
    менеджеров. Пиши мы заказ ещё и напрямую, на каждый заказ из кабинета в TMS
    было бы по два: свой и приехавший из сделки.

    Из-за этого Bitrix здесь — единственный носитель заказа, и обращение к нему
    синхронное: если CRM недоступна, клиент должен увидеть честную ошибку и
    повторить, а корзина обязана остаться нетронутой."""
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

    company_settings = db.query(CompanySettings).first()

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

    items = []
    total = 0.0
    for pid, qty in wanted.items():
        product = products.get(pid)
        if not product:
            continue
        qty = normalize_qty(qty)
        price = round((product.price or 0) * (1 - discount / 100), 2)
        total += qty * price
        items.append((product, qty, price))
    if not items:
        return JSONResponse({"ok": False, "error": "Корзина пуста"}, status_code=400)

    delivery_date = None
    raw_date = (payload.get("delivery_date") or "").strip()
    if raw_date:
        try:
            parsed = date.fromisoformat(raw_date)
            if parsed >= date.today():
                delivery_date = parsed
        except ValueError:
            pass

    # ── Мощность цеха на выбранную дату ────────────────────────────────────
    # Проверяем ПЕРЕД походом в Bitrix: отказ должен быть мгновенным и внятным,
    # а не после того, как заказ уже уехал в сделку.
    total_qty = int(sum(qty for _, qty, _ in items))
    free = date_free(db, company_settings, delivery_date) if delivery_date else None
    if free is not None and total_qty > free:
        alternatives = [s for s in _delivery_slots(db, company_settings)
                        if s["value"] != (delivery_date.isoformat() if delivery_date else "")
                        and (s["free"] is None or s["free"] >= total_qty)]
        if free <= 0:
            msg = f"На {format_slot_date(delivery_date).lower()} мы уже полностью загружены."
        else:
            msg = (f"На {format_slot_date(delivery_date).lower()} осталось "
                   f"{free} орешков, а в заказе {total_qty}.")
        if alternatives:
            msg += " Ближайшая свободная дата — " + alternatives[0]["label"].lower() + "."
        else:
            msg += " Выберите другую дату или свяжитесь с менеджером."
        return JSONResponse({"ok": False, "error": msg,
                             "free": free, "needed": total_qty,
                             "slots": alternatives}, status_code=409)

    outlets = client_outlets(db, cp)
    outlet = pick_outlet(outlets, str(payload.get("outlet") or ""))
    outlet_key = outlet["key"] if outlet else ""

    order_data = {
        "delivery_date": delivery_date,
        "address": (payload.get("address") or "").strip()[:500]
                   or (outlet["address"] if outlet else "")
                   or cp.actual_address or cp.legal_address or "",
        "contact": (payload.get("contact") or "").strip()[:200]
                   or cp.contact_person or cp.phone or "",
        "comment": (payload.get("comment") or "").strip()[:1000],
    }

    from app.services.bitrix_client import apply_shop_order_to_deal
    result = await asyncio.to_thread(apply_shop_order_to_deal, cp, items, order_data,
                                     company_settings, db)

    if not result["ok"]:
        # Корзину НЕ трогаем: заказ никуда не уехал, клиент повторит отправку.
        logger.error("Кабинет клиента %s: заказ не ушёл в Bitrix24 — %s", cp.name, result["error"])
        _notify_telegram(db, cp, total, items, ok=False, detail=result["error"],
                         order_data=order_data)
        return JSONResponse(
            {"ok": False, "error": "Не удалось передать заказ менеджеру. "
                                   "Попробуйте ещё раз или позвоните нам."},
            status_code=502)

    _save_cart(db, cp, [], outlet_key)
    _record_booking(db, cp, delivery_date, total_qty, result["deal_id"], items)
    log_action(db, "counterparty", cp.id, "updated", None,
               f"Заказ из кабинета клиента ушёл в сделку Bitrix24 #{result['deal_id']}"
               + (" (создана новая карточка)" if result["created"] else ""))
    db.add(Notification(
        type="shop_order",
        title=f"🛒 Заказ из кабинета — {cp.trade_name or cp.name}",
        body=(f"Сумма {round(total):,} ₽".replace(",", " ") +
              f", позиций: {len(items)}.\nСделка Bitrix24 #{result['deal_id']} "
              f"переведена на «Заказ согласован» — заказ приедет в TMS автоматически."),
        link=f"/counterparties/{cp.id}",
    ))
    db.commit()

    _notify_telegram(db, cp, total, items, ok=True, detail=result["deal_id"],
                     order_data=order_data)
    return JSONResponse({"ok": True, "redirect": f"/shop/{token}/done"})


@router.get("/{token}/done", response_class=HTMLResponse)
async def order_done(request: Request, token: str, db: Session = Depends(get_db)):
    """Страница «заказ принят».

    Ссылки на трекинг здесь нет намеренно: заказ ещё едет из Bitrix в TMS, и
    номера у него пока не существует. Обещать клиенту статус, которого нет,
    хуже, чем честно сказать, что менеджер подтвердит."""
    cp = _find_counterparty(db, token)
    if not cp:
        return templates.TemplateResponse(request, "public/shop_notfound.html",
                                          {}, status_code=404)
    company = db.query(CompanySettings).first()
    return templates.TemplateResponse(request, "public/shop_done.html", {
        "cp": cp, "company": company, "token": token,
    })


def _notify_telegram(db: Session, cp: Counterparty, total: float, items: list,
                     ok: bool, detail: str = "", order_data: dict | None = None) -> None:
    """Уведомление менеджерам о заказе из кабинета.

    Канал свой (Настройки → Bitrix24 → «Заказы из кабинета»), отдельно от
    алертов по сделкам из CRM: у заказов из кабинета другая аудитория и другая
    срочность. Если канал не задан — молча ничего не шлём.

    В сообщении всё, чтобы принять решение не открывая TMS: заведение и точка,
    юрлицо-заказчик (вывеска у разных ИП совпадает — по ней одной не поймёшь,
    кто заказал), дата и адрес доставки, комментарий клиента, состав и сумма."""
    import os
    company = db.query(CompanySettings).first()
    if not company:
        return
    chat_ids = [c.strip() for c in (company.shop_alert_chat_ids or "").split(",") if c.strip()]
    bot_token = (company.tg_bot_token or "").strip() or os.getenv("TMS_BOT_TOKEN", "").strip()
    if not chat_ids or not bot_token:
        return

    data = order_data or {}
    money = f"{round(total):,} ₽".replace(",", " ")

    outlet = cp.trade_name or cp.name
    if cp.outlet_name:
        outlet += f" — {cp.outlet_name}"
    # Юрлицо показываем отдельной строкой и только если оно отличается от
    # вывески: у сетей под одной вывеской работают разные ИП, и логисту важно
    # видеть, от кого именно заказ.
    head = [outlet]
    if cp.name and cp.name != outlet:
        head.append(cp.name)

    when = format_slot_date(data["delivery_date"]) if data.get("delivery_date") else "не указана"
    where = data.get("address") or cp.actual_address or cp.legal_address or "не указан"

    body = [f"Когда: {when}", f"Куда: {where}"]
    if data.get("contact"):
        body.append(f"Кто примет: {data['contact']}")

    lines = [f"{nut_display_name(p.name)} — {qty:g} шт" for p, qty, _ in items]

    if ok:
        parts = ["🛒 ЗАКАЗ ИЗ КАБИНЕТА КЛИЕНТА", *head, "", *body, "", *lines, "", f"Сумма: {money}"]
        if data.get("comment"):
            parts.append(f"Примечание клиента: {data['comment']}")
        parts.append(f"Сделка #{detail} → «Заказ согласован»")
    else:
        parts = ["⚠️ ЗАКАЗ ИЗ КАБИНЕТА НЕ УШЁЛ В BITRIX24", *head, "", *body, "",
                 *lines, "", f"Сумма: {money}"]
        if data.get("comment"):
            parts.append(f"Примечание клиента: {data['comment']}")
        parts += [f"Причина: {detail}", "Клиент увидел ошибку — свяжитесь с ним."]
    text = "\n".join(parts)

    # Отправляем ТОЛЬКО через общий отправитель: он ходит в Telegram через
    # локальный SOCKS-прокси (TMS_PROXY). Напрямую с российского сервера
    # api.telegram.org не отвечает, и собственный httpx-клиент здесь молча
    # падал с «Network is unreachable», хотя бот и остальные уведомления
    # работали.
    from app.services.telegram_send import send_topic_message
    for chat_id in chat_ids:
        try:
            send_topic_message(int(chat_id), text, bot_token)
        except Exception as e:
            logger.warning("Кабинет клиента: не удалось отправить Telegram-алерт в %s: %s",
                           chat_id, e)

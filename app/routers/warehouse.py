from datetime import date, datetime, timedelta
from fastapi import APIRouter, Request, Depends, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.tz import now as msk_now
from app.database import get_db
from app.auth import login_required, role_required
from app.models import Product, StockMovement, Order, StockAdjustment, StockAdjustmentLine, CompanySettings, User
from app.utils import maybe_notify_low_stock, log_action
from app.services.telegram_send import notify_warehouse_group_bg

# Статусы заказа, считающиеся «в работе» (не черновик и не завершён/отменён)
ACTIVE_ORDER_STATUSES = ["confirmed", "paid", "assembled", "handed", "delivered"]

router = APIRouter(prefix="/warehouse", tags=["warehouse"])


@router.post("/toggle-view")
@login_required
async def toggle_view(request: Request, next: str = Form(default="/warehouse/")):
    """Переключает вид склада между мобильным и десктопным для роли warehouse."""
    current = request.session.get("warehouse_view", "mobile")
    request.session["warehouse_view"] = "desktop" if current == "mobile" else "mobile"
    return RedirectResponse(url=next, status_code=302)
templates = Jinja2Templates(directory="app/templates")

MOVEMENT_TYPES = {
    "in": "Приход",
    "out": "Расход",
    "adjustment": "Корректировка",
}
REASONS = {
    "in":  ["Поставка", "Возврат от клиента", "Начальный остаток", "Другое"],
    "out": ["Продажа", "Списание", "Брак", "Другое"],
    "adjustment": ["Инвентаризация", "Исправление ошибки", "Другое"],
}


def _assembly_queue_count(db: Session) -> int:
    """Количество заказов, ожидающих сборки — для бейджа в таббаре."""
    candidates = (
        db.query(Order)
        .filter(Order.status.in_(["confirmed", "paid"]))
        .all()
    )
    return sum(1 for o in candidates if o.ready_for_assembly)


def _get_balances(db: Session) -> dict:
    """Возвращает словарь {product_id: current_balance}.

    Логика типов движений:
      - in:         приход — увеличивает остаток
      - out:        расход — уменьшает остаток
      - adjustment: корректировка с явным знаком quantity:
                    положительное значение → увеличивает (излишки),
                    отрицательное значение → уменьшает (недостача).
                    Quantity хранится как введённое (может быть < 0).
    """
    from sqlalchemy import case as _case

    products = db.query(Product).filter(Product.is_active == True).all()
    if not products:
        return {}

    # Один агрегирующий запрос вместо 3×N запросов
    rows = db.query(
        StockMovement.product_id,
        func.sum(_case(
            (StockMovement.movement_type == "in", StockMovement.quantity),
            else_=0,
        )).label("in_qty"),
        func.sum(_case(
            (StockMovement.movement_type == "out", StockMovement.quantity),
            else_=0,
        )).label("out_qty"),
        func.sum(_case(
            (StockMovement.movement_type == "adjustment", StockMovement.quantity),
            else_=0,
        )).label("adj_qty"),
    ).group_by(StockMovement.product_id).all()

    agg = {r.product_id: r for r in rows}
    result = {}
    for p in products:
        r = agg.get(p.id)
        in_qty  = float(r.in_qty  or 0) if r else 0.0
        out_qty = float(r.out_qty or 0) if r else 0.0
        adj_qty = float(r.adj_qty or 0) if r else 0.0
        result[p.id] = round((p.initial_stock or 0) + in_qty - out_qty + adj_qty, 3)

    # Реальный остаток по факту продаж видит только 1С (кладовщик кнопкой
    # «Собрано» товар больше не списывает — см. mark_assembled). Для товаров,
    # у которых есть свежая выгрузка остатка из 1С (StockBalance1C), она
    # ЗАМЕНЯЕТ локальный расчёт — это теперь единственный точный источник.
    # Для товаров вне 1С (или пока не засинкан остаток) остаётся локальный расчёт.
    from app.services.onec_client import get_1c_balances
    result.update(get_1c_balances(db))
    return result


# ── Главная страница: остатки ─────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
@login_required
async def warehouse_index(request: Request, db: Session = Depends(get_db)):
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()
    balances = _get_balances(db)

    # Считаем низкий остаток
    low_stock = [p for p in products if balances.get(p.id, 0) <= (p.min_stock or 0) and (p.min_stock or 0) > 0]

    # Последние 10 движений
    recent = (
        db.query(StockMovement)
        .order_by(StockMovement.created_at.desc())
        .limit(10).all()
    )

    # Очередь сборки: заказы, «упавшие» кладовщику
    # (предоплата — после оплаты, отсрочка — после подтверждения)
    assembly_candidates = (
        db.query(Order)
        .filter(Order.status.in_(["confirmed", "paid"]))
        .order_by(Order.delivery_date.asc().nullslast(), Order.date.asc())
        .all()
    )
    assembly_queue = [o for o in assembly_candidates if o.ready_for_assembly]

    return templates.TemplateResponse(request, "warehouse/index.html", {
        "products": products,
        "balances": balances,
        "low_stock": low_stock,
        "recent": recent,
        "assembly_queue": assembly_queue,
        "movement_types": MOVEMENT_TYPES,
    })


# ── Раздел «Сборка заказов» кабинета кладовщика ───────────────────────────────
# Тонкая обёртка над той же очередью, что раньше показывалась на общей главной
# /warehouse/ — вынесена отдельным пунктом меню, логика/кнопка «Собрано» не менялись.

@router.get("/assembly", response_class=HTMLResponse)
@login_required
async def warehouse_assembly(request: Request, db: Session = Depends(get_db)):
    assembly_candidates = (
        db.query(Order)
        .filter(Order.status.in_(["confirmed", "paid"]))
        .order_by(Order.delivery_date.asc().nullslast(), Order.date.asc())
        .all()
    )
    assembly_queue = [o for o in assembly_candidates if o.ready_for_assembly]
    return templates.TemplateResponse(request, "warehouse/assembly.html", {
        "assembly_queue": assembly_queue,
        "assembly_queue_count": len(assembly_queue),
    })


# ── Очередь сборки: кладовщик отмечает заказ собранным ────────────────────────

@router.post("/orders/{order_id}/assemble")
@login_required
def mark_assembled(request: Request, order_id: int, background: BackgroundTasks,
                   db: Session = Depends(get_db)):
    # Обычный def, не async: обработчик пишет в SQLite, а синхронный SQLAlchemy на
    # event loop подвешивал бы весь сайт на время ожидания блокировки записи.
    # Декоратор login_required уводит такие обработчики в threadpool —
    # см. auth._call_handler.
    order = db.query(Order).filter(Order.id == order_id).first()
    if order and order.ready_for_assembly:
        old = order.status
        order.status = "assembled"
        order.assembled_at = datetime.now()   # момент отгрузки для табло цеха
        log_action(db, "order", order_id, "status_changed",
                   request.session.get("user_id"),
                   "Заказ собран кладовщиком",
                   field="status", old_value=old, new_value="assembled")
        # Автосписание убрано: реальный расход товара со склада проводит 1С сама
        # при проведении УПД логистом. Раньше здесь ещё создавалось движение
        # StockMovement(out) — это дублировало списание (товар уходил дважды:
        # один раз в NERPA по факту сборки, второй раз в 1С по факту УПД).
        db.commit()

        user = db.query(User).filter(User.id == request.session.get("user_id")).first()
        items_text = "\n".join(
            f"  • {i.product.name if i.product else '—'} — {i.quantity:g} {i.product.unit if i.product else ''}"
            for i in order.items if i.product_id
        )
        cp = order.counterparty
        # Уведомление уходит после ответа: Telegram ходит через SOCKS-прокси,
        # и его недоступность не должна задерживать редирект кладовщику.
        background.add_task(
            notify_warehouse_group_bg, "assembled",
            f"📦 Заказ №{order.number} собран\n"
            f"Клиент: {(cp.trade_name or cp.name) if cp else '—'}\n"
            f"Кладовщик: {user.full_name if user else '—'}\n"
            f"{items_text}"
        )
    return RedirectResponse(url="/warehouse/", status_code=302)


# ── Печатная наклейка на коробку ──────────────────────────────────────────────

@router.get("/orders/{order_id}/label", response_class=HTMLResponse)
@login_required
async def order_label(request: Request, order_id: int, db: Session = Depends(get_db)):
    """Печатная наклейка на коробку: клиент, адрес, дата, состав заказа.
    Отдельная страница без меню — удобно печатать и клеить на коробку."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return HTMLResponse("<h2 style='font-family:sans-serif'>Заказ не найден</h2>", status_code=404)
    company = db.query(CompanySettings).first()
    return templates.TemplateResponse(request, "warehouse/label.html", {"order": order, "company": company})


# ── Журнал движений ───────────────────────────────────────────────────────────

@router.get("/journal", response_class=HTMLResponse)
@login_required
async def journal(
    request: Request,
    product_id: int = 0,
    mtype: str = "",
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
):
    q = db.query(StockMovement).order_by(StockMovement.date.desc(), StockMovement.id.desc())
    if product_id:
        q = q.filter(StockMovement.product_id == product_id)
    if mtype:
        q = q.filter(StockMovement.movement_type == mtype)
    if date_from:
        try:
            q = q.filter(StockMovement.date >= date.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            q = q.filter(StockMovement.date <= date.fromisoformat(date_to))
        except ValueError:
            pass
    movements = q.limit(200).all()
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()

    return templates.TemplateResponse(request, "warehouse/journal.html", {
        "movements": movements,
        "products": products,
        "movement_types": MOVEMENT_TYPES,
        "product_id": product_id,
        "mtype": mtype,
        "date_from": date_from,
        "date_to": date_to,
        "assembly_queue_count": _assembly_queue_count(db),
    })


# ── Форма новой операции ──────────────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
@login_required
async def new_movement(
    request: Request,
    product_id: int = 0,
    mtype: str = "in",
    db: Session = Depends(get_db),
):
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()
    orders = (
        db.query(Order)
        .filter(Order.status.in_(ACTIVE_ORDER_STATUSES))
        .order_by(Order.date.desc()).limit(50).all()
    )
    balances = _get_balances(db)
    selected_product_obj = next((p for p in products if p.id == product_id), None) if product_id else None
    return templates.TemplateResponse(request, "warehouse/movement_form.html", {
        "products": products,
        "orders": orders,
        "balances": balances,
        "movement_types": MOVEMENT_TYPES,
        "reasons": REASONS,
        "selected_product": product_id,
        "selected_product_name": selected_product_obj.name if selected_product_obj else None,
        "selected_type": mtype,
        "today": date.today().isoformat(),
        "assembly_queue_count": _assembly_queue_count(db),
    })


@router.post("/new")
@login_required
async def create_movement(
    request: Request,
    product_id: int = Form(...),
    movement_type: str = Form(...),
    quantity: float = Form(...),
    mov_date: str = Form(...),
    reason: str = Form(default=""),
    order_id: int = Form(default=0),
    notes: str = Form(default=""),
    db: Session = Depends(get_db),
):
    linked_order_id = order_id or None
    # Валидация типа движения
    if movement_type not in ("in", "out", "adjustment"):
        movement_type = "in"
    # Для прихода/расхода quantity всегда положительный
    # Для корректировки quantity хранится со знаком (+ излишки, - недостача)
    if movement_type in ("in", "out"):
        quantity = abs(quantity)
    if quantity == 0 and movement_type != "adjustment":
        return RedirectResponse(url="/warehouse/", status_code=302)
    try:
        parsed_date = date.fromisoformat(mov_date)
    except (ValueError, TypeError):
        parsed_date = date.today()

    user_id = request.session.get("user_id")

    # Защита от двойной отправки формы (двойной тап на мобильном и т.п.):
    # если точно такое же движение этот же пользователь уже создал за последние
    # несколько секунд — считаем это повторной отправкой и не дублируем запись.
    dup_cutoff = msk_now() - timedelta(seconds=10)
    duplicate = (
        db.query(StockMovement)
        .filter(
            StockMovement.product_id == product_id,
            StockMovement.movement_type == movement_type,
            StockMovement.quantity == quantity,
            StockMovement.reason == reason,
            StockMovement.order_id == linked_order_id,
            StockMovement.created_by_id == user_id,
            StockMovement.created_at >= dup_cutoff,
        )
        .first()
    )
    if duplicate:
        return RedirectResponse(url="/warehouse/", status_code=302)

    mv = StockMovement(
        product_id=product_id,
        movement_type=movement_type,
        quantity=quantity,
        date=parsed_date,
        reason=reason,
        order_id=linked_order_id,
        notes=notes,
        created_by_id=user_id,
    )
    db.add(mv)
    if linked_order_id and movement_type == "out":
        # Отгрузка со склада → заказ передан поставщику
        order = db.query(Order).filter(Order.id == linked_order_id).first()
        if order and order.status not in ("handed", "delivered", "cancelled"):
            order.status = "handed"
    maybe_notify_low_stock(db, product_id)  # добавляет Notification без commit
    db.flush()  # получаем mv.id для записи в журнал действий
    prod = db.query(Product).filter(Product.id == product_id).first()
    log_action(db, "stock_movement", mv.id, "created", user_id,
               f"{MOVEMENT_TYPES.get(movement_type, movement_type)}: {prod.name if prod else product_id} "
               f"— {quantity} {prod.unit if prod else ''}" + (f" ({reason})" if reason else ""))
    db.commit()  # единый коммит — движение + уведомление + запись в журнал атомарно
    return RedirectResponse(url="/warehouse/", status_code=302)


# ── Удаление записи журнала ───────────────────────────────────────────────────

@router.post("/journal/{movement_id}/delete")
@role_required("manager")
async def delete_movement(request: Request, movement_id: int, db: Session = Depends(get_db)):
    mv = db.query(StockMovement).filter(StockMovement.id == movement_id).first()
    if mv:
        prod = mv.product
        log_action(db, "stock_movement", mv.id, "deleted", request.session.get("user_id"),
                   f"Удалено движение: {MOVEMENT_TYPES.get(mv.movement_type, mv.movement_type)} "
                   f"{prod.name if prod else mv.product_id} — {mv.quantity} {prod.unit if prod else ''}")
        db.delete(mv)
        db.commit()
    return RedirectResponse(url="/warehouse/journal", status_code=302)


# ── Редактирование мин. остатка и нач. остатка прямо со страницы склада ──────

@router.post("/product/{product_id}/stock-settings")
@role_required("manager")
async def update_stock_settings(
    request: Request,
    product_id: int,
    min_stock: float = Form(default=0.0),
    initial_stock: float = Form(default=0.0),
    db: Session = Depends(get_db),
):
    p = db.query(Product).filter(Product.id == product_id).first()
    if p:
        old_min, old_init = p.min_stock, p.initial_stock
        p.min_stock = min_stock
        p.initial_stock = initial_stock
        if old_min != min_stock or old_init != initial_stock:
            log_action(db, "product", product_id, "stock_settings_changed",
                       request.session.get("user_id"),
                       f"{p.name}: мин. остаток {old_min}→{min_stock}, нач. остаток {old_init}→{initial_stock}")
        maybe_notify_low_stock(db, product_id)  # добавляет Notification без commit
        db.commit()  # единый коммит
    return RedirectResponse(url="/warehouse/", status_code=302)


# ── Инвентаризация ────────────────────────────────────────────────────────────
# Кладовщик вводит фактические остатки по всем позициям сразу. Система считает
# расхождения и применяет их одним пакетом как StockMovement(adjustment) —
# чтобы _get_balances оставался единым источником истины по остаткам. Сам снимок
# (ожидалось/факт по каждой позиции) сохраняется в StockAdjustment + Line.

@router.get("/inventory", response_class=HTMLResponse)
@login_required
async def inventory_list(request: Request, db: Session = Depends(get_db)):
    sessions = (
        db.query(StockAdjustment)
        .order_by(StockAdjustment.date.desc(), StockAdjustment.id.desc())
        .limit(100).all()
    )
    return templates.TemplateResponse(request, "warehouse/inventory_list.html", {
        "sessions": sessions,
        "assembly_queue_count": _assembly_queue_count(db),
    })


@router.get("/inventory/new", response_class=HTMLResponse)
@role_required("manager")
async def inventory_new(request: Request, db: Session = Depends(get_db)):
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()
    balances = _get_balances(db)
    return templates.TemplateResponse(request, "warehouse/inventory_form.html", {
        "products": products,
        "balances": balances,
        "today": date.today().isoformat(),
        "assembly_queue_count": _assembly_queue_count(db),
    })


@router.post("/inventory")
@role_required("manager")
async def inventory_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        inv_date = date.fromisoformat(str(form.get("inv_date", "")))
    except (ValueError, TypeError):
        inv_date = date.today()
    note = (str(form.get("note", "")) or "").strip() or None

    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()
    balances = _get_balances(db)
    user_id = request.session.get("user_id")

    adj = StockAdjustment(
        date=inv_date,
        reason="Инвентаризация",
        note=note,
        created_by_id=user_id,
    )
    db.add(adj)
    db.flush()  # получить adj.id

    applied = 0
    counted = 0
    for p in products:
        raw = form.get(f"actual_{p.id}")
        if raw is None or str(raw).strip() == "":
            continue  # позицию не пересчитывали — пропускаем
        try:
            actual = float(str(raw).replace(",", "."))
        except (ValueError, TypeError):
            continue
        expected = float(balances.get(p.id, 0) or 0)
        db.add(StockAdjustmentLine(
            adjustment_id=adj.id,
            product_id=p.id,
            expected_qty=expected,
            actual_qty=actual,
        ))
        counted += 1
        diff = round(actual - expected, 3)
        if abs(diff) > 1e-9:
            # Корректировка остатка: quantity хранится со знаком (+ излишек, − недостача)
            db.add(StockMovement(
                product_id=p.id,
                movement_type="adjustment",
                quantity=diff,
                date=inv_date,
                reason="Инвентаризация",
                notes=f"Инвентаризация #{adj.id}: было {expected:g}, стало {actual:g}",
                created_by_id=user_id,
            ))
            maybe_notify_low_stock(db, p.id)
            applied += 1

    if not counted:
        # Ничего не ввели — не плодим пустую инвентаризацию
        db.rollback()
        return RedirectResponse(url="/warehouse/inventory/new", status_code=302)

    log_action(db, "stock_adjustment", adj.id, "created", user_id,
               f"Инвентаризация #{adj.id} от {inv_date.strftime('%d.%m.%Y')}: "
               f"позиций {counted}, корректировок {applied}")
    db.commit()
    return RedirectResponse(url=f"/warehouse/inventory/{adj.id}", status_code=302)


@router.get("/inventory/{adj_id}", response_class=HTMLResponse)
@login_required
async def inventory_view(request: Request, adj_id: int, db: Session = Depends(get_db)):
    adj = db.query(StockAdjustment).filter(StockAdjustment.id == adj_id).first()
    if not adj:
        return RedirectResponse(url="/warehouse/inventory", status_code=302)
    lines = sorted(adj.lines, key=lambda ln: (ln.product.name if ln.product else ""))
    surplus = sum(1 for ln in lines if ln.diff > 1e-9)
    shortage = sum(1 for ln in lines if ln.diff < -1e-9)
    return templates.TemplateResponse(request, "warehouse/inventory_view.html", {
        "adj": adj,
        "lines": lines,
        "surplus": surplus,
        "shortage": shortage,
        "assembly_queue_count": _assembly_queue_count(db),
    })

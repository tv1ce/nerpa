"""Сети заведений — группировка контрагентов одной вывески.

Сетевой клиент по франшизе приходит в базу россыпью: «Кофе Хауз» — это ИП Иванов,
ИП Петров и ООО «Кофе-Юг», каждый со своим договором и счетами. Раздел собирает их
в одну сеть: общий оборот и дебиторка, один ответственный, общие условия.
Документы при этом остаются на конкретном юрлице — сеть их не подменяет.
"""
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.auth import login_required, role_required
from app.database import get_db
from app.models import Claim, Counterparty, Network, Order, User
from app.services.networks import (
    NETWORK_KINDS, apply_network_defaults, network_stats, normalize_brand,
    recalc_categories, suggest_groups,
)
from app.utils import log_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/networks", tags=["networks"])
templates = Jinja2Templates(directory="app/templates")  # подменяется общим env в main.py

CAT_COLORS = {"A": "success", "B": "warning", "C": "light"}


def _clean(v):
    """Пустая строка и мусорные «None»/«null» из формы → None."""
    if v is None:
        return None
    s = str(v).strip()
    return None if not s or s.lower() in ("none", "null") else s


def _num(v, cast=float):
    try:
        return cast(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _get(db: Session, network_id: int) -> Network | None:
    return db.query(Network).filter(Network.id == network_id).first()


# ── Список ───────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
@login_required
async def list_networks(request: Request, q: str = "", db: Session = Depends(get_db)):
    query = db.query(Network).filter(Network.is_active == True)  # noqa: E712
    if q:
        query = query.filter(Network.name.ilike(f"%{q}%"))
    networks = query.order_by(Network.name).all()
    stats = {n.id: network_stats(db, n) for n in networks}
    # Сколько контрагентов ещё не разобрано по сетям — подсказка «есть что объединить»
    suggestions = len(suggest_groups(db))
    return templates.TemplateResponse(request, "networks/list.html", {
        "networks": networks, "stats": stats, "q": q,
        "kinds": NETWORK_KINDS, "cat_colors": CAT_COLORS,
        "suggestions_count": suggestions,
    })


@router.post("/recalc-categories")
@role_required("manager")
async def recalc(request: Request, db: Session = Depends(get_db)):
    recalc_categories(db)
    db.commit()
    return RedirectResponse(url="/networks/", status_code=302)


# ── Автоподбор ───────────────────────────────────────────────────────────────

@router.get("/suggest", response_class=HTMLResponse)
@login_required
async def suggest(request: Request, db: Session = Depends(get_db)):
    """Кандидаты на объединение: контрагенты с совпадающей вывеской."""
    return templates.TemplateResponse(request, "networks/suggest.html", {
        "groups": suggest_groups(db), "kinds": NETWORK_KINDS,
    })


@router.post("/suggest/apply")
@role_required("manager")
async def suggest_apply(
    request: Request,
    title: str = Form(...),
    network_id: str = Form(default=""),
    cp_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Создаёт сеть из группы (или привязывает группу к существующей сети)."""
    if not cp_ids:
        return RedirectResponse(url="/networks/suggest", status_code=302)

    net = _get(db, int(network_id)) if network_id else None
    if net is None:
        net = Network(name=title.strip(), kind="franchise")
        db.add(net)
        db.flush()

    cps = db.query(Counterparty).filter(Counterparty.id.in_(cp_ids)).all()
    for cp in cps:
        cp.network_id = net.id
    db.flush()
    log_action(db, "network", net.id, "attach", request.session.get("user_id"),
               f"Объединено точек: {len(cps)} — {', '.join(c.trade_name or c.name for c in cps)}")
    db.commit()
    return RedirectResponse(url=f"/networks/{net.id}", status_code=302)


# ── Создание / редактирование ────────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
@role_required("manager")
async def new_network(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "networks/form.html", {
        "net": None, "kinds": NETWORK_KINDS, "managers": _managers(db),
    })


def _managers(db: Session):
    return (db.query(User)
            .filter(User.is_active == True, User.role.in_(["admin", "manager"]))  # noqa: E712
            .order_by(User.full_name).all())


def _save_fields(net: Network, name, kind, manager_id, discount, delay_days,
                 delay_type, category, category_manual, notes):
    net.name = name.strip()
    net.kind = kind if kind in NETWORK_KINDS else "franchise"
    net.manager_id = int(manager_id) if manager_id else None
    net.default_discount_pct = _num(discount) or 0.0
    net.payment_delay_days = _num(delay_days, int)
    net.payment_delay_type = delay_type if delay_type in ("banking", "calendar") else "banking"
    net.category = category if category in ("A", "B", "C") else None
    net.category_manual = bool(category_manual)
    net.notes = _clean(notes)


@router.post("/new")
@role_required("manager")
async def create_network(
    request: Request,
    name: str = Form(...),
    kind: str = Form(default="franchise"),
    manager_id: str = Form(default=""),
    default_discount_pct: str = Form(default="0"),
    payment_delay_days: str = Form(default=""),
    payment_delay_type: str = Form(default="banking"),
    category: str = Form(default=""),
    category_manual: bool = Form(default=False),
    notes: str = Form(default=""),
    db: Session = Depends(get_db),
):
    net = Network()
    _save_fields(net, name, kind, manager_id, default_discount_pct, payment_delay_days,
                 payment_delay_type, category, category_manual, notes)
    db.add(net)
    db.flush()
    log_action(db, "network", net.id, "create", request.session.get("user_id"),
               f"Создана сеть «{net.name}»")
    db.commit()
    return RedirectResponse(url=f"/networks/{net.id}", status_code=302)


@router.get("/{network_id}/edit", response_class=HTMLResponse)
@role_required("manager")
async def edit_network(request: Request, network_id: int, db: Session = Depends(get_db)):
    net = _get(db, network_id)
    if not net:
        return RedirectResponse(url="/networks/", status_code=302)
    return templates.TemplateResponse(request, "networks/form.html", {
        "net": net, "kinds": NETWORK_KINDS, "managers": _managers(db),
    })


@router.post("/{network_id}/edit")
@role_required("manager")
async def update_network(
    request: Request,
    network_id: int,
    name: str = Form(...),
    kind: str = Form(default="franchise"),
    manager_id: str = Form(default=""),
    default_discount_pct: str = Form(default="0"),
    payment_delay_days: str = Form(default=""),
    payment_delay_type: str = Form(default="banking"),
    category: str = Form(default=""),
    category_manual: bool = Form(default=False),
    notes: str = Form(default=""),
    apply_to_outlets: bool = Form(default=False),
    db: Session = Depends(get_db),
):
    net = _get(db, network_id)
    if not net:
        return RedirectResponse(url="/networks/", status_code=302)
    _save_fields(net, name, kind, manager_id, default_discount_pct, payment_delay_days,
                 payment_delay_type, category, category_manual, notes)
    if apply_to_outlets:
        # Явное действие пользователя: раскатать условия сети на все точки —
        # здесь перетираем и уже заполненные поля, в отличие от подстановки по умолчанию
        for cp in net.counterparties:
            cp.default_discount_pct = net.default_discount_pct or 0.0
            if net.payment_delay_days is not None:
                cp.payment_delay_days = net.payment_delay_days
                cp.payment_delay_type = net.payment_delay_type
    db.flush()
    log_action(db, "network", net.id, "update", request.session.get("user_id"),
               f"Изменена сеть «{net.name}»"
               + (" (условия раскатаны на точки)" if apply_to_outlets else ""))
    db.commit()
    return RedirectResponse(url=f"/networks/{net.id}", status_code=302)


@router.post("/{network_id}/delete")
@role_required("manager")
async def delete_network(request: Request, network_id: int, db: Session = Depends(get_db)):
    """Расформировывает сеть: точки остаются, теряется только группировка."""
    net = _get(db, network_id)
    if not net:
        return RedirectResponse(url="/networks/", status_code=302)
    for cp in list(net.counterparties):
        cp.network_id = None
    net.is_active = False
    db.flush()
    log_action(db, "network", net.id, "delete", request.session.get("user_id"),
               f"Расформирована сеть «{net.name}»")
    db.commit()
    return RedirectResponse(url="/networks/", status_code=302)


# ── Привязка точек ───────────────────────────────────────────────────────────

@router.post("/{network_id}/attach")
@role_required("manager")
async def attach_counterparty(
    request: Request, network_id: int,
    counterparty_id: int = Form(...),
    outlet_name: str = Form(default=""),
    use_defaults: bool = Form(default=True),
    db: Session = Depends(get_db),
):
    net = _get(db, network_id)
    cp = db.query(Counterparty).filter(Counterparty.id == counterparty_id).first()
    if not net or not cp:
        return RedirectResponse(url="/networks/", status_code=302)
    cp.network_id = net.id
    cp.outlet_name = _clean(outlet_name)
    if use_defaults:
        apply_network_defaults(cp, net)
    db.flush()
    log_action(db, "counterparty", cp.id, "update", request.session.get("user_id"),
               f"Добавлен в сеть «{net.name}»", field="network_id",
               new_value=str(net.id))
    db.commit()
    return RedirectResponse(url=f"/networks/{net.id}", status_code=302)


@router.post("/{network_id}/detach")
@role_required("manager")
async def detach_counterparty(
    request: Request, network_id: int,
    counterparty_id: int = Form(...),
    db: Session = Depends(get_db),
):
    cp = db.query(Counterparty).filter(
        Counterparty.id == counterparty_id, Counterparty.network_id == network_id).first()
    if cp:
        cp.network_id = None
        cp.is_network_hq = False
        db.flush()
        log_action(db, "counterparty", cp.id, "update", request.session.get("user_id"),
                   "Исключён из сети", field="network_id", new_value=None)
        db.commit()
    return RedirectResponse(url=f"/networks/{network_id}", status_code=302)


@router.get("/match", response_class=JSONResponse)
@login_required
async def match_brand(request: Request, q: str = "", exclude_id: int = 0,
                      db: Session = Depends(get_db)):
    """AJAX: похожа ли введённая вывеска на существующую сеть или на другие точки.

    Вызывается из формы контрагента при вводе торгового названия — чтобы франчайзи
    привязывался к сети сразу, а не всплывал потом в «Найти сети»."""
    brand = normalize_brand(q)
    if not brand or len(brand) < 3:
        return JSONResponse({"match": None})

    net = next((n for n in db.query(Network).filter(Network.is_active == True).all()  # noqa: E712
                if normalize_brand(n.name) == brand), None)
    if net:
        return JSONResponse({"match": {
            "kind": "network", "id": net.id, "name": net.name,
            "outlets": len([c for c in net.counterparties if c.is_active]),
        }})

    # Сети ещё нет, но такая же вывеска уже встречается у других контрагентов
    twins = [
        cp for cp in db.query(Counterparty).filter(
            Counterparty.is_active == True,  # noqa: E712
            Counterparty.network_id.is_(None),
            Counterparty.type != "carrier",
        ).all()
        if cp.id != exclude_id and normalize_brand(cp.trade_name or cp.name) == brand
    ]
    if twins:
        return JSONResponse({"match": {
            "kind": "twins", "count": len(twins),
            "names": [cp.name for cp in twins[:5]],
        }})
    return JSONResponse({"match": None})


@router.get("/search-counterparties", response_class=HTMLResponse)
@login_required
async def search_free_counterparties(request: Request, q: str = "",
                                     db: Session = Depends(get_db)):
    """Живой поиск контрагентов без сети — для формы «Добавить точку»."""
    query = db.query(Counterparty).filter(
        Counterparty.is_active == True,  # noqa: E712
        Counterparty.network_id.is_(None),
        Counterparty.type != "carrier",
    )
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Counterparty.name.ilike(like),
                                 Counterparty.trade_name.ilike(like),
                                 Counterparty.inn.ilike(like)))
    rows = query.order_by(Counterparty.name).limit(20).all()
    return templates.TemplateResponse(request, "networks/_cp_options.html",
                                      {"counterparties": rows})


# ── Карточка сети ────────────────────────────────────────────────────────────

@router.get("/{network_id}", response_class=HTMLResponse)
@login_required
async def network_detail(request: Request, network_id: int, db: Session = Depends(get_db)):
    net = _get(db, network_id)
    if not net:
        return RedirectResponse(url="/networks/", status_code=302)
    cp_ids = [cp.id for cp in net.counterparties if cp.is_active]
    orders = (db.query(Order)
              .filter(Order.counterparty_id.in_(cp_ids or [0]))
              .order_by(Order.date.desc(), Order.id.desc()).limit(15).all())
    claims = (db.query(Claim)
              .filter(Claim.counterparty_id.in_(cp_ids or [0]))
              .order_by(Claim.date.desc()).limit(10).all())
    free_cps = (db.query(Counterparty)
                .filter(Counterparty.is_active == True,  # noqa: E712
                        Counterparty.network_id.is_(None),
                        Counterparty.type != "carrier")
                .order_by(Counterparty.name).limit(20).all())
    from app.routers.orders import ORDER_STATUSES
    return templates.TemplateResponse(request, "networks/detail.html", {
        "net": net, "stats": network_stats(db, net), "orders": orders, "claims": claims,
        "kinds": NETWORK_KINDS, "cat_colors": CAT_COLORS, "free_counterparties": free_cps,
        "order_statuses": ORDER_STATUSES,
    })

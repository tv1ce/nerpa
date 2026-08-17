from datetime import date

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import login_required, role_required
from app.models import (CLAIM_SEVERITIES, CLAIM_STATUSES, CLAIM_TYPES,
                        Claim, Counterparty, Order, Product, User)
from app.services import claims as claims_service
from app.services.outlets import address_label, normalize_address
from app.utils import log_action

router = APIRouter(prefix="/claims", tags=["claims"])
templates = Jinja2Templates(directory="app/templates")

STATUS_COLORS = {
    "new":         "info",
    "in_progress": "warning",
    "resolved":    "success",
    "rejected":    "danger",
}


def _int_arg(raw: str | int | None, default: int = 0) -> int:
    """Числовой query-параметр, который спокойно переживает пустую строку.

    Селект «Все контрагенты» в фильтрах отправляет `cp_id=` без значения, и то же
    самое попадает в ссылки плиток и переключателя вида. Со строгой аннотацией
    `int` FastAPI отвечал на это 422 — фильтр и канбан просто не открывались.
    """
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _next_number(db: Session) -> str:
    year = date.today().year
    prefix = f"РЕК-{year}-"
    count = db.query(Claim).filter(Claim.number.like(f"{prefix}%")).count()
    return f"{prefix}{count + 1:04d}"


def _outlet_from_order(db: Session, order_id: int) -> tuple[str | None, str | None]:
    """Точка и исходный адрес по заказу — привязка подставляется сама."""
    if not order_id:
        return None, None
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order or not order.delivery_address:
        return None, None
    return normalize_address(order.delivery_address), order.delivery_address.strip()


@router.get("/", response_class=HTMLResponse)
@login_required
async def list_claims(
    request: Request,
    cp_id: str = "",
    status: str = "",
    ctype: str = "",
    severity: str = "",
    address_key: str = "",
    q: str = "",
    only: str = "",
    view: str = "clients",
    db: Session = Depends(get_db),
):
    """Реестр рекламаций.

    Два представления одного набора: «clients» — контрагент → точка (так ведётся
    разбор), «kanban» — по статусам (так видно поток). Плоской таблицы больше нет:
    у клиента с двадцатью адресами она ничего не объясняла.
    """
    cp_id = _int_arg(cp_id)
    query = db.query(Claim).order_by(Claim.date.desc(), Claim.id.desc())
    if cp_id:
        query = query.filter(Claim.counterparty_id == cp_id)
    if status:
        query = query.filter(Claim.status == status)
    if ctype:
        query = query.filter(Claim.type == ctype)
    if severity:
        query = query.filter(Claim.severity == severity)
    if address_key:
        query = query.filter(Claim.address_key == address_key)
    if only == "open":
        query = query.filter(Claim.status.in_(claims_service.OPEN_STATUSES))
    if q.strip():
        needle = f"%{q.strip().lower()}%"
        query = query.filter(
            Claim.number.ilike(needle)
            | Claim.description.ilike(needle)
            | Claim.delivery_address.ilike(needle)
            | Claim.address_key.ilike(needle)
        )
    claims = query.limit(500).all()

    # Просрочка и «повторы» — расчётные, фильтруем после выборки
    if only == "overdue":
        claims = [c for c in claims if claims_service.is_overdue(c)]
    elif only == "repeat":
        seen = {}
        for c in claims:
            if c.address_key:
                seen[c.address_key] = seen.get(c.address_key, 0) + 1
        claims = [c for c in claims if c.address_key and seen.get(c.address_key, 0) > 1]

    counterparties = (db.query(Counterparty).filter(Counterparty.is_active == True)
                      .order_by(Counterparty.name).all())
    return templates.TemplateResponse(request, "claims/list.html", {
        "claims": claims,
        "clients": claims_service.group_by_client(claims),
        "columns": claims_service.kanban(claims),
        "stats": claims_service.summary(claims),
        "sla": {c.id: claims_service.sla_state(c) for c in claims},
        "counterparties": counterparties,
        "claim_types": CLAIM_TYPES,
        "claim_statuses": CLAIM_STATUSES,
        "claim_severities": CLAIM_SEVERITIES,
        "status_colors": STATUS_COLORS,
        "view": view if view in ("clients", "kanban") else "clients",
        "filter_cp_id": cp_id,
        "filter_status": status,
        "filter_type": ctype,
        "filter_severity": severity,
        "filter_address_key": address_key,
        "filter_address_label": address_label(address_key) if address_key else "",
        "filter_only": only,
        "filter_q": q,
    })


@router.get("/open", response_class=JSONResponse)
@login_required
async def open_claims_json(request: Request, counterparty_id: str = "",
                           delivery_address: str = "", db: Session = Depends(get_db)):
    """Нерешённые рекламации клиента — для напоминания в форме заказа, где
    клиента и адрес выбирают на лету и перерисовать баннер на сервере нечем.

    delivery_address — адрес, который прямо сейчас стоит в форме: по нему
    рекламации делятся на «по этой точке» и «по другим точкам клиента», чтобы
    менеджер видел в первую очередь то, что относится к его заказу.
    """
    from app.utils import open_claims_for_counterparty
    items = open_claims_for_counterparty(db, _int_arg(counterparty_id))
    key = normalize_address(delivery_address) if delivery_address else None

    def _pack(c):
        sla = claims_service.sla_state(c)
        return {
            "id": c.id,
            "number": c.number,
            "status": CLAIM_STATUSES.get(c.status, c.status),
            "status_key": c.status,
            "type": CLAIM_TYPES.get(c.type, c.type),
            "severity": c.severity or "normal",
            "severity_label": CLAIM_SEVERITIES.get(c.severity or "normal", ""),
            "date": c.date.strftime("%d.%m.%Y") if c.date else "",
            "description": c.description or "",
            "order_id": c.order_id,
            "order_number": c.order.number if c.order else "",
            "address_key": c.address_key or "",
            "outlet": claims_service.outlet_label_of(c),
            "age_days": sla["age_days"],
            "overdue": sla["overdue"],
        }

    same = [_pack(c) for c in items if key and c.address_key == key]
    other = [_pack(c) for c in items if not (key and c.address_key == key)]
    return JSONResponse({
        "items": same + other,   # обратная совместимость: плоский список
        "same": same, "other": other,
        "outlet": address_label(key) if key else "",
    })


@router.get("/outlets", response_class=JSONResponse)
@login_required
async def counterparty_outlets_json(request: Request, cp_id: str = "",
                                    db: Session = Depends(get_db)):
    """Точки контрагента для выбора в форме рекламации."""
    outlets = claims_service.counterparty_outlets(db, _int_arg(cp_id))
    return JSONResponse([{
        "address_key": o["address_key"],
        "label": o["label"],
        "address": o["raw_addresses"][0] if o["raw_addresses"] else o["label"],
        "last_date": o["last_date"].strftime("%d.%m.%Y") if o["last_date"] else "",
        "orders_count": len(o["orders"]),
        "claims_total": o["claims_total"],
        "claims_open": o["claims_open"],
        "orders": [{
            "id": ordr.id,
            "number": ordr.number,
            "date": ordr.date.strftime("%d.%m.%Y") if ordr.date else "",
        } for ordr in o["orders"][:20]],
    } for o in outlets])


@router.get("/new", response_class=HTMLResponse)
@login_required
async def new_claim(request: Request, cp_id: str = "", order_id: str = "",
                    address_key: str = "", db: Session = Depends(get_db)):
    """Форма рекламации. Может быть вызвана из карточки заказа (order_id) или
    точки (address_key) — тогда привязка уже заполнена и менять её не нужно."""
    cp_id, order_id = _int_arg(cp_id), _int_arg(order_id)
    counterparties = (db.query(Counterparty).filter(Counterparty.is_active == True)
                      .order_by(Counterparty.name).all())
    preselect_order = db.query(Order).filter(Order.id == order_id).first() if order_id else None
    if preselect_order and not cp_id:
        cp_id = preselect_order.counterparty_id
    if preselect_order and not address_key:
        address_key = normalize_address(preselect_order.delivery_address) or ""

    outlets = claims_service.counterparty_outlets(db, cp_id) if cp_id else []
    preselect_cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first() if cp_id else None
    products = (db.query(Product).filter(Product.is_active == True)
                .order_by(Product.name).all())
    users = (db.query(User).filter(User.is_active == True)
             .order_by(User.full_name).all())
    return templates.TemplateResponse(request, "claims/form.html", {
        "claim": None,
        "counterparties": counterparties,
        "outlets": outlets,
        "products": products,
        "users": users,
        "preselect_cp": preselect_cp,
        "preselect_order": preselect_order,
        "preselect_address_key": address_key,
        "claim_types": CLAIM_TYPES,
        "claim_statuses": CLAIM_STATUSES,
        "claim_severities": CLAIM_SEVERITIES,
        "today": date.today().isoformat(),
    })


@router.post("/new")
@role_required("manager")
async def create_claim(
    request: Request,
    claim_date: str = Form(...),
    counterparty_id: int = Form(...),
    order_id: int = Form(default=0),
    address_key: str = Form(default=""),
    claim_type: str = Form(default="quality"),
    severity: str = Form(default="normal"),
    product_id: int = Form(default=0),
    quantity: str = Form(default=""),
    assignee_id: int = Form(default=0),
    description: str = Form(default=""),
    amount: str = Form(default=""),
    db: Session = Depends(get_db),
):
    def _num(raw: str):
        try:
            return float(raw.replace(",", ".")) if raw.strip() else None
        except ValueError:
            return None

    # Точка: из заказа она достовернее (адрес именно той поставки), поэтому
    # заказ имеет приоритет над выбранной вручную точкой.
    key, raw_address = _outlet_from_order(db, order_id)
    if not key and address_key.strip():
        key = address_key.strip()
        raw_address = next(
            (o["raw_addresses"][0] for o in claims_service.counterparty_outlets(db, counterparty_id)
             if o["address_key"] == key and o["raw_addresses"]), None)

    claim = Claim(
        number=_next_number(db),
        date=date.fromisoformat(claim_date),
        counterparty_id=counterparty_id,
        order_id=order_id or None,
        address_key=key,
        delivery_address=raw_address,
        type=claim_type,
        severity=severity if severity in CLAIM_SEVERITIES else "normal",
        product_id=product_id or None,
        quantity=_num(quantity),
        assignee_id=assignee_id or None,
        description=description,
        amount=_num(amount),
        status="new",
        created_by_id=request.session.get("user_id"),
    )
    db.add(claim)
    db.commit()
    log_action(db, "claim", claim.id, "created", request.session.get("user_id"),
               f"Рекламация {claim.number} заведена"
               + (f" · точка {address_label(key)}" if key else " · без точки"))
    db.commit()
    return RedirectResponse(url=f"/claims/{claim.id}", status_code=302)


@router.get("/{claim_id}", response_class=HTMLResponse)
@login_required
async def view_claim(request: Request, claim_id: int, db: Session = Depends(get_db)):
    claim = db.query(Claim).filter(Claim.id == claim_id).first()
    if not claim:
        return RedirectResponse(url="/claims/", status_code=302)

    history = claims_service.outlet_history(
        db, claim.address_key, exclude_id=claim.id, counterparty_id=claim.counterparty_id)
    outlets = claims_service.counterparty_outlets(db, claim.counterparty_id)
    outlet = next((o for o in outlets if o["address_key"] == claim.address_key), None)
    users = (db.query(User).filter(User.is_active == True)
             .order_by(User.full_name).all())
    return templates.TemplateResponse(request, "claims/detail.html", {
        "claim": claim,
        "sla": claims_service.sla_state(claim),
        "timeline": claims_service.timeline(db, claim),
        "history": history,
        "history_open": [c for c in history if c.status in claims_service.OPEN_STATUSES],
        "outlet": outlet,
        "outlets": outlets,
        "users": users,
        "claim_types": CLAIM_TYPES,
        "claim_statuses": CLAIM_STATUSES,
        "claim_severities": CLAIM_SEVERITIES,
        "status_colors": STATUS_COLORS,
    })


@router.post("/{claim_id}/status")
@role_required("manager")
async def update_status(
    request: Request,
    claim_id: int,
    status: str = Form(...),
    resolution: str = Form(default=""),
    db: Session = Depends(get_db),
):
    claim = db.query(Claim).filter(Claim.id == claim_id).first()
    if claim and status in CLAIM_STATUSES and status != claim.status:
        old_status = claim.status
        claims_service.mark_status(claim, status)
        if resolution.strip():
            claim.resolution = resolution.strip()
        log_action(db, "claim", claim_id, "status_changed",
                   request.session.get("user_id"),
                   f"Статус: {CLAIM_STATUSES.get(old_status)} → {CLAIM_STATUSES.get(status)}"
                   + (f". {resolution.strip()}" if resolution.strip() else ""),
                   field="status", old_value=old_status, new_value=status)
        db.commit()
    elif claim and resolution.strip() and resolution.strip() != (claim.resolution or ""):
        claim.resolution = resolution.strip()
        db.commit()
    return RedirectResponse(url=f"/claims/{claim_id}", status_code=302)


@router.post("/{claim_id}/update")
@role_required("manager")
async def update_claim(
    request: Request,
    claim_id: int,
    address_key: str = Form(default=""),
    severity: str = Form(default=""),
    assignee_id: int = Form(default=-1),
    amount: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Правка привязки и параметров разбора.

    Отдельная точка входа нужна прежде всего для старых рекламаций: у них точки
    нет (заводились до привязки к адресам), и её надо иметь возможность указать
    руками, не пересоздавая рекламацию.
    """
    claim = db.query(Claim).filter(Claim.id == claim_id).first()
    if not claim:
        return RedirectResponse(url="/claims/", status_code=302)

    if address_key.strip() and address_key.strip() != (claim.address_key or ""):
        key = address_key.strip()
        old = claim.address_key
        claim.address_key = key
        claim.delivery_address = next(
            (o["raw_addresses"][0] for o in claims_service.counterparty_outlets(db, claim.counterparty_id)
             if o["address_key"] == key and o["raw_addresses"]), claim.delivery_address)
        log_action(db, "claim", claim_id, "updated", request.session.get("user_id"),
                   f"Точка: {address_label(old) if old else '—'} → {address_label(key)}",
                   field="address_key", old_value=old, new_value=key)
    if severity and severity in CLAIM_SEVERITIES and severity != claim.severity:
        old = claim.severity
        claim.severity = severity
        log_action(db, "claim", claim_id, "updated", request.session.get("user_id"),
                   f"Критичность: {CLAIM_SEVERITIES.get(old, old)} → {CLAIM_SEVERITIES[severity]}",
                   field="severity", old_value=old, new_value=severity)
    if assignee_id >= 0 and (assignee_id or None) != claim.assignee_id:
        claim.assignee_id = assignee_id or None
        who = db.query(User).filter(User.id == assignee_id).first() if assignee_id else None
        log_action(db, "claim", claim_id, "updated", request.session.get("user_id"),
                   f"Ответственный: {who.full_name if who else 'снят'}",
                   field="assignee_id", new_value=str(assignee_id or ""))
    if amount.strip():
        try:
            claim.amount = float(amount.replace(",", "."))
        except ValueError:
            pass
    db.commit()
    return RedirectResponse(url=f"/claims/{claim_id}", status_code=302)


@router.post("/{claim_id}/delete")
@role_required("admin")
async def delete_claim(request: Request, claim_id: int, db: Session = Depends(get_db)):
    claim = db.query(Claim).filter(Claim.id == claim_id).first()
    if claim:
        log_action(db, "claim", claim_id, "deleted",
                   request.session.get("user_id"),
                   f"Рекламация {claim.number} удалена")
        db.delete(claim)
        db.commit()
    return RedirectResponse(url="/claims/", status_code=302)


@router.get("/by-order/{order_id}")
@login_required
async def orders_by_cp(request: Request, order_id: int, cp_id: str = "", db: Session = Depends(get_db)):
    """AJAX: вернуть заказы контрагента для динамического select."""
    orders = (db.query(Order).filter(Order.counterparty_id == _int_arg(cp_id))
              .order_by(Order.date.desc()).limit(50).all())
    return JSONResponse([{"id": o.id, "number": o.number, "date": str(o.date)} for o in orders])

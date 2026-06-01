from datetime import date
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import login_required, role_required
from app.models import Claim, Counterparty, Order
from app.utils import log_action

router = APIRouter(prefix="/claims", tags=["claims"])
templates = Jinja2Templates(directory="app/templates")

CLAIM_TYPES = {
    "quality":   "Качество",
    "delivery":  "Доставка",
    "quantity":  "Количество",
    "documents": "Документы",
    "other":     "Прочее",
}

CLAIM_STATUSES = {
    "new":         "Новая",
    "in_progress": "В работе",
    "resolved":    "Решена",
    "rejected":    "Отклонена",
}

STATUS_COLORS = {
    "new":         "info",
    "in_progress": "warning",
    "resolved":    "success",
    "rejected":    "danger",
}


def _next_number(db: Session) -> str:
    year = date.today().year
    prefix = f"РЕК-{year}-"
    count = db.query(Claim).filter(Claim.number.like(f"{prefix}%")).count()
    return f"{prefix}{count + 1:04d}"


@router.get("/", response_class=HTMLResponse)
@login_required
async def list_claims(
    request: Request,
    cp_id: int = 0,
    status: str = "",
    ctype: str = "",
    db: Session = Depends(get_db),
):
    q = db.query(Claim).order_by(Claim.date.desc(), Claim.id.desc())
    if cp_id:
        q = q.filter(Claim.counterparty_id == cp_id)
    if status:
        q = q.filter(Claim.status == status)
    if ctype:
        q = q.filter(Claim.type == ctype)
    claims = q.limit(200).all()
    counterparties = db.query(Counterparty).filter(Counterparty.is_active == True).order_by(Counterparty.name).all()
    return templates.TemplateResponse(request, "claims/list.html", {
        "claims": claims,
        "counterparties": counterparties,
        "claim_types": CLAIM_TYPES,
        "claim_statuses": CLAIM_STATUSES,
        "status_colors": STATUS_COLORS,
        "filter_cp_id": cp_id,
        "filter_status": status,
        "filter_type": ctype,
    })


@router.get("/new", response_class=HTMLResponse)
@login_required
async def new_claim(request: Request, cp_id: int = 0, db: Session = Depends(get_db)):
    counterparties = db.query(Counterparty).filter(Counterparty.is_active == True).order_by(Counterparty.name).all()
    orders = []
    if cp_id:
        orders = db.query(Order).filter(Order.counterparty_id == cp_id).order_by(Order.date.desc()).limit(50).all()
    preselect_cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first() if cp_id else None
    return templates.TemplateResponse(request, "claims/form.html", {
        "claim": None,
        "counterparties": counterparties,
        "orders": orders,
        "preselect_cp": preselect_cp,
        "claim_types": CLAIM_TYPES,
        "claim_statuses": CLAIM_STATUSES,
        "today": date.today().isoformat(),
    })


@router.post("/new")
@role_required("manager")
async def create_claim(
    request: Request,
    claim_date: str = Form(...),
    counterparty_id: int = Form(...),
    order_id: int = Form(default=0),
    claim_type: str = Form(default="quality"),
    description: str = Form(default=""),
    amount: str = Form(default=""),
    db: Session = Depends(get_db),
):
    try:
        amt = float(amount) if amount.strip() else None
    except ValueError:
        amt = None
    claim = Claim(
        number=_next_number(db),
        date=date.fromisoformat(claim_date),
        counterparty_id=counterparty_id,
        order_id=order_id or None,
        type=claim_type,
        description=description,
        amount=amt,
        status="new",
        created_by_id=request.session.get("user_id"),
    )
    db.add(claim)
    db.commit()
    return RedirectResponse(url=f"/claims/{claim.id}", status_code=302)


@router.get("/{claim_id}", response_class=HTMLResponse)
@login_required
async def view_claim(request: Request, claim_id: int, db: Session = Depends(get_db)):
    claim = db.query(Claim).filter(Claim.id == claim_id).first()
    if not claim:
        return RedirectResponse(url="/claims/", status_code=302)
    return templates.TemplateResponse(request, "claims/detail.html", {
        "claim": claim,
        "claim_types": CLAIM_TYPES,
        "claim_statuses": CLAIM_STATUSES,
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
    if claim and status in CLAIM_STATUSES:
        old_status = claim.status
        claim.status = status
        if resolution.strip():
            claim.resolution = resolution.strip()
        log_action(db, "claim", claim_id, "status_changed",
                   request.session.get("user_id"),
                   f"Статус: {CLAIM_STATUSES.get(old_status)} → {CLAIM_STATUSES.get(status)}",
                   field="status", old_value=old_status, new_value=status)
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
async def orders_by_cp(request: Request, order_id: int, cp_id: int = 0, db: Session = Depends(get_db)):
    """AJAX: вернуть заказы контрагента для динамического select."""
    from fastapi.responses import JSONResponse
    orders = db.query(Order).filter(Order.counterparty_id == cp_id).order_by(Order.date.desc()).limit(50).all()
    return JSONResponse([{"id": o.id, "number": o.number, "date": str(o.date)} for o in orders])

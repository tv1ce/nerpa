from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import httpx
from app.database import get_db
from app.auth import login_required, role_required
from app.models import Counterparty, Claim, Task, Comment, AuditLog, User
from app.utils import log_action

router = APIRouter(prefix="/counterparties", tags=["counterparties"])
templates = Jinja2Templates(directory="app/templates")

DADATA_TOKEN = "f8e4e9a1543e8d8f79091bab8d1422a83ee2d573"
DADATA_HEADERS = {
    "Authorization": f"Token {DADATA_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

CP_TYPES = {"client": "Покупатель", "supplier": "Поставщик", "both": "Покупатель и поставщик"}
ENTITY_TYPES = {"ooo": "ООО", "ip": "ИП", "other": "Прочее"}

CAT_COLORS = {"A": "success", "B": "primary", "C": "warning"}

CLAIM_TYPES = {
    "quality": "Качество", "delivery": "Доставка", "quantity": "Количество",
    "documents": "Документы", "other": "Прочее",
}
CLAIM_STATUSES = {
    "new": "Новая", "in_progress": "В работе",
    "resolved": "Решена", "rejected": "Отклонена",
}
STATUS_COLORS = {
    "new": "info", "in_progress": "warning", "resolved": "success", "rejected": "danger",
}


def _compute_category(cp: Counterparty) -> str | None:
    revenue = sum(o.total_amount for o in cp.orders if o.status not in ("cancelled", "draft"))
    if revenue >= 1_000_000:
        return "A"
    elif revenue >= 200_000:
        return "B"
    elif revenue > 0:
        return "C"
    return None


@router.get("/", response_class=HTMLResponse)
@login_required
async def list_counterparties(
    request: Request, q: str = "", type: str = "", category: str = "",
    entity_type: str = "",
    db: Session = Depends(get_db),
):
    query = db.query(Counterparty).filter(Counterparty.is_active == True)
    if q:
        query = query.filter(Counterparty.name.ilike(f"%{q}%"))
    if type:
        query = query.filter(Counterparty.type == type)
    if category:
        query = query.filter(Counterparty.category == category)
    if entity_type:
        query = query.filter(Counterparty.entity_type == entity_type)
    counterparties = query.order_by(Counterparty.name).all()
    return templates.TemplateResponse(request, "counterparties/list.html", {
        "counterparties": counterparties, "q": q, "type": type,
        "category": category, "entity_type": entity_type,
        "cp_types": CP_TYPES, "cat_colors": CAT_COLORS, "entity_types": ENTITY_TYPES,
    })


@router.post("/recalc-categories")
@login_required
async def recalc_categories(request: Request, db: Session = Depends(get_db)):
    cps = db.query(Counterparty).filter(
        Counterparty.is_active == True,
        Counterparty.category_manual == False,
    ).all()
    for cp in cps:
        cp.category = _compute_category(cp)
    db.commit()
    return RedirectResponse(url="/counterparties", status_code=302)


@router.get("/new", response_class=HTMLResponse)
@login_required
async def new_counterparty(request: Request):
    return templates.TemplateResponse(request, "counterparties/form.html", {
        "cp": None, "cp_types": CP_TYPES, "entity_types": ENTITY_TYPES, "errors": [],
    })


@router.post("/new")
@login_required
async def create_counterparty(
    request: Request,
    name: str = Form(...),
    trade_name: str = Form(default=""),
    short_name: str = Form(default=""),
    inn: str = Form(default=""),
    kpp: str = Form(default=""),
    ogrn: str = Form(default=""),
    legal_address: str = Form(default=""),
    actual_address: str = Form(default=""),
    phone: str = Form(default=""),
    email: str = Form(default=""),
    contact_person: str = Form(default=""),
    type: str = Form(default="client"),
    entity_type: str = Form(default="ooo"),
    bank_name: str = Form(default=""),
    bank_account: str = Form(default=""),
    bank_bik: str = Form(default=""),
    bank_corr_account: str = Form(default=""),
    notes: str = Form(default=""),
    payment_delay_days: int = Form(default=2),
    payment_delay_type: str = Form(default="banking"),
    db: Session = Depends(get_db),
):
    cp = Counterparty(
        name=name, trade_name=trade_name or None, short_name=short_name,
        inn=inn, kpp=kpp, ogrn=ogrn,
        legal_address=legal_address, actual_address=actual_address,
        phone=phone, email=email, contact_person=contact_person, type=type,
        entity_type=entity_type if entity_type in ENTITY_TYPES else "ooo",
        bank_name=bank_name, bank_account=bank_account, bank_bik=bank_bik,
        bank_corr_account=bank_corr_account, notes=notes,
        payment_delay_days=max(0, payment_delay_days),
        payment_delay_type=payment_delay_type if payment_delay_type in ("banking", "calendar") else "banking",
    )
    db.add(cp)
    db.commit()
    return RedirectResponse(url="/counterparties", status_code=302)


@router.get("/dadata/party", response_class=JSONResponse)
@login_required
async def dadata_party(request: Request, inn: str = ""):
    if not inn or len(inn) < 10:
        return JSONResponse({"error": "Введите ИНН (10 или 12 цифр)"}, status_code=400)
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        try:
            resp = await client.post(
                "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party",
                headers=DADATA_HEADERS,
                json={"query": inn.strip()},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            return JSONResponse({"error": f"Ошибка запроса к DaData: {e}"}, status_code=502)

    suggestions = data.get("suggestions", [])
    if not suggestions:
        return JSONResponse({"error": "Компания не найдена по ИНН"}, status_code=404)

    s = suggestions[0]["data"]
    name_block = s.get("name", {})
    addr = s.get("address", {}) or {}
    mgmt = s.get("management", {}) or {}

    result = {
        "name": name_block.get("full_with_opf", ""),
        "short_name": name_block.get("short_with_opf", ""),
        "inn": s.get("inn", ""),
        "kpp": s.get("kpp", ""),
        "ogrn": s.get("ogrn", ""),
        "legal_address": addr.get("value", ""),
        "director": mgmt.get("name", ""),
        "director_post": mgmt.get("post", ""),
        "okpo": s.get("okpo", ""),
        "okved": s.get("okved", ""),
    }
    return JSONResponse(result)


@router.get("/dadata/bank", response_class=JSONResponse)
@login_required
async def dadata_bank(request: Request, bik: str = ""):
    if not bik or len(bik) != 9:
        return JSONResponse({"error": "Введите БИК (9 цифр)"}, status_code=400)
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        try:
            resp = await client.post(
                "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/bank",
                headers=DADATA_HEADERS,
                json={"query": bik.strip()},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            return JSONResponse({"error": f"Ошибка запроса к DaData: {e}"}, status_code=502)

    suggestions = data.get("suggestions", [])
    if not suggestions:
        return JSONResponse({"error": "Банк не найден по БИК"}, status_code=404)

    s = suggestions[0]["data"]
    result = {
        "bank_name": suggestions[0].get("value", ""),
        "bank_bik": s.get("bic", ""),
        "bank_corr_account": s.get("correspondent_account", ""),
    }
    return JSONResponse(result)


@router.get("/{cp_id}", response_class=HTMLResponse)
@login_required
async def view_counterparty(request: Request, cp_id: int, db: Session = Depends(get_db)):
    cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first()
    if not cp:
        return RedirectResponse(url="/counterparties", status_code=302)

    # Статистика
    active_orders = [o for o in cp.orders if o.status not in ("cancelled",)]
    total_revenue = sum(o.total_amount for o in cp.orders if o.status not in ("cancelled", "draft"))
    open_invoices = [inv for inv in cp.invoices if inv.status in ("issued", "overdue")]
    open_debt = sum(inv.total_amount for inv in open_invoices)
    claims = db.query(Claim).filter(Claim.counterparty_id == cp_id).order_by(Claim.date.desc()).all()

    tasks = db.query(Task).filter(
        Task.entity_type == "counterparty", Task.entity_id == cp_id
    ).order_by(Task.status, Task.created_at).all()
    comments = db.query(Comment).filter(
        Comment.entity_type == "counterparty", Comment.entity_id == cp_id
    ).order_by(Comment.created_at).all()
    activity = db.query(AuditLog).filter(
        AuditLog.entity_type == "counterparty", AuditLog.entity_id == cp_id
    ).order_by(AuditLog.created_at.desc()).limit(50).all()
    users = db.query(User).filter(User.is_active == True).order_by(User.full_name).all()

    return templates.TemplateResponse(request, "counterparties/detail.html", {
        "cp": cp,
        "cp_types": CP_TYPES,
        "entity_types": ENTITY_TYPES,
        "cat_colors": CAT_COLORS,
        "total_orders": len(cp.orders),
        "total_revenue": total_revenue,
        "open_debt": open_debt,
        "open_invoices": open_invoices,
        "claims": claims,
        "claim_types": CLAIM_TYPES,
        "claim_statuses": CLAIM_STATUSES,
        "status_colors": STATUS_COLORS,
        "tasks": tasks,
        "comments": comments,
        "activity": activity,
        "users": users,
        "priority_colors": {"low": "secondary", "normal": "primary", "high": "warning", "urgent": "danger"},
    })


@router.post("/{cp_id}/set-category")
@login_required
async def set_category(
    request: Request, cp_id: int,
    category: str = Form(...),
    db: Session = Depends(get_db),
):
    cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first()
    if cp:
        old_cat = cp.category
        if category in ("A", "B", "C"):
            cp.category = category
            cp.category_manual = True
        elif category == "auto":
            cp.category_manual = False
            cp.category = _compute_category(cp)
        if cp.category != old_cat:
            log_action(db, "counterparty", cp_id, "category_set",
                       request.session.get("user_id"),
                       f"Категория: {old_cat or '—'} → {cp.category or '—'}",
                       field="category", old_value=old_cat, new_value=cp.category)
        db.commit()
    return RedirectResponse(url=f"/counterparties/{cp_id}", status_code=302)


@router.get("/{cp_id}/edit", response_class=HTMLResponse)
@login_required
async def edit_counterparty(request: Request, cp_id: int, db: Session = Depends(get_db)):
    cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first()
    if not cp:
        return RedirectResponse(url="/counterparties", status_code=302)
    return templates.TemplateResponse(request, "counterparties/form.html", {
        "cp": cp, "cp_types": CP_TYPES, "entity_types": ENTITY_TYPES, "errors": [],
    })


@router.post("/{cp_id}/edit")
@login_required
async def update_counterparty(
    request: Request, cp_id: int,
    name: str = Form(...),
    trade_name: str = Form(default=""),
    short_name: str = Form(default=""),
    inn: str = Form(default=""),
    kpp: str = Form(default=""),
    ogrn: str = Form(default=""),
    legal_address: str = Form(default=""),
    actual_address: str = Form(default=""),
    phone: str = Form(default=""),
    email: str = Form(default=""),
    contact_person: str = Form(default=""),
    type: str = Form(default="client"),
    entity_type: str = Form(default="ooo"),
    bank_name: str = Form(default=""),
    bank_account: str = Form(default=""),
    bank_bik: str = Form(default=""),
    bank_corr_account: str = Form(default=""),
    notes: str = Form(default=""),
    payment_delay_days: int = Form(default=2),
    payment_delay_type: str = Form(default="banking"),
    db: Session = Depends(get_db),
):
    cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first()
    if cp:
        cp.name = name; cp.trade_name = trade_name or None; cp.short_name = short_name
        cp.inn = inn; cp.kpp = kpp
        cp.ogrn = ogrn; cp.legal_address = legal_address; cp.actual_address = actual_address
        cp.phone = phone; cp.email = email; cp.contact_person = contact_person; cp.type = type
        cp.entity_type = entity_type if entity_type in ENTITY_TYPES else "ooo"
        cp.bank_name = bank_name; cp.bank_account = bank_account; cp.bank_bik = bank_bik
        cp.bank_corr_account = bank_corr_account; cp.notes = notes
        cp.payment_delay_days = max(0, payment_delay_days)
        cp.payment_delay_type = payment_delay_type if payment_delay_type in ("banking", "calendar") else "banking"
        db.commit()
    return RedirectResponse(url=f"/counterparties/{cp_id}", status_code=302)


@router.post("/{cp_id}/delete")
@login_required
async def delete_counterparty(request: Request, cp_id: int, db: Session = Depends(get_db)):
    cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first()
    if cp:
        cp.is_active = False
        db.commit()
    return RedirectResponse(url="/counterparties", status_code=302)

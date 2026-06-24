from datetime import date
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.auth import login_required, role_required
from app.models import (
    ProcurementCategory, Vendor, SourcingRequest, VendorQuote, Counterparty,
)
from app.utils import log_action

router = APIRouter(prefix="/sourcing", tags=["sourcing"])
templates = Jinja2Templates(directory="app/templates")

VENDOR_STATUSES = {
    "new":         "Новый",
    "in_progress": "В работе",
    "approved":    "Одобрен",
    "rejected":    "Отклонён",
}

VENDOR_STATUS_COLORS = {
    "new":         "info",
    "in_progress": "warning",
    "approved":    "success",
    "rejected":    "danger",
}

REQUEST_STATUSES = {
    "open":    "Открыт",
    "decided": "Выбран поставщик",
    "closed":  "Закрыт",
}

REQUEST_STATUS_COLORS = {
    "open":    "primary",
    "decided": "success",
    "closed":  "secondary",
}


def _next_request_number(db: Session) -> str:
    year = date.today().year
    prefix = f"ЗАК-{year}-"
    count = db.query(SourcingRequest).filter(SourcingRequest.number.like(f"{prefix}%")).count()
    return f"{prefix}{count + 1:04d}"


def _recalc_scores(db: Session, request_id: int) -> None:
    """Пересчитывает взвешенный балл всех предложений запроса.

    Критерии нормализуются ВНУТРИ запроса относительно других предложений:
      - цена и срок инвертируются (меньше = лучше), качество прямое (1..5).
    Итоговый балл — взвешенная сумма нормированных оценок в шкале 0..10.
    """
    req = db.query(SourcingRequest).filter(SourcingRequest.id == request_id).first()
    if not req:
        return
    quotes = db.query(VendorQuote).filter(VendorQuote.request_id == request_id).all()
    if not quotes:
        return

    wp = req.weight_price or 0.0
    wt = req.weight_term or 0.0
    wq = req.weight_quality or 0.0
    wsum = wp + wt + wq
    if wsum <= 0:
        wp = wt = wq = 1.0
        wsum = 3.0

    prices = [q.price for q in quotes if q.price and q.price > 0]
    terms = [q.term_days for q in quotes if q.term_days and q.term_days > 0]
    p_min, p_max = (min(prices), max(prices)) if prices else (0, 0)
    t_min, t_max = (min(terms), max(terms)) if terms else (0, 0)

    def _inv_norm(val, vmin, vmax):
        # 1.0 = лучший (минимальный), 0.0 = худший (максимальный)
        if not val or val <= 0:
            return 0.0
        if vmax == vmin:
            return 1.0
        return (vmax - val) / (vmax - vmin)

    for q in quotes:
        n_price = _inv_norm(q.price, p_min, p_max)
        n_term = _inv_norm(q.term_days, t_min, t_max)
        n_quality = (q.quality or 0) / 5.0
        q.score = round((wp * n_price + wt * n_term + wq * n_quality) / wsum * 10, 2)


def _recalc_vendor_rating(db: Session, vendor_id: int) -> None:
    """Кэширует среднее качество поставщика по всем его предложениям."""
    avg = db.query(func.avg(VendorQuote.quality)).filter(
        VendorQuote.vendor_id == vendor_id,
        VendorQuote.quality.isnot(None),
    ).scalar()
    v = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if v:
        v.rating_avg = round(avg, 2) if avg else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Дашборд: список запросов
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
@login_required
async def index(request: Request, status: str = "", cat_id: int = 0,
                db: Session = Depends(get_db)):
    q = db.query(SourcingRequest).order_by(SourcingRequest.id.desc())
    if status:
        q = q.filter(SourcingRequest.status == status)
    if cat_id:
        q = q.filter(SourcingRequest.category_id == cat_id)
    requests = q.limit(200).all()
    # количество предложений по каждому запросу
    counts = dict(
        db.query(VendorQuote.request_id, func.count(VendorQuote.id))
          .group_by(VendorQuote.request_id).all()
    )
    categories = db.query(ProcurementCategory).filter(
        ProcurementCategory.is_active == True).order_by(ProcurementCategory.name).all()
    return templates.TemplateResponse(request, "sourcing/index.html", {
        "requests": requests,
        "quote_counts": counts,
        "categories": categories,
        "request_statuses": REQUEST_STATUSES,
        "request_status_colors": REQUEST_STATUS_COLORS,
        "filter_status": status,
        "filter_cat_id": cat_id,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Запросы на сравнение
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/requests/new", response_class=HTMLResponse)
@login_required
async def new_request(request: Request, db: Session = Depends(get_db)):
    categories = db.query(ProcurementCategory).filter(
        ProcurementCategory.is_active == True).order_by(ProcurementCategory.name).all()
    return templates.TemplateResponse(request, "sourcing/request_form.html", {
        "req": None,
        "categories": categories,
    })


@router.post("/requests/new")
@role_required("manager")
async def create_request(
    request: Request,
    title: str = Form(...),
    category_id: int = Form(default=0),
    description: str = Form(default=""),
    db: Session = Depends(get_db),
):
    req = SourcingRequest(
        number=_next_request_number(db),
        title=title.strip(),
        category_id=category_id or None,
        description=description.strip() or None,
        status="open",
        created_by_id=request.session.get("user_id"),
    )
    db.add(req)
    db.commit()
    log_action(db, "sourcing_request", req.id, "created",
               request.session.get("user_id"), f"Запрос {req.number}: {req.title}")
    db.commit()
    return RedirectResponse(url=f"/sourcing/requests/{req.id}", status_code=302)


@router.get("/requests/{req_id}", response_class=HTMLResponse)
@login_required
async def view_request(request: Request, req_id: int, db: Session = Depends(get_db)):
    req = db.query(SourcingRequest).filter(SourcingRequest.id == req_id).first()
    if not req:
        return RedirectResponse(url="/sourcing/", status_code=302)
    quotes = db.query(VendorQuote).filter(VendorQuote.request_id == req_id).all()
    quotes.sort(key=lambda x: (x.score or 0), reverse=True)
    best_id = quotes[0].id if quotes and (quotes[0].score or 0) > 0 else None
    # поставщики, которых ещё нет в этом запросе — для выпадашки добавления
    used_vendor_ids = {q.vendor_id for q in quotes}
    vendors = db.query(Vendor).filter(Vendor.is_active == True).order_by(Vendor.name).all()
    available_vendors = [v for v in vendors if v.id not in used_vendor_ids]
    return templates.TemplateResponse(request, "sourcing/request_detail.html", {
        "req": req,
        "quotes": quotes,
        "best_id": best_id,
        "available_vendors": available_vendors,
        "request_statuses": REQUEST_STATUSES,
        "request_status_colors": REQUEST_STATUS_COLORS,
        "vendor_status_colors": VENDOR_STATUS_COLORS,
    })


@router.post("/requests/{req_id}/weights")
@role_required("manager")
async def update_weights(
    request: Request, req_id: int,
    weight_price: float = Form(0.5),
    weight_term: float = Form(0.25),
    weight_quality: float = Form(0.25),
    db: Session = Depends(get_db),
):
    req = db.query(SourcingRequest).filter(SourcingRequest.id == req_id).first()
    if req:
        req.weight_price = max(0.0, weight_price)
        req.weight_term = max(0.0, weight_term)
        req.weight_quality = max(0.0, weight_quality)
        _recalc_scores(db, req_id)
        db.commit()
    return RedirectResponse(url=f"/sourcing/requests/{req_id}", status_code=302)


@router.post("/requests/{req_id}/decide")
@role_required("manager")
async def decide_request(request: Request, req_id: int,
                         vendor_id: int = Form(...), db: Session = Depends(get_db)):
    req = db.query(SourcingRequest).filter(SourcingRequest.id == req_id).first()
    if req:
        req.decided_vendor_id = vendor_id
        req.status = "decided"
        v = db.query(Vendor).filter(Vendor.id == vendor_id).first()
        log_action(db, "sourcing_request", req_id, "decided",
                   request.session.get("user_id"),
                   f"Выбран поставщик: {v.name if v else vendor_id}")
        db.commit()
    return RedirectResponse(url=f"/sourcing/requests/{req_id}", status_code=302)


@router.post("/requests/{req_id}/reopen")
@role_required("manager")
async def reopen_request(request: Request, req_id: int, db: Session = Depends(get_db)):
    req = db.query(SourcingRequest).filter(SourcingRequest.id == req_id).first()
    if req:
        req.decided_vendor_id = None
        req.status = "open"
        db.commit()
    return RedirectResponse(url=f"/sourcing/requests/{req_id}", status_code=302)


@router.post("/requests/{req_id}/delete")
@role_required("admin")
async def delete_request(request: Request, req_id: int, db: Session = Depends(get_db)):
    req = db.query(SourcingRequest).filter(SourcingRequest.id == req_id).first()
    if req:
        log_action(db, "sourcing_request", req_id, "deleted",
                   request.session.get("user_id"), f"Запрос {req.number} удалён")
        db.delete(req)
        db.commit()
    return RedirectResponse(url="/sourcing/", status_code=302)


# ─────────────────────────────────────────────────────────────────────────────
# Предложения (quotes)
# ─────────────────────────────────────────────────────────────────────────────
def _parse_int(v: str):
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _parse_float(v: str):
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except (ValueError, TypeError):
        return 0.0


@router.post("/requests/{req_id}/quote")
@role_required("manager")
async def add_quote(
    request: Request, req_id: int,
    vendor_id: int = Form(...),
    price: str = Form(default="0"),
    term_days: str = Form(default="0"),
    quality: int = Form(default=3),
    payment_terms: str = Form(default=""),
    min_batch: str = Form(default=""),
    comment: str = Form(default=""),
    db: Session = Depends(get_db),
):
    req = db.query(SourcingRequest).filter(SourcingRequest.id == req_id).first()
    if not req:
        return RedirectResponse(url="/sourcing/", status_code=302)
    q = VendorQuote(
        request_id=req_id,
        vendor_id=vendor_id,
        price=_parse_float(price),
        term_days=_parse_int(term_days) or 0,
        quality=max(1, min(5, quality)),
        payment_terms=payment_terms.strip() or None,
        min_batch=_parse_int(min_batch),
        comment=comment.strip() or None,
    )
    db.add(q)
    db.commit()
    _recalc_scores(db, req_id)
    _recalc_vendor_rating(db, vendor_id)
    db.commit()
    return RedirectResponse(url=f"/sourcing/requests/{req_id}", status_code=302)


@router.post("/requests/{req_id}/quote/{qid}/edit")
@role_required("manager")
async def edit_quote(
    request: Request, req_id: int, qid: int,
    price: str = Form(default="0"),
    term_days: str = Form(default="0"),
    quality: int = Form(default=3),
    payment_terms: str = Form(default=""),
    min_batch: str = Form(default=""),
    comment: str = Form(default=""),
    db: Session = Depends(get_db),
):
    q = db.query(VendorQuote).filter(VendorQuote.id == qid,
                                     VendorQuote.request_id == req_id).first()
    if q:
        q.price = _parse_float(price)
        q.term_days = _parse_int(term_days) or 0
        q.quality = max(1, min(5, quality))
        q.payment_terms = payment_terms.strip() or None
        q.min_batch = _parse_int(min_batch)
        q.comment = comment.strip() or None
        db.commit()
        _recalc_scores(db, req_id)
        _recalc_vendor_rating(db, q.vendor_id)
        db.commit()
    return RedirectResponse(url=f"/sourcing/requests/{req_id}", status_code=302)


@router.post("/requests/{req_id}/quote/{qid}/delete")
@role_required("manager")
async def delete_quote(request: Request, req_id: int, qid: int,
                       db: Session = Depends(get_db)):
    q = db.query(VendorQuote).filter(VendorQuote.id == qid,
                                     VendorQuote.request_id == req_id).first()
    if q:
        vendor_id = q.vendor_id
        db.delete(q)
        db.commit()
        _recalc_scores(db, req_id)
        _recalc_vendor_rating(db, vendor_id)
        db.commit()
    return RedirectResponse(url=f"/sourcing/requests/{req_id}", status_code=302)


# ─────────────────────────────────────────────────────────────────────────────
# Каталог поставщиков
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/vendors", response_class=HTMLResponse)
@login_required
async def list_vendors(request: Request, cat_id: int = 0, status: str = "",
                       db: Session = Depends(get_db)):
    q = db.query(Vendor).filter(Vendor.is_active == True).order_by(Vendor.name)
    if cat_id:
        q = q.filter(Vendor.category_id == cat_id)
    if status:
        q = q.filter(Vendor.status == status)
    vendors = q.all()
    categories = db.query(ProcurementCategory).filter(
        ProcurementCategory.is_active == True).order_by(ProcurementCategory.name).all()
    return templates.TemplateResponse(request, "sourcing/vendors.html", {
        "vendors": vendors,
        "categories": categories,
        "vendor_statuses": VENDOR_STATUSES,
        "vendor_status_colors": VENDOR_STATUS_COLORS,
        "filter_cat_id": cat_id,
        "filter_status": status,
    })


@router.get("/vendors/new", response_class=HTMLResponse)
@login_required
async def new_vendor(request: Request, db: Session = Depends(get_db)):
    categories = db.query(ProcurementCategory).filter(
        ProcurementCategory.is_active == True).order_by(ProcurementCategory.name).all()
    return templates.TemplateResponse(request, "sourcing/vendor_form.html", {
        "vendor": None,
        "categories": categories,
        "vendor_statuses": VENDOR_STATUSES,
    })


@router.post("/vendors/new")
@role_required("manager")
async def create_vendor(
    request: Request,
    name: str = Form(...),
    category_id: int = Form(default=0),
    website: str = Form(default=""),
    phone: str = Form(default=""),
    email: str = Form(default=""),
    contact_person: str = Form(default=""),
    region: str = Form(default=""),
    status: str = Form(default="new"),
    notes: str = Form(default=""),
    db: Session = Depends(get_db),
):
    v = Vendor(
        name=name.strip(),
        category_id=category_id or None,
        website=website.strip() or None,
        phone=phone.strip() or None,
        email=email.strip() or None,
        contact_person=contact_person.strip() or None,
        region=region.strip() or None,
        status=status if status in VENDOR_STATUSES else "new",
        notes=notes.strip() or None,
    )
    db.add(v)
    db.commit()
    log_action(db, "vendor", v.id, "created",
               request.session.get("user_id"), f"Поставщик: {v.name}")
    db.commit()
    return RedirectResponse(url=f"/sourcing/vendors/{v.id}", status_code=302)


@router.get("/vendors/{vendor_id}", response_class=HTMLResponse)
@login_required
async def view_vendor(request: Request, vendor_id: int, db: Session = Depends(get_db)):
    v = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not v:
        return RedirectResponse(url="/sourcing/vendors", status_code=302)
    categories = db.query(ProcurementCategory).filter(
        ProcurementCategory.is_active == True).order_by(ProcurementCategory.name).all()
    quotes = db.query(VendorQuote).filter(VendorQuote.vendor_id == vendor_id).all()
    return templates.TemplateResponse(request, "sourcing/vendor_detail.html", {
        "vendor": v,
        "categories": categories,
        "quotes": quotes,
        "vendor_statuses": VENDOR_STATUSES,
        "vendor_status_colors": VENDOR_STATUS_COLORS,
    })


@router.post("/vendors/{vendor_id}/edit")
@role_required("manager")
async def edit_vendor(
    request: Request, vendor_id: int,
    name: str = Form(...),
    category_id: int = Form(default=0),
    website: str = Form(default=""),
    phone: str = Form(default=""),
    email: str = Form(default=""),
    contact_person: str = Form(default=""),
    region: str = Form(default=""),
    status: str = Form(default="new"),
    notes: str = Form(default=""),
    db: Session = Depends(get_db),
):
    v = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if v:
        v.name = name.strip()
        v.category_id = category_id or None
        v.website = website.strip() or None
        v.phone = phone.strip() or None
        v.email = email.strip() or None
        v.contact_person = contact_person.strip() or None
        v.region = region.strip() or None
        v.status = status if status in VENDOR_STATUSES else v.status
        v.notes = notes.strip() or None
        db.commit()
    return RedirectResponse(url=f"/sourcing/vendors/{vendor_id}", status_code=302)


@router.post("/vendors/{vendor_id}/delete")
@role_required("admin")
async def delete_vendor(request: Request, vendor_id: int, db: Session = Depends(get_db)):
    v = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if v:
        log_action(db, "vendor", vendor_id, "deleted",
                   request.session.get("user_id"), f"Поставщик {v.name} удалён")
        db.delete(v)
        db.commit()
    return RedirectResponse(url="/sourcing/vendors", status_code=302)


@router.post("/vendors/{vendor_id}/to-counterparty")
@role_required("manager")
async def vendor_to_counterparty(request: Request, vendor_id: int,
                                 db: Session = Depends(get_db)):
    v = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not v:
        return RedirectResponse(url="/sourcing/vendors", status_code=302)
    if v.counterparty_id:
        return RedirectResponse(url=f"/counterparties/{v.counterparty_id}", status_code=302)
    cp = Counterparty(
        name=v.name,
        type="supplier",
        phone=v.phone,
        email=v.email,
        contact_person=v.contact_person,
        notes=v.notes,
    )
    db.add(cp)
    db.commit()
    v.counterparty_id = cp.id
    log_action(db, "vendor", vendor_id, "to_counterparty",
               request.session.get("user_id"),
               f"Поставщик {v.name} заведён как контрагент #{cp.id}")
    db.commit()
    return RedirectResponse(url=f"/counterparties/{cp.id}", status_code=302)


# ─────────────────────────────────────────────────────────────────────────────
# Справочник направлений
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/categories", response_class=HTMLResponse)
@login_required
async def list_categories(request: Request, db: Session = Depends(get_db)):
    categories = db.query(ProcurementCategory).order_by(ProcurementCategory.name).all()
    counts = dict(
        db.query(Vendor.category_id, func.count(Vendor.id))
          .filter(Vendor.is_active == True)
          .group_by(Vendor.category_id).all()
    )
    return templates.TemplateResponse(request, "sourcing/categories.html", {
        "categories": categories,
        "vendor_counts": counts,
    })


@router.post("/categories/new")
@role_required("manager")
async def create_category(request: Request, name: str = Form(...),
                          db: Session = Depends(get_db)):
    name = name.strip()
    if name:
        db.add(ProcurementCategory(name=name))
        db.commit()
    return RedirectResponse(url="/sourcing/categories", status_code=302)


@router.post("/categories/{cat_id}/edit")
@role_required("manager")
async def edit_category(request: Request, cat_id: int, name: str = Form(...),
                        is_active: int = Form(default=1), db: Session = Depends(get_db)):
    c = db.query(ProcurementCategory).filter(ProcurementCategory.id == cat_id).first()
    if c:
        c.name = name.strip() or c.name
        c.is_active = bool(is_active)
        db.commit()
    return RedirectResponse(url="/sourcing/categories", status_code=302)

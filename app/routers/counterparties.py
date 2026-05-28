from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import httpx
from app.database import get_db
from app.auth import login_required
from app.models import Counterparty

router = APIRouter(prefix="/counterparties", tags=["counterparties"])
templates = Jinja2Templates(directory="app/templates")

DADATA_TOKEN = "f8e4e9a1543e8d8f79091bab8d1422a83ee2d573"
DADATA_HEADERS = {
    "Authorization": f"Token {DADATA_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

CP_TYPES = {"client": "Покупатель", "supplier": "Поставщик", "both": "Покупатель и поставщик"}


@router.get("/", response_class=HTMLResponse)
@login_required
async def list_counterparties(request: Request, q: str = "", type: str = "", db: Session = Depends(get_db)):
    query = db.query(Counterparty).filter(Counterparty.is_active == True)
    if q:
        query = query.filter(Counterparty.name.ilike(f"%{q}%"))
    if type:
        query = query.filter(Counterparty.type == type)
    counterparties = query.order_by(Counterparty.name).all()
    return templates.TemplateResponse(request, "counterparties/list.html", {
        "counterparties": counterparties, "q": q, "type": type, "cp_types": CP_TYPES,
    })


@router.get("/new", response_class=HTMLResponse)
@login_required
async def new_counterparty(request: Request):
    return templates.TemplateResponse(request, "counterparties/form.html", {
        "cp": None, "cp_types": CP_TYPES, "errors": [],
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
    bank_name: str = Form(default=""),
    bank_account: str = Form(default=""),
    bank_bik: str = Form(default=""),
    bank_corr_account: str = Form(default=""),
    notes: str = Form(default=""),
    db: Session = Depends(get_db),
):
    cp = Counterparty(
        name=name, trade_name=trade_name or None, short_name=short_name,
        inn=inn, kpp=kpp, ogrn=ogrn,
        legal_address=legal_address, actual_address=actual_address,
        phone=phone, email=email, contact_person=contact_person, type=type,
        bank_name=bank_name, bank_account=bank_account, bank_bik=bank_bik,
        bank_corr_account=bank_corr_account, notes=notes,
    )
    db.add(cp)
    db.commit()
    return RedirectResponse(url="/counterparties", status_code=302)


@router.get("/dadata/party", response_class=JSONResponse)
@login_required
async def dadata_party(request: Request, inn: str = ""):
    """Поиск компании по ИНН через DaData."""
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
    """Поиск банка по БИК через DaData."""
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
    return templates.TemplateResponse(request, "counterparties/detail.html", {
        "cp": cp, "cp_types": CP_TYPES,
    })


@router.get("/{cp_id}/edit", response_class=HTMLResponse)
@login_required
async def edit_counterparty(request: Request, cp_id: int, db: Session = Depends(get_db)):
    cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first()
    if not cp:
        return RedirectResponse(url="/counterparties", status_code=302)
    return templates.TemplateResponse(request, "counterparties/form.html", {
        "cp": cp, "cp_types": CP_TYPES, "errors": [],
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
    bank_name: str = Form(default=""),
    bank_account: str = Form(default=""),
    bank_bik: str = Form(default=""),
    bank_corr_account: str = Form(default=""),
    notes: str = Form(default=""),
    db: Session = Depends(get_db),
):
    cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first()
    if cp:
        cp.name = name; cp.trade_name = trade_name or None; cp.short_name = short_name
        cp.inn = inn; cp.kpp = kpp
        cp.ogrn = ogrn; cp.legal_address = legal_address; cp.actual_address = actual_address
        cp.phone = phone; cp.email = email; cp.contact_person = contact_person; cp.type = type
        cp.bank_name = bank_name; cp.bank_account = bank_account; cp.bank_bik = bank_bik
        cp.bank_corr_account = bank_corr_account; cp.notes = notes
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

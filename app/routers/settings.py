from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db, hash_password
from app.auth import login_required
from app.models import CompanySettings, User

router = APIRouter(prefix="/settings", tags=["settings"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
@login_required
async def settings_page(request: Request, db: Session = Depends(get_db)):
    company = db.query(CompanySettings).first()
    users = db.query(User).filter(User.is_active == True).all()
    return templates.TemplateResponse(request, "settings/index.html", {
        "company": company, "users": users,
        "saved": request.query_params.get("saved"),
    })


@router.post("/company")
@login_required
async def save_company(
    request: Request,
    name: str = Form(default=""),
    short_name: str = Form(default=""),
    inn: str = Form(default=""),
    kpp: str = Form(default=""),
    ogrn: str = Form(default=""),
    legal_address: str = Form(default=""),
    actual_address: str = Form(default=""),
    phone: str = Form(default=""),
    email: str = Form(default=""),
    director: str = Form(default=""),
    director_basis: str = Form(default="Устава"),
    accountant: str = Form(default=""),
    bank_name: str = Form(default=""),
    bank_account: str = Form(default=""),
    bank_bik: str = Form(default=""),
    bank_corr_account: str = Form(default=""),
    monthly_plan: float = Form(default=225000.0),
    db: Session = Depends(get_db),
):
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)
    company.name = name; company.short_name = short_name
    company.inn = inn; company.kpp = kpp; company.ogrn = ogrn
    company.legal_address = legal_address; company.actual_address = actual_address
    company.phone = phone; company.email = email
    company.director = director; company.director_basis = director_basis
    company.accountant = accountant
    company.bank_name = bank_name; company.bank_account = bank_account
    company.bank_bik = bank_bik; company.bank_corr_account = bank_corr_account
    company.monthly_plan = monthly_plan
    db.commit()
    return RedirectResponse(url="/settings/?saved=1", status_code=302)


@router.post("/users/new")
@login_required
async def create_user(
    request: Request,
    username: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    role: str = Form(default="manager"),
    db: Session = Depends(get_db),
):
    existing = db.query(User).filter(User.username == username).first()
    if not existing:
        user = User(
            username=username, full_name=full_name,
            password_hash=hash_password(password), role=role,
        )
        db.add(user)
        db.commit()
    return RedirectResponse(url="/settings/", status_code=302)


@router.post("/users/{user_id}/delete")
@login_required
async def delete_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    if request.session.get("user_id") != user_id:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.is_active = False
            db.commit()
    return RedirectResponse(url="/settings/", status_code=302)

import os
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db, hash_password
from app.auth import login_required, role_required
from app.models import CompanySettings, User

router = APIRouter(prefix="/settings", tags=["settings"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
@login_required
async def settings_page(request: Request, db: Session = Depends(get_db)):
    from app.auth import ROLE_LABELS
    company = db.query(CompanySettings).first()
    users = db.query(User).filter(User.is_active == True).all()
    return templates.TemplateResponse(request, "settings/index.html", {
        "company": company, "users": users, "role_labels": ROLE_LABELS,
        "saved": request.query_params.get("saved"),
    })


@router.get("/board/", response_class=HTMLResponse)
@login_required
async def board_settings_page(request: Request, db: Session = Depends(get_db)):
    from app.routers.board import BOARD_KEY, parse_stations
    company = db.query(CompanySettings).first()
    stations = parse_stations(company.board_stations if company else None)
    return templates.TemplateResponse(request, "settings/board.html", {
        "company": company,
        "saved": request.query_params.get("saved"),
        "board_key": BOARD_KEY,
        "board_stations": stations,
    })


@router.post("/company")
@role_required("admin")
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


@router.post("/board/")
@role_required("admin")
async def save_board(
    request: Request,
    brand_name: str = Form(default=""),
    board_nuts_plan: float = Form(default=0.0),
    board_shift_start: str = Form(default="09:00"),
    board_shift_end: str = Form(default="17:00"),
    board_nut_price: float = Form(default=52.0),
    board_cost_pct: float = Form(default=0.0),
    board_cost_norm_pct: float = Form(default=48.0),
    board_cost_deviation: float = Form(default=5.0),
    board_quotes: str = Form(default=""),
    board_stations: str = Form(default=""),
    board_active_station: int = Form(default=0),
    db: Session = Depends(get_db),
):
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)
    company.brand_name           = brand_name or None
    company.board_nuts_plan      = board_nuts_plan
    company.board_shift_start    = board_shift_start
    company.board_shift_end      = board_shift_end
    company.board_nut_price      = board_nut_price
    company.board_cost_pct       = board_cost_pct
    company.board_cost_norm_pct  = board_cost_norm_pct
    company.board_cost_deviation = board_cost_deviation
    company.board_quotes         = board_quotes
    company.board_stations       = board_stations
    company.board_active_station = board_active_station
    db.commit()
    return RedirectResponse(url="/settings/board/?saved=1", status_code=302)


@router.post("/logo")
@login_required
async def upload_logo(
    request: Request,
    logo: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    os.makedirs("app/static/uploads", exist_ok=True)
    ext = os.path.splitext(logo.filename)[1].lower() or ".png"
    logo_path = f"app/static/uploads/logo{ext}"
    content = await logo.read()
    with open(logo_path, "wb") as f:
        f.write(content)
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)
    company.logo_path = logo_path
    db.commit()
    return RedirectResponse(url="/settings/?saved=1", status_code=302)


@router.post("/users/new")
@role_required("admin")
async def create_user(
    request: Request,
    username: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    role: str = Form(default="manager"),
    db: Session = Depends(get_db),
):
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        # Пользователь был удалён (is_active=False) — восстанавливаем с новыми данными
        existing.full_name = full_name
        existing.password_hash = hash_password(password)
        existing.role = role
        existing.is_active = True
    else:
        db.add(User(
            username=username, full_name=full_name,
            password_hash=hash_password(password), role=role,
        ))
    db.commit()
    return RedirectResponse(url="/settings/", status_code=302)


@router.post("/users/{user_id}/delete")
@role_required("admin")
async def delete_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    if request.session.get("user_id") != user_id:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.is_active = False
            db.commit()
    return RedirectResponse(url="/settings/", status_code=302)

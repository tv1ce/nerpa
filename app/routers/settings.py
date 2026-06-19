import os
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db, hash_password
from app.auth import login_required, role_required
from app.models import CompanySettings, User
from app.routers.counterparties import _validate_inn, _validate_kpp, _validate_ogrn

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
    kpi_product_filter: str = Form(default="орешк"),
    notify_contract_days: int = Form(default=14),
    notify_invoice_days: int = Form(default=3),
    db: Session = Depends(get_db),
):
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)
    company.name = name; company.short_name = short_name
    company.inn = _validate_inn(inn); company.kpp = _validate_kpp(kpp); company.ogrn = _validate_ogrn(ogrn)
    company.legal_address = legal_address; company.actual_address = actual_address
    company.phone = phone; company.email = email
    company.director = director; company.director_basis = director_basis
    company.accountant = accountant
    company.bank_name = bank_name; company.bank_account = bank_account
    company.bank_bik = bank_bik; company.bank_corr_account = bank_corr_account
    company.monthly_plan = monthly_plan
    company.kpi_product_filter = kpi_product_filter.strip() or "орешк"
    company.notify_contract_days = max(0, notify_contract_days)
    company.notify_invoice_days = max(0, notify_invoice_days)
    db.commit()
    return RedirectResponse(url="/settings/?saved=1", status_code=302)


@router.post("/board/")
@login_required
async def save_board(
    request: Request,
    brand_name: str = Form(default=""),
    board_nuts_plan: float = Form(default=0.0),
    board_cost_pct: float = Form(default=0.0),
    board_cost_norm_pct: float = Form(default=48.0),
    board_cost_deviation: float = Form(default=5.0),
    board_quotes: str = Form(default=""),
    board_stations: str = Form(default=""),
    board_active_station: int = Form(default=0),
    db: Session = Depends(get_db),
):
    role = request.session.get("user_role", "viewer")
    if role not in ("admin", "warehouse"):
        from fastapi.responses import HTMLResponse as _HTML
        return _HTML("<h2>403 — Нет доступа</h2>", status_code=403)
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)
    company.brand_name           = brand_name or None
    company.board_nuts_plan      = board_nuts_plan
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
    ext = os.path.splitext(logo.filename or "")[1].lower()
    # Разрешаем только изображения
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
        return RedirectResponse(url="/settings/?logo_error=1", status_code=302)
    # Ограничение 5 МБ
    content = await logo.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        return RedirectResponse(url="/settings/?logo_error=1", status_code=302)
    logo_path = f"app/static/uploads/logo{ext}"
    with open(logo_path, "wb") as f:
        f.write(content)
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)
    company.logo_path = logo_path
    db.commit()
    return RedirectResponse(url="/settings/?saved=1", status_code=302)


def _normalize_chat_ids(raw: str) -> str | None:
    """Нормализует строку chat_id: оставляет только числа через запятую."""
    ids = []
    for part in raw.replace(";", ",").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(str(int(part)))
        except ValueError:
            continue
    return ",".join(ids) or None


@router.post("/telegram")
@role_required("admin")
async def save_telegram(
    request: Request,
    tg_bot_token: str = Form(default=""),
    tg_report_chat_ids: str = Form(default=""),
    tg_callback_chat_ids: str = Form(default=""),
    tg_callback_enabled: str = Form(default=""),
    backup_enabled: str = Form(default=""),
    backup_frequency: str = Form(default="weekly"),
    tg_backup_chat_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)
    company.tg_bot_token = tg_bot_token.strip() or None
    company.tg_report_chat_ids = _normalize_chat_ids(tg_report_chat_ids)
    company.tg_callback_chat_ids = _normalize_chat_ids(tg_callback_chat_ids)
    company.tg_callback_enabled = (tg_callback_enabled == "1")
    company.backup_enabled = (backup_enabled == "1")
    company.backup_frequency = backup_frequency if backup_frequency in ("daily", "weekly", "monthly") else "weekly"
    company.tg_backup_chat_id = _normalize_chat_ids(tg_backup_chat_id) or None
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


@router.post("/users/{user_id}/edit")
@role_required("admin")
async def edit_user(
    request: Request,
    user_id: int,
    full_name: str = Form(...),
    role: str = Form(default="manager"),
    password: str = Form(default=""),
    db: Session = Depends(get_db),
):
    from app.auth import ROLE_LABELS
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.full_name = full_name.strip() or user.full_name
        if role in ROLE_LABELS:
            user.role = role
        # Пароль меняем только если задан новый
        if password.strip():
            user.password_hash = hash_password(password.strip())
            user.must_change_password = False
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


# ── Профиль текущего пользователя (доступен всем ролям) ───────────────────────

@router.get("/profile", response_class=HTMLResponse)
@login_required
async def profile_page(request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == request.session.get("user_id")).first()
    if not user:
        return RedirectResponse(url="/auth/login", status_code=302)
    return templates.TemplateResponse(request, "settings/profile.html", {
        "user": user,
        "saved": request.query_params.get("saved"),
        "pwd_error": request.query_params.get("pwd_error"),
    })


@router.post("/profile")
@login_required
async def profile_save(
    request: Request,
    full_name: str = Form(...),
    birthday: str = Form(default=""),
    db: Session = Depends(get_db),
):
    from datetime import date as _date
    user = db.query(User).filter(User.id == request.session.get("user_id")).first()
    if user:
        user.full_name = full_name.strip() or user.full_name
        if birthday:
            try:
                user.birthday = _date.fromisoformat(birthday)
            except ValueError:
                pass
        else:
            user.birthday = None
        db.commit()
        request.session["user_name"] = user.full_name
    return RedirectResponse(url="/settings/profile?saved=1", status_code=302)


@router.get("/backup")
@role_required("admin")
async def backup_db(request: Request, db: Session = Depends(get_db)):
    """Скачать резервную копию БД. Только admin. WAL-checkpoint перед выдачей."""
    from datetime import date as _date
    # Получаем путь к БД из переменной окружения или используем путь по умолчанию
    db_url = os.getenv("DATABASE_URL", "sqlite:///./tms.db")
    # Парсим SQLite URL: sqlite:///./path/to/db.db → ./path/to/db.db (убираем sqlite:///)
    if db_url.startswith("sqlite:///"):
        db_path = db_url.replace("sqlite:///", "")
    else:
        db_path = "tms.db"
    # Конвертируем в абсолютный путь
    db_path = os.path.abspath(db_path)
    if not os.path.exists(db_path):
        return HTMLResponse(f"База данных не найдена: {db_path}", status_code=404)
    # Сбрасываем WAL в основной файл перед скачиванием
    db.execute(__import__("sqlalchemy").text("PRAGMA wal_checkpoint(TRUNCATE)"))
    filename = f"tms-backup-{_date.today().isoformat()}.db"
    return FileResponse(db_path, filename=filename, media_type="application/octet-stream")


@router.post("/profile/password")
@login_required
async def profile_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    from app.database import verify_password
    user = db.query(User).filter(User.id == request.session.get("user_id")).first()
    if not user:
        return RedirectResponse(url="/auth/login", status_code=302)
    if not verify_password(current_password, user.password_hash):
        return RedirectResponse(url="/settings/profile?pwd_error=wrong", status_code=302)
    if len(new_password) < 6:
        return RedirectResponse(url="/settings/profile?pwd_error=short", status_code=302)
    if new_password != confirm_password:
        return RedirectResponse(url="/settings/profile?pwd_error=mismatch", status_code=302)
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.commit()
    return RedirectResponse(url="/settings/profile?saved=1", status_code=302)

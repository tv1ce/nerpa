import asyncio
import os
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
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


@router.post("/modules")
@role_required("admin")
async def save_modules(
    request: Request,
    module_leads:    str = Form(default=""),
    module_recon:    str = Form(default=""),
    module_sourcing: str = Form(default=""),
    module_field:    str = Form(default=""),
    module_hr:       str = Form(default=""),
    db: Session = Depends(get_db),
):
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)
    company.module_leads    = bool(module_leads)
    company.module_recon    = bool(module_recon)
    company.module_sourcing = bool(module_sourcing)
    company.module_field    = bool(module_field)
    company.module_hr       = bool(module_hr)
    db.commit()
    request.session["mod_leads"]    = company.module_leads
    request.session["mod_recon"]    = company.module_recon
    request.session["mod_sourcing"] = company.module_sourcing
    request.session["mod_field"]    = company.module_field
    request.session["mod_hr"]       = company.module_hr
    return RedirectResponse(url="/settings/?saved=1&tab=modules", status_code=302)


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
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
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


def _normalize_time(raw: str) -> str:
    """«9:5» → «09:05». Мусор превращаем в 09:30, а не роняем сохранение настроек."""
    try:
        hh, mm = (int(x) for x in raw.strip().split(":", 1))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    except (ValueError, AttributeError):
        pass
    return "09:30"


@router.post("/telegram")
@role_required("admin")
async def save_telegram(
    request: Request,
    tg_bot_token: str = Form(default=""),
    tg_report_chat_ids: str = Form(default=""),
    tg_hr_report_chat_ids: str = Form(default=""),
    tg_callback_chat_ids: str = Form(default=""),
    tg_callback_enabled: str = Form(default=""),
    backup_enabled: str = Form(default=""),
    backup_frequency: str = Form(default="weekly"),
    tg_backup_chat_id: str = Form(default=""),
    tg_warehouse_enabled: str = Form(default=""),
    tg_warehouse_chat_id: str = Form(default=""),
    tg_warehouse_topic_receiving: str = Form(default=""),
    tg_warehouse_topic_assembled: str = Form(default=""),
    tg_warehouse_topic_shipped: str = Form(default=""),
    hr_metric_remind_enabled: str = Form(default=""),
    hr_metric_remind_chat_ids: str = Form(default=""),
    hr_metric_check_enabled: str = Form(default=""),
    hr_metric_check_chat_ids: str = Form(default=""),
    outlets_digest_enabled: str = Form(default=""),
    outlets_digest_time: str = Form(default="09:30"),
    outlets_digest_chat_ids: str = Form(default=""),
    outlets_digest_weekdays_only: str = Form(default=""),
    db: Session = Depends(get_db),
):
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)
    company.tg_bot_token = tg_bot_token.strip() or None
    company.tg_report_chat_ids = _normalize_chat_ids(tg_report_chat_ids)
    company.tg_hr_report_chat_ids = _normalize_chat_ids(tg_hr_report_chat_ids)
    company.tg_callback_chat_ids = _normalize_chat_ids(tg_callback_chat_ids)
    company.tg_callback_enabled = (tg_callback_enabled == "1")
    company.backup_enabled = (backup_enabled == "1")
    company.backup_frequency = backup_frequency if backup_frequency in ("daily", "weekly", "monthly") else "weekly"
    company.tg_backup_chat_id = _normalize_chat_ids(tg_backup_chat_id) or None
    company.tg_warehouse_enabled = (tg_warehouse_enabled == "1")
    company.tg_warehouse_chat_id = _normalize_chat_ids(tg_warehouse_chat_id) or None
    company.tg_warehouse_topic_receiving = tg_warehouse_topic_receiving.strip() or None
    company.tg_warehouse_topic_assembled = tg_warehouse_topic_assembled.strip() or None
    company.tg_warehouse_topic_shipped = tg_warehouse_topic_shipped.strip() or None
    company.hr_metric_remind_enabled = (hr_metric_remind_enabled == "1")
    company.hr_metric_remind_chat_ids = _normalize_chat_ids(hr_metric_remind_chat_ids)
    company.hr_metric_check_enabled = (hr_metric_check_enabled == "1")
    company.hr_metric_check_chat_ids = _normalize_chat_ids(hr_metric_check_chat_ids)
    company.outlets_digest_enabled = (outlets_digest_enabled == "1")
    company.outlets_digest_time = _normalize_time(outlets_digest_time)
    company.outlets_digest_chat_ids = _normalize_chat_ids(outlets_digest_chat_ids)
    company.outlets_digest_weekdays_only = (outlets_digest_weekdays_only == "1")
    db.commit()
    return RedirectResponse(url="/settings/?saved=1", status_code=302)


@router.post("/telegram/discover-warehouse-topics")
@role_required("admin")
async def discover_warehouse_topics(request: Request, db: Session = Depends(get_db)):
    """Диагностика: показывает последние сообщения бота в группах — чтобы найти
    chat_id и message_thread_id нужных топиков. Попросите кладовщика/себя
    отправить по одному сообщению в каждый топик группы, затем нажмите эту
    кнопку — ниже появятся chat_id, thread_id и текст последних сообщений."""
    from app.services.telegram_send import fetch_recent_topic_updates
    company = db.query(CompanySettings).first()
    token = (company.tg_bot_token or "").strip() if company else ""
    if not token:
        return JSONResponse({"ok": False, "error": "Сначала укажите и сохраните токен бота"}, status_code=400)
    try:
        updates = fetch_recent_topic_updates(token)
        return JSONResponse({"ok": True, "updates": updates})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/users/new")
@role_required("admin")
async def create_user(
    request: Request,
    username: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    role: str = Form(default="manager"),
    bitrix_user_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        # Пользователь был удалён (is_active=False) — восстанавливаем с новыми данными
        existing.full_name = full_name
        existing.password_hash = hash_password(password)
        existing.role = role
        existing.is_active = True
        existing.bitrix_user_id = bitrix_user_id.strip() or None
    else:
        db.add(User(
            username=username, full_name=full_name,
            password_hash=hash_password(password), role=role,
            bitrix_user_id=bitrix_user_id.strip() or None,
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
    bitrix_user_id: str = Form(default=""),
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
        user.bitrix_user_id = bitrix_user_id.strip() or None
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
    phone: str = Form(default=""),
    birthday: str = Form(default=""),
    db: Session = Depends(get_db),
):
    from datetime import date as _date
    user = db.query(User).filter(User.id == request.session.get("user_id")).first()
    if user:
        user.full_name = full_name.strip() or user.full_name
        user.phone = phone.strip() or None
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
    """Скачать резервную копию БД. Только admin.

    Использует `VACUUM INTO` — SQLite собирает целостную копию со всеми данными,
    включая ещё не сброшенный WAL. Это надёжнее, чем копировать сам файл tms.db
    (он может быть устаревшим, пока WAL не сделал checkpoint, а checkpoint не
    срабатывает при активных соединениях приложения)."""
    from datetime import date as _date
    from app.utils import log_action
    from app.database import make_backup_copy

    # Логируем факт скачивания БД
    log_action(db, "system", 0, "backup_downloaded",
               request.session.get("user_id"),
               f"Скачана резервная копия БД (IP: {request.client.host if request.client else '?'})")

    try:
        tmp_path = make_backup_copy()
    except Exception as e:
        return HTMLResponse(f"Не удалось создать резервную копию: {e}", status_code=500)

    filename = f"tms-backup-{_date.today().isoformat()}.db"
    # BackgroundTask удалит временный файл после того, как ответ отправлен клиенту
    from starlette.background import BackgroundTask
    return FileResponse(
        path=tmp_path, filename=filename, media_type="application/octet-stream",
        background=BackgroundTask(lambda: os.path.exists(tmp_path) and os.remove(tmp_path)),
    )


@router.post("/onec")
@role_required("admin")
async def save_onec(
    request: Request,
    onec_url: str = Form(default=""),
    onec_user: str = Form(default=""),
    onec_password: str = Form(default=""),
    onec_enabled: str = Form(default=""),
    onec_hs_url: str = Form(default=""),
    doc_intake_channel: str = Form(default="off"),
    db: Session = Depends(get_db),
):
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)
    company.onec_url = onec_url.strip() or None
    company.onec_user = onec_user.strip() or None
    if onec_password.strip():
        company.onec_password = onec_password.strip()
    company.onec_enabled = (onec_enabled == "1")
    company.onec_hs_url = onec_hs_url.strip() or None
    if doc_intake_channel in ("off", "telegram", "email", "folder"):
        company.doc_intake_channel = doc_intake_channel
    db.commit()
    return RedirectResponse(url="/settings/?saved=1#onec", status_code=302)


@router.post("/metafora")
@role_required("admin")
async def save_metafora_api(
    request: Request,
    metafora_api_token: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Токен API Метафоры — им TMS создаёт заказы в системе перевозчика."""
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)
    # Пустое поле не стирает токен — «Сохранить» без повторного ввода безопасно
    if metafora_api_token.strip():
        company.metafora_api_token = metafora_api_token.strip()
    db.commit()
    return RedirectResponse(url="/settings/?saved=1#metafora", status_code=302)


@router.post("/tochka")
@role_required("admin")
async def save_tochka(
    request: Request,
    tochka_token: str = Form(default=""),
    tochka_account_id: str = Form(default=""),
    tochka_customer_code: str = Form(default=""),
    tochka_enabled: str = Form(default=""),
    db: Session = Depends(get_db),
):
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)
    # Токен не перетираем пустым значением (как onec_password) — чтобы «Сохранить»
    # без повторного ввода токена не стирал его.
    if tochka_token.strip():
        company.tochka_token = tochka_token.strip()
    company.tochka_account_id = tochka_account_id.strip() or None
    company.tochka_customer_code = tochka_customer_code.strip() or None
    company.tochka_enabled = (tochka_enabled == "1")
    db.commit()
    # Автоподписка на вебхук: при включённой сверке и наличии токена регистрируем
    # приёмник /api/tochka/webhook по публичному адресу приложения (Точка требует
    # HTTPS:443 и доступность из интернета). Ошибка не мешает сохранению настроек.
    hook = ""
    if company.tochka_enabled and company.tochka_token:
        from app.services.tochka_client import ensure_webhook, webhook_url_from_base
        url = webhook_url_from_base(str(request.base_url))
        res = ensure_webhook(db, url)
        if res.get("ok"):
            company.tochka_webhook_url = url
            db.commit()
            hook = "&hook=ok"
        else:
            hook = "&hook=fail"
    return RedirectResponse(url=f"/settings/?saved=1&tab=integrations{hook}#tochka", status_code=302)


@router.post("/sbis")
@role_required("admin")
async def save_sbis(
    request: Request,
    sbis_login: str = Form(default=""),
    sbis_password: str = Form(default=""),
    sbis_account_id: str = Form(default=""),
    saby_cargo_name: str = Form(default=""),
    saby_unit_weight_g: float = Form(default=20.0),
    db: Session = Depends(get_db),
):
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)
    company.sbis_login      = sbis_login.strip() or None
    if sbis_password.strip():
        company.sbis_password = sbis_password.strip()
    company.sbis_account_id = sbis_account_id.strip() or None
    company.saby_cargo_name = saby_cargo_name.strip() or "Орешки кондитерские"
    company.saby_unit_weight_g = saby_unit_weight_g if saby_unit_weight_g > 0 else 20.0
    db.commit()
    return RedirectResponse(url="/settings/?saved=1&tab=integrations", status_code=302)


@router.post("/versta")
@role_required("admin")
async def save_versta(
    request: Request,
    versta_api_key: str = Form(default=""),
    versta_enabled: str = Form(default=""),
    db: Session = Depends(get_db),
):
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)
    if versta_api_key.strip():
        company.versta_api_key = versta_api_key.strip()
    company.versta_enabled = (versta_enabled == "1")
    db.commit()
    return RedirectResponse(url="/settings/?saved=1&tab=integrations#versta", status_code=302)


@router.post("/bitrix")
@role_required("admin")
async def save_bitrix(
    request: Request,
    bitrix_webhook_url: str = Form(default=""),
    bitrix_enabled: str = Form(default=""),
    bitrix_stage_paid: str = Form(default=""),
    bitrix_stage_shipped: str = Form(default=""),
    bitrix_stage_delivered: str = Form(default=""),
    bitrix_alert_chat_ids: str = Form(default=""),
    bitrix_notify_user_ids: list[str] = Form(default=[]),
    bitrix_lead_export_enabled: str = Form(default=""),
    bitrix_lead_responsible_id: str = Form(default=""),
    bitrix_stock_enabled: str = Form(default=""),
    bitrix_stock_field: str = Form(default=""),
    shop_stage_approved: str = Form(default=""),
    shop_alert_chat_ids: str = Form(default=""),
    shipping_weekdays: list[str] = Form(default=[]),
    public_url: str = Form(default=""),
    db: Session = Depends(get_db),
):
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)
    company.bitrix_webhook_url = bitrix_webhook_url.strip() or None
    company.bitrix_enabled = (bitrix_enabled == "1")
    company.bitrix_stage_paid = bitrix_stage_paid.strip() or None
    company.bitrix_stage_shipped = bitrix_stage_shipped.strip() or None
    company.bitrix_stage_delivered = bitrix_stage_delivered.strip() or None
    company.bitrix_alert_chat_ids = bitrix_alert_chat_ids.strip() or None
    company.public_url = public_url.strip().rstrip("/") or None
    valid_ids = {str(uid) for uid, in db.query(User.id)}
    selected = [uid for uid in bitrix_notify_user_ids if uid in valid_ids]
    company.bitrix_notify_user_ids = ",".join(selected) or None
    company.bitrix_lead_export_enabled = (bitrix_lead_export_enabled == "1")
    company.bitrix_lead_responsible_id = bitrix_lead_responsible_id.strip() or None
    company.bitrix_stock_enabled = (bitrix_stock_enabled == "1")
    company.bitrix_stock_field = bitrix_stock_field.strip() or None
    company.shop_stage_approved = shop_stage_approved.strip() or None
    company.shop_alert_chat_ids = shop_alert_chat_ids.strip() or None
    # Дни отгрузки: только валидные номера дней, по возрастанию. Пустой набор
    # не сохраняем — иначе клиенту в кабинете нечего будет выбрать.
    days = sorted({int(d) for d in shipping_weekdays if d.isdigit() and 0 <= int(d) <= 6})
    company.shipping_weekdays = ",".join(str(d) for d in days) or None
    db.commit()
    return RedirectResponse(url="/settings/?saved=1&tab=integrations#bitrix", status_code=302)


@router.post("/bitrix/stock-push", response_class=JSONResponse)
@role_required("admin")
async def bitrix_stock_push(request: Request, db: Session = Depends(get_db)):
    """Ручная выгрузка остатков в каталог Bitrix24.

    Обход каталога форсируем: кнопкой обычно пользуются как раз после того, как
    в CRM завели новые товары и их надо сопоставить с номенклатурой TMS.

    push_stock_to_bitrix — синхронная функция с блокирующими HTTP-вызовами к
    Bitrix24 (httpx.Client, не async). Вызванная напрямую из async-хендлера,
    она замораживает единственный event loop uvicorn на всё время работы —
    именно так один клик по этой кнопке 06.08.2026 положил весь TMS на
    несколько минут. asyncio.to_thread уводит блокирующий вызов в отдельный
    поток, чтобы event loop продолжал обслуживать остальные запросы."""
    from app.services.bitrix_client import push_stock_to_bitrix
    result = await asyncio.to_thread(push_stock_to_bitrix, db, force_rescan=True)
    return JSONResponse(result)


@router.get("/bitrix/users", response_class=JSONResponse)
@role_required("admin")
async def bitrix_users(request: Request, db: Session = Depends(get_db)):
    """Список активных пользователей Bitrix24 — для выбора ответственного по умолчанию
    за авто-выгруженные лиды «Прозвон»/«Поле»."""
    from app.services.bitrix_client import get_bitrix_client, BitrixError
    company = db.query(CompanySettings).first()
    client = get_bitrix_client(company)
    if not client:
        return JSONResponse({"error": "not_configured"}, status_code=400)
    try:
        with client:
            return client.list_users()
    except BitrixError as e:
        return JSONResponse({"error": str(e)}, status_code=502)


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
    if len(new_password) < 8:
        return RedirectResponse(url="/settings/profile?pwd_error=short", status_code=302)
    if new_password != confirm_password:
        return RedirectResponse(url="/settings/profile?pwd_error=mismatch", status_code=302)
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.commit()
    return RedirectResponse(url="/settings/profile?saved=1", status_code=302)

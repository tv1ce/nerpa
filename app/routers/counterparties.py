from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import or_
import httpx
from app.database import get_db
from app.auth import login_required, role_required
from app.models import Counterparty, Claim, Task, Comment, AuditLog, User, ContactPerson, CarrierVehicle
from app.utils import log_action
import json
import logging
import threading

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/counterparties", tags=["counterparties"])


def _sync_carrier_vehicles(db: Session, cp: Counterparty, vehicles_json: str) -> None:
    """Полностью пересобирает список водителей/ТС перевозчика из JSON формы."""
    try:
        rows = json.loads(vehicles_json)
    except (ValueError, TypeError):
        rows = []
    db.query(CarrierVehicle).filter(CarrierVehicle.counterparty_id == cp.id).delete()
    for row in rows:
        driver = (row.get("driver_name") or "").strip()
        plate = (row.get("vehicle_plate") or "").strip()
        vtype = (row.get("vehicle_type") or "").strip()
        if not (driver or plate or vtype):
            continue
        db.add(CarrierVehicle(
            counterparty_id=cp.id,
            driver_name=driver or None,
            vehicle_plate=plate or None,
            vehicle_type=vtype or None,
        ))


def _push_cp_bg(cp_id: int) -> None:
    """Push контрагента в 1С в фоновом потоке (создаёт собственную сессию)."""
    from app.database import SessionLocal
    from app.services.onec_client import push_counterparty
    db = SessionLocal()
    try:
        cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first()
        if cp:
            push_counterparty(cp, db)
    except Exception as e:
        logger.error("push_counterparty bg %s: %s", cp_id, e)
    finally:
        db.close()
templates = Jinja2Templates(directory="app/templates")
# Jinja по умолчанию рендерит None как текст «None». В формах это попадало в
# value=… и при сохранении записывалось в БД строкой «None». Заставляем None
# выводиться пустой строкой во всех шаблонах контрагентов.
templates.env.finalize = lambda v: "" if v is None else v


def _clean(v):
    """Нормализует значение текстового поля: пустая строка и мусорные заглушки
    «None»/«null» → None (чистим легаси-строки, пришедшие из формы/интеграций)."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("none", "null"):
        return None
    return s


def _generate_signatory(full_name: str) -> str:
    """Строит «Фамилия И.О.» из полного ФИО или названия ИП."""
    if not full_name:
        return ""
    name = full_name.strip()
    for prefix in ("Индивидуальный предприниматель ", "индивидуальный предприниматель ", "ИП ", "ип "):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    parts = name.split()
    if len(parts) >= 3:
        return f"{parts[0]} {parts[1][0].upper()}.{parts[2][0].upper()}."
    if len(parts) == 2:
        return f"{parts[0]} {parts[1][0].upper()}."
    return parts[0] if parts else ""

import os as _os
import re as _re
DADATA_TOKEN = _os.getenv("DADATA_TOKEN", "")


def _clean_digits(s: str) -> str:
    """Оставляет только цифры."""
    return _re.sub(r"\D", "", s or "")


def _validate_inn(inn: str) -> str:
    """Возвращает ИНН (10 или 12 цифр) или пустую строку."""
    d = _clean_digits(inn)
    return d if len(d) in (10, 12) else ""


def _validate_kpp(kpp: str) -> str:
    """Возвращает КПП (9 цифр) или пустую строку."""
    d = _clean_digits(kpp)
    return d if len(d) == 9 else ""


def _validate_ogrn(ogrn: str) -> str:
    """Возвращает ОГРН/ОГРНИП (13 или 15 цифр) или пустую строку."""
    d = _clean_digits(ogrn)
    return d if len(d) in (13, 15) else ""
DADATA_HEADERS = {
    "Authorization": f"Token {DADATA_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

CP_TYPES = {"client": "Покупатель", "supplier": "Поставщик", "both": "Покупатель и поставщик", "carrier": "Перевозчик"}
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


# Статусы, отражающие фактическую выручку (деньги получены или товар отгружен).
# confirmed исключён — заказ подтверждён, но ещё не оплачен.
REVENUE_STATUSES = ("paid", "assembled", "handed", "delivered")


def _compute_category(cp: Counterparty) -> str | None:
    revenue = sum(o.total_amount for o in cp.orders if o.status in REVENUE_STATUSES)
    if revenue >= 1_000_000:
        return "A"
    elif revenue >= 200_000:
        return "B"
    elif revenue > 0:
        return "C"
    return None


def _filtered_counterparties(db: Session, q: str, type: str, category: str, entity_type: str):
    query = db.query(Counterparty).filter(Counterparty.is_active == True)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Counterparty.name.ilike(like),
            Counterparty.trade_name.ilike(like),
            Counterparty.inn.ilike(like),
            Counterparty.phone.ilike(like),
            Counterparty.contact_person.ilike(like),
        ))
    if type:
        query = query.filter(Counterparty.type == type)
    if category:
        query = query.filter(Counterparty.category == category)
    if entity_type:
        query = query.filter(Counterparty.entity_type == entity_type)
    return query.order_by(Counterparty.name).all()


@router.get("/", response_class=HTMLResponse)
@login_required
async def list_counterparties(
    request: Request, q: str = "", type: str = "", category: str = "",
    entity_type: str = "",
    db: Session = Depends(get_db),
):
    counterparties = _filtered_counterparties(db, q, type, category, entity_type)
    return templates.TemplateResponse(request, "counterparties/list.html", {
        "counterparties": counterparties, "q": q, "type": type,
        "category": category, "entity_type": entity_type,
        "cp_types": CP_TYPES, "cat_colors": CAT_COLORS, "entity_types": ENTITY_TYPES,
    })


@router.get("/search", response_class=HTMLResponse)
@login_required
async def search_counterparties(
    request: Request, q: str = "", type: str = "", category: str = "",
    entity_type: str = "",
    db: Session = Depends(get_db),
):
    """Живой поиск/фильтр — возвращает только строки таблицы (без каркаса страницы)
    для подстановки через fetch() без перезагрузки страницы."""
    counterparties = _filtered_counterparties(db, q, type, category, entity_type)
    return templates.TemplateResponse(request, "counterparties/_rows.html", {
        "counterparties": counterparties,
        "cp_types": CP_TYPES, "cat_colors": CAT_COLORS, "entity_types": ENTITY_TYPES,
    })


@router.post("/recalc-categories")
@role_required("manager")
async def recalc_categories(request: Request, db: Session = Depends(get_db)):
    from sqlalchemy.orm import joinedload
    # joinedload подгружает заказы одним запросом (без N+1 на cp.orders)
    cps = db.query(Counterparty).options(joinedload(Counterparty.orders)).filter(
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
        "vehicles_initial": [],
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
    signatory: str = Form(default=""),
    type: str = Form(default="client"),
    entity_type: str = Form(default="ooo"),
    bank_name: str = Form(default=""),
    bank_account: str = Form(default=""),
    bank_bik: str = Form(default=""),
    bank_corr_account: str = Form(default=""),
    notes: str = Form(default=""),
    payment_delay_days: int = Form(default=2),
    payment_delay_type: str = Form(default="banking"),
    default_discount_pct: float = Form(default=0.0),
    tg_chat_id: str = Form(default=""),
    tg_chat_id_hidden: str = Form(default=""),
    tg_notify_enabled: str = Form(default=""),
    vehicles_json: str = Form(default="[]"),
    db: Session = Depends(get_db),
):
    resolved_entity = entity_type if entity_type in ENTITY_TYPES else "ooo"
    if resolved_entity == "ip" and not signatory.strip():
        signatory = _generate_signatory(contact_person or name)
    effective_tg = (tg_chat_id.strip() or tg_chat_id_hidden.strip()) or None
    cp = Counterparty(
        name=name, trade_name=_clean(trade_name), short_name=_clean(short_name),
        inn=_validate_inn(inn) or None, kpp=_validate_kpp(kpp) or None, ogrn=_validate_ogrn(ogrn) or None,
        legal_address=_clean(legal_address), actual_address=_clean(actual_address),
        phone=_clean(phone), email=_clean(email), contact_person=_clean(contact_person),
        signatory=_clean(signatory),
        type=type,
        entity_type=resolved_entity,
        bank_name=_clean(bank_name), bank_account=_clean(bank_account), bank_bik=_clean(bank_bik),
        bank_corr_account=_clean(bank_corr_account), notes=_clean(notes),
        payment_delay_days=max(0, payment_delay_days),
        payment_delay_type=payment_delay_type if payment_delay_type in ("banking", "calendar") else "banking",
        default_discount_pct=min(max(default_discount_pct, 0.0), 100.0),
        tg_chat_id=effective_tg,
        tg_notify_enabled=bool(tg_notify_enabled),
    )
    db.add(cp)
    db.flush()
    _sync_carrier_vehicles(db, cp, vehicles_json)
    db.commit()
    threading.Thread(target=_push_cp_bg, args=(cp.id,), daemon=True).start()
    return RedirectResponse(url="/counterparties", status_code=302)


@router.get("/check-inn", response_class=JSONResponse)
@login_required
async def check_inn(request: Request, inn: str = "", exclude_id: int = 0, db: Session = Depends(get_db)):
    """AJAX: проверяет существует ли КА с таким ИНН. exclude_id — текущий КА при редактировании."""
    inn = _clean_digits(inn)
    if len(inn) not in (10, 12):
        return JSONResponse({"duplicate": False})
    q = db.query(Counterparty).filter(
        Counterparty.inn == inn,
        Counterparty.is_active == True,
    )
    if exclude_id:
        q = q.filter(Counterparty.id != exclude_id)
    existing = q.first()
    if existing:
        return JSONResponse({
            "duplicate": True,
            "id": existing.id,
            "name": existing.trade_name or existing.name,
        })
    return JSONResponse({"duplicate": False})


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


_EGRUL_STATUS_LABELS = {
    "ACTIVE":        "Действует",
    "LIQUIDATING":   "В процессе ликвидации",
    "LIQUIDATED":    "Ликвидирована",
    "BANKRUPT":      "Банкротство",
    "REORGANIZING":  "Реорганизация",
}
_EGRUL_STATUS_COLORS = {
    "ACTIVE":        "success",
    "LIQUIDATING":   "warning",
    "LIQUIDATED":    "danger",
    "BANKRUPT":      "danger",
    "REORGANIZING":  "warning",
}


@router.post("/{cp_id}/check-egrul", response_class=JSONResponse)
@login_required
async def check_egrul(request: Request, cp_id: int, db: Session = Depends(get_db)):
    """AJAX: запрашивает статус КА в ЕГРЮЛ через DaData, сохраняет в БД."""
    from datetime import datetime as _dt
    cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first()
    if not cp:
        return JSONResponse({"error": "КА не найден"}, status_code=404)
    if not cp.inn:
        return JSONResponse({"error": "У КА не указан ИНН"}, status_code=400)

    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        try:
            resp = await client.post(
                "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party",
                headers=DADATA_HEADERS,
                json={"query": cp.inn},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            return JSONResponse({"error": f"Ошибка запроса к DaData: {e}"}, status_code=502)

    suggestions = data.get("suggestions", [])
    if not suggestions:
        return JSONResponse({"error": "Компания не найдена по ИНН"}, status_code=404)

    status_code = suggestions[0]["data"].get("state", {}).get("status", "ACTIVE")
    cp.egrul_status = status_code
    cp.egrul_checked_at = _dt.utcnow()
    db.commit()

    return JSONResponse({
        "status": status_code,
        "label": _EGRUL_STATUS_LABELS.get(status_code, status_code),
        "color": _EGRUL_STATUS_COLORS.get(status_code, "secondary"),
        "checked_at": cp.egrul_checked_at.strftime("%d.%m.%Y %H:%M"),
    })


@router.post("/{cp_id}/refresh-bitrix", response_class=JSONResponse)
@login_required
async def refresh_bitrix_requisites(request: Request, cp_id: int, db: Session = Depends(get_db)):
    """AJAX: вручную дозаливает пустые реквизиты контрагента из Bitrix24 + DaData
    (та же логика, что и фоновый поллинг, но по кнопке — не дожидаясь цикла)."""
    from datetime import datetime as _dt
    from app.models import CompanySettings
    from app.services.bitrix_client import (
        BitrixError, get_bitrix_client, refresh_counterparty_requisites,
    )
    cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first()
    if not cp:
        return JSONResponse({"error": "КА не найден"}, status_code=404)
    if not cp.external_id_bitrix:
        return JSONResponse({"error": "КА не связан со сделкой Bitrix24"}, status_code=400)

    company = db.query(CompanySettings).first()
    client = get_bitrix_client(company)
    if not client:
        return JSONResponse({"error": "Bitrix24 не настроен или выключен в Настройках"}, status_code=503)

    try:
        with client:
            updated = refresh_counterparty_requisites(client, cp)
    except BitrixError as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    if updated:
        cp.synced_to_bitrix_at = _dt.now()
        db.commit()

    return JSONResponse({
        "updated": updated,
        "message": "Реквизиты обновлены из Bitrix24" if updated
                   else "Новых данных в Bitrix24 нет — все поля уже заполнены",
        "inn": cp.inn or "",
        "kpp": cp.kpp or "",
        "ogrn": cp.ogrn or "",
        "bank_name": cp.bank_name or "",
        "bank_bik": cp.bank_bik or "",
        "bank_account": cp.bank_account or "",
        "bank_corr_account": cp.bank_corr_account or "",
    })


@router.get("/{cp_id}", response_class=HTMLResponse)
@login_required
async def view_counterparty(request: Request, cp_id: int, db: Session = Depends(get_db)):
    cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first()
    if not cp:
        return RedirectResponse(url="/counterparties", status_code=302)

    # Статистика
    from datetime import date as _date, timedelta as _td
    active_orders = [o for o in cp.orders if o.status not in ("cancelled",)]
    # Выручка — только фактически оплаченные/отгруженные заказы (без confirmed)
    total_revenue = sum(o.total_amount for o in cp.orders if o.status in REVENUE_STATUSES)
    open_invoices = [inv for inv in cp.invoices if inv.status in ("issued", "overdue")]
    open_debt = sum(inv.total_amount for inv in open_invoices)

    # Дней с последнего заказа (без учёта отменённых)
    last_order_dates = [o.date for o in cp.orders if o.status != "cancelled" and o.date]
    last_order_date = max(last_order_dates) if last_order_dates else None
    dormant_days = (_date.today() - last_order_date).days if last_order_date else None
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

    from app.routers.files import files_for, FILE_TYPES
    files = files_for(db, "counterparty", cp_id)

    contacts = (
        db.query(ContactPerson)
        .filter(ContactPerson.counterparty_id == cp_id, ContactPerson.is_active == True)
        .order_by(ContactPerson.is_primary.desc(), ContactPerson.full_name)
        .all()
    )

    return templates.TemplateResponse(request, "counterparties/detail.html", {
        "cp": cp,
        "cp_types": CP_TYPES,
        "entity_types": ENTITY_TYPES,
        "cat_colors": CAT_COLORS,
        "files": files,
        "file_types": FILE_TYPES["counterparty"],
        "contacts": contacts,
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
        "dormant_days": dormant_days,
        "egrul_status_labels": _EGRUL_STATUS_LABELS,
        "egrul_status_colors": _EGRUL_STATUS_COLORS,
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
    vehicles_initial = [
        {"driver_name": v.driver_name or "", "vehicle_plate": v.vehicle_plate or "", "vehicle_type": v.vehicle_type or ""}
        for v in cp.vehicles
    ]
    return templates.TemplateResponse(request, "counterparties/form.html", {
        "cp": cp, "cp_types": CP_TYPES, "entity_types": ENTITY_TYPES, "errors": [],
        "vehicles_initial": vehicles_initial,
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
    signatory: str = Form(default=""),
    type: str = Form(default="client"),
    entity_type: str = Form(default="ooo"),
    bank_name: str = Form(default=""),
    bank_account: str = Form(default=""),
    bank_bik: str = Form(default=""),
    bank_corr_account: str = Form(default=""),
    notes: str = Form(default=""),
    payment_delay_days: int = Form(default=2),
    payment_delay_type: str = Form(default="banking"),
    default_discount_pct: float = Form(default=0.0),
    tg_chat_id: str = Form(default=""),
    tg_chat_id_hidden: str = Form(default=""),
    tg_notify_enabled: str = Form(default=""),
    vehicles_json: str = Form(default="[]"),
    db: Session = Depends(get_db),
):
    cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first()
    if cp:
        resolved_entity = entity_type if entity_type in ENTITY_TYPES else "ooo"
        if resolved_entity == "ip" and not signatory.strip():
            signatory = _generate_signatory(contact_person or name)
        effective_tg = (tg_chat_id.strip() or tg_chat_id_hidden.strip()) or None
        cp.name = name; cp.trade_name = _clean(trade_name); cp.short_name = _clean(short_name)
        cp.inn = _validate_inn(inn) or None; cp.kpp = _validate_kpp(kpp) or None
        cp.ogrn = _validate_ogrn(ogrn) or None
        cp.legal_address = _clean(legal_address); cp.actual_address = _clean(actual_address)
        cp.phone = _clean(phone); cp.email = _clean(email); cp.contact_person = _clean(contact_person)
        cp.signatory = _clean(signatory)
        cp.type = type
        cp.entity_type = resolved_entity
        cp.bank_name = _clean(bank_name); cp.bank_account = _clean(bank_account); cp.bank_bik = _clean(bank_bik)
        cp.bank_corr_account = _clean(bank_corr_account); cp.notes = _clean(notes)
        cp.payment_delay_days = max(0, payment_delay_days)
        cp.payment_delay_type = payment_delay_type if payment_delay_type in ("banking", "calendar") else "banking"
        cp.default_discount_pct = min(max(default_discount_pct, 0.0), 100.0)
        cp.tg_chat_id = effective_tg
        cp.tg_notify_enabled = bool(tg_notify_enabled)
        _sync_carrier_vehicles(db, cp, vehicles_json)
        db.commit()
        threading.Thread(target=_push_cp_bg, args=(cp_id,), daemon=True).start()
    return RedirectResponse(url=f"/counterparties/{cp_id}", status_code=302)


@router.post("/{cp_id}/delete")
@login_required
async def delete_counterparty(request: Request, cp_id: int, db: Session = Depends(get_db)):
    cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first()
    if cp:
        cp.is_active = False
        db.commit()
    return RedirectResponse(url="/counterparties", status_code=302)


# ── Контактные лица (ЛПР) ────────────────────────────────────────────────────

def _clear_other_primary(db: Session, cp_id: int, keep_id: int | None = None) -> None:
    """Снимает флаг is_primary со всех контактов КА, кроме keep_id."""
    q = db.query(ContactPerson).filter(
        ContactPerson.counterparty_id == cp_id,
        ContactPerson.is_primary == True,
    )
    if keep_id:
        q = q.filter(ContactPerson.id != keep_id)
    for c in q.all():
        c.is_primary = False


@router.post("/{cp_id}/contacts")
@role_required("manager")
async def add_contact(
    request: Request, cp_id: int,
    full_name: str = Form(...),
    post: str = Form(default=""),
    phone: str = Form(default=""),
    email: str = Form(default=""),
    telegram: str = Form(default=""),
    whatsapp: str = Form(default=""),
    is_primary: str = Form(default=""),
    db: Session = Depends(get_db),
):
    cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first()
    if not cp or not full_name.strip():
        return RedirectResponse(url=f"/counterparties/{cp_id}#tab-contacts", status_code=302)
    primary = bool(is_primary)
    if primary:
        _clear_other_primary(db, cp_id)
    c = ContactPerson(
        counterparty_id=cp_id,
        full_name=full_name.strip()[:200],
        post=post.strip()[:150] or None,
        phone=phone.strip()[:100] or None,
        email=email.strip()[:150] or None,
        telegram=telegram.strip()[:150] or None,
        whatsapp=whatsapp.strip()[:150] or None,
        is_primary=primary,
        is_active=True,
    )
    db.add(c)
    log_action(db, "counterparty", cp_id, "updated",
               request.session.get("user_id"), f"Добавлен контакт: {c.full_name}")
    db.commit()
    return RedirectResponse(url=f"/counterparties/{cp_id}#tab-contacts", status_code=302)


@router.post("/{cp_id}/contacts/{contact_id}/edit")
@role_required("manager")
async def edit_contact(
    request: Request, cp_id: int, contact_id: int,
    full_name: str = Form(...),
    post: str = Form(default=""),
    phone: str = Form(default=""),
    email: str = Form(default=""),
    telegram: str = Form(default=""),
    whatsapp: str = Form(default=""),
    is_primary: str = Form(default=""),
    db: Session = Depends(get_db),
):
    c = db.query(ContactPerson).filter(
        ContactPerson.id == contact_id, ContactPerson.counterparty_id == cp_id
    ).first()
    if not c or not full_name.strip():
        return RedirectResponse(url=f"/counterparties/{cp_id}#tab-contacts", status_code=302)
    primary = bool(is_primary)
    if primary:
        _clear_other_primary(db, cp_id, keep_id=contact_id)
    c.full_name = full_name.strip()[:200]
    c.post = post.strip()[:150] or None
    c.phone = phone.strip()[:100] or None
    c.email = email.strip()[:150] or None
    c.telegram = telegram.strip()[:150] or None
    c.whatsapp = whatsapp.strip()[:150] or None
    c.is_primary = primary
    db.commit()
    return RedirectResponse(url=f"/counterparties/{cp_id}#tab-contacts", status_code=302)


@router.post("/{cp_id}/contacts/{contact_id}/delete")
@role_required("manager")
async def delete_contact(request: Request, cp_id: int, contact_id: int, db: Session = Depends(get_db)):
    c = db.query(ContactPerson).filter(
        ContactPerson.id == contact_id, ContactPerson.counterparty_id == cp_id
    ).first()
    if c:
        # Мягкое удаление — контакт может быть привязан к визитам (FieldVisit.contact_id)
        c.is_active = False
        c.is_primary = False
        log_action(db, "counterparty", cp_id, "updated",
                   request.session.get("user_id"), f"Удалён контакт: {c.full_name}")
        db.commit()
    return RedirectResponse(url=f"/counterparties/{cp_id}#tab-contacts", status_code=302)

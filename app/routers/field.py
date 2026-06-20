"""Мобильное приложение торгового представителя («Поле»).

Презейл-модель: торгпред сам набирает себе точки на день (из базы прозвона),
строит маршрут, объезжает, на каждой точке фиксирует визит — результат, ЛПР,
комментарий, фото и следующий шаг. Результат визита переносится в статус лида
и логируется в общую ленту контактов (LeadCall) — история точки единая.

Карта переиспользует ту же базу SalesLead и геокодинг, что и /leads.
"""
import json
import math
import os
import uuid
from datetime import datetime, date

from fastapi import APIRouter, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.database import get_db, hash_password, verify_password
from app.auth import login_required, role_required
from app.models import SalesLead, LeadCall, FieldVisit, ContactPerson, User, Counterparty
from app.routers.leads import LEAD_STATUSES, STATUS_COLORS

router = APIRouter(prefix="/field", tags=["field"])
templates = Jinja2Templates(directory="app/templates")

# Каталог для фото визитов — вне /static, доступ только через авторизованный эндпоинт
PHOTO_DIR = "uploads/field_photos"
PHOTO_URL = "/field/photos"
ALLOWED_PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
MAX_PHOTO_BYTES = 8 * 1024 * 1024  # 8 МБ на файл

# Результаты визита — крупные кнопки на экране визита (подмножество статусов лида).
FIELD_RESULTS = [
    ("interested", "Заинтересован", "bi-hand-thumbs-up"),
    ("thinking",   "Думает",         "bi-hourglass-split"),
    ("deal",       "Договор / продажа", "bi-trophy"),
    ("callback",   "Зайти ещё",      "bi-arrow-repeat"),
    ("no_answer",  "Не застал",      "bi-door-closed"),
    ("refused",    "Отказ",          "bi-hand-thumbs-down"),
    ("invalid",    "Закрыто / невалид", "bi-x-octagon"),
]


# ── Вспомогательные ──────────────────────────────────────────────────────────

def _uid(request: Request) -> int | None:
    return request.session.get("user_id")


def _haversine_m(lat1, lng1, lat2, lng2) -> int | None:
    """Расстояние между двумя точками в метрах (для отчёта, не для блокировки)."""
    if None in (lat1, lng1, lat2, lng2):
        return None
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


def _contact_dict(c: ContactPerson) -> dict:
    return {
        "id": c.id, "full_name": c.full_name, "post": c.post or "",
        "phone": c.phone or "", "email": c.email or "",
        "telegram": c.telegram or "", "whatsapp": c.whatsapp or "",
        "is_primary": c.is_primary,
    }


def _timeline(lead: SalesLead, db: Session) -> list[dict]:
    """Единая лента контактов по точке: звонки (LeadCall) + визиты (FieldVisit)."""
    items = []
    for c in lead.calls:  # уже отсортированы desc по created_at
        items.append({
            "kind": "call",
            "when": c.created_at,
            "status": c.status,
            "label": LEAD_STATUSES.get(c.status, c.status or "—"),
            "color": STATUS_COLORS.get(c.status, "secondary"),
            "comment": c.comment or "",
            "user": c.user.full_name if c.user else "—",
            "photos": [],
        })
    visits = db.query(FieldVisit).filter(
        FieldVisit.lead_id == lead.id, FieldVisit.status == "done"
    ).options(joinedload(FieldVisit.rep)).order_by(FieldVisit.checkin_at.desc()).all()
    for v in visits:
        try:
            photos = json.loads(v.photos) if v.photos else []
        except (ValueError, TypeError):
            photos = []
        items.append({
            "kind": "visit",
            "when": v.checkin_at or v.created_at,
            "status": v.result,
            "label": LEAD_STATUSES.get(v.result, v.result or "Визит"),
            "color": STATUS_COLORS.get(v.result, "secondary"),
            "comment": v.comment or "",
            "next_step": v.next_step or "",
            "user": v.rep.full_name if v.rep else "—",
            "photos": photos,
        })
    items.sort(key=lambda x: x["when"] or datetime.min, reverse=True)
    return items


# ── Раздача фото визитов (только авторизованным) ─────────────────────────────

@router.get("/photos/{filename}")
@login_required
async def serve_photo(request: Request, filename: str):
    """Отдаёт фото визита. Доступно только авторизованным пользователям."""
    import re
    from fastapi.responses import FileResponse
    # Разрешаем только безопасные имена файлов (hex + расширение)
    if not re.match(r'^[0-9a-f]{32}\.(jpg|jpeg|png|webp|heic)$', filename, re.IGNORECASE):
        from fastapi.responses import HTMLResponse as _HTML
        return _HTML("404", status_code=404)
    path = os.path.join(PHOTO_DIR, filename)
    if not os.path.exists(path):
        # Backward compat: фото, загруженные до миграции, лежат в app/static/field_photos/
        legacy = os.path.join("app/static/field_photos", filename)
        if os.path.exists(legacy):
            path = legacy
        else:
            from fastapi.responses import HTMLResponse as _HTML
            return _HTML("404", status_code=404)
    return FileResponse(path)


# ── «Мой день» ───────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
@login_required
async def my_day(request: Request, date_str: str = "", db: Session = Depends(get_db)):
    uid = _uid(request)
    try:
        day = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        day = date.today()

    visits = db.query(FieldVisit).filter(
        FieldVisit.rep_id == uid,
        FieldVisit.planned_date == day,
    ).options(joinedload(FieldVisit.lead), joinedload(FieldVisit.contact))\
     .order_by(FieldVisit.status, FieldVisit.id).all()

    done = sum(1 for v in visits if v.status == "done")
    rows = []
    for v in visits:
        l = v.lead
        if not l:
            continue
        rows.append({
            "visit": v, "lead": l,
            "color": STATUS_COLORS.get(v.result or l.call_status, "secondary"),
            "result_label": LEAD_STATUSES.get(v.result, "") if v.result else "",
        })

    # Точки с координатами — для кнопки «маршрут в Яндекс.Картах»
    route_pts = [(v.lead.lat, v.lead.lng) for v in visits
                 if v.lead and v.lead.lat and v.lead.lng]

    return templates.TemplateResponse(request, "field/day.html", {
        "rows": rows, "day": day, "done": done, "total": len(visits),
        "route_pts": route_pts, "statuses": LEAD_STATUSES,
        "is_today": day == date.today(),
    })


# ── Планирование: торгпред сам набирает точки ────────────────────────────────

@router.get("/plan", response_class=HTMLResponse)
@login_required
async def plan(request: Request, date_str: str = "", db: Session = Depends(get_db)):
    """Набор точек на день на карте (как /leads/map): тап по точке — в план/из плана,
    либо выделение зоны — добавить все точки внутри."""
    uid = _uid(request)
    try:
        day = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        day = date.today()
    total = db.query(SalesLead).filter(
        SalesLead.is_active == True, SalesLead.lat.isnot(None)).count()
    planned = db.query(FieldVisit).filter(
        FieldVisit.rep_id == uid, FieldVisit.planned_date == day).count()
    return templates.TemplateResponse(request, "field/plan.html", {
        "day": day, "total": total, "planned": planned,
        "statuses": LEAD_STATUSES, "status_colors": STATUS_COLORS,
    })


@router.get("/plan/data", response_class=JSONResponse)
@login_required
async def plan_data(request: Request, date_str: str = "", db: Session = Depends(get_db)):
    """Все геокодированные точки для набора плана + флаг «уже в плане на этот день»."""
    uid = _uid(request)
    try:
        day = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        day = date.today()
    planned_ids = {r[0] for r in db.query(FieldVisit.lead_id).filter(
        FieldVisit.rep_id == uid, FieldVisit.planned_date == day).all()}
    rows = db.query(SalesLead).filter(
        SalesLead.is_active == True,
        SalesLead.lat.isnot(None), SalesLead.lng.isnot(None),
    ).all()
    return [{
        "id": l.id, "name": l.name, "lat": l.lat, "lng": l.lng,
        "status": l.call_status, "phone": l.phone or "",
        "category": l.category or "", "address": l.address or "",
        "city": l.city or "", "district": l.district or "",
        "mine": l.assigned_to_id == uid,
        "planned": l.id in planned_ids,
    } for l in rows]


@router.post("/plan/bulk_add", response_class=JSONResponse)
@login_required
async def plan_bulk_add(request: Request, db: Session = Depends(get_db)):
    """Добавляет в план на день все переданные точки (выделение зоны на карте)."""
    uid = _uid(request)
    form = await request.form()
    try:
        day = date.fromisoformat(form.get("date_str") or "") if form.get("date_str") else date.today()
    except ValueError:
        day = date.today()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    if not ids:
        return JSONResponse({"ok": True, "added": 0})
    existing = {r[0] for r in db.query(FieldVisit.lead_id).filter(
        FieldVisit.rep_id == uid, FieldVisit.planned_date == day,
        FieldVisit.lead_id.in_(ids)).all()}
    leads = db.query(SalesLead).filter(SalesLead.id.in_(ids), SalesLead.is_active == True).all()
    added = 0
    for l in leads:
        if l.id in existing:
            continue
        db.add(FieldVisit(lead_id=l.id, rep_id=uid, planned_date=day, status="planned"))
        if l.assigned_to_id is None:
            l.assigned_to_id = uid
        added += 1
    db.commit()
    return JSONResponse({"ok": True, "added": added})


@router.post("/plan/add", response_class=JSONResponse)
@login_required
async def plan_add(request: Request, lead_id: int = Form(...),
                   date_str: str = Form(default=""), db: Session = Depends(get_db)):
    uid = _uid(request)
    try:
        day = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        day = date.today()
    lead = db.query(SalesLead).filter(SalesLead.id == lead_id, SalesLead.is_active == True).first()
    if not lead:
        return JSONResponse({"error": "not found"}, status_code=404)
    # Не дублируем визит на тот же день
    exists = db.query(FieldVisit).filter(
        FieldVisit.rep_id == uid, FieldVisit.lead_id == lead_id,
        FieldVisit.planned_date == day, FieldVisit.status == "planned",
    ).first()
    if not exists:
        db.add(FieldVisit(lead_id=lead_id, rep_id=uid, planned_date=day, status="planned"))
        # Закрепляем точку за торгпредом
        if lead.assigned_to_id is None:
            lead.assigned_to_id = uid
        db.commit()
    return JSONResponse({"ok": True, "lead_id": lead_id})


@router.post("/plan/remove", response_class=JSONResponse)
@login_required
async def plan_remove(request: Request, lead_id: int = Form(...),
                      date_str: str = Form(default=""), db: Session = Depends(get_db)):
    uid = _uid(request)
    try:
        day = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        day = date.today()
    db.query(FieldVisit).filter(
        FieldVisit.rep_id == uid, FieldVisit.lead_id == lead_id,
        FieldVisit.planned_date == day, FieldVisit.status == "planned",
    ).delete()
    db.commit()
    return JSONResponse({"ok": True, "lead_id": lead_id})


# ── Создание новой точки торгпредом ─────────────────────────────────────────

# Типичные категории заведений для быстрого выбора
LEAD_CATEGORIES = [
    "Кофейня", "Кондитерская", "Пекарня", "Ресторан", "Кафе", "Бар",
    "Столовая", "Фастфуд", "Пиццерия", "Суши-бар", "Булочная",
    "Гостиница / Отель", "Офис / Бизнес-центр", "Супермаркет / Магазин",
    "Корпоративное питание", "Другое",
]


@router.get("/lead/new", response_class=HTMLResponse)
@login_required
async def new_lead_form(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "field/new_lead.html", {
        "categories": LEAD_CATEGORIES,
        "error": request.query_params.get("error", ""),
    })


@router.post("/lead/new")
@login_required
async def new_lead_save(
    request: Request,
    name: str = Form(...),
    category: str = Form(default=""),
    category_custom: str = Form(default=""),
    city: str = Form(default=""),
    address: str = Form(default=""),
    phone: str = Form(default=""),
    notes: str = Form(default=""),
    gps_lat: str = Form(default=""),
    gps_lng: str = Form(default=""),
    add_to_plan: str = Form(default=""),
    db: Session = Depends(get_db),
):
    uid = _uid(request)
    name = name.strip()[:300]
    if not name:
        return RedirectResponse(url="/field/lead/new?error=name", status_code=302)

    cat = (category_custom.strip() or category.strip())[:150] or None

    lat = lng = None
    try:
        lat = float(gps_lat) if gps_lat else None
        lng = float(gps_lng) if gps_lng else None
    except ValueError:
        pass

    lead = SalesLead(
        name=name,
        category=cat,
        city=city.strip()[:150] or None,
        address=address.strip()[:500] or None,
        phone=phone.strip()[:150] or None,
        notes=notes.strip() or None,
        lat=lat,
        lng=lng,
        call_status="new",
        assigned_to_id=uid,
        source_file="field_rep",
        is_active=True,
    )
    db.add(lead)
    db.flush()  # получаем lead.id

    if add_to_plan == "on":
        db.add(FieldVisit(lead_id=lead.id, rep_id=uid,
                          planned_date=date.today(), status="planned"))

    db.commit()
    return RedirectResponse(url=f"/field/lead/{lead.id}?created=1", status_code=302)


# ── Карточка точки + визит ───────────────────────────────────────────────────

@router.get("/lead/{lead_id}", response_class=HTMLResponse)
@login_required
async def lead_card(request: Request, lead_id: int,
                    visit_id: int = 0, db: Session = Depends(get_db)):
    uid = _uid(request)
    lead = db.query(SalesLead).filter(SalesLead.id == lead_id).first()
    if not lead:
        return RedirectResponse(url="/field/", status_code=302)

    contacts = db.query(ContactPerson).filter(
        ContactPerson.lead_id == lead_id, ContactPerson.is_active == True
    ).order_by(ContactPerson.is_primary.desc(), ContactPerson.id).all()
    # Если ЛПР ещё нет, но в импорте было текстовое контактное лицо — показываем подсказкой
    legacy_contact = lead.contact_person if (lead.contact_person and not contacts) else ""

    timeline = _timeline(lead, db)

    visit = None
    if visit_id:
        visit = db.query(FieldVisit).filter(
            FieldVisit.id == visit_id, FieldVisit.rep_id == uid
        ).first()

    return templates.TemplateResponse(request, "field/lead.html", {
        "lead": lead, "contacts": contacts, "legacy_contact": legacy_contact,
        "timeline": timeline, "visit": visit,
        "results": FIELD_RESULTS, "statuses": LEAD_STATUSES, "status_colors": STATUS_COLORS,
        "photo_url": PHOTO_URL,
    })


@router.post("/lead/{lead_id}/visit")
@login_required
async def save_visit(
    request: Request, lead_id: int,
    result: str = Form(...),
    comment: str = Form(default=""),
    next_step: str = Form(default=""),
    next_visit_at: str = Form(default=""),
    contact_id: str = Form(default=""),
    visit_id: str = Form(default=""),
    gps_lat: str = Form(default=""),
    gps_lng: str = Form(default=""),
    photos: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    uid = _uid(request)
    lead = db.query(SalesLead).filter(SalesLead.id == lead_id).first()
    if not lead:
        return RedirectResponse(url="/field/", status_code=302)
    if result not in LEAD_STATUSES:
        result = "no_answer"

    # Существующий запланированный визит (если открыли из «Моего дня») — закрываем его,
    # иначе создаём новый («свободный визит»).
    visit = None
    if visit_id.isdigit():
        visit = db.query(FieldVisit).filter(
            FieldVisit.id == int(visit_id), FieldVisit.rep_id == uid
        ).first()
    if visit is None:
        visit = FieldVisit(lead_id=lead_id, rep_id=uid,
                           planned_date=date.today(), status="planned")
        db.add(visit)

    # GPS (необязательно) + расстояние до точки для отчёта
    lat = lng = None
    try:
        lat = float(gps_lat) if gps_lat else None
        lng = float(gps_lng) if gps_lng else None
    except ValueError:
        pass
    visit.gps_lat, visit.gps_lng = lat, lng
    visit.gps_distance_m = _haversine_m(lat, lng, lead.lat, lead.lng)

    # Фото
    os.makedirs(PHOTO_DIR, exist_ok=True)
    saved = []
    try:
        saved = json.loads(visit.photos) if visit.photos else []
    except (ValueError, TypeError):
        saved = []
    for f in photos or []:
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_PHOTO_EXT:
            continue
        data = await f.read()
        if not data or len(data) > MAX_PHOTO_BYTES:
            continue
        fname = f"{uuid.uuid4().hex}{ext}"
        with open(os.path.join(PHOTO_DIR, fname), "wb") as out:
            out.write(data)
        saved.append(f"{PHOTO_URL}/{fname}")

    now = datetime.now()
    visit.status = "done"
    visit.result = result
    visit.comment = comment or None
    visit.next_step = next_step or None
    visit.contact_id = int(contact_id) if contact_id.isdigit() else None
    visit.photos = json.dumps(saved, ensure_ascii=False) if saved else None
    if not visit.checkin_at:
        visit.checkin_at = now
    visit.checkout_at = now
    try:
        visit.next_visit_at = date.fromisoformat(next_visit_at) if next_visit_at else None
    except ValueError:
        visit.next_visit_at = None

    # Переносим результат в лид + общую ленту контактов
    lead.call_status = result
    lead.last_call_at = now
    lead.call_count = (lead.call_count or 0) + 1
    if lead.assigned_to_id is None:
        lead.assigned_to_id = uid
    if visit.next_visit_at:
        lead.callback_at = visit.next_visit_at
    db.add(LeadCall(lead_id=lead.id, user_id=uid, status=result,
                    comment=(("[Визит] " + comment) if comment else "[Визит]")))
    db.commit()

    return RedirectResponse(url=f"/field/lead/{lead_id}?saved=1", status_code=302)


# ── ЛПР (контактные лица) ────────────────────────────────────────────────────

@router.post("/lead/{lead_id}/contact")
@login_required
async def save_contact(
    request: Request, lead_id: int,
    contact_id: str = Form(default=""),
    full_name: str = Form(...),
    post: str = Form(default=""),
    phone: str = Form(default=""),
    email: str = Form(default=""),
    telegram: str = Form(default=""),
    whatsapp: str = Form(default=""),
    is_primary: str = Form(default=""),
    db: Session = Depends(get_db),
):
    lead = db.query(SalesLead).filter(SalesLead.id == lead_id).first()
    if not lead or not full_name.strip():
        return RedirectResponse(url=f"/field/lead/{lead_id}", status_code=302)

    c = None
    if contact_id.isdigit():
        c = db.query(ContactPerson).filter(
            ContactPerson.id == int(contact_id), ContactPerson.lead_id == lead_id
        ).first()
    if c is None:
        c = ContactPerson(lead_id=lead_id, counterparty_id=lead.converted_cp_id)
        db.add(c)
    c.full_name = full_name.strip()[:200]
    c.post = post.strip()[:150] or None
    c.phone = phone.strip()[:100] or None
    c.email = email.strip()[:150] or None
    c.telegram = telegram.strip()[:150] or None
    c.whatsapp = whatsapp.strip()[:150] or None
    primary = is_primary == "on"
    if primary:
        # снимаем «основной» с остальных
        db.query(ContactPerson).filter(
            ContactPerson.lead_id == lead_id, ContactPerson.id != (c.id or 0)
        ).update({ContactPerson.is_primary: False})
    c.is_primary = primary
    db.commit()
    return RedirectResponse(url=f"/field/lead/{lead_id}#contacts", status_code=302)


@router.post("/contact/{contact_id}/delete")
@login_required
async def delete_contact(request: Request, contact_id: int,
                         lead_id: int = Form(...), db: Session = Depends(get_db)):
    c = db.query(ContactPerson).filter(ContactPerson.id == contact_id).first()
    if c:
        c.is_active = False
        db.commit()
    return RedirectResponse(url=f"/field/lead/{lead_id}#contacts", status_code=302)


# ── Конвертация точки в контрагента ──────────────────────────────────────────

@router.post("/lead/{lead_id}/convert")
@login_required
async def convert(request: Request, lead_id: int, db: Session = Depends(get_db)):
    lead = db.query(SalesLead).filter(SalesLead.id == lead_id).first()
    if not lead:
        return RedirectResponse(url="/field/", status_code=302)
    if lead.converted_cp_id:
        return RedirectResponse(url=f"/counterparties/{lead.converted_cp_id}", status_code=302)
    cp = Counterparty(
        name=lead.name, short_name=lead.name[:100],
        phone=lead.phone, email=lead.email,
        actual_address=(f"{lead.city}, {lead.address}" if lead.city else lead.address) or None,
        contact_person=lead.contact_person, type="client",
        notes=f"Создан торгпредом из визита. Рубрика: {lead.category or '—'}.",
    )
    db.add(cp)
    db.flush()
    lead.converted_cp_id = cp.id
    # Переносим ЛПР на контрагента
    db.query(ContactPerson).filter(ContactPerson.lead_id == lead_id)\
        .update({ContactPerson.counterparty_id: cp.id})
    if lead.call_status not in ("deal",):
        lead.call_status = "interested"
    db.commit()
    return RedirectResponse(url=f"/counterparties/{cp.id}", status_code=302)


# ── Карта точек торгпреда ────────────────────────────────────────────────────

@router.get("/map", response_class=HTMLResponse)
@login_required
async def field_map(request: Request, db: Session = Depends(get_db)):
    uid = _uid(request)
    base = db.query(SalesLead).filter(
        SalesLead.is_active == True, SalesLead.assigned_to_id == uid)
    total = base.count()
    geocoded = base.filter(SalesLead.lat.isnot(None)).count()
    return templates.TemplateResponse(request, "field/map.html", {
        "total": total, "geocoded": geocoded,
        "statuses": LEAD_STATUSES, "status_colors": STATUS_COLORS,
    })


@router.get("/map/data", response_class=JSONResponse)
@login_required
async def field_map_data(request: Request, only: str = "", db: Session = Depends(get_db)):
    """Точки торгпреда для карты. only=today — только запланированные на сегодня."""
    uid = _uid(request)
    today_ids = set()
    if only == "today":
        today_ids = {r[0] for r in db.query(FieldVisit.lead_id).filter(
            FieldVisit.rep_id == uid, FieldVisit.planned_date == date.today()).all()}

    q = db.query(SalesLead).filter(
        SalesLead.is_active == True, SalesLead.assigned_to_id == uid,
        SalesLead.lat.isnot(None), SalesLead.lng.isnot(None),
    )
    rows = q.all()
    out = []
    for l in rows:
        if only == "today" and l.id not in today_ids:
            continue
        out.append({
            "id": l.id, "name": l.name, "lat": l.lat, "lng": l.lng,
            "status": l.call_status, "phone": l.phone or "",
            "category": l.category or "", "address": l.address or "",
            "city": l.city or "", "district": l.district or "",
            "planned_today": l.id in today_ids,
        })
    return out


# ── История визитов торгпреда ────────────────────────────────────────────────

@router.get("/history", response_class=HTMLResponse)
@login_required
async def history(request: Request, db: Session = Depends(get_db)):
    uid = _uid(request)
    visits = db.query(FieldVisit).filter(
        FieldVisit.rep_id == uid, FieldVisit.status == "done",
    ).options(joinedload(FieldVisit.lead)).order_by(FieldVisit.checkin_at.desc()).limit(200).all()
    return templates.TemplateResponse(request, "field/history.html", {
        "visits": visits, "statuses": LEAD_STATUSES, "status_colors": STATUS_COLORS,
    })


# ── Настройки торгпреда (мобильный дизайн) ───────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
@login_required
async def settings_page(request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == _uid(request)).first()
    return templates.TemplateResponse(request, "field/settings.html", {
        "user": user,
        "ok": request.query_params.get("ok", ""),
        "error": request.query_params.get("error", ""),
    })


@router.post("/settings/profile")
@login_required
async def settings_profile(request: Request, full_name: str = Form(...),
                           birthday: str = Form(default=""), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == _uid(request)).first()
    if user:
        if full_name.strip():
            user.full_name = full_name.strip()[:100]
            request.session["user_name"] = user.full_name
        try:
            user.birthday = date.fromisoformat(birthday) if birthday else None
        except ValueError:
            pass
        db.commit()
    return RedirectResponse(url="/field/settings?ok=profile", status_code=302)


@router.post("/settings/password")
@login_required
async def settings_password(request: Request,
                            current_password: str = Form(...),
                            new_password: str = Form(...),
                            confirm_password: str = Form(...),
                            db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == _uid(request)).first()
    if not user:
        return RedirectResponse(url="/field/settings?error=auth", status_code=302)
    if not verify_password(current_password, user.password_hash):
        return RedirectResponse(url="/field/settings?error=current", status_code=302)
    if len(new_password) < 8:
        return RedirectResponse(url="/field/settings?error=short", status_code=302)
    if new_password != confirm_password:
        return RedirectResponse(url="/field/settings?error=mismatch", status_code=302)
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.commit()
    return RedirectResponse(url="/field/settings?ok=password", status_code=302)


# ── Монитор для руководителя (десктоп) ───────────────────────────────────────

def _block_field_rep(request: Request):
    """Монитор — для руководителей. field_rep имеет тот же уровень, что manager,
    поэтому role_required его не отсекает — закрываем явно."""
    if request.session.get("user_role") == "field_rep":
        from app.auth import _403_HTML
        return HTMLResponse(_403_HTML, status_code=403)
    return None


@router.get("/monitor", response_class=HTMLResponse)
@role_required("manager")
async def monitor(request: Request, date_str: str = "", db: Session = Depends(get_db)):
    denied = _block_field_rep(request)
    if denied:
        return denied
    try:
        day = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        day = date.today()

    reps = db.query(User).filter(
        User.is_active == True, User.role == "field_rep"
    ).order_by(User.full_name).all()

    rows = []
    for r in reps:
        day_visits = db.query(FieldVisit).filter(
            FieldVisit.rep_id == r.id, FieldVisit.planned_date == day
        ).all()
        done = [v for v in day_visits if v.status == "done"]
        last = db.query(FieldVisit).filter(
            FieldVisit.rep_id == r.id, FieldVisit.status == "done"
        ).order_by(FieldVisit.checkin_at.desc()).first()
        rows.append({
            "rep": r,
            "planned": len(day_visits),
            "done": len(done),
            "left": len(day_visits) - len(done),
            "deals": sum(1 for v in done if v.result == "deal"),
            "interested": sum(1 for v in done if v.result in ("interested", "thinking")),
            "last_at": last.checkin_at if last else None,
        })

    totals = {
        "planned": sum(x["planned"] for x in rows),
        "done": sum(x["done"] for x in rows),
        "deals": sum(x["deals"] for x in rows),
    }
    return templates.TemplateResponse(request, "field/monitor.html", {
        "rows": rows, "day": day, "totals": totals,
        "statuses": LEAD_STATUSES, "status_colors": STATUS_COLORS,
    })


@router.get("/monitor/data", response_class=JSONResponse)
@role_required("manager")
async def monitor_data(request: Request, date_str: str = "", db: Session = Depends(get_db)):
    """Все запланированные на день точки всех торгпредов — для карты руководителя."""
    if request.session.get("user_role") == "field_rep":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        day = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        day = date.today()
    visits = db.query(FieldVisit).filter(
        FieldVisit.planned_date == day
    ).options(joinedload(FieldVisit.lead), joinedload(FieldVisit.rep)).all()
    out = []
    for v in visits:
        l = v.lead
        if not l or not l.lat or not l.lng:
            continue
        out.append({
            "id": l.id, "name": l.name, "lat": l.lat, "lng": l.lng,
            "status": v.result or l.call_status, "done": v.status == "done",
            "rep": v.rep.full_name if v.rep else "—",
            "rep_id": v.rep_id,
        })
    return out

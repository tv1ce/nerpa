import secrets
import time
from collections import defaultdict
from datetime import date, datetime

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import login_required
from app.models import (
    HrEmployee, HrRecord, HrVacancy, HrPosition, HrSurvey, HrSurveyToken, HR_SECTIONS,
)

router = APIRouter(prefix="/hr", tags=["hr"])
templates = Jinja2Templates(directory="app/templates")

SECTION_META = {
    "personal":      {"icon": "👨", "label": "Личностный профиль сотрудника"},
    "complaints":    {"icon": "⁉️", "label": "С какой дичью вам приходится сталкиваться каждый день?"},
    "achievements":  {"icon": "🏅", "label": "Достижения"},
    "enps":          {"icon": "📣", "label": "eNPS"},
    "enps_managers": {"icon": "📣", "label": "eNPS Руководителей"},
    "metrics":       {"icon": "📈", "label": "Метрика сотрудников"},
    "gravity":       {"icon": "🧲", "label": "Гравитация и антигравитация"},
}

# Вопросы разделов — используются и в HR-форме, и в публичном опросе.
QUESTIONS = {
    "personal_1": "За прошедший месяц: что из сделанного вами здесь дало ощущение реального результата и ценности для компании?",
    "personal_2": "Был ли в этом месяце момент, когда вам не хватило коммуникации, обратной связи или решений со стороны руководства?",
    "complaints": "С какой дичью вам приходится сталкиваться каждый день?",
    "achievements": "Достижения за период (по одному на строку).",
    "enps_1": "По шкале от 0 до 10, с какой вероятностью вы порекомендуете компанию как отличное место работы?",
    "enps_2": "Пожалуйста, кратко объясните, почему вы поставили такую оценку.",
    "enps_mgr_1": "Оцените, насколько вам комфортно работать и коммуницировать со своим руководителем (по шкале от 0 до 10)?",
    "enps_mgr_2": "Что именно ваш руководитель делает хорошо, а что стоило бы изменить или улучшить в его стиле управления?",
    "metrics": "Метрика сотрудника за период.",
    "gravity": "Гравитация и антигравитация — что притягивает и что отталкивает в работе.",
}


# ── Период ────────────────────────────────────────────────────────────────────

def _period_from_str(raw: str | None) -> date:
    """Парсит период вида YYYY-MM в дату (1-е число месяца). По умолчанию — текущий месяц."""
    if raw:
        try:
            year, month = raw.split("-")
            return date(int(year), int(month), 1)
        except (ValueError, TypeError):
            pass
    today = date.today()
    return date(today.year, today.month, 1)


def _period_str(d: date) -> str:
    return d.strftime("%Y-%m")


def _period_label(d: date) -> str:
    months = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
              "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
    return f"{months[d.month]} {d.year}"


def _shift_period(d: date, delta: int) -> date:
    month = d.month - 1 + delta
    year = d.year + month // 12
    month = month % 12 + 1
    return date(year, month, 1)


# ── Сохранение ответов (общее для HR-формы и публичного опроса) ──────────────

def _score(raw) -> int | None:
    try:
        v = int(raw)
    except (ValueError, TypeError):
        return None
    return max(0, min(10, v))


def _upsert_record(db: Session, employee_id: int, section: str, period_date: date,
                   text_1: str | None, text_2: str | None, score: int | None, user_id: int | None) -> None:
    text_1 = (text_1 or "").strip() or None
    text_2 = (text_2 or "").strip() or None
    if text_1 is None and text_2 is None and score is None:
        return  # ничего не заполнено — пустую запись не создаём
    record = db.query(HrRecord).filter(
        HrRecord.employee_id == employee_id,
        HrRecord.section == section,
        HrRecord.period == period_date,
    ).first()
    if not record:
        record = HrRecord(employee_id=employee_id, section=section, period=period_date, created_by=user_id)
        db.add(record)
    record.text_1 = text_1
    record.text_2 = text_2
    record.score = score


def _save_from_form(db: Session, employee_id: int, period_date: date, form,
                    allowed: set[str], user_id: int | None) -> None:
    """Сохраняет только разрешённые (allowed) разделы из тела формы."""
    def g(name):
        return form.get(name, "")

    if "personal" in allowed:
        _upsert_record(db, employee_id, "personal", period_date, g("personal_1"), g("personal_2"), None, user_id)
    if "complaints" in allowed:
        _upsert_record(db, employee_id, "complaints", period_date, g("complaints_1"), None, None, user_id)
    if "achievements" in allowed:
        _upsert_record(db, employee_id, "achievements", period_date, g("achievements_1"), None, None, user_id)
    if "enps" in allowed:
        _upsert_record(db, employee_id, "enps", period_date, g("enps_comment"), None, _score(g("enps_score")), user_id)
    if "enps_managers" in allowed:
        _upsert_record(db, employee_id, "enps_managers", period_date, g("enps_managers_comment"), None, _score(g("enps_managers_score")), user_id)
    if "metrics" in allowed:
        _upsert_record(db, employee_id, "metrics", period_date, g("metrics_1"), None, None, user_id)
    if "gravity" in allowed:
        _upsert_record(db, employee_id, "gravity", period_date, g("gravity_1"), None, None, user_id)


def _section_ctx() -> dict:
    return {"section_meta": SECTION_META, "questions": QUESTIONS, "all_sections": list(HR_SECTIONS)}


# ── Дашборд ──────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
@login_required
async def hr_home(request: Request, period: str = "", db: Session = Depends(get_db)):
    period_date = _period_from_str(period)

    employees = db.query(HrEmployee).order_by(HrEmployee.is_active.desc(), HrEmployee.full_name).all()
    records = db.query(HrRecord).filter(HrRecord.period == period_date).all()
    filled_sections: dict[int, set] = {}
    for r in records:
        filled_sections.setdefault(r.employee_id, set()).add(r.section)

    positions = db.query(HrPosition).filter(HrPosition.is_active == True).order_by(HrPosition.title).all()
    vacancies = db.query(HrVacancy).order_by(HrVacancy.closed_at.is_not(None), HrVacancy.opened_at.desc()).all()

    return templates.TemplateResponse(request, "hr/list.html", {
        "employees": employees,
        "positions": positions,
        "filled_sections": filled_sections,
        "vacancies": vacancies,
        "period": _period_str(period_date),
        "period_label": _period_label(period_date),
        "prev_period": _period_str(_shift_period(period_date, -1)),
        "next_period": _period_str(_shift_period(period_date, 1)),
        "today_period": _period_str(date.today()),
        "saved": request.query_params.get("saved"),
    })


# ── Сотрудники ────────────────────────────────────────────────────────────────

@router.post("/employees")
@login_required
async def create_employee(
    request: Request,
    full_name: str = Form(...),
    position_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    db.add(HrEmployee(
        full_name=full_name.strip(),
        position_id=int(position_id) if position_id else None,
    ))
    db.commit()
    return RedirectResponse(url="/hr/", status_code=302)


@router.post("/employees/{employee_id}/edit")
@login_required
async def edit_employee(
    request: Request,
    employee_id: int,
    full_name: str = Form(...),
    position_id: str = Form(default=""),
    is_active: str = Form(default=""),
    db: Session = Depends(get_db),
):
    emp = db.query(HrEmployee).filter(HrEmployee.id == employee_id).first()
    if emp:
        emp.full_name = full_name.strip()
        emp.position_id = int(position_id) if position_id else None
        emp.is_active = bool(is_active)
        db.commit()
    return RedirectResponse(url="/hr/", status_code=302)


# ── Должности (справочник) ────────────────────────────────────────────────────

@router.get("/positions", response_class=HTMLResponse)
@login_required
async def positions_list(request: Request, db: Session = Depends(get_db)):
    positions = db.query(HrPosition).order_by(HrPosition.is_active.desc(), HrPosition.title).all()
    counts = dict(
        db.query(HrEmployee.position_id, func.count(HrEmployee.id))
        .group_by(HrEmployee.position_id).all()
    )
    return templates.TemplateResponse(request, "hr/positions.html", {
        "positions": positions,
        "emp_counts": counts,
        **_section_ctx(),
    })


@router.post("/positions")
@login_required
async def create_position(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    title = (form.get("title") or "").strip()
    if title:
        disabled = [s for s in HR_SECTIONS if not form.get(f"sec_{s}")]
        db.add(HrPosition(title=title, disabled_sections=",".join(disabled)))
        db.commit()
    return RedirectResponse(url="/hr/positions", status_code=302)


@router.post("/positions/{position_id}/edit")
@login_required
async def edit_position(request: Request, position_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    pos = db.query(HrPosition).filter(HrPosition.id == position_id).first()
    if pos:
        pos.title = (form.get("title") or "").strip() or pos.title
        pos.is_active = bool(form.get("is_active"))
        disabled = [s for s in HR_SECTIONS if not form.get(f"sec_{s}")]
        pos.disabled_sections = ",".join(disabled)
        db.commit()
    return RedirectResponse(url="/hr/positions", status_code=302)


# ── Вакансии ──────────────────────────────────────────────────────────────────

@router.post("/vacancies")
@login_required
async def create_vacancy(
    request: Request,
    title: str = Form(...),
    opened_at: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user_id = request.session.get("user_id")
    db.add(HrVacancy(
        title=title.strip(),
        opened_at=date.fromisoformat(opened_at) if opened_at else date.today(),
        created_by=user_id,
    ))
    db.commit()
    return RedirectResponse(url="/hr/", status_code=302)


@router.post("/vacancies/{vacancy_id}/close")
@login_required
async def close_vacancy(request: Request, vacancy_id: int, db: Session = Depends(get_db)):
    vac = db.query(HrVacancy).filter(HrVacancy.id == vacancy_id).first()
    if vac and not vac.closed_at:
        vac.closed_at = date.today()
        db.commit()
    return RedirectResponse(url="/hr/", status_code=302)


@router.post("/vacancies/{vacancy_id}/reopen")
@login_required
async def reopen_vacancy(request: Request, vacancy_id: int, db: Session = Depends(get_db)):
    vac = db.query(HrVacancy).filter(HrVacancy.id == vacancy_id).first()
    if vac:
        vac.closed_at = None
        db.commit()
    return RedirectResponse(url="/hr/", status_code=302)


# ── HR-форма ручного ввода ────────────────────────────────────────────────────

@router.get("/entry/{employee_id}", response_class=HTMLResponse)
@login_required
async def hr_entry_form(request: Request, employee_id: int, period: str = "", db: Session = Depends(get_db)):
    employee = db.query(HrEmployee).filter(HrEmployee.id == employee_id).first()
    if not employee:
        return RedirectResponse(url="/hr/", status_code=302)

    period_date = _period_from_str(period)
    records = {
        r.section: r
        for r in db.query(HrRecord).filter(
            HrRecord.employee_id == employee_id, HrRecord.period == period_date
        ).all()
    }

    return templates.TemplateResponse(request, "hr/entry.html", {
        "employee": employee,
        "records": records,
        "sections": employee.enabled_sections,
        "period": _period_str(period_date),
        "period_label": _period_label(period_date),
        "prev_period": _period_str(_shift_period(period_date, -1)),
        "next_period": _period_str(_shift_period(period_date, 1)),
        "saved": request.query_params.get("saved"),
        **_section_ctx(),
    })


@router.post("/entry/{employee_id}")
@login_required
async def hr_entry_save(request: Request, employee_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    employee = db.query(HrEmployee).filter(HrEmployee.id == employee_id).first()
    if not employee:
        return RedirectResponse(url="/hr/", status_code=302)

    period = form.get("period", "")
    period_date = _period_from_str(period)
    user_id = request.session.get("user_id")
    _save_from_form(db, employee_id, period_date, form, set(employee.enabled_sections), user_id)
    db.commit()
    return RedirectResponse(url=f"/hr/entry/{employee_id}?period={period}&saved=1", status_code=302)


# ── Опросы (раунды-рассылки) ──────────────────────────────────────────────────

@router.get("/surveys", response_class=HTMLResponse)
@login_required
async def surveys_list(request: Request, db: Session = Depends(get_db)):
    surveys = db.query(HrSurvey).order_by(HrSurvey.created_at.desc()).all()
    employees = db.query(HrEmployee).filter(HrEmployee.is_active == True).order_by(HrEmployee.full_name).all()
    return templates.TemplateResponse(request, "hr/surveys.html", {
        "surveys": surveys,
        "employees": employees,
        "today_period": _period_str(date.today()),
        "period_label_fn": _period_label,
        **_section_ctx(),
    })


@router.post("/surveys")
@login_required
async def create_survey(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    period_date = _period_from_str(form.get("period"))
    sections = [s for s in HR_SECTIONS if form.get(f"sec_{s}")]
    employee_ids = [int(x) for x in form.getlist("employee_ids")]
    if not sections or not employee_ids:
        return RedirectResponse(url="/hr/surveys?error=empty", status_code=302)

    title = (form.get("title") or "").strip() or None
    survey = HrSurvey(
        title=title,
        period=period_date,
        sections=",".join(sections),
        created_by=request.session.get("user_id"),
    )
    db.add(survey)
    db.flush()

    for emp in db.query(HrEmployee).filter(HrEmployee.id.in_(employee_ids)).all():
        # пропускаем сотрудника, если ни один раздел раунда не применим к его должности
        if not (set(sections) & set(emp.enabled_sections)):
            continue
        db.add(HrSurveyToken(
            survey_id=survey.id,
            employee_id=emp.id,
            token=secrets.token_urlsafe(24),
        ))
    db.commit()
    return RedirectResponse(url=f"/hr/surveys/{survey.id}", status_code=302)


@router.get("/surveys/{survey_id}", response_class=HTMLResponse)
@login_required
async def survey_detail(request: Request, survey_id: int, db: Session = Depends(get_db)):
    survey = db.query(HrSurvey).filter(HrSurvey.id == survey_id).first()
    if not survey:
        return RedirectResponse(url="/hr/surveys", status_code=302)
    base_url = str(request.base_url).rstrip("/")
    return templates.TemplateResponse(request, "hr/survey_detail.html", {
        "survey": survey,
        "period_label": _period_label(survey.period),
        "base_url": base_url,
        **_section_ctx(),
    })


@router.post("/surveys/{survey_id}/toggle")
@login_required
async def survey_toggle(request: Request, survey_id: int, db: Session = Depends(get_db)):
    survey = db.query(HrSurvey).filter(HrSurvey.id == survey_id).first()
    if survey:
        survey.is_open = not survey.is_open
        db.commit()
    return RedirectResponse(url=f"/hr/surveys/{survey_id}", status_code=302)


# ── Публичная форма опроса (без входа в TMS) ─────────────────────────────────

_RATE_WINDOW = 60
_RATE_MAX = 60
_hits: dict[str, list] = defaultdict(list)


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    bucket = _hits[ip]
    cutoff = now - _RATE_WINDOW
    bucket[:] = [t for t in bucket if t > cutoff]
    if len(bucket) >= _RATE_MAX:
        return True
    bucket.append(now)
    if len(_hits) > 2048:
        for k in [k for k, v in list(_hits.items()) if not v]:
            _hits.pop(k, None)
    return False


def _load_token(db: Session, token: str) -> HrSurveyToken | None:
    return db.query(HrSurveyToken).filter(HrSurveyToken.token == token).first()


@router.get("/s/{token}", response_class=HTMLResponse)
async def public_survey_form(request: Request, token: str, db: Session = Depends(get_db)):
    if _rate_limited(request.client.host if request.client else "?"):
        return HTMLResponse("<h3>Слишком много запросов, попробуйте позже.</h3>", status_code=429)
    tok = _load_token(db, token)
    if not tok:
        return templates.TemplateResponse(request, "hr/survey_public.html", {"invalid": True}, status_code=404)

    survey = tok.survey
    sections = tok.effective_sections
    records = {
        r.section: r
        for r in db.query(HrRecord).filter(
            HrRecord.employee_id == tok.employee_id, HrRecord.period == survey.period
        ).all()
    }
    return templates.TemplateResponse(request, "hr/survey_public.html", {
        "invalid": False,
        "token": token,
        "employee": tok.employee,
        "survey": survey,
        "sections": sections,
        "records": records,
        "period_label": _period_label(survey.period),
        "closed": not survey.is_open,
        "submitted": tok.submitted_at is not None,
        "done": request.query_params.get("done"),
        **_section_ctx(),
    })


@router.post("/s/{token}")
async def public_survey_submit(request: Request, token: str, db: Session = Depends(get_db)):
    if _rate_limited(request.client.host if request.client else "?"):
        return HTMLResponse("<h3>Слишком много запросов, попробуйте позже.</h3>", status_code=429)
    tok = _load_token(db, token)
    if not tok:
        return templates.TemplateResponse(request, "hr/survey_public.html", {"invalid": True}, status_code=404)
    if not tok.survey.is_open:
        return RedirectResponse(url=f"/hr/s/{token}", status_code=302)

    form = await request.form()
    _save_from_form(db, tok.employee_id, tok.survey.period, form, set(tok.effective_sections), None)
    tok.submitted_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(url=f"/hr/s/{token}?done=1", status_code=302)

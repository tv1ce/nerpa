from datetime import date

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import login_required
from app.models import HrEmployee, HrRecord, HrVacancy, HR_SECTIONS

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

Q_PERSONAL_1 = "За прошедший месяц: что из сделанного вами здесь дало ощущение реального результата и ценности для компании?"
Q_PERSONAL_2 = "Был ли в этом месяце момент, когда вам не хватило коммуникации, обратной связи или решений со стороны руководства?"
Q_COMPLAINTS = "С какой дичью вам приходится сталкиваться каждый день?"
Q_ENPS_1 = "По шкале от 0 до 10, с какой вероятностью вы порекомендуете компанию как отличное место работы?"
Q_ENPS_2 = "Пожалуйста, кратко объясните, почему вы поставили такую оценку."
Q_ENPS_MGR_1 = "Оцените, пожалуйста, насколько вам комфортно работать и коммуницировать со своим руководителем (по шкале от 1 до 10)?"
Q_ENPS_MGR_2 = "Что именно ваш руководитель делает хорошо, а что стоило бы изменить или улучшить в его стиле управления?"


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


@router.get("/", response_class=HTMLResponse)
@login_required
async def hr_home(request: Request, period: str = "", db: Session = Depends(get_db)):
    period_date = _period_from_str(period)

    employees = db.query(HrEmployee).order_by(HrEmployee.is_active.desc(), HrEmployee.full_name).all()
    records = db.query(HrRecord).filter(HrRecord.period == period_date).all()
    filled_sections = {}
    for r in records:
        filled_sections.setdefault(r.employee_id, set()).add(r.section)

    vacancies = db.query(HrVacancy).order_by(HrVacancy.closed_at.is_not(None), HrVacancy.opened_at.desc()).all()

    return templates.TemplateResponse(request, "hr/list.html", {
        "employees": employees,
        "filled_sections": filled_sections,
        "total_sections": len(HR_SECTIONS),
        "vacancies": vacancies,
        "period": _period_str(period_date),
        "period_label": _period_label(period_date),
        "prev_period": _period_str(_shift_period(period_date, -1)),
        "next_period": _period_str(_shift_period(period_date, 1)),
        "today_period": _period_str(date.today()),
        "saved": request.query_params.get("saved"),
    })


@router.post("/employees")
@login_required
async def create_employee(
    request: Request,
    full_name: str = Form(...),
    position: str = Form(default=""),
    db: Session = Depends(get_db),
):
    db.add(HrEmployee(full_name=full_name.strip(), position=position.strip() or None))
    db.commit()
    return RedirectResponse(url="/hr/", status_code=302)


@router.post("/employees/{employee_id}/edit")
@login_required
async def edit_employee(
    request: Request,
    employee_id: int,
    full_name: str = Form(...),
    position: str = Form(default=""),
    is_active: str = Form(default=""),
    db: Session = Depends(get_db),
):
    emp = db.query(HrEmployee).filter(HrEmployee.id == employee_id).first()
    if emp:
        emp.full_name = full_name.strip()
        emp.position = position.strip() or None
        emp.is_active = bool(is_active)
        db.commit()
    return RedirectResponse(url="/hr/", status_code=302)


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
        "section_meta": SECTION_META,
        "period": _period_str(period_date),
        "period_label": _period_label(period_date),
        "prev_period": _period_str(_shift_period(period_date, -1)),
        "next_period": _period_str(_shift_period(period_date, 1)),
        "q_personal_1": Q_PERSONAL_1, "q_personal_2": Q_PERSONAL_2,
        "q_complaints": Q_COMPLAINTS,
        "q_enps_1": Q_ENPS_1, "q_enps_2": Q_ENPS_2,
        "q_enps_mgr_1": Q_ENPS_MGR_1, "q_enps_mgr_2": Q_ENPS_MGR_2,
    })


def _upsert_record(db: Session, employee_id: int, section: str, period_date: date,
                    text_1: str | None, text_2: str | None, score: int | None, user_id: int | None) -> None:
    text_1 = (text_1 or "").strip() or None
    text_2 = (text_2 or "").strip() or None
    if text_1 is None and text_2 is None and score is None:
        return  # ничего не заполнено — не создаём пустую запись
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


@router.post("/entry/{employee_id}")
@login_required
async def hr_entry_save(
    request: Request,
    employee_id: int,
    period: str = Form(...),
    personal_1: str = Form(default=""),
    personal_2: str = Form(default=""),
    complaints_1: str = Form(default=""),
    achievements_1: str = Form(default=""),
    enps_score: str = Form(default=""),
    enps_comment: str = Form(default=""),
    enps_managers_score: str = Form(default=""),
    enps_managers_comment: str = Form(default=""),
    metrics_1: str = Form(default=""),
    gravity_1: str = Form(default=""),
    db: Session = Depends(get_db),
):
    employee = db.query(HrEmployee).filter(HrEmployee.id == employee_id).first()
    if not employee:
        return RedirectResponse(url="/hr/", status_code=302)

    period_date = _period_from_str(period)
    user_id = request.session.get("user_id")

    def _score(raw: str) -> int | None:
        try:
            v = int(raw)
        except (ValueError, TypeError):
            return None
        return max(0, min(10, v))

    _upsert_record(db, employee_id, "personal", period_date, personal_1, personal_2, None, user_id)
    _upsert_record(db, employee_id, "complaints", period_date, complaints_1, None, None, user_id)
    _upsert_record(db, employee_id, "achievements", period_date, achievements_1, None, None, user_id)
    _upsert_record(db, employee_id, "enps", period_date, enps_comment, None, _score(enps_score), user_id)
    _upsert_record(db, employee_id, "enps_managers", period_date, enps_managers_comment, None, _score(enps_managers_score), user_id)
    _upsert_record(db, employee_id, "metrics", period_date, metrics_1, None, None, user_id)
    _upsert_record(db, employee_id, "gravity", period_date, gravity_1, None, None, user_id)
    db.commit()

    return RedirectResponse(url=f"/hr/entry/{employee_id}?period={period}&saved=1", status_code=302)

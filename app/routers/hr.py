import asyncio
import json
import logging
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
    HrEmployee, HrRecord, HrVacancy, HrPosition, HrSurvey, HrSurveyToken,
    HrEmployeeInsight, Notification, User,
    HR_SECTIONS, HR_PERIOD_KINDS, HR_PERIOD_KIND_LABELS,
)

logger = logging.getLogger(__name__)

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

# Вопросы «Личностного профиля» по умолчанию — для сотрудников без должности
# или с должностью без собственного списка вопросов. Для остальных вопросы
# берутся из должности (HrPosition.personal_questions, по одному на строку).
DEFAULT_PERSONAL_QUESTIONS = [
    "За прошедший месяц: что из сделанного вами здесь дало ощущение реального результата и ценности для компании?",
    "Был ли в этом месяце момент, когда вам не хватило коммуникации, обратной связи или решений со стороны руководства?",
]

# Вопросы остальных разделов — используются и в HR-форме, и в публичном опросе.
QUESTIONS = {
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


# ── Личностный профиль: вопросы должности и разбор ответов ───────────────────

def _personal_questions(employee: HrEmployee) -> list[str]:
    """Вопросы личностного профиля для сотрудника — из его должности или общие."""
    if employee.position_ref and employee.position_ref.personal_question_list:
        return employee.position_ref.personal_question_list
    return list(DEFAULT_PERSONAL_QUESTIONS)


def _parse_personal_answers(rec: HrRecord | None) -> dict[str, str]:
    """Ответы личностного профиля из записи: JSON-пары [{"q","a"}] в text_1.
    Старые записи (до вопросов-по-должностям) хранили два ответа в text_1/text_2 —
    маппим их на дефолтные вопросы, чтобы история не потерялась."""
    if rec is None or not rec.text_1:
        return {}
    try:
        pairs = json.loads(rec.text_1)
        if isinstance(pairs, list):
            return {p.get("q", ""): p.get("a", "") for p in pairs if isinstance(p, dict)}
    except (ValueError, TypeError):
        pass
    legacy = {DEFAULT_PERSONAL_QUESTIONS[0]: rec.text_1}
    if rec.text_2:
        legacy[DEFAULT_PERSONAL_QUESTIONS[1]] = rec.text_2
    return legacy


def _personal_qa(employee: HrEmployee, rec: HrRecord | None) -> list[dict]:
    """[{"q": вопрос, "a": ответ}] для рендера формы — вопросы должности + ответы записи."""
    answers = _parse_personal_answers(rec)
    return [{"q": q, "a": answers.get(q, "")} for q in _personal_questions(employee)]


# ── Сохранение ответов (общее для HR-формы и публичного опроса) ──────────────

def _score(raw) -> int | None:
    try:
        v = int(raw)
    except (ValueError, TypeError):
        return None
    return max(0, min(10, v))


def _upsert_record(db: Session, employee_id: int, section: str, period_date: date, period_kind: str,
                   text_1: str | None, text_2: str | None, score: int | None, user_id: int | None) -> None:
    text_1 = (text_1 or "").strip() or None
    text_2 = (text_2 or "").strip() or None
    if text_1 is None and text_2 is None and score is None:
        return  # ничего не заполнено — пустую запись не создаём
    record = db.query(HrRecord).filter(
        HrRecord.employee_id == employee_id,
        HrRecord.section == section,
        HrRecord.period == period_date,
        HrRecord.period_kind == period_kind,
    ).first()
    if not record:
        record = HrRecord(employee_id=employee_id, section=section, period=period_date,
                          period_kind=period_kind, created_by=user_id)
        db.add(record)
    record.text_1 = text_1
    record.text_2 = text_2
    record.score = score


def _save_from_form(db: Session, employee: HrEmployee, period_date: date, form,
                    allowed: set[str], user_id: int | None) -> None:
    """Сохраняет только разрешённые (allowed) разделы из тела формы.
    Личностный профиль хранится JSON-парами «вопрос-ответ» (вопросы зависят от
    должности); метрика — двумя записями за полумесяцы (h1/h2), остальное — за месяц."""
    def g(name):
        return form.get(name, "")

    def up(section, t1, t2=None, sc=None, kind="month"):
        _upsert_record(db, employee.id, section, period_date, kind, t1, t2, sc, user_id)

    if "personal" in allowed:
        pairs = [{"q": q, "a": (g(f"personal_q{i}") or "").strip()}
                 for i, q in enumerate(_personal_questions(employee))]
        if any(p["a"] for p in pairs):
            up("personal", json.dumps(pairs, ensure_ascii=False))
    if "complaints" in allowed:
        up("complaints", g("complaints_1"))
    if "achievements" in allowed:
        up("achievements", g("achievements_1"))
    if "enps" in allowed:
        up("enps", g("enps_comment"), sc=_score(g("enps_score")))
    if "enps_managers" in allowed:
        up("enps_managers", g("enps_managers_comment"), sc=_score(g("enps_managers_score")))
    if "metrics" in allowed:
        up("metrics", g("metrics_h1"), kind="h1")
        up("metrics", g("metrics_h2"), kind="h2")
    if "gravity" in allowed:
        up("gravity", g("gravity_1"))


def _records_map(db: Session, employee_id: int, period_date: date) -> dict:
    """Записи сотрудника за месяц для формы: {'personal': rec, ..., 'metrics_h1': rec,
    'metrics_h2': rec}. Метрика раскладывается по полумесяцам; для остальных разделов
    приоритет у месячной записи (полумесячные могли остаться от старых раундов)."""
    out: dict[str, HrRecord] = {}
    rows = db.query(HrRecord).filter(
        HrRecord.employee_id == employee_id, HrRecord.period == period_date
    ).all()
    for r in rows:
        if r.section == "metrics":
            if r.period_kind in ("h1", "h2"):
                out[f"metrics_{r.period_kind}"] = r
        elif r.period_kind == "month" or r.section not in out:
            out[r.section] = r
    return out


def _section_ctx() -> dict:
    return {
        "section_meta": SECTION_META, "questions": QUESTIONS, "all_sections": list(HR_SECTIONS),
        "period_kinds": list(HR_PERIOD_KINDS), "period_kind_labels": HR_PERIOD_KIND_LABELS,
    }


def _norm_kind(raw) -> str:
    return raw if raw in HR_PERIOD_KINDS else "month"


def _period_full_label(d: date, kind: str) -> str:
    base = _period_label(d)
    return base if kind == "month" else f"{base} · {HR_PERIOD_KIND_LABELS[kind]}"


# ── Дашборд ──────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
@login_required
async def hr_home(request: Request, period: str = "", db: Session = Depends(get_db)):
    period_date = _period_from_str(period)

    all_employees = db.query(HrEmployee).order_by(HrEmployee.is_active.desc(), HrEmployee.full_name).all()
    # уволенные не показываются в периодах после месяца увольнения — история за прошлые
    # месяцы при этом сохраняется, чтобы старые отчёты не «теряли» человека задним числом
    employees = [e for e in all_employees if e.visible_in_period(period_date)]
    # раздел считается заполненным, если за месяц есть хотя бы одна запись (любой полумесяц)
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
        now_active = bool(is_active)
        emp.full_name = full_name.strip()
        emp.position_id = int(position_id) if position_id else None
        if emp.is_active and not now_active:
            # увольняем: запоминаем месяц, начиная с которого сотрудник больше не в учёте
            today = date.today()
            emp.deactivated_at = date(today.year, today.month, 1)
        elif not emp.is_active and now_active:
            # восстанавливаем — снова виден во всех периодах
            emp.deactivated_at = None
        emp.is_active = now_active
        db.commit()
    return RedirectResponse(url="/hr/", status_code=302)


# ── Профайл сотрудника: история ответов + ИИ-анализ динамики ─────────────────

def _profile_months(employee: HrEmployee, rows: list[HrRecord]) -> list[dict]:
    """Группирует записи по месяцам (свежие сверху) в готовую для шаблона структуру:
    [{"label", "sections": [{"icon", "label", "rows": [{"q","a"} | {"text","score","half"}]}]}]"""
    by_period: dict[date, list[HrRecord]] = {}
    for r in rows:
        by_period.setdefault(r.period, []).append(r)

    months = []
    for period in sorted(by_period, reverse=True):
        sections = []
        recs = {(r.section, r.period_kind or "month"): r for r in by_period[period]}
        for code in HR_SECTIONS:
            entries = []
            if code == "personal":
                rec = recs.get(("personal", "month"))
                if rec:
                    for q, a in _parse_personal_answers(rec).items():
                        if a:
                            entries.append({"q": q, "a": a})
            elif code == "metrics":
                for kind, half in (("h1", "1–15"), ("h2", "16–конец"), ("month", "")):
                    rec = recs.get(("metrics", kind))
                    if rec and rec.text_1:
                        entries.append({"text": rec.text_1, "half": half})
            else:
                rec = recs.get((code, "month")) or next(
                    (r for (s, _k), r in recs.items() if s == code), None)
                if rec and (rec.text_1 or rec.score is not None):
                    entries.append({"text": rec.text_1 or "", "score": rec.score})
            if entries:
                sections.append({"code": code, "icon": SECTION_META[code]["icon"],
                                 "label": SECTION_META[code]["label"], "rows": entries})
        if sections:
            months.append({"label": _period_label(period), "sections": sections})
    return months


def _history_text_for_ai(employee: HrEmployee, months: list[dict]) -> str:
    """История ответов в хронологическом порядке — вход для ИИ-анализа динамики."""
    parts = [f"Сотрудник: {employee.full_name}, должность: {employee.position_title or 'не указана'}."]
    for m in reversed(months):  # от старых к новым — так модели проще увидеть динамику
        parts.append(f"\n=== {m['label']} ===")
        for sec in m["sections"]:
            parts.append(f"[{sec['label']}]")
            for row in sec["rows"]:
                if "q" in row:
                    parts.append(f"Вопрос: {row['q']}\nОтвет: {row['a']}")
                else:
                    prefix = f"({row['half']}) " if row.get("half") else ""
                    score = f" Оценка: {row['score']}/10." if row.get("score") is not None else ""
                    parts.append(f"{prefix}{row['text']}{score}")
    return "\n".join(parts)


AI_PROFILE_PROMPT = """\
Ты — HR-аналитик компании. Тебе дана история ежемесячных ответов одного сотрудника
(личностный профиль, раздражители, достижения, eNPS, метрики, гравитация).

Составь краткий анализ динамики сотрудника:
1. Общий вектор: растёт / стабилен / выгорает — с обоснованием из ответов.
2. Что изменилось между месяцами (мотивация, проблемы, вовлечённость, оценки eNPS).
3. Красные флаги, если есть (потеря смысла, накапливающееся раздражение, падение оценок).
4. Рекомендации HR: на что обратить внимание в разговоре с сотрудником.

Пиши по-русски, по делу, без воды, без markdown-заголовков — обычные абзацы и списки
с дефисами. Не выдумывай ничего, чего нет в ответах. Если данных мало (один месяц) —
так и скажи и дай выводы по тому, что есть."""


@router.get("/employees/{employee_id}/profile", response_class=HTMLResponse)
@login_required
async def employee_profile(request: Request, employee_id: int, db: Session = Depends(get_db)):
    employee = db.query(HrEmployee).filter(HrEmployee.id == employee_id).first()
    if not employee:
        return RedirectResponse(url="/hr/", status_code=302)

    rows = db.query(HrRecord).filter(HrRecord.employee_id == employee_id).all()
    months = _profile_months(employee, rows)
    insights = (db.query(HrEmployeeInsight)
                .filter(HrEmployeeInsight.employee_id == employee_id)
                .order_by(HrEmployeeInsight.created_at.desc()).limit(5).all())

    # средний eNPS по месяцам — маленький тренд в шапке профиля
    enps_trend = [
        {"label": _period_label(r.period), "score": r.score}
        for r in sorted((r for r in rows if r.section == "enps" and r.score is not None),
                        key=lambda r: r.period)
    ]

    return templates.TemplateResponse(request, "hr/profile.html", {
        "employee": employee,
        "months": months,
        "insights": insights,
        "enps_trend": enps_trend,
        "today_period": _period_str(date.today()),
        "error": request.query_params.get("error"),
        "analyzed": request.query_params.get("analyzed"),
    })


@router.post("/employees/{employee_id}/analyze")
@login_required
async def employee_analyze(request: Request, employee_id: int, db: Session = Depends(get_db)):
    """Запускает ИИ-анализ динамики сотрудника (OpenRouter) и сохраняет результат."""
    employee = db.query(HrEmployee).filter(HrEmployee.id == employee_id).first()
    if not employee:
        return RedirectResponse(url="/hr/", status_code=302)

    rows = db.query(HrRecord).filter(HrRecord.employee_id == employee_id).all()
    months = _profile_months(employee, rows)
    if not months:
        return RedirectResponse(url=f"/hr/employees/{employee_id}/profile?error=no_data", status_code=302)

    from app.services import openrouter_client
    history = _history_text_for_ai(employee, months)
    try:
        text = await asyncio.to_thread(openrouter_client.chat, AI_PROFILE_PROMPT, history)
    except Exception as e:
        logger.exception("ИИ-анализ профайла не удался: %s", e)
        return RedirectResponse(url=f"/hr/employees/{employee_id}/profile?error=ai", status_code=302)

    db.add(HrEmployeeInsight(
        employee_id=employee_id,
        text=text.strip(),
        model=openrouter_client.MODEL,
        created_by=request.session.get("user_id"),
    ))
    db.commit()
    return RedirectResponse(url=f"/hr/employees/{employee_id}/profile?analyzed=1", status_code=302)


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
        db.add(HrPosition(
            title=title,
            disabled_sections=",".join(disabled),
            personal_questions=(form.get("personal_questions") or "").strip() or None,
        ))
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
        pos.personal_questions = (form.get("personal_questions") or "").strip() or None
        db.commit()
    return RedirectResponse(url="/hr/positions", status_code=302)


@router.post("/positions/{position_id}/delete")
@login_required
async def delete_position(request: Request, position_id: int, db: Session = Depends(get_db)):
    """Удаляет должность, только если на неё не назначен ни один сотрудник
    (иначе они молча остались бы без применимых разделов отчёта) — в этом
    случае предлагается деактивировать должность вместо удаления."""
    pos = db.query(HrPosition).filter(HrPosition.id == position_id).first()
    if not pos:
        return RedirectResponse(url="/hr/positions", status_code=302)
    in_use = db.query(HrEmployee).filter(HrEmployee.position_id == position_id).count()
    if in_use:
        return RedirectResponse(url="/hr/positions?error=in_use", status_code=302)
    db.delete(pos)
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
async def hr_entry_form(request: Request, employee_id: int, period: str = "",
                        db: Session = Depends(get_db)):
    employee = db.query(HrEmployee).filter(HrEmployee.id == employee_id).first()
    if not employee:
        return RedirectResponse(url="/hr/", status_code=302)

    period_date = _period_from_str(period)
    records = _records_map(db, employee_id, period_date)

    return templates.TemplateResponse(request, "hr/entry.html", {
        "employee": employee,
        "records": records,
        "sections": employee.enabled_sections,
        "personal_qa": _personal_qa(employee, records.get("personal")),
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
    _save_from_form(db, employee, period_date, form, set(employee.enabled_sections), user_id)
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
        "period_label_fn": _period_full_label,
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
        "period_label": _period_full_label(survey.period, survey.period_kind or "month"),
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


@router.post("/surveys/{survey_id}/delete")
@login_required
async def survey_delete(request: Request, survey_id: int, db: Session = Depends(get_db)):
    """Удаляет раунд опроса вместе с его ссылками. Собранные ответы (HrRecord)
    остаются — они уже часть учёта сотрудника."""
    survey = db.query(HrSurvey).filter(HrSurvey.id == survey_id).first()
    if survey:
        db.delete(survey)  # токены удалятся каскадом (cascade на relationship)
        db.commit()
    return RedirectResponse(url="/hr/surveys", status_code=302)


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


def _notify_survey_answered(db: Session, tok: HrSurveyToken) -> None:
    """Уведомляет HR/admin (колокольчик в TMS) о первом ответе сотрудника на раунд."""
    survey = tok.survey
    title = f"✅ {tok.employee.full_name} ответил(а) на опрос «{survey.title or 'Опрос'}»"
    recipients = db.query(User).filter(User.role.in_(("hr", "admin")), User.is_active == True).all()
    for user in recipients:
        db.add(Notification(
            type="hr_survey_answered",
            title=title,
            link=f"/hr/surveys/{survey.id}",
            user_id=user.id,
        ))


@router.get("/s/{token}", response_class=HTMLResponse)
async def public_survey_form(request: Request, token: str, db: Session = Depends(get_db)):
    if _rate_limited(request.client.host if request.client else "?"):
        return HTMLResponse("<h3>Слишком много запросов, попробуйте позже.</h3>", status_code=429)
    tok = _load_token(db, token)
    if not tok:
        return templates.TemplateResponse(request, "hr/survey_public.html", {"invalid": True}, status_code=404)

    survey = tok.survey
    sections = tok.effective_sections
    records = _records_map(db, tok.employee_id, survey.period)
    return templates.TemplateResponse(request, "hr/survey_public.html", {
        "invalid": False,
        "token": token,
        "employee": tok.employee,
        "survey": survey,
        "sections": sections,
        "records": records,
        "personal_qa": _personal_qa(tok.employee, records.get("personal")),
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
    is_first_submit = tok.submitted_at is None
    _save_from_form(db, tok.employee, tok.survey.period, form, set(tok.effective_sections), None)
    tok.submitted_at = datetime.utcnow()
    if is_first_submit:
        _notify_survey_answered(db, tok)
    db.commit()
    return RedirectResponse(url=f"/hr/s/{token}?done=1", status_code=302)

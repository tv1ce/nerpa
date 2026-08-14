import asyncio
import json
import logging
import secrets
import time
from collections import defaultdict
from datetime import date, datetime

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.tz import now as msk_now
from app.database import get_db
from app.auth import login_required
from app.env import getenv as env_get
from app.models import (
    HrEmployee, HrRecord, HrVacancy, HrPosition, HrSurvey, HrSurveyToken,
    HrEmployeeInsight, HrTeamAchievement, HrQuestion, Notification, User, CompanySettings,
    HR_SECTIONS, HR_INPUT_SECTIONS, HR_PERIOD_KINDS, HR_PERIOD_KIND_LABELS,
    HR_ANSWER_TYPES, HR_DEFAULT_QUESTIONS,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hr", tags=["hr"])
templates = Jinja2Templates(directory="app/templates")


@router.post("/toggle-view")
@login_required
async def toggle_view(request: Request, next: str = Form(default="/hr/")):
    """Переключает HR-раздел между мобильным и десктопным видом.

    По умолчанию мобильный вид включён у роли hr (телефон — её основной
    инструмент); остальные роли заходят с десктопа и включают его вручную.
    Светлая/тёмная тема живёт отдельно, в localStorage — см. base_hr.html.
    """
    default = "mobile" if request.session.get("user_role") == "hr" else "desktop"
    current = request.session.get("hr_view", default)
    request.session["hr_view"] = "desktop" if current == "mobile" else "mobile"
    return RedirectResponse(url=next, status_code=302)

SECTION_META = {
    "personal":      {"icon": "👨", "label": "Личностный профиль сотрудника"},
    "complaints":    {"icon": "⁉️", "label": "С какой дичью вам приходится сталкиваться каждый день?"},
    "achievements":  {"icon": "🏅", "label": "Достижения"},
    "enps":          {"icon": "📣", "label": "eNPS"},
    "enps_managers": {"icon": "📣", "label": "eNPS Руководителей"},
    "metrics":       {"icon": "🗄", "label": "Метрика (архив текстовых ответов)"},
    "gravity":       {"icon": "🧲", "label": "Гравитация и антигравитация"},
}

# Вопросы «Личностного профиля» на случай, если HR выключил их все в справочнике
# вопросов, а у должности своего списка нет. По ним же разбираются самые старые
# записи, сделанные до появления вопросов-по-должностям (см. _parse_personal_answers).
DEFAULT_PERSONAL_QUESTIONS = [
    q["text"] for q in HR_DEFAULT_QUESTIONS if q["section"] == "personal"
]


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


def _days_word(n: int) -> str:
    """«1 день», «3 дня», «12 дней» — русское склонение для сроков вакансий."""
    if 11 <= n % 100 <= 14:
        return "дней"
    return {1: "день", 2: "дня", 3: "дня", 4: "дня"}.get(n % 10, "дней")


def _vacancy_rows(vacancies: list[HrVacancy]) -> tuple[list[dict], dict]:
    """Вакансии с посчитанным сроком + сводка. Смысл раздела — именно срок
    закрытия, поэтому он считается здесь, а не остаётся датами в две строки."""
    today = date.today()
    rows = []
    for v in vacancies:
        end = v.closed_at or today
        days = (end - v.opened_at).days if v.opened_at else None
        rows.append({
            "v": v,
            "days": days,
            "days_text": f"{days} {_days_word(days)}" if days is not None else "—",
            "is_closed": v.closed_at is not None,
        })
    closed = [r["days"] for r in rows if r["is_closed"] and r["days"] is not None]
    return rows, {
        "open": sum(1 for r in rows if not r["is_closed"]),
        "closed": len(closed),
        "avg": round(sum(closed) / len(closed)) if closed else None,
        "avg_text": (f"{round(sum(closed) / len(closed))} "
                     f"{_days_word(round(sum(closed) / len(closed)))}") if closed else "",
    }


def _shift_period(d: date, delta: int) -> date:
    month = d.month - 1 + delta
    year = d.year + month // 12
    month = month % 12 + 1
    return date(year, month, 1)


# ── Справочник вопросов (редактируется HR) ───────────────────────────────────

def _all_questions(db: Session, active_only: bool = True) -> dict[str, list[HrQuestion]]:
    """{код раздела: [вопросы по порядку]} — и общие, и должностные вместе.
    Нужен там, где подписываются уже собранные ответы: в истории должен остаться
    подписан и выключенный вопрос, и вопрос чужой должности."""
    query = db.query(HrQuestion)
    if active_only:
        query = query.filter(HrQuestion.is_active == True)
    rows = query.order_by(HrQuestion.section, HrQuestion.sort_order, HrQuestion.id).all()
    out: dict[str, list[HrQuestion]] = defaultdict(list)
    for q in rows:
        out[q.section].append(q)
    return out


def _section_questions(db: Session, position_id: int | None = None,
                       own_only: bool = False) -> dict[str, list[HrQuestion]]:
    """Вопросы по разделам для конкретной должности (или общие, если её нет).

    Правило одно: есть у должности свои вопросы в разделе — задаём только их,
    нет — задаём общие. own_only=True отдаёт только собственные вопросы должности
    (нужно странице настройки, чтобы отличить «свой набор» от «как у всех»)."""
    rows = db.query(HrQuestion).filter(HrQuestion.is_active == True).order_by(
        HrQuestion.sort_order, HrQuestion.id).all()

    own: dict[str, list[HrQuestion]] = defaultdict(list)
    common: dict[str, list[HrQuestion]] = defaultdict(list)
    for q in rows:
        if q.position_id is None:
            common[q.section].append(q)
        elif position_id and q.position_id == position_id:
            own[q.section].append(q)

    if own_only:
        return own
    out: dict[str, list[HrQuestion]] = defaultdict(list)
    for section in set(common) | set(own):
        out[section] = own[section] if section in own else common[section]
    return out


# ── Личностный профиль: вопросы сотрудника и разбор ответов ──────────────────

def _personal_questions(qmap: dict[str, list[HrQuestion]]) -> list[str]:
    """Вопросы личностного профиля (qmap уже разрешён под должность сотрудника)."""
    return [q.text.strip() for q in qmap.get("personal", []) if (q.text or "").strip()]


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


def _personal_qa(qmap: dict[str, list[HrQuestion]], rec: HrRecord | None) -> list[dict]:
    """[{"q": вопрос, "a": ответ}] для рендера формы — вопросы сотрудника + ответы записи."""
    answers = _parse_personal_answers(rec)
    return [{"q": q, "a": answers.get(q, "")} for q in _personal_questions(qmap)]


# ── Дополнительные вопросы раздела: ответы JSON-списком в text_2 ─────────────

def _extra_answers(rec: HrRecord | None) -> dict[str, str]:
    """{"ot_1": "ответ", ...} из text_2. Ключ "comment" — формат ответов
    антигравитации до появления справочника вопросов."""
    if rec is None or not rec.text_2:
        return {}
    try:
        data = json.loads(rec.text_2)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, list):
        return {}
    out = {}
    for item in data:
        if isinstance(item, dict) and item.get("key"):
            value = item.get("answer")
            if value is None:
                value = item.get("comment")
            out[item["key"]] = (value or "").strip()
    return out


def _extra_answers_map(records: dict[str, HrRecord]) -> dict[str, dict[str, str]]:
    """{раздел: {ключ вопроса: ответ}} — для предзаполнения формы."""
    return {code: _extra_answers(rec) for code, rec in records.items()}


def _extra_qa(qmap: dict[str, list[HrQuestion]], section: str,
              rec: HrRecord | None) -> list[dict]:
    """[{"q","a"}] по дополнительным вопросам раздела — для истории и отчётов.
    Ответы на удалённые вопросы не прячем: без формулировки, но с текстом."""
    lookup = {q.key: q for q in qmap.get(section, [])}
    out = []
    for key, answer in _extra_answers(rec).items():
        if not answer:
            continue
        q = lookup.get(key)
        if q is None:
            out.append({"q": "Удалённый вопрос", "a": answer})
        elif q.group_title:
            out.append({"q": f"{q.group_title} — {q.text}", "a": answer})
        else:
            out.append({"q": q.text, "a": answer})
    return out


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
    """Сохраняет только разрешённые (allowed) разделы из тела формы. Состав полей
    задаёт справочник вопросов (HrQuestion): каждый вопрос знает, куда ложится его
    ответ — text_1, score или JSON-список дополнительных ответов в text_2.
    Личностный профиль хранится JSON-парами «вопрос-ответ», т.к. его вопросы
    зависят от должности сотрудника."""
    qmap = _section_questions(db, employee.position_id)

    for section in HR_INPUT_SECTIONS:
        if section not in allowed:
            continue

        if section == "personal":
            pairs = [{"q": q, "a": (form.get(f"personal_q{i}") or "").strip()}
                     for i, q in enumerate(_personal_questions(qmap))]
            if any(p["a"] for p in pairs):
                _upsert_record(db, employee.id, "personal", period_date, "month",
                               json.dumps(pairs, ensure_ascii=False), None, None, user_id)
            continue

        text_1 = None
        score = None
        extras = []
        for q in qmap.get(section, []):
            raw = form.get(q.field_name, "")
            if q.slot == "score":
                score = _score(raw)
            elif q.slot == "text_1":
                text_1 = raw
            else:
                answer = (raw or "").strip()
                if answer:
                    extras.append({"key": q.key, "answer": answer})
        _upsert_record(db, employee.id, section, period_date, "month", text_1,
                       json.dumps(extras, ensure_ascii=False) if extras else None,
                       score, user_id)


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
        "section_meta": SECTION_META, "all_sections": list(HR_INPUT_SECTIONS),
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

    # Порядок: сначала как расставил HR перетаскиванием (sort_order), внутри
    # одинаковых значений — по алфавиту. Пока никого не двигали, у всех 0 и
    # список выглядит ровно как раньше. Уволенные всегда в конце.
    all_employees = db.query(HrEmployee).order_by(
        HrEmployee.is_active.desc(), HrEmployee.sort_order, HrEmployee.full_name).all()
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
    vacancy_rows, vacancy_stats = _vacancy_rows(vacancies)
    company = db.query(CompanySettings).first()
    default_chat_ids = (company.tg_hr_report_chat_ids or company.tg_report_chat_ids or "") if company else ""

    # Ссылка на недельную форму нужна тем, кого не покрывает чужая ссылка:
    #   • руководителям — их ссылка включает и подчинённых, и их самих;
    #   • тем, у кого есть своя метрика, но нет руководителя (например, HR-менеджер) —
    #     иначе внести свою цифру им негде.
    # Рядовым сотрудникам ссылка не нужна: их метрики заполняет руководитель.
    from app.models import HrMetric
    with_metrics = {row[0] for row in db.query(HrMetric.employee_id)
                    .filter(HrMetric.is_active == True).distinct().all()}
    link_ids = {e.manager_id for e in all_employees if e.manager_id}
    link_ids |= {e.id for e in all_employees
                 if e.manager_id is None and e.id in with_metrics}

    team = db.query(HrTeamAchievement).filter(
        HrTeamAchievement.period == period_date).first()

    return templates.TemplateResponse(request, "hr/list.html", {
        "employees": employees,
        "link_ids": link_ids,
        "team_achievement": (team.text or "") if team else "",
        "positions": positions,
        "filled_sections": filled_sections,
        "vacancies": vacancy_rows,
        "vacancy_stats": vacancy_stats,
        "period": _period_str(period_date),
        "period_label": _period_label(period_date),
        "prev_period": _period_str(_shift_period(period_date, -1)),
        "next_period": _period_str(_shift_period(period_date, 1)),
        "today_period": _period_str(date.today()),
        "saved": request.query_params.get("saved"),
        "default_chat_ids": default_chat_ids,
        "report": request.query_params.get("report"),
    })


# ── Сотрудники ────────────────────────────────────────────────────────────────

@router.post("/employees")
@login_required
async def create_employee(
    request: Request,
    full_name: str = Form(...),
    position_id: str = Form(default=""),
    manager_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    db.add(HrEmployee(
        full_name=full_name.strip(),
        position_id=int(position_id) if position_id else None,
        manager_id=int(manager_id) if manager_id else None,
    ))
    db.commit()
    return RedirectResponse(url="/hr/", status_code=302)


@router.post("/employees/order")
@login_required
async def reorder_employees(request: Request, db: Session = Depends(get_db)):
    """Новый порядок списка после перетаскивания строки.

    Приходит весь видимый список целиком, поэтому просто нумеруем по позиции —
    дырок и одинаковых номеров не остаётся, а параллельная правка соседа не
    может «размазать» порядок наполовину."""
    payload = await request.json()
    raw = payload.get("ids") or []
    ids = [int(x) for x in raw if str(x).strip().isdigit()]
    if not ids:
        return JSONResponse({"ok": False, "error": "Пустой список"}, status_code=400)

    employees = {e.id: e for e in db.query(HrEmployee).filter(HrEmployee.id.in_(ids)).all()}
    for position, employee_id in enumerate(ids, start=1):
        employee = employees.get(employee_id)
        if employee:
            employee.sort_order = position
    db.commit()
    return JSONResponse({"ok": True, "count": len(employees)})


@router.post("/employees/{employee_id}/edit")
@login_required
async def edit_employee(
    request: Request,
    employee_id: int,
    full_name: str = Form(...),
    position_id: str = Form(default=""),
    manager_id: str = Form(default=""),
    is_active: str = Form(default=""),
    db: Session = Depends(get_db),
):
    emp = db.query(HrEmployee).filter(HrEmployee.id == employee_id).first()
    if emp:
        now_active = bool(is_active)
        emp.full_name = full_name.strip()
        emp.position_id = int(position_id) if position_id else None
        new_manager_id = int(manager_id) if manager_id else None
        emp.manager_id = new_manager_id if new_manager_id != emp.id else None
        if emp.is_active and not now_active:
            # увольняем: сотрудник пропадает из текущего месяца сразу же — последний
            # видимый период это предыдущий месяц (текущий и позже уже не показываем)
            today = date.today()
            emp.deactivated_at = _shift_period(date(today.year, today.month, 1), -1)
        elif not emp.is_active and now_active:
            # восстанавливаем — снова виден во всех периодах
            emp.deactivated_at = None
        emp.is_active = now_active
        db.commit()
    return RedirectResponse(url="/hr/", status_code=302)


# ── Достижения как команда (одна запись на месяц, заполняет HR) ──────────────

@router.post("/team-achievement")
@login_required
async def save_team_achievement(
    request: Request,
    period: str = Form(...),
    text: str = Form(default=""),
    db: Session = Depends(get_db),
):
    period_date = _period_from_str(period)
    row = db.query(HrTeamAchievement).filter(
        HrTeamAchievement.period == period_date).first()
    cleaned = text.strip()
    if not cleaned:
        # пустое поле = записи за месяц нет, чтобы она не мозолила глаз в отчёте
        if row:
            db.delete(row)
    else:
        if not row:
            row = HrTeamAchievement(period=period_date)
            db.add(row)
        row.text = cleaned
        row.updated_by = request.session.get("user_id")
    db.commit()
    return RedirectResponse(url=f"/hr/?period={period}&saved=1", status_code=302)


# ── Профайл сотрудника: история ответов + ИИ-анализ динамики ─────────────────

def _profile_months(qmap: dict[str, list[HrQuestion]], employee: HrEmployee,
                    rows: list[HrRecord]) -> list[dict]:
    """Группирует записи по месяцам (свежие сверху) в готовую для шаблона структуру:
    [{"label", "sections": [{"icon", "label", "rows": [{"q","a"} | {"text","score","half"}]}]}]

    qmap — справочник вопросов со всеми, включая выключенные: иначе ответ на
    снятый с публикации вопрос остался бы в истории без формулировки."""
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
                if rec:
                    # основной текст подписываем его вопросом — кроме разделов с
                    # оценкой (там строка рендерится вместе с бейджем eNPS) и
                    # случая, когда вопрос дословно повторяет заголовок раздела
                    main_q = next((q for q in qmap.get(code, []) if q.slot == "text_1"), None)
                    if main_q and main_q.text.strip() == SECTION_META[code]["label"]:
                        main_q = None
                    has_score = any(q.slot == "score" for q in qmap.get(code, []))
                    if rec.text_1 and main_q and not has_score:
                        entries.append({"q": main_q.text, "a": rec.text_1})
                    elif rec.text_1 or rec.score is not None:
                        entries.append({"text": rec.text_1 or "", "score": rec.score})
                    entries.extend(_extra_qa(qmap, code, rec))
            if entries:
                sections.append({"code": code, "icon": SECTION_META[code]["icon"],
                                 "label": SECTION_META[code]["label"], "rows": entries})
        if sections:
            months.append({"label": _period_label(period), "period": _period_str(period),
                           "sections": sections})
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


# ── Сводный ИИ-отчёт HR за период → Telegram ──────────────────────────────────
# Числа (средний eNPS, даты вакансий) и достижения считаются/копируются кодом —
# ИИ отвечает только за краткую сводку «Личностного файла» по каждому сотруднику,
# чтобы модель не путала и не выдумывала цифры.

AI_HR_SUMMARY_PROMPT = """\
Ты — HR-аналитик компании. Тебе дан список сотрудников с их ответами за период:
личностный профиль (вопрос-ответ) и текущие раздражители/проблемы в работе.

Для КАЖДОГО сотрудника из списка напиши сжатую, но содержательную сводку на
русском (3-5 предложений) — не сворачивай до одной общей фразы вроде "есть
проблема с коммуникацией". Сохраняй конкретику из ответов: что именно человек
написал, о каких ситуациях, задачах, людях идёт речь, какие детали и цифры он
назвал — перескажи своими словами, но не теряй суть и не обезличивай. Если по
ответам видна динамика настроения, мотивации или отношения к работе — упомяни
её. Не выдумывай ничего, чего нет в ответах, и не додумывай причины, которые
сотрудник не называл. Если ответы пустые, нейтральные или реально без
проблем — выведи ровно "без существенных проблем." (без пояснений).

Верни ТОЛЬКО JSON-массив объектов [{"id": <id сотрудника>, "summary": "..."}], без
markdown-обёртки и пояснений — по одному объекту на каждого сотрудника из списка.
Внутри summary разделяй предложения/абзацы символом \\n\\n, если так яснее."""


AI_GRAVITY_SUMMARY_PROMPT = """\
Ты — HR-аналитик компании. Тебе дан список сотрудников с их ответами на вопросы
о гравитации и антигравитации в работе (общий ответ + структурированные вопросы
«что притягивает / что отталкивает»).

Для КАЖДОГО сотрудника из списка напиши сжатую, но содержательную сводку на русском
(3-5 предложений). Не сворачивай до общих фраз вроде "есть проблемы с руководством" —
сохраняй конкретику из ответов сотрудника: что именно мотивирует, что именно
раздражает, по каким темам/людям/ситуациям, своими словами перескажи суть, но не
теряй детали. Структурируй как два коротких абзаца: сначала что притягивает
(мотивирует, держит), затем что отталкивает (раздражает, выталкивает) — если по
одной из сторон ответов нет, просто опусти этот абзац. Не выдумывай ничего, чего
нет в ответах. Если все ответы пустые или явно нейтральные — выведи ровно
"без выраженных сигналов."

Верни ТОЛЬКО JSON-массив объектов [{"id": <id сотрудника>, "summary": "..."}], без
markdown-обёртки и пояснений — по одному объекту на каждого сотрудника из списка.
Внутри summary разделяй абзацы символом \\n\\n (это попадёт в текст как перенос строки)."""


def _gather_ai_report_context(db: Session, period_date: date) -> dict:
    """Собирает данные всех видимых в периоде сотрудников + вакансий — вход для
    сборки ИИ-отчёта HR (см. _format_hr_report)."""
    from app.routers.hr_metrics import month_metric_lines

    employees = [e for e in db.query(HrEmployee)
                 .order_by(HrEmployee.is_active.desc(), HrEmployee.full_name).all()
                 if e.visible_in_period(period_date)]
    metric_lines = month_metric_lines(db, period_date)
    qmap = _all_questions(db, active_only=False)
    emp_data = []
    for e in employees:
        recs = _records_map(db, e.id, period_date)
        enabled = set(e.enabled_sections)
        personal_answers = _parse_personal_answers(recs.get("personal")) if "personal" in enabled else {}
        complaints_rec = recs.get("complaints") if "complaints" in enabled else None
        achievements_rec = recs.get("achievements")
        enps_rec = recs.get("enps")
        enps_mgr_rec = recs.get("enps_managers")
        gravity_rec = recs.get("gravity") if "gravity" in enabled else None
        gravity_general = (gravity_rec.text_1 or "").strip() if gravity_rec else ""
        gravity_items = _extra_qa(qmap, "gravity", gravity_rec) if gravity_rec else []
        # ответы на вопросы, которые HR добавил сам: у них нет своего места в
        # структуре отчёта, поэтому идут отдельным блоком в конце
        extra_items = []
        for code in HR_INPUT_SECTIONS:
            if code in ("gravity", "personal") or code not in enabled:
                continue
            extra_items.extend(_extra_qa(qmap, code, recs.get(code)))
        emp_data.append({
            "id": e.id, "name": e.full_name, "position": e.position_title or "—",
            "enabled": enabled,
            "personal_answers": personal_answers,
            "complaints": (complaints_rec.text_1 or "").strip() if complaints_rec else "",
            "achievements": (achievements_rec.text_1 or "").strip() if achievements_rec else "",
            "enps_score": enps_rec.score if enps_rec else None,
            "enps_comment": (enps_rec.text_1 or "").strip() if enps_rec else "",
            "enps_mgr_score": enps_mgr_rec.score if enps_mgr_rec else None,
            "enps_mgr_comment": (enps_mgr_rec.text_1 or "").strip() if enps_mgr_rec else "",
            "gravity_general": gravity_general,
            "gravity_items": gravity_items,
            "extra_items": extra_items,
            # метрика берётся из недельного раздела (HrMetric), а не из текстовых ответов
            "metrics": metric_lines.get(e.id, []),
        })
    vacancies = db.query(HrVacancy).order_by(
        HrVacancy.closed_at.is_not(None), HrVacancy.opened_at.desc()).all()
    team = db.query(HrTeamAchievement).filter(
        HrTeamAchievement.period == period_date).first()
    return {
        "period_label": _period_label(period_date),
        "team_achievement": (team.text or "").strip() if team else "",
        "employees": emp_data,
        "vacancies": [{"title": v.title, "opened_at": v.opened_at, "closed_at": v.closed_at}
                      for v in vacancies],
    }


async def _ai_personal_summaries(ctx: dict) -> dict[int, str]:
    """Сводки «Личностного файла» по сотрудникам — один запрос к ИИ на все сразу."""
    candidates = [e for e in ctx["employees"]
                  if "personal" in e["enabled"] or "complaints" in e["enabled"]]
    if not candidates:
        return {}

    lines = []
    for e in candidates:
        lines.append(f"### id={e['id']}: {e['name']} ({e['position']})")
        for q, a in e["personal_answers"].items():
            if a:
                lines.append(f"Вопрос: {q}\nОтвет: {a}")
        if e["complaints"]:
            lines.append(f"Раздражители: {e['complaints']}")
        lines.append("")

    from app.services import openrouter_client
    try:
        result = await asyncio.to_thread(
            openrouter_client.chat_json, AI_HR_SUMMARY_PROMPT, "\n".join(lines))
    except Exception:
        logger.exception("Не удалось получить сводки «Личностного файла» для ИИ-отчёта HR")
        return {}

    out: dict[int, str] = {}
    if isinstance(result, list):
        for item in result:
            if not isinstance(item, dict):
                continue
            try:
                out[int(item.get("id"))] = str(item.get("summary", "")).strip()
            except (TypeError, ValueError):
                continue
    return out


async def _ai_gravity_summaries(ctx: dict) -> dict[int, str]:
    """Сжатые сводки «Гравитации и антигравитации» по сотрудникам для общего отчёта —
    полные ответы остаются только в отчёте «eNPS руководителей», здесь один запрос
    к ИИ на всех сразу даёт по короткой сводке на сотрудника."""
    candidates = [e for e in ctx["employees"] if e["gravity_general"] or e["gravity_items"]]
    if not candidates:
        return {}

    lines = []
    for e in candidates:
        lines.append(f"### id={e['id']}: {e['name']} ({e['position']})")
        if e["gravity_general"]:
            lines.append(f"Общее: {e['gravity_general']}")
        for item in e["gravity_items"]:
            lines.append(f"{item['q']}: {item['a']}")
        lines.append("")

    from app.services import openrouter_client
    try:
        result = await asyncio.to_thread(
            openrouter_client.chat_json, AI_GRAVITY_SUMMARY_PROMPT, "\n".join(lines))
    except Exception:
        logger.exception("Не удалось получить сводки «Гравитации» для ИИ-отчёта HR")
        return {}

    out: dict[int, str] = {}
    if isinstance(result, list):
        for item in result:
            if not isinstance(item, dict):
                continue
            try:
                out[int(item.get("id"))] = str(item.get("summary", "")).strip()
            except (TypeError, ValueError):
                continue
    return out


def _format_hr_report(ctx: dict, summaries: dict[int, str], gravity_summaries: dict[int, str]) -> str:
    """Собирает итоговый текст отчёта (с **bold** для Telegram) из данных периода
    и ИИ-сводок личностного файла и гравитации/антигравитации."""
    lines = [f"📋 **Отчёт HR — {ctx['period_label']}**", ""]

    personal = [e for e in ctx["employees"] if "personal" in e["enabled"] or "complaints" in e["enabled"]]
    if personal:
        lines.append("👨 **Личностный файл**")
        lines.append("")
        for e in personal:
            summary = summaries.get(e["id"]) or "без существенных проблем."
            lines.append(f"**{e['name']}**")
            for row in summary.splitlines():
                row = row.strip()
                if row:
                    lines.append(row)
            lines.append("")

    gravity_emps = [e for e in ctx["employees"] if e["gravity_general"] or e["gravity_items"]]
    if gravity_emps:
        lines.append("🧲 **Гравитация и антигравитация**")
        lines.append("")
        for e in gravity_emps:
            summary = gravity_summaries.get(e["id"]) or "без выраженных сигналов."
            lines.append(f"**{e['name']}**")
            for row in summary.splitlines():
                row = row.strip()
                if row:
                    lines.append(row)
            lines.append("")

    if ctx["team_achievement"]:
        lines.append("🏅 **Достижения как команда**")
        lines.append("")
        for row in ctx["team_achievement"].splitlines():
            row = row.strip()
            if row:
                lines.append(row)
        lines.append("")

    achievers = [e for e in ctx["employees"] if "achievements" in e["enabled"]]
    if achievers:
        lines.append("🏅 **Достижения по сотрудникам**")
        lines.append("")
        for e in achievers:
            lines.append(f"{e['name']} - {e['position']}")
            lines.append("")
            for row in e["achievements"].splitlines():
                row = row.strip()
                if row:
                    lines.append(row)
            lines.append("")

    def _enps_block(title: str, score_key: str, comment_key: str, section: str):
        lines.append(f"📣 **{title}**")
        scored = [e for e in ctx["employees"] if section in e["enabled"] and e[score_key] is not None]
        if not scored:
            lines.append("Данных нет")
            lines.append("")
            return
        avg = sum(e[score_key] for e in scored) / len(scored)
        lines.append(f"Средний балл: {avg:.1f}")
        low = [e for e in scored if e[score_key] <= 6]
        if low:
            for e in low:
                comment = f" — {e[comment_key]}" if e[comment_key] else ""
                lines.append(f"⚠️ {e['name']}: {e[score_key]}/10{comment}")
        else:
            lines.append("Проблемных мест не выявлено ✅")
        lines.append("")

    _enps_block("eNPS (сотрудники)", "enps_score", "enps_comment", "enps")
    _enps_block("eNPS (руководители)", "enps_mgr_score", "enps_mgr_comment", "enps_managers")

    extra_emps = [e for e in ctx["employees"] if e.get("extra_items")]
    if extra_emps:
        lines.append("💬 **Дополнительные вопросы**")
        lines.append("")
        for e in extra_emps:
            lines.append(f"{e['name']} - {e['position']}")
            for item in e["extra_items"]:
                lines.append(f"{item['q']}")
                lines.append(item["a"])
            lines.append("")

    metrics_emps = [e for e in ctx["employees"] if e["metrics"]]
    if metrics_emps:
        lines.append("📈 **Метрики за месяц**")
        lines.append("")
        for e in metrics_emps:
            lines.append(f"{e['name']} - {e['position']}")
            lines.extend(e["metrics"])
            lines.append("")

    if ctx["vacancies"]:
        lines.append("📆 **Сроки закрытия вакансий**")
        lines.append("")
        for v in ctx["vacancies"]:
            lines.append(v["title"])
            lines.append("")
            opened = v["opened_at"].strftime("%d.%m.%Y") if v["opened_at"] else "—"
            lines.append(f"Открытие {opened}")
            lines.append(f"Закрытие {v['closed_at'].strftime('%d.%m.%Y')}" if v["closed_at"] else "Закрытие.")
            lines.append("")

    return "\n".join(lines).strip()


@router.post("/report/telegram")
@login_required
async def send_ai_report(
    request: Request,
    period: str = Form(...),
    chat_ids: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Формирует сводный ИИ-отчёт HR за период и отправляет его в Telegram.
    chat_id можно менять прямо из формы — новое значение сохраняется в настройках."""
    from app.routers.settings import _normalize_chat_ids
    from app.services import telegram_send

    period_date = _period_from_str(period)
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)

    normalized = _normalize_chat_ids(chat_ids)
    if normalized:
        company.tg_hr_report_chat_ids = normalized
    db.commit()

    target_raw = company.tg_hr_report_chat_ids or company.tg_report_chat_ids or ""
    ids = telegram_send.parse_chat_ids(target_raw)
    if not ids:
        return RedirectResponse(url=f"/hr/?period={period}&report=nochat", status_code=302)

    bot_token = (company.tg_bot_token or "").strip() or env_get("NERPA_BOT_TOKEN", "").strip()
    if not bot_token:
        return RedirectResponse(url=f"/hr/?period={period}&report=notoken", status_code=302)

    try:
        ctx = _gather_ai_report_context(db, period_date)
        summaries = await _ai_personal_summaries(ctx)
        gravity_summaries = await _ai_gravity_summaries(ctx)
        text = _format_hr_report(ctx, summaries, gravity_summaries)
        mdv2 = telegram_send.ai_text_to_mdv2(text)
        await asyncio.to_thread(telegram_send.send_markdown, ids, mdv2, bot_token)
    except Exception:
        logger.exception("Не удалось отправить ИИ-отчёт HR в Telegram")
        return RedirectResponse(url=f"/hr/?period={period}&report=error", status_code=302)

    return RedirectResponse(url=f"/hr/?period={period}&report=ok", status_code=302)


def _gather_enps_managers_context(db: Session, period_date: date) -> list[dict]:
    """Собирает по подчинённым — оценку eNPS руководителя и ответы по гравитации/
    антигравитации за период, сгруппированные по руководителю (HrEmployee.manager_id).
    Каждый подчинённый оценивает и комментирует именно своего непосредственного
    руководителя — это и связывает ответ с конкретным управленцем; гравитация
    добавляется тем же подчинённым как дополнительный контекст для руководителя."""
    employees = [e for e in db.query(HrEmployee)
                 .order_by(HrEmployee.full_name).all()
                 if e.visible_in_period(period_date)]
    by_id = {e.id: e for e in employees}
    enps_records = {r.employee_id: r for r in db.query(HrRecord).filter(
        HrRecord.period == period_date, HrRecord.section == "enps_managers").all()}
    gravity_records = {r.employee_id: r for r in db.query(HrRecord).filter(
        HrRecord.period == period_date, HrRecord.section == "gravity").all()}
    qmap = _all_questions(db, active_only=False)

    by_manager: dict[int, list[dict]] = defaultdict(list)
    for e in employees:
        if not e.manager_id or e.manager_id not in by_id:
            continue

        enps_rec = enps_records.get(e.id) if "enps_managers" in e.enabled_sections else None
        has_enps = bool(enps_rec and (enps_rec.score is not None or (enps_rec.text_1 or "").strip()))

        gravity_rec = gravity_records.get(e.id) if "gravity" in e.enabled_sections else None
        gravity_general = (gravity_rec.text_1 or "").strip() if gravity_rec else ""
        gravity_items = _extra_qa(qmap, "gravity", gravity_rec) if gravity_rec else []
        has_gravity = bool(gravity_general or gravity_items)

        if not has_enps and not has_gravity:
            continue

        by_manager[e.manager_id].append({
            "name": e.full_name,
            "score": enps_rec.score if enps_rec else None,
            "comment": (enps_rec.text_1 or "").strip() if enps_rec else "",
            "gravity_general": gravity_general,
            "gravity_items": gravity_items,
        })

    result = []
    for manager_id, answers in by_manager.items():
        manager = by_id.get(manager_id)
        if not manager:
            continue
        scored = [a["score"] for a in answers if a["score"] is not None]
        avg = sum(scored) / len(scored) if scored else None
        result.append({
            "manager": manager.full_name,
            "avg": avg,
            "count": len(scored),
            "answers": answers,
        })
    result.sort(key=lambda m: m["manager"])
    return result


def _format_enps_managers_report(period_label: str, managers: list[dict]) -> str:
    lines = [f"📣 **eNPS руководителей — {period_label}**", ""]
    if not managers:
        lines.append("Данных нет за этот период.")
        return "\n".join(lines).strip()

    for m in managers:
        avg_str = f"{m['avg']:.1f}" if m["avg"] is not None else "—"
        warn = " ⚠️" if m["avg"] is not None and m["avg"] <= 6 else ""
        lines.append(f"**{m['manager']}** — средний балл: {avg_str}/10 ({m['count']} оценок){warn}")
        for a in m["answers"]:
            score = f"{a['score']}/10" if a["score"] is not None else "—"
            comment = f" — {a['comment']}" if a["comment"] else ""
            lines.append(f"• {a['name']}: {score}{comment}")
            if a["gravity_general"]:
                lines.append(f"   🧲 Общее: {a['gravity_general']}")
            for item in a["gravity_items"]:
                lines.append(f"   • {item['q']}: {item['a']}")
        lines.append("")

    return "\n".join(lines).strip()


@router.post("/report/enps-managers/telegram")
@login_required
async def send_enps_managers_report(
    request: Request,
    period: str = Form(...),
    chat_ids: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Формирует отчёт eNPS руководителей за период (на основе оценок подчинённых)
    и отправляет его в Telegram. chat_id можно менять прямо из формы — новое
    значение сохраняется в тех же настройках, что и общий ИИ-отчёт HR."""
    from app.routers.settings import _normalize_chat_ids
    from app.services import telegram_send

    period_date = _period_from_str(period)
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)

    normalized = _normalize_chat_ids(chat_ids)
    if normalized:
        company.tg_hr_report_chat_ids = normalized
    db.commit()

    target_raw = company.tg_hr_report_chat_ids or company.tg_report_chat_ids or ""
    ids = telegram_send.parse_chat_ids(target_raw)
    if not ids:
        return RedirectResponse(url=f"/hr/?period={period}&report=nochat", status_code=302)

    bot_token = (company.tg_bot_token or "").strip() or env_get("NERPA_BOT_TOKEN", "").strip()
    if not bot_token:
        return RedirectResponse(url=f"/hr/?period={period}&report=notoken", status_code=302)

    try:
        managers = _gather_enps_managers_context(db, period_date)
        text = _format_enps_managers_report(_period_label(period_date), managers)
        mdv2 = telegram_send.ai_text_to_mdv2(text)
        await asyncio.to_thread(telegram_send.send_markdown, ids, mdv2, bot_token)
    except Exception:
        logger.exception("Не удалось отправить отчёт eNPS руководителей в Telegram")
        return RedirectResponse(url=f"/hr/?period={period}&report=error", status_code=302)

    return RedirectResponse(url=f"/hr/?period={period}&report=ok", status_code=302)


@router.get("/employees/{employee_id}/profile", response_class=HTMLResponse)
@login_required
async def employee_profile(request: Request, employee_id: int, db: Session = Depends(get_db)):
    employee = db.query(HrEmployee).filter(HrEmployee.id == employee_id).first()
    if not employee:
        return RedirectResponse(url="/hr/", status_code=302)

    rows = db.query(HrRecord).filter(HrRecord.employee_id == employee_id).all()
    months = _profile_months(_all_questions(db, active_only=False), employee, rows)
    insights = (db.query(HrEmployeeInsight)
                .filter(HrEmployeeInsight.employee_id == employee_id)
                .order_by(HrEmployeeInsight.created_at.desc()).limit(5).all())

    # средний eNPS по месяцам — маленький тренд в шапке профиля
    enps_trend = [
        {"label": _period_label(r.period), "score": r.score}
        for r in sorted((r for r in rows if r.section == "enps" and r.score is not None),
                        key=lambda r: r.period)
    ]

    from app.routers.hr_metrics import employee_metric_history

    return templates.TemplateResponse(request, "hr/profile.html", {
        "employee": employee,
        "months": months,
        "insights": insights,
        "enps_trend": enps_trend,
        "metric_history": employee_metric_history(db, employee_id),
        "today_period": _period_str(date.today()),
        "error": request.query_params.get("error"),
        "analyzed": request.query_params.get("analyzed"),
        "deleted": request.query_params.get("deleted"),
    })


async def _analyze_employee(db: Session, employee: HrEmployee,
                            user_id: int | None) -> HrEmployeeInsight | None:
    """ИИ-анализ динамики одного сотрудника + сохранение в историю.
    None — анализировать нечего: за сотрудником нет ни одного месяца ответов."""
    rows = db.query(HrRecord).filter(HrRecord.employee_id == employee.id).all()
    months = _profile_months(_all_questions(db, active_only=False), employee, rows)
    if not months:
        return None

    from app.services import openrouter_client
    history = _history_text_for_ai(employee, months)
    text = await asyncio.to_thread(openrouter_client.chat, AI_PROFILE_PROMPT, history)

    insight = HrEmployeeInsight(
        employee_id=employee.id,
        text=text.strip(),
        model=openrouter_client.MODEL,
        created_by=user_id,
    )
    db.add(insight)
    db.commit()
    return insight


@router.post("/employees/{employee_id}/analyze")
@login_required
async def employee_analyze(request: Request, employee_id: int, db: Session = Depends(get_db)):
    """Запускает ИИ-анализ динамики сотрудника (OpenRouter) и сохраняет результат."""
    employee = db.query(HrEmployee).filter(HrEmployee.id == employee_id).first()
    if not employee:
        return RedirectResponse(url="/hr/", status_code=302)

    try:
        insight = await _analyze_employee(db, employee, request.session.get("user_id"))
    except Exception as e:
        logger.exception("ИИ-анализ профайла не удался: %s", e)
        return RedirectResponse(url=f"/hr/employees/{employee_id}/profile?error=ai", status_code=302)
    if not insight:
        return RedirectResponse(url=f"/hr/employees/{employee_id}/profile?error=no_data", status_code=302)

    return RedirectResponse(url=f"/hr/employees/{employee_id}/profile?analyzed=1", status_code=302)


# ── Единый экран ИИ-анализа по всем сотрудникам ──────────────────────────────
# Читать динамику по одному профайлу — это заход в карточку и отдельная кнопка
# на каждого. Здесь то же самое, но списком: одна кнопка собирает анализ на всех
# и всё читается подряд на одном экране. Хранилище общее с профайлом
# (HrEmployeeInsight), поэтому собранное здесь сразу видно в карточке сотрудника
# и наоборот — двух «правд» про одного человека не появляется.

@router.get("/insights", response_class=HTMLResponse)
@login_required
async def insights_board(request: Request, db: Session = Depends(get_db)):
    employees = (db.query(HrEmployee)
                 .filter(HrEmployee.is_active == True)
                 .order_by(HrEmployee.sort_order, HrEmployee.full_name).all())

    # последний анализ по каждому: записей немного, дешевле пройти по порядку,
    # чем собирать коррелированный подзапрос
    latest: dict[int, HrEmployeeInsight] = {}
    for ins in db.query(HrEmployeeInsight).order_by(HrEmployeeInsight.created_at).all():
        latest[ins.employee_id] = ins

    records = db.query(HrRecord).all()
    with_data: set[int] = set()
    enps: dict[int, list] = defaultdict(list)
    for r in records:
        with_data.add(r.employee_id)
        if r.section == "enps" and r.score is not None:
            enps[r.employee_id].append((r.period, r.score))

    rows = []
    for e in employees:
        scores = [s for _p, s in sorted(enps.get(e.id, []))]
        ins = latest.get(e.id)
        rows.append({
            "employee": e,
            "insight": ins,
            "text": ins.text if ins else "",
            "made_at": ins.created_at.strftime("%d.%m.%Y %H:%M") if ins else "",
            "has_data": e.id in with_data,
            # хвост оценок eNPS — короткий контекст рядом с выводами ИИ
            "enps": scores[-6:],
            "enps_last": scores[-1] if scores else None,
            "enps_delta": (scores[-1] - scores[-2]) if len(scores) > 1 else None,
        })

    return templates.TemplateResponse(request, "hr/insights.html", {
        "rows": rows,
        "ready": sum(1 for r in rows if r["insight"]),
        "analyzable": [r["employee"].id for r in rows if r["has_data"]],
        "today_period": _period_str(date.today()),
    })


@router.post("/insights/run")
@login_required
async def insights_run(request: Request, db: Session = Depends(get_db)):
    """Анализ одного сотрудника — страница вызывает это по очереди для каждого.

    Очередь на стороне браузера, а не один долгий запрос: так HR видит прогресс
    по мере готовности, а не ждёт минуту в пустоту, и таймаут nginx не рубит
    сборку на середине."""
    payload = await request.json()
    try:
        employee_id = int(payload.get("employee_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Не указан сотрудник"}, status_code=400)

    employee = db.query(HrEmployee).filter(HrEmployee.id == employee_id).first()
    if not employee:
        return JSONResponse({"ok": False, "error": "Сотрудник не найден"}, status_code=404)

    try:
        insight = await _analyze_employee(db, employee, request.session.get("user_id"))
    except Exception as e:
        logger.exception("ИИ-анализ (общий экран) не удался для %s: %s", employee.full_name, e)
        return JSONResponse({"ok": False, "error": "ИИ не ответил — проверьте ключ OpenRouter"},
                            status_code=502)

    if not insight:
        return JSONResponse({"ok": False, "skipped": True,
                             "error": "Нет ответов за месяцы — анализировать нечего"})
    return JSONResponse({
        "ok": True,
        "text": insight.text,
        "made_at": insight.created_at.strftime("%d.%m.%Y %H:%M"),
        "model": insight.model or "",
    })


@router.post("/employees/{employee_id}/records/delete")
@login_required
async def delete_employee_records(
    request: Request,
    employee_id: int,
    period: str = Form(...),
    section: str = Form(...),
    db: Session = Depends(get_db),
):
    """Удаляет записи одного раздела за месяц (все полумесяцы) у сотрудника —
    так HR может убрать ошибочный ответ из опроса, не трогая остальные."""
    if section in HR_SECTIONS:
        period_date = _period_from_str(period)
        db.query(HrRecord).filter(
            HrRecord.employee_id == employee_id,
            HrRecord.section == section,
            HrRecord.period == period_date,
        ).delete(synchronize_session=False)
        db.commit()
    return RedirectResponse(
        url=f"/hr/employees/{employee_id}/profile?deleted=records", status_code=302)


@router.post("/employees/{employee_id}/insights/{insight_id}/delete")
@login_required
async def delete_employee_insight(
    request: Request, employee_id: int, insight_id: int, db: Session = Depends(get_db),
):
    """Удаляет один ИИ-отчёт из истории анализа сотрудника."""
    ins = db.query(HrEmployeeInsight).filter(
        HrEmployeeInsight.id == insight_id,
        HrEmployeeInsight.employee_id == employee_id,
    ).first()
    if ins:
        db.delete(ins)
        db.commit()
    return RedirectResponse(
        url=f"/hr/employees/{employee_id}/profile?deleted=insight", status_code=302)


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
        disabled = [s for s in HR_INPUT_SECTIONS if not form.get(f"sec_{s}")]
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
        disabled = [s for s in HR_INPUT_SECTIONS if not form.get(f"sec_{s}")]
        pos.disabled_sections = ",".join(disabled)
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


# ── Вопросы опросника (справочник, редактирует HR) ───────────────────────────

def _next_question_key(db: Session, section: str) -> str:
    """Свободный ключ для нового вопроса раздела. Ключ — то, чем подписан ответ
    в JSON, поэтому он не переиспользуется после удаления вопроса: берём номер
    больше любого уже встречавшегося."""
    used = {k for (k,) in db.query(HrQuestion.key).filter(HrQuestion.section == section).all()}
    n = 1
    while f"c{n}" in used:
        n += 1
    return f"c{n}"


def _questions_url(position_id: int | None, suffix: str = "") -> str:
    base = "/hr/questions" + (f"?position={position_id}" if position_id else "")
    if not suffix:
        return base
    return base + ("&" if position_id else "?") + suffix


@router.get("/questions", response_class=HTMLResponse)
@login_required
async def questions_list(request: Request, position: str = "", db: Session = Depends(get_db)):
    """Вопросы опроса — общие или конкретной должности. Правки сразу действуют
    и в форме ручного ввода, и в уже разосланных ссылках опроса."""
    positions = db.query(HrPosition).filter(HrPosition.is_active == True).order_by(
        HrPosition.title).all()
    position_id = int(position) if position.isdigit() else None
    if position_id and not any(p.id == position_id for p in positions):
        position_id = None

    common = _section_questions(db, None)
    own = _section_questions(db, position_id, own_only=True) if position_id else {}

    sections = []
    for code in HR_INPUT_SECTIONS:
        has_own = position_id is not None and code in own
        sections.append({
            "code": code,
            "questions": own[code] if has_own else common.get(code, []),
            # у должности либо свой набор (редактируем), либо «как у всех» (только показываем)
            "is_own": has_own,
            "common_count": len(common.get(code, [])),
        })

    return templates.TemplateResponse(request, "hr/questions.html", {
        "sections": sections,
        "positions": positions,
        "position_id": position_id,
        "position_title": next((p.title for p in positions if p.id == position_id), ""),
        "hidden_builtin": {
            code for (code,) in db.query(HrQuestion.section).filter(
                HrQuestion.is_builtin == True, HrQuestion.is_active == False).distinct().all()
        } if position_id is None else set(),
        "saved": request.query_params.get("saved"),
        **_section_ctx(),
    })


@router.post("/questions/{section}")
@login_required
async def save_section_questions(request: Request, section: str, db: Session = Depends(get_db)):
    """Сохраняет раздел целиком: тексты, порядок (по порядку строк формы) и удаление.
    Одна форма на раздел — так HR не нужно открывать окно ради каждой строки."""
    if section not in HR_INPUT_SECTIONS:
        return RedirectResponse(url="/hr/questions", status_code=302)

    form = await request.form()
    position_id = int(form["position_id"]) if (form.get("position_id") or "").isdigit() else None
    ids = form.getlist("qid")
    texts = form.getlist("text")
    types = form.getlist("answer_type")

    existing = {q.id: q for q in db.query(HrQuestion).filter(
        HrQuestion.section == section,
        HrQuestion.position_id == position_id,
    ).all()}
    kept = set()

    for i, (raw_id, text) in enumerate(zip(ids, texts)):
        text = (text or "").strip()
        answer_type = types[i] if i < len(types) and types[i] in HR_ANSWER_TYPES else "text"
        q = existing.get(int(raw_id)) if raw_id.isdigit() else None
        if not text:
            continue                      # пустую строку считаем незаполненной, а не вопросом
        if q is None:
            q = HrQuestion(
                section=section,
                key=_next_question_key(db, section),
                position_id=position_id,
                # личностный профиль хранит ответы парами «вопрос-ответ», остальные
                # свои вопросы — JSON-списком в text_2 (слот extra)
                slot="personal" if section == "personal" else "extra",
                is_builtin=False,
            )
            db.add(q)
        q.text = text
        q.sort_order = (i + 1) * 10
        q.is_active = True
        if q.slot in ("extra", "personal"):
            q.answer_type = "text" if section == "personal" else answer_type
        if q.id:
            kept.add(q.id)

    # строки, которых в форме не осталось: свои удаляем, базовые прячем — их
    # слоты (оценка eNPS, основной текст) держат на себе сводки и ИИ-отчёт
    for q in existing.values():
        if q.id in kept:
            continue
        if q.is_builtin:
            q.is_active = False
        else:
            db.delete(q)

    db.commit()
    return RedirectResponse(url=_questions_url(position_id, "saved=1"), status_code=302)


@router.post("/questions/{section}/customize")
@login_required
async def customize_section(request: Request, section: str, db: Session = Depends(get_db)):
    """«Задать свои вопросы для должности» — копирует общие вопросы раздела
    должности, чтобы HR правил их, а не начинал с чистого листа."""
    form = await request.form()
    position_id = int(form["position_id"]) if (form.get("position_id") or "").isdigit() else None
    if section not in HR_INPUT_SECTIONS or not position_id:
        return RedirectResponse(url="/hr/questions", status_code=302)

    already = db.query(HrQuestion).filter(
        HrQuestion.section == section, HrQuestion.position_id == position_id).count()
    if not already:
        for i, src in enumerate(_section_questions(db, None).get(section, [])):
            db.add(HrQuestion(
                section=section,
                # ключ должностной копии свой: по нему подписывается ответ, а пара
                # (раздел, ключ) в справочнике уникальна
                key=f"p{position_id}_{src.key}"[:32],
                position_id=position_id,
                slot=src.slot,
                answer_type=src.answer_type,
                group_title=src.group_title,
                text=src.text,
                hint=src.hint,
                sort_order=(i + 1) * 10,
                is_active=True,
                is_builtin=False,
            ))
        db.commit()
    return RedirectResponse(url=_questions_url(position_id, "saved=1"), status_code=302)


@router.post("/questions/{section}/reset")
@login_required
async def reset_section(request: Request, section: str, db: Session = Depends(get_db)):
    """Возврат раздела к общим вопросам: у должности — удаляем её набор,
    у общих — возвращаем спрятанные базовые вопросы."""
    form = await request.form()
    position_id = int(form["position_id"]) if (form.get("position_id") or "").isdigit() else None
    if section not in HR_INPUT_SECTIONS:
        return RedirectResponse(url="/hr/questions", status_code=302)

    if position_id:
        for q in db.query(HrQuestion).filter(
                HrQuestion.section == section, HrQuestion.position_id == position_id).all():
            db.delete(q)
    else:
        for q in db.query(HrQuestion).filter(
                HrQuestion.section == section, HrQuestion.position_id.is_(None),
                HrQuestion.is_builtin == True).all():
            q.is_active = True
    db.commit()
    return RedirectResponse(url=_questions_url(position_id, "saved=1"), status_code=302)


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
async def close_vacancy(
    request: Request, vacancy_id: int,
    closed_at: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Закрывает вакансию (или правит дату уже закрытой) — дата вводится вручную,
    пустое/некорректное значение падает на сегодня."""
    vac = db.query(HrVacancy).filter(HrVacancy.id == vacancy_id).first()
    if vac:
        try:
            vac.closed_at = date.fromisoformat(closed_at) if closed_at else date.today()
        except ValueError:
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


@router.post("/vacancies/{vacancy_id}/edit")
@login_required
async def edit_vacancy(
    request: Request, vacancy_id: int,
    title: str = Form(default=""),
    opened_at: str = Form(default=""),
    closed_at: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Правка вакансии целиком: название и обе даты. Раньше можно было менять
    только дату закрытия, а опечатку в дате открытия — уже нет, хотя именно от
    неё считается срок."""
    vac = db.query(HrVacancy).filter(HrVacancy.id == vacancy_id).first()
    if vac:
        vac.title = title.strip() or vac.title
        try:
            vac.opened_at = date.fromisoformat(opened_at) if opened_at else vac.opened_at
        except ValueError:
            pass
        try:
            vac.closed_at = date.fromisoformat(closed_at) if closed_at else None
        except ValueError:
            pass
        db.commit()
    return RedirectResponse(url="/hr/", status_code=302)


@router.post("/vacancies/{vacancy_id}/delete")
@login_required
async def delete_vacancy(request: Request, vacancy_id: int, db: Session = Depends(get_db)):
    vac = db.query(HrVacancy).filter(HrVacancy.id == vacancy_id).first()
    if vac:
        db.delete(vac)
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
    qmap = _section_questions(db, employee.position_id)

    return templates.TemplateResponse(request, "hr/entry.html", {
        "employee": employee,
        "records": records,
        "sections": employee.enabled_sections,
        "section_questions": qmap,
        "personal_qa": _personal_qa(qmap, records.get("personal")),
        "extra_answers": _extra_answers_map(records),
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
    sections = [s for s in HR_INPUT_SECTIONS if form.get(f"sec_{s}")]
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


# ── Публичная форма опроса (без входа в NERPA) ─────────────────────────────────

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
    """Уведомляет HR/admin (колокольчик в NERPA) о первом ответе сотрудника на раунд."""
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
    qmap = _section_questions(db, tok.employee.position_id)
    return templates.TemplateResponse(request, "hr/survey_public.html", {
        "invalid": False,
        "token": token,
        "employee": tok.employee,
        "survey": survey,
        "sections": sections,
        "records": records,
        "section_questions": qmap,
        "personal_qa": _personal_qa(qmap, records.get("personal")),
        "extra_answers": _extra_answers_map(records),
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
    tok.submitted_at = msk_now()
    if is_first_submit:
        _notify_survey_answered(db, tok)
    db.commit()
    return RedirectResponse(url=f"/hr/s/{token}?done=1", status_code=302)

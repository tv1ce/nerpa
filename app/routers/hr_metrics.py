"""Метрика сотрудников — еженедельный срез.

Раздел «Метрика» в HR-отчёте собирался текстом раз в полмесяца, и по нему нельзя
было ответить на главный вопрос: у скольких людей метрика растёт. Здесь метрика —
это числовой ряд по неделям: руководитель подразделения раз в неделю вносит цифры
по своим людям (в TMS или по постоянной внешней ссылке), а система сама считает
динамику, выполнение цели и сводную метрику HR.

Неделя везде хранится датой понедельника (ISO) — так недели сравниваются и
сортируются корректно на стыке месяцев.
"""

import asyncio
import csv
import io
import logging
import os
import re
import secrets
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import login_required
from app.models import (
    HrEmployee, HrMetric, HrMetricValue, HrMetricToken, CompanySettings,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hr", tags=["hr-metrics"])
templates = Jinja2Templates(directory="app/templates")

# Сколько недель показывать в таблице по умолчанию
DEFAULT_WEEKS = 4
MAX_WEEKS = 26

MONTHS_GEN = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]
MONTHS_NOM = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
              "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]


# ── Недели ────────────────────────────────────────────────────────────────────

def _week_start(d: date) -> date:
    """Понедельник недели, в которую попадает дата."""
    return d - timedelta(days=d.weekday())


def _parse_week(raw: str | None) -> date:
    """Понедельник недели из строки YYYY-MM-DD (любой день недели). По умолчанию — текущая."""
    if raw:
        try:
            return _week_start(date.fromisoformat(raw.strip()))
        except (ValueError, TypeError):
            pass
    return _week_start(date.today())


def _week_range(anchor: date, count: int) -> list[date]:
    """Список понедельников: count недель, заканчивая неделей anchor (по возрастанию)."""
    return [anchor - timedelta(weeks=i) for i in range(count - 1, -1, -1)]


def _week_label(ws: date) -> str:
    """«21–27 июля» — понятная человеку подпись недели."""
    we = ws + timedelta(days=6)
    if ws.month == we.month:
        return f"{ws.day}–{we.day} {MONTHS_GEN[ws.month]}"
    return f"{ws.day} {MONTHS_GEN[ws.month]} – {we.day} {MONTHS_GEN[we.month]}"


def _week_no_in_month(ws: date) -> int:
    """Номер недели внутри месяца — как «Июль 1…5» в исходной таблице."""
    return (ws.day - 1) // 7 + 1


def _week_head(ws: date) -> dict:
    """Данные для шапки колонки недели."""
    return {
        "start": ws,
        "iso": ws.isoformat(),
        "label": _week_label(ws),
        "short": f"{ws.day:02d}.{ws.month:02d}",
        "month": MONTHS_NOM[ws.month],
        "no": _week_no_in_month(ws),
        "is_current": ws == _week_start(date.today()),
        "is_future": ws > _week_start(date.today()),
    }


# ── Разбор и форматирование чисел ────────────────────────────────────────────

def _num(raw) -> float | None:
    """Мягкий разбор числа из формы: «1 344», «97,5», «1 344.00» → float."""
    if raw is None:
        return None
    s = str(raw).replace(" ", "").replace(" ", "").replace(",", ".").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ── Цель одной строкой ───────────────────────────────────────────────────────
# HR формулирует метрику словами — «не менее 97%», «не более 2 шт.», «1500 шт.».
# Разбираем эту строку в цель + направление + единицу, чтобы не заставлять
# заполнять три отдельных поля. Что распозналось — показываем обратно текстом,
# так что «магия» остаётся проверяемой (см. _target_hint).

_LESS_IS_BETTER = ("не более", "не выше", "не больше", "до", "максимум", "<=", "=<", "≤", "<")
_MORE_IS_BETTER = ("не менее", "не ниже", "не меньше", "от", "минимум", ">=", "=>", "≥", ">")

_TARGET_RE = re.compile(
    r"^\s*(?P<cmp>[^\d\-+]*)?\s*(?P<num>-?[\d][\d\s .,]*)\s*(?P<unit>.*)$")


def parse_target(raw: str | None, two_numbers: bool = False) -> dict:
    """«не менее 97%» → {target: 97, direction: 'up', kind: 'percent', unit: '%'}.

    two_numbers — руководитель вводит «всего» и «с ошибкой»: тип всегда ratio,
    единица всегда процент, что бы ни было написано в строке цели."""
    out = {"target": None, "direction": "up", "kind": "number", "unit": None}
    text = (raw or "").strip()

    if text:
        m = _TARGET_RE.match(text)
        if m:
            out["target"] = _num(m.group("num"))
            cmp_part = (m.group("cmp") or "").strip().casefold()
            unit = (m.group("unit") or "").strip()
            # сравнение может стоять и после числа («2% максимум»)
            haystack = f"{cmp_part} {unit.casefold()}"
            if any(w in haystack for w in _LESS_IS_BETTER):
                out["direction"] = "down"
            elif any(w in haystack for w in _MORE_IS_BETTER):
                out["direction"] = "up"
            # единицу чистим от слов сравнения, чтобы не осталось «шт. максимум»
            for word in _LESS_IS_BETTER + _MORE_IS_BETTER:
                unit = re.sub(re.escape(word), "", unit, flags=re.I)
            out["unit"] = unit.strip(" .,") or None

    unit_l = (out["unit"] or "").casefold()
    if two_numbers:
        out["kind"], out["unit"] = "ratio", "%"
    elif "%" in unit_l or "%" in text:
        out["kind"], out["unit"] = "percent", "%"
    elif "₽" in unit_l or "руб" in unit_l:
        out["kind"], out["unit"] = "money", "₽"
    else:
        out["kind"] = "number"
    return out


def _target_hint(parsed: dict) -> str:
    """Человеческая расшифровка разобранной цели — показывается рядом с полем."""
    if parsed["target"] is None:
        return "цель не задана — в таблице будет видна только динамика"
    unit = parsed["unit"] or ""
    if unit and unit not in ("%",):      # «97%» слитно, «2 шт» — через пробел
        unit = " " + unit
    side = "чем больше — тем лучше" if parsed["direction"] == "up" else "чем меньше — тем лучше"
    return f"цель {_plain(parsed['target'])}{unit}, {side}"


def _plain(value: float | None) -> str:
    """Число как его удобно править в поле ввода: без единиц и лишних нулей."""
    if value is None:
        return ""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _fmt(value: float | None, metric: HrMetric) -> str:
    """Значение в виде, пригодном для таблицы и Telegram."""
    if value is None:
        return "—"
    if metric.kind in ("percent", "ratio"):
        return f"{value:.1f}".rstrip("0").rstrip(".") + "%"
    if metric.kind == "money":
        return f"{value:,.0f}".replace(",", " ") + " ₽"
    text = f"{value:,.2f}".rstrip("0").rstrip(".").replace(",", " ")
    unit = metric.unit_label
    return f"{text} {unit}".strip()


def _compute_value(metric: HrMetric, value, raw_total, raw_bad) -> float | None:
    """Итоговое значение метрики. Для ratio считаем долю без ошибок сами — в
    исходной таблице этот процент считали руками и путали (25% ошибок писали
    как «-25%» при цели «не менее 98% без ошибок»)."""
    if metric.kind != "ratio":
        return value
    if not raw_total:
        return None
    bad = raw_bad or 0
    return max(0.0, (raw_total - bad) / raw_total * 100.0)


# ── Сборка таблицы ───────────────────────────────────────────────────────────

def _metric_rows(db: Session, metrics: list[HrMetric], weeks: list[date]) -> list[dict]:
    """Строки таблицы: по метрике — ячейки за показанные недели, динамика и статус.

    Динамика считается по последним ДВУМ заполненным неделям (не обязательно
    соседним): пропуск недели не должен выглядеть как падение до нуля."""
    if not metrics:
        return []

    ids = [m.id for m in metrics]
    values = (db.query(HrMetricValue)
              .filter(HrMetricValue.metric_id.in_(ids))
              .order_by(HrMetricValue.week_start).all())
    by_metric: dict[int, dict[date, HrMetricValue]] = defaultdict(dict)
    for v in values:
        by_metric[v.metric_id][v.week_start] = v

    rows = []
    for m in metrics:
        hist = by_metric.get(m.id, {})
        filled = [(ws, rec) for ws, rec in sorted(hist.items()) if rec.value is not None]

        last = filled[-1] if filled else None
        prev = filled[-2] if len(filled) > 1 else None
        delta = delta_pct = None
        trend = "none"
        if last and prev:
            delta = last[1].value - prev[1].value
            if prev[1].value:
                delta_pct = delta / abs(prev[1].value) * 100
            trend = "flat" if abs(delta) < 1e-9 else ("up" if delta > 0 else "down")

        # «растёт» — это движение в сторону, которая для метрики хорошая
        growing = (trend == "up") if m.better_higher else (trend == "down")

        cells = []
        for ws in weeks:
            rec = hist.get(ws)
            val = rec.value if rec else None
            status = m.status_for(val)
            if status == "neutral":
                # цели нет — красим по динамике относительно предыдущей заполненной недели
                earlier = [r.value for w, r in filled if w < ws]
                if earlier and val is not None:
                    better = val > earlier[-1] if m.better_higher else val < earlier[-1]
                    status = "ok" if better else ("neutral" if val == earlier[-1] else "bad")
            cells.append({
                "week": ws,
                "iso": ws.isoformat(),
                "value": val,
                "text": _fmt(val, m) if val is not None else "",
                "input": _plain(val),
                "total_input": _plain(rec.raw_total if rec else None),
                "bad_input": _plain(rec.raw_bad if rec else None),
                "raw_total": rec.raw_total if rec else None,
                "raw_bad": rec.raw_bad if rec else None,
                "comment": (rec.comment or "") if rec else "",
                "author": (rec.filled_by_name or "") if rec else "",
                "status": status,
                "filled": val is not None,
            })

        rows.append({
            "metric": m,
            "employee": m.employee,
            "cells": cells,
            "last_value": last[1].value if last else None,
            "last_text": _fmt(last[1].value, m) if last else "—",
            "last_week": last[0] if last else None,
            "last_status": m.status_for(last[1].value) if last else "none",
            "delta": delta,
            "delta_text": ("—" if delta is None else
                           ("+" if delta > 0 else "") + _fmt(delta, m).lstrip("+")),
            "delta_pct": delta_pct,
            "trend": trend,
            "growing": growing,
            "spark": [r.value for _w, r in filled[-12:]],
            "target_text": _fmt(m.target, m) if m.target is not None else "",
            # для формы редактирования: цель как её ввёл человек + признак «два числа»
            "target_raw": m.target_text or (_plain(m.target) if m.target is not None else ""),
            "two_numbers": m.kind == "ratio",
        })
    return rows


def _kpis(rows: list[dict], weeks: list[date]) -> dict:
    """Сводка над таблицей. Главное число — метрика HR из исходной таблицы:
    «сколько сотрудников, чья метрика за последнюю неделю растёт»."""
    # Опорная неделя — последняя, за которую хоть что-то заполнено
    focus = None
    for ws in reversed(weeks):
        if any(c["filled"] for r in rows for c in r["cells"] if c["week"] == ws):
            focus = ws
            break

    compared = [r for r in rows if r["trend"] in ("up", "down", "flat")]
    growing = [r for r in compared if r["growing"]]
    falling = [r for r in compared if r["trend"] != "flat" and not r["growing"]]

    targeted = [r for r in rows if r["metric"].target is not None and r["last_value"] is not None]
    on_target = [r for r in targeted if r["last_status"] == "ok"]

    current = _week_start(date.today())
    cur_cells = [c for r in rows for c in r["cells"] if c["week"] == current]
    filled_now = sum(1 for c in cur_cells if c["filled"])

    return {
        "focus_week": focus,
        "focus_label": _week_label(focus) if focus else "—",
        "total": len(rows),
        "compared": len(compared),
        "growing": len(growing),
        "falling": len(falling),
        "growing_pct": round(len(growing) / len(compared) * 100) if compared else 0,
        "targeted": len(targeted),
        "on_target": len(on_target),
        "on_target_pct": round(len(on_target) / len(targeted) * 100) if targeted else 0,
        "filled_now": filled_now,
        "expected_now": len(rows),
        "filled_pct": round(filled_now / len(rows) * 100) if rows else 0,
    }


def _active_metrics(db: Session, manager_id: int | None = None,
                    employee_ids: list[int] | None = None) -> list[HrMetric]:
    """Активные метрики активных сотрудников, при необходимости — среза подразделения."""
    q = (db.query(HrMetric)
         .join(HrEmployee, HrMetric.employee_id == HrEmployee.id)
         .filter(HrMetric.is_active == True, HrEmployee.is_active == True))
    if employee_ids is not None:
        q = q.filter(HrMetric.employee_id.in_(employee_ids or [-1]))
    elif manager_id:
        q = q.filter((HrEmployee.manager_id == manager_id) | (HrEmployee.id == manager_id))
    return q.order_by(HrEmployee.full_name, HrMetric.sort_order, HrMetric.id).all()


def employee_metric_history(db: Session, employee_id: int, weeks_count: int = 12) -> dict:
    """Недельная история метрик одного сотрудника — для его профайла в HR
    (публичный хелпер, используется routers/hr.py)."""
    metrics = (db.query(HrMetric)
               .filter(HrMetric.employee_id == employee_id)
               .order_by(HrMetric.is_active.desc(), HrMetric.sort_order, HrMetric.id).all())
    week_list = _week_range(_week_start(date.today()), weeks_count)
    return {"weeks": [_week_head(w) for w in week_list],
            "rows": _metric_rows(db, metrics, week_list)}


def month_metric_lines(db: Session, period_date: date) -> dict[int, list[str]]:
    """{employee_id: ["Заказы без ошибок: 95% → 100% → 100% · цель 97%"]} за месяц —
    для месячного отчёта HR (публичный хелпер, используется routers/hr.py).

    В месяц попадают недели, которые в нём начинаются."""
    metrics = _active_metrics(db)
    if not metrics:
        return {}
    next_month = (period_date.replace(day=28) + timedelta(days=4)).replace(day=1)
    values = (db.query(HrMetricValue)
              .filter(HrMetricValue.metric_id.in_([m.id for m in metrics]),
                      HrMetricValue.week_start >= period_date,
                      HrMetricValue.week_start < next_month,
                      HrMetricValue.value.isnot(None))
              .order_by(HrMetricValue.week_start).all())
    by_metric: dict[int, list] = defaultdict(list)
    for v in values:
        by_metric[v.metric_id].append(v)

    out: dict[int, list[str]] = defaultdict(list)
    for m in metrics:
        series = by_metric.get(m.id)
        if not series:
            continue
        chain = " → ".join(_fmt(v.value, m) for v in series)
        target = f" · цель {_fmt(m.target, m)}" if m.target is not None else ""
        out[m.employee_id].append(f"{m.title}: {chain}{target}")
    return dict(out)


def _scope_employee_ids(db: Session, manager_id: int | None) -> list[int]:
    """Кого охватывает ссылка/фильтр руководителя: его подчинённые + он сам.
    Пустой manager_id — вся компания."""
    q = db.query(HrEmployee.id).filter(HrEmployee.is_active == True)
    if manager_id:
        q = q.filter((HrEmployee.manager_id == manager_id) | (HrEmployee.id == manager_id))
    return [row[0] for row in q.all()]


# ── Основная вкладка ─────────────────────────────────────────────────────────

@router.get("/metrics", response_class=HTMLResponse)
@login_required
async def metrics_board(request: Request, week: str = "", weeks: int = DEFAULT_WEEKS,
                        manager: str = "", db: Session = Depends(get_db)):
    anchor = _parse_week(week)
    count = max(3, min(MAX_WEEKS, weeks))
    week_list = _week_range(anchor, count)

    manager_id = int(manager) if manager.isdigit() else None
    metrics = _active_metrics(db, manager_id=manager_id)
    rows = _metric_rows(db, metrics, week_list)

    employees = (db.query(HrEmployee)
                 .filter(HrEmployee.is_active == True)
                 .order_by(HrEmployee.full_name).all())
    # руководители = те, у кого есть подчинённые
    manager_ids = {e.manager_id for e in employees if e.manager_id}
    managers = [e for e in employees if e.id in manager_ids]

    # группировка строк по руководителю — «подразделения» в таблице
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        mgr = r["employee"].manager
        groups[mgr.full_name if mgr else "Без руководителя"].append(r)

    company = db.query(CompanySettings).first()
    default_chat_ids = (company.tg_hr_report_chat_ids or company.tg_report_chat_ids or "") if company else ""

    return templates.TemplateResponse(request, "hr/metrics.html", {
        "weeks": [_week_head(w) for w in week_list],
        "rows": rows,
        "groups": sorted(groups.items()),
        "kpi": _kpis(rows, week_list),
        "employees": employees,
        "managers": managers,
        "manager_id": manager_id,
        "anchor": anchor.isoformat(),
        "prev_anchor": (anchor - timedelta(weeks=4)).isoformat(),
        "next_anchor": (anchor + timedelta(weeks=4)).isoformat(),
        "this_week": _week_start(date.today()).isoformat(),
        "week_count": count,
        "default_chat_ids": default_chat_ids,
        "saved": request.query_params.get("saved"),
        "report": request.query_params.get("report"),
    })


# ── Справочник метрик ────────────────────────────────────────────────────────

def _metric_from_form(metric: HrMetric, form) -> None:
    """Заполняет метрику из упрощённой формы: название, цель строкой и признак
    «вводятся два числа». Тип, направление и единицы выводятся из цели —
    отдельных полей под них в форме больше нет."""
    metric.title = (form.get("title") or "").strip() or metric.title
    metric.formula = (form.get("formula") or "").strip() or None
    metric.target_text = (form.get("target_text") or "").strip() or None

    two_numbers = bool(form.get("two_numbers"))
    parsed = parse_target(metric.target_text, two_numbers)
    metric.target = parsed["target"]
    metric.direction = parsed["direction"]
    metric.kind = parsed["kind"]
    metric.unit = parsed["unit"]
    metric.sort_order = int(_num(form.get("sort_order")) or 0)


@router.post("/metrics/new")
@login_required
async def metric_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    employee_id = form.get("employee_id")
    title = (form.get("title") or "").strip()
    if not (employee_id and str(employee_id).isdigit() and title):
        return RedirectResponse(url="/hr/metrics?saved=error", status_code=302)

    metric = HrMetric(employee_id=int(employee_id), title=title,
                      created_by=request.session.get("user_id"))
    _metric_from_form(metric, form)
    db.add(metric)
    db.commit()
    return RedirectResponse(url="/hr/metrics?saved=1", status_code=302)


@router.post("/metrics/{metric_id}/edit")
@login_required
async def metric_edit(request: Request, metric_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    metric = db.query(HrMetric).filter(HrMetric.id == metric_id).first()
    if metric:
        _metric_from_form(metric, form)
        metric.is_active = bool(form.get("is_active"))
        db.commit()
    return RedirectResponse(url="/hr/metrics?saved=1", status_code=302)


@router.post("/metrics/{metric_id}/delete")
@login_required
async def metric_delete(request: Request, metric_id: int, db: Session = Depends(get_db)):
    """Удаляет метрику вместе с её недельными значениями. Чтобы не терять историю,
    метрику обычно достаточно деактивировать — удаление оставлено для ошибок ввода."""
    metric = db.query(HrMetric).filter(HrMetric.id == metric_id).first()
    if metric:
        db.delete(metric)   # значения удалятся каскадом
        db.commit()
    return RedirectResponse(url="/hr/metrics?saved=deleted", status_code=302)


# ── Автосохранение ячейки (AJAX из таблицы) ──────────────────────────────────

def _upsert_value(db: Session, metric: HrMetric, ws: date, payload: dict,
                  user_id: int | None, author_name: str | None) -> HrMetricValue | None:
    """Создаёт/обновляет значение недели. Полностью пустой ввод удаляет запись —
    так руководитель может стереть ошибочную цифру, а не оставлять ноль."""
    value = _num(payload.get("value"))
    raw_total = _num(payload.get("raw_total"))
    raw_bad = _num(payload.get("raw_bad"))
    comment = (payload.get("comment") or "").strip() or None

    rec = db.query(HrMetricValue).filter(
        HrMetricValue.metric_id == metric.id, HrMetricValue.week_start == ws).first()

    computed = _compute_value(metric, value, raw_total, raw_bad)
    if computed is None and raw_total is None and raw_bad is None and not comment:
        if rec:
            db.delete(rec)
        return None

    if not rec:
        rec = HrMetricValue(metric_id=metric.id, week_start=ws)
        db.add(rec)
    rec.value = computed
    rec.raw_total = raw_total
    rec.raw_bad = raw_bad
    rec.comment = comment
    rec.filled_by = user_id
    rec.filled_by_name = author_name
    return rec


@router.post("/metrics/value")
@login_required
async def metric_value_save(request: Request, db: Session = Depends(get_db)):
    """Автосохранение одной ячейки таблицы. Возвращает пересчитанное значение и
    статус, чтобы таблица перекрасилась без перезагрузки."""
    payload = await request.json()
    metric = db.query(HrMetric).filter(HrMetric.id == int(payload.get("metric_id", 0))).first()
    if not metric:
        return JSONResponse({"ok": False, "error": "Метрика не найдена"}, status_code=404)
    try:
        ws = _week_start(date.fromisoformat(payload["week"]))
    except (KeyError, ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "Некорректная неделя"}, status_code=400)

    rec = _upsert_value(db, metric, ws, payload,
                        request.session.get("user_id"),
                        request.session.get("user_name"))
    db.commit()

    value = rec.value if rec else None
    return JSONResponse({
        "ok": True,
        "value": value,
        "text": _fmt(value, metric) if value is not None else "",
        "status": metric.status_for(value),
    })


# ── Ссылки для руководителей ─────────────────────────────────────────────────

@router.post("/metrics/link/{manager_id}")
@login_required
async def metric_link(request: Request, manager_id: int, refresh: int = 0,
                      db: Session = Depends(get_db)):
    """Ссылка руководителя на еженедельную форму. Отдельного управления ссылками
    нет: она создаётся при первом запросе и дальше просто копируется. refresh=1
    перевыпускает токен — старая ссылка сразу перестаёт работать."""
    tok = db.query(HrMetricToken).filter(
        HrMetricToken.manager_id == manager_id).order_by(HrMetricToken.id.desc()).first()
    if tok and refresh:
        tok.token = secrets.token_urlsafe(24)
        tok.is_active = True
    elif not tok:
        tok = HrMetricToken(manager_id=manager_id, token=secrets.token_urlsafe(24),
                            created_by=request.session.get("user_id"))
        db.add(tok)
    else:
        tok.is_active = True
    db.commit()

    base = str(request.base_url).rstrip("/")
    return JSONResponse({"ok": True, "url": f"{base}/hr/w/{tok.token}",
                         "used": tok.last_used_at.strftime("%d.%m.%Y") if tok.last_used_at else None})


# ── Публичная еженедельная форма руководителя (без входа в TMS) ──────────────

_RATE_WINDOW = 60
_RATE_MAX = 60
_hits: dict[str, list] = defaultdict(list)


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    bucket = _hits[ip]
    bucket[:] = [t for t in bucket if t > now - _RATE_WINDOW]
    if len(bucket) >= _RATE_MAX:
        return True
    bucket.append(now)
    if len(_hits) > 2048:
        for k in [k for k, v in list(_hits.items()) if not v]:
            _hits.pop(k, None)
    return False


def _load_token(db: Session, token: str) -> HrMetricToken | None:
    return db.query(HrMetricToken).filter(
        HrMetricToken.token == token, HrMetricToken.is_active == True).first()


def _week_form_context(db: Session, tok: HrMetricToken, ws: date) -> dict:
    """Данные формы недели: метрики подразделения + значения этой недели и
    три предыдущие недели для контекста («а сколько было в прошлый раз»).

    Руководитель заполняет и свою метрику тоже — его карточка идёт первой и
    помечена, иначе про неё забывают, приняв ссылку за форму «на подчинённых»."""
    metrics = _active_metrics(db, employee_ids=_scope_employee_ids(db, tok.manager_id))
    history_weeks = _week_range(ws, 4)
    rows = _metric_rows(db, metrics, history_weeks)
    for r in rows:
        r["current"] = next(c for c in r["cells"] if c["week"] == ws)
        r["history"] = [c for c in r["cells"] if c["week"] != ws]

    groups: dict[int, dict] = {}
    for r in rows:
        emp = r["employee"]
        group = groups.setdefault(emp.id, {
            "employee": emp,
            "name": emp.full_name,
            "is_self": emp.id == tok.manager_id,
            "rows": [],
        })
        group["rows"].append(r)
    # своя карточка первой, остальные по алфавиту
    by_employee = sorted(groups.values(), key=lambda g: (not g["is_self"], g["name"]))

    # Подставляем в «Кто заполнил» имя из последней записи по этому подразделению —
    # руководитель не должен представляться заново каждую неделю
    last_author = (db.query(HrMetricValue.filled_by_name)
                   .filter(HrMetricValue.metric_id.in_([m.id for m in metrics] or [-1]),
                           HrMetricValue.filled_by_name.is_not(None))
                   .order_by(HrMetricValue.updated_at.desc()).limit(1).scalar())

    return {
        "rows": rows,
        "by_employee": by_employee,
        "filled": sum(1 for r in rows if r["current"]["filled"]),
        "total": len(rows),
        "last_author": (tok.manager.full_name if tok.manager else None) or last_author or "",
    }


@router.get("/w/{token}", response_class=HTMLResponse)
async def week_form(request: Request, token: str, week: str = "",
                    db: Session = Depends(get_db)):
    if _rate_limited(request.client.host if request.client else "?"):
        return HTMLResponse("<h3>Слишком много запросов, попробуйте позже.</h3>", status_code=429)
    tok = _load_token(db, token)
    if not tok:
        return templates.TemplateResponse(request, "hr/metrics_week_public.html",
                                          {"invalid": True}, status_code=404)

    ws = _parse_week(week)
    ctx = _week_form_context(db, tok, ws)
    return templates.TemplateResponse(request, "hr/metrics_week_public.html", {
        "invalid": False,
        "token": token,
        "tok": tok,
        # подпись прямо говорит, что заполнять надо и за себя — иначе ссылку
        # читают как «форму на подчинённых» и свою метрику пропускают
        "scope_title": tok.label or (f"{tok.manager.full_name} — за себя и своих сотрудников"
                                     if tok.manager else "Вся компания"),
        "week": ws.isoformat(),
        "week_label": _week_label(ws),
        "week_no": _week_no_in_month(ws),
        "month": MONTHS_NOM[ws.month],
        "prev_week": (ws - timedelta(weeks=1)).isoformat(),
        "next_week": (ws + timedelta(weeks=1)).isoformat(),
        "this_week": _week_start(date.today()).isoformat(),
        "is_future": ws > _week_start(date.today()),
        "done": request.query_params.get("done"),
        **ctx,
    })


@router.post("/w/{token}")
async def week_form_save(request: Request, token: str, db: Session = Depends(get_db)):
    if _rate_limited(request.client.host if request.client else "?"):
        return HTMLResponse("<h3>Слишком много запросов, попробуйте позже.</h3>", status_code=429)
    tok = _load_token(db, token)
    if not tok:
        return templates.TemplateResponse(request, "hr/metrics_week_public.html",
                                          {"invalid": True}, status_code=404)

    form = await request.form()
    ws = _parse_week(form.get("week"))
    author = (form.get("author") or "").strip() or (
        tok.manager.full_name if tok.manager else None)

    allowed = {m.id for m in _active_metrics(db, employee_ids=_scope_employee_ids(db, tok.manager_id))}
    for metric in db.query(HrMetric).filter(HrMetric.id.in_(allowed or {-1})).all():
        _upsert_value(db, metric, ws, {
            "value": form.get(f"m{metric.id}_value"),
            "raw_total": form.get(f"m{metric.id}_total"),
            "raw_bad": form.get(f"m{metric.id}_bad"),
            "comment": form.get(f"m{metric.id}_comment"),
        }, None, author)

    tok.last_used_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(url=f"/hr/w/{token}?week={ws.isoformat()}&done=1", status_code=302)


# ── Выгрузка CSV ─────────────────────────────────────────────────────────────

@router.get("/metrics/export.csv")
@login_required
async def metrics_export(request: Request, week: str = "", weeks: int = DEFAULT_WEEKS,
                         db: Session = Depends(get_db)):
    anchor = _parse_week(week)
    count = max(3, min(MAX_WEEKS, weeks))
    week_list = _week_range(anchor, count)
    rows = _metric_rows(db, _active_metrics(db), week_list)

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["ФИО", "Должность", "Метрика", "Цель", "Тренд"] +
                    [_week_label(w) for w in week_list])
    for r in rows:
        m = r["metric"]
        writer.writerow([
            r["employee"].full_name, r["employee"].position_title or "",
            m.title, r["target_text"],
            {"up": "растёт", "down": "падает", "flat": "без изменений"}.get(r["trend"], ""),
        ] + [c["text"] for c in r["cells"]])

    data = "﻿" + buf.getvalue()   # BOM — чтобы Excel не ломал кириллицу
    filename = f"metrics-{anchor.isoformat()}.csv"
    return StreamingResponse(
        iter([data.encode("utf-8")]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ── Отчёт недели в Telegram ──────────────────────────────────────────────────

def _format_week_report(rows: list[dict], kpi: dict, ws: date) -> str:
    arrows = {"up": "↑", "down": "↓", "flat": "→", "none": ""}
    lines = [f"📈 **Метрики сотрудников — неделя {_week_label(ws)}**", ""]

    if kpi["compared"]:
        lines.append(f"**Растут: {kpi['growing']} из {kpi['compared']}** "
                     f"({kpi['growing_pct']}%) · падают: {kpi['falling']}")
    if kpi["targeted"]:
        lines.append(f"На цели: {kpi['on_target']} из {kpi['targeted']} метрик")
    lines.append(f"Заполнено за неделю: {kpi['filled_now']} из {kpi['expected_now']}")
    lines.append("")

    by_manager: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        mgr = r["employee"].manager
        by_manager[mgr.full_name if mgr else "Без руководителя"].append(r)

    for manager, group in sorted(by_manager.items()):
        lines.append(f"**{manager}**")
        for r in group:
            cell = next((c for c in r["cells"] if c["week"] == ws), None)
            filled = bool(cell and cell["filled"])
            if not filled:
                mark, value = "▫️", "не заполнено"
            else:
                value = cell["text"]
                # с целью — светофор по её выполнению, без цели — по направлению движения
                mark = {"ok": "✅", "warn": "🟡", "bad": "🔴"}.get(r["last_status"]) or (
                    "📈" if r["growing"] else
                    "▪️" if r["trend"] in ("flat", "none") else "📉")
            delta = ""
            if filled and r["delta"] is not None:
                delta = f" ({arrows[r['trend']]} {r['delta_text']})"
            target = f" · цель {r['target_text']}" if r["target_text"] else ""
            lines.append(f"{mark} {r['employee'].full_name} — {r['metric'].title}: "
                         f"{value}{delta}{target}")
        lines.append("")

    return "\n".join(lines).strip()


@router.post("/metrics/report/telegram")
@login_required
async def metrics_report_telegram(request: Request, week: str = Form(default=""),
                                  chat_ids: str = Form(default=""),
                                  db: Session = Depends(get_db)):
    from app.routers.settings import _normalize_chat_ids
    from app.services import telegram_send

    ws = _parse_week(week)
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)

    normalized = _normalize_chat_ids(chat_ids)
    if normalized:
        company.tg_hr_report_chat_ids = normalized
    db.commit()

    ids = telegram_send.parse_chat_ids(
        company.tg_hr_report_chat_ids or company.tg_report_chat_ids or "")
    if not ids:
        return RedirectResponse(url="/hr/metrics?report=nochat", status_code=302)

    bot_token = (company.tg_bot_token or "").strip() or os.getenv("TMS_BOT_TOKEN", "").strip()
    if not bot_token:
        return RedirectResponse(url="/hr/metrics?report=notoken", status_code=302)

    try:
        week_list = _week_range(ws, DEFAULT_WEEKS)
        rows = _metric_rows(db, _active_metrics(db), week_list)
        text = _format_week_report(rows, _kpis(rows, week_list), ws)
        mdv2 = telegram_send.ai_text_to_mdv2(text)
        await asyncio.to_thread(telegram_send.send_markdown, ids, mdv2, bot_token)
    except Exception:
        logger.exception("Не удалось отправить отчёт по метрикам в Telegram")
        return RedirectResponse(url="/hr/metrics?report=error", status_code=302)

    return RedirectResponse(url="/hr/metrics?report=ok", status_code=302)

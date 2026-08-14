"""Пятничные уведомления по метрике сотрудников.

Метрика сдаётся раз в неделю, и весь смысл ряда теряется, если руководитель про
неделю просто забыл. Отсюда два сообщения в Telegram (рассылает бот — см.
bot/main.py, там живут job_queue и коннект через прокси):

  REMIND (пт 12:00) — руководителям: «внесите метрики по своим сотрудникам»,
                      с остатком по каждому подразделению;
  CHECK  (пт 17:30) — HR: кто из руководителей ещё не сдал.

Личных ссылок на форму недели (/hr/w/{token}) в сообщениях нет: они пускают в
метрику подразделения без входа в TMS, а чат уведомлений общий — ссылку
руководитель получает лично, с доски метрик.

Каждое включается отдельным флагом в «Настройки → Telegram»; чат тоже задаётся
отдельно, а если не задан — берётся чат HR-отчёта, затем общий чат отчётов.

Текст сознательно без Markdown-разметки: в нём ФИО и ссылки с «-», «_» и «.»,
которые в MarkdownV2 пришлось бы экранировать — одна пропущенная экранировка, и
Telegram отклоняет сообщение целиком, то есть напоминание просто не приходит.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date

from sqlalchemy.orm import Session

from app.models import CompanySettings, HrEmployee, HrMetric, HrMetricValue
from app.routers.hr_metrics import _active_metrics, _week_label, _week_start
from app.services.telegram_send import parse_chat_ids

logger = logging.getLogger(__name__)

REMIND = "remind"   # пятница 12:00 — руководителям
CHECK = "check"     # пятница 17:30 — HR, кто не сдал

# Сколько фамилий перечислять в строке «не заполнены» — дальше «и ещё N»
_MAX_NAMES = 5


# ── Кто сколько сдал ─────────────────────────────────────────────────────────

def _stat(employee_ids: list[int], by_employee: dict[int, list[HrMetric]],
          employees: dict[int, HrEmployee], filled_ids: set[int]) -> dict:
    """Прогресс по набору сотрудников: сколько метрик из скольких заполнено и по
    кому именно данных не хватает."""
    total = filled = 0
    pending: list[str] = []
    for eid in employee_ids:
        metrics = by_employee.get(eid) or []
        if not metrics:
            continue
        done = sum(1 for m in metrics if m.id in filled_ids)
        total += len(metrics)
        filled += done
        if done < len(metrics):
            pending.append(employees[eid].full_name)
    return {"total": total, "filled": filled, "pending": sorted(pending)}


def manager_progress(db: Session, ws: date) -> dict:
    """Срез недели по подразделениям: за что отвечает каждый руководитель и что
    из этого уже внесено.

    Охват руководителя тот же, что у его ссылки (_scope_employee_ids): свои люди
    плюс он сам — иначе напоминание считало бы одно, а форма показывала другое.
    Сотрудники, чей руководитель не указан или уволен, попадают в отдельную
    группу: напомнить о них некому, но и потеряться они не должны."""
    metrics = _active_metrics(db)
    empty = {"week": ws, "managers": [], "orphan": None, "total": 0, "filled": 0}
    if not metrics:
        return empty

    by_employee: dict[int, list[HrMetric]] = defaultdict(list)
    employees: dict[int, HrEmployee] = {}
    for m in metrics:
        by_employee[m.employee_id].append(m)
        employees[m.employee_id] = m.employee

    filled_ids = {
        row.metric_id for row in
        db.query(HrMetricValue.metric_id)
          .filter(HrMetricValue.metric_id.in_([m.id for m in metrics]),
                  HrMetricValue.week_start == ws,
                  HrMetricValue.value.isnot(None)).all()
    }

    # Руководители — те, на кого ссылаются сотрудники с метриками. Сам
    # руководитель метрику иметь не обязан, поэтому берём их отдельным запросом.
    manager_ids: list[int] = []
    for emp in employees.values():
        if emp.manager_id and emp.manager_id not in manager_ids:
            manager_ids.append(emp.manager_id)
    managers = {e.id: e for e in db.query(HrEmployee)
                .filter(HrEmployee.id.in_(manager_ids or [-1])).all()}

    rows, covered = [], set()
    for mid in manager_ids:
        mgr = managers.get(mid)
        if not mgr or not mgr.is_active:
            continue   # уволенному напоминать нечего — его люди уйдут в orphan
        scope = [eid for eid, emp in employees.items()
                 if emp.manager_id == mid or eid == mid]
        st = _stat(scope, by_employee, employees, filled_ids)
        if not st["total"]:
            continue
        covered.update(scope)
        rows.append({
            "manager": mgr,
            "name": mgr.full_name,
            "done": st["filled"] >= st["total"],
            **st,
        })
    # сначала должники, внутри — по алфавиту
    rows.sort(key=lambda r: (r["done"], r["name"]))

    orphan_ids = [eid for eid in employees if eid not in covered]
    orphan = _stat(orphan_ids, by_employee, employees, filled_ids) if orphan_ids else None

    return {
        "week": ws,
        "managers": rows,
        "orphan": orphan if orphan and orphan["total"] else None,
        "total": len(metrics),
        "filled": len(filled_ids),
    }


# ── Адрес доски ──────────────────────────────────────────────────────────────

def _base_url(db: Session) -> str:
    """Публичный адрес TMS из настроек. Фоновой задаче взять его больше неоткуда
    (объекта Request нет), поэтому без него шлём сообщение просто без ссылки.

    Личные ссылки руководителей (/hr/w/{token}) в этих сообщениях сознательно
    не приводятся: они открывают форму подразделения без входа в TMS, а чат
    уведомлений общий. Ссылку руководитель получает лично — её выдаёт HR с
    доски метрик."""
    company = db.query(CompanySettings).first()
    return ((company.public_url or "").strip().rstrip("/")) if company else ""


# ── Тексты сообщений ─────────────────────────────────────────────────────────

def _names(pending: list[str]) -> str:
    if not pending:
        return ""
    shown = ", ".join(pending[:_MAX_NAMES])
    extra = len(pending) - _MAX_NAMES
    return f"{shown} и ещё {extra}" if extra > 0 else shown


def build_remind(db: Session, ws: date | None = None) -> str | None:
    """Пятничное напоминание руководителям. None — метрик нет, напоминать не о чем."""
    ws = ws or _week_start(date.today())
    data = manager_progress(db, ws)
    if not data["managers"] and not data["orphan"]:
        return None

    waiting = [r for r in data["managers"] if not r["done"]]
    ready = [r for r in data["managers"] if r["done"]]

    lines = [f"⏰ Метрика за неделю {_week_label(ws)}", ""]
    if waiting:
        lines.append("Руководители, внесите метрики по своим сотрудникам за эту неделю.")
        lines.append("")
        for r in waiting:
            left = r["total"] - r["filled"]
            lines.append(f"• {r['name']} — осталось {left} из {r['total']}")
    else:
        lines.append("✅ Все руководители уже внесли метрики за эту неделю — спасибо.")
    if ready and waiting:
        lines += ["", "✅ Уже сдали: " + ", ".join(r["name"] for r in ready)]

    # Ссылки на доску здесь нет: чат напоминания общий, а доска метрик — раздел
    # HR. Адрес доски идёт только в вечернюю сводку HR (build_check).
    lines += ["", f"Итого за неделю: {data['filled']} из {data['total']} показателей"]
    return "\n".join(lines)


def build_check(db: Session, ws: date | None = None) -> str | None:
    """Вечерняя сводка для HR: кто из руководителей ещё не сдал метрику."""
    ws = ws or _week_start(date.today())
    data = manager_progress(db, ws)
    if not data["managers"] and not data["orphan"]:
        return None

    base = _base_url(db)
    waiting = [r for r in data["managers"] if not r["done"]]
    ready = [r for r in data["managers"] if r["done"]]
    lines = [f"📋 Метрика за неделю {_week_label(ws)} — кто не сдал", ""]

    if waiting:
        lines.append(f"Не сдали: {len(waiting)} из {len(data['managers'])} руководителей")
        for r in waiting:
            names = _names(r["pending"])
            tail = f" · нет данных: {names}" if names else ""
            lines.append(f"▫️ {r['name']} — {r['filled']} из {r['total']}{tail}")
    else:
        lines.append("✅ Все руководители сдали метрику за неделю")

    if ready and waiting:
        lines += ["", "✅ Сдали: " + ", ".join(r["name"] for r in ready)]

    if data["orphan"]:
        o = data["orphan"]
        names = _names(o["pending"])
        tail = f" (нет данных: {names})" if names else ""
        lines += ["", f"⚠️ Топ менеджмент: {o['filled']} из {o['total']}{tail}"]

    lines += ["", f"Итого за неделю: {data['filled']} из {data['total']} показателей"]
    if base:
        lines.append(f"Доска метрик: {base}/hr/metrics")
    return "\n".join(lines)


# ── Настройки рассылки ───────────────────────────────────────────────────────

def notification_settings(db: Session, kind: str) -> tuple[bool, list[int]]:
    """(включено, chat_id) для уведомления kind. Чат не задан — берём чат
    HR-отчёта, затем общий чат отчётов: настройка нужна, только если уведомление
    должно уходить отдельно от них."""
    company = db.query(CompanySettings).first()
    if not company:
        return False, []
    if kind == REMIND:
        enabled, raw = company.hr_metric_remind_enabled, company.hr_metric_remind_chat_ids
    else:
        enabled, raw = company.hr_metric_check_enabled, company.hr_metric_check_chat_ids
    ids = (parse_chat_ids(raw or "")
           or parse_chat_ids(company.tg_hr_report_chat_ids or "")
           or parse_chat_ids(company.tg_report_chat_ids or ""))
    return bool(enabled), ids


def compose(kind: str, ws: date | None = None, force: bool = False) -> tuple[list[int], str | None]:
    """Готовое к отправке: (кому, текст). Открывает свою сессию — вызывается из
    бота, где сессии живут только на время задачи. force=1 игнорирует флаг
    включения (ручной вызов командой бота)."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        enabled, ids = notification_settings(db, kind)
        if not enabled and not force:
            return [], None
        text = build_remind(db, ws) if kind == REMIND else build_check(db, ws)
        return ids, text
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

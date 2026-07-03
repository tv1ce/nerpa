"""
Сборка HR-отчёта из данных, синхронизированных в таблицу hr_entries (см. hr_sync.py).

Отчёт — это ДЕЛЬТА: только записи, попавшие в hr_entries с момента последней
отправки отчёта того же вида (полного или по метрикам). Момент последней
отправки хранится в HR_STATE_FILE, отдельно для каждого из двух циклов:
    generate_metrics_report(db) — только «Метрики», раз в 2 недели
    generate_full_report(db)    — всё остальное, раз в месяц:
        1. Личностный файл — минус «дичь», подмечаем реальные проблемы
        2. Достижения      — новые записи без изменений
        3. eNPS (сотрудники + руководители) — средний балл по новым ответам,
           подмечаем проблемные места
        4. Сроки закрытия вакансий — новое/изменившееся целиком

Раздел «Гравитация и антигравитация» синхронизируется в БД, но в отчёт
намеренно не включается (не входит в согласованное ТЗ).

Итоговый текст готовится для Telegram (MarkdownV2).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import HrEntry
from app.services import openrouter_client

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).resolve().parents[2] / "hr_report_state.json"
_EPOCH = datetime(1970, 1, 1)


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def _last_sent_at(state: dict, key: str) -> datetime:
    raw = state.get(key)
    return datetime.fromisoformat(raw) if raw else _EPOCH


# ── Форматирование ─────────────────────────────────────────────────────────────

def _esc(text) -> str:
    """Экранирует спецсимволы MarkdownV2 (аналог bot/formatters._esc)."""
    if text is None:
        return ""
    s = str(text)
    for ch in r"\_*[]()~`>#+-=|{}.!":
        s = s.replace(ch, "\\" + ch)
    return s


def _entries_since(db: Session, section: str, since: datetime) -> list[HrEntry]:
    return (
        db.query(HrEntry)
        .filter(HrEntry.section == section, HrEntry.imported_at > since)
        .order_by(HrEntry.subject_name, HrEntry.imported_at)
        .all()
    )


def _group_by_subject(rows: list[HrEntry]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for row in rows:
        groups.setdefault(row.subject_name, []).append(row.raw_text)
    return groups


def _blob(rows: list[HrEntry]) -> str:
    """Собирает новые записи в текст, сгруппированный по сотруднику — вход для LLM."""
    parts = []
    for subject, texts in _group_by_subject(rows).items():
        parts.append(subject)
        parts.append("")
        parts.extend(t + "\n" for t in texts)
    return "\n".join(parts).strip()


def _format_list(rows: list[HrEntry]) -> str:
    """Достижения/вакансии: пункты уже содержат собственное форматирование автора
    (тире, нумерацию) — свой маркер не добавляем, выводим как есть."""
    if not rows:
        return "Новых записей нет."
    parts = []
    for subject, texts in _group_by_subject(rows).items():
        parts.append(subject)
        parts.extend(texts)
        parts.append("")
    return "\n".join(parts).strip()


# ── LLM-анализ (только новых данных за период) ────────────────────────────────

def _strip_llm_preamble(text: str) -> str:
    """LLM иногда добавляет свой заголовок в начале и/или дисклеймер после '---' в конце вопреки
    инструкции — срезаем и то, и другое, оставляя только содержательные абзацы по сотрудникам."""
    lines = text.strip().split("\n")
    while lines and lines[0].strip().startswith("#"):
        lines.pop(0)
        while lines and lines[0].strip() == "":
            lines.pop(0)
    if "---" in lines:
        lines = lines[: lines.index("---")]
    return "\n".join(lines).strip()


PERSONAL_SYSTEM_PROMPT = """\
Ты — ассистент HR-отдела компании. Тебе дан текст НОВЫХ (за последний период) ответов из \
двух связанных разделов внутреннего отчёта: «Личностный профиль сотрудника» (результаты, \
ценность работы, сложности) и вопрос «С какой дичью вам приходится сталкиваться каждый день» \
(ежедневные раздражители).

Для каждого сотрудника, у которого есть новые ответы:
- убери откровенную «дичь» — эмоциональные жалобы без сути, бытовой шум, несущественные придирки;
- оставь и явно выдели только содержательные проблемные места — то, что реально мешает работать;
- если у сотрудника нет содержательных проблем — напиши по нему одну короткую строку \
  вида «Имя Фамилия — без существенных проблем»;
- если проблема есть — опиши её под именем сотрудника в 1-2 предложениях, по делу.

Ничего не выдумывай, используй только то, что есть в тексте. Ответь готовым текстом отчёта \
на русском языке, по одному абзацу на сотрудника. Не добавляй никаких заголовков, вступлений, \
итогов, примечаний или разделителей "---" — только сами абзацы по сотрудникам."""

ENPS_SYSTEM_PROMPT = """\
Тебе дан текст НОВЫХ (за последний период) ответов на опрос eNPS (шкала 0-10 — вероятность \
порекомендовать компанию/руководителя как место работы). Для каждого сотрудника извлеки оценку \
и комментарий.

Верни СТРОГО JSON-массив без каких-либо пояснений и markdown-обёртки, формата:
[{"employee": "Имя Фамилия - должность", "score": <int 0-10 или null если ответа нет>, \
"comment": "<краткий комментарий или пустая строка>", "concern": <true/false — есть ли в ответе тревожный сигнал>}]"""


def _analyze_personal(personal_rows: list[HrEntry], complaints_rows: list[HrEntry]) -> str:
    combined = f"{_blob(personal_rows)}\n\n{_blob(complaints_rows)}".strip()
    if not combined:
        return "Новых ответов за период нет."
    return _strip_llm_preamble(openrouter_client.chat(PERSONAL_SYSTEM_PROMPT, combined))


def _analyze_enps(rows: list[HrEntry]) -> tuple[float | None, list[dict]]:
    """Возвращает (средний балл по новым ответам, список [{employee, score, comment, concern}])."""
    raw = _blob(rows)
    if not raw.strip():
        return None, []
    try:
        items = openrouter_client.chat_json(ENPS_SYSTEM_PROMPT, raw)
    except Exception:
        logger.exception("Не удалось разобрать JSON ответа eNPS")
        return None, []
    scores = [it["score"] for it in items if isinstance(it.get("score"), (int, float))]
    avg = round(sum(scores) / len(scores), 1) if scores else None
    return avg, items


def _enps_block(title: str, avg: float | None, items: list[dict]) -> str:
    lines = [f"*{_esc(title)}*"]
    lines.append(f"Средний балл за период: {_esc(avg) if avg is not None else 'новых ответов нет'}")
    concerns = [it for it in items if it.get("concern") or (isinstance(it.get("score"), (int, float)) and it["score"] <= 6)]
    if concerns:
        lines.append("Проблемные места:")
        for it in concerns:
            comment = f" — {it['comment']}" if it.get("comment") else ""
            score = it.get("score")
            score_str = str(score) if score is not None else "нет ответа"
            lines.append(f"  \\- {_esc(it.get('employee'))}: {_esc(score_str)}{_esc(comment)}")
    elif items:
        lines.append("Проблемных мест не выявлено ✅")
    return "\n".join(lines)


# ── Точки входа ────────────────────────────────────────────────────────────────

def generate_metrics_report(db: Session) -> str | None:
    """Раз в 2 недели: только раздел «Метрики». Возвращает None, если ничего не изменилось."""
    state = _load_state()
    since = _last_sent_at(state, "metrics_sent_at")
    rows = _entries_since(db, "metrics", since)
    if not rows:
        return None
    state["metrics_sent_at"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)
    return f"📈 *Метрики* — новые данные\n\n{_esc(_format_list(rows))}"


def generate_full_report(db: Session) -> str:
    """Раз в месяц: личностный файл, достижения, eNPS (х2), сроки закрытия вакансий."""
    state = _load_state()
    since = _last_sent_at(state, "full_sent_at")

    personal_rows = _entries_since(db, "personal", since)
    complaints_rows = _entries_since(db, "complaints", since)
    achievements_rows = _entries_since(db, "achievements", since)
    enps_rows = _entries_since(db, "enps", since)
    enps_mgr_rows = _entries_since(db, "enps_managers", since)
    vacancies_rows = _entries_since(db, "vacancies", since)

    state["full_sent_at"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)

    personal_analysis = _analyze_personal(personal_rows, complaints_rows)
    achievements_text = _format_list(achievements_rows)
    enps_avg, enps_items = _analyze_enps(enps_rows)
    enps_mgr_avg, enps_mgr_items = _analyze_enps(enps_mgr_rows)
    vacancies_text = _format_list(vacancies_rows)

    parts = [
        "📋 *Отчёт HR* (новое за период)",
        "",
        "👨 *Личностный файл*",
        _esc(personal_analysis),
        "",
        "🏅 *Достижения*",
        _esc(achievements_text),
        "",
        _enps_block("📣 eNPS (сотрудники)", enps_avg, enps_items),
        "",
        _enps_block("📣 eNPS (руководители)", enps_mgr_avg, enps_mgr_items),
        "",
        "📆 *Сроки закрытия вакансий*",
        _esc(vacancies_text),
    ]
    return "\n".join(parts)

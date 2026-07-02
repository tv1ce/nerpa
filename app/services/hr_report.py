"""
Сборка HR-отчёта из статьи Teamly «Отчётность HR».

Статья со временем растёт: HR добавляет новые записи, старые не стирает
(новые датированные ответы в eNPS/личностном профиле, новые пункты в
достижениях, новые правки в метриках/вакансиях). Поэтому отчёт всегда
строится как ДЕЛЬТА — сравнение текущего содержимого с снимком, сохранённым
после предыдущего запуска (см. HR_STATE_FILE), а не весь документ целиком.

Два независимых цикла с разной периодичностью:
    generate_metrics_report() — только «Метрики», раз в 2 недели
    generate_full_report()    — всё остальное, раз в месяц:
        1. Личностный файл — минус «дичь», подмечаем реальные проблемы
        2. Достижения      — новые пункты без изменений
        3. eNPS (сотрудники + руководители) — средний балл по новым ответам,
           подмечаем проблемные места
        4. Сроки закрытия вакансий — новое/изменившееся целиком

Раздел «Гравитация и антигравитация» в статье есть, но в отчёт намеренно
не включается (не входит в согласованное ТЗ).

Итоговый текст готовится для Telegram (MarkdownV2).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from app.services import openrouter_client, prosemirror, teamly_client

logger = logging.getLogger(__name__)

HR_REPORT_ARTICLE_ID = os.getenv("TEAMLY_HR_REPORT_ARTICLE_ID", "")
STATE_FILE = Path(__file__).resolve().parents[2] / "hr_report_state.json"


# ── Состояние (снимок предыдущего запуска) ────────────────────────────────────

def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def _diff_new(current: dict[str, str], previous: dict[str, str]) -> dict[str, str]:
    """Новые или изменившиеся записи: ключ отсутствовал раньше или текст стал другим."""
    return {k: v for k, v in current.items() if previous.get(k) != v}


# ── Разбор дерева статьи ──────────────────────────────────────────────────────

def _esc(text) -> str:
    """Экранирует спецсимволы MarkdownV2 (аналог bot/formatters._esc)."""
    if text is None:
        return ""
    s = str(text)
    for ch in r"\_*[]()~`>#+-=|{}.!":
        s = s.replace(ch, "\\" + ch)
    return s


def _find_section(sections: dict[str, dict], *keywords: str) -> dict | None:
    """Ищет раздел, в заголовке которого встречаются все keywords (без учёта регистра)."""
    for title, node in sections.items():
        low = title.lower()
        if all(kw.lower() in low for kw in keywords):
            return node
    return None


def _render(node: dict | None) -> str:
    """Рендерит раздел БЕЗ его собственного заголовка (заголовок мы выводим сами)."""
    if node is None:
        return ""
    body = next((c for c in node.get("content", []) if c.get("type") == "expandContent"), None)
    if body is None:
        return ""
    return prosemirror.render_to_text({"type": "doc", "content": [body]})


def _children(section_node: dict | None) -> dict[str, dict]:
    """Сотрудники (или вакансии) верхнего уровня внутри раздела — вложенные expandSection."""
    if section_node is None:
        return {}
    body = next((c for c in section_node.get("content", []) if c.get("type") == "expandContent"), None)
    if body is None:
        return {}
    return prosemirror.top_level_sections(body)


def _text_of(title_node: dict) -> str:
    return "".join(
        c.get("text", "") for c in (title_node.get("content") or []) if c.get("type") == "text"
    ).strip()


def _has_nested_expand_section(node: dict) -> bool:
    """Есть ли внутри узла (на любой глубине) ещё один expandSection — признак «обёртки»,
    а не самостоятельной записи (например, «Вопросы и ответы» оборачивает даты-карточки)."""
    for child in node.get("content") or []:
        if child.get("type") == "expandSection" or _has_nested_expand_section(child):
            return True
    return False


def _collect_entries(node: dict) -> list[tuple[str, str]]:
    """Универсальный сборщик записей внутри блока одного сотрудника/вакансии:
    - вложенный expandSection без дальнейшей вложенности (дата, месяц) — одна запись целиком;
    - «голый» list_item вне такой секции (старые статьи без периодов) — тоже одна запись;
    - expandSection-«обёртка» (внутри ещё есть expandSection) — не запись, спускаемся глубже.
    Так учитываются оба варианта, реально встречающиеся в статье."""
    entries: list[tuple[str, str]] = []
    for child in node.get("content") or []:
        t = child.get("type")
        if t == "expandSection":
            if _has_nested_expand_section(child):
                entries.extend(_collect_entries(child))
                continue
            titles = [c for c in (child.get("content") or []) if c.get("type") == "expandTitle"]
            title_text = _text_of(titles[0]) if titles else ""
            text = _render(child).strip()
            if text:
                entries.append((title_text or text[:40], text))
        elif t == "list_item":
            text = prosemirror.render_to_text({"type": "doc", "content": child.get("content", [])}).strip()
            if text:
                entries.append((text, text))
        else:
            entries.extend(_collect_entries(child))
    return entries


# ── Сбор текущего состояния секции в плоский словарь key -> text ──────────────

def _collect_dated(section_node: dict | None) -> dict[str, str]:
    """Личностный/дичь/eNPS/достижения: по каждому сотруднику — по одной записи на период/пункт."""
    result: dict[str, str] = {}
    for employee, emp_node in _children(section_node).items():
        for key, text in _collect_entries(emp_node):
            result[f"{employee}::{key}"] = text
    return result


def _collect_block(section_node: dict | None) -> dict[str, str]:
    """Метрики/вакансии: по каждому сотруднику/вакансии — весь блок целиком (нет дат внутри)."""
    result: dict[str, str] = {}
    for name, node in _children(section_node).items():
        text = _render(node).strip()
        if text:
            result[name] = text
    return result


def _group_by_employee(d: dict[str, str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for key, text in d.items():
        employee = key.split("::", 1)[0]
        groups.setdefault(employee, []).append(text)
    return groups


def _blob_from_new(d: dict[str, str]) -> str:
    """Собирает новые записи в текст, сгруппированный по сотруднику — вход для LLM."""
    parts = []
    for employee, texts in _group_by_employee(d).items():
        parts.append(employee)
        parts.append("")
        parts.extend(t + "\n" for t in texts)
    return "\n".join(parts).strip()


def _format_new_list(d: dict[str, str]) -> str:
    """Пункты уже содержат собственное форматирование автора (тире, нумерацию) —
    свой маркер не добавляем, выводим как есть."""
    if not d:
        return "Новых записей нет."
    parts = []
    for employee, texts in _group_by_employee(d).items():
        parts.append(employee)
        parts.extend(texts)
        parts.append("")
    return "\n".join(parts).strip()


def _format_new_blocks(d: dict[str, str]) -> str:
    if not d:
        return "Изменений нет."
    parts = []
    for name, text in d.items():
        parts.append(name)
        parts.append(text)
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


def _analyze_personal(new_personal: dict[str, str], new_complaints: dict[str, str]) -> str:
    combined = f"{_blob_from_new(new_personal)}\n\n{_blob_from_new(new_complaints)}".strip()
    if not combined:
        return "Новых ответов за период нет."
    return _strip_llm_preamble(openrouter_client.chat(PERSONAL_SYSTEM_PROMPT, combined))


def _analyze_enps(new_entries: dict[str, str]) -> tuple[float | None, list[dict]]:
    """Возвращает (средний балл по новым ответам, список [{employee, score, comment, concern}])."""
    raw = _blob_from_new(new_entries)
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

def _load_sections() -> tuple[dict, dict[str, dict]]:
    if not HR_REPORT_ARTICLE_ID:
        raise RuntimeError("TEAMLY_HR_REPORT_ARTICLE_ID не задан в .env")
    article = teamly_client.get_article(HR_REPORT_ARTICLE_ID)
    doc = prosemirror.parse_content(article["editorContentObject"]["content"])
    return article, prosemirror.top_level_sections(doc)


def generate_metrics_report() -> str | None:
    """Раз в 2 недели: только раздел «Метрики». Возвращает None, если ничего не изменилось."""
    _, sections = _load_sections()
    metrics_node = _find_section(sections, "метрик")

    state = _load_state()
    current = _collect_block(metrics_node)
    new_items = _diff_new(current, state.get("metrics", {}))
    state["metrics"] = current
    _save_state(state)

    if not new_items:
        return None
    return f"📈 *Метрики* — новые данные\n\n{_esc(_format_new_blocks(new_items))}"


def generate_full_report() -> str:
    """Раз в месяц: личностный файл, достижения, eNPS (х2), сроки закрытия вакансий."""
    article, sections = _load_sections()

    personal_node = _find_section(sections, "личностн")
    complaints_node = _find_section(sections, "дичь")
    achievements_node = _find_section(sections, "достижени")
    enps_employees_node = None
    enps_managers_node = None
    for title, node in sections.items():
        low = title.lower()
        if "enps" in low and "руководител" in low:
            enps_managers_node = node
        elif "enps" in low:
            enps_employees_node = node
    vacancies_node = _find_section(sections, "вакансий")

    current = {
        "personal": _collect_dated(personal_node),
        "complaints": _collect_dated(complaints_node),
        "achievements": _collect_dated(achievements_node),
        "enps": _collect_dated(enps_employees_node),
        "enps_managers": _collect_dated(enps_managers_node),
        "vacancies": _collect_block(vacancies_node),
    }

    state = _load_state()
    full_state = state.get("full", {})
    new = {key: _diff_new(current[key], full_state.get(key, {})) for key in current}

    state["full"] = current
    _save_state(state)

    personal_analysis = _analyze_personal(new["personal"], new["complaints"])
    achievements_text = _format_new_list(new["achievements"])
    enps_avg, enps_items = _analyze_enps(new["enps"])
    enps_mgr_avg, enps_mgr_items = _analyze_enps(new["enps_managers"])
    vacancies_text = _format_new_blocks(new["vacancies"])

    parts = [
        f"📋 *Отчёт HR* — {_esc(article.get('title', ''))} (новое за период)",
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

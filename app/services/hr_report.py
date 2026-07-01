"""
Сборка HR-отчёта из статьи Teamly «Отчётность HR»:
    1. Личностный файл       — минус «дичь» (шум/жалобы без сути), подмечаем реальные проблемы
    2. Достижения            — без изменений, целиком
    3. eNPS (сотрудники + руководители) — средний балл по каждому, подмечаем проблемные места
    4. Метрики                — без изменений, целиком
    5. Сроки закрытия вакансий — целиком

Раздел «Гравитация и антигравитация» в статье есть, но в отчёт намеренно не включается
(не входит в согласованное ТЗ).

Итоговый текст готовится для Telegram (MarkdownV2).
"""
from __future__ import annotations

import logging
import os

from app.services import openrouter_client, prosemirror, teamly_client

logger = logging.getLogger(__name__)

HR_REPORT_ARTICLE_ID = os.getenv("TEAMLY_HR_REPORT_ARTICLE_ID", "")


def _esc(text) -> str:
    """Экранирует спецсимволы MarkdownV2 (аналог bot/formatters._esc)."""
    if text is None:
        return ""
    s = str(text)
    for ch in r"\_*[]()~`>#+-=|{}.!":
        s = s.replace(ch, "\\" + ch)
    return s


def _find_section(sections: dict[str, dict], *keywords: str) -> dict | None:
    """Ищет раздел верхнего уровня, в заголовке которого встречаются все keywords (без учёта регистра)."""
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
Ты — ассистент HR-отдела компании. Тебе дан текст двух связанных разделов внутреннего \
отчёта: «Личностный профиль сотрудника» (ответы про результаты, ценность работы, сложности) \
и вопрос «С какой дичью вам приходится сталкиваться каждый день» (ежедневные раздражители).

Для каждого сотрудника:
- убери откровенную «дичь» — эмоциональные жалобы без сути, бытовой шум, несущественные придирки;
- оставь и явно выдели только содержательные проблемные места — то, что реально мешает работать;
- если у сотрудника нет содержательных проблем — напиши по нему одну короткую строку \
  вида «Имя Фамилия — без существенных проблем»;
- если проблема есть — опиши её под именем сотрудника в 1-2 предложениях, по делу.

Ничего не выдумывай, используй только то, что есть в тексте. Ответь готовым текстом отчёта \
на русском языке, по одному абзацу на сотрудника. Не добавляй никаких заголовков, вступлений, \
итогов, примечаний или разделителей "---" — только сами абзацы по сотрудникам."""

ENPS_SYSTEM_PROMPT = """\
Тебе дан текст ответов на опрос eNPS (шкала 0-10 — вероятность порекомендовать компанию/руководителя \
как место работы). Для каждого сотрудника извлеки оценку и комментарий.

Верни СТРОГО JSON-массив без каких-либо пояснений и markdown-обёртки, формата:
[{"employee": "Имя Фамилия - должность", "score": <int 0-10 или null если ответа нет>, \
"comment": "<краткий комментарий или пустая строка>", "concern": <true/false — есть ли в ответе тревожный сигнал>}]

Если в тексте оценки нет (стоит "Ответы" без данных) — score: null, comment: "", concern: false."""


def _analyze_personal(raw_personal: str, raw_complaints: str) -> str:
    combined = f"{raw_personal}\n\n{raw_complaints}".strip()
    if not combined:
        return "Нет данных."
    return _strip_llm_preamble(openrouter_client.chat(PERSONAL_SYSTEM_PROMPT, combined))


def _analyze_enps(raw: str) -> tuple[float | None, list[dict]]:
    """Возвращает (средний балл, список записей [{employee, score, comment, concern}])."""
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
    lines.append(f"Средний балл: {_esc(avg) if avg is not None else 'нет ответов'}")
    concerns = [it for it in items if it.get("concern") or (isinstance(it.get("score"), (int, float)) and it["score"] <= 6)]
    if concerns:
        lines.append("Проблемные места:")
        for it in concerns:
            comment = f" — {it['comment']}" if it.get("comment") else ""
            score = it.get("score")
            score_str = str(score) if score is not None else "нет ответа"
            lines.append(f"  \\- {_esc(it.get('employee'))}: {_esc(score_str)}{_esc(comment)}")
    else:
        lines.append("Проблемных мест не выявлено ✅")
    return "\n".join(lines)


def generate_report() -> str:
    """Формирует полный текст HR-отчёта (MarkdownV2, для Telegram)."""
    if not HR_REPORT_ARTICLE_ID:
        raise RuntimeError("TEAMLY_HR_REPORT_ARTICLE_ID не задан в .env")

    article = teamly_client.get_article(HR_REPORT_ARTICLE_ID)
    content = article["editorContentObject"]["content"]
    doc = prosemirror.parse_content(content)
    sections = prosemirror.top_level_sections(doc)

    personal_raw = _render(_find_section(sections, "личностн"))
    complaints_raw = _render(_find_section(sections, "дичь"))
    achievements_raw = _render(_find_section(sections, "достижени"))
    # eNPS сотрудников — раздел с "enps", но БЕЗ слова "руководител"
    enps_employees_node = None
    enps_managers_node = None
    for title, node in sections.items():
        low = title.lower()
        if "enps" in low and "руководител" in low:
            enps_managers_node = node
        elif "enps" in low:
            enps_employees_node = node
    metrics_raw = _render(_find_section(sections, "метрик"))
    vacancies_raw = _render(_find_section(sections, "вакансий"))

    personal_analysis = _analyze_personal(personal_raw, complaints_raw)
    achievements_text = achievements_raw or "Нет данных."
    enps_avg, enps_items = _analyze_enps(_render(enps_employees_node))
    enps_mgr_avg, enps_mgr_items = _analyze_enps(_render(enps_managers_node))
    metrics_text = metrics_raw or "Нет данных."
    vacancies_text = vacancies_raw or "Нет данных."

    parts = [
        f"📋 *Отчёт HR* — {_esc(article.get('title', ''))}",
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
        "📈 *Метрики*",
        _esc(metrics_text),
        "",
        "📆 *Сроки закрытия вакансий*",
        _esc(vacancies_text),
    ]
    return "\n".join(parts)

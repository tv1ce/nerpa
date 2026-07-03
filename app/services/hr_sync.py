"""
Синхронизация статьи Teamly «Отчётность HR» в таблицу hr_entries.

Статья в Teamly растёт со временем — HR добавляет новые записи, старые не
удаляет (месяц-колонки в достижениях, дата-карточки в eNPS/личностном
профиле; метрики/вакансии — просто изменяющийся блок текста без периодов).
Импорт идемпотентен: у каждой записи есть устойчивый source_key, повторный
запуск (в т.ч. с самого начала — полный бэкфилл истории) добавляет только то,
чего ещё нет в БД.
"""
from __future__ import annotations

import hashlib
import logging
import os

from sqlalchemy.orm import Session

from app.models import HrEntry
from app.services import prosemirror, teamly_client

logger = logging.getLogger(__name__)

HR_REPORT_ARTICLE_ID = os.getenv("TEAMLY_HR_REPORT_ARTICLE_ID", "")

# Разделы статьи, которые импортируем, и их тип сбора записей:
#   "entries" — период/дата/пункт список (личностный, дичь, достижения, eNPS)
#   "block"   — весь блок сотрудника/вакансии целиком, версионируется по хэшу содержимого
SECTION_SPECS: list[tuple[str, tuple[str, ...], str]] = [
    ("personal",      ("личностн",),           "entries"),
    ("complaints",    ("дичь",),                "entries"),
    ("achievements",  ("достижени",),           "entries"),
    ("enps",          ("enps",),                "entries_enps"),
    ("enps_managers", ("enps", "руководител"),  "entries_enps_managers"),
    ("metrics",       ("метрик",),              "block"),
    ("vacancies",     ("вакансий",),            "block"),
]


def _find_section(sections: dict[str, dict], *keywords: str) -> dict | None:
    for title, node in sections.items():
        low = title.lower()
        if all(kw.lower() in low for kw in keywords):
            return node
    return None


def _render(node: dict | None) -> str:
    """Рендерит раздел БЕЗ его собственного заголовка."""
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
    """Есть ли внутри узла (на любой глубине) ещё один expandSection — признак «обёртки»
    (например, «Вопросы и ответы» оборачивает дата-карточки), а не самостоятельной записи."""
    for child in node.get("content") or []:
        if child.get("type") == "expandSection" or _has_nested_expand_section(child):
            return True
    return False


def _collect_entries(node: dict) -> list[tuple[str, str]]:
    """Универсальный сборщик записей внутри блока одного сотрудника/вакансии:
    - вложенный expandSection без дальнейшей вложенности (дата, месяц) — одна запись целиком;
    - «голый» list_item вне такой секции (старые статьи без периодов) — тоже одна запись;
    - expandSection-«обёртка» (внутри ещё есть expandSection) — не запись, спускаемся глубже."""
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


def _collect_section_entries(section_node: dict | None) -> list[tuple[str, str, str]]:
    """Возвращает [(subject_name, period_label, text)] для разделов с записями по периодам/пунктам."""
    result = []
    for subject, emp_node in _children(section_node).items():
        for period_label, text in _collect_entries(emp_node):
            result.append((subject, period_label, text))
    return result


def _collect_section_block(section_node: dict | None) -> list[tuple[str, str, str]]:
    """Возвращает [(subject_name, version_hash, text)] для блочных разделов (метрики, вакансии) —
    целиком блок каждого сотрудника/вакансии; версия различается хэшем содержимого."""
    result = []
    for subject, node in _children(section_node).items():
        text = _render(node).strip()
        if text:
            version = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
            result.append((subject, version, text))
    return result


def sync_from_teamly(db: Session) -> dict:
    """Тянет текущее содержимое статьи из Teamly и до-заливает новые записи в hr_entries.
    Идемпотентно — можно звать многократно, в т.ч. для полного бэкфилла истории.
    Возвращает {"section": {"new": N, "total": N}, ...}."""
    if not HR_REPORT_ARTICLE_ID:
        raise RuntimeError("TEAMLY_HR_REPORT_ARTICLE_ID не задан в .env")

    article = teamly_client.get_article(HR_REPORT_ARTICLE_ID)
    doc = prosemirror.parse_content(article["editorContentObject"]["content"])
    sections = prosemirror.top_level_sections(doc)

    existing_keys = {row[0] for row in db.query(HrEntry.source_key).all()}
    stats: dict[str, dict] = {}
    new_rows: list[HrEntry] = []

    for section, keywords, kind in SECTION_SPECS:
        if kind == "entries_enps":
            # eNPS сотрудников — раздел с "enps", но БЕЗ слова "руководител"
            node = next(
                (n for t, n in sections.items() if "enps" in t.lower() and "руководител" not in t.lower()),
                None,
            )
            triples = _collect_section_entries(node)
        elif kind == "entries_enps_managers":
            node = next(
                (n for t, n in sections.items() if "enps" in t.lower() and "руководител" in t.lower()),
                None,
            )
            triples = _collect_section_entries(node)
        elif kind == "block":
            node = _find_section(sections, *keywords)
            triples = _collect_section_block(node)
        else:
            node = _find_section(sections, *keywords)
            triples = _collect_section_entries(node)

        total = len(triples)
        added = 0
        for subject, period_label, text in triples:
            source_key = f"{section}::{subject}::{period_label}"
            if source_key in existing_keys:
                continue
            existing_keys.add(source_key)
            new_rows.append(HrEntry(
                section=section,
                subject_name=subject,
                period_label=period_label,
                raw_text=text,
                source_key=source_key,
            ))
            added += 1
        stats[section] = {"new": added, "total": total}

    if new_rows:
        db.add_all(new_rows)
        db.commit()

    logger.info("HR-синхронизация с Teamly: %s", stats)
    return stats

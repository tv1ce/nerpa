"""
Рендер контента статей Teamly (формат ProseMirror/Tiptap, editorContentObject.content)
в обычный текст — для дальнейшего анализа LLM и постинга в Telegram.
"""
from __future__ import annotations

import json
from typing import Optional


def parse_content(raw_content: str) -> dict:
    """editorContentObject.content приходит строкой с двойным JSON-кодированием."""
    return json.loads(raw_content)


def _render_text_node(node: dict) -> str:
    return node.get("text", "")


def _render_children(nodes: Optional[list], depth: int) -> str:
    return "".join(_render_node(n, depth) for n in (nodes or []))


def _render_node(node: dict, depth: int) -> str:
    t = node.get("type")
    content = node.get("content")

    if t == "text":
        return _render_text_node(node)
    if t == "hardBreak":
        return "\n"
    if t in ("doc", "expandContent"):
        return _render_children(content, depth)
    if t == "expandTitle":
        title = _render_children(content, depth).strip()
        return title + "\n\n"
    if t == "expandSection":
        return _render_children(content, depth + 1)
    if t == "paragraph":
        text = _render_children(content, depth).strip()
        return (text + "\n\n") if text else ""
    if t in ("bullet_list", "ordered_list"):
        return _render_children(content, depth) + "\n"
    if t == "list_item":
        text = _render_children(content, depth).strip()
        return f"- {text}\n"
    if t == "table":
        return _render_children(content, depth) + "\n"
    if t == "table_row":
        cells = [_render_children(c.get("content"), depth).strip() for c in (content or [])]
        return " | ".join(cells) + "\n"
    if t == "table_cell":
        return _render_children(content, depth)

    # неизвестный тип узла — просто спускаемся в детей, чтобы не терять текст
    return _render_children(content, depth)


def render_to_text(doc: dict) -> str:
    """Преобразует ProseMirror-документ статьи в читаемый текст с заголовками по секциям."""
    text = _render_node(doc, depth=0)
    # схлопываем повторяющиеся пустые строки
    lines = [ln.rstrip() for ln in text.splitlines()]
    out, blank = [], False
    for ln in lines:
        if ln == "":
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(ln)
    return "\n".join(out).strip()


def top_level_sections(doc: dict) -> dict[str, dict]:
    """Возвращает {заголовок верхнего раздела: узел expandSection} для документа статьи."""
    sections = {}
    for node in doc.get("content", []) or []:
        if node.get("type") != "expandSection":
            continue
        titles = [c for c in (node.get("content") or []) if c.get("type") == "expandTitle"]
        if not titles:
            continue
        title = "".join(
            t.get("text", "") for t in titles[0].get("content", []) if t.get("type") == "text"
        ).strip()
        sections[title] = node
    return sections

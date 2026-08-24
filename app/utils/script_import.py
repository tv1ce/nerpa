"""Импорт готовых скриптов из Word/текста в граф скрипта.

Скрипты отдела продаж живут в .docx: заголовки — шаги, под ними текст реплики,
ниже списком варианты ответов, иногда со стрелкой «куда». Разбираем именно эту
структуру, а не произвольный документ:

  Заголовок (Heading или отдельная жирная строка)  → новый шаг (Node)
  Обычные абзацы и списки                          → текст шага (rich-text)
  Строка вида «Да → Объём закупки» / «- Нет → 7»    → вариант ответа с переходом

Переход ищется по номеру шага и по названию (без учёта регистра). Если стрелки
нет, шаги связываются последовательно — как читается документ. Ничего не
теряем: непонятные строки попадают в текст шага, а не выбрасываются.

Форматирование (жирный, курсив, подчёркивание, списки, заголовки внутри текста)
переносится в HTML, чтобы после импорта скрипт выглядел как в исходном файле.
"""
import html as _html
import re

# Строка-ответ: «Да → Вопрос 5», «- Нет — 7», «• Если клиент не заинтересован → Завершение»
_ANSWER_RE = re.compile(
    r"^\s*(?:[-–—•*]|\d+[.)])?\s*(?P<text>.+?)\s*(?:→|->|=>|―>)\s*(?P<target>.+?)\s*$")
# Маркер начала блока ответов
_ANSWERS_HEADER = re.compile(
    r"^\s*(возможные\s+ответы|варианты\s+ответов|ответы|ответ\s+клиента)\s*:?\s*$", re.I)
# «Вопрос 7», «Шаг 7», «7»
_TARGET_NUM = re.compile(r"^(?:вопрос|шаг|блок)?\s*№?\s*(\d+)\.?", re.I)

_COLOR_HINTS = [
    (re.compile(r"^\s*(да|согласен|интересно|готов|хорошо)", re.I), "green"),
    (re.compile(r"^\s*(нет|отказ|не\s|дорого|некогда)", re.I), "red"),
    (re.compile(r"^\s*(частично|возможно|подумаю|не\s*знаю|позже)", re.I), "amber"),
]


def _answer_color(text: str) -> str:
    for rx, color in _COLOR_HINTS:
        if rx.match(text or ""):
            return color
    return "gray"


def _runs_to_html(paragraph) -> str:
    """Runs абзаца python-docx → HTML с сохранением жирного/курсива/подчёркивания."""
    out = []
    for run in paragraph.runs:
        text = _html.escape(run.text)
        if not text:
            continue
        if run.bold:
            text = f"<b>{text}</b>"
        if run.italic:
            text = f"<i>{text}</i>"
        if run.underline:
            text = f"<u>{text}</u>"
        if getattr(run.font, "strike", False):
            text = f"<s>{text}</s>"
        color = getattr(getattr(run.font, "color", None), "rgb", None)
        if color:
            text = f'<span style="color: #{color}">{text}</span>'
        out.append(text)
    return "".join(out) or _html.escape(paragraph.text)


def _is_heading(paragraph) -> bool:
    """Заголовок шага: стиль Heading или короткая строка целиком жирным."""
    style = (paragraph.style.name or "").lower()
    if style.startswith("heading") or style in ("title", "заголовок"):
        return True
    text = paragraph.text.strip()
    if not text or len(text) > 120:
        return False
    runs = [r for r in paragraph.runs if r.text.strip()]
    return bool(runs) and all(r.bold for r in runs)


def _blocks_from_docx(path: str):
    """Документ → список (kind, text, html), kind ∈ heading/list/para."""
    from docx import Document
    doc = Document(path)
    blocks = []
    for p in doc.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        style = (p.style.name or "").lower()
        if _is_heading(p):
            blocks.append(("heading", text, ""))
        elif "list" in style or text[:2] in ("- ", "• ", "* "):
            blocks.append(("list", text, _runs_to_html(p)))
        else:
            blocks.append(("para", text, _runs_to_html(p)))
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                blocks.append(("para", " | ".join(cells), _html.escape(" | ".join(cells))))
    return blocks


def _blocks_from_text(raw: str):
    """Текст/Markdown → те же блоки. Заголовок — строка с # или ЗАГЛАВНЫМИ."""
    blocks = []
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("#"):
            blocks.append(("heading", text.lstrip("# ").strip(), ""))
        elif text[:2] in ("- ", "• ", "* "):
            blocks.append(("list", text, _html.escape(text[2:].strip())))
        elif len(text) <= 120 and text == text.upper() and any(c.isalpha() for c in text):
            blocks.append(("heading", text, ""))
        else:
            blocks.append(("para", text, _html.escape(text)))
    return blocks


def parse_script(blocks) -> dict:
    """Блоки документа → граф в формате apply_graph (id отрицательные)."""
    nodes = []
    current = None
    in_answers = False

    def new_node(title):
        node = {
            "id": -(len(nodes) + 1), "title": title[:300], "body_html": "",
            "kind": "question", "x": 60 + (len(nodes) % 4) * 340,
            "y": 60 + (len(nodes) // 4) * 260, "fields": [], "answers": [],
            "_raw_answers": [],
        }
        nodes.append(node)
        return node

    for kind, text, inner in blocks:
        if kind == "heading":
            current = new_node(text)
            in_answers = False
            continue
        if current is None:
            current = new_node("Начало разговора")
        if _ANSWERS_HEADER.match(text):
            in_answers = True
            continue

        m = _ANSWER_RE.match(text)
        if m and (in_answers or kind == "list"):
            current["_raw_answers"].append((m.group("text").strip(), m.group("target").strip()))
            continue
        if in_answers and kind == "list":
            current["_raw_answers"].append((re.sub(r"^[-–—•*]\s*", "", text).strip(), ""))
            continue

        in_answers = False
        if kind == "list":
            if current["body_html"].endswith("</ul>"):
                current["body_html"] = current["body_html"][:-5] + f"<li>{inner}</li></ul>"
            else:
                current["body_html"] += f"<ul><li>{inner}</li></ul>"
        else:
            current["body_html"] += f"<p>{inner}</p>"

    _link(nodes)
    for n in nodes:
        n.pop("_raw_answers", None)
        if not n["answers"] and n is nodes[-1]:
            n["kind"] = "end"
    return {
        "script": {"start_node_id": nodes[0]["id"] if nodes else None, "quick_jumps": []},
        "nodes": nodes,
    }


def _link(nodes) -> None:
    """Проставляет переходы: по номеру шага, по названию, иначе — на следующий."""
    by_title = {n["title"].strip().lower(): n["id"] for n in nodes}

    def resolve(target: str, fallback):
        target = (target or "").strip()
        if not target:
            return fallback
        low = target.lower().rstrip(".")
        # Название шага важнее ключевого слова: если в документе есть шаг
        # «Завершение», стрелка на него ведёт в этот шаг, а не в никуда.
        if low in by_title:
            return by_title[low]
        m = _TARGET_NUM.match(target)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(nodes):
                return nodes[idx]["id"]
        if low in ("конец", "завершение", "стоп", "конец ветки", "завершить",
                   "конец разговора", "завершение разговора"):
            return None
        for title, nid in by_title.items():          # частичное совпадение названия
            if low in title or title in low:
                return nid
        return fallback

    for i, node in enumerate(nodes):
        nxt = nodes[i + 1]["id"] if i + 1 < len(nodes) else None
        raw = node.pop("_raw_answers", [])
        if raw:
            for order, (text, target) in enumerate(raw):
                node["answers"].append({
                    "id": -(1000 + i * 50 + order), "text": text[:500] or "Ответ",
                    "color": _answer_color(text), "next": resolve(target, nxt), "order": order,
                })
        elif nxt:
            node["answers"].append({
                "id": -(1000 + i * 50), "text": "Далее", "color": "gray",
                "next": nxt, "order": 0,
            })


def parse_file(path: str) -> dict:
    """Разбирает .docx / .txt / .md по расширению."""
    low = path.lower()
    if low.endswith(".docx"):
        return parse_script(_blocks_from_docx(path))
    with open(path, encoding="utf-8", errors="replace") as fh:
        return parse_script(_blocks_from_text(fh.read()))

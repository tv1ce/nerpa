"""Экспорт скрипта в Word (.docx).

Два варианта из ТЗ:
  plain — обычный документ: подряд заголовок шага и его текст, читается как речь;
  full  — «полный скрипт»: у каждого шага дополнительно показаны варианты
          ответов и номер вопроса, куда ведёт каждый ответ, т.е. ветвление
          видно на бумаге и документ можно отдать менеджеру или распечатать.

Форматирование текста (жирный/курсив/подчёркивание/списки/заголовки) переносится
из HTML rich-text редактора — иначе выгрузка теряет всё, ради чего его ставили.
"""
import io
from html.parser import HTMLParser

from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

from app.utils.script_text import render_placeholders

_ALIGN = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
}


def _parse_color(value: str):
    """#rrggbb / rgb(r,g,b) → RGBColor. Непонятный цвет игнорируем."""
    value = (value or "").strip().lower()
    try:
        if value.startswith("#") and len(value) == 7:
            return RGBColor(int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
        if value.startswith("rgb"):
            nums = [int(x) for x in value[value.find("(") + 1:value.find(")")].split(",")[:3]]
            return RGBColor(*nums)
    except Exception:
        return None
    return None


class _HtmlToDocx(HTMLParser):
    """Мини-рендерер HTML → абзацы python-docx.

    Поддерживает то, что умеет наш редактор: заголовки, абзацы, b/i/u/s,
    цвет текста, маркированные и нумерованные списки, цитаты, ссылки.
    Таблицы разворачиваются в строки «ячейка | ячейка» — верстать таблицы Word
    из произвольного HTML надёжно нельзя, а терять содержимое нельзя тем более.
    """

    def __init__(self, doc: Document):
        super().__init__(convert_charrefs=True)
        self.doc = doc
        self.p = None
        self.bold = self.italic = self.underline = self.strike = 0
        self.color = None
        self.list_stack = []          # ul / ol
        self.pending_style = None
        self.align = None
        self.in_table_row = False
        self.row_cells = []

    def _new_para(self, style=None, align=None):
        self.p = self.doc.add_paragraph(style=style)
        if align and align in _ALIGN:
            self.p.alignment = _ALIGN[align]
        return self.p

    def _ensure_para(self):
        if self.p is None:
            self._new_para(self.pending_style, self.align)
        return self.p

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        a = {k.lower(): (v or "") for k, v in attrs}
        style_attr = a.get("style", "")
        if "text-align" in style_attr:
            for chunk in style_attr.split(";"):
                if chunk.strip().startswith("text-align"):
                    self.align = chunk.split(":", 1)[1].strip()

        if tag in ("b", "strong"):
            self.bold += 1
        elif tag in ("i", "em"):
            self.italic += 1
        elif tag == "u":
            self.underline += 1
        elif tag in ("s", "strike", "del"):
            self.strike += 1
        elif tag in ("h1", "h2", "h3", "h4"):
            self.p = self.doc.add_paragraph(style="Heading %d" % min(int(tag[1]) + 1, 5))
        elif tag in ("p", "div"):
            self.p = None
            self.pending_style = None
        elif tag == "blockquote":
            self.p = None
            self.pending_style = "Quote"
        elif tag in ("ul", "ol"):
            self.list_stack.append(tag)
        elif tag == "li":
            kind = self.list_stack[-1] if self.list_stack else "ul"
            self.p = self.doc.add_paragraph(
                style="List Number" if kind == "ol" else "List Bullet")
        elif tag == "br":
            self._ensure_para().add_run().add_break()
        elif tag == "tr":
            self.in_table_row = True
            self.row_cells = []
            self.p = None
        elif tag in ("td", "th"):
            self.row_cells.append("")

        if "color" in style_attr and "background" not in style_attr:
            for chunk in style_attr.split(";"):
                prop, _, val = chunk.partition(":")
                if prop.strip() == "color":
                    self.color = _parse_color(val)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("b", "strong"):
            self.bold = max(0, self.bold - 1)
        elif tag in ("i", "em"):
            self.italic = max(0, self.italic - 1)
        elif tag == "u":
            self.underline = max(0, self.underline - 1)
        elif tag in ("s", "strike", "del"):
            self.strike = max(0, self.strike - 1)
        elif tag == "span":
            self.color = None
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
        elif tag in ("p", "div", "li", "h1", "h2", "h3", "h4", "blockquote"):
            self.p = None
            self.align = None
            if tag == "blockquote":
                self.pending_style = None
        elif tag == "tr" and self.in_table_row:
            self.in_table_row = False
            line = " | ".join(c.strip() for c in self.row_cells if c.strip())
            if line:
                self.doc.add_paragraph(line)
            self.row_cells = []
            self.p = None

    def handle_data(self, data):
        if not data or not data.strip():
            # пробел между словами внутри абзаца сохраняем, «пустые» узлы — нет
            if data and self.p is not None and data != "\n":
                self.p.add_run(" ")
            return
        if self.in_table_row and self.row_cells:
            self.row_cells[-1] += data
            return
        run = self._ensure_para().add_run(data)
        run.bold = bool(self.bold)
        run.italic = bool(self.italic)
        run.underline = bool(self.underline)
        if self.strike:
            run.font.strike = True
        if self.color is not None:
            run.font.color.rgb = self.color


def _render_html(doc: Document, html: str, ctx: dict | None = None) -> None:
    text = render_placeholders(html or "", ctx)
    if not text.strip():
        return
    parser = _HtmlToDocx(doc)
    parser.feed(text)
    parser.close()


def build_script_docx(script, nodes, mode: str = "full", ctx: dict | None = None) -> bytes:
    """Собирает .docx. nodes — узлы в порядке документа (см. ordered_nodes).

    mode='full' добавляет к каждому шагу блок «Возможные ответы» с указанием
    номера шага, куда ведёт ответ; mode='plain' — только текст.
    """
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)

    doc.add_heading(script.title or "Скрипт", level=0)
    if script.description:
        p = doc.add_paragraph(script.description)
        p.runs[0].italic = True

    numbers = {n.id: i + 1 for i, n in enumerate(nodes)}
    titles = {n.id: n.title for n in nodes}

    for idx, node in enumerate(nodes, start=1):
        heading = "%d. %s" % (idx, render_placeholders(node.title, ctx))
        if node.kind == "end":
            heading += "  — завершение разговора"
        doc.add_heading(heading, level=1)
        _render_html(doc, node.body_html, ctx)

        if mode != "full":
            continue

        answers = list(node.answers)
        if answers:
            p = doc.add_paragraph()
            p.add_run("Возможные ответы:").bold = True
            for ans in answers:
                target = ans.next_node_id
                if target and target in numbers:
                    where = "→ Вопрос %d. %s" % (numbers[target], titles.get(target, ""))
                else:
                    where = "→ завершение ветки"
                item = doc.add_paragraph(style="List Bullet")
                item.add_run(ans.text + "  ").bold = True
                item.add_run(where)
        elif node.kind != "end":
            doc.add_paragraph("Переход дальше не задан.", style="List Bullet")

        fields = getattr(node, "_fields", None) or []
        if fields:
            p = doc.add_paragraph()
            p.add_run("Заполнить:").bold = True
            for f in fields:
                doc.add_paragraph("%s: ______________" % (f.get("label") or f.get("key") or ""),
                                  style="List Bullet")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()

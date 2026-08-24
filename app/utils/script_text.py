"""Работа с форматированным текстом скриптов.

Текст шага редактируется как rich-text и хранится HTML-ом, поэтому перед
сохранением его нужно чистить: браузерный редактор легко тащит <script>,
обработчики on* и javascript:-ссылки — из вставки буфера обмена, из чужого
письма или намеренно. Здесь один разрешённый список тегов/атрибутов на весь
модуль, чтобы правила не разъезжались между автосохранением и импортом.

Тот же модуль отвечает за подстановки {{Имя}} — их видит и режим прохождения,
и экспорт в Word, и предпросмотр в конструкторе.
"""
import html as _html
import re
from html.parser import HTMLParser

# Теги, которые умеет ставить наш редактор и понимает экспорт в Word.
ALLOWED_TAGS = {
    "p", "br", "div", "span", "b", "strong", "i", "em", "u", "s", "strike", "del",
    "h1", "h2", "h3", "h4", "ul", "ol", "li", "a", "blockquote", "code", "pre",
    "table", "thead", "tbody", "tr", "th", "td", "hr", "mark", "sub", "sup",
}
# Пустые (void) теги — закрывать не нужно
VOID_TAGS = {"br", "hr"}
# Атрибуты по тегам. style пропускаем только по белому списку свойств (см. _clean_style).
ALLOWED_ATTRS = {
    "*": {"style"},
    "a": {"href", "title", "target", "rel"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}
_ALLOWED_CSS_PROPS = {"color", "background-color", "text-align", "font-weight",
                      "font-style", "text-decoration"}
_SAFE_HREF = re.compile(r"^(https?://|mailto:|tel:|/)", re.I)


def _clean_style(value: str) -> str:
    """Оставляет из inline-style только безопасные свойства (цвет, выравнивание)."""
    out = []
    for chunk in (value or "").split(";"):
        if ":" not in chunk:
            continue
        prop, _, val = chunk.partition(":")
        prop = prop.strip().lower()
        val = val.strip()
        if prop in _ALLOWED_CSS_PROPS and "url(" not in val.lower() and "expression" not in val.lower():
            out.append(f"{prop}: {val}")
    return "; ".join(out)


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._open = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style", "iframe", "object", "embed"):
            self._skip_depth += 1
            return
        if self._skip_depth or tag not in ALLOWED_TAGS:
            return
        allowed = ALLOWED_ATTRS.get("*", set()) | ALLOWED_ATTRS.get(tag, set())
        parts = []
        for name, value in attrs:
            name = (name or "").lower()
            if name not in allowed or value is None:
                continue
            if name == "style":
                value = _clean_style(value)
                if not value:
                    continue
            elif name == "href":
                if not _SAFE_HREF.match(value.strip()):
                    continue
            parts.append(f' {name}="{_html.escape(value, quote=True)}"')
        if tag in VOID_TAGS:
            self.out.append(f"<{tag}{''.join(parts)}>")
        else:
            self.out.append(f"<{tag}{''.join(parts)}>")
            self._open.append(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "iframe", "object", "embed"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth or tag in VOID_TAGS or tag not in ALLOWED_TAGS:
            return
        if tag in self._open:
            # закрываем всё, что осталось открытым внутри — иначе вёрстка «течёт»
            while self._open:
                last = self._open.pop()
                self.out.append(f"</{last}>")
                if last == tag:
                    break

    def handle_data(self, data):
        if not self._skip_depth:
            self.out.append(_html.escape(data))

    def result(self) -> str:
        while self._open:
            self.out.append(f"</{self._open.pop()}>")
        return "".join(self.out)


def sanitize_html(raw: str | None) -> str:
    """Чистит HTML из редактора: только разрешённые теги, атрибуты и ссылки."""
    if not raw:
        return ""
    p = _Sanitizer()
    p.feed(raw)
    p.close()
    return p.result().strip()


class _Textify(HTMLParser):
    """HTML → плоский текст: для поиска по скрипту и подписей на блок-схеме."""
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "blockquote", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self._BLOCK:
            self.parts.append("\n")
        if tag.lower() == "li":
            self.parts.append("• ")

    def handle_endtag(self, tag):
        if tag.lower() in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(raw: str | None) -> str:
    """Плоский текст без тегов, с сохранением абзацев."""
    if not raw:
        return ""
    p = _Textify()
    p.feed(raw)
    p.close()
    text = "".join(p.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# ── Подстановки {{Имя}} ──────────────────────────────────────────────────────
#
# Ключи намеренно русские: скрипты пишет РОП, а не разработчик. Латинские
# синонимы оставлены для интеграции — Bitrix отдаёт поля своими именами.

PLACEHOLDER_KEYS = {
    "имя": "name", "name": "name",
    "фамилия": "last_name", "last_name": "last_name",
    "компания": "company", "company": "company",
    "телефон": "phone", "phone": "phone",
    "email": "email", "почта": "email", "e-mail": "email",
    "должность": "post", "post": "post",
    "сделка": "deal", "deal": "deal",
    "менеджер": "manager", "manager": "manager",
    "сумма": "amount", "amount": "amount",
}

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^}]{1,60}?)\s*\}\}")


def render_placeholders(text: str | None, ctx: dict | None) -> str:
    """Подставляет значения вместо {{Имя}}, {{Компания}} и т.п.

    Неизвестный или пустой ключ остаётся как есть — менеджер во время звонка
    должен видеть, что подстановка не сработала, а не пустое место в фразе."""
    if not text:
        return ""
    ctx = ctx or {}
    if not ctx:
        return text

    def _sub(m):
        raw_key = m.group(1).strip()
        key = PLACEHOLDER_KEYS.get(raw_key.lower(), raw_key.lower())
        value = ctx.get(key) or ctx.get(raw_key) or ctx.get(raw_key.lower())
        return str(value) if value not in (None, "") else m.group(0)

    return _PLACEHOLDER_RE.sub(_sub, text)

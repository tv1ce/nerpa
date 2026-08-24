"""Импорт скриптов из HyperScript (hyper-script.ru) в наш граф.

Сервис не умеет экспортировать скрипты, но его конструктор загружает сценарий
целиком одним ответом `scripts.load_without_external_scripts`. Формат ответа:

    response.name                 — название скрипта
    response.target               — цель разговора (идёт в описание)
    response.data.steps[]         — шаги: id, title, text (HTML), top/left,
                                    is_goal (завершающий шаг), images
    response.data.connections[]   — переходы: source, target, condition (текст
                                    ответа), status (positive/negative), sort
    response.data.starred[]       — разделы скрипта: {n: название, s: [id шагов]}

Ложится на нашу модель почти один в один: шаг → ScriptNode, connection →
ScriptAnswer (несколько ответов в один шаг получаются сами собой, потому что
переход — это ребро, а не поле шага). Разделы становятся быстрыми переходами
менеджера: в них перечислены смысловые блоки разговора, а не отдельные реплики.

Текст шага переносится с оформлением. HyperScript красит его классами
(`richedit_comment` — ремарка оператору, `wysiwyg-color-*` — цвет), а наш
санитайзер классы вырезает, поэтому классы заранее переводятся в inline-стили —
иначе после импорта пропала бы разметка, по которой менеджер отличает реплику
клиенту от подсказки себе.
"""
import html as _html
import json
import re

# Классы HyperScript → inline-стиль. Значения подобраны так, чтобы смысл
# сохранялся: ремарка оператору — приглушённый курсив, интонации и цвета — цвет.
_CLASS_STYLES = {
    "richedit_comment": "color: #64748b; font-style: italic",
    "richedit_emotion_interrogative": "color: #2563eb",
    "richedit_emotion_exclamative": "color: #b45309",
    "richedit_emotion_affirmative": "color: #15803d",
    "wysiwyg-color-green": "color: #16a34a",
    "wysiwyg-color-red": "color: #dc2626",
    "wysiwyg-color-blue": "color: #2563eb",
    "wysiwyg-color-orange": "color: #ea580c",
    "wysiwyg-color-black": "color: #0f172a",
    "wysiwyg-color-gray": "color: #64748b",
    "wysiwyg-color-grey": "color: #64748b",
}

_STATUS_COLORS = {"positive": "green", "negative": "red"}

_CLASS_ATTR_RE = re.compile(r'\sclass="([^"]*)"')
_TAG_RE = re.compile(r"<[^>]+>")


def _styles_from_classes(raw_html: str) -> str:
    """class="richedit_comment" → style="color: …" (класс наш санитайзер срежет)."""
    if not raw_html:
        return ""

    def repl(m):
        styles = [_CLASS_STYLES[c] for c in m.group(1).split() if c in _CLASS_STYLES]
        return f' style="{"; ".join(styles)}"' if styles else ""

    return _CLASS_ATTR_RE.sub(repl, raw_html)


def _plain(raw_html: str) -> str:
    text = _TAG_RE.sub(" ", raw_html or "")
    text = _html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _title_for(step: dict, fallback: str) -> str:
    """Название шага: своё, иначе первая фраза текста — по ней шаг ищут в дереве."""
    title = (step.get("title") or "").strip()
    if title:
        return title[:300]
    text = _plain(step.get("text"))
    if not text:
        return fallback
    cut = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    if len(cut) > 90:
        cut = cut[:90].rsplit(" ", 1)[0] + "…"
    return cut or fallback


def _num(value, default=0.0) -> float:
    try:
        return float(str(value).strip() or default)
    except (TypeError, ValueError):
        return default


def convert(payload: dict) -> dict:
    """Ответ HyperScript → граф в формате apply_graph (временные id отрицательные).

    Принимает и полный ответ {status, response: {...}}, и сам объект response.
    """
    response = payload.get("response", payload) if isinstance(payload, dict) else {}
    data = response.get("data") or {}
    raw_steps = data.get("steps") or []
    raw_conns = data.get("connections") or []
    sections = data.get("starred") or []

    if not raw_steps:
        return {"script": {}, "nodes": []}

    # Порядок шагов — по разделам скрипта: так документ и дерево читаются в той
    # логике, в какой автор строил разговор, а не в порядке хранения.
    order, seen = [], set()
    for section in sections:
        for sid in (section.get("s") or []):
            if sid not in seen:
                seen.add(sid)
                order.append(sid)
    by_id = {s.get("id"): s for s in raw_steps}
    for s in raw_steps:
        if s.get("id") not in seen:
            seen.add(s.get("id"))
            order.append(s.get("id"))

    # HyperScript кладёт «start» в произвольное место холста, координаты бывают
    # отрицательными — сдвигаем всё в положительную область, иначе схема
    # открывается за краем экрана.
    min_x = min((_num(s.get("left")) for s in raw_steps), default=0.0)
    min_y = min((_num(s.get("top")) for s in raw_steps), default=0.0)

    id_map, nodes = {}, []
    for i, sid in enumerate(order):
        step = by_id.get(sid)
        if not step:
            continue
        tmp_id = -(i + 1)
        id_map[sid] = tmp_id
        nodes.append({
            "id": tmp_id,
            "title": _title_for(step, f"Шаг {i + 1}"),
            "body_html": _styles_from_classes(step.get("text") or ""),
            "kind": "end" if str(step.get("is_goal")).lower() == "true" else "question",
            "x": round(_num(step.get("left")) - min_x + 60, 1),
            "y": round(_num(step.get("top")) - min_y + 60, 1),
            "fields": [],
            "answers": [],
        })

    nodes_by_tmp = {n["id"]: n for n in nodes}
    for conn in sorted(raw_conns, key=lambda c: _num(c.get("sort"))):
        source = id_map.get(conn.get("source"))
        node = nodes_by_tmp.get(source)
        if not node:
            continue
        condition = (conn.get("condition") or "").strip() or "Далее"
        node["answers"].append({
            "id": -(10000 + len(node["answers"]) + abs(source) * 100),
            "text": condition[:500],
            "color": _STATUS_COLORS.get(conn.get("status") or "", "gray"),
            "next": id_map.get(conn.get("target")),
            "order": len(node["answers"]),
        })

    # Быстрые переходы — по одному на раздел: первый шаг «Приветствия»,
    # «Квалификации», «Работы с возражениями» и т.д. Подпись кнопки берём из
    # названия раздела: заголовок шага — это первая фраза реплики, на кнопке
    # менеджеру от неё пользы нет.
    quick = []
    for section in sections:
        name = (section.get("n") or "").strip()
        for sid in (section.get("s") or []):
            if sid in id_map:
                quick.append({"id": id_map[sid], "label": name[:80]})
                break

    start = id_map.get("start") or (nodes[0]["id"] if nodes else None)
    title = (response.get("name") or "Скрипт HyperScript").strip()
    target = (response.get("target") or "").strip()

    return {
        "script": {
            "title": title[:300],
            "description": f"Цель: {target}" if target else None,
            "start_node_id": start,
            "quick_jumps": quick,
        },
        "nodes": nodes,
    }


def convert_dump(raw) -> list:
    """Разбирает выгрузку целиком. Понимает три формы:

    * {"scripts": [{"id":…, "name":…, "data": <response>}, …]} — наш дамп из браузера
    * [<ответ>, <ответ>, …]                                    — просто список
    * {"status":…, "response": {...}}                          — один скрипт

    Возвращает список графов в формате apply_graph.
    """
    if isinstance(raw, str):
        raw = json.loads(raw)

    items = []
    if isinstance(raw, dict) and isinstance(raw.get("scripts"), list):
        for entry in raw["scripts"]:
            payload = entry.get("data") if isinstance(entry, dict) else None
            if payload:
                items.append(payload)
    elif isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = [raw]

    out = []
    for payload in items:
        graph = convert(payload)
        if graph.get("nodes"):
            out.append(graph)
    return out

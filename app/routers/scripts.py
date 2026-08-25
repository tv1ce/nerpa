"""Скрипты продаж — конструктор ветвящихся сценариев разговора.

Один граф → три режима отображения → одна база и одна логика переходов:

  /scripts/{id}/edit   — конструктор: блок-схема + дерево шагов + rich-text;
  /scripts/{id}/full   — «полный скрипт»: весь сценарий документом с ветвлением;
  /scripts/{id}/run    — прохождение: вопрос и кнопки ответов для менеджера.

Режимы отличаются только отрисовкой: данные берутся из одних и тех же таблиц
(scripts / script_nodes / script_answers), переход по ответу везде считается
одинаково — ScriptAnswer.next_node_id. Поэтому правка в конструкторе сразу
видна и в документе, и у менеджера в звонке.

Запись графа идёт одним эндпоинтом POST /scripts/{id}/graph, который принимает
скрипт целиком. Так автосохранение, undo/redo и откат к версии из истории — это
одна и та же операция, а не три разных пути записи, которые разъезжаются.
"""
import asyncio
import json
import logging
from datetime import timedelta

from fastapi import APIRouter, Request, Depends, Form, UploadFile, File
from fastapi.responses import (
    HTMLResponse, RedirectResponse, JSONResponse, Response,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import login_required, role_required
from app.database import get_db
from app.models import (
    Script, ScriptFolder, ScriptNode, ScriptAnswer, ScriptVersion, ScriptRun,
    User, SCRIPT_STATUSES, SCRIPT_NODE_KINDS, SCRIPT_ANSWER_COLORS,
)
from app.tz import now as msk_now
from app.utils import log_action
from app.utils.script_text import sanitize_html, html_to_text, render_placeholders
from app.utils.script_docx import build_script_docx
from app.utils.script_import import parse_file
from app.utils.script_hyperscript import convert_dump

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scripts", tags=["scripts"])
templates = Jinja2Templates(directory="app/templates")

# Роли, которым разрешено менять структуру скриптов. Менеджер по продажам
# (viewer/warehouse/field_rep) скрипты только проходит — см. ТЗ, п. 26.
EDITOR_ROLES = ("admin", "manager", "sales")

# Как часто автосохранение кладёт снимок в историю версий. Каждое нажатие
# клавиши версией быть не должно — иначе история превращается в шум.
VERSION_THROTTLE = timedelta(minutes=10)

MAX_TITLE = 300


def _can_edit(request: Request) -> bool:
    return request.session.get("user_role") in EDITOR_ROLES


def _is_admin(request: Request) -> bool:
    return request.session.get("user_role") == "admin"


def _deny(msg: str = "Недостаточно прав для изменения скриптов."):
    return HTMLResponse(
        '<div style="font-family:sans-serif;text-align:center;padding:2rem">'
        f'<h2>403 — Нет доступа</h2><p>{msg}</p>'
        '<a href="/scripts/">К списку скриптов</a></div>', status_code=403)


def _json_err(msg: str, code: int = 400):
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


# ── Папки ────────────────────────────────────────────────────────────────────

def _folder_children(folders: list, parent_id):
    return [f for f in folders if f.parent_id == parent_id]


def _folder_path(db: Session, folder_id):
    """Хлебные крошки от корня до папки. Защищено от циклов в parent_id."""
    path = []
    seen = set()
    cur = db.get(ScriptFolder, folder_id) if folder_id else None
    while cur and cur.id not in seen:
        seen.add(cur.id)
        path.append(cur)
        cur = cur.parent
    return list(reversed(path))


def _descendant_folder_ids(db: Session, folder_id: int) -> set:
    """id папки и всех вложенных — для удаления и запрета переноса в себя."""
    all_folders = db.query(ScriptFolder).all()
    by_parent = {}
    for f in all_folders:
        by_parent.setdefault(f.parent_id, []).append(f.id)
    out, stack = set(), [folder_id]
    while stack:
        fid = stack.pop()
        if fid in out:
            continue
        out.add(fid)
        stack.extend(by_parent.get(fid, []))
    return out


# ── Граф ─────────────────────────────────────────────────────────────────────

def _fields_of(node: ScriptNode) -> list:
    try:
        data = json.loads(node.fields_json or "[]")
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _quick_jumps_of(script: Script) -> list:
    """Быстрые переходы: [{"id": id узла, "label": подпись кнопки}].

    Подпись нужна отдельно от заголовка шага: менеджеру во время звонка нужна
    кнопка «Возражения» или «Цена», а заголовок шага — это первая фраза реплики
    («Здравствуйте, {{Имя}}!»), которая на кнопке не читается. Пустая подпись =
    берём заголовок узла, чтобы старые скрипты работали без правок.

    Старый формат (просто список id) читается как есть — скрипты, сохранённые
    до появления подписей, не ломаются."""
    try:
        data = json.loads(script.quick_jumps or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict):
            try:
                out.append({"id": int(item.get("id")), "label": (item.get("label") or "").strip()})
            except (TypeError, ValueError):
                continue
        else:
            try:
                out.append({"id": int(item), "label": ""})
            except (TypeError, ValueError):
                continue
    return out


def _quick_jump_pairs(script: Script, by_id: dict) -> list:
    """[(узел, подпись)] — только для существующих узлов, порядок сохраняется."""
    pairs = []
    for jump in _quick_jumps_of(script):
        node = by_id.get(jump["id"])
        if node:
            pairs.append((node, jump["label"] or node.title))
    return pairs


def _dump_quick_jumps(jumps: list) -> str | None:
    return json.dumps(jumps, ensure_ascii=False) if jumps else None


def ordered_nodes(script: Script) -> list:
    """Узлы в порядке документа: обход от стартового шага по ответам, в ширину.

    Нумерация шагов в «Полном скрипте» и в Word должна читаться как сценарий:
    сначала приветствие, потом то, куда оно ведёт. Порядок в БД (sort_order)
    для этого не годится — блоки добавляют в произвольной последовательности.
    Узлы, до которых нельзя дойти от старта (черновые ветки), идут в конец —
    потерять их нельзя, но и в основную нумерацию они не лезут.
    """
    nodes = list(script.nodes)
    if not nodes:
        return []
    by_id = {n.id: n for n in nodes}
    start = by_id.get(script.start_node_id) or nodes[0]

    out, seen = [], set()
    queue = [start.id]
    while queue:
        nid = queue.pop(0)
        if nid in seen or nid not in by_id:
            continue
        seen.add(nid)
        node = by_id[nid]
        out.append(node)
        for ans in node.answers:
            if ans.next_node_id and ans.next_node_id not in seen:
                queue.append(ans.next_node_id)

    for n in sorted(nodes, key=lambda x: (x.sort_order or 0, x.id)):
        if n.id not in seen:
            out.append(n)
    return out


def graph_dict(script: Script) -> dict:
    """Скрипт целиком в JSON — общий формат для конструктора, версий и отката."""
    return {
        "script": {
            "id": script.id,
            "title": script.title,
            "description": script.description or "",
            "status": script.status or "draft",
            "start_node_id": script.start_node_id,
            "quick_jumps": _quick_jumps_of(script),
        },
        "nodes": [
            {
                "id": n.id,
                "title": n.title,
                "body_html": n.body_html or "",
                "kind": n.kind or "question",
                "x": n.pos_x or 0.0,
                "y": n.pos_y or 0.0,
                "order": n.sort_order or 0,
                "fields": _fields_of(n),
                "answers": [
                    {
                        "id": a.id,
                        "text": a.text,
                        "color": a.color or "gray",
                        "next": a.next_node_id,
                        "order": a.sort_order or 0,
                    }
                    for a in n.answers
                ],
            }
            for n in sorted(script.nodes, key=lambda x: (x.sort_order or 0, x.id))
        ],
    }


def apply_graph(db: Session, script: Script, payload: dict, user_id: int | None) -> dict:
    """Записывает присланный граф в БД. Возвращает карту временных id → реальных.

    Новые узлы и ответы приходят с отрицательными id (их назначает браузер, пока
    блок ещё не сохранён). Ссылки next между новыми блоками переводятся на
    реальные id уже после создания — иначе связь на только что созданный блок
    потерялась бы.
    """
    meta = payload.get("script") or {}
    if "title" in meta:
        title = (meta.get("title") or "").strip()[:MAX_TITLE]
        if title:
            script.title = title
    if "description" in meta:
        script.description = (meta.get("description") or "").strip() or None
    if meta.get("status") in SCRIPT_STATUSES:
        script.status = meta["status"]

    incoming = payload.get("nodes") or []
    existing = {n.id: n for n in script.nodes}
    keep_ids = set()
    id_map = {}

    # 1) узлы: обновляем существующие, создаём новые
    for order, raw in enumerate(incoming):
        raw_id = raw.get("id")
        node = existing.get(raw_id) if isinstance(raw_id, int) and raw_id > 0 else None
        if node is None:
            node = ScriptNode(script_id=script.id)
            db.add(node)
        node.title = (raw.get("title") or "Без названия").strip()[:MAX_TITLE]
        node.body_html = sanitize_html(raw.get("body_html"))
        node.kind = raw.get("kind") if raw.get("kind") in SCRIPT_NODE_KINDS else "question"
        node.pos_x = float(raw.get("x") or 0)
        node.pos_y = float(raw.get("y") or 0)
        node.sort_order = order
        fields = raw.get("fields")
        node.fields_json = json.dumps(fields, ensure_ascii=False) if fields else None
        db.flush()          # нужен id, чтобы связать ответы и переходы
        id_map[raw_id] = node.id
        keep_ids.add(node.id)

    # 2) удаляем узлы, которых больше нет в присланном графе
    for nid, node in existing.items():
        if nid not in keep_ids:
            db.delete(node)

    def _resolve(target):
        """id получателя перехода: реальный, новый по карте, либо None."""
        if target in (None, "", 0):
            return None
        try:
            target = int(target)
        except (TypeError, ValueError):
            return None
        mapped = id_map.get(target, target)
        return mapped if mapped in keep_ids else None

    # 3) ответы каждого узла
    for raw in incoming:
        node_id = id_map.get(raw.get("id"))
        node = db.get(ScriptNode, node_id)
        if not node:
            continue
        existing_answers = {a.id: a for a in node.answers}
        seen_answers = set()
        for order, raw_a in enumerate(raw.get("answers") or []):
            aid = raw_a.get("id")
            ans = existing_answers.get(aid) if isinstance(aid, int) and aid > 0 else None
            if ans is None:
                ans = ScriptAnswer(node_id=node.id)
                db.add(ans)
            ans.text = (raw_a.get("text") or "Ответ").strip()[:500]
            ans.color = raw_a.get("color") if raw_a.get("color") in SCRIPT_ANSWER_COLORS else "gray"
            ans.next_node_id = _resolve(raw_a.get("next"))
            ans.sort_order = order
            db.flush()
            seen_answers.add(ans.id)
        for aid, ans in existing_answers.items():
            if aid not in seen_answers:
                db.delete(ans)

    # 4) точка входа и быстрые переходы
    start = _resolve(meta.get("start_node_id"))
    script.start_node_id = start or (min(keep_ids) if keep_ids else None)
    jumps = []
    for raw_jump in (meta.get("quick_jumps") or []):
        raw_id = raw_jump.get("id") if isinstance(raw_jump, dict) else raw_jump
        label = (raw_jump.get("label") or "").strip()[:80] if isinstance(raw_jump, dict) else ""
        node_id = _resolve(raw_id)
        if node_id:
            jumps.append({"id": node_id, "label": label})
    script.quick_jumps = _dump_quick_jumps(jumps)

    script.updated_at = msk_now()
    script.updated_by_id = user_id
    db.flush()
    return {str(k): v for k, v in id_map.items() if isinstance(k, int) and k < 0}


def _snapshot(db: Session, script: Script, user_id: int | None, note: str,
              throttle: bool = False) -> None:
    """Кладёт снимок графа в историю версий."""
    if throttle:
        last = (db.query(ScriptVersion)
                .filter(ScriptVersion.script_id == script.id)
                .order_by(ScriptVersion.created_at.desc()).first())
        if last and last.created_at and (msk_now() - last.created_at) < VERSION_THROTTLE:
            return
    db.add(ScriptVersion(
        script_id=script.id,
        data_json=json.dumps(graph_dict(script), ensure_ascii=False),
        note=note[:200],
        author_id=user_id,
    ))


def _copy_script(db: Session, src: Script, title: str, folder_id, user_id) -> Script:
    """Полностью независимая копия скрипта: свои узлы, ответы и переходы.

    Ответы копии должны указывать на узлы копии, а не оригинала — иначе правка
    одного скрипта поехала бы во второй, чего ТЗ прямо запрещает (п. 5)."""
    dup = Script(
        title=title[:MAX_TITLE],
        description=src.description,
        folder_id=folder_id,
        status="draft",
        owner_id=user_id,
        updated_by_id=user_id,
    )
    db.add(dup)
    db.flush()

    node_map = {}
    for n in src.nodes:
        copy = ScriptNode(
            script_id=dup.id, title=n.title, body_html=n.body_html, kind=n.kind,
            pos_x=n.pos_x, pos_y=n.pos_y, sort_order=n.sort_order,
            fields_json=n.fields_json,
        )
        db.add(copy)
        db.flush()
        node_map[n.id] = copy.id

    for n in src.nodes:
        for a in n.answers:
            db.add(ScriptAnswer(
                node_id=node_map[n.id], text=a.text, color=a.color,
                next_node_id=node_map.get(a.next_node_id),
                sort_order=a.sort_order, action_json=a.action_json,
            ))

    dup.start_node_id = node_map.get(src.start_node_id)
    jumps = [{"id": node_map[j["id"]], "label": j["label"]}
             for j in _quick_jumps_of(src) if j["id"] in node_map]
    dup.quick_jumps = _dump_quick_jumps(jumps)
    db.flush()
    return dup


# ── Контекст CRM для подстановок {{Имя}} ─────────────────────────────────────

def crm_context(request: Request) -> dict:
    """Значения подстановок: из query-параметров (открытие из карточки Bitrix24)
    и из сессии. Пустой словарь = подстановки остаются видимыми как {{Имя}}."""
    q = request.query_params
    ctx = {}
    for key in ("name", "last_name", "company", "phone", "email", "post", "deal", "amount"):
        value = q.get(key) or q.get("crm_" + key)
        if value:
            ctx[key] = value.strip()[:200]
    if not ctx.get("manager"):
        ctx["manager"] = request.session.get("user_name") or ""
    return {k: v for k, v in ctx.items() if v}


# Данные клиента из CRM по (тип, id). Кэш нужен, чтобы переход по каждому шагу
# скрипта не превращался в отдельный запрос к порталу: за один разговор менеджер
# проходит десяток шагов, а карточка за это время не меняется.
_CRM_CACHE: dict[tuple, tuple] = {}
_CRM_CACHE_TTL = timedelta(minutes=10)


def crm_context_for(request: Request, db: Session) -> dict:
    """Подстановки для скрипта: явные параметры плюс данные из карточки CRM.

    Явно переданные значения важнее: если ссылку собрали руками с именем, оно и
    подставится. Если в ссылке только crm_entity и crm_id — идём в Bitrix24 за
    именем, компанией и телефоном сами. Ошибка портала не мешает открыть
    скрипт: подстановки просто останутся видимыми как {{Имя}}.
    """
    ctx = crm_context(request)
    entity = (request.query_params.get("crm_entity") or "").strip().lower()
    entity_id = (request.query_params.get("crm_id") or "").strip()
    if not entity or not entity_id:
        return ctx
    # Явно переданных данных достаточно — портал не тревожим
    if ctx.get("name") or ctx.get("company") or ctx.get("phone"):
        return ctx

    key = (entity, entity_id)
    cached = _CRM_CACHE.get(key)
    if cached and cached[0] > msk_now():
        fetched = cached[1]
    else:
        fetched = _b24_client_context(db, entity, entity_id)
        _CRM_CACHE[key] = (msk_now() + _CRM_CACHE_TTL, fetched)
        # Кэш живёт в памяти процесса и не должен расти бесконечно
        if len(_CRM_CACHE) > 500:
            for stale in [k for k, v in _CRM_CACHE.items() if v[0] <= msk_now()]:
                _CRM_CACHE.pop(stale, None)

    merged = dict(fetched or {})
    merged.update(ctx)          # явное поверх подтянутого
    return merged


def _base_ctx(request: Request, db: Session) -> dict:
    """Общий контекст шаблонов раздела."""
    return {
        "request": request,
        "can_edit": _can_edit(request),
        "is_admin": _is_admin(request),
        "statuses": SCRIPT_STATUSES,
        "node_kinds": SCRIPT_NODE_KINDS,
        "answer_colors": SCRIPT_ANSWER_COLORS,
    }


# ── Список скриптов и папки ──────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
@login_required
async def index(request: Request, folder: str = "", q: str = "",
                db: Session = Depends(get_db)):
    """Список скриптов текущей папки. При ?q= — глобальный поиск по всему разделу."""
    folder_id = int(folder) if folder.isdigit() else None
    folders = db.query(ScriptFolder).order_by(ScriptFolder.name).all()
    query = (q or "").strip()

    matched_nodes = {}
    if query:
        like = f"%{query.lower()}%"
        scripts = (db.query(Script)
                   .filter(Script.title.ilike(like) | Script.description.ilike(like))
                   .order_by(Script.updated_at.desc()).all())
        # поиск по тексту вопросов и ответов — ТЗ п. 20
        node_hits = (db.query(ScriptNode)
                     .filter(ScriptNode.title.ilike(like) | ScriptNode.body_html.ilike(like))
                     .limit(300).all())
        answer_hits = (db.query(ScriptAnswer).filter(ScriptAnswer.text.ilike(like))
                       .limit(300).all())
        found_ids = {s.id for s in scripts}
        for n in node_hits:
            matched_nodes.setdefault(n.script_id, []).append(("Вопрос", n.title, n.id))
        for a in answer_hits:
            node = db.get(ScriptNode, a.node_id)
            if node:
                matched_nodes.setdefault(node.script_id, []).append(("Ответ", a.text, node.id))
        for sid in matched_nodes:
            if sid not in found_ids:
                s = db.get(Script, sid)
                if s:
                    scripts.append(s)
        shown_folders = [f for f in folders if query.lower() in (f.name or "").lower()]
    else:
        scripts = (db.query(Script).filter(Script.folder_id == folder_id)
                   .order_by(Script.updated_at.desc()).all())
        shown_folders = _folder_children(folders, folder_id)

    from app.models import CompanySettings
    company = db.query(CompanySettings).first()

    counts = {}
    for s in scripts:
        counts[s.id] = db.query(ScriptNode).filter(ScriptNode.script_id == s.id).count()

    ctx = _base_ctx(request, db)
    ctx.update({
        "folders": folders,
        "shown_folders": shown_folders,
        "scripts": scripts,
        "folder_id": folder_id,
        "breadcrumbs": _folder_path(db, folder_id),
        "q": query,
        "matched_nodes": matched_nodes,
        "node_counts": counts,
        "users": db.query(User).filter(User.is_active == True).order_by(User.full_name).all(),
        "public_url": (company.public_url or "").rstrip("/") if company else "",
    })
    return templates.TemplateResponse(request, "scripts/index.html", ctx)


@router.post("/folders/create")
@role_required("sales")
async def folder_create(request: Request, name: str = Form(...), parent_id: str = Form(""),
                        db: Session = Depends(get_db)):
    if not _can_edit(request):
        return _deny()
    parent = int(parent_id) if parent_id.isdigit() else None
    db.add(ScriptFolder(name=name.strip()[:200] or "Новая папка", parent_id=parent,
                        created_by_id=request.session.get("user_id")))
    db.commit()
    return RedirectResponse(url=f"/scripts/?folder={parent or ''}", status_code=302)


@router.post("/folders/{fid}/rename")
@role_required("sales")
async def folder_rename(request: Request, fid: int, name: str = Form(...),
                        db: Session = Depends(get_db)):
    if not _can_edit(request):
        return _deny()
    folder = db.get(ScriptFolder, fid)
    if folder:
        folder.name = name.strip()[:200] or folder.name
        db.commit()
    return RedirectResponse(url=f"/scripts/?folder={folder.parent_id or '' if folder else ''}",
                            status_code=302)


@router.post("/folders/{fid}/move")
@role_required("sales")
async def folder_move(request: Request, fid: int, parent_id: str = Form(""),
                      db: Session = Depends(get_db)):
    if not _can_edit(request):
        return _deny()
    folder = db.get(ScriptFolder, fid)
    if not folder:
        return RedirectResponse(url="/scripts/", status_code=302)
    target = int(parent_id) if parent_id.isdigit() else None
    # папку нельзя положить в саму себя или в свою подпапку — получилось бы
    # «висящее» поддерево, невидимое из корня
    if target and target in _descendant_folder_ids(db, fid):
        return RedirectResponse(url=f"/scripts/?folder={folder.parent_id or ''}&err=loop",
                                status_code=302)
    folder.parent_id = target
    db.commit()
    return RedirectResponse(url=f"/scripts/?folder={target or ''}", status_code=302)


@router.post("/folders/{fid}/delete")
@role_required("admin")
async def folder_delete(request: Request, fid: int, db: Session = Depends(get_db)):
    folder = db.get(ScriptFolder, fid)
    if not folder:
        return RedirectResponse(url="/scripts/", status_code=302)
    parent_id = folder.parent_id
    ids = _descendant_folder_ids(db, fid)
    # скрипты не удаляем вместе с папкой — поднимаем на уровень выше
    db.query(Script).filter(Script.folder_id.in_(ids)).update(
        {"folder_id": parent_id}, synchronize_session=False)
    db.query(ScriptFolder).filter(ScriptFolder.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return RedirectResponse(url=f"/scripts/?folder={parent_id or ''}", status_code=302)


# ── Скрипт: создание, копирование, перемещение, удаление ─────────────────────

@router.post("/create")
@role_required("sales")
async def script_create(request: Request, title: str = Form("Новый скрипт"),
                        folder_id: str = Form(""), db: Session = Depends(get_db)):
    if not _can_edit(request):
        return _deny()
    user_id = request.session.get("user_id")
    script = Script(
        title=title.strip()[:MAX_TITLE] or "Новый скрипт",
        folder_id=int(folder_id) if folder_id.isdigit() else None,
        status="draft", owner_id=user_id, updated_by_id=user_id,
    )
    db.add(script)
    db.flush()
    first = ScriptNode(script_id=script.id, title="Приветствие", kind="question",
                       body_html="<p>Здравствуйте, {{Имя}}!</p>", pos_x=80, pos_y=80)
    db.add(first)
    db.flush()
    script.start_node_id = first.id
    db.commit()
    log_action(db, "script", script.id, "create", user_id, f"Создан скрипт «{script.title}»")
    db.commit()
    return RedirectResponse(url=f"/scripts/{script.id}/edit", status_code=302)


@router.post("/import")
@role_required("sales")
async def script_import(request: Request, files: list[UploadFile] = File(...),
                        folder_id: str = Form(""), db: Session = Depends(get_db)):
    """Импорт готовых скриптов: файл → скрипт-граф.

    Понимает два источника. Документы (.docx/.txt/.md) разбираются по заголовкам
    и стрелкам «Да → Вопрос 5» (app/utils/script_import.py). Выгрузка из
    HyperScript (.json) приходит уже графом — шаги и связи переносятся один в
    один вместе с ветвлением и оформлением (app/utils/script_hyperscript.py);
    в одном файле может лежать сразу вся библиотека скриптов.

    Запись — тот же apply_graph, что и у конструктора: импортированный скрипт
    сразу редактируется как обычный."""
    if not _can_edit(request):
        return _deny()
    import os
    import tempfile

    user_id = request.session.get("user_id")
    target_folder = int(folder_id) if folder_id.isdigit() else None
    created = 0
    for upload in files:
        name = os.path.basename(upload.filename or "")
        low = name.lower()
        if not low.endswith((".docx", ".txt", ".md", ".json")):
            continue
        raw = await upload.read()

        graphs = []
        try:
            if low.endswith(".json"):
                # Выгрузка HyperScript: в файле может быть и один скрипт, и все сразу
                graphs = convert_dump(raw.decode("utf-8", "replace"))
            else:
                fd, tmp_path = tempfile.mkstemp(suffix=os.path.splitext(name)[1])
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(raw)
                    graphs = [parse_file(tmp_path)]
                finally:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
        except Exception as e:
            logger.error("Импорт скрипта %s: %s", name, e)
            continue

        for payload in graphs:
            if not payload.get("nodes"):
                continue
            # Название из самого скрипта важнее имени файла: в выгрузке оно есть,
            # а у документа — нет, там осмысленное имя как раз у файла.
            meta = payload.get("script") or {}
            title = (meta.get("title") or os.path.splitext(name)[0])[:MAX_TITLE]
            script = Script(
                title=title, description=meta.get("description"),
                folder_id=target_folder, status="draft",
                owner_id=user_id, updated_by_id=user_id,
            )
            db.add(script)
            db.flush()
            apply_graph(db, script, payload, user_id)
            _snapshot(db, script, user_id, f"Импорт из файла {name}")
            created += 1
    db.commit()
    return RedirectResponse(
        url=f"/scripts/?folder={target_folder or ''}&imported={created}", status_code=302)


@router.post("/{sid}/duplicate")
@role_required("sales")
async def script_duplicate(request: Request, sid: int, db: Session = Depends(get_db)):
    if not _can_edit(request):
        return _deny()
    src = db.get(Script, sid)
    if not src:
        return RedirectResponse(url="/scripts/", status_code=302)
    dup = _copy_script(db, src, f"{src.title} — копия", src.folder_id,
                       request.session.get("user_id"))
    db.commit()
    return RedirectResponse(url=f"/scripts/{dup.id}/edit", status_code=302)


@router.post("/{sid}/rename")
@role_required("sales")
async def script_rename(request: Request, sid: int, title: str = Form(...),
                        description: str = Form(""), status: str = Form("draft"),
                        owner_id: str = Form(""), db: Session = Depends(get_db)):
    if not _can_edit(request):
        return _deny()
    script = db.get(Script, sid)
    if script:
        script.title = title.strip()[:MAX_TITLE] or script.title
        script.description = description.strip() or None
        if status in SCRIPT_STATUSES:
            script.status = status
        script.owner_id = int(owner_id) if owner_id.isdigit() else None
        script.updated_at = msk_now()
        script.updated_by_id = request.session.get("user_id")
        db.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/scripts/", status_code=302)


@router.post("/{sid}/move")
@role_required("sales")
async def script_move(request: Request, sid: int, folder_id: str = Form(""),
                      db: Session = Depends(get_db)):
    if not _can_edit(request):
        return _deny()
    script = db.get(Script, sid)
    if script:
        script.folder_id = int(folder_id) if folder_id.isdigit() else None
        db.commit()
    return RedirectResponse(url=f"/scripts/?folder={folder_id if folder_id.isdigit() else ''}",
                            status_code=302)


@router.post("/{sid}/delete")
@role_required("admin")
async def script_delete(request: Request, sid: int, db: Session = Depends(get_db)):
    script = db.get(Script, sid)
    if not script:
        return RedirectResponse(url="/scripts/", status_code=302)
    folder_id = script.folder_id
    title = script.title
    db.query(ScriptVersion).filter(ScriptVersion.script_id == sid).delete(synchronize_session=False)
    db.query(ScriptRun).filter(ScriptRun.script_id == sid).delete(synchronize_session=False)
    db.delete(script)
    db.commit()
    log_action(db, "script", sid, "delete", request.session.get("user_id"),
               f"Удалён скрипт «{title}»")
    db.commit()
    return RedirectResponse(url=f"/scripts/?folder={folder_id or ''}", status_code=302)


# ── Режим 1: конструктор ─────────────────────────────────────────────────────

@router.get("/{sid}/edit", response_class=HTMLResponse)
@login_required
async def editor(request: Request, sid: int, db: Session = Depends(get_db)):
    script = db.get(Script, sid)
    if not script:
        return RedirectResponse(url="/scripts/", status_code=302)
    if not _can_edit(request):
        return RedirectResponse(url=f"/scripts/{sid}/full", status_code=302)
    ctx = _base_ctx(request, db)
    ctx.update({
        "script": script,
        "graph_json": json.dumps(graph_dict(script), ensure_ascii=False),
        "mode": "edit",
    })
    return templates.TemplateResponse(request, "scripts/editor.html", ctx)


@router.get("/{sid}/graph")
@login_required
async def graph_get(request: Request, sid: int, db: Session = Depends(get_db)):
    script = db.get(Script, sid)
    if not script:
        return _json_err("Скрипт не найден", 404)
    return JSONResponse({"ok": True, **graph_dict(script)})


@router.post("/{sid}/graph")
@role_required("sales")
async def graph_save(request: Request, sid: int, db: Session = Depends(get_db)):
    """Автосохранение конструктора: принимает скрипт целиком, возвращает id новых блоков."""
    if not _can_edit(request):
        return _json_err("Недостаточно прав", 403)
    script = db.get(Script, sid)
    if not script:
        return _json_err("Скрипт не найден", 404)
    try:
        payload = await request.json()
    except Exception:
        return _json_err("Некорректный JSON")
    if not isinstance(payload, dict):
        return _json_err("Некорректный формат данных")

    user_id = request.session.get("user_id")
    try:
        _snapshot(db, script, user_id, "Автосохранение", throttle=True)
        id_map = apply_graph(db, script, payload, user_id)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("Сохранение скрипта %s: %s", sid, e)
        return _json_err("Не удалось сохранить изменения", 500)
    return JSONResponse({
        "ok": True, "id_map": id_map,
        "saved_at": script.updated_at.strftime("%H:%M:%S") if script.updated_at else "",
    })


# ── История версий ───────────────────────────────────────────────────────────

@router.get("/{sid}/history", response_class=HTMLResponse)
@login_required
async def history(request: Request, sid: int, db: Session = Depends(get_db)):
    script = db.get(Script, sid)
    if not script:
        return RedirectResponse(url="/scripts/", status_code=302)
    versions = (db.query(ScriptVersion).filter(ScriptVersion.script_id == sid)
                .order_by(ScriptVersion.created_at.desc()).limit(100).all())
    counts = {}
    for v in versions:
        try:
            counts[v.id] = len(json.loads(v.data_json).get("nodes") or [])
        except (ValueError, TypeError):
            counts[v.id] = 0
    ctx = _base_ctx(request, db)
    ctx.update({"script": script, "versions": versions, "counts": counts})
    return templates.TemplateResponse(request, "scripts/history.html", ctx)


@router.post("/{sid}/versions/{vid}/restore")
@role_required("sales")
async def version_restore(request: Request, sid: int, vid: int, db: Session = Depends(get_db)):
    if not _can_edit(request):
        return _deny()
    script = db.get(Script, sid)
    version = db.get(ScriptVersion, vid)
    if not script or not version or version.script_id != sid:
        return RedirectResponse(url=f"/scripts/{sid}/history", status_code=302)
    user_id = request.session.get("user_id")
    try:
        data = json.loads(version.data_json)
        # текущее состояние сначала в историю — откат тоже должен быть обратим
        _snapshot(db, script, user_id, "Перед откатом")
        apply_graph(db, script, data, user_id)
        db.add(ScriptVersion(
            script_id=sid, data_json=version.data_json, author_id=user_id,
            note=f"Откат к версии от {version.created_at:%d.%m.%Y %H:%M}"
                 if version.created_at else "Откат к версии"))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("Откат скрипта %s к версии %s: %s", sid, vid, e)
    return RedirectResponse(url=f"/scripts/{sid}/edit", status_code=302)


# ── Режим 2: полный скрипт ───────────────────────────────────────────────────

@router.get("/{sid}/full", response_class=HTMLResponse)
@login_required
async def full_view(request: Request, sid: int, db: Session = Depends(get_db)):
    script = db.get(Script, sid)
    if not script:
        return RedirectResponse(url="/scripts/", status_code=302)
    nodes = ordered_nodes(script)
    numbers = {n.id: i + 1 for i, n in enumerate(nodes)}
    ctx = _base_ctx(request, db)
    crm = await asyncio.to_thread(crm_context_for, request, db)
    ctx.update({
        "script": script,
        "nodes": nodes,
        "numbers": numbers,
        "crm": crm,
        "render": lambda html: render_placeholders(html, crm),
        "mode": "full",
        "quick_jumps": _quick_jump_pairs(script, {n.id: n for n in nodes}),
    })
    return templates.TemplateResponse(request, "scripts/full.html", ctx)


# ── Режим 3: прохождение ─────────────────────────────────────────────────────

@router.get("/{sid}/run", response_class=HTMLResponse)
@login_required
async def run_view(request: Request, sid: int, node: str = "",
                   db: Session = Depends(get_db)):
    script = db.get(Script, sid)
    if not script:
        return RedirectResponse(url="/scripts/", status_code=302)
    nodes = ordered_nodes(script)
    if not nodes:
        return RedirectResponse(url=f"/scripts/{sid}/edit", status_code=302)
    numbers = {n.id: i + 1 for i, n in enumerate(nodes)}
    by_id = {n.id: n for n in nodes}

    node_id = int(node) if node.isdigit() else None
    current = by_id.get(node_id) or by_id.get(script.start_node_id) or nodes[0]

    if not node_id:      # счётчик использований — только на старте прохождения
        script.uses_count = (script.uses_count or 0) + 1
        db.commit()

    crm = await asyncio.to_thread(crm_context_for, request, db)
    ctx = _base_ctx(request, db)
    ctx.update({
        "script": script,
        "nodes": nodes,
        "numbers": numbers,
        "current": current,
        "current_html": render_placeholders(current.body_html or "", crm),
        "current_title": render_placeholders(current.title, crm),
        "current_fields": _fields_of(current),
        "targets": {a.id: by_id.get(a.next_node_id) for a in current.answers},
        "crm": crm,
        "crm_query": _crm_query(request),
        "quick_jumps": _quick_jump_pairs(script, by_id),
        "search_index": [
            {"id": n.id, "n": numbers[n.id], "title": n.title,
             "text": html_to_text(n.body_html)[:400]}
            for n in nodes
        ],
        "mode": "run",
    })
    return templates.TemplateResponse(request, "scripts/run.html", ctx)


def _crm_query(request: Request) -> str:
    """Хвост query-параметров CRM — чтобы контекст клиента не терялся при переходах."""
    from urllib.parse import urlencode
    q = request.query_params

    # Есть идентификатор карточки — этого достаточно: имя и телефон подтянутся
    # заново. Тащить их через каждый переход значило бы светить персональные
    # данные клиента в адресной строке, истории браузера и логах nginx.
    if q.get("crm_entity") and q.get("crm_id"):
        pairs = [(k, q[k]) for k in ("crm_entity", "crm_id", "portal") if q.get(k)]
        return ("&" + urlencode(pairs)) if pairs else ""

    keep = ("name", "last_name", "company", "phone", "email", "post", "deal", "amount", "portal")
    pairs = [(k, q[k]) for k in keep if q.get(k)]
    return ("&" + urlencode(pairs)) if pairs else ""


@router.post("/{sid}/run/save")
@login_required
async def run_save(request: Request, sid: int, db: Session = Depends(get_db)):
    """Сохраняет заполненные менеджером поля и пройденный путь.

    Значения всегда складываются в script_runs. Если скрипт открыт из карточки
    Bitrix24 и у полей задан код CRM, по завершении разговора они уезжают в
    карточку (см. app/services/bitrix_actions.py), а рядом пишется, чем это
    закончилось — иначе неудачная запись прошла бы незамеченной."""
    script = db.get(Script, sid)
    if not script:
        return _json_err("Скрипт не найден", 404)
    try:
        payload = await request.json()
    except Exception:
        return _json_err("Некорректный JSON")

    run_id = payload.get("run_id")
    run = db.get(ScriptRun, run_id) if isinstance(run_id, int) else None
    if run is None or run.script_id != sid:
        run = ScriptRun(script_id=sid, user_id=request.session.get("user_id"))
        db.add(run)
    run.values_json = json.dumps(payload.get("values") or {}, ensure_ascii=False)
    run.path_json = json.dumps(payload.get("path") or [], ensure_ascii=False)
    run.result = (payload.get("result") or "")[:50] or None
    run.crm_entity_type = (payload.get("crm_entity") or "")[:20] or None
    run.crm_entity_id = (payload.get("crm_id") or "")[:30] or None
    finished = bool(payload.get("finished"))
    if finished:
        run.finished_at = msk_now()
    db.commit()

    crm = None
    if finished:
        crm = await asyncio.to_thread(_push_run_to_crm, db, run, script)
    return JSONResponse({"ok": True, "run_id": run.id, "crm": crm})


def _push_run_to_crm(db: Session, run: ScriptRun, script: Script) -> dict | None:
    """Пишет собранные значения в карточку CRM и оставляет комментарий в таймлайне.

    Вызывается только по завершении разговора: писать в карточку на каждом шаге
    значило бы дёргать портал десятки раз за звонок и подсовывать ему
    промежуточные, ещё не уточнённые значения.

    Ошибку портала наружу не пробрасываем — менеджер уже положил трубку, и
    падение здесь ничего не спасёт. Результат осел в script_runs, а причина —
    в логе и в ответе, который видит браузер.
    """
    if not run.crm_entity_type or not run.crm_entity_id:
        return None

    from app.models import CompanySettings
    from app.services.bitrix_client import get_bitrix_client
    from app.services import bitrix_actions

    company = db.query(CompanySettings).first()
    client = get_bitrix_client(company)
    if not client:
        return {"ok": False, "message": "Bitrix24 не настроен"}

    try:
        values = json.loads(run.values_json or "{}")
    except (ValueError, TypeError):
        values = {}
    fields = bitrix_actions.fields_from_run_values(values)

    messages = []
    ok = True
    try:
        with client:
            if fields:
                sent, msg = bitrix_actions.update_entity_fields(
                    client, run.crm_entity_type, run.crm_entity_id, fields)
                ok = ok and sent
                messages.append(msg)

            comment = _run_comment(run, script, values)
            if comment:
                sent, msg = bitrix_actions.add_timeline_comment(
                    client, run.crm_entity_type, run.crm_entity_id, comment)
                ok = ok and sent
                messages.append(msg)
    except Exception as e:
        logger.error("Bitrix24: запись итогов прохождения %s: %s", run.id, e)
        return {"ok": False, "message": str(e)[:200]}

    result = {"ok": ok, "message": "; ".join(m for m in messages if m)}
    run.crm_pushed_at = msk_now()
    run.crm_push_result = result["message"][:500] if result["message"] else None
    db.commit()
    return result


# Как называется итог разговора в комментарии CRM
_RUN_RESULTS = {
    "success": "договорились",
    "callback": "перезвонить",
    "refused": "отказ",
}


def _run_comment(run: ScriptRun, script: Script, values: dict) -> str:
    """Текст комментария в таймлайн: скрипт, итог и что заполнил менеджер."""
    lines = [f"Скрипт «{script.title}»"]
    if run.result:
        lines.append(f"Итог: {_RUN_RESULTS.get(run.result, run.result)}")
    for item in (values or {}).values():
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if value in (None, "", False):
            continue
        label = (item.get("label") or item.get("crm_field") or "Поле").strip()
        lines.append(f"{label}: {value}")
    return "\n".join(lines) if len(lines) > 1 else ""


# ── Bitrix24: скрипт внутри карточки CRM ─────────────────────────────────────
#
# Bitrix24 открывает встроенное приложение POST-запросом в iframe и передаёт
# PLACEMENT_OPTIONS с типом и id сущности (сделка/лид/контакт/компания). Отсюда
# берём контекст клиента и подставляем его в текст скрипта — менеджеру не нужно
# уходить из карточки и заново искать имя и телефон.
#
# Что нужно настроить на стороне портала (Настройки → Интеграции → Bitrix24):
#   placement  CRM_DEAL_DETAIL_TAB (или CRM_LEAD_DETAIL_TAB)
#   handler    https://<адрес TMS>/scripts/b24/placement
# Cookie сессии в iframe требуют SameSite=None; Secure — TMS должен работать
# по HTTPS (за nginx он и работает).

_B24_ENTITIES = {
    "deal": ("crm.deal.get", "CRM_DEAL_DETAIL_TAB"),
    "lead": ("crm.lead.get", "CRM_LEAD_DETAIL_TAB"),
    "contact": ("crm.contact.get", "CRM_CONTACT_DETAIL_TAB"),
    "company": ("crm.company.get", "CRM_COMPANY_DETAIL_TAB"),
}


def _b24_entity_from_placement(placement: str, options: dict):
    """(тип сущности, id) из PLACEMENT/PLACEMENT_OPTIONS."""
    entity = "deal"
    for key, (_, tab) in _B24_ENTITIES.items():
        if (placement or "").upper().startswith(tab):
            entity = key
            break
    entity_id = options.get("ID") or options.get("ENTITY_VALUE_ID") or options.get("entityId")
    return entity, str(entity_id) if entity_id else ""


def _b24_client_context(db: Session, entity: str, entity_id: str) -> dict:
    """Тянет из Bitrix24 имя, компанию, телефон и e-mail для подстановок.

    Ошибку интеграции глотаем намеренно: скрипт должен открыться и без данных —
    менеджер уже на линии, и пустое имя лучше, чем страница с ошибкой."""
    if not entity_id:
        return {}
    from app.models import CompanySettings
    from app.services.bitrix_client import (
        get_bitrix_client, _contact_full_name, _first_multifield,
    )
    company = db.query(CompanySettings).first()
    client = get_bitrix_client(company)
    if not client:
        return {}
    method = _B24_ENTITIES.get(entity, _B24_ENTITIES["deal"])[0]
    ctx = {}
    try:
        with client:
            data = client.call(method, id=entity_id) or {}
            if entity in ("contact", "lead"):
                ctx["name"] = data.get("NAME") or _contact_full_name(data)
                ctx["last_name"] = data.get("LAST_NAME") or ""
                ctx["company"] = data.get("COMPANY_TITLE") or ""
                ctx["phone"] = _first_multifield(data, "PHONE")
                ctx["email"] = _first_multifield(data, "EMAIL")
                ctx["post"] = data.get("POST") or ""
            elif entity == "company":
                ctx["company"] = data.get("TITLE") or ""
                ctx["phone"] = _first_multifield(data, "PHONE")
                ctx["email"] = _first_multifield(data, "EMAIL")
            else:   # сделка: имя и телефон — у привязанного контакта
                ctx["deal"] = data.get("TITLE") or ""
                ctx["amount"] = data.get("OPPORTUNITY") or ""
                if data.get("CONTACT_ID"):
                    contact = client.get_contact(data["CONTACT_ID"]) or {}
                    ctx["name"] = contact.get("NAME") or _contact_full_name(contact)
                    ctx["last_name"] = contact.get("LAST_NAME") or ""
                    ctx["phone"] = _first_multifield(contact, "PHONE")
                    ctx["email"] = _first_multifield(contact, "EMAIL")
                if data.get("COMPANY_ID"):
                    company_data = client.get_company(data["COMPANY_ID"]) or {}
                    ctx["company"] = company_data.get("TITLE") or ""
                    if not ctx.get("phone"):
                        ctx["phone"] = _first_multifield(company_data, "PHONE")
    except Exception as e:
        logger.warning("Bitrix24: не удалось получить %s %s: %s", entity, entity_id, e)
        return {}
    return {k: v for k, v in ctx.items() if v}


@router.api_route("/b24/placement", methods=["GET", "POST"], response_class=HTMLResponse)
async def b24_placement(request: Request, db: Session = Depends(get_db)):
    """Список скриптов внутри карточки Bitrix24 с контекстом клиента.

    Намеренно без login_required. Bitrix24 открывает виджет POST-запросом из
    портала, и CSRF-токена в нём нет — взяться ему неоткуда, запрос приходит с
    чужого домена. Декоратор отвергал бы каждое открытие карточки с «неверный
    CSRF-токен». Страница ничего не меняет, только показывает список, поэтому
    вход проверяем вручную, а защита от подделки запросов ей не нужна.
    """
    from app.auth import get_current_user
    if not get_current_user(request):
        # Внутри рамки форма входа бесполезна: cookie сессии в чужом контексте
        # ставится не всегда, да и логиниться в узкой вкладке неудобно
        return HTMLResponse(
            '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<title>NERPA</title></head>'
            '<body style="font-family:sans-serif;padding:24px;text-align:center">'
            '<p style="color:#64748b">Чтобы увидеть скрипты, войдите в NERPA.</p>'
            '<p><a href="/auth/login" target="_blank" rel="noopener">Открыть вход в новой вкладке</a></p>'
            '<p style="color:#94a3b8;font-size:.85rem">После входа обновите карточку.</p>'
            '</body></html>', status_code=200)
    placement, options = "", {}
    if request.method == "POST":
        form = await request.form()
        placement = str(form.get("PLACEMENT") or "")
        try:
            options = json.loads(str(form.get("PLACEMENT_OPTIONS") or "{}"))
        except (ValueError, TypeError):
            options = {}
    else:
        placement = request.query_params.get("PLACEMENT", "")
        options = {"ID": request.query_params.get("crm_id", "")}

    entity, entity_id = _b24_entity_from_placement(placement, options)
    # Сетевой вызов к порталу — только в потоке, иначе он заморозит
    # событийный цикл и сайт перестанет отвечать всем остальным
    crm = await asyncio.to_thread(_b24_client_context, db, entity, entity_id)

    from urllib.parse import urlencode
    params = dict(crm)
    params.update({"crm_entity": entity, "crm_id": entity_id})
    query = urlencode({k: v for k, v in params.items() if v})

    scripts = (db.query(Script).filter(Script.status == "active")
               .order_by(Script.updated_at.desc()).all())
    if not scripts:
        scripts = db.query(Script).order_by(Script.updated_at.desc()).limit(20).all()

    ctx = _base_ctx(request, db)
    ctx.update({"scripts": scripts, "crm": crm, "query": query,
                "entity": entity, "entity_id": entity_id})
    return templates.TemplateResponse(request, "scripts/b24.html", ctx)


# ── Экспорт в Word ───────────────────────────────────────────────────────────

@router.get("/{sid}/export.docx")
@login_required
async def export_docx(request: Request, sid: int, mode: str = "full",
                      db: Session = Depends(get_db)):
    script = db.get(Script, sid)
    if not script:
        return RedirectResponse(url="/scripts/", status_code=302)
    nodes = ordered_nodes(script)
    for n in nodes:
        n._fields = _fields_of(n)
    data = build_script_docx(script, nodes,
                             mode="full" if mode != "plain" else "plain",
                             ctx=crm_context(request))
    safe = "".join(c for c in (script.title or "script")
                   if c.isalnum() or c in " -_()").strip() or "script"
    suffix = "полный" if mode != "plain" else "текст"
    from urllib.parse import quote
    filename = quote(f"{safe} ({suffix}).docx")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )

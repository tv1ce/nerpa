"""Тренажёр скриптов: разговор с «клиентом», которого играет модель.

Ход тренировки состоит из двух запросов, и это не случайно:

    /say    — новичок произнёс реплику шага, клиент отвечает своими словами;
              сервер запоминает, какую ветку модель имела в виду, и отдаёт
              браузеру только перемешанные варианты;
    /choose — новичок решил, к какой ветке относится услышанное; сервер
              сверяет с запомненным и ведёт разговор дальше.

Ожидаемая ветка не покидает сервер до момента сверки — иначе тренажёр
проходится через инструменты разработчика, а не головой.

Разговор всегда продолжается по той ветке, которую имел в виду клиент, даже
если новичок её не угадал. В жизни менеджер бы ушёл не туда и разговор
развалился, но учебная ценность выше, когда сценарий доигрывается до конца:
ошибка отмечается и попадает в разбор, а человек видит весь путь.
"""
import json
import logging
import random

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import login_required
from app.database import get_db
from app.models import Script, ScriptNode, ScriptTraining
from app.tz import now as msk_now
from app.services import script_trainer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scripts", tags=["script-training"])
templates = Jinja2Templates(directory="app/templates")

# Предохранитель от зацикливания: в сценарии бывают петли «вернуться к вопросу»,
# и без ограничения тренировка может не закончиться никогда.
MAX_TURNS = 30


def _json_err(msg: str, code: int = 400):
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


def _load_log(training: ScriptTraining) -> list:
    try:
        data = json.loads(training.log_json or "[]")
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _save_log(training: ScriptTraining, log: list) -> None:
    training.log_json = json.dumps(log, ensure_ascii=False)


def _node_payload(node: ScriptNode, numbers: dict) -> dict:
    return {
        "id": node.id,
        "number": numbers.get(node.id),
        "title": node.title,
        "html": node.body_html or "",
        "kind": node.kind or "question",
        "has_answers": bool(node.answers),
    }


def _numbers(script: Script) -> dict:
    from app.routers.scripts import ordered_nodes
    return {n.id: i + 1 for i, n in enumerate(ordered_nodes(script))}


def _training_of(db: Session, request: Request, sid: int, training_id) -> ScriptTraining | None:
    """Своя попытка по этому скрипту — чужую подсунуть нельзя."""
    if not isinstance(training_id, int):
        return None
    training = db.get(ScriptTraining, training_id)
    if not training or training.script_id != sid:
        return None
    if training.user_id and training.user_id != request.session.get("user_id"):
        return None
    return training


# ── Страница ─────────────────────────────────────────────────────────────────

@router.get("/{sid}/train", response_class=HTMLResponse)
@login_required
async def train_page(request: Request, sid: int, db: Session = Depends(get_db)):
    script = db.get(Script, sid)
    if not script:
        return RedirectResponse(url="/scripts/", status_code=302)

    history = (db.query(ScriptTraining)
               .filter(ScriptTraining.script_id == sid,
                       ScriptTraining.user_id == request.session.get("user_id"),
                       ScriptTraining.finished_at.isnot(None))
               .order_by(ScriptTraining.finished_at.desc()).limit(10).all())

    from app.routers.scripts import _base_ctx
    ctx = _base_ctx(request, db)
    ctx.update({
        "script": script,
        "mode": "train",
        "personas": script_trainer.PERSONAS,
        "difficulties": script_trainer.DIFFICULTIES,
        "ai_available": script_trainer.is_available(),
        "history": history,
    })
    return templates.TemplateResponse(request, "scripts/train.html", ctx)


# ── Ход тренировки ───────────────────────────────────────────────────────────

@router.post("/{sid}/train/start")
@login_required
async def train_start(request: Request, sid: int, db: Session = Depends(get_db)):
    script = db.get(Script, sid)
    if not script:
        return _json_err("Скрипт не найден", 404)
    from app.routers.scripts import ordered_nodes
    nodes = ordered_nodes(script)
    if not nodes:
        return _json_err("В скрипте нет шагов")

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    persona = script_trainer.pick_persona(str(payload.get("persona") or ""))
    difficulty = str(payload.get("difficulty") or "normal")
    if difficulty not in script_trainer.DIFFICULTIES:
        difficulty = "normal"

    training = ScriptTraining(
        script_id=sid, user_id=request.session.get("user_id"),
        persona=persona["title"], difficulty=difficulty,
        turns_total=0, turns_correct=0, log_json="[]",
    )
    db.add(training)
    db.commit()

    start = next((n for n in nodes if n.id == script.start_node_id), nodes[0])
    return JSONResponse({
        "ok": True,
        "training_id": training.id,
        "persona": persona,
        "difficulty": difficulty,
        "ai": script_trainer.is_available(),
        "node": _node_payload(start, _numbers(script)),
    })


@router.post("/{sid}/train/say")
@login_required
async def train_say(request: Request, sid: int, db: Session = Depends(get_db)):
    """Новичок произнёс реплику шага — клиент отвечает."""
    script = db.get(Script, sid)
    if not script:
        return _json_err("Скрипт не найден", 404)
    try:
        payload = await request.json()
    except Exception:
        return _json_err("Некорректный JSON")

    training = _training_of(db, request, sid, payload.get("training_id"))
    if not training:
        return _json_err("Тренировка не найдена", 404)
    if training.finished_at:
        return _json_err("Тренировка уже завершена")

    node = db.get(ScriptNode, payload.get("node_id") or 0)
    if not node or node.script_id != sid:
        return _json_err("Шаг не найден", 404)

    answers = [{"id": a.id, "text": a.text} for a in node.answers]
    if not answers:
        return JSONResponse({"ok": True, "done": True, "client_reply": "", "options": []})

    log = _load_log(training)
    persona = script_trainer.pick_persona(
        next((p["key"] for p in script_trainer.PERSONAS if p["title"] == training.persona), ""))
    turn = script_trainer.client_turn(
        script.title, script_trainer.node_prompt_text(node), answers,
        persona, training.difficulty, log)

    # Ожидаемая ветка остаётся на сервере — в браузер уходят только варианты
    log.append({
        "node_id": node.id,
        "manager": script_trainer.node_prompt_text(node)[:600],
        "client": turn["reply"],
        "expected_id": turn["answer_id"],
        "expected_text": next((a["text"] for a in answers if a["id"] == turn["answer_id"]), ""),
        "mood": turn.get("mood"),
        "improvised": not turn.get("fallback"),
        "pending": True,
    })
    _save_log(training, log)
    db.commit()

    options = answers[:]
    random.shuffle(options)      # порядок из схемы подсказал бы правильный ответ
    return JSONResponse({
        "ok": True,
        "client_reply": turn["reply"],
        "mood": turn.get("mood", "neutral"),
        "improvised": not turn.get("fallback"),
        "options": options,
    })


@router.post("/{sid}/train/choose")
@login_required
async def train_choose(request: Request, sid: int, db: Session = Depends(get_db)):
    """Новичок выбрал ветку — сверяем и идём дальше."""
    script = db.get(Script, sid)
    if not script:
        return _json_err("Скрипт не найден", 404)
    try:
        payload = await request.json()
    except Exception:
        return _json_err("Некорректный JSON")

    training = _training_of(db, request, sid, payload.get("training_id"))
    if not training:
        return _json_err("Тренировка не найдена", 404)

    log = _load_log(training)
    if not log or not log[-1].get("pending"):
        return _json_err("Нет хода, который нужно оценить")

    turn = log[-1]
    chosen_id = payload.get("answer_id")
    chosen_id = int(chosen_id) if isinstance(chosen_id, int) else None

    node = db.get(ScriptNode, turn["node_id"])
    answers = {a.id: a for a in (node.answers if node else [])}
    expected_id = turn.get("expected_id")
    correct = bool(chosen_id and chosen_id == expected_id)

    turn.update({
        "pending": False,
        "chosen_id": chosen_id,
        "chosen_text": answers[chosen_id].text if chosen_id in answers else "—",
        "correct": correct,
    })
    training.turns_total = (training.turns_total or 0) + 1
    if correct:
        training.turns_correct = (training.turns_correct or 0) + 1
    _save_log(training, log)

    # Разговор идёт туда, куда ведёт ветка клиента, а не догадка новичка
    expected = answers.get(expected_id)
    next_node = db.get(ScriptNode, expected.next_node_id) if expected and expected.next_node_id else None
    done = next_node is None or (next_node.kind == "end" and not next_node.answers) \
        or training.turns_total >= MAX_TURNS

    db.commit()
    return JSONResponse({
        "ok": True,
        "correct": correct,
        "expected_id": expected_id,
        "expected_text": turn.get("expected_text") or "",
        "next": _node_payload(next_node, _numbers(script)) if next_node else None,
        "done": done,
        "turns_total": training.turns_total,
        "turns_correct": training.turns_correct,
    })


@router.post("/{sid}/train/finish")
@login_required
async def train_finish(request: Request, sid: int, db: Session = Depends(get_db)):
    """Итог попытки и разбор от модели."""
    script = db.get(Script, sid)
    if not script:
        return _json_err("Скрипт не найден", 404)
    try:
        payload = await request.json()
    except Exception:
        return _json_err("Некорректный JSON")

    training = _training_of(db, request, sid, payload.get("training_id"))
    if not training:
        return _json_err("Тренировка не найдена", 404)

    log = [t for t in _load_log(training) if not t.get("pending")]
    total = training.turns_total or 0
    correct = training.turns_correct or 0
    training.score = int(round(correct * 100 / total)) if total else 0
    training.finished_at = msk_now()

    persona = script_trainer.pick_persona(
        next((p["key"] for p in script_trainer.PERSONAS if p["title"] == training.persona), ""))
    training.verdict = script_trainer.review(
        script.title, persona, training.difficulty, log, correct, total)
    db.commit()

    script.uses_count = script.uses_count or 0     # тренировки не считаем за звонки
    db.commit()
    return JSONResponse({
        "ok": True,
        "score": training.score,
        "correct": correct,
        "total": total,
        "verdict": training.verdict or "",
    })

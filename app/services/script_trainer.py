"""Тренажёр скриптов: модель играет клиента, новичок ведёт разговор.

Зачем так. Обычное прохождение скрипта — это чтение готового текста и нажатие
кнопки с подходящим ответом, и на нём невозможно научиться главному: живой
клиент не говорит «Частично» словом «Частично». Он говорит «ну, у нас вроде
что-то такое есть, но нерегулярно». Поэтому тренажёр устроен наоборот:

  1. новичок видит реплику скрипта и произносит её;
  2. модель отвечает как настоящий клиент — своими словами, с характером;
  3. новичок сам решает, к какой ветке скрипта относится этот ответ;
  4. система сверяет с тем, какую ветку модель имела в виду.

Ключевая деталь: ожидаемая ветка наружу не отдаётся, иначе тренажёр превращается
в игру «посмотри в отладчике браузера». Сверка целиком на сервере.

Работает и без модели. Если ключ OpenRouter не задан или сервис недоступен,
тренажёр деградирует до простого режима: «клиент» отвечает формулировкой самой
ветки. Учебной ценности меньше, но новичок хотя бы отрабатывает переходы, а не
упирается в неработающий раздел.
"""
import json
import logging
import random

from app.services import openrouter_client
from app.utils.script_text import html_to_text

logger = logging.getLogger(__name__)

# Кого играет модель. Список намеренно из реальных типажей отдела продаж —
# абстрактный «клиент» даёт пресные реплики, по которым учиться нечему.
PERSONAS = [
    {"key": "owner", "title": "Владелец кофейни",
     "brief": "Сам варит кофе и считает каждый рубль. Занят, отвечает коротко, "
              "по делу, легко раздражается на длинные заходы."},
    {"key": "buyer", "title": "Закупщик сети",
     "brief": "Профессиональный переговорщик. Спрашивает про объёмы, отсрочку и "
              "условия возврата. Давит на цену, ссылается на текущего поставщика."},
    {"key": "skeptic", "title": "Скептик",
     "brief": "Уже обжигался на поставщиках. Сомневается в качестве и сроках, "
              "требует конкретики и доказательств, не верит общим словам."},
    {"key": "hurry", "title": "Вечно занятой",
     "brief": "Постоянно куда-то бежит. Просит перезвонить, прислать на почту, "
              "отвечает односложно. Разговор нужно удерживать."},
    {"key": "friendly", "title": "Доброжелательный новичок",
     "brief": "Открыт к разговору, недавно открыл точку, плохо знает рынок. "
              "Много спрашивает, соглашается, но легко срывается на «я подумаю»."},
]

DIFFICULTIES = {
    "easy": {
        "title": "Лёгкий",
        "hint": "Отвечай близко к формулировке выбранной ветки, без лишнего. "
                "Ответ должен легко узнаваться.",
    },
    "normal": {
        "title": "Обычный",
        "hint": "Отвечай своими словами, как говорят живые люди: с оговорками, "
                "разговорными оборотами. Смысл ветки сохраняй.",
    },
    "hard": {
        "title": "Сложный",
        "hint": "Отвечай развёрнуто и уводи в сторону: добавляй встречные вопросы, "
                "сомнения, посторонние детали. Смысл выбранной ветки должен "
                "угадываться, но не лежать на поверхности.",
    },
}

_SYSTEM = (
    "Ты — тренажёр для обучения менеджеров по продажам. Ты играешь роль клиента "
    "в телефонном разговоре. Отвечай только от лица клиента, живой разговорной "
    "речью, одной-тремя фразами. Не пиши пояснений, не подсказывай менеджеру, "
    "не упоминай, что ты модель или что идёт тренировка."
)


def is_available() -> bool:
    """Настроен ли доступ к модели."""
    return bool(openrouter_client.API_KEY)


def pick_persona(key: str = "") -> dict:
    for p in PERSONAS:
        if p["key"] == key:
            return p
    return random.choice(PERSONAS)


def _history_text(history: list, limit: int = 6) -> str:
    """Последние ходы разговора — чтобы клиент помнил, о чём уже говорили."""
    lines = []
    for turn in (history or [])[-limit:]:
        if turn.get("manager"):
            lines.append(f"Менеджер: {turn['manager']}")
        if turn.get("client"):
            lines.append(f"Клиент: {turn['client']}")
    return "\n".join(lines)


def client_turn(script_title: str, node_text: str, answers: list,
                persona: dict, difficulty: str, history: list) -> dict:
    """Реплика клиента и ветка, которую модель имела в виду.

    answers — [{id, text}]. Возвращает {reply, answer_id, mood}. Если модель
    недоступна или ответила мусором, ветка выбирается случайно, а репликой
    становится её текст: тренажёр продолжает работать, просто без импровизации.
    """
    allowed = [a for a in answers if a.get("id")]
    if not allowed:
        return {"reply": "", "answer_id": None, "mood": "neutral", "fallback": True}

    chosen = random.choice(allowed)
    fallback = {"reply": chosen["text"], "answer_id": chosen["id"],
                "mood": "neutral", "fallback": True}
    if not is_available():
        return fallback

    level = DIFFICULTIES.get(difficulty) or DIFFICULTIES["normal"]
    options = "\n".join(f"{a['id']}. {a['text']}" for a in allowed)
    prompt = (
        f"Скрипт разговора: «{script_title}».\n"
        f"Ты играешь клиента: {persona['title']}. {persona['brief']}\n"
        f"Манера ответа: {level['hint']}\n\n"
        f"{('Что уже прозвучало:' + chr(10) + _history_text(history) + chr(10) + chr(10)) if history else ''}"
        f"Менеджер сейчас говорит:\n{node_text}\n\n"
        "У сценария есть заранее заданные варианты реакции клиента:\n"
        f"{options}\n\n"
        "Выбери ОДИН вариант, который отыграешь, и произнеси его живой речью — "
        "так, как сказал бы настоящий человек, не повторяя формулировку дословно.\n"
        "Ответ верни строго в JSON: "
        '{"answer_id": <число из списка>, "reply": "<реплика клиента>", '
        '"mood": "positive|neutral|negative"}'
    )

    try:
        data = openrouter_client.chat_json(_SYSTEM, prompt)
        answer_id = int(data.get("answer_id"))
        reply = str(data.get("reply") or "").strip()
        if not reply or answer_id not in {a["id"] for a in allowed}:
            raise ValueError("модель вернула ветку вне списка")
        mood = str(data.get("mood") or "neutral")
        return {"reply": reply, "answer_id": answer_id,
                "mood": mood if mood in ("positive", "neutral", "negative") else "neutral",
                "fallback": False}
    except Exception as e:
        logger.warning("Тренажёр: реплика клиента не получена (%s) — играем по сценарию", e)
        return fallback


def review(script_title: str, persona: dict, difficulty: str, log: list,
           correct: int, total: int) -> str:
    """Короткий разбор попытки: что получилось, где ошибся, что повторить."""
    if not log:
        return ""
    if not is_available():
        # Без модели даём честную арифметику вместо выдуманного анализа
        if not total:
            return "Разговор не состоялся — ни одного хода не пройдено."
        return (f"Верно распознано веток: {correct} из {total}. "
                "Разбор от ИИ недоступен — не настроен доступ к модели.")

    lines = []
    for i, turn in enumerate(log, start=1):
        lines.append(
            f"{i}. Менеджер: {turn.get('manager', '')[:300]}\n"
            f"   Клиент: {turn.get('client', '')}\n"
            f"   Менеджер понял это как: {turn.get('chosen_text', '—')}\n"
            f"   На самом деле клиент имел в виду: {turn.get('expected_text', '—')}"
            + ("" if turn.get("correct") else "   ← ошибка")
        )

    prompt = (
        f"Скрипт: «{script_title}». Клиент: {persona['title']}. "
        f"Сложность: {DIFFICULTIES.get(difficulty, {}).get('title', difficulty)}.\n"
        f"Верных распознаваний: {correct} из {total}.\n\n"
        "Ход тренировки:\n" + "\n".join(lines) + "\n\n"
        "Разбери эту тренировку для новичка отдела продаж. Коротко, по делу, "
        "без похвалы ради похвалы: что он понял правильно, где перепутал реакцию "
        "клиента и почему её легко было принять за другую, что стоит повторить "
        "перед реальными звонками. Максимум пять предложений, обращайся на «ты»."
    )
    try:
        return openrouter_client.chat(
            "Ты — наставник отдела продаж. Пишешь короткие деловые разборы тренировок.",
            prompt, temperature=0.4).strip()
    except Exception as e:
        logger.warning("Тренажёр: разбор не получен — %s", e)
        return (f"Верно распознано веток: {correct} из {total}. "
                "Разбор от ИИ получить не удалось.")


def node_prompt_text(node) -> str:
    """Текст шага для промпта — без разметки, но с сохранением абзацев."""
    text = html_to_text(node.body_html) if node.body_html else ""
    return (text or node.title or "").strip()

"""Прямая отправка сообщений в Telegram Bot API — для вызова из веб-приложения,
где нет живого объекта Bot/Application (в отличие от bot/main.py)."""
from __future__ import annotations

import os
import re

import httpx

BOT_TOKEN = os.getenv("TMS_BOT_TOKEN", "")

# api.telegram.org недоступен напрямую с российских серверов — ходим через тот же
# локальный SOCKS-прокси, что и остальные Telegram-запросы приложения (bot/main.py,
# routers/orders.py notify_carrier).
PROXY = os.getenv("TMS_PROXY", "socks5://127.0.0.1:1080") or None

_MDV2_RESERVED = r"\_*[]()~`>#+-=|{}.!"


def ai_text_to_mdv2(text: str) -> str:
    """Конвертирует свободный текст от ИИ (с **bold**-разметкой) в Telegram MarkdownV2:
    экранирует все спецсимволы вне пар \\*\\*, а сами пары превращает в одинарное
    \\*bold\\* (принятое в MarkdownV2), с экранированием их содержимого."""
    def esc(s: str) -> str:
        return "".join("\\" + ch if ch in _MDV2_RESERVED else ch for ch in s)

    parts = re.split(r"\*\*(.+?)\*\*", text, flags=re.S)
    out = []
    for i, part in enumerate(parts):
        out.append(f"*{esc(part)}*" if i % 2 == 1 else esc(part))
    return "".join(out)


def parse_chat_ids(raw: str) -> list[int]:
    result = []
    for x in raw.split(","):
        x = x.strip()
        if x:
            try:
                result.append(int(x))
            except ValueError:
                pass
    return result


def _split(text: str, limit: int = 3500) -> list[str]:
    """Режет длинный MarkdownV2-текст по границам пустых строк (не рвёт экранирование)."""
    if len(text) <= limit:
        return [text]
    parts, buf, length = [], [], 0
    for para in text.split("\n\n"):
        if length + len(para) + 2 > limit and buf:
            parts.append("\n\n".join(buf))
            buf, length = [], 0
        buf.append(para)
        length += len(para) + 2
    if buf:
        parts.append("\n\n".join(buf))
    return parts


def send_markdown(chat_ids: list[int], text: str, bot_token: str | None = None) -> None:
    token = (bot_token or BOT_TOKEN or "").strip()
    if not token:
        raise RuntimeError("Токен Telegram-бота не задан (TMS_BOT_TOKEN или Настройки → Telegram-бот)")
    for chat_id in chat_ids:
        for chunk in _split(text):
            r = httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "MarkdownV2"},
                timeout=20,
                proxy=PROXY,
            )
            r.raise_for_status()


def send_topic_message(chat_id: int, text: str, bot_token: str | None = None,
                        thread_id: int | str | None = None) -> None:
    """Отправляет обычный (не MarkdownV2) текст в чат — опционально в конкретный
    топик супергруппы-форума (message_thread_id). Используется для простых
    шаблонных уведомлений склада, где не нужно экранирование разметки."""
    token = (bot_token or BOT_TOKEN or "").strip()
    if not token:
        raise RuntimeError("Токен Telegram-бота не задан (TMS_BOT_TOKEN или Настройки → Telegram-бот)")
    for chunk in _split(text):
        payload = {"chat_id": chat_id, "text": chunk}
        if thread_id:
            payload["message_thread_id"] = int(thread_id)
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=20,
            proxy=PROXY,
        )
        r.raise_for_status()


def notify_warehouse_group(db, topic: str, text: str) -> None:
    """Уведомление в складскую Telegram-супергруппу (топики «Поступления сырья» /
    «Собранные заказы» / «Отгрузки»). topic — 'receiving' / 'assembled' / 'shipped'.
    Никогда не бросает исключение наружу — сбой Telegram не должен ломать
    основной складской флоу (приёмку/сборку/отгрузку)."""
    import logging
    logger = logging.getLogger(__name__)
    try:
        from app.models import CompanySettings
        s = db.query(CompanySettings).first()
        if not s or not s.tg_warehouse_enabled or not s.tg_warehouse_chat_id:
            return
        token = (s.tg_bot_token or "").strip() or BOT_TOKEN
        if not token:
            return
        thread_id = {
            "receiving": s.tg_warehouse_topic_receiving,
            "assembled": s.tg_warehouse_topic_assembled,
            "shipped":   s.tg_warehouse_topic_shipped,
        }.get(topic)
        send_topic_message(int(s.tg_warehouse_chat_id), text, bot_token=token, thread_id=thread_id)
    except Exception:
        logger.error("notify_warehouse_group(%s) failed", topic, exc_info=True)


def fetch_recent_topic_updates(bot_token: str, limit: int = 50) -> list[dict]:
    """Диагностика: последние апдейты бота (getUpdates) — чтобы найти chat_id и
    message_thread_id нужных топиков супергруппы, не залезая в API вручную.
    Просит пользователя написать/переслать по одному сообщению в каждый топик,
    затем показывает (chat_id, топик/thread_id, текст, время) — administrator
    сам сопоставляет thread_id с названием топика по содержимому сообщения.
    Возвращает последние записи в обратном хронологическом порядке.
    """
    token = (bot_token or BOT_TOKEN or "").strip()
    if not token:
        raise RuntimeError("Токен Telegram-бота не задан")
    r = httpx.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params={"limit": limit, "timeout": 0},
        timeout=20,
        proxy=PROXY,
    )
    r.raise_for_status()
    data = r.json()
    out = []
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post")
        if not msg or "chat" not in msg:
            continue
        chat = msg["chat"]
        if chat.get("type") not in ("group", "supergroup"):
            continue
        out.append({
            "chat_id": chat.get("id"),
            "chat_title": chat.get("title"),
            "thread_id": msg.get("message_thread_id"),
            "topic_created_name": (msg.get("forum_topic_created") or {}).get("name"),
            "text": (msg.get("text") or "")[:80],
            "date": msg.get("date"),
        })
    out.sort(key=lambda x: x["date"] or 0, reverse=True)
    return out

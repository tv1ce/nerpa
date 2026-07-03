"""Прямая отправка сообщений в Telegram Bot API — для вызова из веб-приложения,
где нет живого объекта Bot/Application (в отличие от bot/main.py)."""
from __future__ import annotations

import os

import httpx

BOT_TOKEN = os.getenv("TMS_BOT_TOKEN", "")


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


def send_markdown(chat_ids: list[int], text: str) -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TMS_BOT_TOKEN не задан в .env")
    for chat_id in chat_ids:
        for chunk in _split(text):
            r = httpx.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "MarkdownV2"},
                timeout=20,
            )
            r.raise_for_status()

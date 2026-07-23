"""Прямая отправка сообщений в Telegram Bot API — для вызова из веб-приложения,
где нет живого объекта Bot/Application (в отличие от bot/main.py)."""
from __future__ import annotations

import os
import re

import httpx

BOT_TOKEN = os.getenv("TMS_BOT_TOKEN", "")

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
            )
            r.raise_for_status()

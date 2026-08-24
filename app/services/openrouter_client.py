"""Простой клиент OpenRouter (chat completions) для анализа HR-отчётности."""
from __future__ import annotations

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

API_KEY = os.getenv("OPENROUTER_API_KEY", "")
MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
API_URL = "https://openrouter.ai/api/v1/chat/completions"

# OpenRouter отдаёт 403 «Access denied by security policy» на прямые запросы с
# российского IP сервера, поэтому ходим через локальный прокси-обход (v2rayA HTTP,
# он же резолвит DNS удалённо — обходит отсутствие IPv6). Пусто = прямое соединение.
PROXY = os.getenv("OPENROUTER_PROXY", "http://127.0.0.1:20171").strip() or None


def chat(system: str, user: str, *, model: str | None = None, temperature: float = 0.2,
         timeout: float = 120) -> str:
    """Отправляет системный+пользовательский промпт в OpenRouter, возвращает текст ответа.

    timeout по умолчанию рассчитан на фоновые отчёты, где ждать не жалко.
    Интерактивным вызовам (тренажёр) нужен короткий: там за ответом сидит
    живой человек, и две минуты ожидания хуже честного «не получилось»."""
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
    with httpx.Client(timeout=timeout, proxy=PROXY) as client:
        r = client.post(API_URL, headers=headers, json=payload)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


def chat_json(system: str, user: str, *, model: str | None = None,
              timeout: float = 120) -> list | dict:
    """Как chat(), но парсит ответ как JSON (снимая ```json ...``` обёртку при необходимости)."""
    raw = chat(system, user, model=model, temperature=0.1, timeout=timeout).strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())

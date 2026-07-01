"""Простой клиент OpenRouter (chat completions) для анализа HR-отчётности."""
from __future__ import annotations

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

API_KEY = os.getenv("OPENROUTER_API_KEY", "")
MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
API_URL = "https://openrouter.ai/api/v1/chat/completions"


def chat(system: str, user: str, *, model: str | None = None, temperature: float = 0.2) -> str:
    """Отправляет системный+пользовательский промпт в OpenRouter, возвращает текст ответа."""
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
    r = httpx.post(API_URL, headers=headers, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


def chat_json(system: str, user: str, *, model: str | None = None) -> list | dict:
    """Как chat(), но парсит ответ как JSON (снимая ```json ...``` обёртку при необходимости)."""
    raw = chat(system, user, model=model, temperature=0.1).strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())

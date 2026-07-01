"""
Клиент Teamly API — авторизация (OAuth-подобный флоу) и чтение статей вики.

Переменные окружения (.env):
    TEAMLY_SLUG              — поддомен рабочего аккаунта (nuttinelle.teamly.ru)
    TEAMLY_CLUSTER_DOMAIN    — домен кластера для API-запросов (после авторизации)
    TEAMLY_CLIENT_ID         — идентификатор интеграции
    TEAMLY_CLIENT_SECRET     — секретный ключ интеграции
    TEAMLY_REDIRECT_URI      — redirect_uri, указанный при создании интеграции

Токены (access/refresh) хранятся отдельно в teamly_tokens.json (не в .env),
т.к. access_token живёт ~2 дня и требует автообновления по refresh_token
(рефреш действует 2 недели — см. документацию Teamly).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

TOKENS_FILE = Path(__file__).resolve().parents[2] / "teamly_tokens.json"

SLUG = os.getenv("TEAMLY_SLUG", "")
CLUSTER_DOMAIN = os.getenv("TEAMLY_CLUSTER_DOMAIN", "https://app.teamly.ru")
CLIENT_ID = os.getenv("TEAMLY_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("TEAMLY_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("TEAMLY_REDIRECT_URI", "")

# Обновляем access_token заранее, не дожидаясь фактического истечения
_REFRESH_MARGIN_SECONDS = 300


class TeamlyAuthError(RuntimeError):
    pass


def _load_tokens() -> dict:
    if not TOKENS_FILE.exists():
        raise TeamlyAuthError(
            f"Файл токенов {TOKENS_FILE} не найден. "
            "Нужно один раз пройти авторизацию (client_id/client_secret/code) "
            "и сохранить access_token/refresh_token."
        )
    with open(TOKENS_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save_tokens(tokens: dict) -> None:
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)


def _refresh_tokens(refresh_token: str) -> dict:
    url = f"https://{SLUG}.teamly.ru/api/v1/auth/integration/refresh"
    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
    }
    r = httpx.post(url, json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    return {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "access_token_expires_at": int(data["access_token_expires_at"]),
        "refresh_token_expires_at": int(data["refresh_token_expires_at"]),
    }


def get_access_token() -> str:
    """Возвращает валидный access_token, обновляя его через refresh_token при необходимости."""
    tokens = _load_tokens()
    now = time.time()
    if tokens["access_token_expires_at"] - _REFRESH_MARGIN_SECONDS > now:
        return tokens["access_token"]

    logger.info("Teamly access_token истёк/истекает — обновляю по refresh_token")
    fresh = _refresh_tokens(tokens["refresh_token"])
    _save_tokens(fresh)
    return fresh["access_token"]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "X-Account-Slug": SLUG,
        "Content-Type": "application/json",
    }


def _post(path: str, payload: dict) -> dict:
    url = f"{CLUSTER_DOMAIN}{path}"
    r = httpx.post(url, headers=_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def get_article(article_id: str, *, with_content: bool = True) -> dict:
    """Получает статью вики по идентификатору (заголовок, breadcrumbs, при необходимости — контент редактора)."""
    query = {
        "__filter": {"id": article_id},
        "id": True,
        "title": True,
        "space_id": True,
        "breadcrumbs": True,
        "archived": True,
        "updated_at": True,
    }
    if with_content:
        query["editorContentObject"] = {"content": True, "versionAt": True}
    return _post("/api/v1/wiki/ql/article", {"query": query})


def get_space_tree(space_id: str, page: int = 1, per_page: int = 60) -> dict:
    """Список статей в пространстве (постранично, максимум 60 за раз)."""
    url = f"{CLUSTER_DOMAIN}/api/v1/integrations/space/{space_id}/tree"
    r = httpx.get(
        url,
        headers=_headers(),
        params={"page": page, "perPage": per_page},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

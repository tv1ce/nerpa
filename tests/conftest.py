"""
Тестовое окружение: временная БД в памяти, без ENCRYPT_KEY (секреты хранятся открытым
текстом с предупреждением — нормально для тестов), фиксированный SECRET_KEY.

Запуск:
    pip install -r requirements-dev.txt
    pytest tests/ -v
"""
import os
import tempfile

# ── Настраиваем env ДО любых импортов app.* ──────────────────────────────────
# Используем файловую БД во временном каталоге (не in-memory — SQLAlchemy WAL
# не поддерживает :memory: при check_same_thread=False через несколько соединений)
_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_db_file.close()

os.environ.setdefault("DATABASE_URL", f"sqlite:///{_db_file.name}")
os.environ.setdefault("SECRET_KEY",   "test-secret-key-32-chars-minimum!")
# ENCRYPT_KEY намеренно не задан → crypto работает в passthrough-режиме (с предупреждением)

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    """TestClient с полностью инициализированным приложением и тестовой БД."""
    from app.main import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="session")
def admin_client(client):
    """TestClient с активной сессией admin (после смены дефолтного пароля)."""
    # Шаг 1: логин → попадаем на смену пароля (must_change_password=True)
    r = client.post("/auth/login", data={
        "username": "admin",
        "password": "admin",
        "csrf_token": _get_csrf(client, "/auth/login"),
    }, follow_redirects=True)
    assert r.status_code == 200

    # Шаг 2: меняем пароль на тестовый
    csrf = _get_csrf(client, "/auth/change-password")
    client.post("/auth/change-password", data={
        "current_password": "admin",
        "new_password": "Test1234!",
        "confirm_password": "Test1234!",
        "csrf_token": csrf,
    }, follow_redirects=True)

    # Шаг 3: повторный логин с новым паролем
    csrf = _get_csrf(client, "/auth/login")
    client.post("/auth/login", data={
        "username": "admin",
        "password": "Test1234!",
        "csrf_token": csrf,
    }, follow_redirects=True)

    return client


def _get_csrf(client: TestClient, path: str) -> str:
    """Получает CSRF-токен из сессии через GET-запрос на страницу с формой."""
    r = client.get(path)
    # Токен хранится в куке сессии — TestClient сохраняет её автоматически.
    # Нам нужно вытащить его из тела страницы (hidden input) или из сессии напрямую.
    import re
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', r.text)
    return m.group(1) if m else ""

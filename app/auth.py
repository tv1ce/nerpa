from functools import wraps
from fastapi import Request
from fastapi.responses import RedirectResponse, HTMLResponse
from app.database import SessionLocal, verify_password
from app.models import User, CompanySettings
import secrets as _secrets

ROLE_LEVELS = {"admin": 3, "manager": 2, "sales": 2, "field_rep": 2, "viewer": 1, "warehouse": 1,
               "hr": 1, "sales_intern": 1, "demo": 1}


def safe_redirect(url: str, default: str = "/") -> str:
    """Защита от open redirect: разрешаем только локальные пути /path.
    Внешние URL (//evil.com, http://...) → default."""
    if not url:
        return default
    if url.startswith("/") and not url.startswith("//"):
        return url
    return default

# Человекочитаемые названия ролей
ROLE_LABELS = {
    "admin": "Администратор", "manager": "Менеджер", "sales": "Отдел продаж",
    "field_rep": "Торговый представитель",
    "viewer": "Просмотр", "warehouse": "Склад", "hr": "HR",
    "sales_intern": "Стажёр отдела продаж", "demo": "Демо",
}

# Разделы, доступные роли "warehouse" (только чтение)
WAREHOUSE_ALLOWED_PREFIXES = (
    "/orders",
    "/warehouse",
    "/counterparties",
    "/products",   # кладовщику нужен список товаров
    "/files",      # скачивание прикреплённых документов (загрузка/удаление закрыты role_required)
    "/board",           # табло цеха
    "/settings/board",  # настройки табло цеха — кладовщик управляет планом/цитатами
    "/settings/profile",  # свой профиль (ДР, пароль) — доступен всем ролям
    "/auth",
    "/notifications",
    "/static",
    "/manifest.webmanifest",
    "/sw.js",
)

# Разделы, доступные роли "field_rep" (торговый представитель — мобильное приложение).
# Всё остальное (дашборд, счета, склад, настройки) ему недоступно.
FIELD_ALLOWED_PREFIXES = (
    "/field",
    "/counterparties",   # просмотр карточки клиента (после конвертации точки)
    "/settings/profile", # свой профиль (ДР, смена пароля)
    "/auth",
    "/notifications",
    "/static",
    "/manifest.webmanifest",
    "/sw.js",
)

# Разделы, доступные роли "hr" — HR-раздел и общий дашборд, больше ничего.
HR_ALLOWED_PREFIXES = (
    "/hr",
    "/settings/profile",
    "/auth",
    "/notifications",
    "/static",
    "/manifest.webmanifest",
    "/sw.js",
)

# Разделы, доступные роли "sales_intern" (стажёр отдела продаж): скрипты продаж,
# разведка ЛПР и база прозвона — и больше ничего. Дашборд, заказы, счета, склад,
# аналитика и настройки компании стажёру не видны.
#
# Редактирование скриптов отдельно закрывать не нужно: конструктор пускает только
# роли из EDITOR_ROLES в app/routers/scripts.py (admin/manager/sales), а GET
# /scripts/{id}/edit для остальных редиректит на /scripts/{id}/full — режим
# прохождения. Тяжёлые действия в /recon и /leads (запуск сбора, дедуп, импорт)
# закрыты role_required("manager"), то есть уровнем 2 — стажёру с уровнем 1
# они недоступны автоматически.
SALES_INTERN_ALLOWED_PREFIXES = (
    "/scripts",
    "/recon",
    "/leads",
    "/settings/profile",  # свой профиль (ДР, смена пароля)
    "/auth",
    "/notifications",
    "/static",
    "/manifest.webmanifest",
    "/sw.js",
)

# Разделы, закрытые для всех ролей, кроме перечисленных. Кадровые данные —
# зарплатные вилки, оценки, eNPS, метрика по людям — не должны быть видны
# менеджеру или «просмотру» только потому, что он залогинен.
#
# Проверка живёт в login_required/role_required, поэтому распространяется на
# любой новый роут раздела автоматически. Публичные ссылки по токену
# (/hr/w/{token} — форма недели руководителя, /hr/s/{token} — анкета опроса)
# декораторов не имеют и работают по-прежнему: они и рассчитаны на человека
# без логина в NERPA.
SECTION_ROLES = {
    "/hr": ("admin", "hr"),
}

_DEMO_403_HTML = (
    '<div style="font-family:\'Fira Sans\',sans-serif;display:flex;align-items:center;'
    'justify-content:center;height:100vh;flex-direction:column;gap:12px">'
    '<span style="font-size:3rem">👁</span>'
    '<h2 style="margin:0">Демо-режим</h2>'
    '<p style="color:#64748b">В демо-аккаунте редактирование недоступно.</p>'
    '<a href="javascript:history.back()" style="color:#2563eb">Назад</a></div>'
)

_403_HTML = (
    '<div style="font-family:\'Fira Sans\',sans-serif;display:flex;align-items:center;'
    'justify-content:center;height:100vh;flex-direction:column;gap:12px">'
    '<span style="font-size:3rem">🔒</span>'
    '<h2 style="margin:0">403 — Нет доступа</h2>'
    '<p style="color:#64748b">Недостаточно прав для этого действия.</p>'
    '<a href="/orders/" style="color:#2563eb">На главную</a></div>'
)


def get_current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == user_id, User.is_active == True).first()
    finally:
        db.close()


def _demo_check(request: Request):
    """Для роли demo блокирует любые изменяющие запросы."""
    role = request.session.get("user_role", "viewer")
    if role == "demo" and request.method in ("POST", "PUT", "DELETE", "PATCH"):
        return HTMLResponse(_DEMO_403_HTML, status_code=403)
    return None


def _warehouse_check(request: Request):
    """Возвращает 403 если роль warehouse и путь не разрешён."""
    role = request.session.get("user_role", "viewer")
    if role == "warehouse":
        path = request.url.path
        if not any(path.startswith(p) for p in WAREHOUSE_ALLOWED_PREFIXES):
            return HTMLResponse(_403_HTML, status_code=403)
    return None


def _field_check(request: Request):
    """Возвращает 403 если роль field_rep и путь вне его раздела."""
    role = request.session.get("user_role", "viewer")
    if role == "field_rep":
        path = request.url.path
        if not any(path.startswith(p) for p in FIELD_ALLOWED_PREFIXES):
            return HTMLResponse(_403_HTML, status_code=403)
    return None


def _hr_check(request: Request):
    """Возвращает 403 если роль hr и путь вне дашборда/HR-раздела."""
    role = request.session.get("user_role", "viewer")
    if role == "hr":
        path = request.url.path
        if path != "/" and not any(path.startswith(p) for p in HR_ALLOWED_PREFIXES):
            return HTMLResponse(_403_HTML, status_code=403)
    return None


def _sales_intern_check(request: Request):
    """Возвращает 403 если роль sales_intern и путь вне её трёх разделов."""
    role = request.session.get("user_role", "viewer")
    if role == "sales_intern":
        path = request.url.path
        if not any(path.startswith(p) for p in SALES_INTERN_ALLOWED_PREFIXES):
            return HTMLResponse(_403_HTML, status_code=403)
    return None


def _section_check(request: Request):
    """403, если роль не допущена в закрытый раздел (см. SECTION_ROLES)."""
    role = request.session.get("user_role", "viewer")
    path = request.url.path
    for prefix, roles in SECTION_ROLES.items():
        if (path == prefix or path.startswith(prefix + "/")) and role not in roles:
            return HTMLResponse(_403_HTML, status_code=403)
    return None


async def _verify_csrf(request: Request) -> bool:
    """Проверяет CSRF-токен для POST-запросов.
    Принимает токен из тела формы (csrf_token) или заголовка X-CSRF-Token."""
    session_token = request.session.get("csrf_token")
    if not session_token:
        return False
    # Сначала проверяем заголовок (для AJAX)
    header_token = request.headers.get("X-CSRF-Token")
    if header_token:
        return _secrets.compare_digest(session_token, header_token)
    # Затем из тела формы
    try:
        form = await request.form()
        form_token = form.get("csrf_token", "")
        return _secrets.compare_digest(session_token, str(form_token))
    except Exception:
        return False


def _get_fresh_user(request: Request):
    """Проверяет сессию и загружает актуального пользователя из БД.
    Если пользователь деактивирован или удалён — возвращает None."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
        if user:
            request.session["user_role"] = user.role
            company = db.query(CompanySettings).first()
            request.session["mod_leads"]    = bool(company and company.module_leads)
            request.session["mod_recon"]    = bool(company and company.module_recon)
            request.session["mod_sourcing"] = bool(company and company.module_sourcing)
            request.session["mod_field"]    = bool(company and company.module_field)
            request.session["mod_hr"]       = bool(company and company.module_hr)
            request.session["mod_scripts"]  = bool(company and company.module_scripts)
        return user
    finally:
        db.close()


_CHANGE_PWD_PATH = "/auth/change-password"


def login_required(func):
    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        user = _get_fresh_user(request)
        if not user:
            request.session.clear()
            return RedirectResponse(url=f"/auth/login?next={request.url.path}", status_code=302)
        # Принудительная смена пароля — до этого никуда не пускаем
        if getattr(user, "must_change_password", False):
            if not request.url.path.startswith(_CHANGE_PWD_PATH):
                return RedirectResponse(url=_CHANGE_PWD_PATH, status_code=302)
        denied = (_demo_check(request) or _warehouse_check(request) or _field_check(request)
                  or _hr_check(request)
                  or _sales_intern_check(request) or _section_check(request))
        if denied:
            return denied
        # CSRF-проверка для изменяющих запросов
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            if not await _verify_csrf(request):
                return HTMLResponse(
                    '<div style="font-family:sans-serif;text-align:center;padding:2rem">'
                    '<h2>403 — Неверный CSRF-токен</h2>'
                    '<p>Обновите страницу и попробуйте снова.</p>'
                    '<a href="javascript:history.back()">Назад</a></div>',
                    status_code=403,
                )
        return await func(request, *args, **kwargs)
    return wrapper


def role_required(min_role: str = "viewer"):
    """Requires login and a minimum role level (viewer < manager < admin)."""
    def decorator(func):
        @wraps(func)
        async def wrapper(request: Request, *args, **kwargs):
            user = _get_fresh_user(request)
            if not user:
                request.session.clear()
                return RedirectResponse(url=f"/auth/login?next={request.url.path}", status_code=302)
            denied = (_demo_check(request) or _warehouse_check(request) or _field_check(request)
                  or _hr_check(request)
                  or _sales_intern_check(request) or _section_check(request))
            if denied:
                return denied
            role = user.role  # берём роль из БД, не из сессии
            if ROLE_LEVELS.get(role, 0) < ROLE_LEVELS.get(min_role, 0):
                return HTMLResponse(_403_HTML, status_code=403)
            # CSRF-проверка для изменяющих запросов
            if request.method in ("POST", "PUT", "DELETE", "PATCH"):
                if not await _verify_csrf(request):
                    return HTMLResponse(
                        '<div style="font-family:sans-serif;text-align:center;padding:2rem">'
                        '<h2>403 — Неверный CSRF-токен</h2>'
                        '<p>Обновите страницу и попробуйте снова.</p>'
                        '<a href="javascript:history.back()">Назад</a></div>',
                        status_code=403,
                    )
            return await func(request, *args, **kwargs)
        return wrapper
    return decorator

from functools import wraps
from fastapi import Request
from fastapi.responses import RedirectResponse, HTMLResponse
from app.database import SessionLocal, verify_password
from app.models import User

ROLE_LEVELS = {"admin": 3, "manager": 2, "viewer": 1, "warehouse": 1}

# Разделы, доступные роли "warehouse" (только чтение)
WAREHOUSE_ALLOWED_PREFIXES = (
    "/orders",
    "/warehouse",
    "/counterparties",
    "/auth",
    "/notifications",
    "/static",
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


def _warehouse_check(request: Request):
    """Возвращает 403 если роль warehouse и путь не разрешён."""
    role = request.session.get("user_role", "viewer")
    if role == "warehouse":
        path = request.url.path
        if not any(path.startswith(p) for p in WAREHOUSE_ALLOWED_PREFIXES):
            return HTMLResponse(_403_HTML, status_code=403)
    return None


def login_required(func):
    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        if not request.session.get("user_id"):
            return RedirectResponse(url=f"/auth/login?next={request.url.path}", status_code=302)
        denied = _warehouse_check(request)
        if denied:
            return denied
        return await func(request, *args, **kwargs)
    return wrapper


def role_required(min_role: str = "viewer"):
    """Requires login and a minimum role level (viewer < manager < admin)."""
    def decorator(func):
        @wraps(func)
        async def wrapper(request: Request, *args, **kwargs):
            user_id = request.session.get("user_id")
            if not user_id:
                return RedirectResponse(url=f"/auth/login?next={request.url.path}", status_code=302)
            denied = _warehouse_check(request)
            if denied:
                return denied
            role = request.session.get("user_role", "viewer")
            if ROLE_LEVELS.get(role, 0) < ROLE_LEVELS.get(min_role, 0):
                return HTMLResponse(_403_HTML, status_code=403)
            return await func(request, *args, **kwargs)
        return wrapper
    return decorator

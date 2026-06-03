from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db, verify_password, hash_password
from app.models import User
from app.auth import login_required, safe_redirect as _safe_next

router = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(directory="app/templates")


# ── Простой in-memory rate limiting на логин (H-13) ───────────────────────────
import time as _time
from collections import defaultdict

_login_attempts: dict[str, list] = defaultdict(list)
_MAX_ATTEMPTS = 5          # попыток
_WINDOW_SEC = 300          # за 5 минут


def _rate_limited(key: str) -> bool:
    now = _time.time()
    # чистим устаревшие отметки
    attempts = [t for t in _login_attempts[key] if now - t < _WINDOW_SEC]
    _login_attempts[key] = attempts
    return len(attempts) >= _MAX_ATTEMPTS


def _record_attempt(key: str) -> None:
    _login_attempts[key].append(_time.time())


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "auth/login.html", {"next": _safe_next(next), "error": None})


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/"),
    db: Session = Depends(get_db),
):
    next = _safe_next(next)
    # Ключ ограничения — IP клиента + логин
    client_ip = request.client.host if request.client else "?"
    rl_key = f"{client_ip}:{username}"

    if _rate_limited(rl_key):
        return templates.TemplateResponse(
            request, "auth/login.html",
            {"next": next, "error": "Слишком много попыток входа. Попробуйте через 5 минут."},
            status_code=429,
        )

    user = db.query(User).filter(User.username == username, User.is_active == True).first()
    if not user or not verify_password(password, user.password_hash):
        _record_attempt(rl_key)
        return templates.TemplateResponse(
            request, "auth/login.html",
            {"next": next, "error": "Неверный логин или пароль"},
            status_code=401,
        )
    # Успешный вход — сбрасываем счётчик попыток
    _login_attempts.pop(rl_key, None)
    request.session["user_id"] = user.id
    request.session["user_name"] = user.full_name
    request.session["user_role"] = user.role
    # Для роли склада — стартовая страница остатки
    if user.role == "warehouse" and next == "/":
        next = "/warehouse/"
    return RedirectResponse(url=next, status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/auth/login", status_code=302)


# ── Смена пароля (принудительная при первом входе или по желанию) ─────────────

@router.get("/change-password", response_class=HTMLResponse)
@login_required
async def change_password_page(request: Request):
    user_id = request.session.get("user_id")
    return templates.TemplateResponse(request, "auth/change_password.html", {
        "error": None,
        "forced": request.session.get("must_change_password", False),
    })


@router.post("/change-password")
@login_required
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    user_id = request.session.get("user_id")
    user = db.query(User).filter(User.id == user_id).first()

    def _err(msg):
        return templates.TemplateResponse(request, "auth/change_password.html", {
            "error": msg,
            "forced": getattr(user, "must_change_password", False),
        }, status_code=400)

    if not user:
        return RedirectResponse(url="/auth/login", status_code=302)

    if not verify_password(current_password, user.password_hash):
        return _err("Текущий пароль введён неверно")

    if len(new_password) < 6:
        return _err("Новый пароль должен содержать минимум 6 символов")

    if new_password != confirm_password:
        return _err("Пароли не совпадают")

    if new_password == current_password:
        return _err("Новый пароль не должен совпадать с текущим")

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.commit()

    return RedirectResponse(url="/?changed=1", status_code=302)

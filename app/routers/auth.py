from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db, verify_password, hash_password
from app.models import User
from app.auth import login_required

router = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "auth/login.html", {"next": next, "error": None})


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/"),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username, User.is_active == True).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, "auth/login.html",
            {"next": next, "error": "Неверный логин или пароль"},
            status_code=401,
        )
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

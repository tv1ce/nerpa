from functools import wraps
from fastapi import Request
from fastapi.responses import RedirectResponse
from app.database import SessionLocal, verify_password
from app.models import User


def get_current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == user_id, User.is_active == True).first()
    finally:
        db.close()


def login_required(func):
    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        if not request.session.get("user_id"):
            return RedirectResponse(url=f"/auth/login?next={request.url.path}", status_code=302)
        return await func(request, *args, **kwargs)
    return wrapper

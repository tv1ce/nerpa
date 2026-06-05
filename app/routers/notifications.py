from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import login_required
from app.models import Notification

router = APIRouter(prefix="/notifications", tags=["notifications"])
templates = Jinja2Templates(directory="app/templates")


def _visible_filter(request: Request):
    """Уведомления, видимые текущему пользователю:
    его персональные (user_id == me) + системные (user_id IS NULL)."""
    uid = request.session.get("user_id")
    return or_(Notification.user_id == uid, Notification.user_id.is_(None))


@router.get("/count")
@login_required
async def notifications_count(request: Request, db: Session = Depends(get_db)):
    count = db.query(Notification).filter(
        Notification.is_read == False,
        _visible_filter(request),
    ).count()
    return JSONResponse({"count": count})


@router.get("/recent")
@login_required
async def notifications_recent(request: Request, db: Session = Depends(get_db)):
    vis = _visible_filter(request)
    items = db.query(Notification).filter(vis)\
        .order_by(Notification.created_at.desc()).limit(7).all()
    total = db.query(Notification).filter(vis).count()
    unread = db.query(Notification).filter(Notification.is_read == False, vis).count()

    def fmt_time(dt):
        if not dt:
            return ""
        from datetime import date
        if dt.date() == date.today():
            return "сегодня " + dt.strftime("%H:%M")
        return dt.strftime("%d.%m %H:%M")

    return JSONResponse({
        "total": total,
        "unread": unread,
        "items": [
            {
                "id": n.id,
                "type": n.type,
                "title": n.title,
                "body": n.body or "",
                "link": n.link or "",
                "is_read": n.is_read,
                "time": fmt_time(n.created_at),
            }
            for n in items
        ],
    })


@router.post("/mark-read")
@login_required
async def mark_read_ajax(request: Request, db: Session = Depends(get_db)):
    db.query(Notification).filter(
        Notification.is_read == False,
        _visible_filter(request),
    ).update({"is_read": True}, synchronize_session=False)
    db.commit()
    return JSONResponse({"ok": True})


@router.get("/", response_class=HTMLResponse)
@login_required
async def notifications_list(request: Request, db: Session = Depends(get_db)):
    items = db.query(Notification).filter(_visible_filter(request))\
        .order_by(Notification.created_at.desc()).limit(100).all()
    unread_count = sum(1 for n in items if not n.is_read)
    return templates.TemplateResponse(request, "notifications/list.html", {
        "notifications": items,
        "unread_count": unread_count,
    })


@router.post("/read-all")
@login_required
async def read_all(request: Request, db: Session = Depends(get_db)):
    db.query(Notification).filter(
        Notification.is_read == False,
        _visible_filter(request),
    ).update({"is_read": True}, synchronize_session=False)
    db.commit()
    return RedirectResponse(url="/notifications/", status_code=302)


@router.post("/{notif_id}/read")
@login_required
async def read_one(request: Request, notif_id: int, db: Session = Depends(get_db)):
    n = db.query(Notification).filter(
        Notification.id == notif_id,
        _visible_filter(request),
    ).first()
    if n:
        n.is_read = True
        db.commit()
    return RedirectResponse(url="/notifications/", status_code=302)

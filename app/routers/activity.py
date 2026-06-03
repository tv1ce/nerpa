from datetime import date
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import role_required, safe_redirect
from app.models import Task, Comment, User

router = APIRouter(prefix="/activity", tags=["activity"])
templates = Jinja2Templates(directory="app/templates")

PRIORITY_LABELS = {"low": "Низкий", "normal": "Обычный", "high": "Высокий", "urgent": "Срочный"}
PRIORITY_COLORS = {"low": "secondary", "normal": "primary", "high": "warning", "urgent": "danger"}


# ── Задачи ────────────────────────────────────────────────────────────────────

@router.post("/tasks/new")
@role_required("manager")
async def create_task(
    request: Request,
    entity_type: str = Form(...),
    entity_id: int = Form(...),
    title: str = Form(...),
    priority: str = Form(default="normal"),
    assigned_to_id: int = Form(default=0),
    due_date: str = Form(default=""),
    redirect_url: str = Form(default=""),
    db: Session = Depends(get_db),
):
    if title.strip():
        db.add(Task(
            title=title.strip(),
            entity_type=entity_type,
            entity_id=entity_id,
            priority=priority,
            assigned_to_id=assigned_to_id or None,
            due_date=date.fromisoformat(due_date) if due_date else None,
            created_by_id=request.session.get("user_id"),
            status="open",
        ))
        db.commit()
    return RedirectResponse(url=safe_redirect(redirect_url), status_code=302)


@router.post("/tasks/{task_id}/toggle")
@role_required("manager")
async def toggle_task(
    request: Request, task_id: int,
    redirect_url: str = Form(default=""),
    db: Session = Depends(get_db),
):
    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        task.status = "done" if task.status == "open" else "open"
        db.commit()
    return RedirectResponse(url=safe_redirect(redirect_url), status_code=302)


@router.post("/tasks/{task_id}/delete")
@role_required("manager")
async def delete_task(
    request: Request, task_id: int,
    redirect_url: str = Form(default=""),
    db: Session = Depends(get_db),
):
    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        db.delete(task)
        db.commit()
    return RedirectResponse(url=safe_redirect(redirect_url), status_code=302)


# ── Комментарии ───────────────────────────────────────────────────────────────

@router.post("/comments/new")
@role_required("manager")
async def create_comment(
    request: Request,
    entity_type: str = Form(...),
    entity_id: int = Form(...),
    body: str = Form(...),
    redirect_url: str = Form(default=""),
    db: Session = Depends(get_db),
):
    if body.strip():
        db.add(Comment(
            body=body.strip(),
            entity_type=entity_type,
            entity_id=entity_id,
            created_by_id=request.session.get("user_id"),
        ))
        db.commit()
    return RedirectResponse(url=safe_redirect(redirect_url), status_code=302)


@router.post("/comments/{comment_id}/delete")
@role_required("manager")
async def delete_comment(
    request: Request, comment_id: int,
    redirect_url: str = Form(default=""),
    db: Session = Depends(get_db),
):
    comment = db.query(Comment).filter(Comment.id == comment_id).first()
    if comment:
        uid = request.session.get("user_id")
        role = request.session.get("user_role", "viewer")
        if comment.created_by_id == uid or role == "admin":
            db.delete(comment)
            db.commit()
    return RedirectResponse(url=safe_redirect(redirect_url), status_code=302)

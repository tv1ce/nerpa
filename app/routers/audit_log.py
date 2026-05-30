from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import role_required
from app.models import AuditLog

router = APIRouter(prefix="/audit-log", tags=["audit_log"])
templates = Jinja2Templates(directory="app/templates")

ACTION_LABELS = {
    "created":        "Создан",
    "updated":        "Изменён",
    "status_changed": "Статус изменён",
    "deleted":        "Удалён",
    "category_set":   "Категория изменена",
}
ACTION_ICONS = {
    "created":        "bi-plus-circle-fill text-success",
    "updated":        "bi-pencil-fill text-primary",
    "status_changed": "bi-arrow-left-right text-warning",
    "deleted":        "bi-trash-fill text-danger",
    "category_set":   "bi-tag-fill text-info",
}


@router.get("/", response_class=HTMLResponse)
@role_required("admin")
async def audit_log_index(
    request: Request,
    entity_type: str = "",
    db: Session = Depends(get_db),
):
    q = db.query(AuditLog).order_by(AuditLog.created_at.desc())
    if entity_type:
        q = q.filter(AuditLog.entity_type == entity_type)
    logs = q.limit(300).all()
    return templates.TemplateResponse(request, "audit_log/index.html", {
        "logs": logs,
        "filter_type": entity_type,
        "action_labels": ACTION_LABELS,
        "action_icons":  ACTION_ICONS,
    })

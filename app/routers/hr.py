import os
from datetime import datetime

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import login_required, role_required
from app.models import HrEntry, CompanySettings
from app.services import hr_sync, hr_report, telegram_send

router = APIRouter(prefix="/hr", tags=["hr"])
templates = Jinja2Templates(directory="app/templates")

SECTION_LABELS = {
    "personal": "Личностный файл",
    "complaints": "«Дичь»",
    "achievements": "Достижения",
    "enps": "eNPS (сотрудники)",
    "enps_managers": "eNPS (руководители)",
    "metrics": "Метрики",
    "vacancies": "Сроки закрытия вакансий",
}


@router.get("/", response_class=HTMLResponse)
@login_required
async def hr_home(request: Request, section: str = "", db: Session = Depends(get_db)):
    q = db.query(HrEntry).order_by(HrEntry.subject_name, HrEntry.imported_at.desc())
    if section:
        q = q.filter(HrEntry.section == section)
    entries = q.limit(500).all()

    counts = dict(db.query(HrEntry.section, func.count(HrEntry.id)).group_by(HrEntry.section).all())
    company = db.query(CompanySettings).first()

    return templates.TemplateResponse(request, "hr/list.html", {
        "entries": entries,
        "section_labels": SECTION_LABELS,
        "counts": counts,
        "filter_section": section,
        "last_synced_at": company.hr_last_synced_at if company else None,
        "sync_interval": company.hr_sync_interval_minutes if company else 360,
        "synced": request.query_params.get("synced"),
        "reported": request.query_params.get("reported"),
        "error": request.query_params.get("error"),
    })


@router.post("/sync")
@role_required("admin")
async def hr_sync_now(request: Request, db: Session = Depends(get_db)):
    try:
        stats = hr_sync.sync_from_teamly(db)
        company = db.query(CompanySettings).first()
        if company:
            company.hr_last_synced_at = datetime.utcnow()
            db.commit()
        total_new = sum(s["new"] for s in stats.values())
        return RedirectResponse(url=f"/hr/?synced={total_new}", status_code=302)
    except Exception as e:
        return RedirectResponse(url=f"/hr/?error={e}", status_code=302)


@router.post("/report/full")
@role_required("admin")
async def hr_send_full_report(request: Request, db: Session = Depends(get_db)):
    try:
        text = hr_report.generate_full_report(db)
        chat_ids = telegram_send.parse_chat_ids(os.getenv("TMS_HR_CHAT_IDS", ""))
        if not chat_ids:
            return RedirectResponse(url="/hr/?error=TMS_HR_CHAT_IDS+не+задан", status_code=302)
        telegram_send.send_markdown(chat_ids, text)
        return RedirectResponse(url="/hr/?reported=full", status_code=302)
    except Exception as e:
        return RedirectResponse(url=f"/hr/?error={e}", status_code=302)


@router.post("/report/metrics")
@role_required("admin")
async def hr_send_metrics_report(request: Request, db: Session = Depends(get_db)):
    try:
        text = hr_report.generate_metrics_report(db)
        if text is None:
            return RedirectResponse(url="/hr/?reported=metrics-empty", status_code=302)
        chat_ids = telegram_send.parse_chat_ids(os.getenv("TMS_HR_CHAT_IDS", ""))
        if not chat_ids:
            return RedirectResponse(url="/hr/?error=TMS_HR_CHAT_IDS+не+задан", status_code=302)
        telegram_send.send_markdown(chat_ids, text)
        return RedirectResponse(url="/hr/?reported=metrics", status_code=302)
    except Exception as e:
        return RedirectResponse(url=f"/hr/?error={e}", status_code=302)

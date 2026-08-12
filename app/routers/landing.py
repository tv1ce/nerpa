"""Публичный лендинг заявки — /order, без авторизации.

Вторая (после /shop/{token}) точка входа для клиента, но для СОВСЕМ другого:
здесь человек нас ещё не знает, договора и цен у него нет, поэтому заказ ему
оформить нечего. Форма создаёт точку в разделе «Прозвон» (SalesLead) и лид в
Bitrix24 — менеджер перезванивает и превращает её в контрагента штатной
кнопкой «В контрагенты», после чего клиенту уже можно выдать кабинет заказа.

Спам-защита: honeypot-поле (боты заполняют всё подряд, люди его не видят),
минимальная валидация телефона и общий IP-rate-limit из public.py.
"""
import logging
import re
import threading

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CompanySettings, SalesLead, Notification
from app.routers.public import _rate_limited
from app.utils import log_action

logger = logging.getLogger(__name__)

router = APIRouter(tags=["landing"])
templates = Jinja2Templates(directory="app/templates")

# Источник в SalesLead.source_file — по нему заявки с сайта отличаются от
# импортированных списков прозвона и от точек, заведённых торгпредом в «Поле».
SOURCE_SITE = "site_form"

_DIGITS_RE = re.compile(r"\D+")


def _clean_phone(raw: str) -> str:
    """Нормализует телефон к цифрам. Российский номер — 11 цифр; всё, что
    короче 10, считаем опечаткой и заявку не принимаем: перезвонить по такому
    всё равно нельзя, а менеджер потратит время."""
    digits = _DIGITS_RE.sub("", raw or "")
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def _push_site_lead_bg(lead_id: int) -> None:
    """Создаёт CRM-лид в фоновом потоке — клиент не ждёт ответа Bitrix24."""
    from app.database import SessionLocal
    from app.services.bitrix_client import push_site_lead_to_bitrix
    db = SessionLocal()
    try:
        lead = db.query(SalesLead).filter(SalesLead.id == lead_id).first()
        company = db.query(CompanySettings).first()
        if lead:
            push_site_lead_to_bitrix(lead, company, db)
    except Exception as e:
        logger.error("push_site_lead_to_bitrix bg %s: %s", lead_id, e)
    finally:
        db.close()


@router.get("/order", response_class=HTMLResponse)
async def landing(request: Request, db: Session = Depends(get_db)):
    company = db.query(CompanySettings).first()
    return templates.TemplateResponse(request, "public/landing.html", {"company": company})


@router.post("/order/submit")
async def landing_submit(request: Request, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "?"
    if _rate_limited(ip):
        return JSONResponse({"ok": False, "error": "Слишком много запросов"}, status_code=429)

    payload = await request.json()

    # Honeypot: поле спрятано от человека через CSS, но заполняется ботом.
    # Отвечаем «ок», чтобы бот не подбирал обход, а заявку просто не создаём.
    if (payload.get("company_site") or "").strip():
        return JSONResponse({"ok": True})

    name = (payload.get("name") or "").strip()[:300]
    contact = (payload.get("contact") or "").strip()[:150]
    phone_raw = (payload.get("phone") or "").strip()
    city = (payload.get("city") or "").strip()[:150]
    email = (payload.get("email") or "").strip()[:150]
    comment = (payload.get("comment") or "").strip()[:1000]

    if not name:
        return JSONResponse({"ok": False, "error": "Укажите название заведения"}, status_code=400)
    phone = _clean_phone(phone_raw)
    if len(phone) < 11:
        return JSONResponse({"ok": False, "error": "Проверьте номер телефона"}, status_code=400)

    lead = SalesLead(
        name=name,
        contact_person=contact or None,
        phone=phone,
        email=email or None,
        city=city or None,
        notes=comment or None,
        call_status="new",
        source_file=SOURCE_SITE,
    )
    db.add(lead)
    db.flush()

    log_action(db, "lead", lead.id, "created", None, f"Заявка с сайта: {name}")
    db.add(Notification(
        type="site_lead",
        title=f"📩 Заявка с сайта — {name}",
        body=(f"{city + ', ' if city else ''}{contact or 'без имени'}, {phone}."
              + (f"\n{comment}" if comment else "")),
        link=f"/leads/?q={name}",
    ))
    db.commit()

    threading.Thread(target=_push_site_lead_bg, args=(lead.id,), daemon=True).start()
    return JSONResponse({"ok": True})

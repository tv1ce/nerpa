import hmac
import json
import logging
from fastapi import APIRouter, Depends, Request, Form, File, UploadFile, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import login_required, role_required
from app.database import get_db
from app.models import CompanySettings
from app.services.onec_client import (
    test_connection,
    sync_products_from_1c,
    sync_payments_from_1c,
    push_counterparty,
    push_order,
    push_stock_movement,
)
from app.services.onec_inbound import (
    upsert_invoice_from_1c,
    attach_document_pdf,
    find_order_by_1c_ref,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sync/1c", tags=["sync_1c"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/test")
@login_required
async def test_1c_connection(request: Request, db: Session = Depends(get_db)):
    """Проверка подключения к 1С:УНФ."""
    result = test_connection(db)
    return JSONResponse(result)


@router.get("/", response_class=HTMLResponse)
@role_required("admin")
async def sync_status_page(request: Request, db: Session = Depends(get_db)):
    """Страница статуса синхронизации с 1С."""
    from app.models import CompanySettings, AuditLog
    company = db.query(CompanySettings).first()
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.entity_type == "1c_sync")
        .order_by(AuditLog.created_at.desc())
        .limit(50)
        .all()
    )
    return templates.TemplateResponse(request, "sync_1c/index.html", {
        "company": company,
        "logs": logs,
    })


@router.post("/products")
@role_required("admin")
async def sync_products(request: Request, db: Session = Depends(get_db)):
    """Ручной запуск импорта номенклатуры из 1С."""
    result = sync_products_from_1c(db)
    _audit_sync(db, "sync_products", result)
    return JSONResponse(result)


@router.post("/payments")
@role_required("admin")
async def sync_payments(request: Request, db: Session = Depends(get_db)):
    """Ручной запуск импорта оплат из 1С."""
    result = sync_payments_from_1c(db)
    _audit_sync(db, "sync_payments", result)
    return JSONResponse(result)


@router.post("/counterparty/{cp_id}")
@role_required("admin")
async def push_cp(cp_id: int, request: Request, db: Session = Depends(get_db)):
    """Ручной push контрагента в 1С."""
    from app.models import Counterparty
    cp = db.query(Counterparty).filter(Counterparty.id == cp_id).first()
    if not cp:
        return JSONResponse({"ok": False, "message": "Контрагент не найден"}, status_code=404)
    ref_key = push_counterparty(cp, db)
    return JSONResponse({"ok": bool(ref_key), "ref_key": ref_key})


@router.post("/order/{order_id}")
@role_required("admin")
async def push_ord(order_id: int, request: Request, db: Session = Depends(get_db)):
    """Ручной push заказа в 1С."""
    from app.models import Order
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return JSONResponse({"ok": False, "message": "Заказ не найден"}, status_code=404)
    ref_key = push_order(order, db)
    return JSONResponse({"ok": bool(ref_key), "ref_key": ref_key})


@router.post("/stock/{movement_id}")
@role_required("admin")
async def push_stock(movement_id: int, request: Request, db: Session = Depends(get_db)):
    """Ручной push движения склада в 1С."""
    from app.models import StockMovement
    mv = db.query(StockMovement).filter(StockMovement.id == movement_id).first()
    if not mv:
        return JSONResponse({"ok": False, "message": "Движение не найдено"}, status_code=404)
    ref_key = push_stock_movement(mv, db)
    return JSONResponse({"ok": bool(ref_key), "ref_key": ref_key})


@router.post("/run-all")
@role_required("admin")
async def run_all(request: Request, db: Session = Depends(get_db)):
    """Полный цикл синхронизации: номенклатура + оплаты."""
    r1 = sync_products_from_1c(db)
    r2 = sync_payments_from_1c(db)
    result = {
        "products": r1,
        "payments": r2,
        "errors": r1.get("errors", []) + r2.get("errors", []),
    }
    _audit_sync(db, "run_all", result)
    return JSONResponse(result)


# ── Входящий вебхук из 1С (push счетов и печатных форм) ───────────────────────
# Аутентификация — по токену onec_webhook_token (заголовок X-TMS-Token).
# Сессия/роль НЕ требуются: 1С ходит сюда без cookie.

MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 МБ — печатная форма столько не весит


def _check_token(db: Session, token: str | None) -> bool:
    s = db.query(CompanySettings).first()
    secret = (s.onec_webhook_token or "") if s else ""
    if not secret or not token:
        return False
    return hmac.compare_digest(str(token), str(secret))


@router.post("/hook/document")
async def hook_document(
    request: Request,
    db: Session = Depends(get_db),
    x_tms_token: str | None = Header(default=None),
    doc_type: str = Form(...),
    doc_ref: str = Form(...),
    number: str = Form(default=""),
    doc_date: str = Form(default=""),
    amount: str = Form(default=""),
    order_ref: str = Form(default=""),
    counterparty_ref: str = Form(default=""),
    items_json: str = Form(default=""),
    file: UploadFile | None = File(default=None),
):
    """Приём документа из 1С: счёт (метаданные + PDF) или печатная форма УПД/ТН.

    multipart/form-data, заголовок X-TMS-Token. doc_type ∈ {invoice, upd, tn}.
    Для invoice — создаём/обновляем счёт в TMS; PDF (если есть) — вложение заказа.
    Для upd/tn — только PDF-вложение к заказу.
    """
    if not _check_token(db, x_tms_token):
        return JSONResponse({"ok": False, "message": "Неверный токен"}, status_code=401)

    doc_type = (doc_type or "").strip().lower()
    if doc_type not in ("invoice", "upd", "tn"):
        return JSONResponse({"ok": False, "message": f"Неизвестный doc_type: {doc_type}"},
                            status_code=400)

    try:
        amount_val = float(amount) if amount.strip() else None
    except ValueError:
        amount_val = None
    try:
        items = json.loads(items_json) if items_json.strip() else None
        if items and not isinstance(items, list):
            items = None
    except (ValueError, TypeError):
        items = None

    invoice = None
    if doc_type == "invoice":
        invoice = upsert_invoice_from_1c(
            db, doc_ref=doc_ref, number=number.strip(), date_iso=doc_date.strip() or None,
            amount=amount_val, order_ref=order_ref.strip() or None,
            counterparty_ref=counterparty_ref.strip() or None, items=items,
        )

    # PDF-вложение — кладём в карточку заказа (находим заказ по order_ref,
    # либо по заказу связанного счёта)
    file_id = None
    if file is not None and getattr(file, "filename", None):
        data = await file.read()
        if data and len(data) <= MAX_PDF_BYTES:
            order = find_order_by_1c_ref(db, order_ref.strip() or None)
            if not order and invoice and invoice.order_id:
                order = invoice.order
            if order:
                af = attach_document_pdf(
                    db, order=order, doc_type=doc_type, doc_ref=doc_ref,
                    filename=file.filename, data=data,
                )
                file_id = af.id if af else None
            else:
                logger.warning("hook_document: PDF без заказа (doc_type=%s, order_ref=%s)",
                               doc_type, order_ref)

    _audit_sync(db, f"hook_{doc_type}",
                {"updated": 1 if (invoice or file_id) else 0,
                 "errors": [] if (invoice or file_id) else ["не привязано к заказу/счёту"]})
    return JSONResponse({
        "ok": bool(invoice or file_id),
        "invoice_id": invoice.id if invoice else None,
        "file_id": file_id,
    })


def _audit_sync(db: Session, action: str, result: dict) -> None:
    """Пишем краткую запись в AuditLog о результате синхронизации."""
    from app.models import AuditLog
    errors = result.get("errors", [])
    note = f"created={result.get('created', '?')} updated={result.get('updated', '?')}" \
        if "created" in result else f"updated={result.get('updated', '?')}"
    if errors:
        note += f" errors={len(errors)}: {'; '.join(str(e) for e in errors[:3])}"
    try:
        db.add(AuditLog(entity_type="1c_sync", entity_id=0, action=action, note=note))
        db.commit()
    except Exception:
        db.rollback()

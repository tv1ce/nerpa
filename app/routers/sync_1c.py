import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import login_required, role_required
from app.database import get_db
from app.services.onec_client import (
    test_connection,
    sync_products_from_1c,
    sync_payments_from_1c,
    sync_invoices_from_1c,
    sync_shipments_from_1c,
    sync_documents_from_1c,
    sync_warehouses_from_1c,
    sync_categories_from_1c,
    sync_receiving_tasks_from_1c,
    sync_transfer_tasks_from_1c,
    sync_stock_balances_from_1c,
    push_counterparty,
    push_order,
    push_stock_movement,
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


@router.post("/invoices")
@role_required("admin")
async def sync_invoices(request: Request, db: Session = Depends(get_db)):
    """Ручной запуск импорта счетов из 1С (дубль счёта без печатных форм)."""
    result = sync_invoices_from_1c(db)
    _audit_sync(db, "sync_invoices", result)
    return JSONResponse(result)


@router.post("/warehouses")
@role_required("admin")
async def sync_warehouses(request: Request, db: Session = Depends(get_db)):
    """Ручной запуск импорта справочника складов из 1С."""
    result = sync_warehouses_from_1c(db)
    _audit_sync(db, "sync_warehouses", result)
    return JSONResponse(result)


@router.post("/categories")
@role_required("admin")
async def sync_categories(request: Request, db: Session = Depends(get_db)):
    """Ручной запуск импорта категорий (групп номенклатуры) из 1С."""
    result = sync_categories_from_1c(db)
    _audit_sync(db, "sync_categories", result)
    return JSONResponse(result)


@router.post("/receiving")
@role_required("admin")
async def sync_receiving(request: Request, db: Session = Depends(get_db)):
    """Ручной запуск импорта задач на приёмку (непроведённых поступлений) из 1С."""
    result = sync_receiving_tasks_from_1c(db)
    _audit_sync(db, "sync_receiving", result)
    return JSONResponse(result)


@router.post("/transfers")
@role_required("admin")
async def sync_transfers(request: Request, db: Session = Depends(get_db)):
    """Ручной запуск импорта задач на складское перемещение из 1С."""
    result = sync_transfer_tasks_from_1c(db)
    _audit_sync(db, "sync_transfers", result)
    return JSONResponse(result)


@router.post("/balances")
@role_required("admin")
async def sync_balances(request: Request, db: Session = Depends(get_db)):
    """Ручной запуск синхронизации остатков по складам из 1С.

    Следом остаток уезжает в свойство товара каталога Bitrix24 — тем же
    порядком, что и в фоновой синхронизации, чтобы ручной прогон давал
    ровно тот же результат."""
    from app.services.bitrix_client import push_stock_to_bitrix
    result = sync_stock_balances_from_1c(db)
    result["bitrix"] = push_stock_to_bitrix(db)
    result["errors"] = result.get("errors", []) + result["bitrix"].get("errors", [])
    _audit_sync(db, "sync_balances", result)
    return JSONResponse(result)


@router.post("/documents")
@role_required("admin")
async def sync_documents(request: Request, db: Session = Depends(get_db)):
    """Ручной запуск: расходные + печатные формы/XML (Счёт, УПД) из расширения 1С."""
    r1 = sync_shipments_from_1c(db)
    r2 = sync_documents_from_1c(db)
    result = {"shipments": r1, "documents": r2,
              "errors": r1.get("errors", []) + r2.get("errors", [])}
    _audit_sync(db, "sync_documents", {"updated": r2.get("attached", 0),
                                       "errors": result["errors"]})
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
    """Полный цикл синхронизации: справочники + номенклатура + счета + расходные/документы + оплаты + приёмка."""
    rw = sync_warehouses_from_1c(db)
    rc = sync_categories_from_1c(db)
    r1 = sync_products_from_1c(db)
    r3 = sync_invoices_from_1c(db)
    rs = sync_shipments_from_1c(db)
    rd = sync_documents_from_1c(db)
    r2 = sync_payments_from_1c(db)
    rr = sync_receiving_tasks_from_1c(db)
    rtr = sync_transfer_tasks_from_1c(db)
    rb = sync_stock_balances_from_1c(db)
    from app.services.bitrix_client import push_stock_to_bitrix
    rbx = push_stock_to_bitrix(db)
    result = {
        "warehouses": rw,
        "categories": rc,
        "products": r1,
        "invoices": r3,
        "shipments": rs,
        "documents": rd,
        "payments": r2,
        "receiving": rr,
        "transfers": rtr,
        "balances": rb,
        "errors": (rw.get("errors", []) + rc.get("errors", []) + r1.get("errors", [])
                   + r3.get("errors", []) + rs.get("errors", [])
                   + rd.get("errors", []) + r2.get("errors", []) + rr.get("errors", [])
                   + rtr.get("errors", []) + rb.get("errors", [])),
    }
    _audit_sync(db, "run_all", result)
    return JSONResponse(result)


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

"""Раздел «Списание» кабинета кладовщика.

Кладовщик заводит списание прямо в NERPA (корреспонденция/причина, склад,
номенклатура и количество; дата/время — момент создания). При сохранении
сразу создаётся StockMovement(out) и пушится документ в 1С, который
проводится в том же запросе (push_writeoff, см. onec_client.py) — черновика
не бывает, как и просил пользователь.
"""
import json
from datetime import date

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import login_required
from app.models import WriteOff, WriteOffLine, WriteOffReason, Warehouse, Product, StockMovement, CompanySettings
from app.services.onec_client import push_writeoff
from app.utils import log_action, maybe_notify_low_stock

router = APIRouter(prefix="/warehouse/writeoffs", tags=["warehouse_writeoffs"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
@login_required
async def writeoffs_list(request: Request, db: Session = Depends(get_db)):
    writeoffs = (
        db.query(WriteOff)
        .order_by(WriteOff.created_at.desc())
        .limit(50)
        .all()
    )
    return templates.TemplateResponse(request, "warehouse/writeoffs_list.html", {
        "writeoffs": writeoffs,
    })


@router.get("/new", response_class=HTMLResponse)
@login_required
async def writeoff_new(request: Request, db: Session = Depends(get_db)):
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()
    warehouses = db.query(Warehouse).filter(Warehouse.is_active == True).order_by(Warehouse.name).all()
    reasons = db.query(WriteOffReason).filter(WriteOffReason.is_active == True).order_by(WriteOffReason.name).all()
    default_wh = next((w for w in warehouses if w.is_default), warehouses[0] if warehouses else None)
    return templates.TemplateResponse(request, "warehouse/writeoff_form.html", {
        "products": products,
        "warehouses": warehouses,
        "reasons": reasons,
        "default_warehouse_id": default_wh.id if default_wh else None,
    })


@router.post("/new")
@login_required
async def writeoff_create(
    request: Request,
    warehouse_id: str = Form(default=""),
    reason_id: str = Form(default=""),
    notes: str = Form(default=""),
    products_json: str = Form(default="[]"),
    db: Session = Depends(get_db),
):
    try:
        lines = json.loads(products_json)
    except (ValueError, TypeError):
        lines = []
    lines = [ln for ln in lines if ln.get("product_id") and float(ln.get("quantity") or 0) > 0]
    if not lines:
        products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()
        warehouses = db.query(Warehouse).filter(Warehouse.is_active == True).order_by(Warehouse.name).all()
        reasons = db.query(WriteOffReason).filter(WriteOffReason.is_active == True).order_by(WriteOffReason.name).all()
        return templates.TemplateResponse(request, "warehouse/writeoff_form.html", {
            "products": products, "warehouses": warehouses, "reasons": reasons,
            "error": "Добавьте хотя бы одну позицию с количеством больше нуля.",
        })

    user_id = request.session.get("user_id")
    wo = WriteOff(
        warehouse_id=int(warehouse_id) if warehouse_id else None,
        reason_id=int(reason_id) if reason_id else None,
        notes=notes or None,
        created_by_id=user_id,
    )
    db.add(wo)
    db.flush()

    today = date.today()
    for ln in lines:
        qty = float(ln["quantity"])
        db.add(WriteOffLine(writeoff_id=wo.id, product_id=int(ln["product_id"]), quantity=qty))
        db.add(StockMovement(
            product_id=int(ln["product_id"]),
            movement_type="out",
            quantity=qty,
            date=today,
            reason=f"Списание: {ln.get('product_name', '')}".strip(": "),
            warehouse_id=wo.warehouse_id,
            notes=wo.notes,
            created_by_id=user_id,
        ))
        maybe_notify_low_stock(db, int(ln["product_id"]))

    log_action(db, "writeoff", wo.id, "created", user_id,
               f"Списание создано, позиций: {len(lines)}")
    db.commit()

    s = db.query(CompanySettings).first()
    if s and s.onec_enabled:
        push_writeoff(wo, db)

    return RedirectResponse(url="/warehouse/writeoffs/", status_code=302)


@router.get("/{writeoff_id}", response_class=HTMLResponse)
@login_required
async def writeoff_view(request: Request, writeoff_id: int, db: Session = Depends(get_db)):
    wo = db.query(WriteOff).filter(WriteOff.id == writeoff_id).first()
    if not wo:
        return RedirectResponse(url="/warehouse/writeoffs/", status_code=302)
    return templates.TemplateResponse(request, "warehouse/writeoff_view.html", {"writeoff": wo})

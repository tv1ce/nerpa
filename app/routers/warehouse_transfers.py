"""Раздел «Складское перемещение» кабинета кладовщика.

Технолог создаёт в 1С заказ на перемещение и документ «Перемещение товаров»
(непроведённый). sync_transfer_tasks_from_1c пуллит такие документы в
StockTransfer/StockTransferLine — задача кладовщику: что и куда переместить.
Кнопка «Провести перемещение» проводит документ в 1С (Posted: true) и
логирует движение в NERPA как единую запись StockMovement(transfer) со
складом-источником и складом-назначением (общий остаток по товару не
меняется — как и должно быть при перемещении внутри одной компании).
"""
from datetime import date, datetime

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.tz import now as msk_now
from app.database import get_db
from app.auth import login_required
from app.models import StockTransfer, StockMovement, CompanySettings
from app.services.onec_client import confirm_transfer, sync_transfer_tasks_from_1c
from app.utils import log_action

router = APIRouter(prefix="/warehouse/transfers", tags=["warehouse_transfers"])
templates = Jinja2Templates(directory="app/templates")


def _transfer_queue_count(db: Session) -> int:
    return db.query(StockTransfer).filter(StockTransfer.status == "pending").count()


@router.post("/sync")
@login_required
async def transfers_sync_now(request: Request, db: Session = Depends(get_db)):
    """Кладовщик жмёт «Обновить» — не ждать фоновую задачу (раз в минуту)."""
    sync_transfer_tasks_from_1c(db)
    return RedirectResponse(url="/warehouse/transfers/", status_code=302)


@router.get("/", response_class=HTMLResponse)
@login_required
async def transfers_list(request: Request, db: Session = Depends(get_db)):
    transfers = (
        db.query(StockTransfer)
        .filter(StockTransfer.status == "pending")
        .order_by(StockTransfer.planned_at.asc().nullslast())
        .all()
    )
    return templates.TemplateResponse(request, "warehouse/transfers_list.html", {
        "transfers": transfers,
        "transfer_queue_count": len(transfers),
    })


@router.get("/{transfer_id}", response_class=HTMLResponse)
@login_required
async def transfer_detail(request: Request, transfer_id: int, db: Session = Depends(get_db)):
    transfer = db.query(StockTransfer).filter(StockTransfer.id == transfer_id).first()
    if not transfer:
        return RedirectResponse(url="/warehouse/transfers/", status_code=302)
    return templates.TemplateResponse(request, "warehouse/transfer_detail.html", {
        "transfer": transfer,
    })


@router.post("/{transfer_id}/confirm")
@login_required
async def confirm(request: Request, transfer_id: int, db: Session = Depends(get_db)):
    transfer = db.query(StockTransfer).filter(StockTransfer.id == transfer_id).first()
    if not transfer or transfer.status != "pending":
        return RedirectResponse(url="/warehouse/transfers/", status_code=302)

    s = db.query(CompanySettings).first()
    if s and s.onec_enabled and transfer.external_id_1c:
        result = confirm_transfer(transfer, db)
        if not result.get("ok"):
            return templates.TemplateResponse(request, "warehouse/transfer_detail.html", {
                "transfer": transfer,
                "error": f"Не удалось провести документ в 1С: {result.get('message')}",
            })

    user_id = request.session.get("user_id")
    today = date.today()
    for ln in transfer.lines:
        if not ln.product_id or not ln.quantity:
            continue
        mv = StockMovement(
            product_id=ln.product_id,
            movement_type="transfer",
            quantity=ln.quantity,
            date=today,
            reason="Складское перемещение (1С)",
            warehouse_id=transfer.from_warehouse_id,
            to_warehouse_id=transfer.to_warehouse_id,
            created_by_id=user_id,
        )
        if transfer.external_id_1c:
            mv.external_id_1c = transfer.external_id_1c
            mv.synced_to_1c_at = msk_now()
        db.add(mv)

    transfer.status = "done"
    transfer.confirmed_at = datetime.now()
    transfer.confirmed_by_id = user_id
    log_action(db, "stock_transfer", transfer.id, "confirmed", user_id,
               "Перемещение проведено кладовщиком")
    db.commit()
    return RedirectResponse(url="/warehouse/transfers/", status_code=302)

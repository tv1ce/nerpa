"""Раздел «Поступление товаров» кабинета кладовщика.

Технолог создаёт в 1С заказ поставщику и приходную накладную (непроведённую).
sync_receiving_tasks_from_1c (см. app/services/onec_client.py) пуллит такие
документы в Receipt/ReceiptLine — задача кладовщику на приёмку: что принять,
у кого (поставщик) и к какой дате.

Кладовщик вводит фактическое количество по каждой позиции и жмёт одну из двух
кнопок:
  - «Принять»       — работает, только если факт совпал с накладной построчно;
                       тогда документ в 1С проводится (Posted: true) и в NERPA
                       создаётся приход (StockMovement.in) по каждой позиции.
  - «Расхождение»    — сохраняет введённый факт, блокирует проведение (в 1С
                       ничего не меняется) и заводит заявку на рассмотрение
                       менеджеру/технологу (Notification). Тот же экран потом
                       открывается повторно — как только цифры будут скорректированы
                       и совпадут, «Принять» проведёт документ.
"""
from datetime import date, datetime

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.tz import now as msk_now
from app.database import get_db
from app.auth import login_required
from app.models import Receipt, ReceiptLine, StockMovement, Notification, User
from app.services.onec_client import confirm_receipt, sync_receiving_tasks_from_1c
from app.services.telegram_send import notify_warehouse_group
from app.utils import log_action

router = APIRouter(prefix="/warehouse/receiving", tags=["warehouse_receiving"])
templates = Jinja2Templates(directory="app/templates")


def _receiving_queue_count(db: Session) -> int:
    return db.query(Receipt).filter(Receipt.status.in_(["pending", "discrepancy"])).count()


@router.post("/sync")
@login_required
async def receiving_sync_now(request: Request, db: Session = Depends(get_db)):
    """Кладовщик жмёт «Обновить» — не ждать фоновую задачу (раз в минуту)."""
    sync_receiving_tasks_from_1c(db)
    return RedirectResponse(url="/warehouse/receiving/", status_code=302)


@router.get("/", response_class=HTMLResponse)
@login_required
async def receiving_list(request: Request, db: Session = Depends(get_db)):
    receipts = (
        db.query(Receipt)
        .filter(Receipt.status.in_(["pending", "discrepancy"]))
        .order_by(Receipt.expected_date.asc().nullslast(), Receipt.created_at.asc())
        .all()
    )
    return templates.TemplateResponse(request, "warehouse/receiving_list.html", {
        "receipts": receipts,
        "receiving_queue_count": len(receipts),
    })


@router.get("/{receipt_id}", response_class=HTMLResponse)
@login_required
async def receiving_detail(request: Request, receipt_id: int, db: Session = Depends(get_db)):
    receipt = db.query(Receipt).filter(Receipt.id == receipt_id).first()
    if not receipt:
        return RedirectResponse(url="/warehouse/receiving/", status_code=302)
    return templates.TemplateResponse(request, "warehouse/receiving_detail.html", {
        "receipt": receipt,
    })


def _read_actual_qtys(form, receipt: Receipt) -> dict[int, float]:
    result = {}
    for ln in receipt.lines:
        raw = form.get(f"qty_{ln.id}")
        try:
            result[ln.id] = float(raw) if raw not in (None, "") else (ln.expected_qty or 0.0)
        except ValueError:
            result[ln.id] = ln.expected_qty or 0.0
    return result


@router.post("/{receipt_id}/confirm")
@login_required
async def confirm(request: Request, receipt_id: int, db: Session = Depends(get_db)):
    receipt = db.query(Receipt).filter(Receipt.id == receipt_id).first()
    if not receipt:
        return RedirectResponse(url="/warehouse/receiving/", status_code=302)

    form = await request.form()
    actual = _read_actual_qtys(form, receipt)
    for ln in receipt.lines:
        ln.actual_qty = actual.get(ln.id)

    mismatched = [ln for ln in receipt.lines if abs((ln.actual_qty or 0) - (ln.expected_qty or 0)) > 1e-9]
    if mismatched:
        # Защита от случайного проведения при расхождении, даже если нажали «Принять» —
        # тот же путь, что и явная кнопка «Расхождение».
        _flag_discrepancy(db, receipt, request.session.get("user_id"))
        db.commit()
        return RedirectResponse(url=f"/warehouse/receiving/{receipt.id}", status_code=302)

    from app.models import CompanySettings
    s = db.query(CompanySettings).first()
    if s and s.onec_enabled and receipt.external_id_1c:
        result = confirm_receipt(receipt, db)
        if not result.get("ok"):
            db.rollback()
            return templates.TemplateResponse(request, "warehouse/receiving_detail.html", {
                "receipt": receipt,
                "error": f"Не удалось провести документ в 1С: {result.get('message')}",
            })

    user_id = request.session.get("user_id")
    today = date.today()
    for ln in receipt.lines:
        if not ln.product_id or not ln.actual_qty:
            continue
        mv = StockMovement(
            product_id=ln.product_id,
            movement_type="in",
            quantity=ln.actual_qty,
            date=today,
            reason="Поступление (1С)",
            warehouse_id=receipt.warehouse_id,
            created_by_id=user_id,
        )
        # Помечаем как уже синхронизированное с этим документом — сам документ
        # 1С уже создан и проведён технологом+нами выше, повторно пушить не нужно.
        if receipt.external_id_1c:
            mv.external_id_1c = receipt.external_id_1c
            mv.synced_to_1c_at = msk_now()
        db.add(mv)

    receipt.status = "confirmed"
    receipt.confirmed_at = datetime.now()
    receipt.confirmed_by_id = user_id
    log_action(db, "receipt", receipt.id, "confirmed", user_id,
               f"Приёмка подтверждена, поставщик: {receipt.supplier.name if receipt.supplier else '—'}")
    db.commit()

    user = db.query(User).filter(User.id == user_id).first()
    items_text = "\n".join(
        f"  • {ln.product.name if ln.product else '—'} — {ln.actual_qty:g} {ln.product.unit if ln.product else ''}"
        for ln in receipt.lines if ln.product_id
    )
    notify_warehouse_group(
        db, "receiving",
        f"✅ Приход №{receipt.id} принят\n"
        f"Поставщик: {receipt.supplier.name if receipt.supplier else '—'}\n"
        f"Склад: {receipt.warehouse.name if receipt.warehouse else '—'}\n"
        f"Кладовщик: {user.full_name if user else '—'}\n"
        f"{items_text}"
    )
    return RedirectResponse(url="/warehouse/receiving/", status_code=302)


def _flag_discrepancy(db: Session, receipt: Receipt, user_id: int | None) -> None:
    receipt.status = "discrepancy"
    db.add(Notification(
        type="receipt_discrepancy",
        title=f"Расхождение при приёмке — накладная от {receipt.expected_date or '—'}",
        body=f"Поставщик: {receipt.supplier.name if receipt.supplier else '—'}. "
             f"Факт не совпадает с накладной, требуется проверка перед проведением в 1С.",
        link=f"/warehouse/receiving/{receipt.id}",
    ))
    log_action(db, "receipt", receipt.id, "discrepancy", user_id,
               "Зафиксировано расхождение при приёмке")


@router.post("/{receipt_id}/discrepancy")
@login_required
async def discrepancy(request: Request, receipt_id: int, db: Session = Depends(get_db)):
    receipt = db.query(Receipt).filter(Receipt.id == receipt_id).first()
    if not receipt:
        return RedirectResponse(url="/warehouse/receiving/", status_code=302)

    form = await request.form()
    actual = _read_actual_qtys(form, receipt)
    for ln in receipt.lines:
        ln.actual_qty = actual.get(ln.id)

    _flag_discrepancy(db, receipt, request.session.get("user_id"))
    db.commit()
    return RedirectResponse(url=f"/warehouse/receiving/{receipt.id}", status_code=302)

import json
import logging
import threading
from datetime import date
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.utils import log_action
from app.auth import login_required, role_required, safe_redirect
from app.models import Invoice, InvoiceItem, Counterparty, Order, Product, CompanySettings, Contract, Comment, AuditLog, User

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/invoices", tags=["invoices"])


def _push_bitrix_paid_bg(order_id: int) -> None:
    """Двигает сделку Bitrix24 в «Счёт оплачен» + ставит плашку «Оплачено» в фоновом потоке."""
    from app.database import SessionLocal
    from app.services.bitrix_client import push_order_event
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        company = db.query(CompanySettings).first()
        if order and company:
            push_order_event(order, company, "paid", db=db)
    except Exception as e:
        logger.error("push_bitrix_event bg %s (paid): %s", order_id, e)
    finally:
        db.close()


def _calc_totals(items: list) -> tuple[float, float]:
    """Считает subtotal и vat_amount с учётом скидки."""
    subtotal = 0.0
    vat_amount = 0.0
    for i in items:
        qty   = float(i.get("quantity", 0))
        price = float(i.get("price", 0))
        disc  = min(max(float(i.get("discount_pct", 0)), 0), 100)
        vat   = float(i.get("vat_rate", 20))
        line  = qty * price * (1 - disc / 100)
        subtotal  += line
        vat_amount += line * vat / 100
    return subtotal, vat_amount


def _add_invoice_item(db, invoice_id: int, item: dict):
    qty   = float(item.get("quantity", 0))
    price = float(item.get("price", 0))
    disc  = min(max(float(item.get("discount_pct", 0)), 0), 100)
    vat   = float(item.get("vat_rate", 20))
    amount = round(qty * price * (1 - disc / 100), 2)
    db.add(InvoiceItem(
        invoice_id=invoice_id,
        product_id=int(item["product_id"]) if item.get("product_id") else None,
        name=item.get("name", ""), quantity=qty,
        unit=item.get("unit", "шт"),
        price=price, vat_rate=vat, discount_pct=disc, amount=amount,
    ))
templates = Jinja2Templates(directory="app/templates")

INVOICE_STATUSES = {
    "draft": "Черновик",
    "issued": "Выставлен",
    "partial": "Частично оплачено",
    "paid": "Оплачен",
    "overdue": "Просрочен",
    "cancelled": "Отменён",
}


def _next_invoice_number(db: Session) -> str:
    all_numbers = db.query(Invoice.number).all()
    max_num = 0
    for (num,) in all_numbers:
        try:
            max_num = max(max_num, int(num))
        except (ValueError, TypeError):
            pass
    return str(max_num + 1)


@router.get("/", response_class=HTMLResponse)
@login_required
async def list_invoices(
    request: Request,
    q: str = "",
    status: str = "",
    date_from: str = "",
    date_to: str = "",
    counterparty_id: int = 0,
    overdue: str = "",
    paid_done: int = 0,
    db: Session = Depends(get_db),
):
    from datetime import date as _date
    today = _date.today()
    query = db.query(Invoice).join(Counterparty)
    if q:
        query = query.filter(Invoice.number.ilike(f"%{q}%") | Counterparty.name.ilike(f"%{q}%"))
    if status:
        query = query.filter(Invoice.status == status)
    if date_from:
        try:
            query = query.filter(Invoice.date >= _date.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(Invoice.date <= _date.fromisoformat(date_to))
        except ValueError:
            pass
    if counterparty_id:
        query = query.filter(Invoice.counterparty_id == counterparty_id)
    if overdue:
        query = query.filter(
            Invoice.status.in_(["issued", "overdue", "partial"]),
            Invoice.due_date < today,
            Invoice.due_date.isnot(None),
        )
    invoices = query.order_by(Invoice.date.desc(), Invoice.id.desc()).all()
    counterparties = db.query(Counterparty).filter(
        Counterparty.is_active == True
    ).order_by(Counterparty.name).all()
    return templates.TemplateResponse(request, "invoices/list.html", {
        "invoices": invoices, "q": q, "status": status, "statuses": INVOICE_STATUSES,
        "date_from": date_from, "date_to": date_to,
        "counterparty_id": counterparty_id, "overdue": overdue,
        "counterparties": counterparties, "today": today,
        "paid_done": paid_done,
    })


@router.get("/new", response_class=HTMLResponse)
@login_required
async def new_invoice(request: Request, order_id: int = 0, db: Session = Depends(get_db)):
    counterparties = db.query(Counterparty).filter(Counterparty.is_active == True).order_by(Counterparty.name).all()
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()
    contracts = db.query(Contract).filter(Contract.status.in_(["active", "draft"])).order_by(Contract.date.desc()).all()
    order = db.query(Order).filter(Order.id == order_id).first() if order_id else None
    return templates.TemplateResponse(request, "invoices/form.html", {
        "invoice": None, "order": order, "counterparties": counterparties,
        "products": products, "contracts": contracts, "statuses": INVOICE_STATUSES,
        "suggested_number": _next_invoice_number(db),
    })


@router.post("/new")
@role_required("manager")
async def create_invoice(
    request: Request,
    number: str = Form(...),
    invoice_date: str = Form(...),
    counterparty_id: int = Form(...),
    order_id: int = Form(default=0),
    contract_id: int = Form(default=0),
    status: str = Form(default="draft"),
    due_date: str = Form(default=""),
    notes: str = Form(default=""),
    items_json: str = Form(default="[]"),
    db: Session = Depends(get_db),
):
    try:
        items = json.loads(items_json)
    except (ValueError, TypeError):
        items = []
    subtotal, vat_amount = _calc_totals(items)
    invoice = Invoice(
        number=number, date=date.fromisoformat(invoice_date),
        counterparty_id=counterparty_id, order_id=order_id or None,
        contract_id=contract_id or None,
        status=status, subtotal=round(subtotal, 2), vat_amount=round(vat_amount, 2),
        total_amount=round(subtotal + vat_amount, 2),
        due_date=date.fromisoformat(due_date) if due_date else None, notes=notes,
    )
    db.add(invoice)
    db.flush()
    for item in items:
        _add_invoice_item(db, invoice.id, item)
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice.id}", status_code=302)


@router.get("/export.xlsx")
@login_required
async def export_invoices(
    request: Request,
    q: str = "", status: str = "", date_from: str = "", date_to: str = "",
    counterparty_id: int = 0, overdue: str = "",
    db: Session = Depends(get_db),
):
    from datetime import date as _date
    from app.routers.reports import _xlsx_response
    today = _date.today()
    query = db.query(Invoice).join(Counterparty)
    if q:
        query = query.filter(Invoice.number.ilike(f"%{q}%") | Counterparty.name.ilike(f"%{q}%"))
    if status:
        query = query.filter(Invoice.status == status)
    if date_from:
        try: query = query.filter(Invoice.date >= _date.fromisoformat(date_from))
        except ValueError: pass
    if date_to:
        try: query = query.filter(Invoice.date <= _date.fromisoformat(date_to))
        except ValueError: pass
    if counterparty_id:
        query = query.filter(Invoice.counterparty_id == counterparty_id)
    if overdue:
        query = query.filter(Invoice.status.in_(["issued", "overdue", "partial"]),
                             Invoice.due_date < today, Invoice.due_date.isnot(None))
    invoices = query.order_by(Invoice.date.desc(), Invoice.id.desc()).all()
    rows = [[
        inv.number,
        inv.counterparty.name if inv.counterparty else "—",
        inv.date.strftime("%d.%m.%Y") if inv.date else "",
        inv.due_date.strftime("%d.%m.%Y") if inv.due_date else "",
        round(inv.total_amount, 2),
        INVOICE_STATUSES.get(inv.status, inv.status),
        inv.paid_date.strftime("%d.%m.%Y") if inv.paid_date else "",
    ] for inv in invoices]
    return _xlsx_response(
        ["№ счёта", "Клиент", "Дата", "Оплатить до", "Сумма с НДС ₽", "Статус", "Оплачен"],
        rows, "Счета.xlsx", widths=[14, 40, 14, 14, 18, 16, 14],
    )


@router.get("/{invoice_id}", response_class=HTMLResponse)
@login_required
async def view_invoice(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        return RedirectResponse(url="/invoices", status_code=302)
    comments = db.query(Comment).filter(
        Comment.entity_type == "invoice", Comment.entity_id == invoice_id
    ).order_by(Comment.created_at).all()
    activity = db.query(AuditLog).filter(
        AuditLog.entity_type == "invoice", AuditLog.entity_id == invoice_id
    ).order_by(AuditLog.created_at.desc()).limit(30).all()
    company = db.query(CompanySettings).first()
    sbis_configured = bool(company and company.sbis_login and company.sbis_password)
    return templates.TemplateResponse(request, "invoices/detail.html", {
        "invoice": invoice, "statuses": INVOICE_STATUSES,
        "comments": comments, "activity": activity,
        "sbis_configured": sbis_configured,
    })


@router.post("/{invoice_id}/comment")
@login_required
async def add_invoice_comment(
    request: Request, invoice_id: int,
    body: str = Form(...),
    db: Session = Depends(get_db),
):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice or not body.strip():
        return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=302)
    db.add(Comment(
        body=body.strip(),
        entity_type="invoice",
        entity_id=invoice_id,
        created_by_id=request.session.get("user_id"),
    ))
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice_id}#comments", status_code=302)


@router.get("/{invoice_id}/edit", response_class=HTMLResponse)
@login_required
async def edit_invoice(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        return RedirectResponse(url="/invoices", status_code=302)
    counterparties = db.query(Counterparty).filter(Counterparty.is_active == True).order_by(Counterparty.name).all()
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()
    contracts = db.query(Contract).filter(Contract.status.in_(["active", "draft"])).order_by(Contract.date.desc()).all()
    return templates.TemplateResponse(request, "invoices/form.html", {
        "invoice": invoice, "order": invoice.order, "counterparties": counterparties,
        "products": products, "contracts": contracts, "statuses": INVOICE_STATUSES,
        "suggested_number": invoice.number,
    })


@router.post("/{invoice_id}/edit")
@role_required("manager")
async def update_invoice(
    request: Request, invoice_id: int,
    number: str = Form(...),
    invoice_date: str = Form(...),
    counterparty_id: int = Form(...),
    order_id: int = Form(default=0),
    contract_id: int = Form(default=0),
    status: str = Form(default="draft"),
    due_date: str = Form(default=""),
    notes: str = Form(default=""),
    items_json: str = Form(default="[]"),
    db: Session = Depends(get_db),
):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        return RedirectResponse(url="/invoices", status_code=302)
    try:
        items = json.loads(items_json)
    except (ValueError, TypeError):
        items = []
    subtotal, vat_amount = _calc_totals(items)
    invoice.number = number; invoice.date = date.fromisoformat(invoice_date)
    invoice.counterparty_id = counterparty_id; invoice.order_id = order_id or None
    invoice.contract_id = contract_id or None
    invoice.status = status; invoice.subtotal = round(subtotal, 2)
    invoice.vat_amount = round(vat_amount, 2); invoice.total_amount = round(subtotal + vat_amount, 2)
    invoice.due_date = date.fromisoformat(due_date) if due_date else None
    # Дата оплаты выводится из статуса, а не из ручного ввода: при «Оплачен»
    # ставим сегодня (если ещё не было), иначе очищаем. Так редактирование
    # оплаченного счёта больше не обнуляет дату оплаты.
    if status == "paid":
        if invoice.paid_date is None:
            invoice.paid_date = date.today()
        if invoice.order_id:
            from app.utils import sync_order_paid_status
            sync_order_paid_status(db, invoice.order, request.session.get("user_id"))
            if invoice.order.bitrix_deal_id:
                threading.Thread(target=_push_bitrix_paid_bg, args=(invoice.order_id,), daemon=True).start()
    else:
        invoice.paid_date = None
    invoice.notes = notes
    for item in invoice.items:
        db.delete(item)
    db.flush()
    for item in items:
        _add_invoice_item(db, invoice.id, item)
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=302)


@router.post("/{invoice_id}/status")
@role_required("manager")
async def change_status(request: Request, invoice_id: int,
                        status: str = Form(...),
                        db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if invoice:
        old_status = invoice.status
        invoice.status = status
        if status == "paid":
            # Дата оплаты = момент перевода в «Оплачен» (автоматически).
            # При повторном сохранении уже оплаченного счёта дату не трогаем.
            if old_status != "paid" or invoice.paid_date is None:
                invoice.paid_date = date.today()
            # Сумму закрываем вместе со статусом — иначе оплаченный счёт
            # остаётся долгом в дебиторке (там долг = итог минус оплачено).
            invoice.paid_amount = invoice.total_amount
            # Подтягиваем статус связанного заказа (предоплата → «Оплачен»)
            if invoice.order:
                from app.utils import sync_order_paid_status
                sync_order_paid_status(db, invoice.order, request.session.get("user_id"))
                if invoice.order.bitrix_deal_id:
                    threading.Thread(target=_push_bitrix_paid_bg, args=(invoice.order_id,), daemon=True).start()
        else:
            invoice.paid_date = None
        db.commit()
    return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=302)


@router.post("/bulk-paid")
@role_required("manager")
async def bulk_mark_paid(request: Request,
                         invoice_ids: list[str] = Form(default=[]),
                         paid_date: str = Form(default=""),
                         back: str = Form(default=""),
                         db: Session = Depends(get_db)):
    """Массовая отметка счетов оплаченными.

    Часть оплат не подхватывается автоматически из банка (платёж без узнаваемого
    назначения, оплата с чужого счёта, приход мимо интеграции), и такие счета
    приходится закрывать руками. По одному это значит зайти в каждый счёт и
    выбрать статус — на десятке счетов занятие бессмысленное.

    Дата оплаты общая на всю пачку: закрывают обычно задним числом, по выписке
    за конкретный день. Уже оплаченные и отменённые счета пропускаются."""
    ids = []
    for raw in invoice_ids:
        if str(raw).isdigit():
            ids.append(int(raw))
    if not ids:
        return RedirectResponse(url=safe_redirect(back, "/invoices/"), status_code=302)

    try:
        when = date.fromisoformat(paid_date) if paid_date else date.today()
    except ValueError:
        when = date.today()

    user_id = request.session.get("user_id")
    invoices = db.query(Invoice).filter(Invoice.id.in_(ids)).all()
    updated, order_ids = 0, []
    for invoice in invoices:
        if invoice.status in ("paid", "cancelled"):
            continue
        invoice.status = "paid"
        invoice.paid_date = when
        # Закрываем и сумму: иначе счёт считается оплаченным, но продолжает
        # висеть долгом в дебиторке и сводках по сетям, где долг это
        # total_amount - paid_amount.
        invoice.paid_amount = invoice.total_amount
        updated += 1
        log_action(db, "invoice", invoice.id, "status", user_id,
                   f"Отмечен оплаченным ({when.strftime('%d.%m.%Y')}), массово из списка счетов")
        if invoice.order:
            from app.utils import sync_order_paid_status
            sync_order_paid_status(db, invoice.order, user_id)
            if invoice.order.bitrix_deal_id:
                order_ids.append(invoice.order_id)
    db.commit()

    # Bitrix дёргаем в фоне: на пачке счетов это десятки секунд, и держать
    # менеджера на белом экране ради CRM незачем.
    for order_id in order_ids:
        threading.Thread(target=_push_bitrix_paid_bg, args=(order_id,), daemon=True).start()

    target = safe_redirect(back, "/invoices/")
    sep = "&" if "?" in target else "?"
    return RedirectResponse(url=f"{target}{sep}paid_done={updated}", status_code=302)


@router.get("/{invoice_id}/pdf")
@login_required
async def download_pdf(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        return RedirectResponse(url="/invoices", status_code=302)
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings(name="Моя компания")
    from app.utils.pdf_invoice import generate_invoice_pdf, _date_verbose
    from urllib.parse import quote
    pdf_bytes = generate_invoice_pdf(invoice, company)
    filename_ascii = f"invoice_{invoice.id}.pdf"
    filename_utf8  = f"Счет на оплату № {invoice.number} от {_date_verbose(invoice.date)}.pdf"
    cd = f"attachment; filename=\"{filename_ascii}\"; filename*=UTF-8''{quote(filename_utf8)}"
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": cd},
    )


@router.get("/{invoice_id}/offer-buyer")
@login_required
async def download_offer_buyer(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        return RedirectResponse(url="/invoices", status_code=302)
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings(name="Моя компания")
    from app.utils.pdf_offer import generate_offer_pdf
    from urllib.parse import quote
    pdf_bytes = generate_offer_pdf(invoice, company, delivery="buyer")
    date_s = invoice.date.strftime("%d.%m.%Y") if invoice.date else ""
    filename_ascii = f"offer_buyer_{invoice.id}.pdf"
    filename_utf8  = f"Счет-оферта № {invoice.number} от {date_s} (дост. покупатель).pdf"
    cd = f"attachment; filename=\"{filename_ascii}\"; filename*=UTF-8''{quote(filename_utf8)}"
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": cd})


@router.get("/{invoice_id}/offer-supplier")
@login_required
async def download_offer_supplier(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        return RedirectResponse(url="/invoices", status_code=302)
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings(name="Моя компания")
    from app.utils.pdf_offer import generate_offer_pdf
    from urllib.parse import quote
    pdf_bytes = generate_offer_pdf(invoice, company, delivery="supplier")
    date_s = invoice.date.strftime("%d.%m.%Y") if invoice.date else ""
    filename_ascii = f"offer_supplier_{invoice.id}.pdf"
    filename_utf8  = f"Счет-оферта № {invoice.number} от {date_s} (дост. поставщик).pdf"
    cd = f"attachment; filename=\"{filename_ascii}\"; filename*=UTF-8''{quote(filename_utf8)}"
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": cd})


@router.post("/{invoice_id}/delete")
@role_required("manager")
async def delete_invoice(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if invoice:
        db.delete(invoice)
        db.commit()
    return RedirectResponse(url="/invoices", status_code=302)

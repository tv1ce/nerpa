import json
from datetime import date
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import login_required
from app.models import Invoice, InvoiceItem, Counterparty, Order, Product, CompanySettings, Contract

router = APIRouter(prefix="/invoices", tags=["invoices"])
templates = Jinja2Templates(directory="app/templates")

INVOICE_STATUSES = {
    "draft": "Черновик",
    "issued": "Выставлен",
    "paid": "Оплачен",
    "overdue": "Просрочен",
    "cancelled": "Отменён",
}


def _next_invoice_number(db: Session) -> str:
    from sqlalchemy import func
    max_id = db.query(func.max(Invoice.id)).scalar() or 0
    return str(max_id + 1)


@router.get("/", response_class=HTMLResponse)
@login_required
async def list_invoices(request: Request, q: str = "", status: str = "", db: Session = Depends(get_db)):
    query = db.query(Invoice).join(Counterparty)
    if q:
        query = query.filter(Invoice.number.ilike(f"%{q}%") | Counterparty.name.ilike(f"%{q}%"))
    if status:
        query = query.filter(Invoice.status == status)
    invoices = query.order_by(Invoice.date.desc(), Invoice.id.desc()).all()
    return templates.TemplateResponse(request, "invoices/list.html", {
        "invoices": invoices, "q": q, "status": status, "statuses": INVOICE_STATUSES,
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
@login_required
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
    items = json.loads(items_json)
    subtotal = sum(float(i["quantity"]) * float(i["price"]) for i in items)
    vat_amount = sum(
        float(i["quantity"]) * float(i["price"]) * float(i.get("vat_rate", 20)) / 100
        for i in items
    )
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
        qty, price, vat = float(item["quantity"]), float(item["price"]), float(item.get("vat_rate", 20))
        db.add(InvoiceItem(
            invoice_id=invoice.id,
            product_id=int(item["product_id"]) if item.get("product_id") else None,
            name=item["name"], quantity=qty, unit=item.get("unit", "кг"),
            price=price, vat_rate=vat, amount=round(qty * price, 2),
        ))
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice.id}", status_code=302)


@router.get("/{invoice_id}", response_class=HTMLResponse)
@login_required
async def view_invoice(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        return RedirectResponse(url="/invoices", status_code=302)
    return templates.TemplateResponse(request, "invoices/detail.html", {
        "invoice": invoice, "statuses": INVOICE_STATUSES,
    })


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
@login_required
async def update_invoice(
    request: Request, invoice_id: int,
    number: str = Form(...),
    invoice_date: str = Form(...),
    counterparty_id: int = Form(...),
    order_id: int = Form(default=0),
    contract_id: int = Form(default=0),
    status: str = Form(default="draft"),
    due_date: str = Form(default=""),
    paid_date: str = Form(default=""),
    notes: str = Form(default=""),
    items_json: str = Form(default="[]"),
    db: Session = Depends(get_db),
):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        return RedirectResponse(url="/invoices", status_code=302)
    items = json.loads(items_json)
    subtotal = sum(float(i["quantity"]) * float(i["price"]) for i in items)
    vat_amount = sum(
        float(i["quantity"]) * float(i["price"]) * float(i.get("vat_rate", 20)) / 100
        for i in items
    )
    invoice.number = number; invoice.date = date.fromisoformat(invoice_date)
    invoice.counterparty_id = counterparty_id; invoice.order_id = order_id or None
    invoice.contract_id = contract_id or None
    invoice.status = status; invoice.subtotal = round(subtotal, 2)
    invoice.vat_amount = round(vat_amount, 2); invoice.total_amount = round(subtotal + vat_amount, 2)
    invoice.due_date = date.fromisoformat(due_date) if due_date else None
    invoice.paid_date = date.fromisoformat(paid_date) if paid_date else None
    invoice.notes = notes
    for item in invoice.items:
        db.delete(item)
    db.flush()
    for item in items:
        qty, price, vat = float(item["quantity"]), float(item["price"]), float(item.get("vat_rate", 20))
        db.add(InvoiceItem(
            invoice_id=invoice.id,
            product_id=int(item["product_id"]) if item.get("product_id") else None,
            name=item["name"], quantity=qty, unit=item.get("unit", "кг"),
            price=price, vat_rate=vat, amount=round(qty * price, 2),
        ))
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=302)


@router.post("/{invoice_id}/status")
@login_required
async def change_status(request: Request, invoice_id: int,
                        status: str = Form(...), paid_date: str = Form(default=""),
                        db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if invoice:
        invoice.status = status
        if status == "paid":
            invoice.paid_date = date.fromisoformat(paid_date) if paid_date else date.today()
        db.commit()
    return RedirectResponse(url=f"/invoices/{invoice_id}", status_code=302)


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


@router.get("/{invoice_id}/upd")
@login_required
async def download_upd(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        return RedirectResponse(url="/invoices", status_code=302)
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings(name="Моя компания")
    from app.utils.pdf_upd import generate_upd_pdf
    from urllib.parse import quote
    pdf_bytes = generate_upd_pdf(invoice, company)
    filename_ascii = f"upd_{invoice.id}.pdf"
    date_s = invoice.date.strftime("%d.%m.%Y") if invoice.date else ""
    filename_utf8  = f"Универсальный передаточный документ № {invoice.number} от {date_s}.pdf"
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
@login_required
async def delete_invoice(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if invoice:
        db.delete(invoice)
        db.commit()
    return RedirectResponse(url="/invoices", status_code=302)

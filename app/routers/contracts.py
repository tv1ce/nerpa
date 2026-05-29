import os
from datetime import date
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import login_required
from app.models import Contract, Counterparty, DocumentTemplate, CompanySettings

router = APIRouter(prefix="/contracts", tags=["contracts"])
templates = Jinja2Templates(directory="app/templates")

CONTRACT_STATUSES = {
    "draft": "Черновик",
    "active": "Действующий",
    "expired": "Истёк",
    "terminated": "Расторгнут",
}
GENERATED_DIR = "generated"


def _next_contract_number(db: Session) -> str:
    from sqlalchemy import func
    max_id = db.query(func.max(Contract.id)).scalar() or 0
    return str(max_id + 1)


@router.get("/", response_class=HTMLResponse)
@login_required
async def list_contracts(request: Request, q: str = "", status: str = "", db: Session = Depends(get_db)):
    query = db.query(Contract).join(Counterparty)
    if q:
        query = query.filter(Contract.number.ilike(f"%{q}%") | Counterparty.name.ilike(f"%{q}%"))
    if status:
        query = query.filter(Contract.status == status)
    contracts = query.order_by(Contract.date.desc(), Contract.id.desc()).all()
    return templates.TemplateResponse(request, "contracts/list.html", {
        "contracts": contracts, "q": q, "status": status, "statuses": CONTRACT_STATUSES,
    })


@router.get("/new", response_class=HTMLResponse)
@login_required
async def new_contract(request: Request, db: Session = Depends(get_db)):
    counterparties = db.query(Counterparty).filter(Counterparty.is_active == True).order_by(Counterparty.name).all()
    doc_templates = db.query(DocumentTemplate).filter(
        DocumentTemplate.is_active == True, DocumentTemplate.type == "contract"
    ).all()
    return templates.TemplateResponse(request, "contracts/form.html", {
        "contract": None, "counterparties": counterparties, "doc_templates": doc_templates,
        "statuses": CONTRACT_STATUSES, "suggested_number": _next_contract_number(db),
    })


@router.post("/new")
@login_required
async def create_contract(
    request: Request,
    number: str = Form(...),
    contract_date: str = Form(...),
    counterparty_id: int = Form(...),
    template_id: int = Form(default=0),
    subject: str = Form(default=""),
    status: str = Form(default="draft"),
    start_date: str = Form(default=""),
    end_date: str = Form(default=""),
    amount: str = Form(default=""),
    payment_days: str = Form(default=""),
    notes: str = Form(default=""),
    db: Session = Depends(get_db),
):
    contract = Contract(
        number=number, date=date.fromisoformat(contract_date),
        counterparty_id=counterparty_id, template_id=template_id or None,
        subject=subject, status=status,
        start_date=date.fromisoformat(start_date) if start_date else None,
        end_date=date.fromisoformat(end_date) if end_date else None,
        amount=float(amount) if amount else None,
        payment_days=int(payment_days) if payment_days else None,
        notes=notes,
    )
    db.add(contract)
    db.flush()
    if template_id:
        _generate_contract_doc(contract, db)
    db.commit()
    return RedirectResponse(url=f"/contracts/{contract.id}", status_code=302)


@router.get("/{contract_id}", response_class=HTMLResponse)
@login_required
async def view_contract(request: Request, contract_id: int, db: Session = Depends(get_db)):
    contract = db.query(Contract).filter(Contract.id == contract_id).first()
    if not contract:
        return RedirectResponse(url="/contracts", status_code=302)
    return templates.TemplateResponse(request, "contracts/detail.html", {
        "contract": contract, "statuses": CONTRACT_STATUSES,
    })


@router.get("/{contract_id}/edit", response_class=HTMLResponse)
@login_required
async def edit_contract(request: Request, contract_id: int, db: Session = Depends(get_db)):
    contract = db.query(Contract).filter(Contract.id == contract_id).first()
    if not contract:
        return RedirectResponse(url="/contracts", status_code=302)
    counterparties = db.query(Counterparty).filter(Counterparty.is_active == True).order_by(Counterparty.name).all()
    doc_templates = db.query(DocumentTemplate).filter(
        DocumentTemplate.is_active == True, DocumentTemplate.type == "contract"
    ).all()
    return templates.TemplateResponse(request, "contracts/form.html", {
        "contract": contract, "counterparties": counterparties, "doc_templates": doc_templates,
        "statuses": CONTRACT_STATUSES, "suggested_number": contract.number,
    })


@router.post("/{contract_id}/edit")
@login_required
async def update_contract(
    request: Request, contract_id: int,
    number: str = Form(...), contract_date: str = Form(...),
    counterparty_id: int = Form(...), template_id: int = Form(default=0),
    subject: str = Form(default=""), status: str = Form(default="draft"),
    start_date: str = Form(default=""), end_date: str = Form(default=""),
    amount: str = Form(default=""), payment_days: str = Form(default=""),
    notes: str = Form(default=""),
    db: Session = Depends(get_db),
):
    contract = db.query(Contract).filter(Contract.id == contract_id).first()
    if not contract:
        return RedirectResponse(url="/contracts", status_code=302)
    contract.number = number; contract.date = date.fromisoformat(contract_date)
    contract.counterparty_id = counterparty_id; contract.template_id = template_id or None
    contract.subject = subject; contract.status = status
    contract.start_date = date.fromisoformat(start_date) if start_date else None
    contract.end_date = date.fromisoformat(end_date) if end_date else None
    contract.amount = float(amount) if amount else None
    contract.payment_days = int(payment_days) if payment_days else None
    contract.notes = notes
    db.commit()
    return RedirectResponse(url=f"/contracts/{contract_id}", status_code=302)


@router.get("/{contract_id}/generate")
@login_required
async def generate_contract(request: Request, contract_id: int, db: Session = Depends(get_db)):
    contract = db.query(Contract).filter(Contract.id == contract_id).first()
    if contract and contract.template_id:
        _generate_contract_doc(contract, db)
        db.commit()
    return RedirectResponse(url=f"/contracts/{contract_id}", status_code=302)


@router.get("/{contract_id}/download")
@login_required
async def download_contract(request: Request, contract_id: int, db: Session = Depends(get_db)):
    contract = db.query(Contract).filter(Contract.id == contract_id).first()
    if not contract or not contract.file_path or not os.path.exists(contract.file_path):
        return RedirectResponse(url=f"/contracts/{contract_id}", status_code=302)
    return FileResponse(
        contract.file_path,
        filename=os.path.basename(contract.file_path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@router.post("/{contract_id}/delete")
@login_required
async def delete_contract(request: Request, contract_id: int, db: Session = Depends(get_db)):
    contract = db.query(Contract).filter(Contract.id == contract_id).first()
    if contract:
        db.delete(contract)
        db.commit()
    return RedirectResponse(url="/contracts", status_code=302)


# ── Шаблоны ──────────────────────────────────────────────────────────────────

@router.get("/templates/", response_class=HTMLResponse)
@login_required
async def list_templates(request: Request, db: Session = Depends(get_db)):
    doc_templates = db.query(DocumentTemplate).filter(DocumentTemplate.is_active == True).all()
    return templates.TemplateResponse(request, "contracts/templates_list.html", {
        "doc_templates": doc_templates,
    })


@router.post("/templates/upload")
@login_required
async def upload_template(
    request: Request,
    name: str = Form(...),
    type: str = Form(default="contract"),
    description: str = Form(default=""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    os.makedirs("document_templates", exist_ok=True)
    safe_name = file.filename.replace(" ", "_")
    file_path = os.path.join("document_templates", safe_name)
    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)
    tpl = DocumentTemplate(name=name, type=type, file_path=file_path, description=description)
    db.add(tpl)
    db.commit()
    return RedirectResponse(url="/contracts/templates/", status_code=302)


@router.post("/templates/{tpl_id}/delete")
@login_required
async def delete_template(request: Request, tpl_id: int, db: Session = Depends(get_db)):
    tpl = db.query(DocumentTemplate).filter(DocumentTemplate.id == tpl_id).first()
    if tpl:
        tpl.is_active = False
        db.commit()
    return RedirectResponse(url="/contracts/templates/", status_code=302)


def _generate_contract_doc(contract: Contract, db: Session):
    template = db.query(DocumentTemplate).filter(DocumentTemplate.id == contract.template_id).first()
    company = db.query(CompanySettings).first()
    if not template or not company or not os.path.exists(template.file_path):
        return
    os.makedirs(GENERATED_DIR, exist_ok=True)
    output_name = f"contract_{contract.number.replace('/', '-')}_{contract.id}.docx"
    output_path = os.path.join(GENERATED_DIR, output_name)
    from app.utils.doc_generator import fill_contract_template
    fill_contract_template(template.file_path, output_path, contract, company)
    contract.file_path = output_path

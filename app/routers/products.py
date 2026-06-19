from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import login_required
from app.models import Product
from app.utils import get_balances

router = APIRouter(prefix="/products", tags=["products"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
@login_required
async def list_products(request: Request, q: str = "", db: Session = Depends(get_db)):
    query = db.query(Product).filter(Product.is_active == True)
    if q:
        query = query.filter(Product.name.ilike(f"%{q}%"))
    products = query.order_by(Product.name).all()
    balances = get_balances(db)
    low_stock_ids = {
        p.id for p in products
        if (p.min_stock or 0) > 0 and balances.get(p.id, 0) <= (p.min_stock or 0)
    }
    return templates.TemplateResponse(request, "products/list.html", {
        "products": products, "q": q,
        "balances": balances, "low_stock_ids": low_stock_ids,
    })


@router.get("/new", response_class=HTMLResponse)
@login_required
async def new_product(request: Request):
    return templates.TemplateResponse(request, "products/form.html", {"product": None})


@router.post("/new")
@login_required
async def create_product(
    request: Request,
    name: str = Form(...),
    article: str = Form(default=""),
    unit: str = Form(default="шт"),
    sale_unit: str = Form(default=""),
    units_per_box: int = Form(default=1),
    price: float = Form(default=0.0),
    vat_rate: float = Form(default=20.0),
    description: str = Form(default=""),
    category: str = Form(default=""),
    min_stock: float = Form(default=0.0),
    db: Session = Depends(get_db),
):
    product = Product(name=name, article=article, unit=unit,
                      sale_unit=sale_unit or None,
                      units_per_box=max(1, units_per_box),
                      price=price, vat_rate=vat_rate, description=description,
                      category=category.strip() or None,
                      min_stock=min_stock)
    db.add(product)
    db.commit()
    return RedirectResponse(url="/products", status_code=302)


@router.get("/{product_id}/edit", response_class=HTMLResponse)
@login_required
async def edit_product(request: Request, product_id: int, db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        return RedirectResponse(url="/products", status_code=302)
    return templates.TemplateResponse(request, "products/form.html", {"product": product})


@router.post("/{product_id}/edit")
@login_required
async def update_product(
    request: Request, product_id: int,
    name: str = Form(...),
    article: str = Form(default=""),
    unit: str = Form(default="шт"),
    sale_unit: str = Form(default=""),
    units_per_box: int = Form(default=1),
    price: float = Form(default=0.0),
    vat_rate: float = Form(default=20.0),
    description: str = Form(default=""),
    category: str = Form(default=""),
    min_stock: float = Form(default=0.0),
    db: Session = Depends(get_db),
):
    product = db.query(Product).filter(Product.id == product_id).first()
    if product:
        product.name = name; product.article = article; product.unit = unit
        product.sale_unit = sale_unit or None
        product.units_per_box = max(1, units_per_box)
        product.price = price; product.vat_rate = vat_rate; product.description = description
        product.category = category.strip() or None
        product.min_stock = min_stock
        db.commit()
    return RedirectResponse(url="/products", status_code=302)


@router.post("/{product_id}/delete")
@login_required
async def delete_product(request: Request, product_id: int, db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if product:
        product.is_active = False
        db.commit()
    return RedirectResponse(url="/products", status_code=302)

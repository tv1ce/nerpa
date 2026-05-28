from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean,
    ForeignKey, Text, Date,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(100), nullable=False)
    role = Column(String(20), default="manager")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class Counterparty(Base):
    __tablename__ = "counterparties"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    short_name = Column(String(100))
    inn = Column(String(12))
    kpp = Column(String(9))
    ogrn = Column(String(15))
    legal_address = Column(String(500))
    actual_address = Column(String(500))
    phone = Column(String(50))
    email = Column(String(100))
    contact_person = Column(String(100))
    type = Column(String(20), default="client")  # client / supplier / both
    bank_name = Column(String(200))
    bank_account = Column(String(20))
    bank_bik = Column(String(9))
    bank_corr_account = Column(String(20))
    notes = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    orders = relationship("Order", back_populates="counterparty")
    invoices = relationship("Invoice", back_populates="counterparty")
    contracts = relationship("Contract", back_populates="counterparty")


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    article = Column(String(50))
    unit = Column(String(20), default="кг")
    price = Column(Float, default=0.0)
    vat_rate = Column(Float, default=20.0)
    description = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    # Склад
    min_stock = Column(Float, default=0.0)     # минимальный остаток (сигнализирует о нехватке)
    initial_stock = Column(Float, default=0.0) # начальный остаток при постановке на учёт

    order_items = relationship("OrderItem", back_populates="product")
    stock_movements = relationship("StockMovement", back_populates="product")


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    number = Column(String(50), unique=True, nullable=False)
    date = Column(Date, nullable=False)
    counterparty_id = Column(Integer, ForeignKey("counterparties.id"), nullable=False)
    status = Column(String(20), default="draft")
    # draft / confirmed / shipped / delivered / cancelled
    delivery_date = Column(Date)
    delivery_address = Column(String(500))
    notes = Column(Text)
    created_by_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, server_default=func.now())

    counterparty = relationship("Counterparty", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    invoices = relationship("Invoice", back_populates="order")
    created_by = relationship("User")

    @property
    def total_amount(self):
        return sum(i.amount for i in self.items)


class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    vat_rate = Column(Float, default=20.0)
    amount = Column(Float, nullable=False)

    order = relationship("Order", back_populates="items")
    product = relationship("Product", back_populates="order_items")


class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True, index=True)
    number = Column(String(50), unique=True, nullable=False)
    date = Column(Date, nullable=False)
    order_id = Column(Integer, ForeignKey("orders.id"))
    counterparty_id = Column(Integer, ForeignKey("counterparties.id"), nullable=False)
    status = Column(String(20), default="draft")
    # draft / issued / paid / overdue / cancelled
    subtotal = Column(Float, default=0.0)
    vat_amount = Column(Float, default=0.0)
    total_amount = Column(Float, default=0.0)
    due_date = Column(Date)
    paid_date = Column(Date)
    notes = Column(Text)
    created_at = Column(DateTime, server_default=func.now())

    counterparty = relationship("Counterparty", back_populates="invoices")
    order = relationship("Order", back_populates="invoices")
    items = relationship("InvoiceItem", back_populates="invoice", cascade="all, delete-orphan")


class InvoiceItem(Base):
    __tablename__ = "invoice_items"
    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"))
    name = Column(String(200), nullable=False)
    quantity = Column(Float, nullable=False)
    unit = Column(String(20), default="кг")
    price = Column(Float, nullable=False)
    vat_rate = Column(Float, default=20.0)
    amount = Column(Float, nullable=False)

    invoice = relationship("Invoice", back_populates="items")
    product = relationship("Product")


class Contract(Base):
    __tablename__ = "contracts"
    id = Column(Integer, primary_key=True, index=True)
    number = Column(String(50), unique=True, nullable=False)
    date = Column(Date, nullable=False)
    counterparty_id = Column(Integer, ForeignKey("counterparties.id"), nullable=False)
    template_id = Column(Integer, ForeignKey("document_templates.id"))
    subject = Column(String(500))
    status = Column(String(20), default="draft")
    # draft / active / expired / terminated
    start_date = Column(Date)
    end_date = Column(Date)
    amount = Column(Float)
    file_path = Column(String(500))
    notes = Column(Text)
    created_at = Column(DateTime, server_default=func.now())

    counterparty = relationship("Counterparty", back_populates="contracts")
    template = relationship("DocumentTemplate")


class DocumentTemplate(Base):
    __tablename__ = "document_templates"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    type = Column(String(30), default="contract")  # contract / invoice / act / other
    file_path = Column(String(500), nullable=False)
    description = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class CompanySettings(Base):
    __tablename__ = "company_settings"
    id = Column(Integer, primary_key=True)
    name = Column(String(200))
    short_name = Column(String(100))
    inn = Column(String(12))
    kpp = Column(String(9))
    ogrn = Column(String(15))
    legal_address = Column(String(500))
    actual_address = Column(String(500))
    phone = Column(String(50))
    email = Column(String(100))
    director = Column(String(100))
    director_basis = Column(String(200), default="Устава")
    accountant = Column(String(100))
    bank_name = Column(String(200))
    bank_account = Column(String(20))
    bank_bik = Column(String(9))
    bank_corr_account = Column(String(20))
    logo_path = Column(String(500))
    monthly_plan = Column(Float, default=225000.0)


class MonthlyPlan(Base):
    """Фиксированный план продаж на конкретный месяц."""
    __tablename__ = "monthly_plans"
    id = Column(Integer, primary_key=True)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)   # 1–12
    plan_amount = Column(Float, nullable=False, default=0.0)
    notes = Column(String(500))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class StockMovement(Base):
    """Движение товара на складе (приход / расход / корректировка)."""
    __tablename__ = "stock_movements"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    movement_type = Column(String(20), nullable=False)  # in / out / adjustment
    quantity = Column(Float, nullable=False)             # всегда > 0
    date = Column(Date, nullable=False)
    reason = Column(String(200))   # Поставка / Продажа / Списание / Корректировка
    order_id = Column(Integer, ForeignKey("orders.id"))
    notes = Column(Text)
    created_by_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, server_default=func.now())

    product = relationship("Product", back_populates="stock_movements")
    order = relationship("Order")
    created_by = relationship("User")

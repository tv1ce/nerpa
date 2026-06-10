from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean,
    ForeignKey, Text, Date, Index,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
from app.utils.crypto import EncryptedText


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(100), nullable=False)
    role = Column(String(20), default="manager")
    is_active = Column(Boolean, default=True)
    must_change_password = Column(Boolean, default=False)  # принудительная смена при следующем входе
    birthday = Column(Date)  # день рождения — для поздравлений на табло цеха
    created_at = Column(DateTime, server_default=func.now())


class Counterparty(Base):
    __tablename__ = "counterparties"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    trade_name = Column(String(200))
    short_name = Column(String(100))
    inn = Column(String(12))
    kpp = Column(String(9))
    ogrn = Column(String(15))
    legal_address = Column(String(500))
    actual_address = Column(String(500))
    phone = Column(String(50))
    email = Column(String(100))
    contact_person = Column(String(100))
    signatory = Column(String(100))  # Подписант в формате «Фамилия И.О.» (для ИП)
    type = Column(String(20), default="client")  # client / supplier / both
    entity_type = Column(String(10), default="ooo")  # ooo / ip / other
    bank_name = Column(String(200))
    bank_account = Column(String(20))
    bank_bik = Column(String(9))
    bank_corr_account = Column(String(20))
    notes = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    # Условия оплаты
    payment_delay_days = Column(Integer, default=2)           # дней отсрочки
    payment_delay_type = Column(String(10), default="banking") # 'banking' или 'calendar'

    # Скидка по умолчанию (подставляется в новые заказы автоматически)
    default_discount_pct = Column(Float, default=0.0)
    # CRM — категория клиента
    category = Column(String(1))          # A / B / C / None
    category_manual = Column(Boolean, default=False)  # True = вручную, не пересчитывать
    # Telegram-уведомления (для перевозчиков)
    tg_chat_id = Column(String(100))            # ID чата / группы Telegram
    tg_notify_enabled = Column(Boolean, default=False)  # вкл/выкл отправку заказов

    orders = relationship("Order", back_populates="counterparty", foreign_keys="Order.counterparty_id")
    invoices = relationship("Invoice", back_populates="counterparty")
    contracts = relationship("Contract", back_populates="counterparty")
    claims = relationship("Claim", back_populates="counterparty", order_by="Claim.date.desc()")


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    article = Column(String(50))
    unit = Column(String(20), default="шт")       # единица на складе
    sale_unit = Column(String(20))               # единица в заказах/счетах (если отличается)
    units_per_box = Column(Integer, default=1)   # сколько sale_unit в 1 складской единице
    price = Column(Float, default=0.0)
    vat_rate = Column(Float, default=20.0)
    description = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    # Склад
    min_stock = Column(Float, default=0.0)
    initial_stock = Column(Float, default=0.0)

    order_items = relationship("OrderItem", back_populates="product")
    stock_movements = relationship("StockMovement", back_populates="product")


# ── Циклы статусов заказа (зависят от типа оплаты по договору) ────────────────
# Предоплата: клиент платит → заказ падает на сборку кладовщику
ORDER_FLOW_PREPAY   = ["draft", "confirmed", "paid", "assembled", "handed", "delivered"]
# Отсрочка платежа: шаг «Оплачен» пропускается, на сборку падает после подтверждения
ORDER_FLOW_DEFERRED = ["draft", "confirmed", "assembled", "handed", "delivered"]


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    number = Column(String(50), unique=True, nullable=False)
    date = Column(Date, nullable=False)
    counterparty_id = Column(Integer, ForeignKey("counterparties.id"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("counterparties.id"), nullable=True)
    carrier_id = Column(Integer, ForeignKey("counterparties.id"), nullable=True)
    contract_id = Column(Integer, ForeignKey("contracts.id"), nullable=True)
    status = Column(String(20), default="draft")
    # draft / confirmed / paid / assembled / handed / delivered / cancelled
    payment_type = Column(String(10), default="prepay")  # prepay / deferred
    delivery_date = Column(Date)
    delivery_address = Column(String(500))
    notes = Column(Text)
    created_by_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, server_default=func.now())
    # Момент, когда кладовщик нажал «Собрано» — именно с этого момента заказ
    # считается отгруженным и попадает в табло цеха (орешки + выручка).
    assembled_at = Column(DateTime, nullable=True)
    # Доставка — детали для отправки перевозчику
    pickup_city = Column(String(100))        # город отправки
    pickup_address = Column(String(500))     # адрес забора (откуда)
    delivery_contact = Column(String(200))   # телефон + имя получателя, напр. «79119244416 Ольга»
    delivery_time = Column(String(50))       # временной слот, напр. «12-19»

    counterparty = relationship("Counterparty", back_populates="orders", foreign_keys=[counterparty_id])
    supplier = relationship("Counterparty", foreign_keys=[supplier_id])
    carrier = relationship("Counterparty", foreign_keys=[carrier_id])
    contract = relationship("Contract")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    invoices = relationship("Invoice", back_populates="order")
    created_by = relationship("User")

    @property
    def total_amount(self):
        return sum(i.amount for i in self.items)

    @property
    def is_prepay(self) -> bool:
        """True — заказ по предоплате; False — с отсрочкой платежа."""
        return (self.payment_type or "prepay") != "deferred"

    @property
    def workflow(self) -> list:
        """Применимая последовательность статусов для этого заказа."""
        return ORDER_FLOW_PREPAY if self.is_prepay else ORDER_FLOW_DEFERRED

    @property
    def ready_for_assembly(self) -> bool:
        """Заказ «падает» кладовщику на сборку:
        — по предоплате  — когда статус «Оплачен»;
        — по отсрочке    — когда статус «Подтверждён» (оплата пропускается)."""
        if self.status in ("assembled", "handed", "delivered", "cancelled"):
            return False
        return self.status == ("paid" if self.is_prepay else "confirmed")


class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    discount_pct = Column(Float, default=0.0)  # % скидки, 0-100
    vat_rate = Column(Float, default=20.0)
    amount = Column(Float, nullable=False)  # итог строки с учётом скидки

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

    contract_id = Column(Integer, ForeignKey("contracts.id"))

    counterparty = relationship("Counterparty", back_populates="invoices")
    order = relationship("Order", back_populates="invoices")
    contract = relationship("Contract")
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
    discount_pct = Column(Float, default=0.0)
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
    payment_type = Column(String(10), default="prepay")  # prepay (предоплата) / deferred (отсрочка)
    payment_days = Column(Integer)  # дней отсрочки (для шаблонов с отсрочкой)
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
    okpo = Column(String(15))
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
    brand_name = Column(String(200))                        # название бренда для табло цеха
    # ── Табло цеха (digital signage) ──
    board_nuts_plan = Column(Float, default=0.0)        # план отгрузки орешков на месяц, шт
    board_quotes = Column(Text)                         # мотивашки, по одной в строке
    board_stations = Column(Text)                       # радиостанции, "Название | URL" в строке
    board_active_station = Column(Integer, default=0)   # индекс активной станции
    board_cost_pct       = Column(Float, default=0.0)   # фактическая себестоимость %
    board_cost_norm_pct  = Column(Float, default=48.0)  # норма себестоимости %
    board_cost_deviation = Column(Float, default=5.0)   # допустимое отклонение от нормы %
    # ── Интеграция с Метафорой ──
    metafora_email    = Column(String(200))
    metafora_password = Column(EncryptedText)   # зашифровано (PIN-вход, совместимость)
    metafora_token    = Column(EncryptedText)   # Firebase ID-token (зашифровано)
    metafora_refresh  = Column(EncryptedText)   # Firebase refresh-token (зашифровано)
    metafora_app_id   = Column(String(50))      # Glide appID (автоопределяется)
    metafora_url      = Column(String(500))     # URL выгрузки
    # ── Разведка ЛПР (DaData) ──
    dadata_token  = Column(EncryptedText)       # API-ключ DaData (зашифровано)
    dadata_secret = Column(EncryptedText)       # секретный ключ cleaning API (зашифровано)
    # ── Telegram-бот ──
    tg_bot_token  = Column(EncryptedText)       # токен бота от @BotFather (зашифровано)
    tg_report_chat_ids = Column(Text)           # chat_id для отчётов (выручка), через запятую
    tg_callback_chat_ids = Column(Text)         # chat_id для напоминаний о прозвонах (отдельно от отчётов)
    tg_callback_enabled = Column(Boolean, default=True)  # вкл/выкл авторассылку напоминаний о прозвонах
    # ── KPI-фильтр продукта (дашборд и отчёты) ──
    kpi_product_filter = Column(String(100), default="орешк")  # ilike-подстрока для фильтра KPI
    # ── Пороги напоминаний (за сколько дней предупреждать), 0 = выключено ──
    notify_contract_days = Column(Integer, default=14)  # до истечения договора
    notify_invoice_days  = Column(Integer, default=3)   # до дедлайна оплаты счёта


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


class Task(Base):
    """Задача, привязанная к заказу, клиенту или рекламации."""
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    title = Column(String(300), nullable=False)
    description = Column(Text)
    status = Column(String(20), default="open")      # open / done
    priority = Column(String(10), default="normal")  # low / normal / high / urgent
    entity_type = Column(String(30))  # order / counterparty / claim
    entity_id = Column(Integer)
    assigned_to_id = Column(Integer, ForeignKey("users.id"))
    created_by_id = Column(Integer, ForeignKey("users.id"))
    due_date = Column(Date)
    created_at = Column(DateTime, server_default=func.now())

    assigned_to = relationship("User", foreign_keys=[assigned_to_id])
    created_by  = relationship("User", foreign_keys=[created_by_id])


class Comment(Base):
    """Комментарий к заказу, клиенту или рекламации."""
    __tablename__ = "comments"
    id = Column(Integer, primary_key=True)
    body = Column(Text, nullable=False)
    entity_type = Column(String(30))
    entity_id = Column(Integer)
    created_by_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    created_by = relationship("User")


class AuditLog(Base):
    """Лог изменений ключевых сущностей."""
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    entity_type = Column(String(30))    # order / counterparty / invoice / claim
    entity_id = Column(Integer)
    action = Column(String(50))         # created / updated / status_changed / deleted
    field = Column(String(100))
    old_value = Column(String(500))
    new_value = Column(String(500))
    note = Column(Text)
    user_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User")


class Claim(Base):
    """Рекламация / претензия от клиента."""
    __tablename__ = "claims"
    id = Column(Integer, primary_key=True)
    number = Column(String(50), unique=True, nullable=False)
    date = Column(Date, nullable=False)
    counterparty_id = Column(Integer, ForeignKey("counterparties.id"), nullable=False)
    order_id = Column(Integer, ForeignKey("orders.id"))
    type = Column(String(30), default="quality")   # quality / delivery / quantity / documents / other
    status = Column(String(20), default="new")     # new / in_progress / resolved / rejected
    description = Column(Text)
    resolution = Column(Text)
    amount = Column(Float)
    created_by_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    counterparty = relationship("Counterparty", back_populates="claims")
    order = relationship("Order")
    created_by = relationship("User")


class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    type = Column(String(30), default="low_stock")
    title = Column(String(200), nullable=False)
    body = Column(Text)
    link = Column(String(300))  # ссылка на объект: клик по уведомлению → переход
    product_id = Column(Integer, ForeignKey("products.id"))
    # user_id = NULL → системное уведомление, видят все (напр. low_stock склада)
    user_id = Column(Integer, ForeignKey("users.id"))
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())

    product = relationship("Product")
    user = relationship("User")


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


class SalesLead(Base):
    """Точка для прозвона отделом продаж (кофейня, кондитерская и т.п.),
    импортированная из спарсенного Excel/CSV."""
    __tablename__ = "sales_leads"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(300), nullable=False)        # название точки/заведения
    brand = Column(String(300))                       # нормализованный бренд (для группировки сетей)
    is_network = Column(Boolean, default=False)       # сетевая точка?
    network_size = Column(Integer, default=1)         # сколько точек в сети в этой загрузке
    category = Column(String(150))                    # рубрика: кофейня / кондитерская / …
    city = Column(String(150))
    district = Column(String(150))                    # район города
    address = Column(String(500))
    lat = Column(Float)                               # координаты (геокодинг)
    lng = Column(Float)
    phone = Column(String(150))
    email = Column(String(150))
    contact_person = Column(String(150))
    # Соцсети / онлайн
    website = Column(String(500))
    vk = Column(String(500))
    instagram = Column(String(500))
    telegram = Column(String(500))
    whatsapp = Column(String(500))
    # Прозвон
    call_status = Column(String(20), default="new")
    # new / callback / no_answer / interested / thinking / refused / deal / invalid
    assigned_to_id = Column(Integer, ForeignKey("users.id"))
    last_call_at = Column(DateTime)
    callback_at = Column(Date)
    call_count = Column(Integer, default=0)
    notes = Column(Text)
    # Импорт
    source_file = Column(String(300))
    raw = Column(Text)                                # JSON исходной строки (на всякий случай)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    # ── Разведка / обогащение (ЛПР, реквизиты из открытых источников РФ) ──
    inn = Column(String(12))                          # ИНН организации/ИП
    kpp = Column(String(9))
    ogrn = Column(String(15))
    company_name_full = Column(String(500))           # полное наименование с ОПФ
    director = Column(String(200))                    # ЛПР — ФИО руководителя
    director_post = Column(String(200))               # должность ЛПР
    company_status = Column(String(30))               # действующая / ликвидируется / ликвидирована
    okved = Column(String(300))                       # основной вид деятельности (код + описание)
    registration_date = Column(String(20))            # дата регистрации (как строка)
    enriched_at = Column(DateTime)                    # когда последний раз обогащали
    enrich_source = Column(String(50))                # источник: dadata / local / manual
    recon_reviewed = Column(Boolean, default=False)   # карточку разведки открывали/проверяли
    recon_reviewed_at = Column(DateTime)
    # Конвертация в контрагента
    converted_cp_id = Column(Integer, ForeignKey("counterparties.id"))

    assigned_to = relationship("User")
    converted_cp = relationship("Counterparty")
    calls = relationship("LeadCall", back_populates="lead",
                         order_by="LeadCall.created_at.desc()",
                         cascade="all, delete-orphan")


class LeadCall(Base):
    """Запись о звонке/контакте по точке прозвона (история)."""
    __tablename__ = "lead_calls"
    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("sales_leads.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"))
    status = Column(String(20))        # статус, установленный этим звонком
    comment = Column(Text)
    created_at = Column(DateTime, server_default=func.now())

    lead = relationship("SalesLead", back_populates="calls")
    user = relationship("User")


class ContactPerson(Base):
    """ЛПР — лицо, принимающее решения по точке (управляющий, закупщик, директор).

    Привязан к точке прозвона (SalesLead) и/или к контрагенту. Один и тот же ЛПР
    может вести целую сеть — поэтому это отдельная сущность, а не текстовое поле.
    Заменяет/дополняет SalesLead.contact_person и Counterparty.contact_person.
    """
    __tablename__ = "contact_persons"
    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("sales_leads.id"), index=True)
    counterparty_id = Column(Integer, ForeignKey("counterparties.id"))
    full_name = Column(String(200), nullable=False)
    post = Column(String(150))                 # должность (управляющий / закупщик / …)
    phone = Column(String(100))
    email = Column(String(150))
    telegram = Column(String(150))
    whatsapp = Column(String(150))
    is_primary = Column(Boolean, default=False)  # основной контакт точки
    notes = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    lead = relationship("SalesLead", backref="contacts")
    counterparty = relationship("Counterparty")


class FieldVisit(Base):
    """Визит торгового представителя к точке (план + факт).

    Презейл-модель: торгпред приезжает, общается с ЛПР, фиксирует результат,
    фото и следующий шаг. Результат визита переносится в SalesLead.call_status,
    а сам контакт логируется в общую ленту LeadCall — чтобы история точки была
    единой (и звонки телесейла, и визиты в полях).
    """
    __tablename__ = "field_visits"
    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("sales_leads.id"), nullable=False, index=True)
    rep_id = Column(Integer, ForeignKey("users.id"), index=True)        # торгпред
    contact_id = Column(Integer, ForeignKey("contact_persons.id"))      # с кем общались
    planned_date = Column(Date, index=True)                            # на какой день запланирован
    status = Column(String(20), default="planned")                     # planned / done / skipped
    checkin_at = Column(DateTime)                                      # фактическая отметка «начал визит»
    checkout_at = Column(DateTime)                                     # «завершил визит»
    gps_lat = Column(Float)                                            # координаты в момент чек-ина (необязательно)
    gps_lng = Column(Float)
    gps_distance_m = Column(Integer)                                   # расстояние до точки, м (для отчёта, не блокирует)
    result = Column(String(20))                                       # маппится на call_status лида
    comment = Column(Text)
    next_step = Column(Text)                                          # договорённость / следующий шаг
    next_visit_at = Column(Date)                                      # когда зайти снова
    photos = Column(Text)                                             # JSON — список путей к фото
    created_at = Column(DateTime, server_default=func.now())

    lead = relationship("SalesLead")
    rep = relationship("User")
    contact = relationship("ContactPerson")


class AttachedFile(Base):
    """Прикреплённый документ к карточке контрагента или заказа.

    Хранится вне app/static (документы приватные!) — в каталоге uploads/ в корне
    проекта, отдаётся только через защищённый роут /files/{id}/download.
    """
    __tablename__ = "attached_files"
    id = Column(Integer, primary_key=True)
    entity_type = Column(String(20), nullable=False)   # counterparty / order
    entity_id = Column(Integer, nullable=False)
    file_type = Column(String(30), default="other")
    # КА: contract / extra / other · Заказ: invoice / upd / tn / other
    original_name = Column(String(300), nullable=False)
    stored_path = Column(String(500), nullable=False)   # относительный путь от корня проекта
    size_original = Column(Integer, default=0)           # байт до сжатия
    size_compressed = Column(Integer, default=0)         # байт после сжатия (= original, если не сжимали)
    uploaded_by_id = Column(Integer, ForeignKey("users.id"))
    uploaded_at = Column(DateTime, server_default=func.now())

    uploaded_by = relationship("User")


class LogisticsCost(Base):
    """Затраты на логистику (импорт из Метафоры или ручной ввод)."""
    __tablename__ = "logistics_costs"
    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False)
    description = Column(String(500))
    amount = Column(Float, nullable=False, default=0.0)
    source = Column(String(30), default="manual")  # manual / metafora / upload
    external_id = Column(String(100))              # ID из внешней системы (для дедупликации)
    notes = Column(Text)
    created_at = Column(DateTime, server_default=func.now())


# ── Индексы для часто фильтруемых колонок ────────────────────────────────────
# SQLAlchemy создаёт их через Base.metadata.create_all(); для существующей БД
# добавляются отдельной миграцией в database._migrate_db().

Index("ix_orders_status",          Order.status)
Index("ix_orders_date",            Order.date)
Index("ix_orders_counterparty_id", Order.counterparty_id)

Index("ix_invoices_status",          Invoice.status)
Index("ix_invoices_date",            Invoice.date)
Index("ix_invoices_counterparty_id", Invoice.counterparty_id)

Index("ix_invoice_items_invoice_id", InvoiceItem.invoice_id)

Index("ix_order_items_order_id",   OrderItem.order_id)
Index("ix_order_items_product_id", OrderItem.product_id)

Index("ix_sales_leads_call_status",    SalesLead.call_status)
Index("ix_sales_leads_assigned_to_id", SalesLead.assigned_to_id)

Index("ix_audit_logs_entity", AuditLog.entity_type, AuditLog.entity_id)

Index("ix_stock_movements_product_id", StockMovement.product_id)

Index("ix_attached_files_entity", AttachedFile.entity_type, AttachedFile.entity_id)

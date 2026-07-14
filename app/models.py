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
    phone = Column(String(50))  # рабочий телефон менеджера — показывается клиенту в трекинге
    created_at = Column(DateTime, server_default=func.now())
    # Bitrix24: если задано — авто-выгруженные лиды этого торгпреда/менеджера
    # назначаются на этого сотрудника Bitrix, а не на глобального bitrix_lead_responsible_id
    bitrix_user_id = Column(String(20))


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
    # ЕГРЮЛ — кэш последней проверки статуса через DaData
    egrul_status = Column(String(30))           # ACTIVE / LIQUIDATING / LIQUIDATED / BANKRUPT / REORGANIZING
    egrul_checked_at = Column(DateTime)
    # 1С:УНФ — идентификатор объекта в 1С и дата последней синхронизации
    external_id_1c  = Column(String(36))        # Ref_Key (GUID) контрагента в 1С
    synced_to_1c_at = Column(DateTime)
    # Bitrix24 — ID компании/контакта CRM, из которых создан этот контрагент
    external_id_bitrix  = Column(String(20))
    synced_to_bitrix_at = Column(DateTime)
    # Versta24 — если True, при выборе этого контрагента перевозчиком в заказе
    # появляются поля привязки к заказу Versta (курьерская экспедиция: СДЭК, КСЭ и т.д.)
    is_versta_expeditor = Column(Boolean, default=False)

    orders = relationship("Order", back_populates="counterparty", foreign_keys="Order.counterparty_id")
    invoices = relationship("Invoice", back_populates="counterparty")
    contracts = relationship("Contract", back_populates="counterparty")
    claims = relationship("Claim", back_populates="counterparty", order_by="Claim.date.desc()")
    vehicles = relationship("CarrierVehicle", back_populates="counterparty",
                             cascade="all, delete-orphan", order_by="CarrierVehicle.id")


class CarrierVehicle(Base):
    """Водитель + ТС перевозчика. У одного перевозчика может быть несколько —
    выбираются из списка при оформлении заказа."""
    __tablename__ = "carrier_vehicles"
    id = Column(Integer, primary_key=True, index=True)
    counterparty_id = Column(Integer, ForeignKey("counterparties.id"), nullable=False)
    driver_name   = Column(String(200))
    vehicle_plate = Column(String(20))
    vehicle_type  = Column(String(100))
    # Реквизиты водителя (для ЭТрН)
    driver_inn            = Column(String(12))   # ИНН водителя
    driver_phone          = Column(String(50))   # телефон водителя
    driver_license_series = Column(String(20))   # серия вод. удостоверения
    driver_license_number = Column(String(20))   # номер вод. удостоверения
    driver_license_date   = Column(Date)         # дата выдачи вод. удостоверения
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    counterparty = relationship("Counterparty", back_populates="vehicles")


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
    category = Column(String(100))             # группа для навигации в пикере (Орешки / Упаковка / …)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    # Склад
    min_stock = Column(Float, default=0.0)
    initial_stock = Column(Float, default=0.0)
    # 1С:УНФ
    external_id_1c   = Column(String(36))       # Ref_Key (GUID) номенклатуры в 1С
    synced_from_1c_at = Column(DateTime)        # когда последний раз тянули из 1С

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
    delivery_date = Column(Date)          # «доставить до» — крайний срок доставки
    dispatch_date = Column(Date)          # дата отправления (подача ТС / погрузка)
    delivery_address = Column(String(500))
    notes = Column(Text)
    created_by_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, server_default=func.now())
    # Момент, когда кладовщик нажал «Собрано» — именно с этого момента заказ
    # считается отгруженным и попадает в табло цеха (орешки + выручка).
    assembled_at = Column(DateTime, nullable=True)
    # Момент перехода в «Передан поставщику» — дата отгрузки для отчётов.
    handed_at = Column(DateTime, nullable=True)
    # Доставка — детали для отправки перевозчику
    pickup_city = Column(String(100))        # город отправки
    pickup_address = Column(String(500))     # адрес забора (откуда)
    delivery_contact = Column(String(200))   # телефон + имя получателя, напр. «79119244416 Ольга»
    delivery_time = Column(String(50))       # временной слот, напр. «12-19»
    # Публичный токен для клиентского трекинга /track/{token} (без логина).
    # Генерируется лениво при первом запросе ссылки в карточке заказа.
    public_token = Column(String(40), unique=True, index=True)
    # Менеджер по продажам (может отличаться от создателя заказа).
    # Именно его имя и телефон видит клиент в публичной ссылке трекинга.
    sales_manager_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # 1С:УНФ
    external_id_1c  = Column(String(36))        # Ref_Key (GUID) Document_ЗаказПокупателя в 1С
    synced_to_1c_at = Column(DateTime)          # datetime последнего успешного push в 1С
    shipment_id_1c  = Column(String(36))        # Ref_Key (GUID) Document_РасходнаяНакладная в 1С (для УПД)
    # ── Доставка: транспорт и водитель (для ЭТРН) ──
    driver_name    = Column(String(200))        # ФИО водителя
    vehicle_plate  = Column(String(20))         # Гос. номер ТС, напр. «А001АА77»
    vehicle_type   = Column(String(100))        # Марка/модель ТС, напр. «ГАЗель Next»
    # ── Консолидация груза (для заказа-заявки/ЭТрН): один груз вместо списка позиций ──
    cargo_places   = Column(Integer)            # кол-во грузомест (коробок); пусто = авто из позиций
    cargo_pallets  = Column(Integer)            # кол-во паллет; пусто = не указывать
    cargo_name     = Column(String(200))        # наименование груза; пусто = дефолт из настроек
    # ── СБИС ЭПД / ЭТРН ──
    etran_id     = Column(String(100))          # ID документа в СБИС
    etran_status = Column(String(50))           # черновик / отправлен / подписан / завершён / ошибка
    etran_url    = Column(String(500))          # ссылка на документ в СБИС Online
    # ── Saby «Управление транспортом»: заказ-заявка перевозчику (ЭЗЗ) ──
    transport_order_id     = Column(String(100))   # Идентификатор документа TransportOrder в Saby
    transport_order_status = Column(String(50))    # черновик / отправлен / утверждён / отклонён / ошибка
    transport_order_url    = Column(String(500))   # ссылка на документ в кабинете Saby
    # ── СБИС ЭДО — УПД (формализованный XML из 1С), отправленный в документооборот ──
    upd_sbis_id     = Column(String(100))          # ID документа-УПД в СБИС
    upd_sbis_status = Column(String(50))           # черновик / отправлен / подписан / ошибка
    upd_sbis_url    = Column(String(500))          # ссылка на документ в СБИС Online
    # ── Bitrix24 CRM ──
    bitrix_deal_id      = Column(String(20), index=True)  # ID сделки, из которой создан заказ
    bitrix_category_id  = Column(Integer)                 # CATEGORY_ID направления (воронки) сделки
    synced_to_bitrix_at = Column(DateTime)                # datetime последнего push статуса в Bitrix24
    # ── Versta24 (экспедитор курьерских служб: СДЭК, КСЭ и т.д.) ──
    versta_courier_company = Column(String(100))    # название курьерской службы, напр. «СДЭК», «КСЭ»
    versta_order_number    = Column(String(50))      # номер заказа Versta (V24X-...) — основной ключ трекинга
    versta_tracking_number = Column(String(100))     # номер накладной у самой курьерской службы (опционально)
    versta_status_code     = Column(Integer)          # числовой код статуса из ответа Versta
    versta_status_name     = Column(String(200))      # человекочитаемое имя статуса
    versta_last_event      = Column(String(500))      # текст последнего события трекинга
    versta_synced_at       = Column(DateTime)          # когда последний раз опрашивали статус

    counterparty = relationship("Counterparty", back_populates="orders", foreign_keys=[counterparty_id])
    supplier = relationship("Counterparty", foreign_keys=[supplier_id])
    carrier = relationship("Counterparty", foreign_keys=[carrier_id])
    contract = relationship("Contract")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    invoices = relationship("Invoice", back_populates="order")
    created_by = relationship("User", foreign_keys=[created_by_id])
    sales_manager = relationship("User", foreign_keys=[sales_manager_id])

    @property
    def total_amount(self):
        return sum(i.amount for i in self.items)

    @property
    def delivery_cost(self) -> float:
        """Стоимость доставки именно этого заказа (строка логистики 'delivery')."""
        for c in self.logistics_costs:
            if c.cost_type == "delivery":
                return c.amount or 0.0
        return 0.0

    @property
    def delivery_tax_rate(self) -> float:
        """Налог на доставку этого заказа, % (вносится вручную в карточке заказа)."""
        for c in self.logistics_costs:
            if c.cost_type == "delivery":
                return c.tax_rate or 0.0
        return 0.0

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
    # draft / issued / partial / paid / overdue / cancelled
    subtotal = Column(Float, default=0.0)
    vat_amount = Column(Float, default=0.0)
    total_amount = Column(Float, default=0.0)
    paid_amount = Column(Float, default=0.0)   # фактически оплачено (сумма привязанных платежей)
    due_date = Column(Date)
    paid_date = Column(Date)
    notes = Column(Text)
    created_at = Column(DateTime, server_default=func.now())

    contract_id = Column(Integer, ForeignKey("contracts.id"))
    # 1С:УНФ
    external_id_1c  = Column(String(36))        # Ref_Key (GUID) Document_СчётНаОплатуПокупателю в 1С
    synced_to_1c_at = Column(DateTime)          # datetime последнего успешного push в 1С
    # СБИС ЭДО — счёт, отправленный в документооборот
    sbis_doc_id  = Column(String(100))          # ID документа-счёта в СБИС
    sbis_status  = Column(String(50))           # черновик / отправлен / подписан / ошибка
    sbis_url     = Column(String(500))          # ссылка на документ в СБИС Online

    counterparty = relationship("Counterparty", back_populates="invoices")
    order = relationship("Order", back_populates="invoices")
    contract = relationship("Contract")
    items = relationship("InvoiceItem", back_populates="invoice", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="invoice",
                            cascade="all, delete-orphan", order_by="Payment.date")


class Payment(Base):
    """Банковское поступление (оплата счёта). Единый журнал для всех источников —
    источник истины для суммы оплаты счёта, частичной оплаты и дедупа «Точка ↔ 1С».

    external_id — идентификатор платежа в источнике (paymentId Точки / Ref документа
    1С). Пара (source, external_id) уникальна — гарантирует идемпотентность приёма."""
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"))          # NULL = не привязан (ручной разбор)
    counterparty_id = Column(Integer, ForeignKey("counterparties.id"))
    amount = Column(Float, nullable=False)
    date = Column(Date, nullable=False)
    purpose = Column(Text)                                           # назначение платежа
    payer_inn = Column(String(12))
    payer_name = Column(String(200))
    source = Column(String(10), default="tochka")                   # tochka / 1c
    external_id = Column(String(64))                                # paymentId Точки / Ref 1С
    created_at = Column(DateTime, server_default=func.now())

    invoice = relationship("Invoice", back_populates="payments")
    counterparty = relationship("Counterparty")


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
    external_id_1c = Column(String(36))   # Ref_Key (GUID) ДоговораКонтрагента в 1С
    synced_to_1c_at = Column(DateTime)

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
    # ── Saby «Управление транспортом»: наименование груза по умолчанию для заявок/ЭТрН ──
    saby_cargo_name = Column(String(200), default="Орешки кондитерские")
    saby_unit_weight_g = Column(Float, default=20.0)  # вес единицы (шт) в граммах — для массы брутто
    # ── Пороги напоминаний (за сколько дней предупреждать), 0 = выключено ──
    notify_contract_days = Column(Integer, default=14)  # до истечения договора
    notify_invoice_days  = Column(Integer, default=3)   # до дедлайна оплаты счёта
    # ── Автобекап БД ──
    backup_enabled = Column(Boolean, default=False)     # вкл/выкл автобекап
    backup_frequency = Column(String(20), default="weekly")  # daily / weekly / monthly
    tg_backup_chat_id = Column(String(100))              # chat_id куда отправлять бекап
    # ── Интеграция 1С:УНФ ──
    onec_url      = Column(String(500))                  # http://<сервер>/hnf/odata/standard.odata
    onec_user     = Column(String(100))                  # логин пользователя 1С
    onec_password = Column(String(200))                  # пароль (зашифрован через ENCRYPT_KEY)
    onec_enabled  = Column(Boolean, default=False)       # вкл/выкл синхронизацию
    onec_hs_url   = Column(String(500))                  # база HTTP-сервиса расширения (печать/ЭДО); пусто = вывести из onec_url
    # ── Интеграция с банком «Точка» (авто-оплата счетов) ──
    tochka_token         = Column(EncryptedText)         # JWT-ключ (Bearer-токен), зашифрован
    tochka_account_id    = Column(String(64))            # accountId (счёт/БИК); пусто = автоопределение через /accounts
    tochka_customer_code = Column(String(32))            # код клиента в Точке
    tochka_enabled       = Column(Boolean, default=False)  # вкл/выкл сверку с Точкой
    tochka_webhook_url   = Column(String(500))           # публичный HTTPS-адрес подписки на вебхуки (мгновенные оплаты)
    # ── Приём документов из 1С (Счёт/УПД/XML) ──
    doc_intake_channel = Column(String(20), default="off")  # off / telegram / email / folder
    # ── СБИС (ЭДО / ЭПД / ЭТРН) ──
    sbis_login      = Column(String(200))       # логин в СБИС (email)
    sbis_password   = Column(EncryptedText)     # пароль (зашифровано)
    sbis_account_id = Column(String(100))       # идентификатор аккаунта/абонента СБИС
    # ── Модули (вкл/выкл из меню) ──
    module_leads    = Column(Boolean, default=False)  # Прозвон
    module_recon    = Column(Boolean, default=False)  # Разведка ЛПР
    module_sourcing = Column(Boolean, default=False)  # Закупки
    module_field    = Column(Boolean, default=False)  # Поле (торгпреды)
    module_hr       = Column(Boolean, default=False)  # HR-учёт
    # ── Bitrix24 CRM ──
    bitrix_webhook_url    = Column(EncryptedText)   # входящий вебхук, напр. https://x.bitrix24.ru/rest/1/xxxxx/
    bitrix_enabled        = Column(Boolean, default=False)
    bitrix_stage_paid     = Column(String(60))      # STAGE_ID сделки на «Счёт оплачен»
    bitrix_stage_shipped  = Column(String(60))      # STAGE_ID сделки на «Отгрузка» (заказ собран)
    bitrix_stage_delivered = Column(String(60))     # STAGE_ID сделки на «Доставлено» (необязательно)
    bitrix_field_paid     = Column(String(60))      # код UF-поля «Оплачено» (плашка), автосоздаётся
    bitrix_field_delivered = Column(String(60))     # код UF-поля «Доставлено» (плашка), автосоздаётся
    bitrix_alert_chat_ids = Column(Text)            # Telegram chat_id для громкого уведомления о новом заказе, через запятую
    bitrix_notify_user_ids = Column(Text)           # ID пользователей TMS для уведомления о новом заказе (через запятую); пусто = все
    # Авто-выгрузка лидов «Прозвон»/«Поле» в Bitrix24 при статусе «Договор/продажа»
    bitrix_lead_export_enabled = Column(Boolean, default=False)
    bitrix_lead_responsible_id = Column(String(20))  # ID пользователя Bitrix24 — ASSIGNED_BY_ID нового CRM-лида, если у торгпреда нет своего User.bitrix_user_id
    # Публичный URL TMS (напр. https://nuttshell.ru) — для обратной ссылки на карточку
    # точки в комментарии Bitrix-лида. Строится не из request.base_url, т.к. авто-выгрузка
    # может идти из фоновой задачи (APScheduler), где объекта Request нет.
    public_url = Column(String(300))
    # ── Versta24 (api.versta24.ru) — экспедитор курьерских служб (СДЭК, КСЭ и т.д.) ──
    versta_api_key = Column(EncryptedText)          # ключ клиента, выдаётся support@versta24.ru (зашифровано)
    versta_enabled = Column(Boolean, default=False)


class BitrixPipeline(Base):
    """Маппинг стадий Bitrix24 → события TMS для конкретного направления (воронки) сделок.

    Портал Bitrix24 обычно имеет несколько направлений (напр. «Первичные продажи»,
    «Вторичные продажи») с независимыми наборами STAGE_ID — значение стадии одной
    воронки бессмысленно/невалидно в другой. Если для CATEGORY_ID сделки есть
    строка здесь — используются её стадии (пустая стадия = событие не пушится,
    напр. «Вторичные продажи» без «Отгрузки»). Если строки нет — используются
    глобальные bitrix_stage_* из CompanySettings (направление по умолчанию)."""
    __tablename__ = "bitrix_pipelines"
    id = Column(Integer, primary_key=True)
    category_id = Column(Integer, nullable=False, unique=True)  # CATEGORY_ID в Bitrix24 (0 = общая воронка)
    name = Column(String(200))            # название направления — кэш для отображения в Настройках
    stage_paid = Column(String(60))       # STAGE_ID на «Счёт оплачен»
    stage_shipped = Column(String(60))    # STAGE_ID на «Отгрузка»; пусто — заказ на этом и заканчивается
    stage_delivered = Column(String(60))  # STAGE_ID на «Доставлено» (необязательно)
    created_at = Column(DateTime, server_default=func.now())


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
    # Момент последнего эскалирующего Telegram-напоминания (напр. для bitrix_order,
    # пока уведомление не прочитано). NULL — ещё не эскалировалось.
    escalated_at = Column(DateTime)

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
    # 1С:УНФ — только для типов 'in' (поступление) и 'adjustment' (инвентаризация)
    # Движения 'out' с order_id не пушатся — 1С создаёт их сама через заказ
    external_id_1c  = Column(String(36))
    synced_to_1c_at = Column(DateTime)

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
    # Bitrix24: авто-выгрузка при переходе в статус «Договор/продажа» (deal)
    bitrix_lead_id = Column(String(20))          # ID созданного CRM-лида в Bitrix24
    bitrix_lead_synced_at = Column(DateTime)     # когда выгружен

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
    source = Column(String(20), default="manual")        # manual / 1c — кто прикрепил файл
    external_key = Column(String(80))                    # ключ источника для идемпотентности (1С: «<ref>:<type>»)
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
    source = Column(String(30), default="manual")  # manual / metafora / upload / order
    external_id = Column(String(100))              # ID из внешней системы (для дедупликации)
    notes = Column(Text)
    # Привязка к заказу: cost_type='delivery' — довоз конкретного заказа (order_id задан),
    # 'pickup' — платный забор за день (общий, order_id пустой), 'other' — прочее.
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)
    cost_type = Column(String(20), default="other")  # delivery / pickup / other
    created_at = Column(DateTime, server_default=func.now())
    # Налог, % — вносится вручную по каждой строке (и для перевоза, и для платного забора).
    # NULL/0 = без налога. Раньше был жёстко зашит блок +6% на все суммы разом.
    tax_rate = Column(Float, default=6.0)

    order = relationship("Order", backref="logistics_costs")


class StockAdjustment(Base):
    """Сессия инвентаризации: снимок фактических остатков по складу на дату.

    Хранит «шапку» (дата, кто провёл, причина) и строки (StockAdjustmentLine)
    с парами «ожидалось / факт». Сами корректировки остатка применяются как
    StockMovement(adjustment) — чтобы _get_balances оставался единым источником
    истины по остаткам.
    """
    __tablename__ = "stock_adjustments"
    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False)
    reason = Column(String(200), default="Инвентаризация")
    note = Column(Text)
    created_by_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, server_default=func.now())

    created_by = relationship("User")
    lines = relationship("StockAdjustmentLine", back_populates="adjustment",
                         cascade="all, delete-orphan")

    @property
    def total_diff_count(self) -> int:
        """Сколько позиций с расхождением (факт ≠ ожидалось)."""
        return sum(1 for ln in self.lines if abs(ln.diff) > 1e-9)


class StockAdjustmentLine(Base):
    """Строка инвентаризации: ожидалось / факт по одному товару."""
    __tablename__ = "stock_adjustment_lines"
    id = Column(Integer, primary_key=True)
    adjustment_id = Column(Integer, ForeignKey("stock_adjustments.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    expected_qty = Column(Float, default=0.0)   # остаток в системе на момент инвентаризации
    actual_qty = Column(Float, default=0.0)     # введённый фактический остаток

    adjustment = relationship("StockAdjustment", back_populates="lines")
    product = relationship("Product")

    @property
    def diff(self) -> float:
        """Разница факт − ожидалось (+ излишек, − недостача)."""
        return round((self.actual_qty or 0) - (self.expected_qty or 0), 3)


# ── Индексы для часто фильтруемых колонок ────────────────────────────────────
# SQLAlchemy создаёт их через Base.metadata.create_all(); для существующей БД
# добавляются отдельной миграцией в database._migrate_db().

Index("ix_orders_status",          Order.status)
# ── Закупки / сравнение поставщиков ──────────────────────────────────────────
class ProcurementCategory(Base):
    """Направление закупки: «Типография», «Брендированные пакеты» и т.п."""
    __tablename__ = "procurement_categories"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    vendors = relationship("Vendor", back_populates="category")
    requests = relationship("SourcingRequest", back_populates="category")


class Vendor(Base):
    """Поставщик-кандидат для закупочного ресёрча (ещё не контрагент в 1С)."""
    __tablename__ = "vendors"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    website = Column(String(300))
    phone = Column(String(50))
    email = Column(String(100))
    contact_person = Column(String(100))
    region = Column(String(100))
    category_id = Column(Integer, ForeignKey("procurement_categories.id"), nullable=True)
    status = Column(String(20), default="new")   # new / in_progress / approved / rejected
    rating_avg = Column(Float, default=0.0)       # кэш средней оценки по предложениям
    notes = Column(Text)
    counterparty_id = Column(Integer, ForeignKey("counterparties.id"), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    category = relationship("ProcurementCategory", back_populates="vendors")
    counterparty = relationship("Counterparty", foreign_keys=[counterparty_id])
    quotes = relationship("VendorQuote", back_populates="vendor", cascade="all, delete-orphan")


class SourcingRequest(Base):
    """Запрос на сравнение: конкретная потребность, под которую собираем предложения."""
    __tablename__ = "sourcing_requests"
    id = Column(Integer, primary_key=True, index=True)
    number = Column(String(30))                    # «ЗАК-2026-0001»
    title = Column(String(300), nullable=False)
    category_id = Column(Integer, ForeignKey("procurement_categories.id"), nullable=True)
    description = Column(Text)
    status = Column(String(20), default="open")    # open / decided / closed
    decided_vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True)
    # Веса критериев (нормализуются при расчёте балла)
    weight_price = Column(Float, default=0.5)
    weight_term = Column(Float, default=0.25)
    weight_quality = Column(Float, default=0.25)
    created_at = Column(DateTime, server_default=func.now())
    created_by_id = Column(Integer, ForeignKey("users.id"))

    category = relationship("ProcurementCategory", back_populates="requests")
    decided_vendor = relationship("Vendor", foreign_keys=[decided_vendor_id])
    quotes = relationship("VendorQuote", back_populates="request",
                          foreign_keys="VendorQuote.request_id",
                          cascade="all, delete-orphan")


class VendorQuote(Base):
    """Предложение конкретного поставщика под конкретный запрос."""
    __tablename__ = "vendor_quotes"
    id = Column(Integer, primary_key=True, index=True)
    request_id = Column(Integer, ForeignKey("sourcing_requests.id"), nullable=False)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=False)
    price = Column(Float, default=0.0)
    term_days = Column(Integer, default=0)
    quality = Column(Integer, default=3)           # 1..5
    payment_terms = Column(String(100))
    min_batch = Column(Integer, nullable=True)
    comment = Column(Text)
    score = Column(Float, default=0.0)             # кэш взвешенного балла 0..10
    created_at = Column(DateTime, server_default=func.now())

    request = relationship("SourcingRequest", back_populates="quotes", foreign_keys=[request_id])
    vendor = relationship("Vendor", back_populates="quotes")


# ── HR-учёт (ручной ввод + сбор через опросы; форма по образцу статьи Teamly) ─

# Разделы HR-учёта — соответствуют вкладкам статьи Teamly «Отчётность HR»:
#   personal       — Личностный профиль сотрудника (2 текстовых вопроса)
#   complaints     — «С какой дичью вам приходится сталкиваться каждый день?» (1 вопрос)
#   achievements   — Достижения (свободный текст/список)
#   enps           — eNPS: оценка 0-10 + комментарий
#   enps_managers  — eNPS Руководителей: оценка 0-10 + комментарий
#   metrics        — Метрика сотрудников (свободный текст)
#   gravity        — Гравитация и антигравитация (свободный текст)
HR_SECTIONS = (
    "personal", "complaints", "achievements",
    "enps", "enps_managers", "metrics", "gravity",
)

# Вид периода сбора: весь месяц или половина месяца (двухнедельный сбор).
#   month — весь месяц; h1 — 1–15; h2 — 16–конец
HR_PERIOD_KINDS = ("month", "h1", "h2")
HR_PERIOD_KIND_LABELS = {"month": "весь месяц", "h1": "1–15", "h2": "16–конец"}


class HrPosition(Base):
    """Должность (справочник). Позволяет отключить отдельные разделы отчёта для
    должности — напр. «Метрика сотрудников» и eNPS руководителя нужны не всем."""
    __tablename__ = "hr_positions"
    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    is_active = Column(Boolean, default=True)
    disabled_sections = Column(Text)  # CSV кодов разделов, выключенных для должности
    # Вопросы «Личностного профиля» именно для этой должности — по одному на строку
    # (в Teamly вопросы подбираются под чувствительности роли: РОПу — про влияние
    # и ресурсы, кондитеру — про процессы). Пусто = общие вопросы по умолчанию.
    personal_questions = Column(Text)
    created_at = Column(DateTime, server_default=func.now())

    employees = relationship("HrEmployee", back_populates="position_ref")

    @property
    def disabled_set(self) -> set[str]:
        return {s.strip() for s in (self.disabled_sections or "").split(",") if s.strip()}

    @property
    def enabled_sections(self) -> list[str]:
        off = self.disabled_set
        return [s for s in HR_SECTIONS if s not in off]

    @property
    def personal_question_list(self) -> list[str]:
        return [q.strip() for q in (self.personal_questions or "").splitlines() if q.strip()]


class HrEmployee(Base):
    """Сотрудник для HR-учёта (не обязательно имеет логин в TMS)."""
    __tablename__ = "hr_employees"
    id = Column(Integer, primary_key=True)
    full_name = Column(String(200), nullable=False)
    position = Column(String(200))                                    # legacy: текст должности
    position_id = Column(Integer, ForeignKey("hr_positions.id"))      # ссылка на справочник должностей
    is_active = Column(Boolean, default=True)
    # Месяц (1-е число), с которого сотрудник снят с учёта — сохраняется при деактивации,
    # чтобы прошлые периоды по-прежнему его показывали, а новые — нет. NULL — не увольнялся.
    deactivated_at = Column(Date)
    created_at = Column(DateTime, server_default=func.now())

    records = relationship("HrRecord", back_populates="employee")
    position_ref = relationship("HrPosition", back_populates="employees")

    @property
    def position_title(self) -> str:
        if self.position_ref:
            return self.position_ref.title
        return self.position or ""

    @property
    def enabled_sections(self) -> list[str]:
        """Разделы, применимые к сотруднику с учётом его должности."""
        if self.position_ref:
            return self.position_ref.enabled_sections
        return list(HR_SECTIONS)

    def visible_in_period(self, period_date) -> bool:
        """Виден ли сотрудник в списке за данный месяц: активные — всегда,
        уволенные — только в периодах до месяца увольнения включительно."""
        if self.is_active:
            return True
        return self.deactivated_at is not None and period_date <= self.deactivated_at


class HrSurvey(Base):
    """Раунд-рассылка опроса за период: HR выбирает разделы и сотрудников,
    система выдаёт персональные токен-ссылки для сбора ответов без входа в TMS."""
    __tablename__ = "hr_surveys"
    id = Column(Integer, primary_key=True)
    title = Column(String(200))
    period = Column(Date, nullable=False)     # месяц опроса (хранится 1-м числом)
    period_kind = Column(String(8), default="month")  # month / h1 / h2 (двухнедельный сбор)
    sections = Column(Text, nullable=False)   # CSV кодов разделов этого раунда
    is_open = Column(Boolean, default=True)   # принимаются ли ещё ответы
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, server_default=func.now())

    tokens = relationship("HrSurveyToken", back_populates="survey", cascade="all, delete-orphan")

    @property
    def section_list(self) -> list[str]:
        return [s.strip() for s in (self.sections or "").split(",") if s.strip()]


class HrSurveyToken(Base):
    """Персональная ссылка сотрудника в рамках раунда опроса."""
    __tablename__ = "hr_survey_tokens"
    id = Column(Integer, primary_key=True)
    survey_id = Column(Integer, ForeignKey("hr_surveys.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("hr_employees.id"), nullable=False)
    token = Column(String(64), unique=True, nullable=False)
    submitted_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now())

    survey = relationship("HrSurvey", back_populates="tokens")
    employee = relationship("HrEmployee")

    @property
    def effective_sections(self) -> list[str]:
        """Разделы раунда, применимые к должности сотрудника."""
        enabled = set(self.employee.enabled_sections) if self.employee else set(HR_SECTIONS)
        return [s for s in self.survey.section_list if s in enabled]


class HrRecord(Base):
    """Одна запись HR-учёта за период (месяц) по одному сотруднику.

    Разделы имеют разную форму — под это переиспользуются общие поля:
    text_1/text_2 — тексты ответов (для personal используются оба, для
    остальных текстовых разделов — только text_1); score — только для eNPS."""
    __tablename__ = "hr_records"
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("hr_employees.id"), nullable=False)
    section = Column(String(20), nullable=False)  # см. HR_SECTIONS
    period = Column(Date, nullable=False)          # месяц записи (хранится 1-м числом)
    period_kind = Column(String(8), default="month")  # month / h1 / h2 (см. HR_PERIOD_KINDS)
    text_1 = Column(Text)
    text_2 = Column(Text)
    score = Column(Integer)   # eNPS: 0-10
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    employee = relationship("HrEmployee", back_populates="records")
    author = relationship("User")


class HrVacancy(Base):
    """Вакансия — срок закрытия (не привязана к конкретному сотруднику)."""
    __tablename__ = "hr_vacancies"
    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    opened_at = Column(Date)
    closed_at = Column(Date)
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, server_default=func.now())


class HrEmployeeInsight(Base):
    """ИИ-анализ динамики сотрудника (OpenRouter) по накопленным ежемесячным
    ответам — хранится историей, чтобы видеть, как менялись выводы."""
    __tablename__ = "hr_employee_insights"
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("hr_employees.id"), nullable=False)
    text = Column(Text, nullable=False)
    model = Column(String(100))     # какая модель сгенерировала
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, server_default=func.now())

    employee = relationship("HrEmployee")


Index("ix_hr_employee_insights_employee_id", HrEmployeeInsight.employee_id)
Index("ix_hr_records_employee_id", HrRecord.employee_id)
Index("ix_hr_records_section",     HrRecord.section)
Index("ix_hr_records_period",      HrRecord.period)
Index("ix_hr_employees_position_id",  HrEmployee.position_id)
Index("ix_hr_survey_tokens_survey_id", HrSurveyToken.survey_id)
Index("ix_hr_survey_tokens_token",     HrSurveyToken.token)

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

Index("ix_vendor_quotes_request_id", VendorQuote.request_id)
Index("ix_vendor_quotes_vendor_id",  VendorQuote.vendor_id)
Index("ix_vendors_category_id",      Vendor.category_id)
Index("ix_sourcing_requests_status", SourcingRequest.status)

Index("ix_stock_adj_lines_adjustment", StockAdjustmentLine.adjustment_id)

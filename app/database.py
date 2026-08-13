import os
import bcrypt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./tms.db")


def _set_wal(connection, _):
    """WAL-mode + оптимизация для двух процессов (бот + сервер) на одном SQLite файле.
    WAL позволяет читателям не блокировать писателей. busy_timeout даёт 10с на retry
    вместо немедленного SQLITE_BUSY. synchronous=NORMAL безопасен с WAL и быстрее FULL."""
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA cache_size=-8000")       # 8 MB page cache
    connection.execute("PRAGMA wal_autocheckpoint=100") # checkpoint каждые 100 страниц

    # SQLite's built-in lower() (использует его ILIKE/LIKE-поиск) казуфолдит
    # только ASCII — «Кофе».ilike("%кофе%") не совпадёт. Подменяем на
    # Unicode-aware str.lower(), чтобы регистр не влиял на поиск по кириллице.
    connection.create_function("lower", 1, lambda s: s.lower() if s is not None else None)


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

from sqlalchemy import event
event.listen(engine, "connect", _set_wal)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _db_file_path() -> str:
    """Абсолютный путь к файлу SQLite-БД из DATABASE_URL."""
    if DATABASE_URL.startswith("sqlite:///"):
        raw = DATABASE_URL[len("sqlite:///"):]
    else:
        raw = "tms.db"
    return os.path.abspath(raw)


def make_backup_copy(dest_path: str | None = None) -> str:
    """Создаёт целостную резервную копию БД через `VACUUM INTO`.

    В отличие от копирования файла tms.db, VACUUM INTO выгружает полностью
    согласованный снимок со всеми данными WAL — даже если checkpoint не
    срабатывал (активные соединения приложения этому мешают). Использует
    отдельное подключение sqlite3, чтобы не трогать пул SQLAlchemy.

    Возвращает путь к созданному файлу-копии. Если dest_path не задан —
    создаётся временный файл (вызывающий код обязан его удалить)."""
    import sqlite3
    import tempfile

    src = _db_file_path()
    if not os.path.exists(src):
        raise FileNotFoundError(f"База данных не найдена: {src}")

    if dest_path is None:
        fd, dest_path = tempfile.mkstemp(prefix="tms-backup-", suffix=".db")
        os.close(fd)
    # VACUUM INTO требует, чтобы целевой файл не существовал
    if os.path.exists(dest_path):
        os.remove(dest_path)

    conn = sqlite3.connect(src, timeout=15)
    try:
        conn.execute("VACUUM INTO ?", (dest_path,))
    finally:
        conn.close()
    return dest_path


class Base(DeclarativeBase):
    pass


# ── Хэширование паролей через bcrypt напрямую ────────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from app import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _migrate_db()
    _seed_defaults()


def _migrate_db():
    """Добавляет новые колонки в существующие таблицы (SQLite не поддерживает ALTER COLUMN).
    Использует тот же SQLAlchemy engine — без второго соединения."""
    conn = engine.raw_connection()
    cur = conn.cursor()
    migrations = [
        ("company_settings", "monthly_plan", "REAL DEFAULT 225000.0"),
        ("company_settings", "logo_path",    "TEXT"),
        ("products", "min_stock",     "REAL DEFAULT 0.0"),
        ("products", "initial_stock", "REAL DEFAULT 0.0"),
        ("invoices",  "contract_id",  "INTEGER REFERENCES contracts(id)"),
        ("counterparties", "trade_name", "TEXT"),
        ("counterparties", "payment_delay_days", "INTEGER DEFAULT 2"),
        ("counterparties", "payment_delay_type", "TEXT DEFAULT 'banking'"),
        ("counterparties", "category",        "TEXT"),
        ("counterparties", "category_manual", "INTEGER DEFAULT 0"),
        ("counterparties", "entity_type",     "TEXT DEFAULT 'ooo'"),
        ("order_items",    "discount_pct",    "REAL DEFAULT 0.0"),
        ("company_settings", "okpo",          "TEXT"),
        ("contracts",      "payment_days",    "INTEGER"),
        ("contracts",      "payment_type",    "TEXT DEFAULT 'prepay'"),
        ("orders",         "payment_type",    "TEXT DEFAULT 'prepay'"),
        ("orders",         "contract_id",     "INTEGER REFERENCES contracts(id)"),
        ("company_settings", "board_nuts_plan",      "REAL DEFAULT 0.0"),
        ("company_settings", "board_quotes",         "TEXT"),
        ("company_settings", "board_stations",       "TEXT"),
        ("company_settings", "board_active_station", "INTEGER DEFAULT 0"),
        ("company_settings", "brand_name",           "TEXT"),
        ("orders", "supplier_id", "INTEGER REFERENCES counterparties(id)"),
        ("orders", "carrier_id",  "INTEGER REFERENCES counterparties(id)"),
        ("counterparties", "signatory", "TEXT"),
        ("company_settings", "metafora_email",    "TEXT"),
        ("company_settings", "metafora_password", "TEXT"),
        ("company_settings", "metafora_token",    "TEXT"),
        ("company_settings", "metafora_refresh",  "TEXT"),
        ("company_settings", "metafora_app_id",   "TEXT"),
        ("company_settings", "metafora_url",      "TEXT"),
        ("products", "sale_unit", "TEXT"),
        ("products", "units_per_box", "INTEGER DEFAULT 1"),
        ("invoice_items", "discount_pct", "REAL DEFAULT 0.0"),
        ("company_settings", "board_cost_pct",       "REAL DEFAULT 0.0"),
        ("company_settings", "board_cost_norm_pct",  "REAL DEFAULT 48.0"),
        ("company_settings", "board_cost_deviation", "REAL DEFAULT 5.0"),
        ("sales_leads", "district",         "TEXT"),
        ("sales_leads", "lat",              "REAL"),
        ("sales_leads", "lng",              "REAL"),
        ("sales_leads", "converted_cp_id", "INTEGER REFERENCES counterparties(id)"),
        # Разведка ЛПР по точкам прозвона
        ("sales_leads", "inn",               "TEXT"),
        ("sales_leads", "kpp",               "TEXT"),
        ("sales_leads", "ogrn",              "TEXT"),
        ("sales_leads", "company_name_full", "TEXT"),
        ("sales_leads", "director",          "TEXT"),
        ("sales_leads", "director_post",     "TEXT"),
        ("sales_leads", "company_status",    "TEXT"),
        ("sales_leads", "okved",             "TEXT"),
        ("sales_leads", "registration_date", "TEXT"),
        ("sales_leads", "enriched_at",       "TIMESTAMP"),
        ("sales_leads", "enrich_source",     "TEXT"),
        ("sales_leads", "recon_reviewed",    "INTEGER DEFAULT 0"),
        ("sales_leads", "recon_reviewed_at", "TIMESTAMP"),
        ("company_settings", "dadata_token",  "TEXT"),
        ("company_settings", "dadata_secret", "TEXT"),
        # Поля доставки для отправки перевозчику через Telegram
        ("orders", "pickup_city",      "TEXT"),
        ("orders", "pickup_address",   "TEXT"),
        ("orders", "delivery_contact", "TEXT"),
        ("orders", "delivery_time",    "TEXT"),
        # Telegram-настройки перевозчика
        ("counterparties", "tg_chat_id",        "TEXT"),
        ("counterparties", "tg_notify_enabled", "INTEGER DEFAULT 0"),
        # Токен Telegram-бота (глобальные настройки)
        ("company_settings", "tg_bot_token", "TEXT"),
        # Получатели отчётов о выручке (chat_id через запятую)
        ("company_settings", "tg_report_chat_ids", "TEXT"),
        # Принудительная смена пароля при следующем входе
        ("users", "must_change_password", "INTEGER DEFAULT 0"),
        # Адресат уведомления (NULL = системное, видят все)
        ("notifications", "user_id", "INTEGER REFERENCES users(id)"),
        # KPI-фильтр продукта для дашборда и отчётов
        ("company_settings", "kpi_product_filter", "TEXT DEFAULT 'орешк'"),
        # Скидка по умолчанию для контрагента (подставляется в новые заказы)
        ("counterparties", "default_discount_pct", "REAL DEFAULT 0.0"),
        # День рождения сотрудника (для поздравлений на табло цеха)
        ("users", "birthday", "DATE"),
        # Ссылка на объект в уведомлении (клик → переход на страницу)
        ("notifications", "link", "TEXT"),
        # Пороги напоминаний (за сколько дней предупреждать), 0 = выключено
        ("company_settings", "notify_contract_days", "INTEGER DEFAULT 14"),
        ("company_settings", "notify_invoice_days",  "INTEGER DEFAULT 3"),
        # Напоминания о прозвонах — отдельный чат и флаг вкл/выкл
        ("company_settings", "tg_callback_chat_ids", "TEXT"),
        ("company_settings", "tg_callback_enabled",  "INTEGER DEFAULT 1"),
        # Момент сборки заказа (нажатие «Собрано») — для учёта отгрузки на табло
        ("orders", "assembled_at", "TIMESTAMP"),
        # Публичный токен клиентского трекинга /track/{token}
        ("orders", "public_token", "TEXT"),
        # ЕГРЮЛ — кэш статуса из DaData
        ("counterparties", "egrul_status",     "TEXT"),
        ("counterparties", "egrul_checked_at", "TIMESTAMP"),
        # Автобекап БД в Telegram
        ("company_settings", "backup_enabled",     "INTEGER DEFAULT 0"),
        ("company_settings", "backup_frequency",   "TEXT DEFAULT 'weekly'"),
        ("company_settings", "tg_backup_chat_id",  "TEXT"),
        # Категория товара для группировки в пикере
        ("products", "category", "TEXT"),
        # ── Интеграция 1С:УНФ ────────────────────────────────────────────────
        # external_id_1c — GUID объекта в 1С (заполняется при первом push/pull)
        # synced_*_at    — datetime последней успешной синхронизации
        ("counterparties",  "external_id_1c",    "TEXT"),
        ("counterparties",  "synced_to_1c_at",   "TIMESTAMP"),
        ("products",        "external_id_1c",    "TEXT"),
        ("products",        "synced_from_1c_at", "TIMESTAMP"),
        ("orders",          "external_id_1c",    "TEXT"),
        ("orders",          "synced_to_1c_at",   "TIMESTAMP"),
        ("invoices",        "external_id_1c",    "TEXT"),
        ("invoices",        "synced_to_1c_at",   "TIMESTAMP"),
        ("contracts",       "external_id_1c",    "TEXT"),
        ("contracts",       "synced_to_1c_at",   "TIMESTAMP"),
        ("stock_movements", "external_id_1c",    "TEXT"),
        ("stock_movements", "synced_to_1c_at",   "TIMESTAMP"),
        # Настройки подключения к 1С (URL OData, учётные данные, вкл/выкл)
        ("company_settings", "onec_url",      "TEXT"),
        ("company_settings", "onec_user",     "TEXT"),
        ("company_settings", "onec_password", "TEXT"),
        ("company_settings", "onec_enabled",  "INTEGER DEFAULT 0"),
        ("company_settings", "onec_hs_url",   "TEXT"),
        # ── Интеграция с банком «Точка» ──
        ("invoices",         "paid_amount",          "REAL DEFAULT 0.0"),
        ("company_settings", "tochka_token",         "TEXT"),
        ("company_settings", "tochka_account_id",    "TEXT"),
        ("company_settings", "tochka_customer_code", "TEXT"),
        ("company_settings", "tochka_enabled",       "INTEGER DEFAULT 0"),
        ("company_settings", "tochka_webhook_url",   "TEXT"),
        ("attached_files",   "source",        "TEXT DEFAULT 'manual'"),
        ("attached_files",   "external_key",  "TEXT"),
        ("orders",           "shipment_id_1c", "TEXT"),
        ("company_settings", "doc_intake_channel", "TEXT DEFAULT 'off'"),
        # Телефон менеджера — отображается клиенту в публичной ссылке трекинга
        ("users", "phone", "TEXT"),
        # Менеджер по продажам заказа (может отличаться от создателя)
        ("orders", "sales_manager_id", "INTEGER REFERENCES users(id)"),
        # Транспорт и водитель для ЭТРН
        ("orders", "driver_name",   "TEXT"),
        ("orders", "vehicle_plate", "TEXT"),
        ("orders", "vehicle_type",  "TEXT"),
        # СБИС ЭПД / ЭТРН
        ("orders", "etran_id",     "TEXT"),
        ("orders", "etran_status", "TEXT"),
        ("orders", "etran_url",    "TEXT"),
        # СБИС настройки компании
        ("company_settings", "sbis_login",      "TEXT"),
        ("company_settings", "sbis_password",   "TEXT"),
        ("company_settings", "sbis_account_id", "TEXT"),
        # Модули (вкл/выкл в меню)
        ("company_settings", "module_leads",    "INTEGER DEFAULT 0"),
        ("company_settings", "module_recon",    "INTEGER DEFAULT 0"),
        ("company_settings", "module_sourcing", "INTEGER DEFAULT 0"),
        ("company_settings", "module_field",    "INTEGER DEFAULT 0"),
        # ── Интеграция Bitrix24 CRM ────────────────────────────────────────────
        ("counterparties", "external_id_bitrix",  "TEXT"),
        ("counterparties", "synced_to_bitrix_at", "TIMESTAMP"),
        ("orders",         "bitrix_deal_id",      "TEXT"),
        ("orders",         "synced_to_bitrix_at", "TIMESTAMP"),
        ("company_settings", "bitrix_webhook_url",     "TEXT"),
        ("company_settings", "bitrix_enabled",          "INTEGER DEFAULT 0"),
        ("company_settings", "bitrix_stage_paid",       "TEXT"),
        ("company_settings", "bitrix_stage_shipped",    "TEXT"),
        ("company_settings", "bitrix_stage_delivered",  "TEXT"),
        ("company_settings", "bitrix_field_paid",       "TEXT"),
        ("company_settings", "bitrix_field_delivered",  "TEXT"),
        ("company_settings", "bitrix_alert_chat_ids",   "TEXT"),
        ("company_settings", "bitrix_notify_user_ids",  "TEXT"),
        ("notifications",    "escalated_at",            "TIMESTAMP"),
        ("orders",           "bitrix_category_id",      "INTEGER"),
        # ── HR-учёт ──
        ("company_settings", "module_hr", "INTEGER DEFAULT 0"),
        ("hr_employees", "position_id", "INTEGER REFERENCES hr_positions(id)"),
        ("hr_records", "period_kind", "TEXT DEFAULT 'month'"),
        ("hr_surveys", "period_kind", "TEXT DEFAULT 'month'"),
        ("hr_employees", "deactivated_at", "DATE"),
        ("hr_positions", "personal_questions", "TEXT"),
        # Вопрос опросника адресуется должности (NULL — вопрос для всех)
        ("hr_questions", "position_id", "INTEGER REFERENCES hr_positions(id)"),
        # ── Bitrix24: авто-выгрузка лидов «Прозвон»/«Поле» при статусе «Договор/продажа» ──
        ("company_settings", "bitrix_lead_responsible_id", "TEXT"),
        ("company_settings", "bitrix_lead_export_enabled", "INTEGER DEFAULT 0"),
        ("sales_leads", "bitrix_lead_id", "TEXT"),
        ("sales_leads", "bitrix_lead_synced_at", "TIMESTAMP"),
        ("users", "bitrix_user_id", "TEXT"),
        ("company_settings", "public_url", "TEXT"),
        # ── Saby «Управление транспортом»: заказ-заявка перевозчику (ЭЗЗ) ──
        ("orders", "transport_order_id",     "TEXT"),
        ("orders", "transport_order_status", "TEXT"),
        ("orders", "transport_order_url",    "TEXT"),
        # ── Консолидация груза для заявки/ЭТрН ──
        ("orders", "cargo_places",  "INTEGER"),
        ("orders", "cargo_pallets", "INTEGER"),
        ("orders", "cargo_name",    "TEXT"),
        ("company_settings", "saby_cargo_name", "TEXT DEFAULT 'Орешки кондитерские'"),
        ("company_settings", "saby_unit_weight_g", "REAL DEFAULT 20"),
        ("orders", "dispatch_date", "DATE"),
        ("carrier_vehicles", "driver_inn", "TEXT"),
        ("carrier_vehicles", "driver_phone", "TEXT"),
        ("carrier_vehicles", "driver_license_series", "TEXT"),
        ("carrier_vehicles", "driver_license_number", "TEXT"),
        ("carrier_vehicles", "driver_license_date", "DATE"),
        # ── СБИС ЭДО: счёт и УПД ──
        ("invoices", "sbis_doc_id", "TEXT"),
        ("invoices", "sbis_status", "TEXT"),
        ("invoices", "sbis_url",    "TEXT"),
        ("orders", "upd_sbis_id",     "TEXT"),
        ("orders", "upd_sbis_status", "TEXT"),
        ("orders", "upd_sbis_url",    "TEXT"),
        ("orders", "handed_at",       "TIMESTAMP"),
        # ── Versta24 — экспедитор курьерских служб (СДЭК, КСЭ и т.д.) ──
        ("counterparties", "is_versta_expeditor", "INTEGER DEFAULT 0"),
        ("orders", "versta_courier_company", "TEXT"),
        ("orders", "versta_order_number",    "TEXT"),
        ("orders", "versta_tracking_number", "TEXT"),
        ("orders", "versta_status_code",     "INTEGER"),
        ("orders", "versta_status_name",     "TEXT"),
        ("orders", "versta_last_event",      "TEXT"),
        ("orders", "versta_tracking_history", "TEXT"),
        ("orders", "versta_synced_at",       "TIMESTAMP"),
        ("company_settings", "versta_api_key", "TEXT"),
        ("company_settings", "versta_enabled", "INTEGER DEFAULT 0"),
        # Налог на логистику — раньше был жёстко зашит блок +6% на все суммы разом (см.
        # git-историю app/routers/logistics.py). Теперь вносится вручную по каждой строке.
        # DEFAULT 6.0 в самом ALTER TABLE — SQLite проставит его существующим строкам,
        # сохраняя прежние итоги «задним числом»; для новых строк форма даёт менять/обнулять.
        ("logistics_costs", "tax_rate", "REAL DEFAULT 6.0"),
        # order_id/cost_type исторически отсутствовали в списке миграций (на проде колонка
        # уже была — добавлена вручную при переносе модели); чиним для локальных БД, где
        # таблица logistics_costs создана до появления этих полей в модели.
        ("logistics_costs", "order_id",  "INTEGER REFERENCES orders(id)"),
        ("logistics_costs", "cost_type", "TEXT DEFAULT 'other'"),
        ("company_settings", "tg_hr_report_chat_ids", "TEXT"),
        # Уведомления склада в Telegram-супергруппу с топиками
        ("company_settings", "tg_warehouse_enabled",         "INTEGER DEFAULT 0"),
        ("company_settings", "tg_warehouse_chat_id",         "TEXT"),
        ("company_settings", "tg_warehouse_topic_receiving", "TEXT"),
        ("company_settings", "tg_warehouse_topic_assembled", "TEXT"),
        ("company_settings", "tg_warehouse_topic_shipped",   "TEXT"),
        # ── Кабинет кладовщика: склады, категории, приёмка/перемещение/списание ──
        ("products", "category_id", "INTEGER REFERENCES categories(id)"),
        ("products", "unit_id_1c", "TEXT"),
        ("stock_movements", "warehouse_id",    "INTEGER REFERENCES warehouses(id)"),
        ("stock_movements", "to_warehouse_id", "INTEGER REFERENCES warehouses(id)"),
        # Непосредственный руководитель сотрудника (для отчёта eNPS руководителей)
        ("hr_employees", "manager_id", "INTEGER REFERENCES hr_employees(id)"),
        # Цель метрики в том виде, как её написал HR («не менее 97%») — из неё
        # выводятся числовая цель, направление и единица измерения
        ("hr_metrics", "target_text", "TEXT"),
        # Свод недель месяца для kind="number": сумма/максимум/минимум (см. HR_METRIC_MONTH_AGGS)
        ("hr_metrics", "month_agg", "TEXT DEFAULT 'sum'"),
        # Метафора: перевозчик принимает заказы через API, токен доступа и отметка отправки
        ("counterparties", "metafora_enabled", "BOOLEAN DEFAULT 0"),
        ("company_settings", "metafora_api_token", "TEXT"),
        ("orders", "metafora_sent_at", "TIMESTAMP"),
        ("orders", "metafora_attempt", "INTEGER DEFAULT 0"),
        # Пятничные уведомления по метрике сотрудников: напоминание руководителям
        # (12:00) и сводка «кто не сдал» для HR (17:30) — каждое со своим чатом
        ("company_settings", "hr_metric_remind_enabled",  "INTEGER DEFAULT 0"),
        ("company_settings", "hr_metric_remind_chat_ids", "TEXT"),
        ("company_settings", "hr_metric_check_enabled",   "INTEGER DEFAULT 0"),
        ("company_settings", "hr_metric_check_chat_ids",  "TEXT"),
        # Порядок сотрудников в списке HR-учёта (перетаскивание строк)
        ("hr_employees", "sort_order", "INTEGER DEFAULT 0"),
        # Выгрузка остатков в свойство товара каталога Bitrix24 (PROPERTY_119)
        ("company_settings", "bitrix_stock_enabled", "INTEGER DEFAULT 0"),
        ("company_settings", "bitrix_stock_field",   "TEXT"),
        ("bitrix_product_links", "last_stock_pushed", "REAL"),
        ("bitrix_product_links", "stock_pushed_at",   "TIMESTAMP"),
        # ── Сети заведений: группировка контрагентов-франчайзи под одной вывеской ──
        ("counterparties", "network_id",    "INTEGER REFERENCES networks(id)"),
        ("counterparties", "outlet_name",   "TEXT"),
        ("counterparties", "is_network_hq", "BOOLEAN DEFAULT 0"),
        # ── Аналитика точек: ИИ-разборы (таблица создаётся через create_all) ──
        ("outlet_insights", "scope", "TEXT DEFAULT 'outlet'"),
        ("outlet_geo", "not_found", "BOOLEAN DEFAULT 0"),
        ("company_settings", "outlets_digest_enabled",       "INTEGER DEFAULT 0"),
        ("company_settings", "outlets_digest_time",          "TEXT DEFAULT '09:30'"),
        ("company_settings", "outlets_digest_chat_ids",      "TEXT"),
        ("company_settings", "outlets_digest_weekdays_only", "INTEGER DEFAULT 1"),
        ("company_settings", "outlets_digest_last_sent",     "DATE"),
        # ── Клиентский кабинет заказа /shop/{token} ──
        ("counterparties", "manager_id",   "INTEGER REFERENCES users(id)"),
        ("counterparties", "shop_token",   "TEXT"),
        ("counterparties", "shop_enabled", "BOOLEAN DEFAULT 0"),
        ("orders", "source", "TEXT DEFAULT 'manual'"),
        ("company_settings", "shop_stage_approved", "TEXT"),
        ("company_settings", "shop_alert_chat_ids", "TEXT"),
        ("company_settings", "shipping_weekdays", "TEXT DEFAULT '0,3'"),
        ("company_settings", "daily_nut_capacity", "INTEGER"),
        ("shop_bookings", "items", "TEXT"),
        ("shop_carts", "notified_at", "TIMESTAMP"),
        ("company_settings", "shop_abandon_hours", "INTEGER DEFAULT 2"),
    ]
    # Whitelist: таблицы/колонки — только идентификаторы; col_def — ограниченный SQL-тип
    import re as _re
    _ident = _re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')
    _col_def_re = _re.compile(
        r"^(TEXT|INTEGER|REAL|BLOB|NUMERIC|BOOLEAN|TIMESTAMP|DATE)"
        r"(\s+DEFAULT\s+(-?\d[\d.]*|'[^']*'))?"
        r"(\s+REFERENCES\s+[a-zA-Z_]\w*\([a-zA-Z_]\w*\))?$",
        _re.IGNORECASE,
    )
    for table, column, col_def in migrations:
        if not _ident.match(table) or not _ident.match(column):
            raise ValueError(f"Небезопасное имя в миграции: table={table!r}, column={column!r}")
        if not _col_def_re.match(col_def.strip()):
            raise ValueError(f"Небезопасный col_def в миграции: {col_def!r}")
        existing = [row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")

    # Вопросы личностного профиля должности переезжают из hr_positions в hr_questions:
    # там под должность настраивается любой раздел, а не только личностный профиль.
    # Перенос — именно перенос: исходная колонка обнуляется, поэтому повторный запуск
    # не воскресит вопросы, которые HR потом удалил.
    _pos_cols = {row[1] for row in cur.execute("PRAGMA table_info(hr_positions)").fetchall()}
    _q_cols = {row[1] for row in cur.execute("PRAGMA table_info(hr_questions)").fetchall()}
    if "personal_questions" in _pos_cols and "position_id" in _q_cols:
        rows = cur.execute(
            "SELECT id, personal_questions FROM hr_positions "
            "WHERE personal_questions IS NOT NULL AND TRIM(personal_questions) <> ''"
        ).fetchall()
        for pos_id, raw in rows:
            questions = [q.strip() for q in (raw or "").splitlines() if q.strip()]
            for i, text in enumerate(questions, start=1):
                key = f"p{pos_id}_{i}"
                exists = cur.execute(
                    "SELECT 1 FROM hr_questions WHERE section='personal' AND key=?", (key,)
                ).fetchone()
                if exists:
                    continue
                cur.execute(
                    "INSERT INTO hr_questions "
                    "(section, key, position_id, slot, answer_type, text, sort_order, "
                    " is_active, is_builtin, created_at) "
                    "VALUES ('personal', ?, ?, 'personal', 'text', ?, ?, 1, 0, CURRENT_TIMESTAMP)",
                    (key, pos_id, text, i * 10),
                )
            cur.execute("UPDATE hr_positions SET personal_questions=NULL WHERE id=?", (pos_id,))

    # Перевод орешков с «Коробки» на «шт»
    cur.execute("UPDATE products SET unit='шт', sale_unit=NULL, units_per_box=1 WHERE unit='Коробки'")
    cur.execute("UPDATE invoice_items SET unit='шт' WHERE unit='Коробки'")

    # Бэкфилл handed_at (дата передачи поставщику) для уже отгруженных заказов —
    # из журнала аудита (первый переход статуса в 'handed'). Отчёты датируют
    # отгрузку по этому полю, историю восстанавливаем однократно.
    _order_cols = {row[1] for row in cur.execute("PRAGMA table_info(orders)").fetchall()}
    if "handed_at" in _order_cols:
        cur.execute(
            "UPDATE orders SET handed_at = ("
            "  SELECT MIN(al.created_at) FROM audit_logs al"
            "  WHERE al.entity_type='order' AND al.entity_id=orders.id"
            "    AND al.field='status' AND al.new_value='handed'"
            ") "
            "WHERE handed_at IS NULL AND status IN ('handed','delivered') "
            "  AND EXISTS ("
            "    SELECT 1 FROM audit_logs al2 WHERE al2.entity_type='order'"
            "      AND al2.entity_id=orders.id AND al2.field='status' AND al2.new_value='handed'"
            "  )"
        )

    # Чистка легаси-заглушек «None» в реквизитах контрагентов: Jinja раньше выводил
    # Python None как текст «None» в value=… формы, и при сохранении он записывался
    # в БД строкой. Обнуляем такие поля (и пустые строки заодно) — единоразово.
    _cp_text_cols = [
        "trade_name", "short_name", "kpp", "ogrn", "legal_address", "actual_address",
        "phone", "email", "contact_person", "signatory",
        "bank_name", "bank_account", "bank_bik", "bank_corr_account", "notes",
    ]
    _existing_cp_cols = {row[1] for row in cur.execute("PRAGMA table_info(counterparties)").fetchall()}
    for _c in _cp_text_cols:
        if _c in _existing_cp_cols:
            cur.execute(
                f"UPDATE counterparties SET {_c}=NULL "
                f"WHERE {_c}='None' OR {_c}='none' OR {_c}='null' OR {_c}=''"
            )

    # Водитель/ТС перевозчика: переход с одиночных полей на список carrier_vehicles.
    # Старые колонки удаляем (SQLite 3.35+ поддерживает DROP COLUMN); если версия
    # SQLite старая — колонки останутся в таблице неиспользуемыми, это не критично.
    _cp_cols = [row[1] for row in cur.execute("PRAGMA table_info(counterparties)").fetchall()]
    if "driver_name" in _cp_cols:
        rows = cur.execute(
            "SELECT id, driver_name, vehicle_plate, vehicle_type FROM counterparties "
            "WHERE driver_name IS NOT NULL OR vehicle_plate IS NOT NULL OR vehicle_type IS NOT NULL"
        ).fetchall()
        for cp_id, driver, plate, vtype in rows:
            cur.execute(
                "INSERT INTO carrier_vehicles (counterparty_id, driver_name, vehicle_plate, vehicle_type, is_active, created_at) "
                "VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP)",
                (cp_id, driver, plate, vtype),
            )
        for _col in ("driver_name", "vehicle_plate", "vehicle_type"):
            try:
                cur.execute(f"ALTER TABLE counterparties DROP COLUMN {_col}")
            except Exception:
                pass

    # Переход на новый цикл статусов заказа: старый «shipped» (Отгружен)
    # соответствует новому «handed» (Передан поставщику)
    cur.execute("UPDATE orders SET status='handed' WHERE status='shipped'")
    # Договоры с заполненной отсрочкой считаем договорами с отсрочкой платежа
    cur.execute("UPDATE contracts SET payment_type='deferred' WHERE payment_days IS NOT NULL AND payment_days > 0 AND (payment_type IS NULL OR payment_type='prepay')")

    # Причины списания → корреспондирующий счёт 1С:УНФ (ChartOfAccounts_Управленческий).
    # GUID'ы взяты из реальной базы (сверено через $metadata + выгрузку плана счетов):
    # 94 «Недостачи и потери от порчи ценностей», 91.02 «Прочие расходы».
    # Обновляем только пока не проставлено вручную (external_id_1c IS NULL).
    cur.execute(
        "UPDATE writeoff_reasons SET external_id_1c='9ff0458b-3b08-11f1-a504-8aba90adaa03' "
        "WHERE external_id_1c IS NULL AND name IN ('Порча', 'Брак', 'Недостача')"
    )
    cur.execute(
        "UPDATE writeoff_reasons SET external_id_1c='9ff04589-3b08-11f1-a504-8aba90adaa03' "
        "WHERE external_id_1c IS NULL AND name IN ('Собственное потребление', 'Прочее')"
    )

    # Помечаем admin как требующего смены пароля, если пароль ещё не менялся.
    # Проверяем по bcrypt-хэшу: если хэш совпадает с «admin» — пароль дефолтный.
    row = cur.execute("SELECT password_hash FROM users WHERE username='admin' LIMIT 1").fetchone()
    if row:
        try:
            import bcrypt as _bcrypt
            if _bcrypt.checkpw(b"admin", row[0].encode("utf-8")):
                cur.execute(
                    "UPDATE users SET must_change_password=1 WHERE username='admin'"
                )
        except Exception:
            pass

    # ── Индексы (CREATE INDEX IF NOT EXISTS — идемпотентно) ──────────────────
    indexes = [
        ("ix_orders_status",            "orders",         "status"),
        ("ix_orders_date",              "orders",         "date"),
        ("ix_orders_counterparty_id",   "orders",         "counterparty_id"),
        ("ix_invoices_status",          "invoices",       "status"),
        ("ix_invoices_date",            "invoices",       "date"),
        ("ix_invoices_counterparty_id", "invoices",       "counterparty_id"),
        ("ix_invoice_items_invoice_id", "invoice_items",  "invoice_id"),
        ("ix_payments_invoice_id",      "payments",       "invoice_id"),
        ("ix_order_items_order_id",     "order_items",    "order_id"),
        ("ix_order_items_product_id",   "order_items",    "product_id"),
        ("ix_sales_leads_call_status",  "sales_leads",    "call_status"),
        ("ix_sales_leads_assigned",     "sales_leads",    "assigned_to_id"),
        ("ix_stock_movements_product",  "stock_movements","product_id"),
    ]
    for idx_name, tbl, col in indexes:
        if not _ident.match(idx_name) or not _ident.match(tbl) or not _ident.match(col):
            continue
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS {idx_name} ON {tbl}({col})"
        )
    # Составной индекс для audit_logs
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_audit_logs_entity "
        "ON audit_logs(entity_type, entity_id)"
    )
    # Уникальный индекс для публичного токена трекинга заказа
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_orders_public_token "
        "ON orders(public_token) WHERE public_token IS NOT NULL"
    )
    # Бронь мощности хранит дату ОТГРУЗКИ — старое имя delivery_date вводило в
    # заблуждение (доставка на межгород бывает на дни позже отгрузки).
    cur.execute("PRAGMA table_info(shop_bookings)")
    _booking_cols = {row[1] for row in cur.fetchall()}
    if _booking_cols and "delivery_date" in _booking_cols and "ship_date" not in _booking_cols:
        cur.execute("ALTER TABLE shop_bookings RENAME COLUMN delivery_date TO ship_date")

    # Уникальный индекс для токена клиентского кабинета заказа (/shop/{token})
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_counterparties_shop_token "
        "ON counterparties(shop_token) WHERE shop_token IS NOT NULL"
    )
    # Идемпотентность приёма платежей: один (источник, внешний id) = одна запись
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_payments_source_ext "
        "ON payments(source, external_id) WHERE external_id IS NOT NULL"
    )

    conn.commit()
    conn.close()


def _seed_defaults():
    import logging as _logging
    _log = _logging.getLogger(__name__)
    db = SessionLocal()
    try:
        from app.models import (
            User, CompanySettings, Warehouse, StockMovement, WriteOffReason,
            HrQuestion, HR_DEFAULT_QUESTIONS,
        )
        if not db.query(User).first():
            admin = User(
                username="admin",
                password_hash=hash_password("admin"),
                full_name="Администратор",
                role="admin",
                must_change_password=True,
            )
            db.add(admin)
            _log.warning("Создан пользователь admin — при первом входе потребуется сменить пароль!")
        if not db.query(CompanySettings).first():
            db.add(CompanySettings(name="Моя компания"))

        # Склад по умолчанию — до появления реальной синхронизации складов из 1С
        # (и для старых строк StockMovement, у которых warehouse_id ещё пуст).
        default_wh = db.query(Warehouse).filter(Warehouse.is_default.is_(True)).first()
        if not default_wh:
            default_wh = Warehouse(name="Основной склад", is_default=True, is_active=True)
            db.add(default_wh)
            db.flush()
            db.query(StockMovement).filter(StockMovement.warehouse_id.is_(None)).update(
                {StockMovement.warehouse_id: default_wh.id}, synchronize_session=False
            )
            _log.info("Создан склад по умолчанию «Основной склад» и привязан к старым движениям")

        # Стандартный набор причин списания — до появления точного справочника
        # причин/корреспонденций из 1С (сопоставление добавится позже через
        # WriteOffReason.external_id_1c, как и для остальных 1С-сущностей).
        if not db.query(WriteOffReason).first():
            for name in ["Порча", "Брак", "Недостача", "Собственное потребление", "Прочее"]:
                db.add(WriteOffReason(name=name))

        # Базовые вопросы HR-опросника. Добавляем только недостающие пары
        # (раздел, ключ) — переформулировки и выключения, сделанные HR, не трогаем.
        existing_q = {(s, k) for s, k in db.query(HrQuestion.section, HrQuestion.key).all()}
        for seed in HR_DEFAULT_QUESTIONS:
            if (seed["section"], seed["key"]) not in existing_q:
                db.add(HrQuestion(is_builtin=True, **seed))

        db.commit()
    finally:
        db.close()

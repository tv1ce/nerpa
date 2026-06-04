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


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

from sqlalchemy import event
event.listen(engine, "connect", _set_wal)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


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
        ("company_settings", "board_shift_start",    "TEXT DEFAULT '09:00'"),
        ("company_settings", "board_shift_end",      "TEXT DEFAULT '17:00'"),
        ("company_settings", "board_nut_price",      "REAL DEFAULT 52.0"),
        ("company_settings", "board_cost_pct",       "REAL DEFAULT 0.0"),
        ("company_settings", "board_cost_norm_pct",  "REAL DEFAULT 48.0"),
        ("company_settings", "board_cost_deviation", "REAL DEFAULT 5.0"),
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
        # Принудительная смена пароля при следующем входе
        ("users", "must_change_password", "INTEGER DEFAULT 0"),
        # Адресат уведомления (NULL = системное, видят все)
        ("notifications", "user_id", "INTEGER REFERENCES users(id)"),
        # KPI-фильтр продукта для дашборда и отчётов
        ("company_settings", "kpi_product_filter", "TEXT DEFAULT 'орешк'"),
        # Скидка по умолчанию для контрагента (подставляется в новые заказы)
        ("counterparties", "default_discount_pct", "REAL DEFAULT 0.0"),
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

    # Перевод орешков с «Коробки» на «шт»
    cur.execute("UPDATE products SET unit='шт', sale_unit=NULL, units_per_box=1 WHERE unit='Коробки'")
    cur.execute("UPDATE invoice_items SET unit='шт' WHERE unit='Коробки'")

    # Переход на новый цикл статусов заказа: старый «shipped» (Отгружен)
    # соответствует новому «handed» (Передан поставщику)
    cur.execute("UPDATE orders SET status='handed' WHERE status='shipped'")
    # Договоры с заполненной отсрочкой считаем договорами с отсрочкой платежа
    cur.execute("UPDATE contracts SET payment_type='deferred' WHERE payment_days IS NOT NULL AND payment_days > 0 AND (payment_type IS NULL OR payment_type='prepay')")

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

    conn.commit()
    conn.close()


def _seed_defaults():
    db = SessionLocal()
    try:
        from app.models import User, CompanySettings
        if not db.query(User).first():
            import sys
            admin = User(
                username="admin",
                password_hash=hash_password("admin"),
                full_name="Администратор",
                role="admin",
                must_change_password=True,  # при первом входе потребуем смену пароля
            )
            db.add(admin)
            print(
                "\n[TMS] Создан пользователь admin. "
                "При первом входе потребуется сменить пароль!\n",
                file=sys.stderr,
            )
        if not db.query(CompanySettings).first():
            db.add(CompanySettings(name="Моя компания"))
        db.commit()
    finally:
        db.close()

import bcrypt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = "sqlite:///./tms.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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
    """Добавляет новые колонки в существующие таблицы (SQLite не поддерживает ALTER COLUMN)."""
    import sqlite3
    conn = sqlite3.connect("tms.db")
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
    ]
    for table, column, col_def in migrations:
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

    conn.commit()
    conn.close()


def _seed_defaults():
    db = SessionLocal()
    try:
        from app.models import User, CompanySettings
        if not db.query(User).first():
            admin = User(
                username="admin",
                password_hash=hash_password("admin"),
                full_name="Администратор",
                role="admin",
            )
            db.add(admin)
        if not db.query(CompanySettings).first():
            db.add(CompanySettings(name="Моя компания"))
        db.commit()
    finally:
        db.close()

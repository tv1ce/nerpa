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
        ("products", "min_stock",     "REAL DEFAULT 0.0"),
        ("products", "initial_stock", "REAL DEFAULT 0.0"),
    ]
    for table, column, col_def in migrations:
        existing = [row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
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

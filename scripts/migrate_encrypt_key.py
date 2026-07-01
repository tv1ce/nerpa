"""
Скрипт перешифрования чувствительных полей БД при смене ENCRYPT_KEY.

Запускать ДО обновления ENCRYPT_KEY в .env:
    python scripts/migrate_encrypt_key.py --old-key "OLD_KEY" --new-key "NEW_KEY"

Или если старый ключ — placeholder (как было по умолчанию):
    python scripts/migrate_encrypt_key.py --old-placeholder --new-key "NEW_KEY"
"""
import argparse
import base64
import hashlib
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_fernet(raw_key: str):
    from cryptography.fernet import Fernet
    try:
        key_bytes = base64.urlsafe_b64decode(raw_key + "==")
        if len(key_bytes) == 32:
            fernet_key = raw_key.encode() if len(raw_key) == 44 else base64.urlsafe_b64encode(key_bytes)
        else:
            raise ValueError
    except Exception:
        derived = hashlib.sha256(raw_key.encode()).digest()
        fernet_key = base64.urlsafe_b64encode(derived)
    return Fernet(fernet_key)


_PREFIX = "enc:"

_OLD_PLACEHOLDER = '<python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">'


def reencrypt(value: str, old_fernet, new_fernet) -> str | None:
    if value is None:
        return None
    if not value.startswith(_PREFIX):
        # Незашифрованное значение — шифруем новым ключом
        return _PREFIX + new_fernet.encrypt(value.encode()).decode()
    token = value[len(_PREFIX):].encode("ascii")
    try:
        plaintext = old_fernet.decrypt(token)
    except Exception as e:
        print(f"  WARN: не удалось расшифровать значение ({e}), пропускаем")
        return value
    return _PREFIX + new_fernet.encrypt(plaintext).decode()


def main():
    parser = argparse.ArgumentParser(description="Перешифрование полей БД")
    parser.add_argument("--old-key", help="Старый ENCRYPT_KEY")
    parser.add_argument("--old-placeholder", action="store_true",
                        help="Использовать стандартный placeholder как старый ключ")
    parser.add_argument("--new-key", required=True, help="Новый ENCRYPT_KEY")
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL", "sqlite:///./tms.db"))
    args = parser.parse_args()

    if args.old_placeholder:
        old_raw = _OLD_PLACEHOLDER
    elif args.old_key:
        old_raw = args.old_key
    else:
        parser.error("Укажите --old-key или --old-placeholder")

    old_fernet = _make_fernet(old_raw)
    new_fernet = _make_fernet(args.new_key)

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(args.db_url, connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine)
    db = Session()

    # Поля CompanySettings, хранящие зашифрованные данные
    encrypted_columns = [
        "metafora_password", "metafora_token", "metafora_refresh",
        "dadata_token", "dadata_secret", "tg_bot_token",
    ]

    print("Читаю company_settings...")
    rows = db.execute(text("SELECT id FROM company_settings")).fetchall()
    for row in rows:
        row_id = row[0]
        for col in encrypted_columns:
            try:
                result = db.execute(
                    text(f"SELECT {col} FROM company_settings WHERE id = :id"),
                    {"id": row_id}
                ).fetchone()
                if result is None or result[0] is None:
                    continue
                old_val = result[0]
                new_val = reencrypt(old_val, old_fernet, new_fernet)
                if new_val != old_val:
                    db.execute(
                        text(f"UPDATE company_settings SET {col} = :v WHERE id = :id"),
                        {"v": new_val, "id": row_id}
                    )
                    print(f"  company_settings[{row_id}].{col} — перешифровано")
            except Exception as e:
                print(f"  ERROR: company_settings[{row_id}].{col}: {e}")

    db.commit()
    print("Готово. Теперь обновите ENCRYPT_KEY в .env и перезапустите сервис.")


if __name__ == "__main__":
    main()

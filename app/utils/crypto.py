"""
Прозрачное шифрование чувствительных полей БД через Fernet (AES-128-CBC + HMAC-SHA256).

Ключ берётся из переменной окружения ENCRYPT_KEY.
Если ключ не задан — используется заглушка (данные хранятся без шифрования,
но в stderr выводится предупреждение).

Использование в models.py:
    from app.utils.crypto import EncryptedText
    tg_bot_token = Column(EncryptedText)
"""
import os
import sys
import base64
import hashlib
from sqlalchemy import TypeDecorator, Text


def _load_fernet():
    """Загружает Fernet с ключом из ENCRYPT_KEY.
    Если ключ не задан — возвращает None (режим без шифрования)."""
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        print("[TMS WARNING] Пакет cryptography не установлен. Секреты хранятся без шифрования.", file=sys.stderr)
        return None

    raw_key = os.environ.get("ENCRYPT_KEY", "").strip()
    if not raw_key:
        print(
            "\n[TMS WARNING] ENCRYPT_KEY не задан в .env — секреты (токены, пароли) "
            "хранятся в БД без шифрования. Добавьте ENCRYPT_KEY в .env.\n"
            "Сгенерировать ключ: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n",
            file=sys.stderr,
        )
        return None

    # Приводим ключ к формату Fernet (32 байта, base64url)
    # Если пользователь ввёл произвольную строку — хэшируем её в 32 байта
    try:
        key_bytes = base64.urlsafe_b64decode(raw_key + "==")
        if len(key_bytes) != 32:
            raise ValueError
        fernet_key = raw_key.encode() if len(raw_key) == 44 else base64.urlsafe_b64encode(key_bytes)
    except Exception:
        # Произвольная строка → деривируем 32-байтный ключ через SHA-256
        derived = hashlib.sha256(raw_key.encode()).digest()
        fernet_key = base64.urlsafe_b64encode(derived)

    return Fernet(fernet_key)


# Инициализируем один раз при импорте модуля
_fernet = _load_fernet()

# Префикс для различения зашифрованных и открытых значений в БД
_PREFIX = "enc:"


def encrypt(value: str) -> str:
    """Шифрует строку. Возвращает 'enc:<base64>' или исходную строку если ключ не задан."""
    if value is None:
        return None
    if _fernet is None:
        return value
    encrypted = _fernet.encrypt(value.encode("utf-8"))
    return _PREFIX + encrypted.decode("ascii")


def decrypt(value: str) -> str:
    """Расшифровывает строку. Если значение не зашифровано — возвращает как есть (backward compat)."""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    # Незашифрованное значение (старые данные или режим без ключа)
    if not value.startswith(_PREFIX):
        return value
    if _fernet is None:
        # Ключ не задан, но данные зашифрованы — не можем расшифровать
        return ""
    try:
        token = value[len(_PREFIX):].encode("ascii")
        return _fernet.decrypt(token).decode("utf-8")
    except Exception:
        # Повреждённые данные или неверный ключ — возвращаем пустую строку
        return ""


class EncryptedText(TypeDecorator):
    """SQLAlchemy тип: прозрачно шифрует при записи, расшифровывает при чтении.

    Хранится как TEXT в БД. Existing plaintext values читаются без ошибок
    (backward compatible — данные до включения шифрования доступны до следующего сохранения).
    """
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        """Шифруем при записи в БД."""
        if value is None:
            return None
        return encrypt(str(value))

    def process_result_value(self, value, dialect):
        """Расшифровываем при чтении из БД."""
        if value is None:
            return None
        return decrypt(str(value))

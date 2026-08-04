"""Единое «сейчас» для TMS — наивный datetime в московском времени.

Компания работает в одном часовом поясе, интерфейс показывает время как есть,
без пересчёта. Поэтому в БД всё должно лежать в МСК.

Раньше часть меток писалась через SQLite `CURRENT_TIMESTAMP` (а он всегда UTC) —
из-за этого уведомления и журналы отставали на 3 часа. Все новые записи идут
через `now()` отсюда.

Пояс берётся из TMS_TZ (как в боте), фолбэк — фиксированный UTC+3: в Windows-
окружении без пакета tzdata ZoneInfo недоступен, а перевода часов в Москве нет
с 2014 года, так что смещение постоянное.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

TZ_NAME = os.getenv("TMS_TZ", "Europe/Moscow")

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(TZ_NAME)
except Exception:  # noqa: BLE001 — нет tzdata или неизвестное имя пояса
    TZ = timezone(timedelta(hours=3))
    logger.warning("Часовой пояс %r недоступен, используется фиксированный UTC+3", TZ_NAME)


def now() -> datetime:
    """Текущее время в МСК, наивное (без tzinfo) — как хранится в БД."""
    return datetime.now(TZ).replace(tzinfo=None)


def today():
    """Сегодняшняя дата по МСК."""
    return now().date()

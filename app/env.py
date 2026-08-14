"""Чтение переменных окружения с оглядкой на старые имена.

При ребрендинге TMS → NERPA все переменные окружения переехали с префикса
`TMS_` на `NERPA_`. Ломать этим работающий прод нельзя: `.env` лежит на сервере
и обновляется руками, а бот с отчётами и табло падают молча, если токен вдруг
стал пустым. Поэтому читаем новое имя, а при его отсутствии — старое.

Старый префикс поддерживается ради совместимости и когда-нибудь уедет; чтобы
это не прошло незамеченным, каждый фолбэк один раз пишет предупреждение в лог.

Не относится к `TMS_PAID` / `TMS_DELIVERED` (коды полей в Bitrix24) и
`SABY_TMS_URL` (внешний сервис СБИС) — это чужие имена, они не переименованы.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PREFIX = "NERPA_"
_LEGACY_PREFIX = "TM" "S_"   # склейка, чтобы массовый ребрендинг не тронул строку

# Чтобы не засорять лог: об одной переменной предупреждаем один раз за процесс.
_warned: set[str] = set()


def getenv(name: str, default: str | None = None) -> str | None:
    """Значение `name`, при отсутствии — устаревшего аналога, иначе `default`."""
    value = os.environ.get(name)
    if value is not None:
        return value

    if name.startswith(_PREFIX):
        legacy = _LEGACY_PREFIX + name[len(_PREFIX):]
        value = os.environ.get(legacy)
        if value is not None:
            if legacy not in _warned:
                _warned.add(legacy)
                logger.warning(
                    "Переменная окружения %s устарела — переименуйте её в %s "
                    "(пока читаем старое имя)", legacy, name,
                )
            return value

    return default

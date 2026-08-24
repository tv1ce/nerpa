"""Геокодирование адресов через Nominatim (OpenStreetMap, бесплатно, без ключа).
Rate limit: 1 запрос/сек. Используется из фонового потока.
"""
import logging
import os
import time

import httpx

logger = logging.getLogger("nerpa.geocode")

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Nominatim usage policy требует identifying User-Agent + контакт для связи в случае проблем,
# иначе IP/UA банится без предупреждения. См. https://operations.osmfoundation.org/policies/nominatim/
_CONTACT = os.getenv("NOMINATIM_CONTACT", "")
_HEADERS = {
    "User-Agent": f"NERPA-SalesLeads/1.0 ({_CONTACT})" if _CONTACT else "NERPA-SalesLeads/1.0 (internal)",
}
_DELAY = 1.15  # секунд между запросами


class GeocodeRequestError(Exception):
    """Запрос к Nominatim не удался (сеть, таймаут, 429/403/5xx) — это не значит,
    что адрес не найден, повторить нужно позже, а не блокировать адрес навсегда."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def geocode_address_sync(query: str) -> tuple[float, float] | None:
    """Возвращает (lat, lng), None если адрес не найден Nominatim-ом (окончательно),
    либо бросает GeocodeRequestError если сам запрос не удался (нужно повторить позже)."""
    if not query or not query.strip():
        return None
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(
                _NOMINATIM,
                params={"q": query, "format": "json", "limit": 1, "addressdetails": 0},
                headers=_HEADERS,
            )
            if r.status_code == 429 or r.status_code >= 500:
                retry_after = r.headers.get("Retry-After")
                logger.warning("Nominatim %s for %r, retry_after=%s", r.status_code, query, retry_after)
                raise GeocodeRequestError(
                    f"nominatim status {r.status_code}",
                    retry_after=float(retry_after) if retry_after else None,
                )
            r.raise_for_status()
            data = r.json()
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
            return None
    except GeocodeRequestError:
        raise
    except httpx.HTTPStatusError as e:
        logger.warning("Nominatim HTTP error for %r: %s", query, e)
        raise GeocodeRequestError(str(e)) from e
    except httpx.HTTPError as e:
        logger.warning("Nominatim request failed for %r: %s", query, e)
        raise GeocodeRequestError(str(e)) from e


def geocode_batch_sync(items: list[tuple[int, str]], on_progress=None) -> dict[int, tuple[float, float]]:
    """Геокодирует список (id, query). Возвращает {id: (lat, lng)}.
    on_progress(done, total) вызывается после каждого запроса.
    Останавливается раньше, если Nominatim перестал отвечать (не тратит бюджет впустую).
    """
    results: dict[int, tuple[float, float]] = {}
    total = len(items)
    for i, (lead_id, query) in enumerate(items):
        try:
            result = geocode_address_sync(query)
        except GeocodeRequestError:
            break
        if result:
            results[lead_id] = result
        if on_progress:
            on_progress(i + 1, total)
        if i < total - 1:
            time.sleep(_DELAY)
    return results

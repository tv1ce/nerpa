"""Геокодирование адресов через Nominatim (OpenStreetMap, бесплатно, без ключа).
Rate limit: 1 запрос/сек. Используется из фонового потока.
"""
import time
import httpx

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_HEADERS = {"User-Agent": "TMS-SalesLeads/1.0 (internal)"}
_DELAY = 1.15  # секунд между запросами


def geocode_address_sync(query: str) -> tuple[float, float] | None:
    """Возвращает (lat, lng) или None если адрес не найден."""
    if not query or not query.strip():
        return None
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(
                _NOMINATIM,
                params={"q": query, "format": "json", "limit": 1, "addressdetails": 0},
                headers=_HEADERS,
            )
            r.raise_for_status()
            data = r.json()
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None


def geocode_batch_sync(items: list[tuple[int, str]], on_progress=None) -> dict[int, tuple[float, float]]:
    """Геокодирует список (id, query). Возвращает {id: (lat, lng)}.
    on_progress(done, total) вызывается после каждого запроса.
    """
    results: dict[int, tuple[float, float]] = {}
    total = len(items)
    for i, (lead_id, query) in enumerate(items):
        result = geocode_address_sync(query)
        if result:
            results[lead_id] = result
        if on_progress:
            on_progress(i + 1, total)
        if i < total - 1:
            time.sleep(_DELAY)
    return results

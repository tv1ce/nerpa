"""
Versta24 (api.versta24.ru) — экспедитор курьерских служб (СДЭК, КСЭ и др.).
Заказы на доставку оформляются вручную в личном кабинете my.versta24.ru
(контрагент «ООО Верста» в TMS); здесь только чтение статуса/трекинга по
номеру заказа Versta, чтобы не дублировать оформление.

Документация: https://api.versta24.ru/docs (OpenAPI). Ключ выдаётся
поддержкой (support@versta24.ru) и передаётся полем apiKey в теле запроса.
"""
import logging
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.versta24.ru/OpenAPI/v1"


class VerstaError(Exception):
    pass


class VerstaClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._http = httpx.Client(base_url=BASE_URL, timeout=30)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._http.close()

    def _post(self, path: str, payload: dict) -> dict:
        body = {**payload, "apiKey": self.api_key}
        resp = self._http.post(path, json=body)
        if resp.status_code >= 400:
            raise VerstaError(f"HTTP {resp.status_code} от Versta [{path}]: {resp.text[:300]}")
        return resp.json() or {}

    def track(self, order_id: str, order_key: str | None = None) -> list[dict]:
        """POST /Track — история событий трекинга по номеру заказа Versta.
        Возвращает trackItems: [{eventDateTime, eventSource, eventText, status, isReturn}, ...]."""
        data = self._post("/Track", {"orderId": order_id, "orderKey": order_key})
        return data.get("trackItems") or []

    def get(self, order_id: str, order_key: str | None = None) -> Optional[dict]:
        """POST /Get — карточка заказа: статус, накладная поставщика, плановая дата доставки."""
        data = self._post("/Get", {"orderId": order_id, "orderKey": order_key})
        return data.get("orderInfo")

    def get_order_statuses(self, versta_order_numbers: list[str]) -> list[dict]:
        """POST /GetOrderStatuses — батч-опрос статусов по списку номеров заказов Versta."""
        items = [{"verstaOrderNumber": n} for n in versta_order_numbers]
        data = self._post("/GetOrderStatuses", {"orderStatusesInfo": items})
        return data.get("orderStatusesInfo") or []


def get_versta_client(company) -> Optional[VerstaClient]:
    """Клиент из настроек компании. None — если интеграция выключена/ключ не задан."""
    if not company or not company.versta_enabled or not company.versta_api_key:
        return None
    return VerstaClient(api_key=company.versta_api_key)


# ── Поллинг статусов заказов Versta ────────────────────────────────────────

def poll_versta_statuses(db) -> dict:
    """Опрашивает Versta по каждому заказу с заполненным versta_order_number
    и обновляет статус/последнее событие трекинга. При смене статуса пишет
    уведомление ответственному менеджеру.

    Вызывается периодически из APScheduler (см. app/main.py)."""
    from app.models import CompanySettings, Order, Notification

    company = db.query(CompanySettings).first()
    client = get_versta_client(company)
    if not client:
        return {"checked": 0, "updated": 0}

    orders = db.query(Order).filter(Order.versta_order_number.isnot(None)).all()
    if not orders:
        return {"checked": 0, "updated": 0}

    updated = 0
    with client:
        for o in orders:
            try:
                items = client.track(o.versta_order_number)
            except VerstaError as e:
                logger.warning("Versta поллинг заказа %s [%s]: %s", o.number, o.versta_order_number, e)
                continue
            if not items:
                continue
            last = max(items, key=lambda i: i.get("eventDateTime") or "")
            new_status_code = last.get("status")
            new_event = last.get("eventText") or ""
            new_courier = last.get("eventSource") or o.versta_courier_company
            changed = (
                new_status_code != o.versta_status_code
                or new_event != (o.versta_last_event or "")
            )
            if not changed:
                continue
            o.versta_status_code = new_status_code
            o.versta_last_event = new_event
            o.versta_courier_company = new_courier
            o.versta_synced_at = datetime.now()
            updated += 1
            uid = o.sales_manager_id or o.created_by_id
            db.add(Notification(
                type="versta_status",
                title=f"Versta по заказу №{o.number}: {new_event or 'обновление статуса'}",
                body=f"Контрагент: {o.counterparty.name if o.counterparty else '—'}. "
                     f"Курьер: {new_courier or '—'}.",
                link=f"/orders/{o.id}",
                user_id=uid,
            ))

    if updated:
        db.commit()
    return {"checked": len(orders), "updated": updated}

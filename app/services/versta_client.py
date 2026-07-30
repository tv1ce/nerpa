"""
Versta24 (api.versta24.ru) — экспедитор курьерских служб (СДЭК, КСЭ и др.).
Заказы на доставку оформляются вручную в личном кабинете my.versta24.ru
(контрагент «ООО Верста» в TMS); здесь только чтение статуса/трекинга по
номеру заказа Versta, чтобы не дублировать оформление.

Документация: https://api.versta24.ru/docs (OpenAPI). Ключ выдаётся
поддержкой (support@versta24.ru) и передаётся полем apiKey в теле запроса.
"""
import json
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

def _apply_versta_sync(db, o, items: list[dict], info: Optional[dict]) -> tuple[bool, bool]:
    """Обновляет один заказ данными /Track (items — вся история событий) и
    /Get (info — карточка заказа с авторитетным statusName и deliveryDate).
    Возвращает (changed, newly_delivered)."""
    changed = False
    if items:
        last = max(items, key=lambda i: i.get("eventDateTime") or "")
        new_status_code = last.get("status")
        new_event = last.get("eventText") or ""
        new_courier = last.get("eventSource") or o.versta_courier_company
        history_json = json.dumps(items, ensure_ascii=False)
        if (new_status_code != o.versta_status_code
                or new_event != (o.versta_last_event or "")
                or history_json != (o.versta_tracking_history or "")):
            o.versta_status_code = new_status_code
            o.versta_last_event = new_event
            o.versta_courier_company = new_courier
            o.versta_tracking_history = history_json
            changed = True
    if info:
        new_name = info.get("statusName") or None
        if new_name and new_name != o.versta_status_name:
            o.versta_status_name = new_name
            changed = True
    if changed:
        o.versta_synced_at = datetime.now()

    # Versta проставляет deliveryDate только когда заказ реально вручён —
    # это надёжнее, чем гадать по числовому коду статуса (единой таблицы
    # кодов документация не даёт). Подтягиваем это в основной статус заказа.
    newly_delivered = False
    if info and info.get("deliveryDate") and o.status not in ("delivered", "cancelled"):
        from app.routers.orders import ORDER_STATUSES
        from app.utils import log_action

        old_status = o.status
        o.status = "delivered"
        log_action(
            db, "order", o.id, "status_changed", None,
            f"Статус: {ORDER_STATUSES.get(old_status, old_status)} → Доставлено "
            f"(автоматически по данным Versta24)",
            field="status", old_value=old_status, new_value="delivered",
        )
        newly_delivered = True
    return changed, newly_delivered


def _notify_versta_changed(db, o) -> None:
    from app.models import Notification
    uid = o.sales_manager_id or o.created_by_id
    db.add(Notification(
        type="versta_status",
        title=f"Versta по заказу №{o.number}: {o.versta_last_event or o.versta_status_name or 'обновление статуса'}",
        body=f"Контрагент: {o.counterparty.name if o.counterparty else '—'}. "
             f"Курьер: {o.versta_courier_company or '—'}.",
        link=f"/orders/{o.id}",
        user_id=uid,
    ))


def _push_versta_delivered(o, company, db) -> None:
    from app.services.bitrix_client import push_order_event
    if not o.bitrix_deal_id:
        return
    try:
        push_order_event(o, company, "delivered", db=db)
    except Exception as e:
        logger.error("Versta→Bitrix push delivered для заказа %s: %s", o.number, e)


def poll_versta_statuses(db) -> dict:
    """Опрашивает Versta по всем заказам с заполненным versta_order_number.
    Сначала дешёвым батч-запросом (/GetOrderStatuses) проверяет, у кого код
    статуса вообще сменился — и только для них тянет полную детализацию
    (/Track — история событий, /Get — авторитетное имя статуса и дата
    вручения). Если Versta считает заказ вручённым, статус заказа в TMS
    автоматически переводится в «Доставлено» и уходит пуш в Bitrix24.

    Вызывается периодически из APScheduler (см. app/main.py)."""
    from app.models import CompanySettings, Order

    company = db.query(CompanySettings).first()
    client = get_versta_client(company)
    if not client:
        return {"checked": 0, "updated": 0, "delivered": 0}

    orders = db.query(Order).filter(Order.versta_order_number.isnot(None)).all()
    if not orders:
        return {"checked": 0, "updated": 0, "delivered": 0}

    by_number = {o.versta_order_number: o for o in orders}
    updated = 0
    delivered_orders = []

    with client:
        try:
            batch = client.get_order_statuses(list(by_number))
        except VerstaError as e:
            logger.warning("Versta batch-поллинг статусов: %s", e)
            batch = []
        codes = {row.get("verstaOrderNumber"): row.get("orderStatus")
                 for row in batch if row.get("verstaOrderNumber")}

        for number, o in by_number.items():
            new_code = codes.get(number, o.versta_status_code)
            # Код не менялся и заказ уже когда-то опрашивался — детальный запрос не нужен
            if new_code == o.versta_status_code and o.versta_synced_at is not None:
                continue
            try:
                items = client.track(number)
            except VerstaError as e:
                logger.warning("Versta поллинг заказа %s [%s]: %s", o.number, number, e)
                continue
            try:
                info = client.get(number)
            except VerstaError as e:
                logger.warning("Versta /Get заказа %s [%s]: %s", o.number, number, e)
                info = None

            changed, newly_delivered = _apply_versta_sync(db, o, items, info)
            if changed:
                updated += 1
                _notify_versta_changed(db, o)
            if newly_delivered:
                delivered_orders.append(o)

    if updated or delivered_orders:
        db.commit()

    # Пуш в Bitrix24 — после коммита, чтобы статус «Доставлено» в TMS остался
    # зафиксированным независимо от исхода сетевого запроса к Bitrix.
    for o in delivered_orders:
        _push_versta_delivered(o, company, db)

    return {"checked": len(orders), "updated": updated, "delivered": len(delivered_orders)}


def refresh_versta_order(db, order) -> bool:
    """Ручное обновление статуса одного заказа («Обновить статус» в карточке
    заказа) — без ожидания планового поллинга. Возвращает True, если что-то
    изменилось (статус трекинга или сам заказ переведён в «Доставлено»)."""
    from app.models import CompanySettings

    if not order.versta_order_number:
        return False
    company = db.query(CompanySettings).first()
    client = get_versta_client(company)
    if not client:
        return False

    with client:
        try:
            items = client.track(order.versta_order_number)
        except VerstaError as e:
            logger.warning("Versta ручное обновление заказа %s: %s", order.number, e)
            return False
        try:
            info = client.get(order.versta_order_number)
        except VerstaError as e:
            logger.warning("Versta /Get (ручное обновление) заказа %s: %s", order.number, e)
            info = None

    changed, newly_delivered = _apply_versta_sync(db, order, items, info)
    if changed:
        _notify_versta_changed(db, order)
    if changed or newly_delivered:
        db.commit()
    if newly_delivered:
        _push_versta_delivered(order, company, db)
    return changed or newly_delivered

"""
Bitrix24 CRM — интеграция через входящий вебхук (без OAuth-приложения).

Направление Bitrix24 → TMS:
  Правило автоматизации на стадии «Заказ согласован» (настраивается вручную
  в Bitrix24, см. Настройки → Интеграции → Bitrix24 в TMS) дёргает
  POST /api/bitrix/webhook/deal-approved?key=...&deal_id={=Document:ID} —
  TMS подтягивает контрагента (компания/контакт + реквизиты + DaData) и
  создаёт черновик заказа, привязанный к сделке.

Направление TMS → Bitrix24:
  При смене статуса счёта/заказа в TMS (оплачен / собран / доставлен) TMS
  зовёт crm.deal.update — двигает стадию сделки и/или проставляет булево
  UF-поле («плашку» на карточке).

Docs: https://apidocs.bitrix24.ru/api-reference/
"""
import logging
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

ENTITY_TYPE_COMPANY = 4
ENTITY_TYPE_CONTACT = 3


class BitrixError(Exception):
    pass


class BitrixClient:
    """Клиент REST API Bitrix24 через входящий вебхук. Один экземпляр = одна база."""

    def __init__(self, webhook_url: str):
        self.base = webhook_url.rstrip("/") + "/"
        self._http = httpx.Client(timeout=30)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._http.close()

    # ── Базовый вызов ───────────────────────────────────────────────────────

    def call(self, method: str, **params) -> object:
        try:
            resp = self._http.post(self.base + method + ".json", json=params)
        except httpx.HTTPError as e:
            raise BitrixError(f"Bitrix24 [{method}]: сетевая ошибка — {e}")
        if resp.status_code >= 400:
            try:
                body = resp.json()
                msg = body.get("error_description") or body.get("error") or body
            except Exception:
                msg = resp.text[:500]
            raise BitrixError(f"HTTP {resp.status_code} от Bitrix24 [{method}]: {msg}")
        data = resp.json()
        if "error" in data:
            raise BitrixError(f"Bitrix24 [{method}]: {data.get('error_description') or data['error']}")
        return data.get("result")

    # ── Сделки ───────────────────────────────────────────────────────────────

    def get_deal(self, deal_id) -> dict:
        return self.call("crm.deal.get", id=deal_id) or {}

    def update_deal(self, deal_id, fields: dict) -> bool:
        logger.info("Bitrix24: обновляю сделку %s — %s", deal_id, fields)
        return bool(self.call("crm.deal.update", id=deal_id, fields=fields))

    def get_deal_products(self, deal_id) -> list:
        return self.call("crm.deal.productrows.get", id=deal_id) or []

    # ── Компания / контакт ──────────────────────────────────────────────────

    def get_company(self, company_id) -> dict:
        rows = self.call("crm.company.list", filter={"ID": company_id},
                          select=["ID", "TITLE", "PHONE", "EMAIL"]) or []
        return rows[0] if rows else {}

    def get_contact(self, contact_id) -> dict:
        rows = self.call("crm.contact.list", filter={"ID": contact_id},
                          select=["ID", "NAME", "LAST_NAME", "SECOND_NAME", "PHONE", "EMAIL"]) or []
        return rows[0] if rows else {}

    def get_requisite(self, entity_type_id: int, entity_id) -> dict:
        """Первый реквизит компании/контакта — содержит ИНН/КПП/ОГРН/название."""
        rows = self.call("crm.requisite.list",
                          filter={"ENTITY_TYPE_ID": entity_type_id, "ENTITY_ID": entity_id}) or []
        return rows[0] if rows else {}

    def get_bank_detail(self, requisite_id) -> dict:
        rows = self.call("crm.requisite.bankdetail.list", filter={"ENTITY_ID": requisite_id}) or []
        return rows[0] if rows else {}

    # ── Стадии сделок (для настройки маппинга в TMS) ──────────────────────────

    def list_categories(self) -> list:
        """Направления (воронки) сделок. Общая воронка приходит с id=0."""
        try:
            result = self.call("crm.category.list", entityTypeId=2) or {}
        except BitrixError:
            return []
        return result.get("categories", [])

    def list_stages(self, category_id: int = 0) -> list:
        """Стадии сделки для направления (0 — общая воронка)."""
        entity_id = "DEAL_STAGE" if not category_id else f"DEAL_STAGE_{category_id}"
        rows = self.call("crm.status.list", filter={"ENTITY_ID": entity_id}, order={"SORT": "ASC"}) or []
        return [{"id": r.get("STATUS_ID"), "name": r.get("NAME")} for r in rows]

    # ── CRM-лиды (TMS → Bitrix24, авто-выгрузка «Прозвон»/«Поле») ─────────────

    def add_lead(self, fields: dict) -> str:
        """Создаёт CRM-лид, возвращает его ID."""
        result = self.call("crm.lead.add", fields=fields)
        return str(result)

    # ── Пользователи (для настройки ответственного) ───────────────────────────

    def list_users(self) -> list:
        """Активные сотрудники портала — для выбора ответственного в настройках."""
        rows = self.call("user.get", ACTIVE=True, ADMIN_MODE=True) or []
        return [
            {"id": r.get("ID"), "name": f"{r.get('LAST_NAME', '')} {r.get('NAME', '')}".strip() or r.get("ID")}
            for r in rows
        ]

    # ── Пользовательские поля-«плашки» ────────────────────────────────────────

    def ensure_userfield(self, field_name: str, label: str) -> str:
        """Создаёт булево UF-поле сделки, если его ещё нет. Возвращает код поля."""
        code = f"UF_CRM_{field_name}"
        existing = self.call("crm.deal.userfield.list", filter={"FIELD_NAME": field_name}) or []
        if existing:
            return code
        self.call("crm.deal.userfield.add", fields={
            "FIELD_NAME": field_name,
            "USER_TYPE_ID": "boolean",
            "LABEL": label,
            "EDIT_FORM_LABEL": label,
            "LIST_COLUMN_LABEL": label,
            "SHOW_IN_LIST": "Y",
        })
        logger.info("Bitrix24: создано UF-поле %s (%s)", code, label)
        return code


def get_bitrix_client(company) -> Optional[BitrixClient]:
    """Создаёт клиент из настроек компании. Возвращает None если не настроен/выключен."""
    if not company or not company.bitrix_enabled or not company.bitrix_webhook_url:
        return None
    return BitrixClient(company.bitrix_webhook_url)


# ── Данные контрагента из сделки Bitrix24 ────────────────────────────────────

def _contact_full_name(contact: dict) -> str:
    parts = [contact.get("LAST_NAME"), contact.get("NAME"), contact.get("SECOND_NAME")]
    return " ".join(p for p in parts if p)


def _first_multifield(entity: dict, code: str) -> str:
    """PHONE/EMAIL приходят как список {'VALUE':..., 'VALUE_TYPE':...}."""
    items = entity.get(code) or []
    return items[0].get("VALUE", "") if items else ""


def extract_counterparty_data(client: BitrixClient, deal: dict) -> Optional[dict]:
    """Собирает данные контрагента (компания или контакт) из сделки Bitrix24.

    Возвращает dict с полями, готовыми для Counterparty(**data), либо None,
    если у сделки нет ни компании, ни контакта.
    """
    company_id = deal.get("COMPANY_ID")
    contact_id = deal.get("CONTACT_ID")

    if company_id and str(company_id) != "0":
        entity = client.get_company(company_id)
        name = entity.get("TITLE") or ""
        entity_type_id = ENTITY_TYPE_COMPANY
        entity_id = company_id
        external_id = f"C{company_id}"
        entity_type = "ooo"
    elif contact_id and str(contact_id) != "0":
        entity = client.get_contact(contact_id)
        name = _contact_full_name(entity)
        entity_type_id = ENTITY_TYPE_CONTACT
        entity_id = contact_id
        external_id = f"P{contact_id}"
        entity_type = "ip"
    else:
        return None

    phone = _first_multifield(entity, "PHONE")
    email = _first_multifield(entity, "EMAIL")

    requisite = client.get_requisite(entity_type_id, entity_id)
    inn = (requisite.get("RQ_INN") or "").strip()
    kpp = (requisite.get("RQ_KPP") or "").strip()
    ogrn = (requisite.get("RQ_OGRN") or requisite.get("RQ_OGRNIP") or "").strip()
    full_name = (requisite.get("RQ_COMPANY_FULL_NAME") or requisite.get("RQ_COMPANY_NAME")
                 or name or "").strip()

    data = {
        "name": full_name or name or f"Контрагент из Bitrix24 #{external_id}",
        "trade_name": name or None,
        "inn": inn or None,
        "kpp": kpp or None,
        "ogrn": ogrn or None,
        "phone": phone or None,
        "email": email or None,
        "entity_type": entity_type,
        "external_id_bitrix": external_id,
    }

    if requisite.get("ID"):
        bank = client.get_bank_detail(requisite["ID"])
        if bank:
            data["bank_name"] = bank.get("RQ_BANK_NAME") or None
            data["bank_bik"] = bank.get("RQ_BIK") or None
            data["bank_account"] = bank.get("RQ_ACC_NUM") or None
            data["bank_corr_account"] = bank.get("RQ_COR_ACC_NUM") or None

    return data


def enrich_from_dadata(data: dict) -> dict:
    """Дополняет данные контрагента полными реквизитами по ИНН через DaData
    (та же логика, что и ручная кнопка «Заполнить по ИНН» в карточке контрагента).
    Заполняет только пустые поля — Bitrix24 остаётся источником истины там, где данные есть."""
    import os
    token = os.getenv("DADATA_TOKEN", "")
    inn = (data.get("inn") or "").strip()
    if not token or not inn:
        return data
    try:
        resp = httpx.post(
            "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party",
            headers={"Authorization": f"Token {token}", "Content-Type": "application/json"},
            json={"query": inn},
            timeout=10.0,
        )
        resp.raise_for_status()
        suggestions = resp.json().get("suggestions", [])
    except Exception as e:
        logger.warning("DaData: не удалось дополнить контрагента по ИНН %s: %s", inn, e)
        return data
    if not suggestions:
        return data

    s = suggestions[0]["data"]
    name_block = s.get("name") or {}
    addr = s.get("address") or {}
    mgmt = s.get("management") or {}

    fill = {
        "name": name_block.get("full_with_opf"),
        "short_name": name_block.get("short_with_opf"),
        "kpp": s.get("kpp"),
        "ogrn": s.get("ogrn"),
        "legal_address": (addr.get("value") or {}) if isinstance(addr.get("value"), dict) else addr.get("value"),
        "signatory": mgmt.get("name"),
    }
    for k, v in fill.items():
        if v and not data.get(k):
            data[k] = v
    return data


# ── TMS → Bitrix24: push статуса заказа в сделку ─────────────────────────────

def push_order_event(order, company, event: str, db=None) -> bool:
    """Двигает стадию сделки и/или ставит булево UF-поле-«плашку» по событию TMS.

    event:
      'paid'      — счёт оплачен      → стадия «paid» + флаг bitrix_field_paid=Y
      'assembled' — заказ собран      → стадия «shipped» («Отгрузка»)
      'delivered' — заказ доставлен   → флаг bitrix_field_delivered=Y (+ стадия «delivered», если задана)

    STAGE_ID валиден только внутри своего направления (CATEGORY_ID) — у разных
    воронок (напр. «Первичные» и «Вторичные продажи») разные наборы стадий.
    Если для order.bitrix_category_id есть строка в BitrixPipeline — берём
    стадии оттуда (пустая стадия там = событие для этого направления не
    пушится, напр. у «Вторичных продаж» нет «Отгрузки»). Иначе — глобальные
    bitrix_stage_* из CompanySettings (направление по умолчанию).

    Не бросает исключения наружу — только логирует, чтобы сбой Bitrix24
    не мешал основному действию в TMS (аналогично push в 1С)."""
    if not order.bitrix_deal_id:
        return False
    client = get_bitrix_client(company)
    if not client:
        return False

    pipeline = None
    if db is not None and order.bitrix_category_id is not None:
        from app.models import BitrixPipeline
        pipeline = db.query(BitrixPipeline).filter(
            BitrixPipeline.category_id == order.bitrix_category_id
        ).first()

    if pipeline:
        stage_paid, stage_shipped, stage_delivered = (
            pipeline.stage_paid, pipeline.stage_shipped, pipeline.stage_delivered,
        )
    else:
        stage_paid, stage_shipped, stage_delivered = (
            company.bitrix_stage_paid, company.bitrix_stage_shipped, company.bitrix_stage_delivered,
        )

    fields = {}
    if event == "paid":
        if stage_paid:
            fields["STAGE_ID"] = stage_paid
        if company.bitrix_field_paid:
            fields[company.bitrix_field_paid] = "Y"
    elif event == "assembled":
        if stage_shipped:
            fields["STAGE_ID"] = stage_shipped
    elif event == "delivered":
        if stage_delivered:
            fields["STAGE_ID"] = stage_delivered
        if company.bitrix_field_delivered:
            fields[company.bitrix_field_delivered] = "Y"

    if not fields:
        return False

    try:
        with client:
            client.update_deal(order.bitrix_deal_id, fields)
        return True
    except BitrixError as e:
        logger.error("Bitrix24 push (%s) заказ #%s сделка %s: %s",
                     event, order.number, order.bitrix_deal_id, e)
        return False


# ── TMS → Bitrix24: авто-выгрузка лидов «Прозвон»/«Поле» в статусе «deal» ────

def push_lead_deal_to_bitrix(lead, company, db=None) -> bool:
    """Создаёт CRM-лид в Bitrix24, когда точка «Прозвона»/«Поля» переходит в
    статус call_status == 'deal'. Ответственный всегда один и тот же —
    company.bitrix_lead_responsible_id (задаётся в Настройках → Bitrix24).

    Идемпотентно: если lead.bitrix_lead_id уже заполнен, повторно не создаёт.
    Не бросает исключения наружу — только логирует, как и push_order_event."""
    if lead.call_status != "deal" or lead.bitrix_lead_id:
        return False
    if not company or not company.bitrix_lead_export_enabled:
        return False
    client = get_bitrix_client(company)
    if not client:
        return False

    address = ", ".join(p for p in (lead.city, lead.address) if p) or None
    fields = {
        "TITLE": lead.name,
        "STATUS_ID": "NEW",
        "SOURCE_ID": "OTHER",
        "SOURCE_DESCRIPTION": "TMS — Прозвон/Поле",
        "COMPANY_TITLE": lead.name,
        "PHONE": [{"VALUE": lead.phone, "VALUE_TYPE": "WORK"}] if lead.phone else None,
        "EMAIL": [{"VALUE": lead.email, "VALUE_TYPE": "WORK"}] if lead.email else None,
        "ADDRESS": address,
        "COMMENTS": lead.notes or None,
    }
    fields = {k: v for k, v in fields.items() if v}
    if company.bitrix_lead_responsible_id:
        fields["ASSIGNED_BY_ID"] = company.bitrix_lead_responsible_id

    try:
        with client:
            new_id = client.add_lead(fields)
        lead.bitrix_lead_id = new_id
        lead.bitrix_lead_synced_at = datetime.now()
        if db is not None:
            db.commit()
        return True
    except BitrixError as e:
        logger.error("Bitrix24: не удалось создать CRM-лид из точки #%s (%s): %s",
                     lead.id, lead.name, e)
        return False

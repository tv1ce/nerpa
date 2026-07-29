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
ENTITY_TYPE_REQUISITE = 8   # владелец адресов в crm.address (см. requisite_addresses)

# Типы адресов (crm.enum.addresstype). Числовые ID у типов «юридический»/
# «фактический» на разных порталах разные, поэтому сопоставляем их по названию
# (BitrixClient.address_types), а это — запасной вариант, если метод недоступен.
ADDRESS_TYPE_FALLBACK = {"legal": 6, "actual": 1, "registered": 4}

# Пользовательские поля сделки, которые заполняет менеджер в карточке Bitrix24.
# Ключ TMS -> (подпись поля в карточке, известный код на текущем портале).
#
# Ищем поля по подписи (BitrixClient.deal_uf_codes): код вида
# UF_CRM_1783407291516 содержит таймстамп создания поля и меняется, если поле
# пересоздать, поэтому жёсткая привязка к коду ломается молча. Код держим
# запасным вариантом на случай переименования подписи.
DEAL_UF_FIELDS = {
    "delivery_address": ("Адрес доставки",           "UF_CRM_1783407291516"),
    "delivery_phone":   ("Телефон для доставки",      "UF_CRM_1783407392328"),
    "delivery_date":    ("Планируемая дата доставки", "UF_CRM_1784195630695"),
    "inn":              ("ИНН",                       "UF_CRM_1783406375670"),
    "bank_bik":         ("БИК",                       "UF_CRM_1784195656617"),
    "bank_account":     ("Расчетный счет",            "UF_CRM_1784195664176"),
}


class BitrixError(Exception):
    pass


def _entity_ref_from_external(external_id_bitrix: str):
    """external_id_bitrix контрагента → (ENTITY_TYPE_ID, entity_id) для CRM.

    Формат кода задаётся в extract_counterparty_data: 'C{company_id}' для
    компании, 'P{contact_id}' для контакта. Возвращает (None, None), если код
    пустой или не распознан (например, контрагент заведён не из Bitrix24)."""
    s = (external_id_bitrix or "").strip()
    if len(s) < 2:
        return None, None
    kind, ident = s[0], s[1:]
    if not ident.isdigit():
        return None, None
    if kind == "C":
        return ENTITY_TYPE_COMPANY, ident
    if kind == "P":
        return ENTITY_TYPE_CONTACT, ident
    return None, None


class BitrixClient:
    """Клиент REST API Bitrix24 через входящий вебхук. Один экземпляр = одна база."""

    def __init__(self, webhook_url: str):
        self.base = webhook_url.rstrip("/") + "/"
        self._http = httpx.Client(timeout=30)
        self._uf_codes = None      # кэш карты UF-полей сделки (см. deal_uf_codes)
        self._addr_types = None    # кэш карты типов адресов (см. address_types)

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

    def get_product(self, product_id) -> dict:
        """Карточка товара каталога Bitrix24 — читаем XML_ID (обычно код/GUID
        номенклатуры из 1С, если каталог заведён через штатную выгрузку), чтобы
        сопоставлять товарные позиции сделки с TMS не только по названию."""
        return self.call("crm.product.get", id=product_id) or {}

    def deal_uf_codes(self) -> dict:
        """Карта {ключ TMS: код UF-поля сделки}, найденная по подписям полей.

        См. DEAL_UF_FIELDS. Кэшируется на время жизни клиента, поэтому на один
        вебхук приходится ровно один лишний вызов crm.deal.fields. Поле, которое
        найти не удалось, получает None — вызывающий код просто его не заполнит.
        """
        if self._uf_codes is not None:
            return self._uf_codes
        try:
            fields = self.call("crm.deal.fields") or {}
        except BitrixError as e:
            logger.warning("Bitrix24: не удалось получить поля сделки: %s", e)
            fields = {}
        by_label = {}
        for code, meta in fields.items():
            meta = meta or {}
            label = (meta.get("formLabel") or meta.get("title") or "").strip().lower()
            if label:
                by_label.setdefault(label, code)
        codes = {}
        for key, (label, fallback) in DEAL_UF_FIELDS.items():
            code = by_label.get(label.lower()) or (fallback if fallback in fields else None)
            if not code:
                logger.warning("Bitrix24: поле сделки [%s] не найдено на портале", label)
            codes[key] = code
        self._uf_codes = codes
        return codes

    # ── Компания / контакт ──────────────────────────────────────────────────

    def get_company(self, company_id) -> dict:
        rows = self.call("crm.company.list", filter={"ID": company_id},
                          select=["ID", "TITLE", "PHONE", "EMAIL",
                                  "ADDRESS", "ADDRESS_LEGAL", "REG_ADDRESS"]) or []
        return rows[0] if rows else {}

    def get_contact(self, contact_id) -> dict:
        rows = self.call("crm.contact.list", filter={"ID": contact_id},
                          select=["ID", "NAME", "LAST_NAME", "SECOND_NAME", "PHONE", "EMAIL",
                                  "ADDRESS", "ADDRESS_LEGAL"]) or []
        return rows[0] if rows else {}

    def get_requisite(self, entity_type_id: int, entity_id) -> dict:
        """Первый реквизит компании/контакта — содержит ИНН/КПП/ОГРН/название."""
        rows = self.call("crm.requisite.list",
                          filter={"ENTITY_TYPE_ID": entity_type_id, "ENTITY_ID": entity_id}) or []
        return rows[0] if rows else {}

    def get_bank_detail(self, requisite_id) -> dict:
        rows = self.call("crm.requisite.bankdetail.list", filter={"ENTITY_ID": requisite_id}) or []
        return rows[0] if rows else {}

    def address_types(self) -> dict:
        """{'legal': <TYPE_ID>, 'actual': ..., 'registered': ...}.

        Сопоставляем по названию, а не по числу: ID типов адресов различаются
        между порталами (на текущем «Юридический адрес» = 6, а «Фактический» = 1,
        что не совпадает с порядком из документации). Кэшируется на сессию.
        """
        if self._addr_types is not None:
            return self._addr_types
        types = dict(ADDRESS_TYPE_FALLBACK)
        try:
            rows = self.call("crm.enum.addresstype") or []
        except BitrixError as e:
            logger.warning("Bitrix24: не удалось получить типы адресов: %s", e)
            rows = []
        for row in rows:
            try:
                type_id = int(row.get("ID"))
            except (TypeError, ValueError):
                continue
            name = (row.get("NAME") or "").lower()
            if "юридическ" in name:
                types["legal"] = type_id
            elif "фактическ" in name:
                types["actual"] = type_id
            elif "регистрац" in name:
                types["registered"] = type_id
        self._addr_types = types
        return types

    def requisite_addresses(self, requisite_id) -> dict:
        """{TYPE_ID: адрес} для реквизита.

        У компаний с заполненными реквизитами адреса лежат именно здесь, а поля
        карточки ADDRESS/ADDRESS_LEGAL остаются пустыми — поэтому юр.адрес и не
        доезжал до TMS.
        """
        try:
            rows = self.call("crm.address.list",
                              filter={"ENTITY_TYPE_ID": ENTITY_TYPE_REQUISITE,
                                      "ENTITY_ID": requisite_id}) or []
        except BitrixError as e:
            logger.warning("Bitrix24: не удалось получить адреса реквизита %s: %s", requisite_id, e)
            return {}
        result = {}
        for row in rows:
            try:
                result[int(row.get("TYPE_ID"))] = row
            except (TypeError, ValueError):
                continue
        return result

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


def _clean_addr(v) -> str:
    """Отрезает технический хвост адреса Bitrix24.

    У поля компании это «|;|<id локации>», у UF-поля типа address —
    «|<координаты>|<id>»: «Средний проспект ВО 19|0;0|629». Режем по первому
    «|» — в человекочитаемой части этого символа не бывает."""
    return str(v).split("|")[0].strip() if v else ""


def _format_bx_address(addr: dict) -> str:
    """Адрес из crm.address (разложен по полям) → одна строка для TMS.

    Части часто дублируют друг друга («г Санкт-Петербург» в PROVINCE и он же
    внутри CITY), поэтому вложенные повторы выбрасываем — иначе в карточке
    контрагента получается «Россия, г Санкт-Петербург, г Санкт-Петербург ...».
    """
    parts = []
    for key in ("POSTAL_CODE", "COUNTRY", "REGION", "PROVINCE", "CITY",
                "ADDRESS_1", "ADDRESS_2"):
        value = str(addr.get(key) or "").strip()
        if not value:
            continue
        low = value.lower()
        if any(low in kept.lower() for kept in parts):
            continue
        parts = [kept for kept in parts if kept.lower() not in low]
        parts.append(value)
    return ", ".join(parts)


def _parse_bx_date(v):
    """Дата из Bitrix24 → date. Принимает и ISO с таймзоной, и «25.07.2026»."""
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    logger.warning("Bitrix24: не удалось разобрать дату %r", s)
    return None


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

    # Фактический адрес самой компании/контакта. Это НЕ адрес доставки заказа:
    # тот живёт на сделке (см. extract_delivery_from_deal), потому что у одного
    # клиента бывает несколько точек. Юр.адрес — ADDRESS_LEGAL/REG_ADDRESS.
    actual_address = _clean_addr(entity.get("ADDRESS"))
    legal_from_bx = _clean_addr(entity.get("ADDRESS_LEGAL") or entity.get("REG_ADDRESS"))

    requisite = client.get_requisite(entity_type_id, entity_id)

    # Если реквизит заведён, адреса живут на нём (crm.address), а не в карточке:
    # у таких компаний ADDRESS/ADDRESS_LEGAL пустые. Карточку не перебиваем —
    # берём адрес с реквизита только туда, где выше ничего не нашлось.
    if requisite.get("ID"):
        types = client.address_types()
        addresses = client.requisite_addresses(requisite["ID"])
        if not legal_from_bx:
            for key in ("legal", "registered"):
                addr = addresses.get(types.get(key))
                legal_from_bx = _clean_addr(_format_bx_address(addr)) if addr else ""
                if legal_from_bx:
                    break
        if not actual_address:
            addr = addresses.get(types.get("actual"))
            actual_address = _clean_addr(_format_bx_address(addr)) if addr else ""

    inn = (requisite.get("RQ_INN") or "").strip()
    kpp = (requisite.get("RQ_KPP") or "").strip()
    ogrn = (requisite.get("RQ_OGRN") or requisite.get("RQ_OGRNIP") or "").strip()

    # Компанию/контакт в Bitrix могли завести «заглушкой»: вбить ИНН прямо в
    # название, а реквизиты (RQ_INN и пр.) заполнить позже. Если робот стадии
    # «Заказ согласован» успел дёрнуть вебхук ДО заполнения, RQ_INN приходит
    # пустым, а name — это ИНН (10 цифр у юрлица, 12 у ИП). Распознаём его как
    # ИНН, чтобы сматчить существующего контрагента по ИНН и дополнить из DaData,
    # а не плодить дубль с числовым именем.
    if not inn and name.isdigit() and len(name) in (10, 12):
        inn = name
        entity_type = "ip" if len(inn) == 12 else "ooo"
        name = ""

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
        "actual_address": actual_address or None,   # адрес доставки (точка)
        "legal_address": legal_from_bx or None,     # юр.адрес (DaData уточнит, если пусто)
    }

    if requisite.get("ID"):
        bank = client.get_bank_detail(requisite["ID"])
        if bank:
            data["bank_name"] = bank.get("RQ_BANK_NAME") or None
            data["bank_bik"] = bank.get("RQ_BIK") or None
            data["bank_account"] = bank.get("RQ_ACC_NUM") or None
            data["bank_corr_account"] = bank.get("RQ_COR_ACC_NUM") or None

    return data


def extract_delivery_from_deal(client: BitrixClient, deal: dict) -> dict:
    """Данные доставки из карточки сделки: куда везти, кому звонить, когда.

    Адрес доставки берём именно со сделки, а не с компании: у клиента бывает
    несколько торговых точек, и адрес у каждой сделки свой — у компании же поле
    ADDRESS одно на всех, а заполнено обычно юридическим адресом.

    Контакт для доставки — телефон из UF-поля «Телефон для доставки», если
    менеджер его вписал, иначе телефон привязанного к сделке контакта; имя —
    всегда из контакта сделки.

    Заодно снимаем ИНН/БИК/р-счёт со сделки: робот стадии часто срабатывает
    раньше, чем менеджер заполнит реквизиты в карточке компании, и тогда это
    единственное место, где реквизиты уже есть.
    """
    uf = client.deal_uf_codes()

    def _uf(key) -> str:
        code = uf.get(key)
        return (deal.get(code) or "") if code else ""

    address = _clean_addr(_uf("delivery_address"))
    phone = str(_uf("delivery_phone")).strip()

    name = ""
    contact_id = deal.get("CONTACT_ID")
    if contact_id and str(contact_id) != "0":
        try:
            contact = client.get_contact(contact_id)
        except BitrixError as e:
            logger.warning("Bitrix24: не удалось получить контакт %s сделки %s: %s",
                           contact_id, deal.get("ID"), e)
            contact = {}
        name = _contact_full_name(contact)
        if not phone:
            phone = _first_multifield(contact, "PHONE").strip()

    # Формат Order.delivery_contact — «<телефон> <имя>», как его читает логист.
    contact_line = " ".join(part for part in (phone, name) if part)

    return {
        "delivery_address": address or None,
        "delivery_contact": contact_line or None,
        "delivery_date": _parse_bx_date(_uf("delivery_date")),
        "inn": str(_uf("inn")).strip() or None,
        "bank_bik": str(_uf("bank_bik")).strip() or None,
        "bank_account": str(_uf("bank_account")).strip() or None,
    }


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
        "legal_address": addr.get("value") or None,
        "signatory": mgmt.get("name"),
    }
    for k, v in fill.items():
        if v and not data.get(k):
            data[k] = v
    return data


# ── Дозаливка реквизитов контрагента из Bitrix24 (отложенная) ────────────────

# Поля контрагента, которые дозаполняем из CRM/DaData. Заполняем ТОЛЬКО пустые —
# уже введённые в TMS значения не затираем.
_REFRESHABLE_FIELDS = (
    "inn", "kpp", "ogrn", "bank_name", "bank_bik", "bank_account",
    "bank_corr_account", "short_name", "legal_address", "signatory",
)


def refresh_counterparty_requisites(client: BitrixClient, cp) -> bool:
    """Дозаполняет ПУСТЫЕ реквизиты контрагента, созданного из Bitrix24, свежими
    данными из CRM (ИНН/КПП/ОГРН/банк) + DaData по ИНН.

    Робот стадии «Заказ согласован» дёргает вебхук раньше, чем менеджер успевает
    вписать реквизиты в карточку CRM, поэтому при создании контрагента они бывают
    пустыми. Эта функция возвращается к контрагенту позже и добирает их.

    Никогда не затирает уже заполненные поля. Возвращает True, если что-то
    реально заполнил."""
    entity_type_id, entity_id = _entity_ref_from_external(getattr(cp, "external_id_bitrix", None))
    if not entity_id:
        return False

    requisite = client.get_requisite(entity_type_id, entity_id)
    if not requisite:
        return False

    vals = {
        "inn":  (requisite.get("RQ_INN") or "").strip() or None,
        "kpp":  (requisite.get("RQ_KPP") or "").strip() or None,
        "ogrn": (requisite.get("RQ_OGRN") or requisite.get("RQ_OGRNIP") or "").strip() or None,
    }
    if requisite.get("ID"):
        bank = client.get_bank_detail(requisite["ID"])
        if bank:
            vals["bank_name"]         = (bank.get("RQ_BANK_NAME") or "").strip() or None
            vals["bank_bik"]          = (bank.get("RQ_BIK") or "").strip() or None
            vals["bank_account"]      = (bank.get("RQ_ACC_NUM") or "").strip() or None
            vals["bank_corr_account"] = (bank.get("RQ_COR_ACC_NUM") or "").strip() or None

    # По ИНН добираем юр.адрес/подписанта/короткое имя из DaData (только пустые).
    inn_for_enrich = vals.get("inn") or (cp.inn or None)
    if inn_for_enrich:
        enriched = enrich_from_dadata({"inn": inn_for_enrich})
        for k in ("short_name", "kpp", "ogrn", "legal_address", "signatory"):
            if enriched.get(k) and not vals.get(k):
                vals[k] = enriched[k]

    updated = False
    for attr in _REFRESHABLE_FIELDS:
        value = vals.get(attr)
        if value and not getattr(cp, attr, None):
            setattr(cp, attr, value)
            updated = True
    return updated


def retry_bitrix_counterparty_requisites(db) -> dict:
    """Фоновая (поллинг) дозаливка реквизитов контрагентов из Bitrix24.

    Берёт недавно созданных из Bitrix24 контрагентов, у которых до сих пор пуст
    ИНН или расчётный счёт, и добирает данные из CRM + DaData. Вызывается
    периодически из APScheduler (см. app/main.py)."""
    from datetime import timedelta
    from sqlalchemy import or_
    from app.models import CompanySettings, Counterparty

    company = db.query(CompanySettings).first()
    client = get_bitrix_client(company)
    if not client:
        return {"checked": 0, "updated": 0}

    cutoff = datetime.now() - timedelta(days=3)
    pending = db.query(Counterparty).filter(
        Counterparty.external_id_bitrix.isnot(None),
        Counterparty.created_at >= cutoff,
        or_(
            Counterparty.inn.is_(None),
            Counterparty.bank_account.is_(None),
        ),
    ).all()

    checked = updated = 0
    with client:
        for cp in pending:
            checked += 1
            try:
                if refresh_counterparty_requisites(client, cp):
                    cp.synced_to_bitrix_at = datetime.now()
                    updated += 1
            except BitrixError as e:
                logger.warning("Bitrix24: дозаливка реквизитов КА #%s не удалась: %s", cp.id, e)
    if updated:
        db.commit()
    return {"checked": checked, "updated": updated}


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
    статус call_status == 'deal'.

    Ответственный: если у назначенного в TMS торгпреда/менеджера (lead.assigned_to)
    задан персональный User.bitrix_user_id — лид уходит на него, иначе на общий
    company.bitrix_lead_responsible_id (Настройки → Bitrix24).

    Идемпотентно: если lead.bitrix_lead_id уже заполнен, повторно не создаёт
    (так что эту функцию безопасно дёргать из фоновой задачи-ретрая)."""
    if lead.call_status != "deal" or lead.bitrix_lead_id:
        return False
    if not company or not company.bitrix_lead_export_enabled:
        return False
    client = get_bitrix_client(company)
    if not client:
        return False

    responsible_id = None
    if lead.assigned_to and getattr(lead.assigned_to, "bitrix_user_id", None):
        responsible_id = lead.assigned_to.bitrix_user_id
    elif company.bitrix_lead_responsible_id:
        responsible_id = company.bitrix_lead_responsible_id

    source_is_field = lead.source_file == "field_rep"
    source_label = "TMS — Поле (торгпред)" if source_is_field else "TMS — Прозвон"

    comments_parts = []
    if lead.category:
        comments_parts.append(f"Рубрика: {lead.category}")
    if lead.notes:
        comments_parts.append(lead.notes)
    if lead.director:
        who = lead.director + (f", {lead.director_post}" if lead.director_post else "")
        comments_parts.append(f"ЛПР: {who}")
    if getattr(company, "public_url", None):
        path = f"/field/lead/{lead.id}" if source_is_field else f"/leads/?q={lead.name}"
        comments_parts.append(f"Карточка в TMS: {company.public_url.rstrip('/')}{path}")

    address = ", ".join(p for p in (lead.city, lead.address) if p) or None
    fields = {
        "TITLE": lead.name,
        # Лид уже привёл к реальному договору в TMS — сразу «В работе», а не
        # «Не обработан», чтобы не создавать у ответственного впечатление
        # свежего холодного лида, который ещё никто не трогал.
        "STATUS_ID": "IN_PROCESS",
        # OPENED=Y значит «лид доступен всем» — Bitrix кладёт такие лиды в общий
        # раздел «Неразобранные» ВНЕ ЗАВИСИМОСТИ от ASSIGNED_BY_ID. Явно ставим
        # 'N', чтобы лид сразу был закреплён за ответственным, а не висел в общей куче.
        "OPENED": "N",
        "SOURCE_ID": "OTHER",
        "SOURCE_DESCRIPTION": source_label,
        "COMPANY_TITLE": lead.name,
        "POST": lead.director_post or None,
        "PHONE": [{"VALUE": lead.phone, "VALUE_TYPE": "WORK"}] if lead.phone else None,
        "EMAIL": [{"VALUE": lead.email, "VALUE_TYPE": "WORK"}] if lead.email else None,
        "ADDRESS": address,
        "COMMENTS": "\n".join(comments_parts) or None,
    }
    fields = {k: v for k, v in fields.items() if v}
    if responsible_id:
        fields["ASSIGNED_BY_ID"] = responsible_id

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


def retry_unpushed_leads(db) -> dict:
    """Самовосстановление: находит точки в статусе 'deal', для которых
    push_lead_deal_to_bitrix ещё не сработал (bitrix_lead_id пуст — сеть
    моргнула, вебхук был недоступен и т.п.), и пробует ещё раз.
    Вызывается периодически из APScheduler (см. app/main.py)."""
    from app.models import CompanySettings, SalesLead

    company = db.query(CompanySettings).first()
    if not company or not company.bitrix_lead_export_enabled:
        return {"pushed": 0, "failed": 0}

    pending = db.query(SalesLead).filter(
        SalesLead.call_status == "deal",
        SalesLead.bitrix_lead_id.is_(None),
    ).all()

    pushed = failed = 0
    for lead in pending:
        if push_lead_deal_to_bitrix(lead, company, db=db):
            pushed += 1
        else:
            failed += 1
    return {"pushed": pushed, "failed": failed}

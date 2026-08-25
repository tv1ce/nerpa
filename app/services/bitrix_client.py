"""
Bitrix24 CRM — интеграция через входящий вебхук (без OAuth-приложения).

Направление Bitrix24 → NERPA:
  Правило автоматизации на стадии «Заказ согласован» (настраивается вручную
  в Bitrix24, см. Настройки → Интеграции → Bitrix24 в NERPA) дёргает
  POST /api/bitrix/webhook/deal-approved?key=...&deal_id={=Document:ID} —
  NERPA подтягивает контрагента (компания/контакт + реквизиты + DaData) и
  создаёт черновик заказа, привязанный к сделке.

Направление NERPA → Bitrix24:
  При смене статуса счёта/заказа в NERPA (оплачен / собран / доставлен) NERPA
  зовёт crm.deal.update — двигает стадию сделки и/или проставляет булево
  UF-поле («плашку» на карточке).

Docs: https://apidocs.bitrix24.ru/api-reference/
"""
import logging
import re
import time
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
# Ключ NERPA -> (подпись поля в карточке, известный код на текущем портале).
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
    """Клиент REST API Bitrix24. Один экземпляр = одна база.

    Работает в двух режимах авторизации, снаружи неотличимых:
      вебхук — постоянный ключ портала зашит прямо в адрес;
      OAuth  — адрес общий (client_endpoint), а к каждому вызову добавляется
               access_token, который клиент сам обновляет, когда тот истёк.
    """

    def __init__(self, webhook_url: str = "", *, token_source=None):
        self._token_source = token_source
        if token_source is not None:
            self.base = (token_source.endpoint or "").rstrip("/") + "/"
        else:
            self.base = webhook_url.rstrip("/") + "/"
        self._http = httpx.Client(timeout=30)
        self._uf_codes = None      # кэш карты UF-полей сделки (см. deal_uf_codes)
        self._addr_types = None    # кэш карты типов адресов (см. address_types)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._http.close()

    # ── Базовый вызов ───────────────────────────────────────────────────────

    # Ошибки, после которых имеет смысл обновить токен и повторить вызов
    _AUTH_ERRORS = ("expired_token", "invalid_token", "NO_AUTH_FOUND")

    def _post(self, method: str, params: dict):
        """Один HTTP-вызов. Токен уходит в query, чтобы не мешаться в теле."""
        url = self.base + method + ".json"
        query = None
        if self._token_source is not None:
            url = (self._token_source.endpoint or "").rstrip("/") + "/" + method + ".json"
            query = {"auth": self._token_source.access_token}
        try:
            return self._http.post(url, json=params, params=query)
        except httpx.HTTPError as e:
            raise BitrixError(f"Bitrix24 [{method}]: сетевая ошибка — {e}")

    def call(self, method: str, **params) -> object:
        resp = self._post(method, params)

        # Протухший access_token — обычное дело: он живёт около часа. Обновляем
        # пару и повторяем ровно один раз, чтобы не зациклиться, если портал
        # отвечает ошибкой авторизации по другой причине (снесли приложение).
        if self._token_source is not None and self._is_auth_error(resp):
            logger.info("Bitrix24: токен истёк, обновляю и повторяю [%s]", method)
            self._token_source.refresh()
            resp = self._post(method, params)

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

    def _is_auth_error(self, resp) -> bool:
        try:
            error = (resp.json() or {}).get("error") or ""
        except Exception:
            return False
        return str(error) in self._AUTH_ERRORS

    # ── Сделки ───────────────────────────────────────────────────────────────

    def get_deal(self, deal_id) -> dict:
        return self.call("crm.deal.get", id=deal_id) or {}

    def update_deal(self, deal_id, fields: dict) -> bool:
        logger.info("Bitrix24: обновляю сделку %s — %s", deal_id, fields)
        return bool(self.call("crm.deal.update", id=deal_id, fields=fields))

    def get_deal_products(self, deal_id) -> list:
        return self.call("crm.deal.productrows.get", id=deal_id) or []

    def list_deals(self, filter: dict, select: list = None, order: dict = None) -> list:
        return self.call("crm.deal.list", filter=filter,
                         select=select or ["ID", "TITLE", "CATEGORY_ID", "STAGE_ID", "CLOSED"],
                         order=order or {"ID": "DESC"}) or []

    def find_companies_by_inn(self, inn: str) -> list:
        """ID компаний CRM с этим ИНН — через реквизиты (crm.requisite.list),
        свежие первыми.

        В CRM ИНН живёт не в карточке компании, а в её реквизитах, поэтому ищем
        именно там. Возвращаем СПИСОК, а не одну карточку: на боевом портале
        один и тот же ИНН нередко висит на двух компаниях (клиента заводили
        дважды), причём сделки лежат на одной из них, а не обязательно на
        самой свежей. Выбор правильной — задача вызывающего кода, который
        знает, что ищет."""
        inn = (inn or "").strip()
        if not inn:
            return []
        rows = self.call("crm.requisite.list", filter={"RQ_INN": inn},
                         select=["ID", "ENTITY_TYPE_ID", "ENTITY_ID"]) or []
        companies = [r for r in rows if str(r.get("ENTITY_TYPE_ID")) == str(ENTITY_TYPE_COMPANY)]
        companies.sort(key=lambda r: int(r.get("ID") or 0), reverse=True)
        seen, out = set(), []
        for r in companies:
            cid = str(r.get("ENTITY_ID"))
            if cid not in seen:
                seen.add(cid)
                out.append(cid)
        return out

    def find_company_by_inn(self, inn: str) -> Optional[str]:
        """Одна компания по ИНН — самая свежая. Для случаев, где выбирать не из чего."""
        found = self.find_companies_by_inn(inn)
        return found[0] if found else None

    def add_deal(self, fields: dict) -> str:
        """Создаёт сделку и возвращает её ID.

        STAGE_ID намеренно не подставляем по умолчанию: без него Bitrix кладёт
        сделку на ПЕРВУЮ стадию направления — ровно туда же, куда попадают
        сделки, заведённые менеджером руками. Заказ из клиентского кабинета
        не должен выглядеть в воронке как-то по-особенному.

        REGISTER_SONET_EVENT=Y — чтобы сделка появилась в живой ленте и
        ответственный получил штатное уведомление Bitrix, а не узнал о заказе
        случайно, открыв список."""
        logger.info("Bitrix24: создаю сделку — %s", fields.get("TITLE"))
        return str(self.call("crm.deal.add", fields=fields,
                             params={"REGISTER_SONET_EVENT": "Y"}))

    def set_deal_products(self, deal_id, rows: list) -> bool:
        """Записывает товарные позиции сделки (перезаписывает целиком)."""
        return bool(self.call("crm.deal.productrows.set", id=deal_id, rows=rows))

    def get_product(self, product_id) -> dict:
        """Карточка товара каталога Bitrix24 — читаем XML_ID (обычно код/GUID
        номенклатуры из 1С, если каталог заведён через штатную выгрузку), чтобы
        сопоставлять товарные позиции сделки с NERPA не только по названию.

        Спрашиваем в два захода. crm.product.get знает только карточки старого
        CRM-каталога: товары нового торгового каталога ему не видны, и он
        отвечает «Product is not found» даже на существующий товар. Под вебхуком
        это чаще всего незаметно, а под токеном приложения ломает сопоставление
        позиций у всей сделки — заказ приезжает пустым.

        Поэтому при неудаче переспрашиваем catalog.product.get: тот же товар,
        другой модуль. Поля там в другом регистре (xmlId вместо XML_ID),
        приводим ответ к общему виду, чтобы вызывающий код не знал об этом.
        """
        try:
            card = self.call("crm.product.get", id=product_id) or {}
            if card:
                return card
        except BitrixError as e:
            logger.info("Bitrix24: crm.product.get %s без карточки (%s) — спрашиваю каталог",
                        product_id, e)

        try:
            card = self.get_catalog_product(product_id)
        except BitrixError as e:
            logger.warning("Bitrix24: catalog.product.get %s тоже не дал карточку: %s",
                           product_id, e)
            return {}
        if not card:
            return {}
        return {
            "ID": card.get("id") or product_id,
            "NAME": card.get("name") or "",
            "XML_ID": card.get("xmlId") or card.get("XML_ID") or "",
        }

    # ── Каталог товаров (выгрузка остатков NERPA → Bitrix24) ───────────────────
    #
    # На этом портале старый CRM-каталог (crm.product.*) — кладбище «призрачных»
    # записей NAME='Удален' (crm.product.get/update на реальный товар отвечает
    # «Product is not found»). Настоящие товары и их свойства живут в Universal
    # Catalog (catalog.product.*), выяснено вручную перебором на боевом портале:
    #   - catalog.catalog.list → iblockId=17 «Товарный каталог CRM» (сам)
    #     и iblockId=21 «…(предложения)» — SKU-варианты, потомки iblockId=17
    #     через parentId.
    #   - Свойство «Остаток» (crm.product.property.list) — ID=119, IBLOCK_ID=17,
    #     тип N (число). У SKU-потомков (iblockId=21) этого свойства просто нет
    #     в схеме — писать нужно на РОДИТЕЛЬСКИЙ товар (iblockId=17, type=3).
    #   - xmlId родителя — чистый GUID 1С, совпадает с Product.external_id_1c.
    #     У SKU-потомка xmlId составной: «<родительский GUID>#<суффикс>».
    #   - Формат записи: catalog.product.update(id=<parent_id>,
    #     fields={"property119": {"value": ...}}) — именно вложенным словарём;
    #     тот же ключ строчными буквами и скаляром API проглатывает молча,
    #     ничего не записывая (без ошибки — только по этому и поймали).
    STOCK_IBLOCK_ID = 17
    STOCK_PRODUCT_TYPE = 3

    def list_catalog_products(self, iblock_id: int, product_type: int,
                              select: list = None) -> list:
        """Товары каталога (Universal Catalog) постранично, по 50 за раз.

        Живые «Удален»-призраки отфильтрованы условием !name — без него первые
        страницы почти целиком состоят из них и поиск реальных товаров занимает
        сотни лишних вызовов."""
        select = list(select or []) + ["id", "name", "xmlId", "iblockId"]
        select = list(dict.fromkeys(select))   # без дублей, порядок не важен
        rows, start = [], 0
        while True:
            resp = self.call(
                "catalog.product.list", select=select,
                filter={"iblockId": iblock_id, "type": product_type, "!name": "Удален"},
                start=start,
            ) or {}
            batch = resp.get("products") if isinstance(resp, dict) else resp
            batch = batch or []
            rows.extend(batch)
            if len(batch) < 50:
                break
            start += 50
            if start > 20000:      # предохранитель от бесконечного цикла
                logger.warning("Bitrix24: каталог длиннее 20000 позиций, обрываю обход")
                break
        return rows

    def get_catalog_product(self, product_id) -> dict:
        return (self.call("catalog.product.get", id=product_id) or {}).get("product") or {}

    def update_product_field(self, product_id, field: str, value) -> None:
        """Пишет значение свойства товара каталога — см. комментарий к классу
        выше про обязательный вложенный формат {'value': ...}."""
        self.call("catalog.product.update", id=product_id, fields={field: {"value": value}})

    def deal_uf_codes(self) -> dict:
        """Карта {ключ NERPA: код UF-поля сделки}, найденная по подписям полей.

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
        доезжал до NERPA.
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

    # ── Стадии сделок (для настройки маппинга в NERPA) ──────────────────────────

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

    # ── CRM-лиды (NERPA → Bitrix24, авто-выгрузка «Прозвон»/«Поле») ─────────────

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
    """Создаёт клиент из настроек компании. Возвращает None если не настроен/выключен.

    Единственная точка сборки клиента на всё приложение — заказы, контрагенты,
    лиды и скрипты ходят через неё. Поэтому переключение портала на OAuth
    достаточно сделать здесь: остальной код о способе авторизации не знает.
    """
    if not company or not company.bitrix_enabled:
        return None

    if (getattr(company, "bitrix_auth_mode", "") or "webhook") == "oauth":
        from app.services.bitrix_oauth import build_token_source
        source = build_token_source(company)
        if not source or not source.endpoint:
            logger.warning("Bitrix24: выбран режим OAuth, но приложение не установлено")
            return None
        return BitrixClient(token_source=source)

    if not company.bitrix_webhook_url:
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
    """Адрес из crm.address (разложен по полям) → одна строка для NERPA.

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
# уже введённые в NERPA значения не затираем.
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


# ── NERPA → Bitrix24: push статуса заказа в сделку ─────────────────────────────

def push_order_event(order, company, event: str, db=None) -> bool:
    """Двигает стадию сделки и/или ставит булево UF-поле-«плашку» по событию NERPA.

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
    не мешал основному действию в NERPA (аналогично push в 1С)."""
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


def category_from_stage(stage_id: str) -> int:
    """CATEGORY_ID направления по коду стадии: 'C1:UC_Z7L4EZ' → 1, 'NEW' → 0.

    Bitrix кодирует направление префиксом самой стадии, поэтому отдельная
    настройка воронки не нужна — она однозначно следует из выбранной стадии."""
    m = re.match(r"^C(\d+):", (stage_id or "").strip())
    return int(m.group(1)) if m else 0


def find_client_deal(client: BitrixClient, company_ids: list, category_id: int,
                     busy_deal_ids: set, address: str = "") -> Optional[dict]:
    """Открытая карточка клиента в нужном направлении, готовая принять заказ.

    Логика — «одна сделка = один заказ»: берём ОТКРЫТУЮ сделку, по которой в NERPA
    ещё нет заказа. Если такой нет (клиент заказывает второй раз, а менеджер не
    довёл первую сделку до отгрузки) — возвращаем None, и вызывающий код заводит
    новую карточку. Иначе второй заказ клиента потерялся бы: вебхук
    deal-approved идемпотентен по deal_id и второй заказ на ту же сделку не создаст.

    ВЫБОР ПО АДРЕСУ. У одного юрлица бывает несколько точек, и в CRM под каждую
    заведена своя сделка со своим адресом доставки. Свободных сделок при этом
    несколько, и «просто первая» — это заказ, уехавший на чужую точку. Поэтому
    сначала ищем сделку, адрес которой совпадает с адресом заказа; сравниваем по
    ключу «улица+дом» (тот же, что в аналитике точек), потому что одна и та же
    кофейня записана то «г Санкт-Петербург, ул Гончарная, д 2», то «Гончарная 2».

    Если адреса в сделках проставлены, но ни один не совпал — возвращаем None:
    пусть лучше заведётся новая карточка с верным адресом, чем заказ уедет не
    туда. Если адресов в сделках нет вовсе (поле не заполняют) — работаем как
    раньше, по первой свободной: иначе на каждый заказ плодились бы дубли.

    company_ids — все карточки компании с этим ИНН, свежие первыми. Перебор
    нужен из-за дублей в CRM: у клиента может быть две компании с одним ИНН, и
    сделки при этом лежат на старой."""
    from app.services.outlets import normalize_address

    uf_addr = client.deal_uf_codes().get("delivery_address")
    select = ["ID", "TITLE", "CATEGORY_ID", "STAGE_ID", "CLOSED"]
    if uf_addr:
        select.append(uf_addr)

    free = []
    for company_id in company_ids:
        if not company_id:
            continue
        deals = client.list_deals(
            filter={"COMPANY_ID": company_id, "CATEGORY_ID": category_id, "CLOSED": "N"},
            select=select)
        for deal in deals:
            if str(deal.get("ID")) not in busy_deal_ids:
                deal = dict(deal)
                deal["COMPANY_ID"] = company_id
                free.append(deal)

    if not free:
        return None

    want = normalize_address(address) if address else ""
    if uf_addr and want:
        with_addr = [d for d in free if (d.get(uf_addr) or "").strip()]
        for deal in with_addr:
            if normalize_address(deal.get(uf_addr)) == want:
                logger.info("Bitrix24: сделка %s выбрана по совпадению адреса «%s»",
                            deal.get("ID"), deal.get(uf_addr))
                return deal
        if with_addr:
            logger.info("Bitrix24: среди %d свободных сделок нет адреса «%s» — "
                        "заведём отдельную карточку", len(with_addr), address)
            return None

    return free[0]


def apply_shop_order_to_deal(cp, items: list, order_data: dict, company, db) -> dict:
    """Заказ из клиентского кабинета → карточка клиента в Bitrix24.

    Сделку НЕ создаём без нужды и заказ в NERPA не пишем вовсе: заполняем
    существующую карточку клиента (адрес, дата, телефон, номенклатура, сумма)
    и двигаем её на стадию «Заказ согласован». Дальше срабатывает робот,
    который дёргает /api/bitrix/webhook/deal-approved — и заказ приезжает в
    NERPA штатным путём, тем же, что и заказы менеджеров. Так у заказа остаётся
    один источник истины и не возникает дублей.

    items: [(Product, qty, price)] — цены уже со скидкой контрагента.
    Возвращает {"ok": bool, "deal_id": str|None, "created": bool, "error": str|None}.
    """
    fail = lambda msg: {"ok": False, "deal_id": None, "created": False, "error": msg}

    stage = (getattr(company, "shop_stage_approved", None) or "").strip()
    if not stage:
        return fail("Не выбрана стадия «Заказ согласован» — Настройки → Bitrix24")
    client = get_bitrix_client(company)
    if not client:
        return fail("Bitrix24 не настроен или выключен")

    _, company_id = _entity_ref_from_external(cp.external_id_bitrix)
    category_id = category_from_stage(stage)
    total = round(sum(qty * price for _, qty, price in items), 2)

    try:
        with client:
            # Кандидаты — привязанная карточка плюс все компании с этим ИНН:
            # на портале встречаются дубли, и сделки могут лежать не на той
            # карточке, к которой контрагент привязан.
            candidates = [company_id] if company_id else []
            if cp.inn:
                for cid in client.find_companies_by_inn(cp.inn):
                    if cid not in candidates:
                        candidates.append(cid)
            if not candidates:
                return fail(f"Клиент не найден в Bitrix24 по ИНН {cp.inn or '—'}")
            if not company_id:
                company_id = candidates[0]
                cp.external_id_bitrix = f"C{company_id}"

            from app.models import Order
            busy = {str(d) for (d,) in db.query(Order.bitrix_deal_id)
                    .filter(Order.bitrix_deal_id.isnot(None)).all()}
            deal = find_client_deal(client, candidates, category_id, busy,
                                    address=order_data.get("address") or "")

            uf = client.deal_uf_codes()
            fields = {"OPPORTUNITY": total, "CURRENCY_ID": "RUB", "STAGE_ID": stage}
            if uf.get("delivery_address") and order_data.get("address"):
                fields[uf["delivery_address"]] = order_data["address"]
            if uf.get("delivery_phone") and order_data.get("contact"):
                fields[uf["delivery_phone"]] = order_data["contact"]
            # UF-поле называется «Планируемая дата доставки», но по факту в нём
            # держат день отгрузки — так его заполняют менеджеры, так же читает
            # приём сделки (api_bitrix кладёт его в dispatch_date).
            if uf.get("delivery_date") and order_data.get("ship_date"):
                fields[uf["delivery_date"]] = order_data["ship_date"].isoformat()
            if order_data.get("comment"):
                fields["COMMENTS"] = ("Заказ из кабинета клиента:\n"
                                      + order_data["comment"])

            created = False
            if deal:
                deal_id = str(deal.get("ID"))
            else:
                # Свободных карточек нет — заводим новую сразу в нужном
                # направлении и на нужной стадии, а не в «первичке».
                created = True
                # Заводим её у той компании, где лежит остальная история
                # клиента: при дублях в CRM привязка может указывать на пустую
                # карточку, и новая сделка повисла бы в стороне от всех прочих.
                owner_id = company_id
                for cid in candidates:
                    if client.list_deals(filter={"COMPANY_ID": cid, "CATEGORY_ID": category_id}):
                        owner_id = cid
                        break
                new_fields = dict(fields)
                new_fields.update({
                    "TITLE": f"Заказ из кабинета — {cp.trade_name or cp.name}",
                    "COMPANY_ID": owner_id,
                    "CATEGORY_ID": category_id,
                    "SOURCE_ID": "WEB",
                    "SOURCE_DESCRIPTION": "NERPA — кабинет клиента",
                    "OPENED": "N",
                })
                manager = getattr(cp, "manager", None)
                if getattr(manager, "bitrix_user_id", None):
                    new_fields["ASSIGNED_BY_ID"] = manager.bitrix_user_id
                elif company.bitrix_lead_responsible_id:
                    new_fields["ASSIGNED_BY_ID"] = company.bitrix_lead_responsible_id
                deal_id = client.add_deal(new_fields)

            # Номенклатура — до смены стадии: робот на «Заказ согласован»
            # читает состав сделки, и пустых позиций он видеть не должен.
            rows = []
            link_map = {}
            from app.models import BitrixProductLink
            product_ids = [p.id for p, _, _ in items]
            if product_ids:
                for link in (db.query(BitrixProductLink)
                             .filter(BitrixProductLink.product_id.in_(product_ids)).all()):
                    bx_id = str(link.bitrix_product_id or "")
                    if bx_id and not bx_id.startswith(CATALOG_LINK_PREFIX):
                        link_map.setdefault(link.product_id, bx_id)
            for product, qty, price in items:
                row = {"PRODUCT_NAME": product.name, "PRICE": price, "QUANTITY": qty}
                if link_map.get(product.id):
                    row["PRODUCT_ID"] = link_map[product.id]
                rows.append(row)
            if rows:
                client.set_deal_products(deal_id, rows)

            if not created:
                client.update_deal(deal_id, fields)

        cp.synced_to_bitrix_at = datetime.now()
        db.commit()
        return {"ok": True, "deal_id": str(deal_id), "created": created, "error": None}
    except BitrixError as e:
        logger.error("Bitrix24: заказ из кабинета (%s) не уехал в сделку: %s", cp.name, e)
        return fail(str(e))


# ── NERPA → Bitrix24: остаток товара в свойство каталога ──────────────────────
# Остаток в карточке товара живёт в свойстве «Остаток» (id=119, IBLOCK_ID=17)
# Universal Catalog — см. развёрнутый комментарий у BitrixClient.list_catalog_products.
# Значение приезжает следом за синхронизацией остатков из 1С: NERPA не считает
# остаток сам, а берёт кэш StockBalance1C — тот же, что показывает кладовщику
# (см. get_1c_balances).

DEFAULT_STOCK_FIELD = "property119"

# Строки bitrix_product_links для остатка (товар NERPA → РОДИТЕЛЬСКИЙ товар
# каталога) держим с этим префиксом — иначе они пересекутся по смыслу со
# строками, которые создаёт приём сделок (api_bitrix._match_bitrix_product):
# та таблица уже используется для сопоставления SKU-офера сделки с товаром
# NERPA, там bitrix_product_id — голый числовой ID офера. Остаток пишется на
# СОВСЕМ другой ID (родителя), поэтому смешивать их в одном пространстве id
# нельзя — префикс разводит два назначения одной таблицы.
CATALOG_LINK_PREFIX = "cat:"

# Каталог обходим не чаще раза в час: привязка товар Bitrix ↔ товар NERPA
# запоминается в bitrix_product_links, а синхронизация остатков идёт раз в
# минуту — сканировать каталог на каждый прогон незачем.
_CATALOG_SCAN_TTL_SEC = 3600
_last_catalog_scan = 0.0

_NAME_PUNCT_RE = re.compile(r"[,.;:!?\"'()«»]")
_NAME_SPACE_RE = re.compile(r"\s+")


def normalize_product_name(name: str) -> str:
    """Имя товара к сравнимому виду: регистр, «ё», пунктуация, лишние пробелы.

    Общая для приёма сделки (api_bitrix) и выгрузки остатков: товар должен
    сопоставляться одинаково независимо от того, откуда пришёл."""
    s = (name or "").strip().lower().replace("ё", "е")
    s = _NAME_PUNCT_RE.sub(" ", s)
    return _NAME_SPACE_RE.sub(" ", s).strip()


def _fmt_stock(value: float) -> str:
    """Остаток строкой для свойства каталога: целое — без хвоста «.0»."""
    v = round(float(value or 0), 3)
    return str(int(v)) if v == int(v) else f"{v:g}"


def _scan_bitrix_catalog(client, db, field: str) -> dict:
    """Сопоставляет РОДИТЕЛЬСКИЕ товары каталога (iblockId=17, type=3) с
    номенклатурой NERPA, создаёт недостающие привязки в bitrix_product_links
    (с префиксом CATALOG_LINK_PREFIX). Возвращает {bitrix_product_id: текущее
    значение свойства остатка} — чтобы не переписывать то, что уже совпадает.

    xmlId родителя — чистый GUID 1С, поэтому основной способ сопоставления —
    прямое совпадение с Product.external_id_1c; имя — запасной вариант для
    товаров без GUID (заведённых в Bitrix вручную).
    """
    from app.models import BitrixProductLink, Product

    rows = client.list_catalog_products(
        BitrixClient.STOCK_IBLOCK_ID, BitrixClient.STOCK_PRODUCT_TYPE, select=[field])

    products = db.query(Product).filter(Product.is_active == True).all()
    by_xml_id = {p.external_id_1c.strip(): p for p in products if (p.external_id_1c or "").strip()}
    # Неоднозначные имена (одно нормализованное имя на два товара) не матчим —
    # лучше не выгрузить остаток, чем записать его не тому товару.
    by_name: dict[str, Optional[Product]] = {}
    for p in products:
        key = normalize_product_name(p.name)
        by_name[key] = None if key in by_name else p

    known = {l.bitrix_product_id for l in db.query(BitrixProductLink)
             .filter(BitrixProductLink.bitrix_product_id.like(f"{CATALOG_LINK_PREFIX}%")).all()}
    remote: dict[str, str] = {}
    linked = 0

    for row in rows:
        raw_id = row.get("id")
        if raw_id is None:
            continue
        bid = f"{CATALOG_LINK_PREFIX}{raw_id}"
        remote[bid] = _read_property(row.get(field))
        if bid in known:
            continue
        name = (row.get("name") or "").strip()
        xml_id = (row.get("xmlId") or "").strip()
        product = by_xml_id.get(xml_id) if xml_id else None
        if not product:
            product = by_name.get(normalize_product_name(name))
        if product:
            db.add(BitrixProductLink(bitrix_product_id=bid, product_id=product.id,
                                     bitrix_product_name=name))
            linked += 1

    if linked:
        try:
            db.commit()
            logger.info("Bitrix24: сопоставлено новых товаров каталога — %d", linked)
        except Exception as e:
            db.rollback()
            logger.error("Bitrix24: не удалось сохранить привязки товаров: %s", e)

    return remote


def _read_property(raw) -> str:
    """Значение свойства из ответа catalog.product.*: приходит либо None, либо
    {'value': ..., 'valueId': ...}, либо списком таких словарей."""
    if raw is None:
        return ""
    if isinstance(raw, dict):
        return str(raw.get("value", "") or "")
    if isinstance(raw, list):
        return _read_property(raw[0]) if raw else ""
    return str(raw)


def push_stock_to_bitrix(db, force_rescan: bool = False) -> dict:
    """Выгружает остатки NERPA/1С в свойство товара каталога Bitrix24.

    Вызывается сразу после sync_stock_balances_from_1c — то есть остаток в CRM
    обновляется тем же тактом, что и на складе в NERPA. Пишем только изменившиеся
    значения (см. BitrixProductLink.last_stock_pushed), поэтому обычный прогон
    раз в минуту почти всегда не делает ни одного вызова Bitrix24.

    Товары, по которым 1С ни разу не отдавала остаток, пропускаются: у них нет
    строки в StockBalance1C, и записать им 0 значило бы затереть значение,
    которое в CRM могли проставить руками.

    Наружу не бросает — сбой CRM не должен ронять синхронизацию с 1С.
    """
    global _last_catalog_scan
    from app.models import BitrixProductLink, CompanySettings
    from app.services.onec_client import get_1c_balances

    result = {"pushed": 0, "skipped": 0, "linked": 0, "errors": []}

    company = db.query(CompanySettings).first()
    if not company or not company.bitrix_stock_enabled:
        return result
    client = get_bitrix_client(company)
    if not client:
        result["errors"].append("Bitrix24 не настроен или синхронизация выключена")
        return result

    field = (company.bitrix_stock_field or DEFAULT_STOCK_FIELD).strip()
    balances = get_1c_balances(db)
    if not balances:
        result["errors"].append("Остатки из 1С ещё не синхронизированы — выгружать нечего")
        return result

    try:
        with client:
            links = (db.query(BitrixProductLink)
                     .filter(BitrixProductLink.bitrix_product_id.like(f"{CATALOG_LINK_PREFIX}%"))
                     .all())
            linked_pids = {l.product_id for l in links}
            # Каталог обходим, когда есть товары с остатком, но без привязки к
            # CRM (или по явной кнопке), и не чаще раза в час.
            unlinked = any(pid not in linked_pids for pid in balances)
            stale = (time.monotonic() - _last_catalog_scan) > _CATALOG_SCAN_TTL_SEC
            remote = {}
            if force_rescan or (unlinked and stale):
                before = len(links)
                remote = _scan_bitrix_catalog(client, db, field)
                _last_catalog_scan = time.monotonic()
                links = (db.query(BitrixProductLink)
                         .filter(BitrixProductLink.bitrix_product_id.like(f"{CATALOG_LINK_PREFIX}%"))
                         .all())
                result["linked"] = len(links) - before

            now = datetime.now()
            for link in links:
                qty = balances.get(link.product_id)
                if qty is None:
                    continue
                value = _fmt_stock(qty)
                # Уже отправляли ровно это значение — в CRM оно и лежит
                if link.last_stock_pushed is not None and _fmt_stock(link.last_stock_pushed) == value:
                    result["skipped"] += 1
                    continue
                # После обхода каталога знаем и фактическое значение в CRM:
                # совпало — значит писать нечего, только отмечаем у себя
                if link.bitrix_product_id in remote and remote[link.bitrix_product_id] == value:
                    link.last_stock_pushed = float(qty)
                    link.stock_pushed_at = now
                    result["skipped"] += 1
                    continue
                catalog_id = link.bitrix_product_id[len(CATALOG_LINK_PREFIX):]
                try:
                    client.update_product_field(catalog_id, field, value)
                    link.last_stock_pushed = float(qty)
                    link.stock_pushed_at = now
                    result["pushed"] += 1
                except BitrixError as e:
                    result["errors"].append(f"товар {catalog_id}: {e}")
                    if len(result["errors"]) >= 10:
                        result["errors"].append("…дальнейшие ошибки не показаны")
                        break
    except BitrixError as e:
        result["errors"].append(str(e))
    except Exception as e:                       # noqa: BLE001 — сбой CRM не роняет синк 1С
        logger.error("push_stock_to_bitrix: %s", e)
        result["errors"].append(str(e))

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        result["errors"].append(f"commit: {e}")

    if result["pushed"] or result["errors"]:
        logger.info("Bitrix24 остатки: отправлено %d, без изменений %d, привязано %d, ошибок %d",
                    result["pushed"], result["skipped"], result["linked"], len(result["errors"]))
    return result


# ── NERPA → Bitrix24: авто-выгрузка лидов «Прозвон»/«Поле» в статусе «deal» ────

def push_lead_deal_to_bitrix(lead, company, db=None) -> bool:
    """Создаёт CRM-лид в Bitrix24, когда точка «Прозвона»/«Поля» переходит в
    статус call_status == 'deal'.

    Ответственный: если у назначенного в NERPA торгпреда/менеджера (lead.assigned_to)
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
    source_label = "NERPA — Поле (торгпред)" if source_is_field else "NERPA — Прозвон"

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
        comments_parts.append(f"Карточка в NERPA: {company.public_url.rstrip('/')}{path}")

    address = ", ".join(p for p in (lead.city, lead.address) if p) or None
    fields = {
        "TITLE": lead.name,
        # Лид уже привёл к реальному договору в NERPA — сразу «В работе», а не
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


def push_site_lead_to_bitrix(lead, company, db=None) -> bool:
    """Создаёт CRM-лид из заявки с публичного лендинга /order.

    Отличие от push_lead_deal_to_bitrix (там точка прозвона уже дошла до
    договора): здесь лид холодный и свежий, поэтому STATUS_ID='NEW' —
    менеджер должен увидеть его именно как новое обращение с сайта и
    перезвонить, а не решить, что его кто-то уже ведёт.

    Идемпотентно и не бросает исключений: заявка клиента не должна теряться
    из-за недоступной CRM — она в любом случае уже сохранена в NERPA."""
    if lead.bitrix_lead_id:
        return False
    if not company or not company.bitrix_lead_export_enabled:
        return False
    client = get_bitrix_client(company)
    if not client:
        return False

    comments = []
    if lead.city:
        comments.append(f"Город: {lead.city}")
    if lead.contact_person:
        comments.append(f"Контакт: {lead.contact_person}")
    if lead.notes:
        comments.append(lead.notes)

    fields = {
        "TITLE": f"Заявка с сайта — {lead.name}",
        "STATUS_ID": "NEW",
        "OPENED": "N",
        "SOURCE_ID": "WEB",
        "SOURCE_DESCRIPTION": "NERPA — форма на сайте",
        "COMPANY_TITLE": lead.name,
        "NAME": lead.contact_person or None,
        "PHONE": [{"VALUE": lead.phone, "VALUE_TYPE": "WORK"}] if lead.phone else None,
        "EMAIL": [{"VALUE": lead.email, "VALUE_TYPE": "WORK"}] if lead.email else None,
        "ADDRESS": lead.city or None,
        "COMMENTS": "\n".join(comments) or None,
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
        logger.error("Bitrix24: не удалось создать лид из заявки с сайта (%s): %s", lead.name, e)
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

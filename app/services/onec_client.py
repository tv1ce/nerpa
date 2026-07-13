"""
Клиент к OData API 1С:УНФ.

Базовый URL: {settings.onec_url}   пример: http://srv4.life-it.pro/grach_unf/odata/standard.odata
Аутентификация: HTTP Basic (onec_user / onec_password)
Формат: JSON (odata=nometadata — меньше трафика)
"""
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.models import CompanySettings, Product

logger = logging.getLogger(__name__)

TIMEOUT = 15


# ── Вспомогательные ──────────────────────────────────────────────────────────

def _get_settings(db: Session) -> CompanySettings | None:
    s = db.query(CompanySettings).first()
    if not s or not s.onec_url:
        return None
    return s


def _client(s: CompanySettings) -> httpx.Client:
    return httpx.Client(
        base_url=s.onec_url.rstrip("/") + "/",
        auth=(s.onec_user or "", s.onec_password or ""),
        headers={
            "Accept": "application/json;odata=nometadata",
            "Content-Type": "application/json",
        },
        timeout=TIMEOUT,
    )


def _save_external_id(db: Session, obj, ref_key: str) -> None:
    obj.external_id_1c = ref_key
    obj.synced_to_1c_at = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        db.commit()
    except Exception:
        db.rollback()


# ── Проверка подключения ──────────────────────────────────────────────────────

def test_connection(db: Session) -> dict:
    """
    Проверяет подключение к 1С:УНФ.
    Не требует onec_enabled=True — нужна только строка URL.
    Возвращает {"ok": bool, "message": str}.
    """
    s = _get_settings(db)
    if not s:
        return {"ok": False, "message": "URL 1С не задан в настройках"}
    try:
        with _client(s) as c:
            r = c.get("$metadata", timeout=10)
        if r.status_code == 200:
            return {"ok": True, "message": "Подключение успешно"}
        if r.status_code == 401:
            return {"ok": False, "message": "Неверный логин или пароль (HTTP 401)"}
        return {"ok": False, "message": f"HTTP {r.status_code}: {r.text[:300]}"}
    except httpx.ConnectError as e:
        return {"ok": False, "message": f"Не удалось подключиться: {e}"}
    except httpx.TimeoutException:
        return {"ok": False, "message": "Таймаут подключения (>10 с)"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


# ── Номенклатура: 1С → TMS ───────────────────────────────────────────────────

def sync_products_from_1c(db: Session) -> dict:
    """
    Читает Catalog_Номенклатура из 1С, создаёт/обновляет Products в TMS.
    Маппинг: Ref_Key→external_id_1c, Code→article, Description→name.
    Пропускает записи с ПометкаУдаления=true.
    Возвращает {"created": N, "updated": N, "errors": [...]}.
    """
    s = _get_settings(db)
    if not s or not s.onec_enabled:
        return {"created": 0, "updated": 0, "errors": ["Синхронизация отключена"]}

    errors: list[str] = []
    created = updated = 0

    try:
        with _client(s) as c:
            r = c.get(
                "Catalog_Номенклатура",
                params={
                    "$format": "json",
                    "$select": "Ref_Key,Code,Description,DeletionMark",
                    "$top": "5000",
                },
            )
        r.raise_for_status()
        # Фильтруем помеченные на удаление в Python — булевые фильтры в OData УНФ нестабильны
        items = [i for i in r.json().get("value", []) if not i.get("DeletionMark", False)]
    except Exception as e:
        logger.error("sync_products_from_1c: %s", e)
        return {"created": 0, "updated": 0, "errors": [str(e)]}

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for item in items:
        ref_key = item.get("Ref_Key")
        name = (item.get("Description") or "").strip()
        code = (item.get("Code") or "").strip()

        if not ref_key or not name:
            continue

        try:
            # Ищем по GUID 1С
            p = db.query(Product).filter(Product.external_id_1c == ref_key).first()
            if p:
                p.name = name
                if code:
                    p.article = code
                p.synced_from_1c_at = now
                updated += 1
            else:
                # Пытаемся связать по артикулу
                p = db.query(Product).filter(Product.article == code).first() if code else None
                if p:
                    p.external_id_1c = ref_key
                    p.synced_from_1c_at = now
                    updated += 1
                else:
                    # Пытаемся связать по имени (для продуктов созданных до интеграции)
                    p = db.query(Product).filter(Product.name == name, Product.external_id_1c == None).first()
                    if p:
                        p.external_id_1c = ref_key
                        if code:
                            p.article = code
                        p.synced_from_1c_at = now
                        updated += 1
                    else:
                        # Не создаём новые продукты автоматически —
                        # только линкуем уже существующие в TMS
                        logger.debug("sync_products_from_1c: нет в TMS, пропускаем %s (%s)", name, ref_key)
        except Exception as e:
            errors.append(f"{name}: {e}")
            logger.warning("sync_products_from_1c item error: %s", e)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        errors.append(f"commit: {e}")
        created = updated = 0

    logger.info(
        "sync_products_from_1c: создано %d, обновлено %d, ошибок %d",
        created, updated, len(errors),
    )
    return {"created": created, "updated": updated, "errors": errors}


# ── Контрагенты: TMS → 1С ───────────────────────────────────────────────────

_ENTITY_TYPE_MAP = {
    "ooo":   "ЮрЛицо",
    "ip":    "ИндивидуальныйПредприниматель",
    "other": "ФизЛицо",
}


import re as _re_cp

# Префиксы полных наименований ИП/ООО → нормализуем к «ИП …» / «ООО …»
_IP_PREFIX = _re_cp.compile(r"^\s*(индивидуальный\s+предприниматель|ип)\s+", _re_cp.I)
_OOO_PREFIX = _re_cp.compile(r"^\s*(общество\s+с\s+ограниченной\s+ответственностью|ооо)\s+", _re_cp.I)

# Валюта «рубль» в этой базе 1С (взято из существующего банковского счёта)
_RUB_KEY = "c26a4d87-c6e2-4aca-ab05-1b02be6ecaec"


def _format_cp_name(cp, entity: str | None) -> str:
    """Возвращает наименование в формате «ИП Фамилия И.О.» / «ООО Название»."""
    base = (getattr(cp, "short_name", "") or cp.name or "").strip()
    if entity == "ip":
        core = _IP_PREFIX.sub("", base).strip()
        return f"ИП {core}" if core else base
    if entity == "ooo":
        core = _OOO_PREFIX.sub("", base).strip()
        return f"ООО {core}" if core else base
    return base


def _ensure_bank(c, bik: str, name: str, corr: str) -> str | None:
    """Находит банк в Catalog_Банки по БИК (поле Code) или создаёт новый. Возвращает Ref_Key."""
    bik = (bik or "").strip()
    if not bik:
        return None
    # Фильтр по Code в этой УНФ не работает — тянем все банки (их немного) и ищем
    r = c.get("Catalog_Банки", params={"$format": "json", "$select": "Ref_Key,Code"})
    if r.is_success:
        for b in r.json().get("value", []):
            if (b.get("Code") or "").strip() == bik:
                return b["Ref_Key"]
    # Не нашли — создаём
    payload = {"Code": bik, "Description": (name or bik).strip()}
    if corr:
        payload["КоррСчет"] = corr.strip()
    rr = c.post("Catalog_Банки", json=payload)
    rr.raise_for_status()
    return rr.json().get("Ref_Key")


_ZERO_GUID = "00000000-0000-0000-0000-000000000000"


def _attach_bank_account(c, cp, cp_ref: str) -> None:
    """Создаёт расчётный счёт контрагента и делает его счётом по умолчанию.
    Идемпотентно: если у контрагента уже есть счёт по умолчанию — ничего не делает."""
    account = (getattr(cp, "bank_account", "") or "").strip()
    bik = (getattr(cp, "bank_bik", "") or "").strip()
    if not account or not bik:
        return
    # Уже есть счёт по умолчанию — не дублируем
    chk = c.get(f"Catalog_Контрагенты(guid'{cp_ref}')",
                params={"$format": "json", "$select": "БанковскийСчетПоУмолчанию_Key"})
    if chk.is_success:
        cur = chk.json().get("БанковскийСчетПоУмолчанию_Key")
        if cur and cur != _ZERO_GUID:
            return
    bank_ref = _ensure_bank(c, bik, getattr(cp, "bank_name", ""), getattr(cp, "bank_corr_account", ""))
    if not bank_ref:
        logger.warning("attach_bank_account %s: банк по БИК %s не найден/не создан", cp.id, bik)
        return
    acc_payload = {
        "Owner": cp_ref,
        "Owner_Type": "StandardODATA.Catalog_Контрагенты",
        "Description": account,
        "НомерСчета": account,
        "Банк_Key": bank_ref,
        "ВидСчета": "Расчетный",
        "ВалютаДенежныхСредств_Key": _RUB_KEY,
    }
    ra = c.post("Catalog_БанковскиеСчета", json=acc_payload)
    ra.raise_for_status()
    acc_ref = ra.json().get("Ref_Key")
    if acc_ref:
        # Делаем счёт основным у контрагента
        c.patch(f"Catalog_Контрагенты(guid'{cp_ref}')",
                json={"БанковскийСчетПоУмолчанию_Key": acc_ref})
        logger.info("attach_bank_account %s: создан счёт %s", cp.id, account)


def push_counterparty(cp, db: Session, create_if_missing: bool = True) -> str | None:
    """
    Создаёт или обновляет контрагента в 1С.
    Поиск дубля по ИНН перед созданием.
    create_if_missing=False — только ищет в 1С, не создаёт нового.
    Возвращает Ref_Key (GUID) или None при ошибке/не найдено.
    """
    s = _get_settings(db)
    if not s or not s.onec_enabled:
        return None

    inn = (cp.inn or "").strip()
    entity = getattr(cp, "entity_type", None)

    # Наименование в формате «ИП …» / «ООО …» — и в программе (Description),
    # и для документов (НаименованиеПолное)
    display_name = _format_cp_name(cp, entity)
    payload: dict = {"Description": display_name, "НаименованиеПолное": display_name}

    # Вид контрагента и юр/физ статус — КРИТИЧНО: у ЮрЛица ИНН 10 цифр,
    # у ИП/физлица — 12. Без явного указания 1С создаёт ЮрЛицо и обрезает
    # 12-значный ИНН ИП до 10, а контрагент остаётся без роли и не виден.
    if entity == "ip":
        payload["ВидКонтрагента"] = "ИндивидуальныйПредприниматель"
        payload["ЮридическоеФизическоеЛицо"] = "ФизическоеЛицо"
    elif entity == "ooo":
        payload["ВидКонтрагента"] = "ЮридическоеЛицо"
        payload["ЮридическоеФизическоеЛицо"] = "ЮридическоеЛицо"
    elif len(inn) == 12:
        payload["ВидКонтрагента"] = "ФизическоеЛицо"
        payload["ЮридическоеФизическоеЛицо"] = "ФизическоеЛицо"
    else:
        payload["ВидКонтрагента"] = "ЮридическоеЛицо"
        payload["ЮридическоеФизическоеЛицо"] = "ЮридическоеЛицо"

    if inn:
        payload["ИНН"] = inn
    # КПП — только у юрлиц (у ИП КПП не бывает)
    if entity == "ooo" and getattr(cp, "kpp", None):
        payload["КПП"] = cp.kpp
    # ОГРН/ОГРНИП
    if getattr(cp, "ogrn", None):
        payload["РегистрационныйНомер"] = cp.ogrn

    # Роли — иначе контрагент не попадёт в списки покупателей/поставщиков 1С
    cp_type = getattr(cp, "type", "client")
    payload["Покупатель"] = cp_type in ("client", "both")
    payload["Поставщик"] = cp_type in ("supplier", "both", "carrier")

    try:
        with _client(s) as c:
            # Если уже привязан — просто обновляем
            if cp.external_id_1c:
                c.patch(f"Catalog_Контрагенты(guid'{cp.external_id_1c}')", json=payload)
                _save_external_id(db, cp, cp.external_id_1c)
                try:
                    _attach_bank_account(c, cp, cp.external_id_1c)
                except Exception as be:
                    logger.error("push_counterparty %s: банк.счёт не создан: %s", cp.id, be)
                return cp.external_id_1c

            # Ищем по ИНН: OData-фильтр не работает в УНФ — тянем всех и ищем в Python
            if cp.inn:
                r = c.get(
                    "Catalog_Контрагенты",
                    params={"$format": "json", "$select": "Ref_Key,ИНН", "$top": "2000"},
                )
                if r.is_success:
                    for item in r.json().get("value", []):
                        if (item.get("ИНН") or "").strip() == (cp.inn or "").strip():
                            ref_key = item["Ref_Key"]
                            logger.info("push_counterparty %s: найден в 1С по ИНН → %s", cp.id, ref_key)
                            _save_external_id(db, cp, ref_key)
                            try:
                                _attach_bank_account(c, cp, ref_key)
                            except Exception as be:
                                logger.error("push_counterparty %s: банк.счёт не создан: %s", cp.id, be)
                            return ref_key

            if not create_if_missing:
                logger.warning("push_counterparty %s: не найден в 1С по ИНН, создание запрещено", cp.id)
                return None

            # Создаём нового (только из страницы контрагентов, не из заказа)
            r = c.post("Catalog_Контрагенты", json=payload)
            r.raise_for_status()
            ref_key = r.json().get("Ref_Key")
            if ref_key:
                _save_external_id(db, cp, ref_key)
                # Расчётный счёт + БИК — отдельный справочник, заполняем после
                # создания контрагента (ошибка тут не должна срывать создание)
                try:
                    _attach_bank_account(c, cp, ref_key)
                except Exception as be:
                    logger.error("push_counterparty %s: банк.счёт не создан: %s", cp.id, be)
            return ref_key
    except Exception as e:
        logger.error("push_counterparty %s: %s", cp.id, e)
        return None


# ── Договоры: TMS → 1С ───────────────────────────────────────────────────────

_org_key_cache: str | None = None


def _get_org_key(c) -> str | None:
    """Ref_Key основной организации (в этой базе она одна). Кэшируется."""
    global _org_key_cache
    if _org_key_cache:
        return _org_key_cache
    r = c.get("Catalog_Организации", params={"$format": "json", "$select": "Ref_Key", "$top": "1"})
    if r.is_success:
        vals = r.json().get("value", [])
        if vals:
            _org_key_cache = vals[0]["Ref_Key"]
    return _org_key_cache


def push_contract(contract, db: Session) -> str | None:
    """
    Создаёт/обновляет Catalog_ДоговорыКонтрагентов в 1С.
    Договор принадлежит контрагенту (Owner). Контрагент пушится при необходимости.
    """
    s = _get_settings(db)
    if not s or not s.onec_enabled:
        return None

    cp = contract.counterparty
    if not cp:
        logger.warning("push_contract %s: нет контрагента", contract.id)
        return None

    # Контрагент должен существовать в 1С — пушим при необходимости
    if not cp.external_id_1c:
        logger.info("push_contract %s: контрагент без external_id_1c — пушим", contract.id)
        push_counterparty(cp, db)
    if not cp.external_id_1c:
        logger.warning("push_contract %s: контрагент не в 1С — договор не создан", contract.id)
        return None

    # Дата/срок в формате OData datetime
    date_iso = contract.date.isoformat() + "T00:00:00" if contract.date else None
    end_iso = contract.end_date.isoformat() + "T00:00:00" if contract.end_date else None

    # Наименование договора: «24 от 24.06.2026 (ИП Данилюк Ирина Юрьевна)».
    # Если задан собственный предмет — используем его.
    if contract.subject:
        descr = contract.subject[:200]
    else:
        dt = contract.date.strftime("%d.%m.%Y") if contract.date else ""
        cp_disp = (getattr(cp, "short_name", None) or cp.name or "").strip()
        descr = f"{contract.number} от {dt}".strip()
        if cp_disp:
            descr += f" ({cp_disp})"

    cp_type = getattr(cp, "type", "client")
    vid = "СПоставщиком" if cp_type in ("supplier", "carrier") else "СПокупателем"

    payload: dict = {
        "Owner": cp.external_id_1c,
        "Owner_Type": "StandardODATA.Catalog_Контрагенты",
        "Description": descr,
        "НомерДоговора": str(contract.number),
        "ВидДоговора": vid,
        "ВалютаРасчетов_Key": _RUB_KEY,
        "ДоговорПодписан": contract.status in ("active", "expired", "terminated"),
    }
    if date_iso:
        payload["ДатаДоговора"] = date_iso
    if end_iso:
        payload["СрокДействия"] = end_iso
    if contract.amount:
        payload["Сумма"] = float(contract.amount)

    try:
        with _client(s) as c:
            org = _get_org_key(c)
            if org:
                payload["Организация_Key"] = org
            if contract.external_id_1c:
                c.patch(f"Catalog_ДоговорыКонтрагентов(guid'{contract.external_id_1c}')", json=payload)
                _save_external_id(db, contract, contract.external_id_1c)
                return contract.external_id_1c
            r = c.post("Catalog_ДоговорыКонтрагентов", json=payload)
            r.raise_for_status()
            ref_key = r.json().get("Ref_Key")
            if ref_key:
                _save_external_id(db, contract, ref_key)
                logger.info("push_contract %s: создан договор в 1С → %s", contract.id, ref_key)
            return ref_key
    except Exception as e:
        logger.error("push_contract %s: %s", contract.id, e)
        return None


# ── Заказы: TMS → 1С ─────────────────────────────────────────────────────────

# Конфигурационные GUID'ы этой базы 1С (одна организация, один склад).
# Нужны для записи табличной части Запасы в заказ покупателя.
_ORDER_ORG_KEY       = "0db5df97-3b08-11f1-a504-8aba90adaa03"  # организация
_ORDER_PRICEKIND_KEY = "0db5df98-3b08-11f1-a504-8aba90adaa03"  # вид цен
_ORDER_SALEUNIT_KEY  = "0db5df9a-3b08-11f1-a504-8aba90adaa03"  # структ. единица продажи
_ORDER_WAREHOUSE_KEY = "0db5df9b-3b08-11f1-a504-8aba90adaa03"  # склад резерва
_ORDER_OPERATION_KEY = "99925b82-4855-11f1-b24a-e0071bf33940"  # хозоперация «Заказ на продажу»
_ORDER_UNIT_KEY      = "a8db4701-3b08-11f1-a504-8aba90adaa03"  # единица «шт»
_ORDER_NDS_KEY       = "a8db4750-3b08-11f1-a504-8aba90adaa03"  # ставка НДС «Без НДС»


def push_order(order, db: Session) -> str | None:
    """
    Создаёт/обновляет Document_ЗаказПокупателя в 1С.
    Вызывать при status='confirmed'. При повторных сменах статуса — PATCH.
    """
    s = _get_settings(db)
    if not s or not s.onec_enabled:
        return None

    if not order.counterparty:
        logger.warning("push_order %s: нет контрагента", order.id)
        return None
    if not order.counterparty.external_id_1c:
        logger.info("push_order %s: контрагент без external_id_1c — ищем в 1С по ИНН", order.id)
        push_counterparty(order.counterparty, db, create_if_missing=False)
    if not order.counterparty.external_id_1c:
        logger.warning("push_order %s: контрагент не найден в 1С — заказ не будет создан", order.id)
        return None

    # Состав заказа → табличная часть Запасы (нормальные строки номенклатуры).
    # Требуется привязка товара к 1С (product.external_id_1c). Непривязанные
    # товары не имеют GUID — их перечисляем в комментарии как fallback.
    zapasy = []
    unlinked = []
    for i, item in enumerate(order.items, 1):
        prod = item.product
        ext = getattr(prod, "external_id_1c", None) if prod else None
        if ext:
            qty = item.quantity or 0
            price = item.price or 0
            disc_pct = max(0.0, min(item.discount_pct or 0, 100.0))
            gross = round(qty * price, 2)               # Сумма = Цена×Количество (до скидки)
            net = round(float(item.amount or 0), 2)     # итог строки со скидкой (amount)
            disc_amt = round(gross - net, 2)            # СуммаСкидкиНаценки = gross − net
            zapasy.append({
                "LineNumber": str(i),
                "КлючСвязи": str(i),
                "ТипНоменклатурыЗапас": True,
                "Номенклатура": ext,
                "Номенклатура_Type": "StandardODATA.Catalog_Номенклатура",
                "ЕдиницаИзмерения": _ORDER_UNIT_KEY,
                "ЕдиницаИзмерения_Type": "StandardODATA.Catalog_КлассификаторЕдиницИзмерения",
                "Количество": qty,
                "Цена": price,
                # УНФ-инвариант строки: Сумма = Цена×Кол (до скидки), Всего = Сумма − Скидка.
                # Только так 1С не пересчитывает Цену. Итог со скидкой = «Всего».
                "Сумма": gross,
                "ПроцентСкидкиНаценки": disc_pct,
                "СуммаСкидкиНаценки": disc_amt,
                "ПроцентАвтоматическойСкидки": 0,
                "СуммаАвтоматическойСкидки": 0,
                "Всего": net,
                "СтавкаНДС_Key": _ORDER_NDS_KEY,
                "СуммаНДС": 0,
                "СтруктурнаяЕдиницаРезерв_Key": _ORDER_WAREHOUSE_KEY,
            })
        else:
            unlinked.append(item)

    comment_lines = [f"TMS заказ #{order.number}"]
    for item in unlinked:
        name = item.product.name if item.product else "—"
        comment_lines.append(f"  (нет в 1С) {name}: {item.quantity} шт × {item.price}")
    comment = "\n".join(comment_lines)

    # Договор: ссылаемся на договор контрагента в 1С (пушим, если ещё не привязан)
    contract_key = None
    if getattr(order, "contract", None):
        if not order.contract.external_id_1c:
            try:
                push_contract(order.contract, db)
            except Exception as ce:
                logger.error("push_order %s: договор не запушен: %s", order.id, ce)
        contract_key = order.contract.external_id_1c

    # Дата отгрузки
    ship_iso = order.delivery_date.isoformat() + "T00:00:00" if getattr(order, "delivery_date", None) else None

    try:
        with _client(s) as c:
            # PATCH существующего: только шапка. Табличную часть НЕ трогаем —
            # проведённый документ её менять не даёт (500), плюс операторы могли
            # вручную скорректировать строки/скидки в 1С.
            if order.external_id_1c:
                patch_payload = {
                    "Date": order.date.isoformat() if order.date else None,
                    "Комментарий": comment,
                }
                r = c.patch(f"Document_ЗаказПокупателя(guid'{order.external_id_1c}')", json=patch_payload)
                r.raise_for_status()
                _save_external_id(db, order, order.external_id_1c)
                return order.external_id_1c

            # POST нового документа: пишем состав строками в Запасы.
            # Поля шапки (организация, вид цен, валюта, склады, хозоперация) —
            # обязательны при записи табличной части, иначе OData отдаёт 500.
            payload = {
                "Date": order.date.isoformat() if order.date else None,
                "Контрагент_Key": order.counterparty.external_id_1c,
                "Комментарий": comment,
                "Организация_Key": _ORDER_ORG_KEY,
                "ВидЦен_Key": _ORDER_PRICEKIND_KEY,
                "ВалютаДокумента_Key": _RUB_KEY,
                "СтруктурнаяЕдиницаПродажи_Key": _ORDER_SALEUNIT_KEY,
                "СтруктурнаяЕдиницаРезерв_Key": _ORDER_WAREHOUSE_KEY,
                "ХозяйственнаяОперация_Key": _ORDER_OPERATION_KEY,
                "ВидОперации": "ЗаказНаПродажу",
                "НалогообложениеНДС": "НеОблагаетсяНДС",
                "СуммаВключаетНДС": True,
            }
            if contract_key:
                payload["Договор_Key"] = contract_key
            if ship_iso:
                payload["ДатаОтгрузки"] = ship_iso
            if zapasy:
                payload["Запасы"] = zapasy
            r = c.post("Document_ЗаказПокупателя", json=payload)
            r.raise_for_status()
            ref_key = r.json().get("Ref_Key")
            if ref_key:
                _save_external_id(db, order, ref_key)
            return ref_key
    except Exception as e:
        logger.error("push_order %s: %s", order.id, e)
        return None


# ── Самовосстановление push заказов: TMS → 1С ───────────────────────────────
# Push заказа триггерный (при смене статуса на confirmed) и без ретрая: при
# рестарте сервиса/падении фонового потока заказ может не уехать. Эта функция
# в составе регламентной синхронизации дотолкивает «застрявшие» заказы.

def retry_unpushed_orders(db: Session) -> dict:
    """Дотолкивает в 1С заказы в статусе confirmed/paid без external_id_1c.

    Только активные статусы — чтобы НЕ создавать задним числом старые
    delivered/handed заказы (риск дублей). push_order сам пропустит заказы,
    чей контрагент ещё не в 1С (повторит на следующем цикле).
    Возвращает {"pushed": N, "errors": [...]}.
    """
    s = _get_settings(db)
    if not s or not s.onec_enabled:
        return {"pushed": 0, "errors": ["Синхронизация отключена"]}

    from app.models import Order

    orders = (
        db.query(Order)
        .filter(Order.external_id_1c.is_(None), Order.status.in_(("confirmed", "paid")))
        .all()
    )
    pushed = 0
    errors: list[str] = []
    for order in orders:
        try:
            if push_order(order, db):
                pushed += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"order {order.id}: {e}")
            logger.error("retry_unpushed_orders %s: %s", order.id, e)

    if pushed:
        logger.info("retry_unpushed_orders: дотолкнуто заказов %d", pushed)
    return {"pushed": pushed, "errors": errors}


# ── Оплаты: 1С → TMS ─────────────────────────────────────────────────────────
# Счета из TMS в 1С НЕ выгружаются. Счёт ведётся в 1С, TMS только тянет статус
# оплаты (см. sync_payments_from_1c).

def _digits_to_int(v) -> int | None:
    """«НФНФ-000022» → 22, «22» → 22, иначе None."""
    import re as _re2
    d = _re2.sub(r"\D", "", str(v or ""))
    return int(d) if d else None


def sync_payments_from_1c(db: Session) -> dict:
    """
    Тянет оплату счетов ИЗ 1С (счета НЕ пушатся из TMS) и разносит её через общий
    журнал платежей (app.utils.apply_payment/recompute_invoice_payment).
    Сопоставление: числовой номер счёта 1С == номер счёта TMS И совпадение суммы.
    Оплата берётся из регистра ОплатаСчетовИЗаказов (СуммаОплаты + СуммаАванса) —
    поддерживается частичная оплата. 1С — вторичный источник: то, что уже разнесено
    из банка «Точка», повторно не заводится (сверка сначала по Точке, потом по 1С).
    """
    s = _get_settings(db)
    if not s or not s.onec_enabled:
        return {"updated": 0, "errors": ["Синхронизация отключена"]}

    from datetime import date as _date
    from app.models import Invoice

    errors: list[str] = []
    updated = 0

    try:
        with _client(s) as c:
            # 1. Регистр оплат: агрегируем по счёту (обязательство / оплата / дата)
            rr = c.get("AccumulationRegister_ОплатаСчетовИЗаказов", params={"$format": "json"})
            rr.raise_for_status()
            agg: dict[str, list[float]] = {}      # ref -> [обязательство, оплачено]
            last_pay: dict[str, str] = {}         # ref -> дата последней оплаты
            for rec in rr.json().get("value", []):
                for ln in rec.get("RecordSet", []):
                    ref = ln.get("СчетНаОплату")
                    typ = ln.get("СчетНаОплату_Type") or ""
                    # интересуют только счета на оплату (не заказы)
                    if not ref or "Document_СчетНаОплату" not in typ:
                        continue
                    paid = (ln.get("СуммаОплаты") or 0) + (ln.get("СуммаАванса") or 0)
                    o = agg.setdefault(ref, [0.0, 0.0])
                    o[0] += ln.get("Сумма") or 0
                    o[1] += paid
                    if paid > 0:
                        p = (ln.get("Period") or "")[:10]
                        if p and p > last_pay.get(ref, ""):
                            last_pay[ref] = p
            # 2. Карта оплат 1С: (числовой номер, сумма документа) -> (оплачено, дата).
            #    Берём счета с ненулевой оплатой — в т.ч. частично оплаченные (не только закрытые).
            ri = c.get("Document_СчетНаОплату",
                       params={"$format": "json", "$select": "Ref_Key,Number,СуммаДокумента"})
            ri.raise_for_status()
            pay_index: dict[tuple, tuple[float, str | None]] = {}
            for d in ri.json().get("value", []):
                ref = d.get("Ref_Key")
                ob_pd = agg.get(ref)
                if not ob_pd or ob_pd[1] <= 0:
                    continue
                n = _digits_to_int(d.get("Number"))
                amt = round(d.get("СуммаДокумента") or 0, 2)
                if n is not None:
                    pay_index[(n, amt)] = (round(ob_pd[1], 2), last_pay.get(ref))

        # 3. Разносим оплаты 1С по счетам TMS через общий журнал платежей.
        #    «Top-up до фактической суммы»: доводим оплату счёта до значения из 1С,
        #    не задваивая то, что уже пришло из Точки (Точка — приоритетный источник).
        from sqlalchemy import func as _func
        from app.models import Payment
        from app.utils import recompute_invoice_payment
        for inv in db.query(Invoice).filter(~Invoice.status.in_(["cancelled", "draft"])).all():
            n = _digits_to_int(inv.number)
            amt = round(inv.total_amount or 0, 2)
            if n is None:
                continue
            hit = pay_index.get((n, amt))
            if not hit:
                continue
            paid_total, pd = hit
            existing = db.query(_func.coalesce(_func.sum(Payment.amount), 0.0)) \
                         .filter(Payment.invoice_id == inv.id).scalar() or 0.0
            delta = round(paid_total - existing, 2)
            if delta <= 0.01:
                continue   # уже покрыто (в т.ч. Точкой) — не дублируем
            try:
                pay_date = _date.fromisoformat(pd) if pd else _date.today()
            except (ValueError, TypeError):
                pay_date = _date.today()
            db.add(Payment(
                invoice_id=inv.id, counterparty_id=inv.counterparty_id,
                amount=delta, date=pay_date, source="1c",
                external_id=f"1c:{n}:{paid_total}", purpose="Оплата по данным 1С",
            ))
            db.flush()
            recompute_invoice_payment(db, inv)
            updated += 1

        if updated:
            db.commit()
    except Exception as e:
        db.rollback()
        logger.error("sync_payments_from_1c: %s", e)
        return {"updated": updated, "errors": [str(e)]}

    logger.info("sync_payments_from_1c: оплачено %d", updated)
    return {"updated": updated, "errors": errors}


# ── Счета: 1С → TMS (дубль счёта без печатных форм) ──────────────────────────
# В этой УНФ печатные формы и присоединённые файлы к документам через OData
# недоступны, поэтому тянем только данные счёта. Счёт в 1С привязан к договору и
# контрагенту (прямой ссылки на заказ нет) — заказ в TMS подбираем эвристически.

def _parse_1c_dt(v):
    """«2026-05-05T21:27:57» → date. None при пустом/нулевом значении."""
    from datetime import date as _date
    s = str(v or "")[:10]
    if not s or s.startswith("0001"):
        return None
    try:
        return _date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _guess_order_for_invoice(db: Session, cp, contract, total: float):
    """Эвристика привязки счёта 1С к заказу TMS (1С ссылку на заказ не хранит).

    Берём заказы контрагента; если задан договор — сужаем по нему; среди них ищем
    единственный с совпадающей суммой. Неоднозначность → не привязываем (None)."""
    from app.models import Order
    q = (db.query(Order)
         .filter(Order.counterparty_id == cp.id, Order.status != "cancelled"))
    candidates = q.all()
    if contract:
        narrowed = [o for o in candidates if o.contract_id == contract.id]
        if narrowed:
            candidates = narrowed
    matched = [o for o in candidates if abs((o.total_amount or 0) - total) < 0.01]
    return matched[0] if len(matched) == 1 else None


def sync_invoices_from_1c(db: Session) -> dict:
    """Тянет счета из 1С (Document_СчетНаОплату) и создаёт/обновляет их в TMS.

    Идемпотентно по external_id_1c. Если в TMS уже есть «ручной» счёт с тем же
    номером и суммой без привязки к 1С — присваиваем ему Ref_Key (не плодим дубль).
    Печатные формы не тянутся (недоступны через OData в этой конфигурации).
    """
    s = _get_settings(db)
    if not s or not s.onec_enabled:
        return {"created": 0, "updated": 0, "errors": ["Синхронизация отключена"]}

    from datetime import date as _date
    from app.models import Invoice, InvoiceItem, Counterparty, Contract, Product, Order
    from app.utils import compute_invoice_due_date

    errors: list[str] = []
    created = updated = 0

    # Индексы TMS по Ref_Key (один проход, без N запросов)
    cp_by_ref = {c.external_id_1c: c for c in
                 db.query(Counterparty).filter(Counterparty.external_id_1c.isnot(None)).all()}
    contract_by_ref = {c.external_id_1c: c for c in
                       db.query(Contract).filter(Contract.external_id_1c.isnot(None)).all()}
    product_by_ref = {p.external_id_1c: p for p in
                      db.query(Product).filter(Product.external_id_1c.isnot(None)).all()}

    try:
        with _client(s) as c:
            r = c.get("Document_СчетНаОплату", params={"$format": "json", "$top": "5000"})
        r.raise_for_status()
        docs = r.json().get("value", [])
    except Exception as e:
        logger.error("sync_invoices_from_1c: %s", e)
        return {"created": 0, "updated": 0, "errors": [str(e)]}

    for d in docs:
        ref = d.get("Ref_Key")
        if not ref or d.get("DeletionMark") or not d.get("Posted"):
            continue
        cp = cp_by_ref.get(d.get("Контрагент_Key"))
        if not cp:
            continue  # без контрагента в TMS привязать счёт некуда

        number = (d.get("Number") or "").strip()
        inv_date = _parse_1c_dt(d.get("Date"))
        total = round(float(d.get("СуммаДокумента") or 0), 2)
        contract = contract_by_ref.get(d.get("Договор_Key"))

        inv = db.query(Invoice).filter(Invoice.external_id_1c == ref).first()
        is_new = inv is None
        if is_new:
            # Попытка «усыновить» ранее заведённый вручную счёт (тот же номер+сумма)
            n = _digits_to_int(number)
            if n is not None:
                for cand in (db.query(Invoice)
                             .filter(Invoice.counterparty_id == cp.id,
                                     Invoice.external_id_1c.is_(None)).all()):
                    if _digits_to_int(cand.number) == n and round(cand.total_amount or 0, 2) == total:
                        inv = cand
                        is_new = False
                        break
        if inv is None:
            inv = Invoice(number=number or ref[:50],
                          date=inv_date or _date.today(),
                          counterparty_id=cp.id, status="issued")
            db.add(inv)

        # Шапка
        inv.external_id_1c = ref
        inv.synced_to_1c_at = datetime.now(timezone.utc).replace(tzinfo=None)
        inv.number = number or inv.number
        if inv_date:
            inv.date = inv_date
        inv.counterparty_id = cp.id
        if contract:
            inv.contract_id = contract.id
        inv.total_amount = total
        # «Оплатить до» берём из 1С — поле «Оплата» (ОплатаДо), менеджер ставит его
        # вручную на счёте. Если в 1С не заполнено — вычисляем по условиям договора.
        inv.due_date = _parse_1c_dt(d.get("ОплатаДо")) or compute_invoice_due_date(inv.date, contract, cp)
        if inv.status not in ("paid", "cancelled"):
            inv.status = "issued"
        # Привязка к заказу (только если ещё не привязан).
        if not inv.order_id:
            order = None
            # 1) Прямая ссылка из 1С: ДокументОснование → ЗаказПокупателя.
            #    Надёжнее суммы: у контрагента может быть несколько заказов с
            #    одинаковой суммой по одному договору (эвристика тогда пасует).
            osn = d.get("ДокументОснование")
            if osn and "Document_ЗаказПокупателя" in (d.get("ДокументОснование_Type") or ""):
                order = db.query(Order).filter(Order.external_id_1c == osn).first()
            # 2) Фолбэк — эвристика по сумме, если прямой ссылки нет/заказ не найден.
            if not order:
                order = _guess_order_for_invoice(db, cp, contract, total)
            if order:
                inv.order_id = order.id
        # Если счёт уже оплачен и появилась привязка к заказу — подтянуть статус заказа
        if inv.status == "paid" and inv.order_id:
            from app.utils import sync_order_paid_status
            db.flush()
            sync_order_paid_status(db, inv.order)

        # Позиции — пересобираем из табличной части «Запасы»
        rows = d.get("Запасы") or []
        if rows:
            db.flush()
            for old in list(inv.items):
                db.delete(old)
            db.flush()
            subtotal = vat_amount = 0.0
            for row in rows:
                qty = float(row.get("Количество") or 0)
                price = float(row.get("Цена") or 0)
                line_total = round(float(row.get("Всего") or row.get("Сумма") or 0), 2)
                line_vat = round(float(row.get("СуммаНДС") or 0), 2)
                disc = min(max(float(row.get("ПроцентСкидкиНаценки") or 0), 0), 100)
                base = line_total - line_vat
                vat_rate = round(line_vat / base * 100) if base > 0 and line_vat > 0 else 0.0
                product = product_by_ref.get(row.get("Номенклатура_Key"))
                name = (product.name if product else None) or "Позиция"
                unit = (product.sale_unit or product.unit) if product else "шт"
                db.add(InvoiceItem(
                    invoice_id=inv.id,
                    product_id=product.id if product else None,
                    name=name[:200], quantity=qty, unit=(unit or "шт")[:20],
                    price=price, vat_rate=vat_rate, discount_pct=disc, amount=line_total,
                ))
                subtotal += base
                vat_amount += line_vat
            inv.subtotal = round(subtotal, 2)
            inv.vat_amount = round(vat_amount, 2)

        if is_new:
            created += 1
        else:
            updated += 1

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("sync_invoices_from_1c commit: %s", e)
        return {"created": created, "updated": updated, "errors": [str(e)]}

    logger.info("sync_invoices_from_1c: создано %d, обновлено %d", created, updated)
    return {"created": created, "updated": updated, "errors": errors}


# ── Документы (Счёт PDF / УПД PDF / УПД XML): 1С → TMS через HTTP-сервис ───────
# Печатные формы и XML отдаёт расширение 1С (HTTP-сервис tms на той же публикации,
# что OData). TMS тянет файлы и прикладывает к заказу. Расходная накладная
# (Document_РасходнаяНакладная) сопоставляется с заказом по полю «Заказ» или по
# «ДокументОснование» (→ СчетНаОплату → заказ).

_ZERO_GUID_DOC = "00000000-0000-0000-0000-000000000000"


def _hs_base(s: CompanySettings) -> str | None:
    """База HTTP-сервиса расширения. Явный onec_hs_url или вывод из onec_url."""
    explicit = (getattr(s, "onec_hs_url", None) or "").strip()
    if explicit:
        return explicit.rstrip("/")
    if not s.onec_url:
        return None
    base = s.onec_url.rstrip("/").replace("/odata/standard.odata", "").rstrip("/")
    return base + "/hs/tms"


def fetch_doc_file(s: CompanySettings, kind: str, ref: str) -> bytes | None:
    """Скачивает файл из расширения 1С. kind: invoice|upd|tn (PDF) или upd_xml (XML)."""
    base = _hs_base(s)
    if not base or not ref:
        return None
    if kind == "upd_xml":
        url = f"{base}/edo?ref={ref}"
    else:
        url = f"{base}/print?type={kind}&ref={ref}"
    try:
        with httpx.Client(auth=(s.onec_user or "", s.onec_password or ""), timeout=60) as c:
            r = c.get(url)
        if r.status_code == 200 and r.content:
            return r.content
        logger.warning("fetch_doc_file %s/%s: HTTP %s %s", kind, ref, r.status_code, r.text[:200])
    except Exception as e:
        logger.error("fetch_doc_file %s/%s: %s", kind, ref, e)
    return None


def _save_order_file(db: Session, order, *, file_type: str, ext: str,
                     external_key: str, data: bytes, original_name: str,
                     source: str = "1c") -> bool:
    """Идемпотентно сохраняет файл документа как вложение заказа (PDF — со сжатием)."""
    import os, uuid as _uuid
    from app.models import AttachedFile
    from app.utils.file_compress import compress_file

    # заменяем прежнюю версию того же документа
    for old in (db.query(AttachedFile)
                .filter(AttachedFile.entity_type == "order",
                        AttachedFile.entity_id == order.id,
                        AttachedFile.external_key == external_key).all()):
        try:
            if old.stored_path and os.path.exists(old.stored_path):
                os.remove(old.stored_path)
        except OSError:
            pass
        db.delete(old)

    dest_dir = os.path.join("uploads", "orders", str(order.id))
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, f"{_uuid.uuid4().hex}{ext}")
    with open(path, "wb") as f:
        f.write(data)
    size = len(data)
    comp = size
    if ext.lower() == ".pdf":
        try:
            comp = compress_file(path, ".pdf")
        except Exception:  # noqa: BLE001
            comp = size
    db.add(AttachedFile(
        entity_type="order", entity_id=order.id, file_type=file_type,
        original_name=original_name[:300], stored_path=path.replace("\\", "/"),
        size_original=size, size_compressed=comp,
        source=source, external_key=external_key,
    ))
    return True


def sync_shipments_from_1c(db: Session) -> dict:
    """Тянет Document_РасходнаяНакладная из 1С и проставляет заказам shipment_id_1c.

    Сопоставление: поле «Заказ» (→ ЗаказПокупателя) либо «ДокументОснование»
    (→ СчетНаОплату → заказ через счёт в TMS)."""
    s = _get_settings(db)
    if not s or not s.onec_enabled:
        return {"updated": 0, "errors": ["Синхронизация отключена"]}

    from app.models import Order, Invoice
    orders_by_1c = {o.external_id_1c: o for o in
                    db.query(Order).filter(Order.external_id_1c.isnot(None)).all()}
    inv_by_1c = {i.external_id_1c: i for i in
                 db.query(Invoice).filter(Invoice.external_id_1c.isnot(None)).all()}
    updated = 0
    try:
        with _client(s) as c:
            r = c.get("Document_РасходнаяНакладная",
                      params={"$format": "json", "$top": "5000",
                              "$select": "Ref_Key,DeletionMark,Заказ,ДокументОснование,"
                                         "ДокументОснование_Type,Контрагент_Key,СуммаДокумента"})
        r.raise_for_status()
        for d in r.json().get("value", []):
            ref = d.get("Ref_Key")
            if not ref or d.get("DeletionMark"):
                continue
            order = None
            zak = d.get("Заказ")
            if zak and zak != _ZERO_GUID_DOC:
                order = orders_by_1c.get(zak)
            if not order:
                osn = d.get("ДокументОснование")
                if osn and "СчетНаОплату" in (d.get("ДокументОснование_Type") or ""):
                    inv = inv_by_1c.get(osn)
                    if inv and inv.order_id:
                        order = db.query(Order).filter(Order.id == inv.order_id).first()
            if order and order.shipment_id_1c != ref:
                order.shipment_id_1c = ref
                updated += 1
        if updated:
            db.commit()
    except Exception as e:
        db.rollback()
        logger.error("sync_shipments_from_1c: %s", e)
        return {"updated": updated, "errors": [str(e)]}
    return {"updated": updated, "errors": []}


def sync_documents_from_1c(db: Session) -> dict:
    """Тянет файлы документов (Счёт PDF, УПД PDF, УПД XML) из расширения 1С и
    прикладывает к заказам. Идемпотентно: уже скачанные (по external_key) не трогаем.
    Если HTTP-сервис недоступен — просто логируем, без падения."""
    s = _get_settings(db)
    if not s or not s.onec_enabled:
        return {"attached": 0, "errors": ["Синхронизация отключена"]}
    if not _hs_base(s):
        return {"attached": 0, "errors": ["URL HTTP-сервиса 1С не задан"]}

    from app.models import Order, Invoice, AttachedFile
    attached = 0
    errors: list[str] = []

    def _have(order_id: int, key: str) -> bool:
        return db.query(AttachedFile.id).filter(
            AttachedFile.entity_type == "order", AttachedFile.entity_id == order_id,
            AttachedFile.external_key == key).first() is not None

    orders = db.query(Order).filter(
        (Order.shipment_id_1c.isnot(None)) | (Order.id.in_(
            db.query(Invoice.order_id).filter(Invoice.external_id_1c.isnot(None),
                                              Invoice.order_id.isnot(None))))
    ).all()

    for order in orders:
        # Счёт PDF — по счёту заказа с external_id_1c
        inv = next((i for i in order.invoices if i.external_id_1c), None)
        if inv and inv.external_id_1c:
            key = f"{inv.external_id_1c}:invoice"
            if not _have(order.id, key):
                data = fetch_doc_file(s, "invoice", inv.external_id_1c)
                if data and _save_order_file(db, order, file_type="invoice", ext=".pdf",
                                             external_key=key, data=data,
                                             original_name=f"Счет {order.number}.pdf"):
                    attached += 1
        # УПД PDF + XML — по расходной накладной
        ship = order.shipment_id_1c
        if ship:
            for kind, ftype, ext, label in (
                ("upd", "upd", ".pdf", f"УПД {order.number}.pdf"),
                ("upd_xml", "upd_xml", ".xml", f"УПД {order.number}.xml"),
            ):
                key = f"{ship}:{ftype}"
                if _have(order.id, key):
                    continue
                data = fetch_doc_file(s, kind, ship)
                if data and _save_order_file(db, order, file_type=ftype, ext=ext,
                                             external_key=key, data=data,
                                             original_name=label):
                    attached += 1
        if attached:
            try:
                db.commit()
            except Exception as e:  # noqa: BLE001
                db.rollback(); errors.append(str(e))

    logger.info("sync_documents_from_1c: приложено %d", attached)
    return {"attached": attached, "errors": errors}


# ── Склад: TMS → 1С ──────────────────────────────────────────────────────────

def push_stock_movement(movement, db: Session) -> str | None:
    """
    Пушит движения типа 'in' (Document_ПоступлениеТоваров)
    и 'adjustment' (Document_ИнвентаризацияТоваров).
    Движения 'out' с order_id пропускаются — 1С создаёт их сама через заказ.
    """
    if movement.movement_type == "out" and movement.order_id:
        return None

    s = _get_settings(db)
    if not s or not s.onec_enabled:
        return None

    if not movement.product or not movement.product.external_id_1c:
        return None

    doc_type = (
        "Document_ПоступлениеТоваров"
        if movement.movement_type == "in"
        else "Document_ИнвентаризацияТоваров"
    )

    payload = {
        "Date": movement.date.isoformat() if movement.date else None,
        "Комментарий": movement.notes or "",
        "Товары": [{
            "Номенклатура_Key": movement.product.external_id_1c,
            "Количество": movement.quantity,
        }],
    }

    try:
        with _client(s) as c:
            if movement.external_id_1c:
                c.patch(f"{doc_type}(guid'{movement.external_id_1c}')", json=payload)
                _save_external_id(db, movement, movement.external_id_1c)
                return movement.external_id_1c
            r = c.post(doc_type, json=payload)
            r.raise_for_status()
            ref_key = r.json().get("Ref_Key")
            if ref_key:
                _save_external_id(db, movement, ref_key)
            return ref_key
    except Exception as e:
        logger.error("push_stock_movement %s: %s", movement.id, e)
        return None

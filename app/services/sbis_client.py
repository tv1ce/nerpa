"""
СБИС (Saby) API client — ЭДО/ЭПД/ЭТРН интеграция.

API: JSON-RPC 2.0
Docs: https://online.sbis.ru/page/sbis-api
"""
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SBIS_AUTH_URL = "https://online.sbis.ru/auth/service/"
SBIS_API_URL  = "https://online.sbis.ru/service/?srv=1"

DADATA_TOKEN = os.getenv("DADATA_TOKEN", "")


def _lookup_party_by_inn(inn: str) -> Optional[dict]:
    """Свежие реквизиты компании по ИНН через DaData (грузополучатель ЭТРН
    заполняется по актуальным данным, а не по тому, что могло устареть в TMS)."""
    if not inn or not DADATA_TOKEN:
        return None
    try:
        resp = httpx.post(
            "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party",
            headers={
                "Authorization": f"Token {DADATA_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={"query": inn},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("DaData: не удалось найти компанию по ИНН %s: %s", inn, e)
        return None
    suggestions = data.get("suggestions") or []
    if not suggestions:
        return None
    s = suggestions[0]["data"]
    name_block = s.get("name") or {}
    addr = s.get("address") or {}
    return {
        "name": name_block.get("full_with_opf") or name_block.get("short_with_opf") or "",
        "kpp":  s.get("kpp") or "",
        "address": addr.get("value") or "",
    }

# Статусы ЭТРН (СБИС → TMS)
ETRAN_STATUS_MAP = {
    "draft":     "черновик",
    "send":      "отправлен",
    "signed":    "подписан",
    "complete":  "завершён",
    "revoked":   "отозван",
    "error":     "ошибка",
}


class SbisError(Exception):
    pass


def _raise_for_sbis_status(resp: httpx.Response) -> None:
    """Как resp.raise_for_status(), но при ошибке достаёт текст из тела ответа
    (СБИС часто кладёт понятное описание ошибки в JSON даже при HTTP 4xx/5xx)."""
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
        msg = body.get("error", {}).get("message") or body
    except Exception:
        msg = resp.text[:500]
    raise SbisError(f"HTTP {resp.status_code} от СБИС: {msg}")


class SbisClient:
    """Клиент СБИС JSON-RPC API. Один экземпляр = одна сессия."""

    def __init__(self, login: str, password: str, account_id: str | None = None):
        self.login = login
        self.password = password
        self.account_id = account_id or None
        self._session_id: Optional[str] = None
        self._http = httpx.Client(timeout=30, headers={"Content-Type": "application/json; charset=UTF-8"})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._http.close()

    # ── Аутентификация ──────────────────────────────────────────────────────

    def authenticate(self) -> str:
        auth_param = {"Логин": self.login, "Пароль": self.password}
        if self.account_id:
            auth_param["НомерАккаунта"] = self.account_id
        resp = self._http.post(SBIS_AUTH_URL, json={
            "jsonrpc": "2.0",
            "method": "СБИС.Аутентифицировать",
            "params": {"Параметр": auth_param},
            "id": "0",
        })
        _raise_for_sbis_status(resp)
        data = resp.json()
        if "error" in data:
            raise SbisError(f"Ошибка авторизации СБИС: {data['error'].get('message', data['error'])}")
        # Результат — просто строка с идентификатором сессии, не объект
        self._session_id = data["result"]
        logger.info("СБИС: авторизация успешна")
        return self._session_id

    # ── Базовый вызов ───────────────────────────────────────────────────────

    def _call(self, method: str, params: dict, retry: bool = True) -> dict:
        if not self._session_id:
            self.authenticate()
        resp = self._http.post(
            SBIS_API_URL,
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": "1"},
            headers={"X-SBISSessionID": self._session_id},
            timeout=60,
        )
        _raise_for_sbis_status(resp)
        data = resp.json()
        if "error" in data:
            err = data["error"]
            code = err.get("code", 0)
            msg  = err.get("message", str(err))
            # Сессия протухла — переавторизуемся один раз
            if code in (401, -1) and retry:
                logger.warning("СБИС: сессия протухла, переавторизуюсь")
                self._session_id = None
                return self._call(method, params, retry=False)
            raise SbisError(f"СБИС API [{code}]: {msg}")
        return data["result"]

    # ── ЭТРН ────────────────────────────────────────────────────────────────

    def create_etran(self, order, company) -> dict:
        """Создаёт черновик ЭТРН в СБИС. Возвращает {id, url}."""
        doc = self._build_document(order, company)
        result = self._call("СБИС.ЗаписатьДокумент", {"Документ": doc})
        doc_id = result.get("Идентификатор") or result.get("id") or result.get("Документ", {}).get("Идентификатор")
        url = f"https://online.sbis.ru/opendoc.html?guid={doc_id}" if doc_id else None
        logger.info("СБИС: создан ЭТРН id=%s для заказа #%s", doc_id, order.number)
        return {"id": doc_id, "url": url, "raw": result}

    # ── ЭДО: счёт / УПД ──────────────────────────────────────────────────────

    def write_edo_document(self, doc_type: str, vlozh_type: str,
                           attachment_b64: str, filename: str,
                           doc_fields: dict | None = None) -> dict:
        """СБИС.ЗаписатьДокумент для ЭДО-документа (счёт «СчетИсх»/«ЭДОСч» или
        УПД «ДокОтгрИсх»/«УпдСчфДоп»). Вложение — готовый файл в base64
        (счёт — PDF, УПД — формализованный XML).

        doc_fields — поля уровня документа (Контрагент, Номер, Дата, Сумма и т.п.),
        чтобы карточка в СБИС была заполнена, а не пустая. Возвращает объект документа."""
        doc = {
            "Тип": doc_type,
            "Вложение": [{
                "Тип": vlozh_type,
                "Файл": {"ДвоичныеДанные": attachment_b64, "Имя": filename},
            }],
        }
        if doc_fields:
            doc.update(doc_fields)
        result = self._call("СБИС.ЗаписатьДокумент", {"Документ": doc})
        return result if isinstance(result, dict) else {}

    @staticmethod
    def kontragent_block(cp) -> dict:
        """Блок «Контрагент» (получатель) для ЭДО-документа. СБИС сматчит его по ИНН.
        Ключ реквизита именно «ИНН» (не «ИННЮЛ») — см. пример СБИС.ЗаписатьДокумент."""
        if cp is None:
            return {}
        inn = getattr(cp, "inn", "") or ""
        name = getattr(cp, "trade_name", None) or getattr(cp, "name", "") or ""
        if getattr(cp, "entity_type", "ooo") == "ip":
            return {"СвИП": {"ИННФЛ": inn, "Наименование": name}}
        return {"СвЮЛ": {
            "ИНН": inn, "КПП": getattr(cp, "kpp", "") or "",
            "КодСтраны": "643", "Название": name,
        }}

    @staticmethod
    def nasha_org_block(company) -> dict:
        """Блок «НашаОрганизация» (отправитель) для ЭДО-документа."""
        if company is None:
            return {}
        return {"СвЮЛ": {
            "ИНН": getattr(company, "inn", "") or "",
            "КПП": getattr(company, "kpp", "") or "",
            "КодСтраны": "643", "Название": getattr(company, "name", "") or "",
        }}

    @staticmethod
    def doc_link(doc_id: str) -> str:
        return f"https://online.sbis.ru/opendoc.html?guid={doc_id}" if doc_id else ""

    def get_status(self, etran_id: str) -> str:
        """Возвращает текстовый статус ЭТРН из СБИС."""
        result = self._call("СБИС.ПрочитатьДокумент", {"Идентификатор": etran_id})
        doc = result if isinstance(result, dict) else {}
        raw_status = (
            doc.get("Состояние")
            or doc.get("Статус")
            or doc.get("Документ", {}).get("Состояние")
            or "unknown"
        )
        return ETRAN_STATUS_MAP.get(raw_status.lower(), raw_status)

    # ── Построение документа ────────────────────────────────────────────────

    def _build_document(self, order, company) -> dict:
        carrier = order.carrier
        cp      = order.counterparty

        # Грузополучатель — ищем свежие реквизиты по ИНН через DaData,
        # свои данные (адрес доставки заказа) остаются приоритетными
        dadata_info = _lookup_party_by_inn(cp.inn) if cp.inn else None
        receiver_name = (dadata_info or {}).get("name") or cp.trade_name or cp.name or ""
        receiver_kpp  = (dadata_info or {}).get("kpp") or ""
        receiver_addr = order.delivery_address or (dadata_info or {}).get("address") or cp.actual_address or ""

        items = []
        for i in order.items:
            if i.product:
                items.append({
                    "НаименованиеГруза": i.product.name,
                    "КоличествоМест":    str(int(i.quantity)),
                    "МассаБрутто":       "0",
                    "ЕдИзм":             i.product.unit or "шт",
                })

        # NB: реальная схема полей ConsignmentNote нигде в открытой документации
        # Saby не описана. В первом тесте поля внутри "Вложение" были молча
        # проигнорированы СБИС (создался только "Отправитель" из НашаОрганизация) —
        # похоже, специфичные для типа документа поля должны быть на верхнем уровне
        # объекта "Документ", а не вложены в "Вложение". Пробуем так.
        return {
            "Тип": "ConsignmentNote",
            "НашаОрганизация": {
                "СвЮЛ": {
                    "ИННЮЛ":   company.inn or "",
                    "КПП":     company.kpp or "",
                    "НаимОрг": company.name or "",
                }
            },
            "Контрагент": {
                "СвЮЛ": {
                    "ИННЮЛ":   cp.inn or "",
                    "КПП":     receiver_kpp,
                    "НаимОрг": receiver_name,
                }
            },
            "Получатель": {
                "ИНН":          cp.inn or "",
                "КПП":          receiver_kpp,
                "Наименование": receiver_name,
                "Адрес":        receiver_addr,
            },
            "Перевозчик": {
                "ИНН":          carrier.inn if carrier else "",
                "Наименование": (carrier.trade_name or carrier.name) if carrier else "",
            },
            "ТС": {
                "ГосНомер": order.vehicle_plate or "",
                "Марка":    order.vehicle_type or "",
            },
            "Водитель": {
                "ФИО": order.driver_name or "",
            },
            "ПунктПогрузки": {
                "Адрес": order.pickup_address or company.actual_address or "",
            },
            "ПунктВыгрузки": {
                "Адрес": receiver_addr,
            },
            "АдресДоставки": receiver_addr,
            "Груз": items,
            "ДатаОтгрузки":  order.date.isoformat() if order.date else "",
            "СрокДоставки":  order.delivery_date.isoformat() if order.delivery_date else "",
            "ВремяДоставки": order.delivery_time or "",
        }


def get_sbis_client(company) -> Optional[SbisClient]:
    """Создаёт клиент из настроек компании. Возвращает None если не настроен."""
    if not company or not company.sbis_login or not company.sbis_password:
        return None
    return SbisClient(login=company.sbis_login, password=company.sbis_password,
                       account_id=company.sbis_account_id)


# ── Поллинг статусов ЭДО-документов (счёт / УПД) ─────────────────────────────

# Ключевые слова статуса, при появлении которых уведомляем менеджера
# (контрагент подписал/завершил/отклонил/аннулировал документооборот).
_EDO_NOTIFY_KEYS = ("подпис", "заверш", "отклон", "аннул", "revoked")


def poll_sbis_edo_statuses(db) -> dict:
    """Опрашивает статусы счетов и УПД, отправленных в СБИС ЭДО, через
    СБИС.ПрочитатьДокумент и обновляет их в TMS. При переходе в терминальный
    статус (подписан/завершён/отклонён) уведомляет ответственного менеджера.

    Вызывается периодически из APScheduler (см. app/main.py)."""
    from app.models import CompanySettings, Order, Invoice, Notification

    company = db.query(CompanySettings).first()
    client = get_sbis_client(company)
    if not client:
        return {"checked": 0, "updated": 0}

    invoices = db.query(Invoice).filter(Invoice.sbis_doc_id.isnot(None)).all()
    orders = db.query(Order).filter(Order.upd_sbis_id.isnot(None)).all()
    if not invoices and not orders:
        return {"checked": 0, "updated": 0}

    updated = 0

    def _notify(status, title, link, uid):
        if any(k in (status or "").lower() for k in _EDO_NOTIFY_KEYS):
            db.add(Notification(type="sbis_edo", title=title,
                                body=f"Статус в СБИС: {status}.", link=link, user_id=uid))

    with client:
        for inv in invoices:
            try:
                status = client.get_status(inv.sbis_doc_id)
            except SbisError as e:
                logger.warning("СБИС статус счёта #%s: %s", inv.number, e)
                continue
            if status and status != inv.sbis_status:
                inv.sbis_status = status
                updated += 1
                uid = inv.order.sales_manager_id if inv.order else None
                _notify(status, f"Счёт №{inv.number} в СБИС: {status}", f"/invoices/{inv.id}", uid)
        for o in orders:
            try:
                status = client.get_status(o.upd_sbis_id)
            except SbisError as e:
                logger.warning("СБИС статус УПД заказа #%s: %s", o.number, e)
                continue
            if status and status != o.upd_sbis_status:
                o.upd_sbis_status = status
                updated += 1
                _notify(status, f"УПД по заказу №{o.number} в СБИС: {status}",
                        f"/orders/{o.id}", o.sales_manager_id or o.created_by_id)

    if updated:
        db.commit()
    return {"checked": len(invoices) + len(orders), "updated": updated}

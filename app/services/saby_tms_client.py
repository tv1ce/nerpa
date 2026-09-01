"""
Saby (СБИС) «Управление транспортом» — API заказов на перевозку (ЭЗЗ) и ЭТрН.

Отдельный от app/services/sbis_client.py клиент, потому что документы транспортного
модуля живут НЕ на online.sbis.ru, а на выделенном эндпоинте tms.saby.ru/service/
и работают по другому флоу (генерация вложения → запись → подготовка → подпись).

Порядок вызовов (одинаков для «Заказ на перевозку» и «ЭТрН»):
  1. СБИС.Аутентифицировать               — логин/пароль → идентификатор сессии
  2. СБИС.СгенерироватьВложение           — данные «ключ-значение» → base64-XML вложения
  3. СБИС.ЗаписатьДокумент                 — создаёт документ-черновик с вложением
  4. СБИС.ПодготовитьДействие              — дозаполняет служебку, отдаёт хеш на подпись
  5. СБИС.ВыполнитьДействие                — ПОДПИСЫВАЕТ и отправляет (требует ЭП)
  6. СБИС.СписокИзменений / ПрочитатьДокумент — статусы/чтение

ВАЖНО про подпись: шаг 5 требует закрытого ключа ЭП. При физическом сертификате
(КриптоПро/токен) сервер подписать не может — NERPA доводит документ до ЧЕРНОВИКА
(шаги 2-3, опц. 4), а подписание и отправку менеджер делает вручную в кабинете СБИС
по ссылке. Метод execute_action оставлен для будущей клиентской подписи (плагин
КриптоПро) и в текущем MVP не вызывается автоматически.

Типы документов:
  ЭЗЗ  (заказ-заявка):  Документ.Тип = "TransportOrder", вложение "ЗаказЗаявка" (КНД 1110361)
  ЭТрН (накладная):     Документ.Тип = "ConsignmentNote", вложение "ЭТрН" (титулы 1110339…1110346)

Docs (PDF): «Заказы на перевозку», «ЭТрН» — Saby «Управление транспортом».
"""
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Аутентификация — общий контур СБИС; сессия валидна и для транспортного эндпоинта.
SABY_AUTH_URL = "https://online.sbis.ru/auth/service/"
# Документы транспортного модуля — выделенный эндпоинт.
SABY_TMS_URL  = "https://tms.saby.ru/service/"

# Актуальная версия формата вложений транспортного модуля.
FORMAT_VERSION = "5.01"

# Тип документа (Документ.Тип) и параметры вложения по видам ЭПД.
DOC_TRANSPORT_ORDER = "TransportOrder"    # Заказ-заявка перевозчику (ЭЗЗ)
DOC_CONSIGNMENT_NOTE = "ConsignmentNote"  # Электронная транспортная накладная (ЭТрН)

VLOZH_TYPE_ORDER = "ЗаказЗаявка"          # тип вложения ЭЗЗ
VLOZH_SUBTYPE_ORDER = "1110361"           # КНД заказ-заявки

VLOZH_TYPE_ETRAN = "ЭТрН"                 # тип вложения ЭТрН
# Титулы ЭТрН (КНД): грузоотправитель — стартовый титул, с которого начинается ЭДО.
ETRAN_TITLE_SHIPPER = "1110339"           # титул грузоотправителя

# Коды состояний из СБИС.СписокИзменений (Состояние.Код).
STATE_EDITING   = "0"   # документ редактируется / ожидает отправки (черновик)
STATE_CARRIER   = "4"   # доставлен, нужны данные грузоперевозчика
STATE_APPROVED  = "7"   # утверждён
STATE_REJECTED  = "9"   # закрыт с отрицательным итогом (отклонён)

STATE_LABELS = {
    STATE_EDITING:  "черновик",
    "1":            "отправлен",
    STATE_CARRIER:  "ожидает перевозчика",
    STATE_APPROVED:  "утверждён",
    STATE_REJECTED: "отклонён",
}


class SabyTmsError(Exception):
    pass


def _raise_for_status(resp: httpx.Response) -> None:
    """raise_for_status с извлечением понятного текста ошибки из тела ответа СБИС."""
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
        msg = body.get("error", {}).get("message") or body
    except Exception:
        msg = resp.text[:500]
    raise SabyTmsError(f"HTTP {resp.status_code} от Saby: {msg}")


class SabyTmsClient:
    """Клиент транспортного API Saby (JSON-RPC 2.0). Один экземпляр = одна сессия."""

    def __init__(self, login: str, password: str, account_id: str | None = None):
        self.login = login
        self.password = password
        self.account_id = account_id or None
        self._session_id: Optional[str] = None
        self._http = httpx.Client(
            timeout=60,
            headers={"Content-Type": "application/json; charset=UTF-8"},
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._http.close()

    # ── Аутентификация ──────────────────────────────────────────────────────

    def authenticate(self) -> str:
        auth_param = {"Логин": self.login, "Пароль": self.password}
        if self.account_id:
            auth_param["НомерАккаунта"] = self.account_id
        resp = self._http.post(SABY_AUTH_URL, json={
            "jsonrpc": "2.0",
            "method": "СБИС.Аутентифицировать",
            "params": {"Параметр": auth_param},
            "id": "0",
        })
        _raise_for_status(resp)
        data = resp.json()
        if "error" in data:
            raise SabyTmsError(f"Ошибка авторизации Saby: {data['error'].get('message', data['error'])}")
        self._session_id = data["result"]
        logger.info("Saby NERPA: авторизация успешна")
        return self._session_id

    # ── Базовый вызов транспортного эндпоинта ───────────────────────────────

    def _call(self, method: str, params: dict, retry: bool = True):
        if not self._session_id:
            self.authenticate()
        resp = self._http.post(
            SABY_TMS_URL,
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": "1"},
            headers={"X-SBISSessionID": self._session_id},
        )
        _raise_for_status(resp)
        data = resp.json()
        if "error" in data:
            err = data["error"]
            code = err.get("code", 0)
            msg = err.get("message", str(err))
            if code in (401, -1) and retry:
                logger.warning("Saby NERPA: сессия протухла, переавторизуюсь")
                self._session_id = None
                return self._call(method, params, retry=False)
            raise SabyTmsError(f"Saby NERPA API [{code}]: {msg}")
        return data["result"]

    # ── Шаг 2: генерация вложения из подстановок ─────────────────────────────

    def generate_attachment(self, vlozh_type: str, subtype: str, substitution: dict,
                            version: str = FORMAT_VERSION) -> dict:
        """СБИС.СгенерироватьВложение — из данных «ключ-значение» собирает XML вложения
        по утверждённому формату. Возвращает {ДвоичныеДанные(base64), Имя}."""
        result = self._call("СБИС.СгенерироватьВложение", {
            "Документ": {
                "Вложение": [{
                    "Тип": vlozh_type,
                    "Подтип": subtype,
                    "ВерсияФормата": version,
                    "ПодверсияФормата": "",
                    "Подстановка": substitution,
                }]
            }
        })
        vlozh = (result or {}).get("Вложение") or []
        if not vlozh or not vlozh[0].get("Файл"):
            raise SabyTmsError(f"Saby не вернул сгенерированное вложение: {result}")
        return vlozh[0]["Файл"]  # {"ДвоичныеДанные": "...", "Имя": "...xml"}

    # ── Шаг 3: запись документа ──────────────────────────────────────────────

    def write_document(self, doc_type: str, reglament_name: str, our_org: dict,
                       attachment: dict, doc_id: str | None = None) -> dict:
        """СБИС.ЗаписатьДокумент — создаёт документ-черновик (или обновляет, если doc_id).
        attachment — {ДвоичныеДанные, Имя} из generate_attachment.
        Возвращает объект документа СБИС (в т.ч. Идентификатор, ссылки, Этап)."""
        doc = {
            "Тип": doc_type,
            "Регламент": {"Название": reglament_name},
            "НашаОрганизация": our_org,
            "Вложение": [{"Файл": attachment}],
        }
        if doc_id:
            doc["Идентификатор"] = doc_id
        return self._call("СБИС.ЗаписатьДокумент", {"Документ": doc}) or {}

    # ── Шаг 4: подготовка действия (перед подписанием) ───────────────────────

    def prepare_action(self, doc_id: str, action_name: str, certificate: dict) -> dict:
        """СБИС.ПодготовитьДействие — дозаполняет служебные теги (Отправитель/Получатель/
        Подписант), формирует имя файла и отдаёт вложение/хеш на подпись.
        certificate: {ФИО, Должность, ИНН} или {Отпечаток}."""
        return self._call("СБИС.ПодготовитьДействие", {
            "Документ": {
                "Идентификатор": doc_id,
                "Этап": {"Действие": {"Название": action_name, "Сертификат": certificate}},
            }
        }) or {}

    # ── Шаг 5: подписание и отправка (требует ЭП — в MVP не используется авто) ─

    def execute_action(self, doc_id: str, action_name: str, certificate: dict,
                       attachment_id: str, signature_b64: str) -> dict:
        """СБИС.ВыполнитьДействие — переход на следующий этап с подписью.
        signature_b64 — открепленная подпись (PKCS#7) в base64, посчитанная клиентом
        (КриптоПро). Для физического сертификата вызывается НЕ с сервера."""
        return self._call("СБИС.ВыполнитьДействие", {
            "Документ": {
                "Идентификатор": doc_id,
                "Этап": {
                    "Действие": [{"Название": action_name, "Сертификат": certificate}],
                    "Вложение": [{
                        "Идентификатор": attachment_id,
                        "Подпись": [{"Файл": {"ДвоичныеДанные": signature_b64}}],
                    }],
                },
            }
        }) or {}

    # ── Шаг 6: статусы и чтение ──────────────────────────────────────────────

    def list_changes(self, doc_type: str, date_from: str | None = None,
                     date_to: str | None = None, page_size: int = 50,
                     event_id: str | None = None) -> dict:
        """СБИС.СписокИзменений — события/статусы документов за период.
        date_from/date_to: «ДД.ММ.ГГГГ ЧЧ.ММ.СС». Возвращает {Документ:[...], Навигация:{}}."""
        flt = {
            "Тип": doc_type,
            "Навигация": {"РазмерСтраницы": str(page_size)},
            "ПолныйСертификатЭП": "Нет",
            "ДопПоля": "ДопДействия,ТекущиеЭтапы,Подстановки",
        }
        if date_from:
            flt["ДатаВремяС"] = date_from
        if date_to:
            flt["ДатаВремяПо"] = date_to
        if event_id:
            flt["ИдентификаторСобытия"] = event_id
        return self._call("СБИС.СписокИзменений", {"Фильтр": flt}) or {}

    def read_document(self, doc_id: str) -> dict:
        """СБИС.ПрочитатьДокумент — полный объект документа по идентификатору."""
        return self._call("СБИС.ПрочитатьДокумент", {"Идентификатор": doc_id}) or {}


def get_saby_tms_client(company) -> Optional[SabyTmsClient]:
    """Создаёт клиент из настроек компании (те же логин/пароль СБИС). None — если не настроен."""
    if not company or not company.sbis_login or not company.sbis_password:
        return None
    return SabyTmsClient(login=company.sbis_login, password=company.sbis_password,
                         account_id=company.sbis_account_id)


def our_org_from_company(company) -> dict:
    """Блок «НашаОрганизация» для ЗаписатьДокумент — реквизиты грузоотправителя (нас).

    Всегда СвЮЛ, в том числе когда мы ИП. Ветку СвИП/ИННФЛ здесь пробовали (форма
    взята из ЭДО-клиента, sbis_client.kontragent_block) — транспортный эндпоинт
    tms.saby.ru её не понимает: не распознав ключ, отвечает HTTP 500 «Для создания
    документа необходимо передать реквизит "НашаОрганизация"». Формы блоков у
    online.sbis.ru и tms.saby.ru не совпадают, переносить между ними нельзя.

    12-значный ИНН ИП здесь допустим: это внутреннее поле Saby для матчинга
    аккаунта по ИНН, а не тег ИННЮЛ из формата ФНС. Ошибка «в документе ошибка,
    ИНН 12 цифр вместо 10» приходила не отсюда, а из титула — см. saby_docs._is_ip.
    """
    return {
        "СвЮЛ": {
            "ИНН": company.inn or "",
            "КПП": company.kpp or "",
            "КодСтраны": "643",
            "Название": company.name or "",
            "НазваниеПолное": company.name or "",
        }
    }


def state_label(code: str) -> str:
    """Человекочитаемый статус по коду состояния СБИС."""
    return STATE_LABELS.get(str(code), f"код {code}")


# ── Поллинг статусов заказов-заявок и ЭТрН (Фаза 3) ──────────────────────────

def poll_saby_tms_statuses(db) -> dict:
    """Опрашивает СБИС.СписокИзменений по обоим типам документов и обновляет
    статусы заказов, у которых есть привязанный документ Saby. При переходе в
    «утверждён»/«отклонён» уведомляет ответственного менеджера.

    Вызывается периодически из APScheduler (см. app/main.py)."""
    from datetime import datetime, timedelta
    from app.models import CompanySettings, Order, Notification

    company = db.query(CompanySettings).first()
    client = get_saby_tms_client(company)
    if not client:
        return {"checked": 0, "updated": 0}

    to_orders = db.query(Order).filter(Order.transport_order_id.isnot(None)).all()
    etran_orders = db.query(Order).filter(Order.etran_id.isnot(None)).all()
    if not to_orders and not etran_orders:
        return {"checked": 0, "updated": 0}

    date_from = (datetime.now() - timedelta(days=45)).strftime("%d.%m.%Y %H.%M.%S")
    updated = 0

    def _apply(doc_type, orders, id_attr, status_attr, label):
        nonlocal updated
        if not orders:
            return
        try:
            result = client.list_changes(doc_type, date_from=date_from, page_size=50)
        except SabyTmsError as e:
            logger.warning("Saby NERPA поллинг [%s]: %s", doc_type, e)
            return
        docs = (result or {}).get("Документ") or []
        if isinstance(docs, dict):
            docs = [docs]
        by_id = {d.get("Идентификатор"): (d.get("Состояние") or {}).get("Код", "")
                 for d in docs if d.get("Идентификатор")}
        for o in orders:
            code = by_id.get(getattr(o, id_attr))
            if not code:
                continue
            new_status = state_label(code)
            if getattr(o, status_attr) == new_status:
                continue
            setattr(o, status_attr, new_status)
            updated += 1
            if str(code) in (STATE_APPROVED, STATE_REJECTED):
                verb = "утверждён" if str(code) == STATE_APPROVED else "отклонён"
                uid = o.sales_manager_id or o.created_by_id
                db.add(Notification(
                    type="saby_status",
                    title=f"{label} по заказу №{o.number}: {verb}",
                    body=f"Контрагент: {o.counterparty.name if o.counterparty else '—'}. Статус в Saby: {new_status}.",
                    link=f"/orders/{o.id}",
                    user_id=uid,
                ))

    with client:
        _apply(DOC_TRANSPORT_ORDER, to_orders, "transport_order_id", "transport_order_status", "Заказ-заявка")
        _apply(DOC_CONSIGNMENT_NOTE, etran_orders, "etran_id", "etran_status", "ЭТрН")

    if updated:
        db.commit()
    return {"checked": len(to_orders) + len(etran_orders), "updated": updated}

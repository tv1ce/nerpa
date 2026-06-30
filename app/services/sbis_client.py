"""
СБИС (Saby) API client — ЭДО/ЭПД/ЭТРН интеграция.

API: JSON-RPC 2.0
Docs: https://online.sbis.ru/page/sbis-api
"""
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SBIS_AUTH_URL = "https://online.sbis.ru/auth/service/"
SBIS_API_URL  = "https://online.sbis.ru/service/?srv=1"

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


class SbisClient:
    """Клиент СБИС JSON-RPC API. Один экземпляр = одна сессия."""

    def __init__(self, login: str, password: str):
        self.login = login
        self.password = password
        self._session_id: Optional[str] = None
        self._http = httpx.Client(timeout=30)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._http.close()

    # ── Аутентификация ──────────────────────────────────────────────────────

    def authenticate(self) -> str:
        resp = self._http.post(SBIS_AUTH_URL, json={
            "jsonrpc": "2.0",
            "method": "СБИС.Аутентифицировать",
            "params": {"Логин": self.login, "Пароль": self.password},
            "id": 0,
        })
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise SbisError(f"Ошибка авторизации СБИС: {data['error'].get('message', data['error'])}")
        self._session_id = data["result"]["Сессия"]
        logger.info("СБИС: авторизация успешна")
        return self._session_id

    # ── Базовый вызов ───────────────────────────────────────────────────────

    def _call(self, method: str, params: dict, retry: bool = True) -> dict:
        if not self._session_id:
            self.authenticate()
        resp = self._http.post(
            SBIS_API_URL,
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
            cookies={"SBIS3SESSIONID": self._session_id},
            timeout=60,
        )
        resp.raise_for_status()
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

    def send_etran(self, etran_id: str) -> dict:
        """Отправляет ЭТРН на подписание всем участникам."""
        result = self._call("СБИС.ОтправитьДокумент", {"Идентификатор": etran_id})
        logger.info("СБИС: ЭТРН %s отправлен на подписание", etran_id)
        return result

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

        items = []
        for i in order.items:
            if i.product:
                items.append({
                    "НаименованиеГруза": i.product.name,
                    "КоличествоМест":    str(int(i.quantity)),
                    "МассаБрутто":       "0",
                    "ЕдИзм":             i.product.unit or "шт",
                })

        return {
            "Тип": "ТН",
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
                    "НаимОрг": cp.name or "",
                }
            },
            "Вложение": [{
                "Тип": "ЭТРН",
                "Документ": {
                    "Грузоотправитель": {
                        "ИНН":          company.inn or "",
                        "КПП":          company.kpp or "",
                        "Наименование": company.name or "",
                        "Адрес":        company.actual_address or company.legal_address or "",
                    },
                    "Грузополучатель": {
                        "ИНН":          cp.inn or "",
                        "Наименование": cp.name or "",
                        "Адрес":        order.delivery_address or cp.actual_address or "",
                    },
                    "Перевозчик": {
                        "ИНН":          carrier.inn if carrier else "",
                        "Наименование": carrier.name if carrier else "",
                    },
                    "ТС": {
                        "ГосНомер": order.vehicle_plate or "",
                        "Вид":      order.vehicle_type or "Автомобиль",
                    },
                    "Водитель": {
                        "ФИО": order.driver_name or "",
                    },
                    "ПунктПогрузки": {
                        "Адрес": order.pickup_address or company.actual_address or "",
                    },
                    "ПунктВыгрузки": {
                        "Адрес": order.delivery_address or "",
                    },
                    "Груз": items,
                    "ДатаОтгрузки": order.delivery_date.isoformat() if order.delivery_date else "",
                }
            }]
        }


def get_sbis_client(company) -> Optional[SbisClient]:
    """Создаёт клиент из настроек компании. Возвращает None если не настроен."""
    if not company or not company.sbis_login or not company.sbis_password:
        return None
    return SbisClient(login=company.sbis_login, password=company.sbis_password)

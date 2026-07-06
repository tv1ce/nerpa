"""
Клиент к API банка «Точка» (Открытый банк) — авто-разнос оплат по счетам ТМС.

База:  https://enter.tochka.com/uapi/
Авторизация: Bearer <JWT-ключ> (раздел «Интеграции и API → JWT-ключи» в кабинете).
Приём оплат идёт двумя путями (см. app/routers/api_tochka.py):
  • опрос выписки (этот модуль, sync_payments_from_tochka) — раз в ~15 мин;
  • вебхук incomingPayment — мгновенно.
Оба сводятся к общему apply_payment() (app/utils) — единый журнал и дедуп.
"""
import base64
import json
import logging
import time
from datetime import date, timedelta

import httpx
from sqlalchemy.orm import Session

from app.models import CompanySettings

logger = logging.getLogger(__name__)

BASE_URL = "https://enter.tochka.com/uapi/"
OB = "open-banking/v1.0"          # префикс методов Открытого банка
WH = "webhook/v1.0"              # префикс методов вебхуков
TIMEOUT = 30
STATEMENT_DAYS = 35               # глубина выписки (≈ «последний месяц»)
# События, на которые подписываемся (все виды входящих поступлений).
WEBHOOK_EVENTS = ["incomingPayment", "incomingSbpPayment", "incomingSbpB2BPayment"]


# ── Вспомогательные ──────────────────────────────────────────────────────────

def _get_settings(db: Session) -> CompanySettings | None:
    s = db.query(CompanySettings).first()
    if not s or not s.tochka_token:
        return None
    return s


def _client(s: CompanySettings) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        headers={
            "Authorization": f"Bearer {s.tochka_token}",
            "Content-Type": "application/json",
        },
        timeout=TIMEOUT,
    )


def _unwrap(node):
    """Точка иногда отдаёт объект как одноэлементный список — приводим к dict."""
    if isinstance(node, list):
        return node[0] if node else {}
    return node or {}


# ── Счета ────────────────────────────────────────────────────────────────────

def list_accounts(c: httpx.Client) -> list[dict]:
    r = c.get(f"{OB}/accounts")
    r.raise_for_status()
    return _unwrap(r.json().get("Data", {})).get("Account", []) or []


def _resolve_account_id(c: httpx.Client, s: CompanySettings) -> str | None:
    """accountId из настроек, иначе — единственный рублёвый счёт из /accounts."""
    if s.tochka_account_id:
        return s.tochka_account_id
    accounts = list_accounts(c)
    rub = [a for a in accounts if (a.get("currency") or "RUB") == "RUB"]
    pick = rub or accounts
    return pick[0].get("accountId") if len(pick) == 1 else (pick[0].get("accountId") if pick else None)


def test_connection(db: Session) -> dict:
    """Проверка токена/доступа: список счетов. Не требует tochka_enabled."""
    s = _get_settings(db)
    if not s:
        return {"ok": False, "message": "JWT-токен Точки не задан в настройках"}
    try:
        with _client(s) as c:
            accounts = list_accounts(c)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code in (401, 403):
            return {"ok": False, "message": f"Токен отклонён (HTTP {code}). Проверьте JWT-ключ и его права на выписки."}
        return {"ok": False, "message": f"HTTP {code}: {e.response.text[:200]}"}
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"Не удалось подключиться: {e}"}
    if not accounts:
        return {"ok": False, "message": "Подключение есть, но счетов не видно (проверьте права ключа)."}
    names = ", ".join(a.get("accountId", "?") for a in accounts)
    return {"ok": True, "message": f"Подключение успешно. Счета: {names}"}


# ── Выписка ──────────────────────────────────────────────────────────────────

def _init_statement(c: httpx.Client, account_id: str, d_from: date, d_to: date) -> str | None:
    body = {"Data": {"Statement": {
        "accountId": account_id,
        "startDateTime": d_from.isoformat(),
        "endDateTime": d_to.isoformat(),
    }}}
    r = c.post(f"{OB}/statements", json=body)
    r.raise_for_status()
    stmt = _unwrap(r.json().get("Data", {}).get("Statement"))
    return stmt.get("statementId")


def _get_statement(c: httpx.Client, account_id: str, statement_id: str) -> dict:
    """Опрашивает готовность выписки, возвращает объект Statement с Transaction[]."""
    url = f"{OB}/accounts/{account_id}/statements/{statement_id}"
    for _ in range(8):
        r = c.get(url)
        r.raise_for_status()
        stmt = _unwrap(r.json().get("Data", {}).get("Statement"))
        status = stmt.get("status")
        if status in ("Ready", "Processed") or stmt.get("Transaction"):
            return stmt
        time.sleep(2)
    return stmt


def _parse_transaction(t: dict) -> dict | None:
    """Входящий Booked-платёж → нормализованные поля. Иначе None."""
    if t.get("creditDebitIndicator") != "Credit":
        return None
    if (t.get("status") or "Booked") not in ("Booked", "Pending"):
        return None
    debtor = t.get("DebtorParty") or {}
    amount = (t.get("Amount") or {}).get("amount")
    d = str(t.get("documentProcessDate") or t.get("valueDateTime") or "")[:10]
    try:
        pay_date = date.fromisoformat(d) if d else date.today()
    except ValueError:
        pay_date = date.today()
    return {
        "external_id": t.get("paymentId") or t.get("transactionId"),
        "amount": amount,
        "pay_date": pay_date,
        "purpose": t.get("description") or "",
        "payer_inn": (debtor.get("inn") or "").strip(),
        "payer_name": (debtor.get("name") or "").strip(),
    }


# ── Разнос оплат ──────────────────────────────────────────────────────────────

def apply_incoming_payment(db: Session, tx: dict) -> str:
    """Разносит один входящий платёж: матч счёта → apply_payment.
    Возвращает 'matched' | 'unmatched' | 'skip' (дубль/не создан)."""
    from app.utils import match_invoice_for_payment, apply_payment
    inv = match_invoice_for_payment(db, tx["payer_inn"], tx["amount"],
                                    tx["purpose"], tx["pay_date"])
    created = apply_payment(
        db, invoice=inv, amount=tx["amount"], pay_date=tx["pay_date"],
        source="tochka", external_id=tx["external_id"], purpose=tx["purpose"],
        payer_inn=tx["payer_inn"], payer_name=tx["payer_name"],
    )
    if not created:
        return "skip"
    return "matched" if inv is not None else "unmatched"


def sync_payments_from_tochka(db: Session) -> dict:
    """Тянет выписку за последний месяц и разносит входящие платежи по счетам ТМС."""
    s = _get_settings(db)
    if not s or not s.tochka_enabled:
        return {"updated": 0, "matched": 0, "unmatched": 0, "errors": ["Сверка с Точкой отключена"]}

    matched = unmatched = 0
    errors: list[str] = []
    try:
        with _client(s) as c:
            account_id = _resolve_account_id(c, s)
            if not account_id:
                return {"updated": 0, "matched": 0, "unmatched": 0,
                        "errors": ["Не удалось определить счёт (несколько счетов — укажите accountId в настройках)"]}
            d_to = date.today()
            d_from = d_to - timedelta(days=STATEMENT_DAYS)
            sid = _init_statement(c, account_id, d_from, d_to)
            if not sid:
                return {"updated": 0, "matched": 0, "unmatched": 0, "errors": ["Не получен statementId"]}
            stmt = _get_statement(c, account_id, sid)
            txs = stmt.get("Transaction") or []

        for raw in txs:
            try:
                tx = _parse_transaction(raw)
                if not tx or not tx.get("amount") or not tx.get("external_id"):
                    continue
                res = apply_incoming_payment(db, tx)
                if res == "matched":
                    matched += 1
                elif res == "unmatched":
                    unmatched += 1
            except Exception as e:
                errors.append(str(e))
        db.commit()
    except httpx.HTTPStatusError as e:
        db.rollback()
        return {"updated": matched, "matched": matched, "unmatched": unmatched,
                "errors": [f"HTTP {e.response.status_code}: {e.response.text[:200]}"]}
    except Exception as e:
        db.rollback()
        logger.error("sync_payments_from_tochka: %s", e)
        return {"updated": matched, "matched": matched, "unmatched": unmatched, "errors": [str(e)]}

    logger.info("Точка: разнесено matched=%d unmatched=%d", matched, unmatched)
    return {"updated": matched, "matched": matched, "unmatched": unmatched, "errors": errors}


# ── Подписка на вебхуки (мгновенные оплаты) ──────────────────────────────────

def _client_id_from_token(token: str) -> str | None:
    """client_id для методов вебхуков = claim `iss` в JWT-ключе Точки."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return data.get("iss")
    except Exception:
        return None


def webhook_url_from_base(base_url: str) -> str:
    """Строит адрес приёмника вебхука из публичного базового URL приложения.
    Точка принимает только HTTPS:443 — схему принудительно приводим к https."""
    base = (base_url or "").rstrip("/")
    if base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    elif not base.startswith("https://"):
        base = "https://" + base
    return base + "/api/tochka/webhook"


def get_webhook(db: Session) -> dict:
    """Текущая подписка на вебхуки (или её отсутствие)."""
    s = _get_settings(db)
    if not s:
        return {"ok": False, "message": "JWT-токен Точки не задан"}
    cid = _client_id_from_token(s.tochka_token)
    if not cid:
        return {"ok": False, "message": "Не удалось определить client_id из токена"}
    try:
        with _client(s) as c:
            r = c.get(f"{WH}/{cid}")
        if r.status_code == 404:
            return {"ok": True, "exists": False, "message": "Вебхук не настроен"}
        r.raise_for_status()
        return {"ok": True, "exists": True, "data": r.json().get("Data", {})}
    except httpx.HTTPError as e:
        return {"ok": False, "message": str(e)}


def ensure_webhook(db: Session, webhook_url: str) -> dict:
    """Создаёт/обновляет подписку на входящие платежи (PUT).

    Точка при подписке шлёт тестовый вебхук на указанный URL и требует, чтобы он
    был доступен по HTTPS:443 из интернета — иначе вернёт ошибку. Возвращает
    {ok, message, url}."""
    s = _get_settings(db)
    if not s or not s.tochka_token:
        return {"ok": False, "message": "JWT-токен Точки не задан"}
    if not webhook_url or not webhook_url.startswith("https://"):
        return {"ok": False, "message": "Нужен публичный HTTPS-адрес вебхука"}
    cid = _client_id_from_token(s.tochka_token)
    if not cid:
        return {"ok": False, "message": "Не удалось определить client_id из токена"}
    body = {"Data": {"webhooksList": WEBHOOK_EVENTS, "url": webhook_url}}
    try:
        with _client(s) as c:
            r = c.put(f"{WH}/{cid}", json=body)
        if r.status_code in (200, 201):
            return {"ok": True, "message": "Вебхук подписан", "url": webhook_url}
        return {"ok": False, "message": f"HTTP {r.status_code}: {r.text[:200]}"}
    except httpx.HTTPError as e:
        return {"ok": False, "message": str(e)}


def ensure_webhook_saved(db: Session) -> dict:
    """Переподписывает вебхук по сохранённому в настройках адресу (для старта/деплоя)."""
    s = _get_settings(db)
    if not s or not s.tochka_enabled or not s.tochka_token or not s.tochka_webhook_url:
        return {"ok": False, "message": "нет условий для подписки"}
    return ensure_webhook(db, s.tochka_webhook_url)

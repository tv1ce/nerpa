"""
Банк «Точка» → TMS: приём входящих платежей.

POST /api/tochka/webhook — вебхук Точки (тело = JWT, RS256). Проверяем подпись
                           публичным ключом Точки, разносим incomingPayment по счетам.
POST /api/tochka/sync    — ручной запуск сверки выписки (admin).
GET  /api/tochka/test    — проверка подключения (admin).

Вебхук и опрос выписки сводятся к общему apply_payment() — идемпотентность и дедуп
«Точка ↔ 1С» гарантируют, что один и тот же платёж не учтётся дважды.
"""
import base64
import json
import logging

import httpx
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session

from app.auth import role_required
from app.database import get_db
from app.services import tochka_client as tc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tochka", tags=["api_tochka"])

TOCHKA_PUBKEY_URL = "https://enter.tochka.com/doc/openapi/static/keys/public"
_PUBKEY = None   # кэш публичного ключа (RSAPublicKey)


# ── Проверка подписи вебхука (RS256, ключ в формате JWK) ─────────────────────

def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _get_public_key():
    """Строит RSA-ключ из JWK Точки (n, e). Кэшируется на процесс."""
    global _PUBKEY
    if _PUBKEY is None:
        from cryptography.hazmat.primitives.asymmetric import rsa
        jwk = httpx.get(TOCHKA_PUBKEY_URL, timeout=10).json()
        n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
        _PUBKEY = rsa.RSAPublicNumbers(e, n).public_key()
    return _PUBKEY


def verify_webhook_jwt(token: str) -> dict | None:
    """Проверяет RS256-подпись JWT публичным ключом Точки. Возвращает payload или None."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    try:
        header_b64, payload_b64, sig_b64 = token.strip().split(".")
        signing_input = f"{header_b64}.{payload_b64}".encode()
        _get_public_key().verify(_b64url_decode(sig_b64), signing_input,
                                 padding.PKCS1v15(), hashes.SHA256())
        return json.loads(_b64url_decode(payload_b64))
    except Exception as e:
        logger.warning("Точка вебхук: подпись невалидна: %s", e)
        return None


# ── Нормализация payload вебхука → входящий платёж ───────────────────────────

_INCOMING = {"incomingPayment", "incomingSbpPayment", "incomingSbpB2BPayment"}


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _webhook_to_tx(payload: dict) -> dict | None:
    """Приводит payload incomingPayment к формату _parse_transaction (защитно —
    имена полей вебхука могут отличаться, берём с несколькими фолбэками)."""
    from datetime import date
    amount = _first(payload, "amount", "sumAmount", "paymentAmount")
    d = str(_first(payload, "date", "paymentDate", "documentProcessDate",
                   "operationDate") or "")[:10]
    try:
        pay_date = date.fromisoformat(d) if d else date.today()
    except ValueError:
        pay_date = date.today()
    return {
        "external_id": _first(payload, "paymentId", "transactionId", "documentId"),
        "amount": float(amount) if amount is not None else None,
        "pay_date": pay_date,
        "purpose": _first(payload, "purpose", "paymentPurpose", "description") or "",
        "payer_inn": (_first(payload, "payerInn", "sidePayerInn", "counterpartyInn",
                             "payerINN") or "").strip(),
        "payer_name": (_first(payload, "payerName", "counterpartyName",
                              "payerNameFull") or "").strip(),
    }


# ── Эндпоинты ─────────────────────────────────────────────────────────────────

@router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    """Приём вебхука Точки. Тело запроса — строка JWT (RS256)."""
    raw = (await request.body()).decode("utf-8", "ignore").strip()
    # Точка шлёт «голый» JWT; но на всякий случай поддержим form/json-обёртку
    if raw.startswith("{"):
        try:
            raw = json.loads(raw).get("token") or raw
        except Exception:
            pass
    payload = verify_webhook_jwt(raw)
    if payload is None:
        return PlainTextResponse("invalid signature", status_code=403)

    event = payload.get("webhookType") or payload.get("event") or ""
    if event and event not in _INCOMING:
        return JSONResponse({"ok": True, "skipped": event})   # 200, чтобы Точка не ретраила

    tx = _webhook_to_tx(payload)
    if not tx or not tx.get("amount") or not tx.get("external_id"):
        logger.warning("Точка вебхук: не распознан платёж, payload keys=%s", list(payload.keys()))
        return JSONResponse({"ok": True, "parsed": False})
    try:
        res = tc.apply_incoming_payment(db, tx)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("Точка вебхук: ошибка разноса: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    logger.info("Точка вебхук: %s платёж %s на %s", res, tx["external_id"], tx["amount"])
    return JSONResponse({"ok": True, "result": res})


@router.post("/sync")
@role_required("admin")
async def sync(request: Request, db: Session = Depends(get_db)):
    """Ручной запуск сверки выписки Точки (админ)."""
    result = tc.sync_payments_from_tochka(db)
    return JSONResponse(result)


@router.get("/test")
@role_required("admin")
async def test(request: Request, db: Session = Depends(get_db)):
    """Проверка подключения к API Точки (админ)."""
    return JSONResponse(tc.test_connection(db))


@router.post("/webhook-subscribe")
@role_required("admin")
async def webhook_subscribe(request: Request, db: Session = Depends(get_db)):
    """Подписка на вебхук по публичному адресу этого сервера (админ)."""
    from app.models import CompanySettings
    url = tc.webhook_url_from_base(str(request.base_url))
    res = tc.ensure_webhook(db, url)
    if res.get("ok"):
        company = db.query(CompanySettings).first()
        if company:
            company.tochka_webhook_url = url
            db.commit()
    return JSONResponse(res)


@router.get("/webhook-status")
@role_required("admin")
async def webhook_status(request: Request, db: Session = Depends(get_db)):
    """Текущее состояние подписки на вебхук (админ)."""
    return JSONResponse(tc.get_webhook(db))

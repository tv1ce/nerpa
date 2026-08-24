"""OAuth 2.0 для Bitrix24: установка приложения, хранение и обновление токенов.

Зачем это поверх уже работающего вебхука. Вебхук — это постоянный ключ портала:
он не истекает, но лежит одной строкой и даёт доступ ко всему, на что выдан.
Приложение с OAuth выдаёт короткий access_token (около часа) и refresh_token для
его обновления, права ограничены заявленными scope, а отзыв делается на портале
удалением приложения. Логины и пароли сотрудников Bitrix24 здесь не участвуют —
ни в каком виде, как и требует схема Bitrix24.

Поддержаны оба пути получения токенов:

  установка приложения — портал сам присылает AUTH_ID/REFRESH_ID на handler-адрес
  (это путь для «локального приложения» с плейсментом в карточке CRM);

  authorization_code — администратор жмёт «Подключить» в настройках, портал
  спрашивает разрешение и возвращает code, который меняется на пару токенов.

Обновление пары ленивое: клиент идёт в REST с текущим access_token и обновляет
его только когда портал ответил expired_token. Обновлять по таймеру смысла нет —
часы сервера и портала расходятся, и «свежий по нашим часам» токен всё равно
может оказаться просроченным.
"""
import logging
from datetime import timedelta

import httpx

from app.database import SessionLocal
from app.models import BitrixOAuthToken, CompanySettings
from app.tz import now as msk_now

logger = logging.getLogger(__name__)

# Общий сервер авторизации Bitrix24 — единый для всех облачных порталов
TOKEN_URL = "https://oauth.bitrix.info/oauth/token/"

# Права, которые запрашивает приложение. crm — карточки и поля, user — кто
# работает, task — постановка задач по итогам разговора (следующий шаг ТЗ).
DEFAULT_SCOPE = "crm,user,task"

# Запас перед истечением: если до конца жизни токена меньше — считаем протухшим
# и обновляем заранее, чтобы не ловить ошибку на середине цепочки вызовов.
EXPIRY_MARGIN = timedelta(seconds=90)


class BitrixOAuthError(Exception):
    pass


# ── Хранилище токенов ────────────────────────────────────────────────────────

def current_token(db) -> BitrixOAuthToken | None:
    """Токены портала. Ожидается один портал, поэтому берём свежайший."""
    return (db.query(BitrixOAuthToken)
            .order_by(BitrixOAuthToken.updated_at.desc().nullslast(),
                      BitrixOAuthToken.id.desc())
            .first())


def save_tokens(db, payload: dict, user_id: int | None = None) -> BitrixOAuthToken:
    """Сохраняет ответ портала (установка, code или refresh) в БД.

    Ключ — member_id портала: повторная установка обновляет ту же строку, а не
    плодит вторую, иначе клиент начал бы случайно брать устаревшую пару.
    """
    member_id = str(payload.get("member_id") or "").strip()
    if not member_id:
        raise BitrixOAuthError("Портал не передал member_id — токены не сохранены")

    token = (db.query(BitrixOAuthToken)
             .filter(BitrixOAuthToken.member_id == member_id).first())
    if token is None:
        token = BitrixOAuthToken(member_id=member_id)
        db.add(token)

    access = payload.get("access_token") or payload.get("AUTH_ID")
    refresh = payload.get("refresh_token") or payload.get("REFRESH_ID")
    if not access or not refresh:
        raise BitrixOAuthError("В ответе портала нет пары токенов")

    token.access_token = access
    token.refresh_token = refresh
    token.domain = (payload.get("domain") or payload.get("DOMAIN") or token.domain or "")[:120]
    endpoint = (payload.get("client_endpoint") or "").strip()
    if not endpoint and token.domain:
        endpoint = f"https://{token.domain}/rest/"
    token.client_endpoint = (endpoint or token.client_endpoint or "")[:255]
    token.scope = (payload.get("scope") or token.scope or "")[:255]

    try:
        expires_in = int(payload.get("expires_in") or payload.get("AUTH_EXPIRES") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    token.expires_at = msk_now() + timedelta(seconds=expires_in)

    if user_id and not token.installed_by_id:
        token.installed_by_id = user_id
    token.updated_at = msk_now()
    db.flush()
    logger.info("Bitrix24 OAuth: токены портала %s сохранены (до %s)",
                token.domain, token.expires_at)
    return token


def disconnect(db) -> int:
    """Забывает токены. На портале приложение удаляется отдельно, вручную."""
    count = db.query(BitrixOAuthToken).delete()
    db.commit()
    logger.info("Bitrix24 OAuth: токены удалены (%d)", count)
    return count


# ── Обмен и обновление ───────────────────────────────────────────────────────

def _post_token(params: dict) -> dict:
    try:
        resp = httpx.post(TOKEN_URL, params=params, timeout=30)
    except httpx.HTTPError as e:
        raise BitrixOAuthError(f"Сервер авторизации Bitrix24 недоступен: {e}")
    try:
        data = resp.json()
    except ValueError:
        raise BitrixOAuthError(f"Неожиданный ответ сервера авторизации: {resp.text[:200]}")
    if resp.status_code >= 400 or data.get("error"):
        raise BitrixOAuthError(
            data.get("error_description") or data.get("error") or f"HTTP {resp.status_code}")
    return data


def authorize_url(domain: str, client_id: str, redirect_uri: str, state: str = "") -> str:
    """Адрес страницы согласия на портале — туда уходит администратор."""
    from urllib.parse import urlencode
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
    }
    if state:
        params["state"] = state
    return f"https://{domain.strip().strip('/')}/oauth/authorize/?{urlencode(params)}"


def exchange_code(company: CompanySettings, code: str) -> dict:
    """authorization_code → пара токенов."""
    if not company or not company.bitrix_client_id or not company.bitrix_client_secret:
        raise BitrixOAuthError("Не заданы client_id и client_secret приложения")
    return _post_token({
        "grant_type": "authorization_code",
        "client_id": company.bitrix_client_id,
        "client_secret": company.bitrix_client_secret,
        "code": code,
    })


def refresh_tokens(db, token: BitrixOAuthToken, company: CompanySettings) -> BitrixOAuthToken:
    """Меняет refresh_token на новую пару и сохраняет её."""
    if not company or not company.bitrix_client_id or not company.bitrix_client_secret:
        raise BitrixOAuthError("Не заданы client_id и client_secret приложения")
    if not token or not token.refresh_token:
        raise BitrixOAuthError("Нет refresh_token — приложение нужно установить заново")

    data = _post_token({
        "grant_type": "refresh_token",
        "client_id": company.bitrix_client_id,
        "client_secret": company.bitrix_client_secret,
        "refresh_token": token.refresh_token,
    })
    data.setdefault("member_id", token.member_id)
    saved = save_tokens(db, data)
    db.commit()
    return saved


# ── Источник токена для REST-клиента ─────────────────────────────────────────

class TokenSource:
    """Отдаёт клиенту действующий access_token и умеет обновлять пару.

    Своя сессия БД намеренно: клиент живёт внутри `with`-блока в самых разных
    местах приложения (заказы, контрагенты, скрипты), и тащить туда чужую
    сессию ради обновления токена значило бы менять сигнатуры половины кода.
    """

    def __init__(self, member_id: str, endpoint: str, access_token: str):
        self.member_id = member_id
        self.endpoint = endpoint
        self.access_token = access_token

    def refresh(self) -> str:
        db = SessionLocal()
        try:
            company = db.query(CompanySettings).first()
            token = (db.query(BitrixOAuthToken)
                     .filter(BitrixOAuthToken.member_id == self.member_id).first())
            fresh = refresh_tokens(db, token, company)
            self.access_token = fresh.access_token
            self.endpoint = fresh.client_endpoint or self.endpoint
            return self.access_token
        finally:
            db.close()


def build_token_source(company: CompanySettings) -> TokenSource | None:
    """Готовый источник токена. Протухший access_token обновляет сразу."""
    db = SessionLocal()
    try:
        token = current_token(db)
        if not token or not token.access_token:
            return None
        if token.expires_at and token.expires_at - EXPIRY_MARGIN <= msk_now():
            try:
                token = refresh_tokens(db, token, company)
            except BitrixOAuthError as e:
                # Не роняем вызывающий код: пусть попробует текущим токеном —
                # часы могли разойтись, и он ещё жив. Если нет, клиент получит
                # expired_token и обновится по месту.
                logger.warning("Bitrix24 OAuth: обновление не удалось (%s)", e)
        return TokenSource(token.member_id, token.client_endpoint or "", token.access_token)
    finally:
        db.close()


def status(db) -> dict:
    """Сводка для страницы настроек."""
    token = current_token(db)
    if not token:
        return {"connected": False}
    return {
        "connected": True,
        "domain": token.domain,
        "scope": token.scope,
        "expires_at": token.expires_at,
        "expired": bool(token.expires_at and token.expires_at <= msk_now()),
        "installed_at": token.installed_at,
        "installed_by": token.installed_by.full_name if token.installed_by else None,
    }

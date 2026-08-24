"""Bitrix24 OAuth 2.0 — установка приложения и подключение портала.

Схема Bitrix24 даёт два пути получить токены, поддержаны оба.

1. Установка приложения. На портале создаётся «локальное приложение» с
   handler-адресом https://<адрес NERPA>/api/bitrix/oauth/install. При установке
   портал сам присылает туда AUTH_ID/REFRESH_ID — отдельный обмен кода не нужен.
   Этим же приложением ставится плейсмент со скриптами в карточку CRM.

2. authorization_code. Администратор жмёт «Подключить» в настройках, портал
   спрашивает разрешение и возвращает code на /api/bitrix/oauth/callback.

Логины и пароли сотрудников Bitrix24 не запрашиваются и не хранятся ни в одном
из путей — только выданные порталом токены (см. app/services/bitrix_oauth.py).

Вебхук при этом никуда не девается: пока администратор не переключил режим,
приложение работает по-старому. Способ авторизации выбирается в одном месте —
get_bitrix_client() в app/services/bitrix_client.py.
"""
import logging
import re
import secrets

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from app.auth import role_required
from app.database import get_db
from app.models import CompanySettings
from app.services import bitrix_oauth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bitrix/oauth", tags=["api_bitrix_oauth"])

_INSTALL_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>NERPA</title>
<script src="//api.bitrix24.com/api/v1/"></script></head>
<body style="font-family:sans-serif;padding:24px">
<h3>%(title)s</h3><p style="color:#64748b">%(text)s</p>
<script>try { BX24.init(function () { BX24.installFinish(); }); } catch (e) {}</script>
</body></html>"""


def _install_page(title: str, text: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(_INSTALL_HTML % {"title": title, "text": text}, status_code=status_code)


def _portal_allowed(company: CompanySettings, domain: str) -> bool:
    """Проверяет, что установку прислал ожидаемый портал.

    Эндпоинт установки открыт без авторизации — иначе портал до него не
    достучится, — поэтому принимать токены от произвольного домена нельзя: так
    чужой портал подменил бы нам рабочие токены. Если домен в настройках не
    задан, берём его из адреса вебхука; если нет и его — принимаем первую
    установку и запоминаем домен как доверенный.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return False
    expected = (company.bitrix_portal_domain or "").strip().lower()
    if not expected and company.bitrix_webhook_url:
        m = re.search(r"https?://([^/]+)/", company.bitrix_webhook_url)
        expected = m.group(1).lower() if m else ""
    return (not expected) or domain == expected


@router.api_route("/install", methods=["GET", "POST"])
async def oauth_install(request: Request, db: Session = Depends(get_db)):
    """Handler-адрес приложения: портал присылает сюда токены при установке."""
    form = {}
    if request.method == "POST":
        try:
            form = dict(await request.form())
        except Exception:
            form = {}
    params = {**dict(request.query_params), **form}

    domain = str(params.get("DOMAIN") or params.get("domain") or "")
    company = db.query(CompanySettings).first()
    if not company:
        return _install_page("Не настроено", "В NERPA не заполнены настройки компании.", 400)
    if not _portal_allowed(company, domain):
        logger.warning("Bitrix24 OAuth: отклонена установка с чужого портала %s", domain)
        return _install_page(
            "Портал не разрешён",
            f"Установка с домена {domain} отклонена. "
            "Укажите домен портала в настройках NERPA.", 403)

    payload = {
        "member_id": params.get("member_id") or params.get("MEMBER_ID"),
        "access_token": params.get("AUTH_ID") or params.get("access_token"),
        "refresh_token": params.get("REFRESH_ID") or params.get("refresh_token"),
        "expires_in": params.get("AUTH_EXPIRES") or params.get("expires_in"),
        "domain": domain,
        "client_endpoint": params.get("client_endpoint") or (f"https://{domain}/rest/" if domain else ""),
        "scope": params.get("scope") or params.get("SCOPE"),
    }
    try:
        bitrix_oauth.save_tokens(db, payload)
        # Домен фиксируем как доверенный и переводим портал на OAuth: раз
        # приложение установили, дальше REST должен ходить через него.
        company.bitrix_portal_domain = domain[:120]
        company.bitrix_auth_mode = "oauth"
        db.commit()
    except bitrix_oauth.BitrixOAuthError as e:
        db.rollback()
        logger.error("Bitrix24 OAuth: установка не удалась — %s", e)
        return _install_page("Установка не завершена", str(e), 400)

    logger.info("Bitrix24 OAuth: приложение установлено на портале %s", domain)
    return _install_page("Приложение установлено",
                         "NERPA подключена к порталу. Можно закрыть это окно.")


@router.get("/connect")
@role_required("admin")
async def oauth_connect(request: Request, db: Session = Depends(get_db)):
    """Начало authorization_code: уводит администратора на страницу согласия."""
    company = db.query(CompanySettings).first()
    if not company or not company.bitrix_client_id:
        return RedirectResponse(url="/settings/?tab=integrations&err=no_client_id#bitrix", status_code=302)
    domain = (company.bitrix_portal_domain or "").strip()
    if not domain:
        return RedirectResponse(url="/settings/?tab=integrations&err=no_domain#bitrix", status_code=302)

    redirect_uri = str(request.url_for("bitrix_oauth_callback"))
    state = secrets.token_urlsafe(16)
    request.session["bitrix_oauth_state"] = state
    return RedirectResponse(
        url=bitrix_oauth.authorize_url(domain, company.bitrix_client_id, redirect_uri, state),
        status_code=302)


@router.get("/callback", name="bitrix_oauth_callback")
@role_required("admin")
async def oauth_callback(request: Request, code: str = "", state: str = "",
                         db: Session = Depends(get_db)):
    """Возврат с портала: меняем code на пару токенов."""
    expected = request.session.pop("bitrix_oauth_state", "")
    # state защищает от подсунутого кода: без сверки кто угодно мог бы прислать
    # администратору ссылку и привязать наш NERPA к своему порталу
    if not expected or not secrets.compare_digest(expected, state or ""):
        return RedirectResponse(url="/settings/?tab=integrations&err=bad_state#bitrix", status_code=302)
    if not code:
        return RedirectResponse(url="/settings/?tab=integrations&err=no_code#bitrix", status_code=302)

    company = db.query(CompanySettings).first()
    try:
        data = bitrix_oauth.exchange_code(company, code)
        bitrix_oauth.save_tokens(db, data, request.session.get("user_id"))
        company.bitrix_auth_mode = "oauth"
        if data.get("domain"):
            company.bitrix_portal_domain = str(data["domain"])[:120]
        db.commit()
    except bitrix_oauth.BitrixOAuthError as e:
        db.rollback()
        logger.error("Bitrix24 OAuth: обмен кода не удался — %s", e)
        return RedirectResponse(url="/settings/?tab=integrations&err=oauth_failed#bitrix", status_code=302)
    return RedirectResponse(url="/settings/?tab=integrations&saved=1#bitrix", status_code=302)


@router.post("/disconnect")
@role_required("admin")
async def oauth_disconnect(request: Request, db: Session = Depends(get_db)):
    """Забывает токены и возвращает портал на вебхук."""
    bitrix_oauth.disconnect(db)
    company = db.query(CompanySettings).first()
    if company:
        company.bitrix_auth_mode = "webhook"
        db.commit()
    return RedirectResponse(url="/settings/?tab=integrations&saved=1#bitrix", status_code=302)


@router.get("/status")
@role_required("admin")
async def oauth_status(request: Request, db: Session = Depends(get_db)):
    """Сводка подключения для страницы настроек."""
    info = bitrix_oauth.status(db)
    for key in ("expires_at", "installed_at"):
        if info.get(key):
            info[key] = info[key].strftime("%d.%m.%Y %H:%M")
    return JSONResponse({"ok": True, **info})

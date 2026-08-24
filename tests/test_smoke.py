"""
Smoke-тесты — проверяем что приложение стартует, ключевые роуты отвечают,
авторизация работает, базовые страницы рендерятся без 500.
"""


# ── Без авторизации ───────────────────────────────────────────────────────────

def test_health(client):
    """Health-check endpoint должен отвечать 200 без авторизации."""
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_login_page_renders(client):
    """Страница входа должна рендериться (200) и содержать форму."""
    r = client.get("/auth/login")
    assert r.status_code == 200
    assert 'name="username"' in r.text
    assert 'name="password"' in r.text


def test_manifest_pwa(client):
    """PWA-манифест отдаётся с правильным Content-Type."""
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert "manifest" in r.headers.get("content-type", "")


def test_service_worker(client):
    """Service Worker отдаётся с нужными заголовками."""
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "Service-Worker-Allowed" in r.headers


def test_app_version_json(client):
    """Эндпоинт версии APK возвращает JSON (даже если APK нет)."""
    r = client.get("/app/version.json")
    assert r.status_code == 200
    data = r.json()
    assert "versionCode" in data


def test_unauthenticated_redirects_to_login(client):
    """Защищённые страницы без сессии редиректят на /auth/login."""
    for path in ("/", "/orders/", "/invoices/", "/counterparties/", "/warehouse/"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 302, f"{path} должен редиректить, вернул {r.status_code}"
        assert "/auth/login" in r.headers.get("location", ""), f"{path}: неверный редирект"


def test_apk_download_requires_auth(client):
    """Скачивание APK без авторизации — редирект на логин."""
    r = client.get("/app/download", follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/login" in r.headers.get("location", "")


def test_wrong_password_returns_login_form(client):
    """Неверный пароль не создаёт сессию — снова показывается форма входа."""
    from tests.conftest import _get_csrf
    csrf = _get_csrf(client, "/auth/login")
    r = client.post("/auth/login", data={
        "username": "admin",
        "password": "wrong_password_xyz",
        "csrf_token": csrf,
    }, follow_redirects=True)
    assert r.status_code == 200
    assert 'name="password"' in r.text   # форма снова показана


# ── С авторизацией ────────────────────────────────────────────────────────────

def test_dashboard_renders(admin_client):
    """Дашборд рендерится после логина."""
    r = admin_client.get("/")
    assert r.status_code == 200
    assert "NERPA" in r.text or "дашборд" in r.text.lower() or "заказ" in r.text.lower()


def test_orders_list(admin_client):
    r = admin_client.get("/orders/")
    assert r.status_code == 200


def test_invoices_list(admin_client):
    r = admin_client.get("/invoices/")
    assert r.status_code == 200


def test_counterparties_list(admin_client):
    r = admin_client.get("/counterparties/")
    assert r.status_code == 200


def test_warehouse_page(admin_client):
    r = admin_client.get("/warehouse/")
    assert r.status_code == 200


def test_settings_page(admin_client):
    r = admin_client.get("/settings/")
    assert r.status_code == 200
    assert 'name="kpi_product_filter"' in r.text   # новое поле присутствует


def test_reports_revenue(admin_client):
    r = admin_client.get("/reports/revenue")
    assert r.status_code == 200


def test_leads_list(admin_client):
    r = admin_client.get("/leads/")
    assert r.status_code == 200


def test_board_page(admin_client):
    """Табло цеха доступно авторизованным."""
    r = admin_client.get("/board/")
    assert r.status_code == 200


def test_csrf_rejects_post_without_token(admin_client):
    """POST без CSRF-токена должен возвращать 403."""
    r = admin_client.post("/settings/company", data={"name": "Тест"})
    assert r.status_code == 403

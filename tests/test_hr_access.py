"""
Права на HR-раздел: кадровые данные (оценки, eNPS, метрика по людям) видят
только администратор и роль hr.

  - менеджеру/просмотру/демо страницы /hr/* отдают 403, а не содержимое
  - в меню у них нет и самого пункта «HR-отчётность»
  - у админа и hr доступ сохраняется
  - публичные ссылки по токену (/hr/w/{token}) продолжают работать без логина —
    ими пользуются руководители, у которых входа в NERPA нет

Роль переключается прямо в БД: login_required перечитывает пользователя из БД
на каждом запросе, поэтому этого достаточно и сессию подделывать не нужно.
"""
import contextlib

import pytest


@contextlib.contextmanager
def _as_role(role: str):
    """Временно переводит пользователя admin в другую роль."""
    from app.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "admin").one()
        was = user.role
        user.role = role
        db.commit()
    finally:
        db.close()
    try:
        yield
    finally:
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == "admin").one()
            user.role = was
            db.commit()
        finally:
            db.close()


HR_PAGES = ["/hr/", "/hr/metrics", "/hr/metrics/month", "/hr/surveys",
            "/hr/questions", "/hr/positions", "/hr/employees/1/profile"]


@pytest.mark.parametrize("role", ["manager", "sales", "viewer", "demo"])
def test_hr_section_closed_for_other_roles(admin_client, role):
    with _as_role(role):
        for path in HR_PAGES:
            r = admin_client.get(path, follow_redirects=False)
            assert r.status_code == 403, f"{path} для роли {role}: {r.status_code}"
            assert "Нет доступа" in r.text


def test_hr_write_routes_closed_too(admin_client):
    """Закрыт весь раздел, а не только страницы: POST тоже не проходит."""
    with _as_role("manager"):
        r = admin_client.post("/hr/employees", data={"full_name": "Чужой"},
                              follow_redirects=False)
        assert r.status_code == 403


@pytest.mark.parametrize("role", ["admin", "hr"])
def test_hr_section_open_for_hr_and_admin(admin_client, role):
    with _as_role(role):
        r = admin_client.get("/hr/", follow_redirects=False)
        assert r.status_code == 200


def test_hr_menu_hidden_from_other_roles(admin_client):
    from app.database import SessionLocal
    from app.models import CompanySettings

    db = SessionLocal()
    try:
        company = db.query(CompanySettings).first()
        was = company.module_hr
        company.module_hr = True
        db.commit()
    finally:
        db.close()

    try:
        with _as_role("manager"):
            assert 'href="/hr/"' not in admin_client.get("/").text
        with _as_role("admin"):
            assert 'href="/hr/"' in admin_client.get("/").text
    finally:
        db = SessionLocal()
        try:
            company = db.query(CompanySettings).first()
            company.module_hr = was
            db.commit()
        finally:
            db.close()


def test_public_week_form_needs_no_login(admin_client):
    """Ссылка руководителя живёт вне прав раздела — иначе метрику некому вносить."""
    from fastapi.testclient import TestClient

    from app.database import SessionLocal
    from app.main import app
    from app.models import HrMetricToken

    db = SessionLocal()
    try:
        tok = HrMetricToken(manager_id=None, token="public-week-form-token")
        db.add(tok)
        db.commit()
    finally:
        db.close()

    anon = TestClient(app)     # свой куки-джар: сессии нет
    r = anon.get("/hr/w/public-week-form-token", follow_redirects=False)
    assert r.status_code == 200
    assert "Метрика" in r.text

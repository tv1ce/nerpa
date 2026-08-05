"""
Единый экран ИИ-анализа (/hr/insights):
  - страница показывает всех активных сотрудников, даже тех, кого ещё не анализировали
  - кнопка собирает анализ по одному сотруднику за запрос (очередь — в браузере)
  - результат ложится в ту же историю, что и анализ из профайла
  - сотрудника без ответов пропускаем с понятной причиной, а не 500-й
  - на экране виден последний анализ, а не первый

ИИ подменяется: тесты проверяют сборку и хранение, а не ответ модели.
"""
import re
from datetime import date

import pytest

from app.database import SessionLocal
from app.models import HrEmployee, HrEmployeeInsight, HrRecord


@pytest.fixture
def fake_ai(monkeypatch):
    """Подменяет вызов OpenRouter — возвращает предсказуемый текст."""
    calls = []

    def fake_chat(system, user, **kw):
        calls.append(user)
        # имя в ответе — БД общая на сессию, и на общем экране висят выводы,
        # собранные соседними тестами: без имени их не отличить от своих
        name = re.search(r"Сотрудник: ([^,]+)", user).group(1)
        return "  Вектор: стабилен. Анализ №%d для %s.  " % (len(calls), name)

    from app.services import openrouter_client
    monkeypatch.setattr(openrouter_client, "chat", fake_chat)
    monkeypatch.setattr(openrouter_client, "MODEL", "test/model")
    return calls


def _employee(name, with_records=True, enps=None):
    db = SessionLocal()
    try:
        emp = HrEmployee(full_name=name)
        db.add(emp)
        db.flush()
        if with_records:
            db.add(HrRecord(employee_id=emp.id, section="complaints",
                            period=date(2026, 7, 1), text_1="Сломался станок"))
        for i, score in enumerate(enps or []):
            db.add(HrRecord(employee_id=emp.id, section="enps", score=score,
                            period=date(2026, 5 + i, 1)))
        db.commit()
        return emp.id
    finally:
        db.close()


def _run(client, employee_id):
    import re
    csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', client.get("/hr/").text).group(1)
    return client.post("/hr/insights/run", json={"employee_id": employee_id},
                       headers={"X-CSRF-Token": csrf})


def test_board_lists_employees_without_analysis(admin_client):
    emp_id = _employee("Ааа Безанализа")
    html = admin_client.get("/hr/insights").text
    assert "Ааа Безанализа" in html
    assert f'data-id="{emp_id}"' in html
    assert "Анализ ещё не собирали" in html


def test_run_saves_insight_and_shows_it(admin_client, fake_ai):
    emp_id = _employee("Ббб Аналитик")

    r = _run(admin_client, emp_id)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["text"] == "Вектор: стабилен. Анализ №1 для Ббб Аналитик."   # пробелы обрезаны
    assert body["made_at"]

    db = SessionLocal()
    try:
        saved = db.query(HrEmployeeInsight).filter_by(employee_id=emp_id).all()
        assert len(saved) == 1
        assert saved[0].model == "test/model"
    finally:
        db.close()

    # тот же анализ виден и на общем экране, и в профайле сотрудника
    mark = "Анализ №1 для Ббб Аналитик."
    assert mark in admin_client.get("/hr/insights").text
    assert mark in admin_client.get(f"/hr/employees/{emp_id}/profile").text


def test_board_shows_latest_analysis(admin_client, fake_ai):
    emp_id = _employee("Ввв Повторный")
    _run(admin_client, emp_id)
    _run(admin_client, emp_id)

    html = admin_client.get("/hr/insights").text
    assert "Анализ №2 для Ввв Повторный." in html
    assert "Анализ №1 для Ввв Повторный." not in html


def test_employee_without_records_is_skipped(admin_client, fake_ai):
    emp_id = _employee("Ггг Пустой", with_records=False)
    body = _run(admin_client, emp_id).json()
    assert body["ok"] is False
    assert body["skipped"] is True
    assert not fake_ai        # до модели дело не дошло


def test_unknown_employee_is_404(admin_client, fake_ai):
    assert _run(admin_client, 987654).status_code == 404


def test_ai_failure_returns_readable_error(admin_client, monkeypatch):
    emp_id = _employee("Ддд Сломанный")

    from app.services import openrouter_client

    def boom(system, user, **kw):
        raise RuntimeError("OpenRouter 403")

    monkeypatch.setattr(openrouter_client, "chat", boom)
    r = _run(admin_client, emp_id)
    assert r.status_code == 502
    assert "OpenRouter" in r.json()["error"]


def test_enps_tail_is_shown(admin_client):
    _employee("Еее Настроение", enps=[9, 6])
    html = admin_client.get("/hr/insights").text
    assert "eNPS 6/10" in html
    assert "9 → 6" in html

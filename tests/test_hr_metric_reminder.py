"""
Тесты пятничных уведомлений по метрике сотрудников
(app/services/hr_metric_reminder.py):
  - охват руководителя = его люди + он сам (как у его ссылки на форму недели)
  - «сдал» — когда заполнены все метрики подразделения за неделю
  - личных ссылок на форму недели в общем чате нет (по ним входят без пароля)
  - сотрудники без руководителя не теряются, а выносятся отдельной группой
  - чат берётся из своей настройки, иначе — из чата HR-отчёта

БД в тестах общая на сессию, поэтому проверяем строки своих руководителей,
а не глобальные итоги.
"""
from datetime import date

from app.services import hr_metric_reminder as hmr

WEEK = date(2020, 1, 6)     # понедельник далёкой недели — чужие значения не мешают


def _row(data: dict, name: str) -> dict | None:
    return next((r for r in data["managers"] if r["name"] == name), None)


def _seed_department(db, prefix: str, with_manager: bool = True):
    """Руководитель с метрикой + двое подчинённых с метрикой каждый."""
    from app.models import HrEmployee, HrMetric

    manager = None
    if with_manager:
        manager = HrEmployee(full_name=f"{prefix} Руководитель")
        db.add(manager)
        db.flush()

    people = []
    for i in (1, 2):
        emp = HrEmployee(full_name=f"{prefix} Сотрудник {i}",
                         manager_id=manager.id if manager else None)
        db.add(emp)
        people.append(emp)
    db.flush()

    metrics = []
    for emp in ([manager] if manager else []) + people:
        m = HrMetric(employee_id=emp.id, title=f"{prefix} метрика", kind="number")
        db.add(m)
        metrics.append(m)
    db.flush()
    return manager, people, metrics


def _fill(db, metric, value=10.0, week=WEEK):
    from app.models import HrMetricValue
    db.add(HrMetricValue(metric_id=metric.id, week_start=week, value=value))
    db.flush()


def _company(db):
    from app.models import CompanySettings
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)
        db.flush()
    return company


# ── Кто сколько сдал ─────────────────────────────────────────────────────────

def test_manager_scope_includes_himself(admin_client):
    """Своя метрика руководителя входит в его же охват — так же, как в форме
    по ссылке, иначе про неё забывают."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        manager, _people, metrics = _seed_department(db, "Скоуп")
        db.commit()

        row = _row(hmr.manager_progress(db, WEEK), "Скоуп Руководитель")
        assert row is not None
        assert row["total"] == 3          # сам + двое подчинённых
        assert row["filled"] == 0
        assert row["done"] is False
        assert row["pending"] == ["Скоуп Руководитель",
                                  "Скоуп Сотрудник 1", "Скоуп Сотрудник 2"]
    finally:
        db.close()


def test_partial_fill_leaves_manager_in_debt(admin_client):
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        _mgr, _people, metrics = _seed_department(db, "Частично")
        _fill(db, metrics[0])
        db.commit()

        row = _row(hmr.manager_progress(db, WEEK), "Частично Руководитель")
        assert (row["filled"], row["total"]) == (1, 3)
        assert row["done"] is False
        assert "Частично Руководитель" not in row["pending"]   # свою метрику сдал
    finally:
        db.close()


def test_full_fill_marks_done(admin_client):
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        _mgr, _people, metrics = _seed_department(db, "Полностью")
        for m in metrics:
            _fill(db, m)
        db.commit()

        row = _row(hmr.manager_progress(db, WEEK), "Полностью Руководитель")
        assert row["done"] is True
        assert row["pending"] == []
    finally:
        db.close()


def test_value_of_another_week_does_not_count(admin_client):
    """Заполненная прошлая неделя не закрывает текущую."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        _mgr, _people, metrics = _seed_department(db, "Прошлая")
        for m in metrics:
            _fill(db, m, week=date(2019, 12, 30))
        db.commit()

        row = _row(hmr.manager_progress(db, WEEK), "Прошлая Руководитель")
        assert row["filled"] == 0
        assert row["done"] is False
    finally:
        db.close()


def test_employee_without_manager_goes_to_orphans(admin_client):
    """Напомнить о таком сотруднике некому — но и пропасть он не должен."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        _seed_department(db, "Ничей", with_manager=False)
        db.commit()

        orphan = hmr.manager_progress(db, WEEK)["orphan"]
        assert orphan is not None
        assert "Ничей Сотрудник 1" in orphan["pending"]
    finally:
        db.close()


# ── Тексты ───────────────────────────────────────────────────────────────────

def test_remind_text_lists_debt_per_manager(admin_client):
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        _seed_department(db, "Остаток")
        _company(db).public_url = "https://tms.example/"
        db.commit()

        text = hmr.build_remind(db, WEEK)
        assert "Остаток Руководитель — осталось 3 из 3" in text
        assert "6–12 января" in text
        # ссылка на доску метрик — раздел HR, в общий чат не уходит
        assert "https://tms.example" not in text
        assert "https://tms.example/hr/metrics" in hmr.build_check(db, WEEK)
    finally:
        db.close()


def test_messages_never_carry_personal_form_links(admin_client):
    """Ссылка /hr/w/{token} пускает в метрику подразделения без входа в TMS —
    в общий чат она уходить не должна ни в одном из сообщений."""
    from app.database import SessionLocal
    from app.models import HrMetricToken

    db = SessionLocal()
    try:
        manager, _people, _metrics = _seed_department(db, "БезСсылок")
        _company(db).public_url = "https://tms.example"
        db.add(HrMetricToken(manager_id=manager.id, token="secret-token-value"))
        db.commit()

        for text in (hmr.build_remind(db, WEEK), hmr.build_check(db, WEEK)):
            assert "/hr/w/" not in text
            assert "secret-token-value" not in text
    finally:
        db.close()


def test_remind_text_without_public_url_has_no_links(admin_client):
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        _seed_department(db, "БезДомена")
        _company(db).public_url = None
        db.commit()

        text = hmr.build_remind(db, WEEK)
        assert "БезДомена Руководитель" in text
        assert "http" not in text
    finally:
        db.close()


def test_check_text_names_debtors_and_their_people(admin_client):
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        _mgr, _people, metrics = _seed_department(db, "Сводка")
        _fill(db, metrics[0])       # руководитель сдал только свою
        db.commit()

        text = hmr.build_check(db, WEEK)
        assert "кто не сдал" in text
        assert "▫️ Сводка Руководитель — 1 из 3" in text
        assert "Сводка Сотрудник 1" in text
    finally:
        db.close()


def test_check_text_lists_ready_managers(admin_client):
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        _mgr, _people, metrics = _seed_department(db, "Молодец")
        for m in metrics:
            _fill(db, m)
        db.commit()

        text = hmr.build_check(db, WEEK)
        assert "Молодец Руководитель" in text.split("✅ Сдали:")[-1]
    finally:
        db.close()


# ── Настройки рассылки ───────────────────────────────────────────────────────

def test_settings_fall_back_to_hr_report_chat(admin_client):
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        company = _company(db)
        was = company.tg_hr_report_chat_ids      # БД общая на сессию — вернём как было
        company.hr_metric_remind_enabled = True
        company.hr_metric_remind_chat_ids = None
        company.hr_metric_check_enabled = False
        company.hr_metric_check_chat_ids = "-100777"
        company.tg_hr_report_chat_ids = "-100555"
        db.commit()

        assert hmr.notification_settings(db, hmr.REMIND) == (True, [-100555])
        assert hmr.notification_settings(db, hmr.CHECK) == (False, [-100777])

        company.tg_hr_report_chat_ids = was
        db.commit()
    finally:
        db.close()


def test_compose_respects_disabled_flag(admin_client):
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        company = _company(db)
        company.hr_metric_remind_enabled = False
        db.commit()
    finally:
        db.close()

    assert hmr.compose(hmr.REMIND, WEEK) == ([], None)
    # ручной вызов командой бота флаг игнорирует
    ids, text = hmr.compose(hmr.REMIND, WEEK, force=True)
    assert text

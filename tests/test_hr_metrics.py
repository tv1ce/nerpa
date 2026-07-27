"""
Тесты недельной метрики сотрудников (/hr/metrics):
  - неделя нормализуется к понедельнику
  - «доля без ошибок» считается системой, а не вводится руками
  - статус относительно цели учитывает направление метрики
  - пустой ввод удаляет значение недели
  - публичная ссылка руководителя собирает метрики его подразделения
"""
import re
from datetime import date, timedelta

from app.routers import hr_metrics as hm


def _csrf(client) -> str:
    """CSRF-токен текущей сессии — из hidden-поля любой страницы с формой."""
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', client.get("/hr/").text)
    return m.group(1) if m else ""


# ── Недели ────────────────────────────────────────────────────────────────────

def test_week_start_is_monday():
    """Любой день недели сводится к её понедельнику."""
    for offset in range(7):
        d = date(2026, 7, 27) + timedelta(days=offset)   # 27.07.2026 — понедельник
        assert hm._week_start(d) == date(2026, 7, 27)


def test_parse_week_falls_back_to_current():
    assert hm._parse_week("2026-07-30") == date(2026, 7, 27)
    assert hm._parse_week("мусор") == hm._week_start(date.today())
    assert hm._parse_week(None) == hm._week_start(date.today())


def test_week_range_is_chronological():
    weeks = hm._week_range(date(2026, 7, 27), 4)
    assert weeks == [date(2026, 7, 6), date(2026, 7, 13),
                     date(2026, 7, 20), date(2026, 7, 27)]


def test_week_label_spans_months():
    assert hm._week_label(date(2026, 7, 6)) == "6–12 июля"
    assert hm._week_label(date(2026, 6, 29)) == "29 июня – 5 июля"


# ── Разбор чисел ─────────────────────────────────────────────────────────────

def test_num_accepts_human_input():
    assert hm._num("1 344") == 1344
    assert hm._num("97,5") == 97.5
    assert hm._num("") is None
    assert hm._num("не помню") is None


# ── Цель одной строкой ───────────────────────────────────────────────────────

def test_target_more_is_better():
    p = hm.parse_target("не менее 97%")
    assert (p["target"], p["direction"], p["kind"], p["unit"]) == (97.0, "up", "percent", "%")


def test_target_less_is_better():
    p = hm.parse_target("не более 2 шт.")
    assert p["target"] == 2.0
    assert p["direction"] == "down"
    assert p["kind"] == "number"
    assert p["unit"] == "шт"


def test_target_bare_number_is_growth_by_default():
    p = hm.parse_target("1500 шт.")
    assert (p["target"], p["direction"], p["kind"]) == (1500.0, "up", "number")


def test_target_recognises_money_and_symbols():
    assert hm.parse_target("от 300 000 ₽")["kind"] == "money"
    assert hm.parse_target("≥ 98%")["direction"] == "up"
    assert hm.parse_target("<= 5")["direction"] == "down"


def test_target_two_numbers_forces_percent():
    """Признак «вводятся два числа» важнее того, что написано в строке цели."""
    p = hm.parse_target("не менее 98 операций", two_numbers=True)
    assert p["kind"] == "ratio"
    assert p["unit"] == "%"
    assert p["target"] == 98.0


def test_target_empty_is_no_target():
    p = hm.parse_target("")
    assert p["target"] is None
    assert p["direction"] == "up"
    assert "не задана" in hm._target_hint(p)


def test_target_hint_is_readable():
    assert hm._target_hint(hm.parse_target("не более 2 шт.")) == "цель 2 шт, чем меньше — тем лучше"
    assert hm._target_hint(hm.parse_target("не менее 97%")) == "цель 97%, чем больше — тем лучше"


# ── Расчёт значения ──────────────────────────────────────────────────────────

def _metric(**kw):
    from app.models import HrMetric
    return HrMetric(employee_id=1, title="test", **kw)


def test_ratio_value_is_computed_not_typed():
    """8 операций, 2 с ошибкой → 75% без ошибок (в исходной таблице писали «-25%»)."""
    m = _metric(kind="ratio")
    assert hm._compute_value(m, None, 8, 2) == 75.0
    assert hm._compute_value(m, None, 9, 0) == 100.0
    assert hm._compute_value(m, None, 0, 0) is None      # делить не на что
    assert hm._compute_value(m, None, None, None) is None


def test_plain_value_passes_through():
    m = _metric(kind="number")
    assert hm._compute_value(m, 1344, None, None) == 1344


# ── Статус относительно цели ─────────────────────────────────────────────────

def test_status_for_growth_metric():
    m = _metric(kind="ratio", direction="up", target=98.0)
    assert m.status_for(100.0) == "ok"
    assert m.status_for(95.0) == "warn"      # в пределах 10% от цели
    assert m.status_for(75.0) == "bad"
    assert m.status_for(None) == "none"


def test_status_for_reduction_metric():
    """У метрики «чем меньше — тем лучше» шкала перевёрнута."""
    m = _metric(kind="number", direction="down", target=2.0)
    assert m.status_for(1.0) == "ok"
    assert m.status_for(2.1) == "warn"
    assert m.status_for(10.0) == "bad"


def test_status_neutral_without_target():
    assert _metric(kind="number").status_for(5.0) == "neutral"


# ── Форматирование ───────────────────────────────────────────────────────────

def test_fmt_by_kind():
    assert hm._fmt(91.666, _metric(kind="ratio")) == "91.7%"
    assert hm._fmt(1344, _metric(kind="number", unit="шт.")) == "1 344 шт."
    assert hm._fmt(None, _metric(kind="number")) == "—"


# ── Сохранение значений ──────────────────────────────────────────────────────

def _seed_metric(db, **kw):
    from app.models import HrEmployee, HrMetric
    emp = HrEmployee(full_name="Тестов Тест Тестович", position="Тестировщик")
    db.add(emp)
    db.flush()
    metric = HrMetric(employee_id=emp.id, title="Тестовая метрика", **kw)
    db.add(metric)
    db.flush()
    return emp, metric


def test_upsert_value_creates_updates_and_deletes(admin_client):
    """Пустой ввод не создаёт запись, а существующую — удаляет."""
    from app.database import SessionLocal
    from app.models import HrMetricValue

    db = SessionLocal()
    try:
        _emp, metric = _seed_metric(db, kind="ratio", direction="up", target=97.0)
        week = date(2026, 7, 20)

        # создание: 20 отгрузок, 1 с ошибкой → 95%
        hm._upsert_value(db, metric, week,
                         {"raw_total": "20", "raw_bad": "1"}, None, "Тест")
        db.commit()
        rec = db.query(HrMetricValue).filter_by(metric_id=metric.id, week_start=week).one()
        assert rec.value == 95.0

        # обновление той же недели — не плодим вторую запись
        hm._upsert_value(db, metric, week,
                         {"raw_total": "10", "raw_bad": "0"}, None, "Тест")
        db.commit()
        assert db.query(HrMetricValue).filter_by(metric_id=metric.id).count() == 1
        assert db.query(HrMetricValue).filter_by(metric_id=metric.id).one().value == 100.0

        # пустой ввод — значение стирается
        hm._upsert_value(db, metric, week,
                         {"raw_total": "", "raw_bad": ""}, None, "Тест")
        db.commit()
        assert db.query(HrMetricValue).filter_by(metric_id=metric.id).count() == 0
    finally:
        db.close()


def test_metric_rows_trend_skips_empty_weeks(admin_client):
    """Пропущенная неделя не должна выглядеть падением до нуля."""
    from app.database import SessionLocal
    from app.models import HrMetricValue

    db = SessionLocal()
    try:
        _emp, metric = _seed_metric(db, kind="number", direction="up")
        for week, value in [(date(2026, 7, 6), 100.0), (date(2026, 7, 20), 150.0)]:
            db.add(HrMetricValue(metric_id=metric.id, week_start=week, value=value))
        db.commit()

        weeks = hm._week_range(date(2026, 7, 27), 4)   # 06.07 … 27.07
        row = hm._metric_rows(db, [metric], weeks)[0]
        assert row["trend"] == "up"
        assert row["delta"] == 50.0
        assert row["growing"] is True
    finally:
        db.close()


def test_metric_rows_growing_respects_direction(admin_client):
    """Для метрики «меньше — лучше» падение считается ростом результата."""
    from app.database import SessionLocal
    from app.models import HrMetricValue

    db = SessionLocal()
    try:
        _emp, metric = _seed_metric(db, kind="number", direction="down", target=2.0)
        for week, value in [(date(2026, 7, 13), 5.0), (date(2026, 7, 20), 3.0)]:
            db.add(HrMetricValue(metric_id=metric.id, week_start=week, value=value))
        db.commit()

        row = hm._metric_rows(db, [metric], hm._week_range(date(2026, 7, 27), 4))[0]
        assert row["trend"] == "down"
        assert row["growing"] is True
    finally:
        db.close()


# ── Страницы ─────────────────────────────────────────────────────────────────

def test_metrics_board_renders(admin_client):
    r = admin_client.get("/hr/metrics")
    assert r.status_code == 200
    assert "Метрика растёт у" in r.text


def test_metrics_export_csv(admin_client):
    r = admin_client.get("/hr/metrics/export.csv?weeks=4")
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
    assert "ФИО;Должность;Метрика" in r.text


def test_team_achievement_saved_and_reaches_report(admin_client):
    """«Достижения как команда» — одна запись на месяц, попадает в отчёт HR."""
    from app.database import SessionLocal
    from app.routers import hr

    period = date.today().replace(day=1)
    text = "Команда движется сама, без ручного управления."

    r = admin_client.post("/hr/team-achievement", data={
        "period": period.strftime("%Y-%m"), "text": text,
        "csrf_token": _csrf(admin_client),
    }, follow_redirects=False)
    assert r.status_code == 302

    # поле возвращается в форму на той же странице периода
    page = admin_client.get(f"/hr/?period={period:%Y-%m}")
    assert text in page.text

    db = SessionLocal()
    try:
        ctx = hr._gather_ai_report_context(db, period)
        assert ctx["team_achievement"] == text
        report = hr._format_hr_report(ctx, {})
        assert "Достижения как команда" in report
        assert text in report
    finally:
        db.close()

    # пустой текст стирает запись за месяц, чтобы она не висела в отчёте
    admin_client.post("/hr/team-achievement", data={
        "period": period.strftime("%Y-%m"), "text": "   ",
        "csrf_token": _csrf(admin_client),
    }, follow_redirects=False)

    db = SessionLocal()
    try:
        ctx = hr._gather_ai_report_context(db, period)
        assert ctx["team_achievement"] == ""
        assert "Достижения как команда" not in hr._format_hr_report(ctx, {})
    finally:
        db.close()


def test_public_week_form_rejects_unknown_token(client):
    r = client.get("/hr/w/несуществующий-токен")
    assert r.status_code == 404
    assert "Ссылка недействительна" in r.text


def test_old_text_metrics_section_is_retired(admin_client):
    """Старый текстовый раздел больше не предлагается к заполнению, но его код
    остаётся в HR_SECTIONS — иначе уже собранные ответы пропадут из профайла."""
    from app.models import HR_SECTIONS, HR_INPUT_SECTIONS

    assert "metrics" in HR_SECTIONS
    assert "metrics" not in HR_INPUT_SECTIONS

    r = admin_client.get("/hr/positions")
    assert r.status_code == 200
    assert 'name="sec_metrics"' not in r.text, "чекбокс старого раздела остался в должностях"


def test_manager_link_is_created_on_demand_and_reused(admin_client):
    """Ссылка не заводится руками: первый запрос создаёт её, второй отдаёт ту же."""
    from app.database import SessionLocal
    from app.models import HrEmployee

    db = SessionLocal()
    try:
        boss = HrEmployee(full_name="Ссылкин Пётр Петрович", position="Мастер")
        db.add(boss)
        db.commit()
        boss_id = boss.id
    finally:
        db.close()

    # ссылка запрашивается из UI через fetch — CSRF приходит заголовком
    headers = {"X-CSRF-Token": _csrf(admin_client)}

    assert admin_client.post(f"/hr/metrics/link/{boss_id}").status_code == 403, \
        "эндпоинт должен требовать CSRF-токен"

    first = admin_client.post(f"/hr/metrics/link/{boss_id}", headers=headers)
    assert first.status_code == 200
    url = first.json()["url"]
    assert "/hr/w/" in url

    again = admin_client.post(f"/hr/metrics/link/{boss_id}", headers=headers)
    assert again.json()["url"] == url, "повторный запрос должен отдавать ту же ссылку"

    refreshed = admin_client.post(f"/hr/metrics/link/{boss_id}?refresh=1", headers=headers)
    assert refreshed.json()["url"] != url, "перевыпуск должен менять токен"

    # старая ссылка после перевыпуска больше не открывается
    assert admin_client.get(url.replace("http://testserver", "")).status_code == 404


def test_week_form_includes_manager_own_metric_first(admin_client):
    """Руководитель вносит и свою метрику: она идёт первой карточкой и помечена."""
    import secrets
    from app.database import SessionLocal
    from app.models import HrEmployee, HrMetric, HrMetricToken

    db = SessionLocal()
    try:
        boss = HrEmployee(full_name="Яковлев Босс Боссович", position="Мастер-Технолог")
        db.add(boss)
        db.flush()
        # подчинённый с фамилией на «А» — без сортировки он оказался бы выше начальника
        worker = HrEmployee(full_name="Абрамов Раб Рабович", position="Кондитер",
                            manager_id=boss.id)
        db.add(worker)
        db.flush()
        db.add(HrMetric(employee_id=boss.id, title="Отгружено орешков", kind="number"))
        db.add(HrMetric(employee_id=worker.id, title="Изделия по ТТК", kind="number"))
        token = secrets.token_urlsafe(16)
        db.add(HrMetricToken(manager_id=boss.id, token=token))
        db.commit()
        boss_id = boss.id
    finally:
        db.close()

    r = admin_client.get(f"/hr/w/{token}")
    assert r.status_code == 200
    assert "Отгружено орешков" in r.text, "своей метрики руководителя нет в форме"
    assert "Абрамов Раб Рабович" in r.text

    # своя карточка — раньше подчинённого, несмотря на алфавит
    assert r.text.index("Яковлев Босс Боссович") < r.text.index("Абрамов Раб Рабович")
    assert "ваша метрика" in r.text

    # и она действительно сохраняется через эту же форму
    from app.database import SessionLocal as SL
    from app.models import HrMetric as M, HrMetricValue as V
    db = SL()
    try:
        own = db.query(M).filter(M.employee_id == boss_id).one()
        metric_id = own.id
    finally:
        db.close()

    week = hm._week_start(date.today()).isoformat()
    saved = admin_client.post(f"/hr/w/{token}", data={
        "week": week, f"m{metric_id}_value": "1680", "author": "Босс",
    }, follow_redirects=False)
    assert saved.status_code == 302

    db = SL()
    try:
        rec = db.query(V).filter(V.metric_id == metric_id).one()
        assert rec.value == 1680
    finally:
        db.close()


def test_link_offered_to_employee_without_subordinates(admin_client):
    """У сотрудника без подчинённых, но со своей метрикой, тоже есть ссылка —
    иначе внести свою цифру ему негде."""
    from app.database import SessionLocal
    from app.models import HrEmployee, HrMetric

    db = SessionLocal()
    try:
        solo = HrEmployee(full_name="Одиночкина Анна Сергеевна", position="HR-менеджер")
        db.add(solo)
        db.flush()
        db.add(HrMetric(employee_id=solo.id, title="Сотрудники с растущей метрикой",
                        kind="number"))
        db.commit()
        solo_id = solo.id
    finally:
        db.close()

    r = admin_client.get("/hr/")
    assert r.status_code == 200
    assert f'class="btn btn-sm btn-outline-secondary metric-link" data-id="{solo_id}"' in r.text

    # ссылка охватывает только её саму
    link = admin_client.post(f"/hr/metrics/link/{solo_id}",
                             headers={"X-CSRF-Token": _csrf(admin_client)}).json()["url"]
    form = admin_client.get(link.replace("http://testserver", ""))
    assert "Одиночкина Анна Сергеевна" in form.text
    assert "ваша метрика" in form.text


def test_public_week_form_shows_department(admin_client):
    """Ссылка руководителя открывает метрики его подчинённых без входа в TMS."""
    import secrets
    from app.database import SessionLocal
    from app.models import HrEmployee, HrMetric, HrMetricToken

    db = SessionLocal()
    try:
        boss = HrEmployee(full_name="Начальников Босс Боссович", position="Мастер")
        db.add(boss)
        db.flush()
        worker = HrEmployee(full_name="Подчинённый Раб Отникович",
                            position="Кондитер", manager_id=boss.id)
        db.add(worker)
        db.flush()
        db.add(HrMetric(employee_id=worker.id, title="Изделия по ТТК",
                        kind="number", unit="шт."))
        token = secrets.token_urlsafe(16)
        db.add(HrMetricToken(manager_id=boss.id, token=token, label="Цех"))
        db.commit()
    finally:
        db.close()

    r = admin_client.get(f"/hr/w/{token}")
    assert r.status_code == 200
    assert "Подчинённый Раб Отникович" in r.text
    assert "Изделия по ТТК" in r.text

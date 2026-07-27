"""
Тесты недельной метрики сотрудников (/hr/metrics):
  - неделя нормализуется к понедельнику
  - «доля без ошибок» считается системой, а не вводится руками
  - статус относительно цели учитывает направление метрики
  - пустой ввод удаляет значение недели
  - публичная ссылка руководителя собирает метрики его подразделения
"""
from datetime import date, timedelta

from app.routers import hr_metrics as hm


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


def test_public_week_form_rejects_unknown_token(client):
    r = client.get("/hr/w/несуществующий-токен")
    assert r.status_code == 404
    assert "Ссылка недействительна" in r.text


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

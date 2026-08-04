"""
Тесты вопросов опроса (/hr/questions):
  - базовые вопросы заводятся сидером и правятся, а не хардкодятся
  - изменённая формулировка видна и в форме HR, и в публичной ссылке опроса
  - добавленный HR вопрос собирает ответ и попадает в профайл сотрудника
  - свои вопросы должности заменяют общие, и их можно вернуть обратно
  - убранный базовый вопрос прячется (не удаляется) и возвращается кнопкой
  - старые вопросы личностного профиля из карточки должности переехали в справочник
"""
import json
import re
from datetime import date

from app.database import SessionLocal
from app.models import HrEmployee, HrPosition, HrQuestion, HrRecord, HrSurveyToken


def _csrf(client) -> str:
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', client.get("/hr/").text)
    return m.group(1) if m else ""


def _rows(section: str, position_id=None) -> list[HrQuestion]:
    db = SessionLocal()
    try:
        rows = db.query(HrQuestion).filter(
            HrQuestion.section == section,
            HrQuestion.position_id == position_id,
        ).order_by(HrQuestion.sort_order, HrQuestion.id).all()
        for r in rows:
            db.expunge(r)
        return rows
    finally:
        db.close()


def _save_section(client, section: str, questions: list[tuple], position_id=None):
    """Отправляет раздел целиком, как это делает форма: [(qid, текст, тип), ...]."""
    data = {
        "csrf_token": _csrf(client),
        "position_id": str(position_id or ""),
        "qid": [str(qid or "") for qid, _t, _a in questions],
        "text": [text for _q, text, _a in questions],
        "answer_type": [answer_type for _q, _t, answer_type in questions],
    }
    return client.post(f"/hr/questions/{section}", data=data, follow_redirects=False)


def _make_employee(name: str, position_id=None) -> int:
    db = SessionLocal()
    try:
        emp = HrEmployee(full_name=name, position_id=position_id)
        db.add(emp)
        db.commit()
        return emp.id
    finally:
        db.close()


def _make_position(title: str) -> int:
    db = SessionLocal()
    try:
        pos = HrPosition(title=title, is_active=True)
        db.add(pos)
        db.commit()
        return pos.id
    finally:
        db.close()


def _survey_token(admin_client, employee_id: int, sections: list[str]) -> str:
    data = {"period": "2026-08", "employee_ids": str(employee_id), "csrf_token": _csrf(admin_client)}
    for code in sections:
        data[f"sec_{code}"] = "1"
    r = admin_client.post("/hr/surveys", data=data, follow_redirects=False)
    assert r.status_code == 302, r.text
    survey_id = int(r.headers["location"].rsplit("/", 1)[1])
    db = SessionLocal()
    try:
        return db.query(HrSurveyToken).filter(
            HrSurveyToken.survey_id == survey_id,
            HrSurveyToken.employee_id == employee_id).first().token
    finally:
        db.close()


# ── Общие вопросы ─────────────────────────────────────────────────────────────

def test_builtin_questions_are_seeded(admin_client):
    """Формулировки живут в БД, а не в коде: сидер завёл базовый набор."""
    r = admin_client.get("/hr/questions")
    assert r.status_code == 200
    assert "С какой дичью вам приходится сталкиваться каждый день?" in r.text

    enps = _rows("enps")
    assert [q.slot for q in enps] == ["score", "text_1"]
    assert all(q.is_builtin and q.position_id is None for q in enps)


def test_edited_wording_reaches_hr_form_and_public_survey(admin_client):
    """HR переформулировал вопрос — сотрудник видит новую формулировку."""
    emp_id = _make_employee("Вопросов Иван Иванович")
    q = _rows("complaints")[0]
    new_text = "Что мешало вам работать в этом месяце?"

    assert _save_section(admin_client, "complaints", [(q.id, new_text, "text")]).status_code == 302

    assert new_text in admin_client.get(f"/hr/entry/{emp_id}?period=2026-08").text
    token = _survey_token(admin_client, emp_id, ["complaints"])
    assert new_text in admin_client.get(f"/hr/s/{token}").text


def test_added_question_collects_answer_and_shows_in_profile(admin_client):
    """Свой вопрос HR: попадает в опрос, ответ сохраняется и виден в профайле."""
    emp_id = _make_employee("Дополнов Пётр Петрович")
    text = "Чего вам не хватает для работы?"
    existing = _rows("achievements")

    assert _save_section(
        admin_client, "achievements",
        [(q.id, q.text, "text") for q in existing] + [(None, text, "text")],
    ).status_code == 302

    added = [q for q in _rows("achievements") if q.text == text]
    assert len(added) == 1 and not added[0].is_builtin and added[0].slot == "extra"

    token = _survey_token(admin_client, emp_id, ["achievements"])
    assert text in admin_client.get(f"/hr/s/{token}").text

    assert admin_client.post(f"/hr/s/{token}", data={
        added[0].field_name: "Второго монитора", "csrf_token": _csrf(admin_client),
    }, follow_redirects=False).status_code == 302

    db = SessionLocal()
    try:
        rec = db.query(HrRecord).filter(
            HrRecord.employee_id == emp_id, HrRecord.section == "achievements").first()
        assert added[0].key in rec.text_2 and "Второго монитора" in rec.text_2
    finally:
        db.close()

    profile = admin_client.get(f"/hr/employees/{emp_id}/profile")
    assert text in profile.text and "Второго монитора" in profile.text


def test_removed_builtin_is_hidden_and_restorable(admin_client):
    """Базовый вопрос убирается из формы, но не удаляется — его слот держит на
    себе сводки eNPS, поэтому его можно вернуть кнопкой."""
    score_q, comment_q = _rows("enps")
    assert _save_section(admin_client, "enps",
                         [(comment_q.id, comment_q.text, "text")]).status_code == 302

    db = SessionLocal()
    try:
        assert db.query(HrQuestion).filter(HrQuestion.id == score_q.id).first().is_active is False
    finally:
        db.close()

    page = admin_client.get("/hr/questions")
    assert "вернуть стандартные вопросы" in page.text

    assert admin_client.post("/hr/questions/enps/reset",
                             data={"csrf_token": _csrf(admin_client)},
                             follow_redirects=False).status_code == 302
    assert [q.slot for q in _rows("enps")] == ["score", "text_1"]


# ── Вопросы должности ─────────────────────────────────────────────────────────

def test_position_questions_replace_common_ones(admin_client):
    """У должности свой набор — он заменяет общие вопросы раздела, а остальные
    сотрудники продолжают отвечать на общие."""
    pos_id = _make_position("Кондитер-тестовый")
    cook_id = _make_employee("Кондитеров Кондрат", position_id=pos_id)
    other_id = _make_employee("Общий Олег")

    # копируем общие вопросы раздела должности и переписываем их своими
    assert admin_client.post("/hr/questions/complaints/customize", data={
        "position_id": str(pos_id), "csrf_token": _csrf(admin_client),
    }, follow_redirects=False).status_code == 302
    own = _rows("complaints", pos_id)
    assert len(own) == 1, "должны скопироваться общие вопросы, а не пустой список"

    own_text = "Что в цеху мешает больше всего?"
    assert _save_section(admin_client, "complaints", [(own[0].id, own_text, "text")],
                         position_id=pos_id).status_code == 302

    common_text = _rows("complaints")[0].text
    cook_form = admin_client.get(f"/hr/entry/{cook_id}?period=2026-08").text
    assert own_text in cook_form and common_text not in cook_form

    other_form = admin_client.get(f"/hr/entry/{other_id}?period=2026-08").text
    assert common_text in other_form and own_text not in other_form


def test_position_answers_are_saved_and_reset_returns_common(admin_client):
    """Ответ на должностной вопрос сохраняется; «вернуть общие» убирает набор."""
    pos_id = _make_position("Кладовщик-тестовый")
    emp_id = _make_employee("Кладовщиков Клим", position_id=pos_id)

    admin_client.post("/hr/questions/personal/customize", data={
        "position_id": str(pos_id), "csrf_token": _csrf(admin_client)}, follow_redirects=False)
    own_text = "Что бы вы поменяли на складе?"
    _save_section(admin_client, "personal", [(None, own_text, "text")], position_id=pos_id)

    token = _survey_token(admin_client, emp_id, ["personal"])
    page = admin_client.get(f"/hr/s/{token}").text
    assert own_text in page
    idx = len(re.findall(r'name="personal_q\d+"', page)) - 1
    admin_client.post(f"/hr/s/{token}", data={
        f"personal_q{idx}": "Стеллажи по зонам", "csrf_token": _csrf(admin_client),
    }, follow_redirects=False)

    profile = admin_client.get(f"/hr/employees/{emp_id}/profile")
    assert own_text in profile.text and "Стеллажи по зонам" in profile.text

    assert admin_client.post("/hr/questions/personal/reset", data={
        "position_id": str(pos_id), "csrf_token": _csrf(admin_client),
    }, follow_redirects=False).status_code == 302
    assert _rows("personal", pos_id) == []
    # ответ остаётся в истории, даже когда должностного вопроса уже нет
    assert "Стеллажи по зонам" in admin_client.get(f"/hr/employees/{emp_id}/profile").text


def test_legacy_position_questions_are_migrated(admin_client):
    """Вопросы личностного профиля из карточки должности переезжают в справочник
    вопросов и продолжают работать как «свои вопросы должности»."""
    from app.database import _migrate_db

    db = SessionLocal()
    try:
        pos = HrPosition(title="Легаси-должность", is_active=True,
                         personal_questions="Первый вопрос роли\nВторой вопрос роли")
        db.add(pos)
        db.commit()
        pos_id = pos.id
    finally:
        db.close()

    _migrate_db()

    migrated = _rows("personal", pos_id)
    assert [q.text for q in migrated] == ["Первый вопрос роли", "Второй вопрос роли"]
    assert all(q.slot == "personal" and not q.is_builtin for q in migrated)

    db = SessionLocal()
    try:
        # перенос, а не копия: повторный запуск не воскресит удалённые вопросы
        assert db.query(HrPosition).filter(HrPosition.id == pos_id).first().personal_questions is None
    finally:
        db.close()

    emp_id = _make_employee("Легасин Лев", position_id=pos_id)
    form = admin_client.get(f"/hr/entry/{emp_id}?period=2026-08").text
    assert "Первый вопрос роли" in form and "Второй вопрос роли" in form


def test_legacy_gravity_answers_stay_readable(admin_client):
    """Ответы антигравитации, записанные до справочника (ключ «comment»),
    по-прежнему подписываются вопросом в профайле."""
    emp_id = _make_employee("Легаси Олег Олегович")
    db = SessionLocal()
    try:
        db.add(HrRecord(
            employee_id=emp_id, section="gravity", period=date(2026, 7, 1),
            period_kind="month", text_1="Команда",
            text_2=json.dumps([{"key": "ot_1", "comment": "Полгода назад"}], ensure_ascii=False),
        ))
        db.commit()
    finally:
        db.close()

    profile = admin_client.get(f"/hr/employees/{emp_id}/profile")
    assert "Полгода назад" in profile.text
    assert "Антигравитация «ОТ»" in profile.text

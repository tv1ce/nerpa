"""
Тесты справочника вопросов опросника (/hr/questions):
  - базовые вопросы заводятся сидером и правятся, а не хардкодятся
  - изменённая формулировка видна и в форме HR, и в публичной ссылке опроса
  - добавленный HR вопрос собирает ответ и попадает в профайл сотрудника
  - выключенный вопрос исчезает из формы, но ответ на него остаётся в истории
  - базовый вопрос нельзя удалить (на его слот опираются сводки), только выключить
"""
import json
import re
from datetime import date

from app.database import SessionLocal
from app.models import HrEmployee, HrQuestion, HrRecord, HrSurvey, HrSurveyToken


def _csrf(client) -> str:
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', client.get("/hr/").text)
    return m.group(1) if m else ""


def _question(section: str, key: str) -> HrQuestion:
    db = SessionLocal()
    try:
        q = db.query(HrQuestion).filter(
            HrQuestion.section == section, HrQuestion.key == key).first()
        db.expunge(q)
        return q
    finally:
        db.close()


def _make_employee(name: str) -> int:
    db = SessionLocal()
    try:
        emp = HrEmployee(full_name=name)
        db.add(emp)
        db.commit()
        return emp.id
    finally:
        db.close()


def _survey_token(admin_client, employee_id: int, sections: list[str]) -> str:
    """Создаёт раунд опроса через UI и возвращает персональную ссылку сотрудника."""
    data = {"period": "2026-08", "employee_ids": str(employee_id), "csrf_token": _csrf(admin_client)}
    for code in sections:
        data[f"sec_{code}"] = "1"
    r = admin_client.post("/hr/surveys", data=data, follow_redirects=False)
    assert r.status_code == 302, r.text
    survey_id = int(r.headers["location"].rsplit("/", 1)[1])

    db = SessionLocal()
    try:
        tok = db.query(HrSurveyToken).filter(
            HrSurveyToken.survey_id == survey_id,
            HrSurveyToken.employee_id == employee_id).first()
        return tok.token
    finally:
        db.close()


# ── Справочник ────────────────────────────────────────────────────────────────

def test_builtin_questions_are_seeded(admin_client):
    """Формулировки живут в БД, а не в коде: сидер завёл базовый набор."""
    r = admin_client.get("/hr/questions")
    assert r.status_code == 200
    assert "С какой дичью вам приходится сталкиваться каждый день?" in r.text

    enps = _question("enps", "score")
    assert enps.is_builtin and enps.slot == "score"


def test_builtin_question_cannot_be_deleted_but_can_be_hidden(admin_client):
    """Удаление базового вопроса развалило бы сводку eNPS — разрешено только выключение."""
    q = _question("enps", "score")
    r = admin_client.post(f"/hr/questions/{q.id}/delete",
                          data={"csrf_token": _csrf(admin_client)}, follow_redirects=False)
    assert r.status_code == 302
    assert "error=builtin" in r.headers["location"]

    db = SessionLocal()
    try:
        assert db.query(HrQuestion).filter(HrQuestion.id == q.id).first() is not None
    finally:
        db.close()


def test_edited_wording_reaches_hr_form_and_public_survey(admin_client):
    """HR переформулировал вопрос — сотрудник видит новую формулировку."""
    emp_id = _make_employee("Вопросов Иван Иванович")
    q = _question("complaints", "main")
    new_text = "Что мешало вам работать в этом месяце?"

    r = admin_client.post(f"/hr/questions/{q.id}/edit", data={
        "text": new_text, "is_active": "1", "csrf_token": _csrf(admin_client),
    }, follow_redirects=False)
    assert r.status_code == 302

    form = admin_client.get(f"/hr/entry/{emp_id}?period=2026-08")
    assert new_text in form.text

    token = _survey_token(admin_client, emp_id, ["complaints"])
    public = admin_client.get(f"/hr/s/{token}")
    assert public.status_code == 200
    assert new_text in public.text


def test_custom_question_collects_answer_and_shows_in_profile(admin_client):
    """Свой вопрос HR: попадает в опрос, ответ сохраняется и виден в профайле."""
    emp_id = _make_employee("Дополнов Пётр Петрович")
    text = "Чего вам не хватает для работы?"
    r = admin_client.post("/hr/questions", data={
        "section": "complaints", "text": text, "answer_type": "text",
        "hint": "Коротко", "csrf_token": _csrf(admin_client),
    }, follow_redirects=False)
    assert r.status_code == 302

    db = SessionLocal()
    try:
        q = db.query(HrQuestion).filter(HrQuestion.text == text).first()
        assert q is not None and not q.is_builtin and q.slot == "extra"
        field, key, qid = q.field_name, q.key, q.id
    finally:
        db.close()

    token = _survey_token(admin_client, emp_id, ["complaints"])
    assert text in admin_client.get(f"/hr/s/{token}").text

    saved = admin_client.post(f"/hr/s/{token}", data={
        field: "Второго монитора", "csrf_token": _csrf(admin_client),
    }, follow_redirects=False)
    assert saved.status_code == 302

    db = SessionLocal()
    try:
        rec = db.query(HrRecord).filter(
            HrRecord.employee_id == emp_id, HrRecord.section == "complaints").first()
        assert rec is not None
        assert key in rec.text_2 and "Второго монитора" in rec.text_2
    finally:
        db.close()

    profile = admin_client.get(f"/hr/employees/{emp_id}/profile")
    assert text in profile.text
    assert "Второго монитора" in profile.text

    # выключенный вопрос уходит из формы, но ответ на него остаётся подписан в истории
    admin_client.post(f"/hr/questions/{qid}/edit", data={
        "text": text, "csrf_token": _csrf(admin_client),   # без is_active
    }, follow_redirects=False)
    token2 = _survey_token(admin_client, emp_id, ["complaints"])
    assert text not in admin_client.get(f"/hr/s/{token2}").text
    profile = admin_client.get(f"/hr/employees/{emp_id}/profile")
    assert text in profile.text and "Второго монитора" in profile.text


def test_custom_personal_question_is_asked_and_saved(admin_client):
    """Личностный профиль хранит пары «вопрос-ответ» — новый общий вопрос
    подхватывается сотрудниками без своей должности."""
    emp_id = _make_employee("Личностный Сергей Сергеевич")
    text = "Что бы вы поменяли в своей роли?"
    admin_client.post("/hr/questions", data={
        "section": "personal", "text": text, "csrf_token": _csrf(admin_client),
    }, follow_redirects=False)

    db = SessionLocal()
    try:
        q = db.query(HrQuestion).filter(HrQuestion.text == text).first()
        assert q.slot == "personal"   # ответы хранятся парами, а не в JSON доп. вопросов
    finally:
        db.close()

    token = _survey_token(admin_client, emp_id, ["personal"])
    page = admin_client.get(f"/hr/s/{token}")
    assert text in page.text
    # вопрос добавлен последним → у него последний индекс среди полей раздела
    idx = len(re.findall(r'name="personal_q\d+"', page.text)) - 1

    admin_client.post(f"/hr/s/{token}", data={
        f"personal_q{idx}": "Больше влияния на процесс",
        "csrf_token": _csrf(admin_client),
    }, follow_redirects=False)

    profile = admin_client.get(f"/hr/employees/{emp_id}/profile")
    assert text in profile.text
    assert "Больше влияния на процесс" in profile.text


def test_custom_question_can_be_deleted(admin_client):
    """Свой вопрос удаляется целиком — базовые остаются на месте."""
    text = "Временный вопрос"
    admin_client.post("/hr/questions", data={
        "section": "achievements", "text": text, "csrf_token": _csrf(admin_client),
    }, follow_redirects=False)

    db = SessionLocal()
    try:
        qid = db.query(HrQuestion).filter(HrQuestion.text == text).first().id
    finally:
        db.close()

    r = admin_client.post(f"/hr/questions/{qid}/delete",
                          data={"csrf_token": _csrf(admin_client)}, follow_redirects=False)
    assert r.status_code == 302 and "error" not in r.headers["location"]

    db = SessionLocal()
    try:
        assert db.query(HrQuestion).filter(HrQuestion.id == qid).first() is None
    finally:
        db.close()


def test_move_reorders_questions_inside_section(admin_client):
    """Порядок вопросов раздела задаётся стрелками и виден в форме опроса."""
    first, second = _question("enps", "score"), _question("enps", "comment")
    r = admin_client.post(f"/hr/questions/{second.id}/move",
                          data={"dir": "up", "csrf_token": _csrf(admin_client)},
                          follow_redirects=False)
    assert r.status_code == 302

    db = SessionLocal()
    try:
        a = db.query(HrQuestion).filter(HrQuestion.id == first.id).first()
        b = db.query(HrQuestion).filter(HrQuestion.id == second.id).first()
        assert b.sort_order < a.sort_order
    finally:
        db.close()

    # возвращаем исходный порядок, чтобы не влиять на другие тесты
    admin_client.post(f"/hr/questions/{second.id}/move",
                      data={"dir": "down", "csrf_token": _csrf(admin_client)},
                      follow_redirects=False)


def test_survey_with_gravity_keeps_legacy_answers_readable(admin_client):
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

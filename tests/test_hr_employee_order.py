"""
Порядок сотрудников в HR-учёте (перетаскивание строк):
  - пока никого не двигали, список идёт по алфавиту, как раньше
  - после сохранения порядок именно тот, в каком строки бросили
  - уволенные остаются в конце списка, куда бы их ни перетащили
  - чужие/битые id в запросе не ломают сохранение
"""
import re

from app.database import SessionLocal
from app.models import HrEmployee


def _csrf(client) -> str:
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', client.get("/hr/").text)
    return m.group(1) if m else ""


def _seed(names, active=True):
    """Уволенного помечаем месяцем увольнения — иначе он не виден ни в одном
    периоде (см. HrEmployee.visible_in_period) и в списке его просто нет."""
    from datetime import date

    db = SessionLocal()
    try:
        ids = []
        for name in names:
            emp = HrEmployee(full_name=name, is_active=active,
                             deactivated_at=None if active else date.today().replace(day=1))
            db.add(emp)
            db.flush()
            ids.append(emp.id)
        db.commit()
        return ids
    finally:
        db.close()


def _order_of(client, ids):
    """Позиции наших сотрудников на странице списка — в порядке отрисовки."""
    html = client.get("/hr/").text
    seen = [(html.index(f'data-id="{i}"'), i) for i in ids if f'data-id="{i}"' in html]
    return [i for _pos, i in sorted(seen)]


def _post_order(client, ids):
    return client.post("/hr/employees/order", json={"ids": ids},
                       headers={"X-CSRF-Token": _csrf(client)})


def test_default_order_is_alphabetical(admin_client):
    ids = _seed(["Яшин Пётр", "Абрамов Иван", "Мишин Сергей"])
    by_name = dict(zip(["Яшин", "Абрамов", "Мишин"], ids))
    assert _order_of(admin_client, ids) == [by_name["Абрамов"], by_name["Мишин"], by_name["Яшин"]]


def test_drag_order_is_saved_and_applied(admin_client):
    ids = _seed(["Ррр Первый", "Ррр Второй", "Ррр Третий"])
    wanted = [ids[2], ids[0], ids[1]]

    r = _post_order(admin_client, wanted)
    assert r.status_code == 200 and r.json()["ok"] is True

    db = SessionLocal()
    try:
        got = {e.id: e.sort_order for e in db.query(HrEmployee).filter(HrEmployee.id.in_(wanted))}
    finally:
        db.close()
    assert [got[i] for i in wanted] == [1, 2, 3]
    assert _order_of(admin_client, wanted) == wanted


def test_inactive_stay_at_the_bottom(admin_client):
    """Уволенного можно утащить наверх — в списке он всё равно останется внизу."""
    active = _seed(["Ттт Активный"])[0]
    fired = _seed(["Ттт Уволенный"], active=False)[0]

    _post_order(admin_client, [fired, active])
    assert _order_of(admin_client, [active, fired]) == [active, fired]


def test_unknown_ids_are_ignored(admin_client):
    ids = _seed(["Ннн Один", "Ннн Два"])
    r = _post_order(admin_client, [ids[1], 999999, "мусор", ids[0]])
    assert r.status_code == 200
    assert r.json()["count"] == 2
    assert _order_of(admin_client, ids) == [ids[1], ids[0]]


def test_empty_list_is_rejected(admin_client):
    assert _post_order(admin_client, []).status_code == 400

"""
Первичное наполнение раздела «Метрика сотрудников» по таблице, которую HR вёл
в Excel («Метрика сотрудников.xlsx»): создаёт сотрудников (если их ещё нет),
их метрики и значения за уже собранные недели.

Запуск (из каталога tms):
    python scripts/seed_hr_metrics.py              # показать, что будет сделано
    python scripts/seed_hr_metrics.py --apply      # записать в БД

Сотрудники ищутся по ФИО без учёта регистра и лишних пробелов — повторный запуск
ничего не дублирует: существующие сотрудники и метрики переиспользуются, значения
недель обновляются по месту.
"""
import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, init_db          # noqa: E402
from app.models import HrEmployee, HrMetric, HrMetricValue  # noqa: E402


# Недели, по которым в таблице были колонки «Июль 1…5». В таблице неделя
# обозначалась только номером — здесь фиксируем реальные понедельники июля 2026.
JULY_WEEKS = [date(2026, 6, 29), date(2026, 7, 6), date(2026, 7, 13),
              date(2026, 7, 20), date(2026, 7, 27)]

# (ФИО, должность, метрики). У метрики: title, formula, kind, unit, direction,
# target, label_total, label_bad, values — по индексу недели из JULY_WEEKS.
#   number  → values: число
#   ratio   → values: (всего, с ошибкой)
SEED = [
    {
        "name": "Секацкая Маргарита Дмитриевна",
        "position": "HR-менеджер",
        "metrics": [{
            "title": "Сотрудники с растущей метрикой",
            "formula": "Количество сотрудников, чья метрика за последнюю неделю растёт.\n"
                       "HR не знает свою метрику, пока не знает метрику всех сотрудников.",
            "kind": "number", "unit": "чел.", "direction": "up", "target": None,
            "values": {1: 3, 2: 3},
        }],
    },
    {
        "name": "Шевцов Роман Владимирович",
        "position": "Руководитель отдела продаж",
        "metrics": [{
            "title": "Прирост валовой прибыли",
            "formula": "Системный рост валовой прибыли с рентабельностью 75–80% от месяца к месяцу.\n"
                       "Прирост ВП (₽) = ВП текущего месяца – ВП прошлого месяца",
            "kind": "money", "unit": "₽", "direction": "up", "target": None,
            "values": {},
        }],
    },
    {
        "name": "Трухин Николай Владиславович",
        "position": "Мастер-Технолог",
        "metrics": [{
            "title": "Произведено и отгружено орешков",
            "formula": "Итоговое количество произведённых и отгруженных орешков.",
            "kind": "number", "unit": "шт.", "direction": "up", "target": None,
            "values": {0: 336, 1: 1344, 2: 1512},
        }],
    },
    {
        "name": "Корсаков Василий Никитич",
        "position": "Офис-менеджер/Логист",
        "metrics": [{
            "title": "Заказы без ошибок",
            "formula": "Доля заказов, обработанных и подготовленных к отгрузке без ошибок "
                       "(не менее 97%).\n\nОшибкой считается:\n"
                       "— несоответствие количества продукции в заказе, документах или отгрузке\n"
                       "— несоответствие номенклатуры (не тот товар/SKU/вкус)\n"
                       "— некорректные реквизиты в документах (ИНН, наименование, контрагент и т.д.)\n"
                       "— некорректный адрес доставки или контактные данные\n"
                       "— ошибки в отгрузочных документах (счёт, договор, накладные и др.)\n"
                       "— несоответствие фактической отгрузки согласованному заказу",
            "kind": "ratio", "unit": "%", "direction": "up", "target": 97.0,
            "label_total": "отгрузок", "label_bad": "с ошибкой",
            "values": {0: (20, 1), 1: (9, 0), 2: (9, 0)},
        }],
    },
    {
        "name": "Петров Андрей Дмитриевич",
        "position": "Кладовщик-комплектовщик",
        "metrics": [{
            "title": "Складские операции без расхождений",
            "formula": "Доля складских операций без ошибок и расхождений — не менее 98%.\n\n"
                       "Что считается операцией: приёмка, перемещение, комплектация, "
                       "отгрузка, инвентаризация.",
            "kind": "ratio", "unit": "%", "direction": "up", "target": 98.0,
            "label_total": "операций", "label_bad": "с ошибкой",
            "values": {0: (8, 2), 1: (36, 8), 2: (27, 5)},
        }],
    },
    {
        "name": "Патокин Анатолий Андреевич",
        "position": "Кондитер",
        "metrics": [{
            "title": "Изделия по ТТК",
            "formula": "Количество изделий, соответствующих ТТК (шт./мес.), "
                       "при уровне брака не выше 2%.",
            "kind": "number", "unit": "шт.", "direction": "up", "target": None,
            "values": {0: 540, 1: 528, 2: 1818},
        }],
    },
    {
        "name": "Волкова Юлия Олеговна",
        "position": "Кондитер",
        "metrics": [{
            "title": "Изделия по ТТК",
            "formula": "Количество изделий, соответствующих ТТК (шт./мес.), "
                       "при уровне брака не выше 2%.",
            "kind": "number", "unit": "шт.", "direction": "up", "target": None,
            "values": {1: 1083, 2: 264},
        }],
    },
]


def _norm(name: str) -> str:
    return " ".join(name.split()).casefold()


def _short(name: str) -> str:
    """Фамилия + имя без отчества. В TMS сотрудник может быть заведён как
    «Волкова Юлия», а в HR-таблице — «Волкова Юлия Олеговна»: сопоставляем по
    первым двум словам, иначе скрипт наплодит дубли вместо привязки к своим."""
    return " ".join(_norm(name).split()[:2])


def _find_employee(db, name: str):
    """Ищет сотрудника: сначала по полному ФИО, затем по «фамилия + имя».
    Если по короткому имени нашлось несколько — не угадываем, возвращаем None
    (лучше создать явный дубль и дать человеку разобраться, чем привязать
    метрику к однофамильцу)."""
    everyone = db.query(HrEmployee).all()
    exact = [e for e in everyone if _norm(e.full_name) == _norm(name)]
    if exact:
        return exact[0]
    short = [e for e in everyone if _short(e.full_name) == _short(name)]
    return short[0] if len(short) == 1 else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Наполнение метрик сотрудников")
    parser.add_argument("--apply", action="store_true",
                        help="записать изменения (без флага — только показать план)")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    log: list[str] = []
    try:
        for item in SEED:
            employee = _find_employee(db, item["name"])
            if employee is None:
                employee = HrEmployee(full_name=item["name"], position=item["position"])
                db.add(employee)
                db.flush()
                log.append(f"+ сотрудник: {item['name']} ({item['position']})")
            else:
                log.append(f"= сотрудник: {employee.full_name} (уже есть, id={employee.id})")

            for spec in item["metrics"]:
                metric = next((m for m in db.query(HrMetric)
                               .filter(HrMetric.employee_id == employee.id).all()
                               if m.title == spec["title"]), None)
                if metric is None:
                    metric = HrMetric(employee_id=employee.id, title=spec["title"])
                    db.add(metric)
                    log.append(f"  + метрика: {spec['title']}")
                else:
                    log.append(f"  = метрика: {spec['title']} (уже есть)")
                metric.formula = spec["formula"]
                metric.kind = spec["kind"]
                metric.unit = spec["unit"]
                metric.direction = spec["direction"]
                metric.target = spec["target"]
                metric.label_total = spec.get("label_total", "всего")
                metric.label_bad = spec.get("label_bad", "с ошибкой")
                metric.is_active = True
                db.flush()

                for week_idx, raw in spec["values"].items():
                    ws = JULY_WEEKS[week_idx]
                    if isinstance(raw, tuple):
                        total, bad = raw
                        value = (total - bad) / total * 100 if total else None
                    else:
                        total = bad = None
                        value = float(raw)

                    rec = db.query(HrMetricValue).filter(
                        HrMetricValue.metric_id == metric.id,
                        HrMetricValue.week_start == ws).first()
                    if rec is None:
                        rec = HrMetricValue(metric_id=metric.id, week_start=ws)
                        db.add(rec)
                    rec.value = value
                    rec.raw_total = total
                    rec.raw_bad = bad
                    rec.filled_by_name = "Импорт из таблицы HR"
                    shown = f"{value:.1f}%" if isinstance(raw, tuple) else raw
                    log.append(f"    · {ws:%d.%m}–{ws + timedelta(days=6):%d.%m}: {shown}")

        print("\n".join(log))
        if args.apply:
            db.commit()
            print("\nГотово: изменения записаны.")
        else:
            db.rollback()
            print("\nПробный прогон — ничего не записано. Повторите с флагом --apply.")
    finally:
        db.close()


if __name__ == "__main__":
    main()

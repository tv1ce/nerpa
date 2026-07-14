"""Налог на логистику — раньше был жёстко зашит (+6% на все суммы). Теперь
хранится в LogisticsCost.tax_rate (%, вносится вручную по каждой строке —
и для «перевоза» (cost_type='delivery'), и для «платного забора» (cost_type='pickup')),
0/NULL = без налога. Общее место расчёта, чтобы не разъезжались формулы
в разных роутерах (logistics.py, reports.py)."""
from sqlalchemy import func

from app.models import LogisticsCost


def taxed_amount_expr():
    """SQL-выражение: amount * (1 + tax_rate/100), NULL tax_rate = 0%."""
    return LogisticsCost.amount * (1 + func.coalesce(LogisticsCost.tax_rate, 0) / 100.0)


def taxed_amount(row: LogisticsCost) -> float:
    """То же самое в Python — для уже загруженных объектов."""
    return round((row.amount or 0.0) * (1 + (row.tax_rate or 0.0) / 100.0), 2)


def sum_taxed(db, d_from, d_to) -> float:
    """Сумма логистических затрат с налогом за период [d_from, d_to]."""
    return db.query(func.sum(taxed_amount_expr())).filter(
        LogisticsCost.date >= d_from,
        LogisticsCost.date <= d_to,
    ).scalar() or 0.0

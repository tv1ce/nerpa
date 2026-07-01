"""Форматирование отчётов в Telegram MarkdownV2."""
from __future__ import annotations

from datetime import date


def _esc(text) -> str:
    """Экранирует все спецсимволы MarkdownV2 в пользовательских строках.
    Обязательно для всех данных из БД (имена клиентов, продуктов и т.п.)."""
    if text is None:
        return ""
    s = str(text)
    for ch in r"\_*[]()~`>#+-=|{}.!":
        s = s.replace(ch, "\\" + ch)
    return s


def _esc_date(s: str) -> str:
    """Экранирует точки и дефисы в строках дат для MarkdownV2."""
    return s.replace(".", "\\.").replace("-", "\\-")


def _fmt(amount: float) -> str:
    """Форматирует число: 123456.7 → '123 456 ₽'."""
    return f"{amount:,.0f} ₽".replace(",", " ")


def _qty(q: float) -> str:
    return f"{q:,.0f} шт".replace(",", " ")


def _pct(val: float | None, positive_good: bool = True) -> str:
    if val is None:
        return "нет данных"
    arrow = "▲" if val > 0 else ("▼" if val < 0 else "→")
    if positive_good:
        sign = "✅" if val >= 0 else "🔴"
    else:
        sign = "🔴" if val > 0 else "✅"
    pct_str = f"{abs(val):.1f}%".replace(".", "\\.")
    return f"{sign} {arrow} {pct_str}"


def _plan_bar(pct: float | None) -> str:
    if pct is None:
        return "план не задан"
    filled = min(int(pct / 10), 10)
    bar = "█" * filled + "░" * (10 - filled)
    emoji = "🎯" if pct >= 100 else ("⚡" if pct >= 70 else "📉")
    pct_str = f"{pct:.1f}%".replace(".", "\\.")
    return f"{emoji} \\[{bar}\\] {pct_str}"


# ─────────────────────────────────────────────────────────────────────────────

def format_daily(m: dict) -> str:
    d: date = m["date"]
    date_str = _esc_date(d.strftime("%d.%m.%Y"))
    lines = [
        f"📊 *Ежедневный отчёт — {date_str}*",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "📦 *Отгрузки*",
        f"  Заказов создано: `{m['orders_today']}`",
        f"  Отгружено заказов: `{m['orders_shipped']}`",
        f"  Сумма отгрузок: `{_fmt(m['shipped_amount_today'])}`",
        f"  Орешков: `{_qty(m['qty_today'])}`",
        "",
        "💰 *Оплаты*",
        f"  Поступило сегодня: `{_fmt(m['paid_today'])}`",
        f"  Счетов выставлено: `{m['issued_today']}`",
    ]

    if m["overdue_count"] > 0:
        lines += [
            "",
            "⚠️ *Просроченные счета*",
            f"  Штук: `{m['overdue_count']}`",
            f"  На сумму: `{_fmt(m['overdue_sum'])}`",
        ]

    if m["new_claims"] > 0:
        lines += [
            "",
            f"🚨 Новых рекламаций: `{m['new_claims']}`",
        ]

    return "\n".join(lines)


def format_weekly(m: dict) -> str:
    ws  = _esc_date(m["week_start"].strftime("%d.%m"))
    we  = _esc_date(m["week_end"].strftime("%d.%m.%Y"))
    pws = _esc_date(m["prev_week_start"].strftime("%d.%m"))
    pwe = _esc_date(m["prev_week_end"].strftime("%d.%m"))

    lines = [
        "📈 *Еженедельный отчёт*",
        f"_{ws} – {we}_",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "📦 *Отгрузки* \\(по дате заказа\\)",
        f"  Эта неделя: `{_fmt(m['shipped_week'])}`",
        f"  Прошлая \\({pws}–{pwe}\\): `{_fmt(m['shipped_prev'])}`",
        f"  Динамика: {_pct(m['delta_shipped_pct'])}",
        "",
        "💰 *Оплаты* \\(поступило\\)",
        f"  Эта неделя: `{_fmt(m['paid_week'])}`",
        f"  Прошлая: `{_fmt(m['paid_prev'])}`",
        f"  Динамика: {_pct(m['delta_paid_pct'])}",
        "",
        "🌰 *Орешки отгружено*",
        f"  Эта неделя: `{_qty(m['qty_week'])}`",
        f"  Прошлая: `{_qty(m['qty_prev'])}`",
        f"  Динамика: {_pct(m['delta_qty_pct'])}",
        "",
        "📋 *Заказы за неделю*",
        f"  Всего: `{m['orders_week']}`",
        f"  Новых контрагентов: `{m['new_clients']}`",
        "",
        "🚚 *Логистика*",
        f"  Расходы за неделю: `{_fmt(m['logistics_week'])}`",
        f"  На 1 заказ: `{_fmt(m['logistics_per_order_week'])}`",
    ]

    if m["unpaid_issued"] > 0:
        lines += [
            "",
            f"💳 Выставлено, не оплачено: `{_fmt(m['unpaid_issued'])}`",
        ]

    # Статус месяца
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "🗓 *Статус месяца*",
        f"  Отгрузки с начала месяца: `{_fmt(m['shipped_month_so_far'])}`",
        f"  Оплаты с начала месяца: `{_fmt(m['paid_month_so_far'])}`",
    ]
    if m["plan_amount"]:
        lines.append(f"  {_plan_bar(m['plan_pct'])}")
        if m["plan_remaining"] and m["plan_remaining"] > 0:
            lines.append(f"  До плана по оплатам: `{_fmt(m['plan_remaining'])}`")
        else:
            lines.append("  ✅ План по оплатам выполнен\\!")
    else:
        lines.append("  план не задан")

    # Сводные показатели
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "📊 *Сводные показатели*",
        f"  Оплаты за {m['week_start'].year} год: `{_fmt(m['paid_year'])}`",
        f"  Орешков продано за всё время: `{_qty(m['qty_all_time'])}`",
    ]

    if m["top_clients"]:
        lines += ["", "🏆 *Топ клиентов за неделю \\(по отгрузкам\\)*"]
        for i, (name, total) in enumerate(m["top_clients"], 1):
            lines.append(f"  {i}\\. {_esc(name)}: `{_fmt(total)}`")

    return "\n".join(lines)


def format_monthly(m: dict) -> str:
    month_lbl      = _esc(m["month_label"])
    prev_month_lbl = _esc(m["prev_month_label"])

    lines = [
        f"🗓 *Итоги месяца — {month_lbl}*",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "📦 *Отгрузки* \\(по дате заказа\\)",
        f"  Месяц: `{_fmt(m['shipped_month'])}`",
        f"  {prev_month_lbl}: `{_fmt(m['shipped_prev'])}`",
        f"  Динамика: {_pct(m['delta_shipped_pct'])}",
        "",
        "💰 *Оплаты* \\(поступило\\)",
        f"  Месяц: `{_fmt(m['paid_month'])}`",
        f"  {prev_month_lbl}: `{_fmt(m['paid_prev'])}`",
        f"  Динамика: {_pct(m['delta_paid_pct'])}",
        f"  С начала года: `{_fmt(m['paid_year'])}`",
        "",
        "🎯 *Выполнение плана \\(по оплатам\\)*",
    ]

    if m["plan_amount"]:
        lines.append(f"  План: `{_fmt(m['plan_amount'])}`")
        lines.append(f"  {_plan_bar(m['plan_pct'])}")
    else:
        lines.append("  план не задан")

    lines += [
        "",
        "🌰 *Орешки*",
        f"  Отгружено за месяц: `{_qty(m['qty_month'])}`",
        f"  За {prev_month_lbl}: `{_qty(m['qty_prev'])}`",
        f"  Динамика: {_pct(m['delta_qty_pct'])}",
        "",
        "📋 *Заказы*",
        f"  Всего за месяц: `{m['orders_month']}`",
        f"  Новые клиенты: `{m['orders_new_clients']}`",
        "",
        "🚚 *Логистика*",
        f"  Расходы за месяц: `{_fmt(m['logistics_month'])}`",
        f"  На 1 заказ: `{_fmt(m['logistics_per_order_month'])}`",
        f"  За год: `{_fmt(m['logistics_year'])}`",
        "",
        f"📊 Маржа \\(отгрузки − логистика\\): `{_fmt(m['margin_month'])}`",
    ]

    if m["overdue_count"] > 0 or m["unpaid_total"] > 0:
        lines += [
            "",
            "💳 *Дебиторская задолженность*",
            f"  К оплате \\(выставлены\\): `{_fmt(m['unpaid_total'])}`",
            f"  Просроченные \\({m['overdue_count']} шт\\): `{_fmt(m['overdue_total'])}`",
        ]

    if m["top_clients"]:
        lines += ["", "🏆 *Топ\\-5 клиентов \\(по отгрузкам\\)*"]
        for i, (name, total) in enumerate(m["top_clients"], 1):
            lines.append(f"  {i}\\. {_esc(name)}: `{_fmt(total)}`")

    if m["top_products"]:
        lines += ["", "🔝 *Топ продуктов*"]
        for i, (name, qty) in enumerate(m["top_products"], 1):
            lines.append(f"  {i}\\. {_esc(name)}: `{_qty(qty)}`")

    if m["claims_new"] > 0:
        lines += [
            "",
            "🚨 *Рекламации за месяц*",
            f"  Открыто: `{m['claims_new']}`",
            f"  Закрыто: `{m['claims_resolved']}`",
        ]

    if m["expiring_contracts"] > 0:
        lines += [
            "",
            f"⚠️ Договоров истекает \\(30 дней\\): `{m['expiring_contracts']}`",
        ]

    return "\n".join(lines)

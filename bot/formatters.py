"""Форматирование отчётов в Telegram Markdown."""
from __future__ import annotations

from datetime import date


def _esc(text) -> str:
    """Экранирует спецсимволы Markdown v1 в пользовательских строках
    (имена клиентов, продуктов), чтобы не сломать форматирование сообщения."""
    if text is None:
        return ""
    s = str(text)
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


def _fmt(amount: float) -> str:
    """Форматирует число: 123456.7 → '123 456 ₽'."""
    return f"{amount:,.0f} ₽".replace(",", " ")


def _qty(q: float) -> str:
    return f"{q:,.0f} шт".replace(",", " ")


def _pct(val: float | None, positive_good: bool = True) -> str:
    if val is None:
        return "нет данных"
    arrow = "▲" if val > 0 else ("▼" if val < 0 else "→")
    if positive_good:
        sign = "✅" if val >= 0 else "🔴"
    else:
        sign = "🔴" if val > 0 else "✅"
    return f"{sign} {arrow} {abs(val):.1f}%"


def _plan_bar(pct: float | None) -> str:
    if pct is None:
        return "план не задан"
    filled = min(int(pct / 10), 10)
    bar = "█" * filled + "░" * (10 - filled)
    emoji = "🎯" if pct >= 100 else ("⚡" if pct >= 70 else "📉")
    return f"{emoji} [{bar}] {pct:.1f}%"


# ─────────────────────────────────────────────────────────────────────────────

def format_daily(m: dict) -> str:
    d: date = m["date"]
    lines = [
        f"📊 *Ежедневный отчёт — {d.strftime('%d.%m.%Y')}*",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "📦 *Заказы*",
        f"  Создано сегодня: `{m['orders_today']}`",
        f"  Отгружено/доставлено: `{m['orders_shipped']}`",
        f"  Орешков отгружено: `{_qty(m['qty_today'])}`",
        "",
        "💰 *Финансы*",
        f"  Оплачено сегодня: `{_fmt(m['revenue_today'])}`",
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
    ws = m["week_start"].strftime("%d.%m")
    we = m["week_end"].strftime("%d.%m.%Y")
    pws = m["prev_week_start"].strftime("%d.%m")
    pwe = m["prev_week_end"].strftime("%d.%m")

    lines = [
        f"📈 *Еженедельный отчёт*",
        f"_{ws} – {we}_",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "💰 *Выручка*",
        f"  Эта неделя: `{_fmt(m['revenue_week'])}`",
        f"  Прошлая ({pws}–{pwe}): `{_fmt(m['revenue_prev'])}`",
        f"  Динамика: {_pct(m['delta_rev_pct'])}",
        "",
        "📦 *Орешки отгружено*",
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
    ]

    if m["unpaid_issued"] > 0:
        lines += [
            "",
            f"💳 Выставлено, не оплачено: `{_fmt(m['unpaid_issued'])}`",
        ]

    if m["top_clients"]:
        lines += ["", "🏆 *Топ клиентов за неделю*"]
        for i, (name, total) in enumerate(m["top_clients"], 1):
            lines.append(f"  {i}. {_esc(name)}: `{_fmt(total)}`")

    return "\n".join(lines)


def format_monthly(m: dict) -> str:
    lines = [
        f"🗓 *Итоги месяца — {m['month_label']}*",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "💰 *Выручка*",
        f"  Месяц: `{_fmt(m['revenue_month'])}`",
        f"  {m['prev_month_label']}: `{_fmt(m['revenue_prev'])}`",
        f"  Динамика: {_pct(m['delta_month_pct'])}",
        f"  С начала года: `{_fmt(m['revenue_year'])}`",
        "",
        f"🎯 *Выполнение плана*",
    ]

    if m["plan_amount"]:
        lines.append(f"  План: `{_fmt(m['plan_amount'])}`")
        lines.append(f"  {_plan_bar(m['plan_pct'])}")
    else:
        lines.append("  план не задан")

    lines += [
        "",
        "📦 *Орешки*",
        f"  Отгружено за месяц: `{_qty(m['qty_month'])}`",
        f"  За {m['prev_month_label']}: `{_qty(m['qty_prev'])}`",
        f"  Динамика: {_pct(m['delta_qty_pct'])}",
        "",
        "📋 *Заказы*",
        f"  Всего за месяц: `{m['orders_month']}`",
        f"  Новые клиенты: `{m['orders_new_clients']}`",
        "",
        "🚚 *Логистика*",
        f"  Расходы за месяц: `{_fmt(m['logistics_month'])}`",
        f"  За год: `{_fmt(m['logistics_year'])}`",
    ]

    if m["overdue_count"] > 0 or m["unpaid_total"] > 0:
        lines += [
            "",
            "💳 *Дебиторская задолженность*",
            f"  К оплате (выставлены): `{_fmt(m['unpaid_total'])}`",
            f"  Просроченные ({m['overdue_count']} шт): `{_fmt(m['overdue_total'])}`",
        ]

    if m["top_clients"]:
        lines += ["", "🏆 *Топ-5 клиентов*"]
        for i, (name, total) in enumerate(m["top_clients"], 1):
            lines.append(f"  {i}. {_esc(name)}: `{_fmt(total)}`")

    if m["top_products"]:
        lines += ["", "🔝 *Топ продуктов*"]
        for i, (name, qty) in enumerate(m["top_products"], 1):
            lines.append(f"  {i}. {_esc(name)}: `{_qty(qty)}`")

    if m["claims_new"] > 0:
        lines += [
            "",
            f"🚨 *Рекламации за месяц*",
            f"  Открыто: `{m['claims_new']}`",
            f"  Закрыто: `{m['claims_resolved']}`",
        ]

    if m["expiring_contracts"] > 0:
        lines += [
            "",
            f"⚠️ Договоров истекает (30 дней): `{m['expiring_contracts']}`",
        ]

    return "\n".join(lines)

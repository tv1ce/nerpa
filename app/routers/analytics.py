"""Аналитика по точкам: расход орешков, ритм заказов, прогноз и ИИ-разбор.

Раздел отвечает на вопрос «что происходит на конкретном адресе»: сколько точка
продаёт в день, как часто заказывает, когда у неё кончится товар и кому звонить
сегодня. Считается по всем контрагентам, сеть — только один из срезов.
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import login_required, role_required
from app.database import get_db
from app.models import Network, OutletInsight
from app.services.outlets import (
    OUTLET_STATUSES, add_benchmarks, build_outlets, summary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics"])
templates = Jinja2Templates(directory="app/templates")  # подменяется общим env в main.py

STATUS_COLORS = {
    "empty": "danger", "soon": "warning", "ok": "success",
    "sleeping": "secondary", "new": "info",
}

# ── Промпты ──────────────────────────────────────────────────────────────────
# Все числа считает код — модель их только интерпретирует. Так она не выдумывает
# расход и даты, а занимается тем, что действительно умеет: связывает факты и
# формулирует, что с этим делать.

AI_OUTLET_PROMPT = """\
Ты — аналитик отдела продаж кондитерской компании: мы поставляем орешки со сгущёнкой
в кофейни и кафе. Тебе дана статистика по одной торговой точке (адресу доставки).

Разбери точку по пунктам:
1. Что происходит: точка растёт, стабильна или проседает — с опорой на цифры расхода
   и интервалов между поставками.
2. Есть ли риск, что точка стоит без товара или уйдёт к конкуренту.
3. Рекомендация по следующей поставке: когда и в каком объёме (в штуках, кратно
   поставкам, которые точка уже брала), какие вкусы усилить исходя из её микса.
4. Короткий текст (2-3 предложения), который менеджер может отправить клиенту.

Пиши по-русски, по делу, без markdown-заголовков и без воды. Не выдумывай цифр,
которых нет во входных данных. Если данных мало (одна-две поставки) — так и скажи."""

AI_DIGEST_PROMPT = """\
Ты — руководитель отдела продаж кондитерской компании (поставляем орешки в кофейни).
Тебе дана сводная таблица по торговым точкам: расход в день, дни до конца запаса,
ритм заказов, тренд, отклонение от медианы по базе.

Составь список задач на сегодня: кому звонить в первую очередь и зачем.
Верни СТРОГО JSON-массив без пояснений, максимум 7 элементов, формата:
[{"outlet": "адрес как в таблице", "priority": "high|mid|low",
  "action": "что сделать одной фразой", "why": "обоснование с цифрами из таблицы"}]

Сортируй по важности: сначала точки, которые уже стоят без товара или встанут
завтра, потом просевшие по расходу, потом спящие. Не выдумывай точек и цифр."""


def _outlets(db: Session) -> list[dict]:
    outlets = build_outlets(db)
    add_benchmarks(outlets)
    return outlets


def _filtered(outlets: list[dict], q: str, status: str, network_id: str) -> list[dict]:
    res = outlets
    if status:
        res = [o for o in res if o["status"] == status]
    if q:
        needle = q.lower()
        res = [o for o in res
               if needle in o["label"].lower()
               or any(needle in a.lower() for a in o["raw_addresses"])
               or any(needle in (cp.trade_name or cp.name or "").lower()
                      for cp in o["counterparties"])]
    if network_id:
        nid = int(network_id)
        res = [o for o in res
               if any(cp.network_id == nid for cp in o["counterparties"])]
    return res


def _latest_insight(db: Session, scope: str, key: str | None = None) -> OutletInsight | None:
    q = db.query(OutletInsight).filter(OutletInsight.scope == scope)
    q = q.filter(OutletInsight.address_key == key) if key else q
    return q.order_by(OutletInsight.created_at.desc()).first()


# ── Список точек ─────────────────────────────────────────────────────────────

@router.get("/outlets", response_class=HTMLResponse)
@login_required
async def outlets_list(request: Request, q: str = "", status: str = "",
                       network_id: str = "", db: Session = Depends(get_db)):
    outlets = _outlets(db)
    rows = _filtered(outlets, q, status, network_id)
    return templates.TemplateResponse(request, "analytics/outlets.html", {
        "outlets": rows,
        "summary": summary(outlets),
        "statuses": OUTLET_STATUSES,
        "status_colors": STATUS_COLORS,
        "q": q, "status": status, "network_id": network_id,
        "networks": db.query(Network).filter(Network.is_active == True)  # noqa: E712
                      .order_by(Network.name).all(),
        "digest": _latest_insight(db, "digest"),
        "error": request.query_params.get("error"),
    })


# ── Карточка точки ───────────────────────────────────────────────────────────

@router.get("/outlets/detail", response_class=HTMLResponse)
@login_required
async def outlet_detail(request: Request, key: str = "", db: Session = Depends(get_db)):
    outlet = next((o for o in _outlets(db) if o["key"] == key), None)
    if not outlet:
        return RedirectResponse(url="/analytics/outlets", status_code=302)
    insights = (db.query(OutletInsight)
                .filter(OutletInsight.scope == "outlet", OutletInsight.address_key == key)
                .order_by(OutletInsight.created_at.desc()).limit(5).all())
    return templates.TemplateResponse(request, "analytics/outlet_detail.html", {
        "o": outlet, "statuses": OUTLET_STATUSES, "status_colors": STATUS_COLORS,
        "insights": insights,
        "error": request.query_params.get("error"),
    })


# ── ИИ ───────────────────────────────────────────────────────────────────────

def _outlet_facts(o: dict) -> str:
    """Цифры точки для модели — обычным текстом, без markdown."""
    fmt = lambda v, s="": f"{v:.1f}{s}" if isinstance(v, (int, float)) else "нет данных"
    lines = [
        f"Точка: {o['label']}",
        f"Адреса в заказах: {'; '.join(o['raw_addresses'])}",
        f"Клиент(ы): {', '.join((cp.trade_name or cp.name) for cp in o['counterparties']) or '—'}",
        f"Сеть: {', '.join(o['networks']) or 'вне сети'}",
        f"Поставок всего: {o['deliveries_count']}, первая {o['first_date']}, последняя {o['last_date']}",
        f"Дней с последней поставки: {o['days_since']}",
        f"Средний расход: {fmt(o['daily_rate'])} шт/день",
        f"Средний интервал между поставками: {fmt(o['avg_interval'])} дней",
        f"Частота заказов: {fmt(o['orders_per_month'])} раз(а) в месяц",
        f"Средний объём поставки: {fmt(o['avg_qty'])} шт, последняя поставка {o['last_qty']:.0f} шт",
        f"Расчётный остаток: {fmt(o['days_left'])} дней",
        f"Тренд последнего интервала к своему среднему: "
        f"{o['trend_pct'] if o['trend_pct'] is not None else 'нет данных'}%",
        f"Отклонение расхода от медианы по всем точкам: "
        f"{o['vs_median_pct'] if o.get('vs_median_pct') is not None else 'нет данных'}%",
        f"Микс вкусов, %: {', '.join(f'{k} {v}' for k, v in o['flavor_mix'].items()) or '—'}",
        f"Выручка по точке за всё время: {o['revenue']:.0f} руб",
        "История поставок (дата — штук):",
    ]
    lines += [f"  {d['date']} — {d['qty']:.0f}" for d in o["deliveries"]]
    return "\n".join(lines)


@router.post("/outlets/analyze")
@role_required("manager")
async def outlet_analyze(request: Request, key: str = Form(...),
                         db: Session = Depends(get_db)):
    """ИИ-разбор одной точки (OpenRouter) с сохранением в историю."""
    outlet = next((o for o in _outlets(db) if o["key"] == key), None)
    if not outlet:
        return RedirectResponse(url="/analytics/outlets", status_code=302)

    from app.services import openrouter_client
    try:
        text = await asyncio.to_thread(
            openrouter_client.chat, AI_OUTLET_PROMPT, _outlet_facts(outlet))
    except Exception as e:
        logger.error("outlet ai %s: %s", key, e)
        return RedirectResponse(
            url=f"/analytics/outlets/detail?key={key}&error=Не+удалось+получить+ответ+ИИ",
            status_code=302)

    db.add(OutletInsight(address_key=key, scope="outlet", text=text.strip(),
                         model=openrouter_client.MODEL,
                         created_by=request.session.get("user_id")))
    db.commit()
    return RedirectResponse(url=f"/analytics/outlets/detail?key={key}", status_code=302)


def _digest_table(outlets: list[dict]) -> str:
    """Компактная таблица по всем точкам — вход для сводки ИИ."""
    head = ("адрес | клиент | расход шт/день | дней до конца | интервал дней | "
            "заказов в месяц | тренд % | к медиане % | дней с поставки | статус")
    rows = [head]
    for o in outlets:
        num = lambda v: f"{v:.1f}" if isinstance(v, (int, float)) else "—"
        client = ", ".join((cp.trade_name or cp.name) for cp in o["counterparties"]) or "—"
        rows.append(" | ".join([
            o["label"], client, num(o["daily_rate"]), num(o["days_left"]),
            num(o["avg_interval"]), num(o["orders_per_month"]),
            str(o["trend_pct"] if o["trend_pct"] is not None else "—"),
            str(o.get("vs_median_pct") if o.get("vs_median_pct") is not None else "—"),
            str(o["days_since"]), OUTLET_STATUSES.get(o["status"], o["status"]),
        ]))
    return "\n".join(rows)


@router.post("/outlets/digest")
@role_required("manager")
async def outlets_digest(request: Request, db: Session = Depends(get_db)):
    """ИИ-сводка «кому звонить сегодня» по всем точкам."""
    outlets = _outlets(db)
    if not outlets:
        return RedirectResponse(url="/analytics/outlets", status_code=302)

    import json

    from app.services import openrouter_client
    try:
        data = await asyncio.to_thread(
            openrouter_client.chat_json, AI_DIGEST_PROMPT, _digest_table(outlets))
    except Exception as e:
        logger.error("outlets digest ai: %s", e)
        return RedirectResponse(
            url="/analytics/outlets?error=Не+удалось+получить+сводку+ИИ", status_code=302)

    db.add(OutletInsight(address_key=None, scope="digest",
                         text=json.dumps(data, ensure_ascii=False),
                         model=openrouter_client.MODEL,
                         created_by=request.session.get("user_id")))
    db.commit()
    return RedirectResponse(url="/analytics/outlets", status_code=302)

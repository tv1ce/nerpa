"""Аналитика по точкам: расход орешков, ритм заказов, прогноз и ИИ-разбор.

Раздел отвечает на вопрос «что происходит на конкретном адресе»: сколько точка
продаёт в день, как часто заказывает, когда у неё кончится товар и кому звонить
сегодня. Считается по всем контрагентам, сеть — только один из срезов.
"""
import asyncio
import logging
import threading

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import login_required, role_required
from app.database import get_db
from app.models import Network, OutletGeo, OutletInsight
from app.services.outlets import (
    OUTLET_STATUSES, add_benchmarks, add_network_comparison, build_outlets,
    geocode_queries, network_outlets, summary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics"])
templates = Jinja2Templates(directory="app/templates")  # подменяется общим env в main.py

STATUS_COLORS = {
    "empty": "danger", "soon": "warning", "ok": "success",
    "sleeping": "secondary", "lost": "dark", "new": "info",
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

Важно про нулевые суммы: поставка на 0 руб — это не дозаказ и не рост продаж, а почти
всегда рекламация (везём замену брака бесплатно). Такие строки помечены «рекламация».
Не считай их признаком спроса, не предлагай «повторить успех», а наоборот — обрати
внимание, если рекламаций несколько: это проблема с качеством или с хранением на точке.

Пиши по-русски, по делу, без markdown-заголовков и без воды. Не выдумывай цифр,
которых нет во входных данных. Если данных мало (одна-две поставки) — так и скажи."""

AI_DIGEST_PROMPT = """\
Ты — руководитель отдела продаж кондитерской компании (поставляем орешки в кофейни).
Тебе дана сводная таблица по торговым точкам: расход в день, дни до конца запаса,
ритм заказов, тренд, отклонение от медианы по базе.

Составь список задач на сегодня: кому звонить в первую очередь и зачем.
Верни СТРОГО JSON-массив без пояснений, максимум 7 элементов, формата:
[{"outlet": "адрес как в таблице", "client": "контрагент из колонки «клиент»",
  "priority": "high|mid|low",
  "action": "что сделать одной фразой", "why": "обоснование с цифрами из таблицы"}]

Всегда заполняй client — менеджеру звонить контрагенту, а адрес нужен, чтобы
понимать, о какой именно его точке речь.

Сортируй по важности: сначала точки, которые уже стоят без товара или встанут
завтра, потом просевшие по расходу, потом спящие. Не выдумывай точек и цифр.

Колонка «рекламаций» — это поставки с нулевой суммой: бесплатная замена брака, а не
продажа. Точка с рекламациями — повод разобраться с качеством, но не признак роста."""


def _outlets(db: Session) -> list[dict]:
    outlets = build_outlets(db)
    add_benchmarks(outlets)
    add_network_comparison(outlets)
    return outlets


def _filtered(outlets: list[dict], q: str, status: str, network_id: str) -> list[dict]:
    res = outlets
    if status:
        res = [o for o in res if o["status"] == status]
    if q:
        needle = q.lower()
        res = [o for o in res
               if needle in o["label"].lower()
               or needle in o.get("client_label", "").lower()
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
    outlets = _outlets(db)
    outlet = next((o for o in outlets if o["key"] == key), None)
    if not outlet:
        return RedirectResponse(url="/analytics/outlets", status_code=302)
    # Соседи по адресу: на одном адресе может работать несколько контрагентов
    neighbours = [o for o in outlets
                  if o["address_key"] == outlet["address_key"] and o["key"] != key]
    insights = (db.query(OutletInsight)
                .filter(OutletInsight.scope == "outlet", OutletInsight.address_key == key)
                .order_by(OutletInsight.created_at.desc()).limit(5).all())
    geo = (db.query(OutletGeo)
           .filter(OutletGeo.address_key == outlet["address_key"]).first())
    return templates.TemplateResponse(request, "analytics/outlet_detail.html", {
        "o": outlet, "statuses": OUTLET_STATUSES, "status_colors": STATUS_COLORS,
        "insights": insights, "geo": geo, "neighbours": neighbours,
        "error": request.query_params.get("error"),
    })


# ── ИИ ───────────────────────────────────────────────────────────────────────

def _outlet_facts(o: dict) -> str:
    """Цифры точки для модели — обычным текстом, без markdown."""
    fmt = lambda v, s="": f"{v:.1f}{s}" if isinstance(v, (int, float)) else "нет данных"
    lines = [
        f"Точка: {o['label']} — {o['client_label']}",
        f"Адреса в заказах: {'; '.join(o['raw_addresses'])}",
        f"Контрагент(ы), на кого оформлены заказы:",
    ]
    for cp in o["counterparties"] or []:
        lines.append(
            f"  — {cp.trade_name or cp.name} (юр. лицо: {cp.name}"
            + (f", ИНН {cp.inn}" if cp.inn else "")
            + (f", категория {cp.category}" if cp.category else "")
            + (f", контакт: {cp.contact_person}" if cp.contact_person else "")
            + (f", сеть «{cp.network.name}»" if cp.network_id and cp.network else "")
            + ")"
        )
    if not o["counterparties"]:
        lines.append("  — не определён")
    lines += [
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
    ]
    rank = o.get("network_rank")
    if rank:
        lines.append(
            f"Место внутри сети «{rank['network']}»: {rank['place']} из {rank['total']}; "
            f"лучшая точка сети {rank['best_label']} — {rank['best_rate']:.1f} шт/день "
            f"(эта точка {rank['gap_to_best_pct']:+d}% к лучшей, "
            f"{rank['vs_network_pct']:+d}% к среднему по сети)"
        )
    lines += [
        "История поставок (дата — штук, сумма):",
    ]
    lines += [
        f"  {d['date']} — {d['qty']:.0f} шт, {d['amount']:.0f} руб"
        + (" — РЕКЛАМАЦИЯ (нулевая сумма), не дозаказ" if d.get("free") else "")
        for d in o["deliveries"]
    ]
    if o.get("free_count"):
        lines.append(f"Из них рекламаций (нулевая сумма): {o['free_count']}")
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
            "заказов в месяц | тренд % | к медиане % | дней с поставки | "
            "рекламаций | статус")
    rows = [head]
    for o in outlets:
        num = lambda v: f"{v:.1f}" if isinstance(v, (int, float)) else "—"
        client = ", ".join((cp.trade_name or cp.name) for cp in o["counterparties"]) or "—"
        rows.append(" | ".join([
            o["label"], client, num(o["daily_rate"]), num(o["days_left"]),
            num(o["avg_interval"]), num(o["orders_per_month"]),
            str(o["trend_pct"] if o["trend_pct"] is not None else "—"),
            str(o.get("vs_median_pct") if o.get("vs_median_pct") is not None else "—"),
            str(o["days_since"]), str(o.get("free_count", 0)),
            OUTLET_STATUSES.get(o["status"], o["status"]),
        ]))
    return "\n".join(rows)


@router.post("/outlets/digest")
@role_required("manager")
async def outlets_digest(request: Request, send_tg: str = Form(default=""),
                         db: Session = Depends(get_db)):
    """ИИ-сводка «кому звонить сегодня». send_tg=1 — сразу отправить её в Telegram
    (то же самое, что делает ежедневная задача по расписанию)."""
    from app.services import outlets_digest as digest_service

    user_id = request.session.get("user_id")
    try:
        if send_tg == "1":
            result = await asyncio.to_thread(digest_service.send, db, user_id)
            if not result.get("ok"):
                return RedirectResponse(
                    url=f"/analytics/outlets?error={result.get('error', 'Не отправлено')}",
                    status_code=302)
        else:
            await asyncio.to_thread(digest_service.generate, db, user_id)  # (задачи, спящие)
    except Exception as e:
        logger.error("outlets digest: %s", e)
        return RedirectResponse(
            url="/analytics/outlets?error=Не+удалось+получить+сводку+ИИ", status_code=302)
    return RedirectResponse(url="/analytics/outlets", status_code=302)


# ── Карта точек ──────────────────────────────────────────────────────────────
# Геокодирование — фоновым потоком через тот же Nominatim, что и в «Прозвоне»
# (1 запрос/сек, поэтому 80 точек занимают полторы минуты). Результат кэшируется
# в outlet_geo: ключ точки стабилен, повторно дёргать геокодер незачем.

_geo_state: dict = {"running": False, "done": 0, "total": 0, "found": 0, "error": None}


def _geo_queries(outlet: dict) -> list[str]:
    """Варианты адреса для геокодера — от написания в заказе к очищенному."""
    return geocode_queries(outlet["address_key"], outlet["raw_addresses"]) or [outlet["label"]]


def _geocode_worker(items: list[tuple[str, str]]) -> None:
    """Фоновый обход точек: (ключ, адрес) → координаты в outlet_geo.

    Nominatim разрешает не больше запроса в секунду и на превышение отвечает 429,
    поэтому идём с паузой и переживаем короткие сбои повтором — иначе проход
    обрывается на первом же лимите и большинство точек остаётся без координат."""
    import time

    from app.database import SessionLocal
    from app.utils.geocode import _DELAY, GeocodeRequestError, geocode_address_sync

    db = SessionLocal()
    stopped = False
    try:
        for i, (key, queries) in enumerate(items):
            found, used = None, queries[0]
            # Варианты адреса пробуем по очереди: как в заказе, затем очищенный
            for query in queries:
                if i or query != queries[0]:
                    time.sleep(_DELAY)
                for attempt in range(3):
                    try:
                        found = geocode_address_sync(query)
                        break
                    except GeocodeRequestError as e:
                        # Сеть/лимит — не вина адреса: ждём дольше и пробуем ещё раз
                        wait = e.retry_after or (_DELAY * (attempt + 2) * 2)
                        logger.warning("geocode retry %s после %.1fс: %s", attempt + 1, wait, e)
                        _geo_state["error"] = "геокодер ограничивает запросы, идём медленнее"
                        time.sleep(min(wait, 30))
                else:
                    _geo_state["error"] = ("геокодер недоступен — часть точек осталась без "
                                           "координат, попробуйте позже")
                    logger.warning("geocode stopped after retries on %r", query)
                    stopped = True
                    break
                used = query
                if found:
                    break
            if stopped:
                break
            row = db.query(OutletGeo).filter(OutletGeo.address_key == key).first()
            if not row:
                row = OutletGeo(address_key=key)
                db.add(row)
            row.query = used
            if found:
                row.lat, row.lng = found
                row.not_found = False
                _geo_state["found"] += 1
            else:
                row.not_found = True
            db.commit()
            _geo_state["done"] += 1
    except Exception as e:
        logger.error("geocode worker: %s", e)
    finally:
        _geo_state["running"] = False
        db.close()


@router.post("/outlets/geocode", response_class=JSONResponse)
@role_required("manager")
async def outlets_geocode(request: Request, db: Session = Depends(get_db)):
    """Запускает геокодирование точек без координат."""
    if _geo_state["running"]:
        return JSONResponse(dict(_geo_state))

    known = {g.address_key for g in db.query(OutletGeo).filter(
        (OutletGeo.lat.isnot(None)) | (OutletGeo.not_found == True))}  # noqa: E712
    # Ключ координат — адрес: у двух контрагентов на одном адресе точка на карте одна
    pending = {}
    for o in _outlets(db):
        if o["address_key"] not in known:
            pending.setdefault(o["address_key"], _geo_queries(o))
    pending = list(pending.items())
    if not pending:
        return JSONResponse({**_geo_state, "total": 0, "done": 0})

    _geo_state.update(running=True, done=0, total=len(pending), found=0, error=None)
    threading.Thread(target=_geocode_worker, args=(pending,), daemon=True).start()
    return JSONResponse(dict(_geo_state))


@router.post("/outlets/geocode/retry-failed", response_class=JSONResponse)
@role_required("manager")
async def outlets_geocode_retry(request: Request, db: Session = Depends(get_db)):
    """Забывает адреса, которые геокодер не нашёл, — чтобы попробовать заново
    (например, после того как адрес в заказе поправили)."""
    count = db.query(OutletGeo).filter(OutletGeo.not_found == True).delete()  # noqa: E712
    db.commit()
    return JSONResponse({"ok": True, "cleared": count})


@router.get("/outlets/geocode/status", response_class=JSONResponse)
@login_required
async def outlets_geocode_status(request: Request):
    return JSONResponse(dict(_geo_state))


@router.get("/outlets/map", response_class=HTMLResponse)
@login_required
async def outlets_map(request: Request, db: Session = Depends(get_db)):
    outlets = _outlets(db)
    geo = {g.address_key: g for g in db.query(OutletGeo).all()}
    addresses = {o["address_key"] for o in outlets}
    located = sum(1 for a in addresses if geo.get(a) and geo[a].lat)
    not_found = sum(1 for a in addresses if geo.get(a) and geo[a].not_found)
    return templates.TemplateResponse(request, "analytics/map.html", {
        "total": len(addresses), "located": located, "not_found": not_found,
        "statuses": OUTLET_STATUSES, "status_colors": STATUS_COLORS,
        "geo_state": dict(_geo_state),
    })


@router.get("/outlets/map/data", response_class=JSONResponse)
@login_required
async def outlets_map_data(request: Request, db: Session = Depends(get_db)):
    """Точки с координатами для карты."""
    outlets = _outlets(db)
    geo = {g.address_key: g for g in db.query(OutletGeo).all()
           if g.lat is not None and g.lng is not None}
    rows = []
    seen_at_address: dict[str, int] = {}
    for o in outlets:
        g = geo.get(o["address_key"])
        if not g:
            continue
        # Два контрагента на одном адресе получили бы маркеры друг под другом —
        # раздвигаем их на несколько метров, чтобы кликались оба
        n = seen_at_address.get(o["address_key"], 0)
        seen_at_address[o["address_key"]] = n + 1
        lat, lng = g.lat + n * 0.00012, g.lng + n * 0.00022
        rows.append({
            "key": o["key"], "label": o["label"], "lat": lat, "lng": lng,
            "status": o["status"], "status_label": OUTLET_STATUSES[o["status"]],
            "daily_rate": round(o["daily_rate"], 1) if o["daily_rate"] else None,
            "days_left": round(o["days_left"]) if o["days_left"] is not None else None,
            "orders_per_month": round(o["orders_per_month"], 1) if o["orders_per_month"] else None,
            "last_date": o["last_date"].isoformat(),
            "clients": [(cp.trade_name or cp.name) for cp in o["counterparties"]],
            "networks": o["networks"],
            "address": _geo_queries(o)[0],
        })
    return JSONResponse(rows)


# ── Сравнение точек внутри сетей ─────────────────────────────────────────────

@router.get("/networks", response_class=HTMLResponse)
@login_required
async def networks_compare(request: Request, db: Session = Depends(get_db)):
    """Точки каждой сети рядом: кто тянет вывеску, а кто её проедает."""
    outlets = _outlets(db)
    groups = []
    for net in db.query(Network).filter(Network.is_active == True).order_by(Network.name):  # noqa: E712
        members = network_outlets(outlets, net.name)
        if not members:
            continue
        rates = [m["daily_rate"] for m in members if m["daily_rate"]]
        groups.append({
            "network": net,
            "outlets": members,
            "avg_rate": (sum(rates) / len(rates)) if rates else 0.0,
            "best": members[0] if rates else None,
            "worst": next((m for m in reversed(members) if m["daily_rate"]), None),
            "empty": sum(1 for m in members if m["status"] == "empty"),
            "daily_total": sum(rates),
        })
    groups.sort(key=lambda g: -g["daily_total"])
    return templates.TemplateResponse(request, "analytics/networks.html", {
        "groups": groups, "statuses": OUTLET_STATUSES, "status_colors": STATUS_COLORS,
    })

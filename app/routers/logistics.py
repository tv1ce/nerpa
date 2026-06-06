from datetime import date, timedelta
from io import BytesIO
import logging
import os, re, json

logger = logging.getLogger(__name__)

import httpx
from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import login_required
from app.database import get_db
from app.models import LogisticsCost, CompanySettings, Order

router = APIRouter(prefix="/reports/logistics", tags=["logistics"])
templates = Jinja2Templates(directory="app/templates")

METAFORA_URL = "https://app2024.damasevich.ru/dl/6471c6"


@router.get("", include_in_schema=False)
async def logistics_redirect():
    return RedirectResponse(url="/reports/logistics/", status_code=301)


def _month_bounds(d: date):
    ms = d.replace(day=1)
    if ms.month == 12:
        me = ms.replace(year=ms.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        me = ms.replace(month=ms.month + 1, day=1) - timedelta(days=1)
    return ms, me


def _parse_glide_snapshot(j: dict) -> list[dict]:
    """Парсит ответ getAppSnapshot — ищет расходы на доставку."""
    result = []
    # Glide snapshot хранит данные в tables[i].rows[j].cells
    for key in ("tables", "data", "columnValues", "rows"):
        if key not in j:
            continue
        items = j[key]
        if isinstance(items, dict):
            items = list(items.values())
        if not isinstance(items, list):
            continue
        for tbl in items:
            rows = tbl.get("rows") or tbl.get("data") or []
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                # Ищем сумму расходов и дату
                amt = None
                row_date = date.today()
                desc = ""
                for k, v in row.items():
                    kl = str(k).lower()
                    if any(x in kl for x in ("расход", "сумма", "amount", "стоимость", "cost")):
                        try:
                            amt = abs(float(str(v).replace(" ", "").replace(",", ".")))
                        except Exception as e:
                            logger.debug("Glide CSV: не удалось распарсить сумму %r: %s", v, e)
                    if any(x in kl for x in ("дата", "date")):
                        try:
                            from datetime import datetime
                            if isinstance(v, str):
                                for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
                                    try:
                                        row_date = datetime.strptime(v[:10], fmt).date(); break
                                    except Exception:
                                        pass
                        except Exception as e:
                            logger.debug("Glide CSV: не удалось распарсить дату %r: %s", v, e)
                    if any(x in kl for x in ("опис", "наим", "адрес", "name", "desc", "address")):
                        desc = str(v)
                if amt and amt > 0:
                    result.append({"date": row_date, "description": desc, "amount": amt, "external_id": ""})
    return result


def _parse_glide_html(html: str) -> list[dict]:
    """
    Парсит HTML страницы Glide-приложения Метафоры.
    Ищет embedded JSON с данными (window.__INITIAL_DATA__ или похожее),
    затем пробует regex по числам и датам в тексте.
    Формат по скриншоту: строки с датой, адресом, суммой расхода.
    """
    result = []

    # ── 1. Поиск JSON, встроенного в HTML ────────────────────────────────
    for pat in [
        r'window\.__[A-Z_]+__\s*=\s*(\{.+?\})(?:;|\s*</script>)',
        r'<script[^>]*type="application/json"[^>]*>(.+?)</script>',
        r'initialData\s*[:=]\s*(\{.+?\})\s*[,;]',
    ]:
        for m in re.finditer(pat, html, re.DOTALL):
            try:
                j = json.loads(m.group(1))
                parsed = _parse_glide_snapshot(j)
                if parsed:
                    return parsed
            except Exception as e:
                logger.debug("Ошибка парсинга Glide snapshot: %s", e)

    # ── 2. Regex по паттерну строк таблицы ───────────────────────────────
    # Паттерн из скриншота: дата ДД.ММ.ГГГГ, адрес, сумма NNN₽ или NNN Р
    row_pattern = re.compile(
        r'(\d{2}[./]\d{2}[./]\d{4})'       # дата
        r'[^\d]{0,200}?'
        r'([А-Яа-яЁёA-Za-z][^\n\r<]{5,80})' # описание/адрес
        r'[^\d]{0,50}?'
        r'(\d[\d\s]*[\d])\s*[₽РRr]',        # сумма
        re.UNICODE,
    )
    for m in row_pattern.finditer(html):
        try:
            raw_date, desc, raw_amt = m.group(1), m.group(2).strip(), m.group(3)
            amt = float(raw_amt.replace(" ", ""))
            if amt <= 0:
                continue
            from datetime import datetime
            for fmt in ("%d.%m.%Y", "%d/%m/%Y"):
                try:
                    row_date = datetime.strptime(raw_date, fmt).date(); break
                except Exception:
                    pass
            else:
                continue
            if any(x in desc.lower() for x in ("расход", "доставка", "внесение", "кол-во")):
                continue  # пропускаем заголовки
            result.append({"date": row_date, "description": desc, "amount": amt, "external_id": ""})
        except Exception as e:
            logger.debug("_parse_glide_html: пропущена строка: %s", e)

    return result


def _parse_excel_bytes(data: bytes) -> list[dict]:
    """Парсит Excel-файл, ищет колонки с датой и суммой."""
    import openpyxl
    wb = openpyxl.load_workbook(BytesIO(data), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    # Определяем заголовки (первая непустая строка)
    header_row_idx = 0
    for i, row in enumerate(rows):
        if any(cell is not None for cell in row):
            header_row_idx = i
            break

    headers = [str(h).strip().lower() if h else "" for h in rows[header_row_idx]]

    # Ищем колонки по ключевым словам
    date_col = amt_col = desc_col = ext_col = None
    for i, h in enumerate(headers):
        if any(k in h for k in ("дата", "date", "период")):
            if date_col is None:
                date_col = i
        if any(k in h for k in ("сумма", "стоимость", "amount", "итого", "cost", "цена")):
            if amt_col is None:
                amt_col = i
        if any(k in h for k in ("описание", "наименование", "услуга", "name", "desc", "комментарий")):
            if desc_col is None:
                desc_col = i
        if any(k in h for k in ("номер", "id", "№", "заявка")):
            if ext_col is None:
                ext_col = i

    if amt_col is None:
        return []

    result = []
    for row in rows[header_row_idx + 1:]:
        if not any(cell is not None for cell in row):
            continue
        try:
            amt_raw = row[amt_col]
            if amt_raw is None:
                continue
            amount = float(str(amt_raw).replace(" ", "").replace(",", ".").replace("₽", ""))
            if amount <= 0:
                continue
        except (ValueError, TypeError):
            continue

        row_date = date.today()
        if date_col is not None and row[date_col]:
            try:
                d = row[date_col]
                if isinstance(d, (date,)):
                    row_date = d
                elif hasattr(d, "date"):
                    row_date = d.date()
                else:
                    from datetime import datetime
                    row_date = datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
            except Exception as e:
                logger.debug("Excel: не удалось распарсить дату %r: %s", row[date_col], e)

        description = str(row[desc_col]).strip() if desc_col is not None and row[desc_col] else ""
        ext_id = str(row[ext_col]).strip() if ext_col is not None and row[ext_col] else ""

        result.append({"date": row_date, "description": description,
                        "amount": amount, "external_id": ext_id})
    return result


def _parse_csv_bytes(data: bytes) -> list[dict]:
    import csv, io
    text = data.decode("utf-8-sig", errors="replace")
    dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    result = []
    for row in reader:
        # Ищем колонки
        amt = None
        row_date = date.today()
        desc = ""
        ext_id = ""
        for k, v in row.items():
            kl = k.lower()
            if any(x in kl for x in ("сумма", "стоимость", "amount", "итого", "cost")):
                try:
                    amt = float(str(v).replace(" ", "").replace(",", ".").replace("₽", ""))
                except Exception as e:
                    logger.debug("CSV: не удалось распарсить сумму %r: %s", v, e)
            if any(x in kl for x in ("дата", "date", "период")):
                try:
                    from datetime import datetime
                    row_date = datetime.strptime(v.strip()[:10], "%Y-%m-%d").date()
                except Exception:
                    try:
                        from datetime import datetime
                        row_date = datetime.strptime(v.strip()[:10], "%d.%m.%Y").date()
                    except Exception:
                        pass
            if any(x in kl for x in ("описание", "наименование", "услуга", "name", "desc")):
                desc = str(v).strip()
            if any(x in kl for x in ("номер", "id", "№", "заявка")):
                ext_id = str(v).strip()
        if amt and amt > 0:
            result.append({"date": row_date, "description": desc,
                            "amount": amt, "external_id": ext_id})
    return result


def _import_rows(db: Session, rows: list[dict], source: str) -> tuple[int, int]:
    """Сохраняет строки в БД, возвращает (добавлено, пропущено как дубли)."""
    added = skipped = 0
    for r in rows:
        if r.get("external_id"):
            exists = db.query(LogisticsCost).filter(
                LogisticsCost.external_id == r["external_id"],
                LogisticsCost.source == source,
            ).first()
            if exists:
                skipped += 1
                continue
        db.add(LogisticsCost(
            date=r["date"], description=r.get("description", ""),
            amount=r["amount"], source=source,
            external_id=r.get("external_id") or None,
        ))
        added += 1
    db.commit()
    return added, skipped


# ── Список ────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
@login_required
async def logistics_index(
    request: Request,
    period: str = "month",
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
):
    today = date.today()
    month_start, month_end = _month_bounds(today)
    year_start = today.replace(month=1, day=1)

    # Неделя
    week_start = today - timedelta(days=today.weekday())
    week_end   = week_start + timedelta(days=6)

    # Предыдущий месяц
    prev_month_last  = month_start - timedelta(days=1)
    prev_month_start, prev_month_end = _month_bounds(prev_month_last)

    if period == "week":
        tbl_from, tbl_to = week_start, week_end
    elif period == "year":
        tbl_from, tbl_to = year_start, today
    elif period == "prev_month":
        tbl_from, tbl_to = prev_month_start, prev_month_end
    elif period == "custom" and date_from and date_to:
        try:
            tbl_from = date.fromisoformat(date_from)
            tbl_to   = date.fromisoformat(date_to)
        except ValueError:
            tbl_from, tbl_to = month_start, today
    else:  # month (default)
        tbl_from, tbl_to = month_start, today

    raw_rows = db.query(LogisticsCost).filter(
        LogisticsCost.date >= tbl_from,
        LogisticsCost.date <= tbl_to,
    ).order_by(LogisticsCost.date.desc()).all()

    TAX = 1.06  # +6% налог (применяется ко всем отображаемым суммам)
    rows = [
        {
            "id":          r.id,
            "date":        r.date,
            "description": r.description,
            "notes":       r.notes,
            "source":      r.source,
            "amount":      round(r.amount * TAX, 2),
        }
        for r in raw_rows
    ]
    total = round(sum(r["amount"] for r in rows), 2)

    def _logi_sum(d_from, d_to):
        return db.query(func.sum(LogisticsCost.amount)).filter(
            LogisticsCost.date >= d_from,
            LogisticsCost.date <= d_to,
        ).scalar() or 0.0

    # ID перевозчика «логистики 1 заказа» берём из таблицы контрагентов.
    # Имя задаётся через env LOGI_CARRIER_NAME (по умолчанию — текущий перевозчик).
    from app.models import Counterparty as _CP
    _carrier_name = os.getenv("LOGI_CARRIER_NAME", "Гоголев Николай Николаевич")
    _carrier = db.query(_CP).filter(
        _CP.name.ilike(f"%{_carrier_name}%")
    ).first()
    _carrier_id = _carrier.id if _carrier else None

    def _orders_count(d_from, d_to):
        if not _carrier_id:
            return 0
        return db.query(func.count(Order.id)).filter(
            Order.date >= d_from,
            Order.date <= d_to,
            Order.carrier_id == _carrier_id,
        ).scalar() or 0

    def _per_order(logi, orders):
        return round(logi / orders, 2) if orders > 0 else None

    total_week       = round(_logi_sum(week_start, week_end)       * TAX, 2)
    total_month      = round(_logi_sum(month_start, month_end)     * TAX, 2)
    total_prev_month = round(_logi_sum(prev_month_start, prev_month_end) * TAX, 2)
    total_year       = round(_logi_sum(year_start, today)          * TAX, 2)

    # Выручка по периодам (оплаченные счета) — для расчёта доли логистики
    from app.models import Invoice as _Invoice

    def _revenue_sum(d_from, d_to):
        return db.query(func.sum(_Invoice.total_amount)).filter(
            _Invoice.date >= d_from,
            _Invoice.date <= d_to,
            _Invoice.status == "paid",
        ).scalar() or 0.0

    def _pct_of_revenue(logi, d_from, d_to):
        rev = _revenue_sum(d_from, d_to)
        return round(logi / rev * 100, 1) if rev > 0 else None

    pct_week       = _pct_of_revenue(total_week,       week_start, week_end)
    pct_month      = _pct_of_revenue(total_month,      month_start, month_end)
    pct_prev_month = _pct_of_revenue(total_prev_month, prev_month_start, prev_month_end)
    pct_year       = _pct_of_revenue(total_year,       year_start, today)

    orders_week       = _orders_count(week_start, week_end)
    orders_month      = _orders_count(month_start, month_end)
    orders_prev_month = _orders_count(prev_month_start, prev_month_end)
    orders_year       = _orders_count(year_start, today)

    company = db.query(CompanySettings).first()
    return templates.TemplateResponse(request, "reports/logistics.html", {
        "rows": rows, "total": total,
        # суммы по периодам
        "total_week":       total_week,
        "total_month":      total_month,
        "total_prev_month": total_prev_month,
        "total_year":       total_year,
        # логистика на 1 заказ
        "per_order_week":       _per_order(total_week,       orders_week),
        "per_order_month":      _per_order(total_month,      orders_month),
        "per_order_prev_month": _per_order(total_prev_month, orders_prev_month),
        "per_order_year":       _per_order(total_year,       orders_year),
        # доля логистики в выручке (%)
        "pct_week":       pct_week,
        "pct_month":      pct_month,
        "pct_prev_month": pct_prev_month,
        "pct_year":       pct_year,
        # кол-во заказов
        "orders_week":       orders_week,
        "orders_month":      orders_month,
        "orders_prev_month": orders_prev_month,
        "orders_year":       orders_year,
        # метки
        "week_start": week_start, "week_end": week_end,
        "prev_month_label": prev_month_start.strftime("%B %Y"),
        "period": period, "date_from": date_from, "date_to": date_to,
        "tbl_from": tbl_from, "tbl_to": tbl_to,
        "today": today,
        "metafora_url":  getattr(company, "metafora_url",  None) or METAFORA_URL,
        "metafora_email": getattr(company, "metafora_email", None) or "",
        "has_token": bool(getattr(company, "metafora_token", None)),
    })


# ── Ручное добавление ─────────────────────────────────────────────────────────

@router.post("/add")
@login_required
async def add_cost(
    request: Request,
    cost_date: str = Form(...),
    description: str = Form(default=""),
    amount: float = Form(...),
    notes: str = Form(default=""),
    db: Session = Depends(get_db),
):
    db.add(LogisticsCost(
        date=date.fromisoformat(cost_date),
        description=description, amount=amount,
        source="manual", notes=notes or None,
    ))
    db.commit()
    return RedirectResponse(url="/reports/logistics", status_code=302)


# ── Экспорт в Excel ───────────────────────────────────────────────────────────

@router.get("/export.xlsx")
@login_required
async def export_logistics(
    request: Request,
    period: str = "month", date_from: str = "", date_to: str = "",
    db: Session = Depends(get_db),
):
    from app.routers.reports import _xlsx_response
    today = date.today()
    month_start, month_end = _month_bounds(today)
    year_start = today.replace(month=1, day=1)
    week_start = today - timedelta(days=today.weekday())
    prev_month_last = month_start - timedelta(days=1)
    prev_month_start, prev_month_end = _month_bounds(prev_month_last)
    if period == "week":
        tbl_from, tbl_to = week_start, week_start + timedelta(days=6)
    elif period == "year":
        tbl_from, tbl_to = year_start, today
    elif period == "prev_month":
        tbl_from, tbl_to = prev_month_start, prev_month_end
    elif period == "custom" and date_from and date_to:
        try: tbl_from, tbl_to = date.fromisoformat(date_from), date.fromisoformat(date_to)
        except ValueError: tbl_from, tbl_to = month_start, today
    else:
        tbl_from, tbl_to = month_start, today

    TAX = 1.06
    raw = db.query(LogisticsCost).filter(
        LogisticsCost.date >= tbl_from, LogisticsCost.date <= tbl_to,
    ).order_by(LogisticsCost.date.desc()).all()
    src_label = {"metafora": "Метафора", "manual": "Вручную"}
    rows = [[
        r.date.strftime("%d.%m.%Y") if r.date else "",
        r.description or "",
        round((r.amount or 0) * TAX, 2),
        src_label.get(r.source, "Файл"),
        r.notes or "",
    ] for r in raw]
    fn = f"Логистика {tbl_from.strftime('%d.%m.%Y')}-{tbl_to.strftime('%d.%m.%Y')}.xlsx"
    return _xlsx_response(
        ["Дата", "Описание", "Сумма ₽ (с налогом)", "Источник", "Заметки"],
        rows, fn, widths=[14, 44, 20, 14, 30],
    )


# ── Удаление ──────────────────────────────────────────────────────────────────

@router.post("/{cost_id}/delete")
@login_required
async def delete_cost(request: Request, cost_id: int, db: Session = Depends(get_db)):
    row = db.query(LogisticsCost).filter(LogisticsCost.id == cost_id).first()
    if row:
        db.delete(row)
        db.commit()
    return RedirectResponse(url="/reports/logistics", status_code=302)


# ── Загрузка файла (Excel / CSV) ──────────────────────────────────────────────

@router.post("/upload")
@login_required
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    data = await file.read()
    fname = (file.filename or "").lower()
    try:
        if fname.endswith((".xlsx", ".xls")):
            rows = _parse_excel_bytes(data)
            src = "upload_excel"
        else:
            rows = _parse_csv_bytes(data)
            src = "upload_csv"
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    if not rows:
        return JSONResponse({"ok": False, "error": "Не удалось найти данные в файле. Нужны колонки: дата, сумма."}, status_code=400)

    added, skipped = _import_rows(db, rows, src)
    return JSONResponse({"ok": True, "added": added, "skipped": skipped})


# ══════════════════════════════════════════════════════════════════════════════
#  Авторизация в Метафоре (Glide) по PIN-коду из письма
# ══════════════════════════════════════════════════════════════════════════════

GLIDE_GW = "https://functions.prod.internal.glideapps.com"
DEFAULT_APP_ID = os.getenv("GLIDE_APP_ID", "c1zFt3H3s7tBVg16UEKR")
# Firebase API-ключ читается из .env
GLIDE_FB_KEY = os.getenv("GLIDE_FB_KEY", "")
FB_CUSTOM_URL  = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={GLIDE_FB_KEY}"
FB_REFRESH_URL = f"https://securetoken.googleapis.com/v1/token?key={GLIDE_FB_KEY}"


async def _resolve_app_id(client: httpx.AsyncClient, base_url: str) -> str:
    """Достаёт appID Glide-приложения из HTML. Фолбэк — известный ID."""
    import re
    try:
        host = base_url.split("/dl/")[0] if "/dl/" in base_url else base_url
        r = await client.get(host, timeout=15)
        m = re.search(r'appID["\s:=]{1,4}["\'`]([a-zA-Z0-9]{10,30})["\'`]', r.text)
        if m:
            return m.group(1)
    except Exception as e:
        logger.debug("Не удалось определить Glide appID: %s", e)
    return DEFAULT_APP_ID


def _company_url(company) -> str:
    return (getattr(company, "metafora_url", None) or METAFORA_URL).strip()


# ── Сохранение настроек (email + url) ─────────────────────────────────────────

@router.post("/save-settings")
@login_required
async def save_metafora_settings(
    request: Request,
    metafora_email: str = Form(default=""),
    metafora_url: str = Form(default=""),
    db: Session = Depends(get_db),
):
    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings()
        db.add(company)
    company.metafora_email = metafora_email.strip() or None
    company.metafora_url   = metafora_url.strip() or None
    # при смене настроек сбрасываем токены
    company.metafora_token = None
    company.metafora_refresh = None
    db.commit()
    return RedirectResponse(url="/reports/logistics/", status_code=302)


# ── Шаг 1: запросить PIN на почту ─────────────────────────────────────────────

@router.post("/request-pin")
@login_required
async def request_pin(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    email = email.strip().lower()
    if not email or "@" not in email:
        return JSONResponse({"ok": False, "error": "Введите корректный email"})

    company = db.query(CompanySettings).first()
    if not company:
        company = CompanySettings(); db.add(company)

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        app_id = await _resolve_app_id(client, _company_url(company))
        try:
            r = await client.post(
                f"{GLIDE_GW}/playerFunctionSmall/sendPinForEmail",
                json={"appID": app_id, "email": email},
            )
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"Сеть: {e}"})

    if r.status_code == 200:
        company.metafora_email = email
        company.metafora_app_id = app_id
        db.commit()
        return JSONResponse({"ok": True})
    if r.status_code == 401:
        return JSONResponse({"ok": False,
            "error": "Этот email не зарегистрирован в Метафоре (нет доступа к приложению)."})
    return JSONResponse({"ok": False, "error": f"Метафора вернула {r.status_code}: {r.text[:150]}"})


# ── Шаг 2: проверить PIN, получить и сохранить Firebase-токен ─────────────────

@router.post("/verify-pin")
@login_required
async def verify_pin(request: Request, pin: str = Form(...), db: Session = Depends(get_db)):
    pin = pin.strip()
    company = db.query(CompanySettings).first()
    email  = getattr(company, "metafora_email", None) if company else None
    app_id = getattr(company, "metafora_app_id", None) if company else None
    app_id = app_id or DEFAULT_APP_ID
    if not email:
        return JSONResponse({"ok": False, "error": "Сначала запросите PIN (шаг 1)."})

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        # 2.1 PIN → password
        try:
            r = await client.post(
                f"{GLIDE_GW}/playerFunctionCritical/getPasswordForEmailPin",
                json={"appID": app_id, "email": email, "pin": pin},
            )
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"Сеть: {e}"})
        if r.status_code != 200:
            return JSONResponse({"ok": False, "error": "Неверный или просроченный PIN."})
        try:
            password = r.json().get("password")
        except Exception:
            password = r.text.strip().strip('"')
        if not password:
            return JSONResponse({"ok": False, "error": "Метафора не вернула токен доступа."})

        # 2.2 password из Glide — это УЖЕ Firebase custom token, привязанный к email.
        # Напрямую обмениваем его на Firebase id-token (без getCustomTokenForApp).
        try:
            rf = await client.post(FB_CUSTOM_URL,
                                   json={"token": password, "returnSecureToken": True})
            if rf.status_code != 200:
                return JSONResponse({"ok": False,
                    "error": f"Firebase: {rf.json().get('error',{}).get('message', rf.text[:120])}"})
            fb = rf.json()
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"Ошибка Firebase: {e}"})

    company.metafora_token   = fb.get("idToken")
    company.metafora_refresh = fb.get("refreshToken")
    db.commit()
    return JSONResponse({"ok": True})


# ── Обновление id-токена по refresh-токену ────────────────────────────────────

async def _refresh_token(client: httpx.AsyncClient, refresh: str) -> str | None:
    try:
        r = await client.post(FB_REFRESH_URL,
                              data={"grant_type": "refresh_token", "refresh_token": refresh})
        if r.status_code == 200:
            return r.json().get("id_token") or r.json().get("access_token")
    except Exception as e:
        logger.warning("Не удалось обновить токен Метафоры: %s", e)
    return None


async def _fetch_with_token(client: httpx.AsyncClient, url: str, token: str) -> httpx.Response:
    """Скачивает URL с Firebase id-токеном."""
    last = None
    for headers, cookies in [
        ({"Authorization": f"Bearer {token}"}, {}),
        ({"Authorization": f"Bearer {token}"}, {"token": token}),
        ({}, {"glide-app-auth": token, "token": token}),
    ]:
        r = await client.get(url, headers=headers, cookies=cookies)
        last = r
        # Если это не HTML-шелл SPA — значит получили реальные данные
        if r.status_code == 200 and r.content[:4] == b"PK\x03\x04":
            return r  # Excel
        if r.status_code == 200 and r.content[:5] not in (b"<!DOC", b"<html"):
            ct = r.headers.get("content-type", "")
            if "json" in ct or "csv" in ct or "excel" in ct or "sheet" in ct:
                return r
    return last


FINANCE_TABLE = "native-table-KNWWpUgWlo9IgjaJ3EHH"  # Date/Debt/Credit/Sum
# Mapping обфусцированных колонок Glide → понятные имена (из схемы приложения)
# Input/Date=gya25, Input/Debt=oz0UW, Input/Credit=eWx6A, Input/Sum=?
_DEBT_KEYS   = {"oz0UW", "Input/Debt",   "debt",   "Debt",   "расход", "Расход"}
_CREDIT_KEYS = {"eWx6A", "Input/Credit", "credit", "Credit", "приход", "Приход"}
_DATE_KEYS   = {"gya25", "Input/Date",   "date",   "Date",   "дата",   "Дата"}
_DESC_KEYS   = {"Input/Comment", "Input/Order ID", "Input/Owner", "comment", "Comment"}


def _parse_finance_rows(rows: list) -> list[dict]:
    """Парсит строки финансовой таблицы Metafora."""
    result = []
    for row in rows:
        data = row.get("data", row) if isinstance(row, dict) else {}
        # Ищем дату
        row_date = date.today()
        for k in _DATE_KEYS:
            if k in data and data[k]:
                v = data[k]
                if isinstance(v, dict) and "value" in v:
                    # Glide datetime: {"kind":"glide-date-time","value":timestamp_ms}
                    try:
                        from datetime import datetime, timezone
                        row_date = datetime.fromtimestamp(v["value"] / 1000, tz=timezone.utc).date()
                        break
                    except Exception:
                        pass
                elif isinstance(v, str):
                    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y"):
                        try:
                            from datetime import datetime
                            row_date = datetime.strptime(v[:10], fmt).date(); break
                        except Exception:
                            pass
                    else:
                        continue
                    break
        # Ищем сумму расхода (Debt = расход = то что нас интересует)
        amount = None
        for k in _DEBT_KEYS:
            if k in data and data[k] not in (None, "", 0):
                try:
                    amount = abs(float(str(data[k]).replace(",", ".")))
                    if amount > 0:
                        break
                except Exception as e:
                    logger.debug("Glide snapshot: не удалось распарсить сумму: %s", e)
        if not amount:
            continue
        # Описание
        desc_parts = []
        for k in _DESC_KEYS:
            if k in data and data[k]:
                desc_parts.append(str(data[k]))
        desc = " | ".join(desc_parts[:2]) or "Метафора"
        result.append({"date": row_date, "description": desc,
                       "amount": amount, "external_id": row.get("id", "")})
    return result


async def _fetch_glide_api(client: httpx.AsyncClient, app_id: str, token: str) -> list[dict]:
    """Пробует получить данные через Glide getAppSnapshot и финансовую таблицу."""
    auth = {"Authorization": f"Bearer {token}"}
    try:
        r = await client.post(
            f"{GLIDE_GW}/playerFunctionCritical/getAppSnapshot",
            json={"appID": app_id, "nativeTableIDs": [FINANCE_TABLE]},
            headers=auth, timeout=30,
        )
        if r.status_code != 200:
            return []
        j = r.json()
        snaps = j.get("nativeTableSnapshots", {})
        # Скачиваем все снапшоты
        for tid, url in snaps.items():
            if not (isinstance(url, str) and url.startswith("http")):
                continue
            rs = await client.get(url)
            if rs.status_code == 200:
                rows = rs.json().get("rows", [])
                parsed = _parse_finance_rows(rows)
                if parsed:
                    return parsed
        # Пробуем dataSnapshot
        ds_url = j.get("dataSnapshot")
        if isinstance(ds_url, str) and ds_url.startswith("http"):
            rds = await client.get(ds_url)
            if rds.status_code == 200:
                return _parse_glide_snapshot(rds.json())
    except Exception as e:
        logger.warning("Синхронизация Glide не удалась: %s", e)
    return []


# ── Синхронизация ─────────────────────────────────────────────────────────────

@router.post("/sync-metafora")
@login_required
async def sync_metafora(request: Request, db: Session = Depends(get_db)):
    company = db.query(CompanySettings).first()
    token   = getattr(company, "metafora_token",   None) if company else None
    refresh = getattr(company, "metafora_refresh", None) if company else None
    app_id  = getattr(company, "metafora_app_id",  None) if company else None
    dl_url  = _company_url(company) if company else METAFORA_URL
    app_id  = app_id or DEFAULT_APP_ID

    if not token:
        return JSONResponse({"ok": False,
            "error": "Не авторизованы. Войдите по PIN-коду."})

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:

        # ── Шаг A: Glide API (getAppSnapshot) ────────────────────────────
        rows = await _fetch_glide_api(client, app_id, token)
        if rows:
            added, skipped = _import_rows(db, rows, "metafora")
            return JSONResponse({"ok": True, "added": added, "skipped": skipped})

        # ── Шаг B: Прямой GET dl/ ─────────────────────────────────────────
        resp = await _fetch_with_token(client, dl_url, token)
        data = resp.content
        ct   = resp.headers.get("content-type", "").lower()
        is_html = b"<!DOCTYPE" in data[:200] or b"<html" in data[:200].lower()

        # Токен истёк — пробуем refresh
        if resp.status_code in (401, 403) and refresh:
            new_tok = await _refresh_token(client, refresh)
            if new_tok:
                company.metafora_token = new_tok
                db.commit()
                resp = await _fetch_with_token(client, dl_url, new_tok)
                data = resp.content
                ct   = resp.headers.get("content-type", "").lower()
                is_html = b"<!DOCTYPE" in data[:200] or b"<html" in data[:200].lower()

    # ── Шаг C: Excel / CSV ────────────────────────────────────────────────
    if not is_html and resp.status_code == 200:
        try:
            if data[:4] == b"PK\x03\x04" or "excel" in ct or "spreadsheet" in ct:
                rows = _parse_excel_bytes(data)
            else:
                rows = _parse_csv_bytes(data)
            if rows:
                added, skipped = _import_rows(db, rows, "metafora")
                return JSONResponse({"ok": True, "added": added, "skipped": skipped})
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"Ошибка парсинга файла: {e}"})

    # ── Шаг D: данные внутри HTML (regex) ────────────────────────────────
    if is_html and resp.status_code == 200:
        rows = _parse_glide_html(data.decode("utf-8", errors="replace"))
        if rows:
            added, skipped = _import_rows(db, rows, "metafora")
            return JSONResponse({"ok": True, "added": added, "skipped": skipped})

    # ── Ничего не подошло — нужна диагностика ────────────────────────────
    if resp.status_code in (401, 403) or (is_html and len(data) < 10000):
        company.metafora_token = None
        db.commit()
        return JSONResponse({"ok": False,
            "error": "Сессия истекла — войдите по PIN-коду заново."})

    # Возвращаем диагностику чтобы понять формат
    preview = data[:200].decode("utf-8", errors="replace").replace("\n", " ")
    return JSONResponse({"ok": False,
        "error": f"Данные получены, но формат не распознан. "
                 f"status={resp.status_code}, {len(data)} байт, ct={ct}. "
                 f"Начало: {preview[:100]}"
    })


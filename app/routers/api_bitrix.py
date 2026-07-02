"""
Bitrix24 CRM — приём сделок (push из Bitrix24) + вспомогательные эндпоинты
для настройки маппинга стадий/полей в Настройки → Интеграции → Bitrix24.

POST /api/bitrix/webhook/deal-approved  — сделка попала на стадию «Заказ согласован»:
                                           создаёт/обновляет контрагента и черновик заказа,
                                           поднимает громкое уведомление менеджеру.
GET  /api/bitrix/test                   — проверка вебхука (admin)
GET  /api/bitrix/categories              — список направлений (воронок) сделок (admin)
GET  /api/bitrix/stages                  — список стадий сделки для направления (admin)
POST /api/bitrix/ensure-userfields       — создать UF-поля «Оплачено»/«Доставлено» (admin)

Авторизация вебхука Bitrix24 → TMS — статический ключ в query-параметре ?key=
(значение из env BITRIX_PUSH_KEY), аналогично приёму документов из 1С.
"""
import logging
import os
import secrets

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth import role_required
from app.database import get_db
from app.models import (
    Counterparty, Order, OrderItem, Contract, Product, CompanySettings, Notification, BitrixPipeline,
)
from app.services.bitrix_client import (
    BitrixError, get_bitrix_client, extract_counterparty_data, enrich_from_dadata,
)
from app.utils import log_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bitrix", tags=["api_bitrix"])

_CP_FIELDS = {
    "name", "trade_name", "inn", "kpp", "ogrn", "phone", "email", "entity_type",
    "external_id_bitrix", "bank_name", "bank_bik", "bank_account", "bank_corr_account",
    "legal_address", "short_name", "signatory",
}


def _json(ok: bool, **kw):
    return JSONResponse({"ok": ok, **kw})


def _check_key(request: Request) -> bool:
    expected = os.environ.get("BITRIX_PUSH_KEY", "")
    if not expected:
        return False
    got = request.query_params.get("key", "")
    return bool(got) and secrets.compare_digest(got, expected)


def _next_order_number(db: Session) -> str:
    from app.routers.orders import _next_order_number as _f
    return _f(db)


def _next_contract_number(db: Session) -> str:
    from app.routers.contracts import _next_contract_number as _f
    return _f(db)


# ── Приём сделки из Bitrix24 ────────────────────────────────────────────────

@router.post("/webhook/deal-approved")
async def deal_approved(request: Request, db: Session = Depends(get_db)):
    if not os.environ.get("BITRIX_PUSH_KEY"):
        return JSONResponse({"ok": False, "error": "BITRIX_PUSH_KEY не задан на сервере"}, status_code=503)
    if not _check_key(request):
        return JSONResponse({"ok": False, "error": "Неверный или отсутствующий key"}, status_code=401)

    deal_id = request.query_params.get("deal_id", "").strip()
    if not deal_id:
        # Робот в автоматизации Bitrix24 может передать ID и в теле формы
        form = await request.form()
        deal_id = (form.get("deal_id") or form.get("document_id") or "").strip()
    if not deal_id:
        return JSONResponse({"ok": False, "error": "Не передан deal_id"}, status_code=400)

    company = db.query(CompanySettings).first()
    client = get_bitrix_client(company)
    if not client:
        return JSONResponse({"ok": False, "error": "Bitrix24 не настроен или выключен в Настройках"},
                            status_code=503)

    # Идемпотентность: сделка уже приводила к созданию заказа — не дублируем
    existing_order = db.query(Order).filter(Order.bitrix_deal_id == deal_id).first()
    if existing_order:
        return _json(True, order_id=existing_order.id, order_number=existing_order.number,
                     message="Заказ по этой сделке уже создан ранее")

    try:
        with client:
            deal = client.get_deal(deal_id)
            if not deal:
                return _json(False, error=f"Сделка {deal_id} не найдена в Bitrix24")

            cp_data = extract_counterparty_data(client, deal)
            products = []
            try:
                products = client.get_deal_products(deal_id)
            except BitrixError as e:
                logger.warning("Bitrix24: не удалось получить товары сделки %s: %s", deal_id, e)
    except BitrixError as e:
        logger.error("Bitrix24 webhook deal-approved %s: %s", deal_id, e)
        return _json(False, error=str(e))

    if not cp_data:
        return _json(False, error="У сделки не указана ни компания, ни контакт — контрагента создать не из чего")

    # ── Контрагент: ищем по external_id_bitrix, затем по ИНН ────────────────
    cp = db.query(Counterparty).filter(
        Counterparty.external_id_bitrix == cp_data["external_id_bitrix"]
    ).first()
    is_new_counterparty = False
    if not cp and cp_data.get("inn"):
        cp = db.query(Counterparty).filter(Counterparty.inn == cp_data["inn"]).first()
    if not cp:
        is_new_counterparty = True
        cp_data = enrich_from_dadata(cp_data)
        cp = Counterparty(**{k: v for k, v in cp_data.items() if k in _CP_FIELDS})
        db.add(cp)
        db.flush()
    else:
        if not cp.external_id_bitrix:
            cp.external_id_bitrix = cp_data["external_id_bitrix"]
        if not cp.is_active:
            # Найденный по ИНН/привязке контрагент был деактивирован (архивирован
            # вручную) — новая сделка из Bitrix24 означает, что он снова активен.
            cp.is_active = True
            log_action(db, "counterparty", cp.id, "updated", None,
                       "Контрагент реактивирован — новая сделка из Bitrix24")

    is_primary_sale = is_new_counterparty or db.query(Order.id).filter(
        Order.counterparty_id == cp.id
    ).first() is None

    # ── Черновик заказа ───────────────────────────────────────────────────
    from datetime import date as _date
    title = deal.get("TITLE") or f"Сделка Bitrix24 #{deal_id}"
    opportunity = deal.get("OPPORTUNITY") or "0"
    comments = (deal.get("COMMENTS") or "").strip()
    notes = f"Из Bitrix24: «{title}», сумма сделки {opportunity} {deal.get('CURRENCY_ID', '')}."
    if comments:
        notes += f"\n{comments}"

    try:
        category_id = int(deal.get("CATEGORY_ID")) if deal.get("CATEGORY_ID") is not None else None
    except (TypeError, ValueError):
        category_id = None

    order = Order(
        number=_next_order_number(db),
        date=_date.today(),
        counterparty_id=cp.id,
        status="draft",
        payment_type="prepay",
        notes=notes,
        bitrix_deal_id=deal_id,
        bitrix_category_id=category_id,
    )
    db.add(order)
    db.flush()

    matched, unmatched = [], []
    for row in products:
        pname = (row.get("PRODUCT_NAME") or "").strip()
        if not pname:
            continue
        product = db.query(Product).filter(Product.name.ilike(pname)).first()
        if not product:
            unmatched.append(pname)
            continue
        qty = float(row.get("QUANTITY") or 0)
        price = float(row.get("PRICE") or 0)
        db.add(OrderItem(
            order_id=order.id, product_id=product.id, quantity=qty, price=price,
            vat_rate=product.vat_rate, amount=round(qty * price, 2),
        ))
        matched.append(pname)
    if unmatched:
        order.notes += "\n\nНе удалось сопоставить товарные позиции (добавьте вручную): " + ", ".join(unmatched)

    contract = None
    if is_primary_sale:
        contract = Contract(
            number=_next_contract_number(db),
            date=_date.today(),
            counterparty_id=cp.id,
            subject=f"Договор поставки — {cp.trade_name or cp.name}",
            status="draft",
            payment_type="prepay",
            notes=f"Черновик создан автоматически: первая сделка с контрагентом из Bitrix24 (#{deal_id}).",
        )
        db.add(contract)

    log_action(db, "order", order.id, "created", None,
               f"Заказ создан из Bitrix24 (сделка #{deal_id})")

    # ── Громкое уведомление менеджеру ────────────────────────────────────────
    actions = ["Счёт", "УПД"]
    if is_primary_sale:
        actions.append("Договор")
    body = (
        f"Контрагент: {cp.trade_name or cp.name}. Сумма сделки: {opportunity} ₽.\n"
        f"Нужно создать: {' + '.join(actions)}."
    )
    if unmatched:
        body += f"\nПроверьте товарные позиции — часть не сопоставлена автоматически."
    notif_title = f"🔴 Новый заказ из Bitrix24 — «{title}»"
    notify_ids = [uid.strip() for uid in (company.bitrix_notify_user_ids or "").split(",") if uid.strip()]
    if notify_ids:
        for uid in notify_ids:
            db.add(Notification(type="bitrix_order", title=notif_title, body=body,
                                 link=f"/orders/{order.id}", user_id=int(uid)))
    else:
        # Настройка не задана — уведомление системное, видят все пользователи TMS
        db.add(Notification(type="bitrix_order", title=notif_title, body=body,
                             link=f"/orders/{order.id}"))
    db.commit()

    await _send_bitrix_alert(company, order, cp, actions)

    return _json(True, order_id=order.id, order_number=order.number, counterparty_id=cp.id,
                 is_primary_sale=is_primary_sale, contract_id=contract.id if contract else None)


async def _send_bitrix_alert(company, order, cp, actions) -> None:
    """Немедленная Telegram-рассылка о новом заказе — параллельно с уведомлением в TMS."""
    if not company:
        return
    chat_ids = [c.strip() for c in (company.bitrix_alert_chat_ids or "").split(",") if c.strip()]
    bot_token = (company.tg_bot_token or "").strip() or os.getenv("TMS_BOT_TOKEN", "").strip()
    if not chat_ids or not bot_token:
        return
    text = (
        f"🔴 НОВЫЙ ЗАКАЗ ИЗ BITRIX24\n"
        f"Заказ №{order.number} — {cp.trade_name or cp.name}\n"
        f"Нужно создать: {' + '.join(actions)}\n"
        f"Открыть: смотрите вкладку «Уведомления» в TMS"
    )
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            for chat_id in chat_ids:
                await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                )
    except Exception as e:
        logger.warning("Bitrix24: не удалось отправить Telegram-алерт: %s", e)


# ── Настройка: проверка соединения, стадии, UF-поля ──────────────────────────

@router.get("/test")
@role_required("admin")
async def test_connection(request: Request, db: Session = Depends(get_db)):
    company = db.query(CompanySettings).first()
    client = get_bitrix_client(company)
    if not client:
        return _json(False, message="Bitrix24 не настроен — укажите URL вебхука и включите синхронизацию")
    try:
        with client:
            profile = client.call("profile")
        return _json(True, message=f"Подключено: {profile.get('NAME', '')} {profile.get('LAST_NAME', '')}".strip())
    except BitrixError as e:
        return _json(False, message=str(e))


@router.get("/categories")
@role_required("admin")
async def categories(request: Request, db: Session = Depends(get_db)):
    company = db.query(CompanySettings).first()
    client = get_bitrix_client(company)
    if not client:
        return _json(False, error="Bitrix24 не настроен")
    with client:
        cats = client.list_categories()
    result = [{"id": c.get("id"), "name": c.get("name")} for c in cats]
    return _json(True, categories=result)


@router.get("/stages")
@role_required("admin")
async def stages(request: Request, category_id: int = 0, db: Session = Depends(get_db)):
    company = db.query(CompanySettings).first()
    client = get_bitrix_client(company)
    if not client:
        return _json(False, error="Bitrix24 не настроен")
    try:
        with client:
            result = client.list_stages(category_id)
        return _json(True, stages=result)
    except BitrixError as e:
        return _json(False, error=str(e))


@router.post("/ensure-userfields")
@role_required("admin")
async def ensure_userfields(request: Request, db: Session = Depends(get_db)):
    company = db.query(CompanySettings).first()
    client = get_bitrix_client(company)
    if not client:
        return _json(False, error="Bitrix24 не настроен")
    try:
        with client:
            paid_code = client.ensure_userfield("TMS_PAID", "Оплачено (TMS)")
            delivered_code = client.ensure_userfield("TMS_DELIVERED", "Доставлено (TMS)")
    except BitrixError as e:
        return _json(False, error=str(e))
    company.bitrix_field_paid = paid_code
    company.bitrix_field_delivered = delivered_code
    db.commit()
    return _json(True, field_paid=paid_code, field_delivered=delivered_code)


# ── Маппинг направлений (воронок) — для сценариев вроде «Вторичных продаж»,
#    у которых свой набор стадий и часть событий (напр. «Отгрузка») не нужна ──

@router.get("/pipelines")
@role_required("admin")
async def list_pipelines(request: Request, db: Session = Depends(get_db)):
    rows = db.query(BitrixPipeline).order_by(BitrixPipeline.id).all()
    return _json(True, pipelines=[{
        "id": p.id, "category_id": p.category_id, "name": p.name,
        "stage_paid": p.stage_paid, "stage_shipped": p.stage_shipped,
        "stage_delivered": p.stage_delivered,
    } for p in rows])


@router.post("/pipelines")
@role_required("admin")
async def upsert_pipeline(
    request: Request,
    category_id: int = Form(...),
    name: str = Form(default=""),
    stage_paid: str = Form(default=""),
    stage_shipped: str = Form(default=""),
    stage_delivered: str = Form(default=""),
    db: Session = Depends(get_db),
):
    row = db.query(BitrixPipeline).filter(BitrixPipeline.category_id == category_id).first()
    if not row:
        row = BitrixPipeline(category_id=category_id)
        db.add(row)
    row.name = name.strip() or None
    row.stage_paid = stage_paid.strip() or None
    row.stage_shipped = stage_shipped.strip() or None
    row.stage_delivered = stage_delivered.strip() or None
    db.commit()
    return _json(True, id=row.id)


@router.post("/pipelines/{pipeline_id}/delete")
@role_required("admin")
async def delete_pipeline(request: Request, pipeline_id: int, db: Session = Depends(get_db)):
    row = db.query(BitrixPipeline).filter(BitrixPipeline.id == pipeline_id).first()
    if row:
        db.delete(row)
        db.commit()
    return _json(True)

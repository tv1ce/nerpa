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
    BitrixProductLink,
)
from app.services.bitrix_client import (
    BitrixError, get_bitrix_client, extract_counterparty_data, enrich_from_dadata,
    refresh_counterparty_requisites, extract_delivery_from_deal, normalize_product_name,
)
from app.utils import log_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bitrix", tags=["api_bitrix"])

_CP_FIELDS = {
    "name", "trade_name", "inn", "kpp", "ogrn", "phone", "email", "entity_type",
    "external_id_bitrix", "bank_name", "bank_bik", "bank_account", "bank_corr_account",
    "legal_address", "actual_address", "short_name", "signatory",
}


def _json(ok: bool, **kw):
    return JSONResponse({"ok": ok, **kw})


# ── Сопоставление товарных позиций сделки с номенклатурой TMS ─────────────
# Название товара в Bitrix24 часто не совпадает буква-в-букву с названием в
# TMS/1С, поэтому одного точного сравнения строк недостаточно (см. историю
# случая, когда позиция «не подсосалась» в заказ). Порядок попыток:
#   1. Уже запомненная привязка PRODUCT_ID → товар TMS (bitrix_product_links).
#   2. Код/артикул из карточки товара Bitrix (XML_ID) против article/external_id_1c.
#   3. Точное совпадение по нормализованному названию (регистр/пробелы/пунктуация).
# Как только (2) или (3) сработали — привязка сохраняется, и в следующий раз
# тот же товар Bitrix матчится мгновенно через (1), даже если имя никогда не
# приведут к единому виду в CRM.
# Нормализация имени живёт в bitrix_client: тем же правилом каталог CRM
# сопоставляется с номенклатурой при выгрузке остатков — товар должен
# матчиться одинаково с обеих сторон.
_normalize_product_name = normalize_product_name


def _build_product_name_index(db: Session) -> dict:
    """{нормализованное имя: Product}, без неоднозначных совпадений (когда двум
    разным товарам TMS соответствует одно и то же нормализованное имя)."""
    idx, ambiguous = {}, set()
    for p in db.query(Product).filter(Product.is_active == True).all():  # noqa: E712
        norm = _normalize_product_name(p.name)
        if not norm:
            continue
        if norm in idx and idx[norm].id != p.id:
            ambiguous.add(norm)
        else:
            idx[norm] = p
    for norm in ambiguous:
        idx.pop(norm, None)
    return idx


def _match_bitrix_product(db: Session, row: dict, name_index: dict, product_codes: dict):
    bitrix_pid = str(row.get("PRODUCT_ID") or "").strip()
    pname = (row.get("PRODUCT_NAME") or "").strip()

    if bitrix_pid:
        link = db.query(BitrixProductLink).filter(
            BitrixProductLink.bitrix_product_id == bitrix_pid).first()
        if link:
            return link.product

    product = None
    code = (product_codes.get(bitrix_pid) if bitrix_pid else None) or ""
    if code:
        product = (db.query(Product).filter(Product.external_id_1c == code).first()
                   or db.query(Product).filter(Product.article == code).first())

    if not product:
        product = name_index.get(_normalize_product_name(pname))

    if product and bitrix_pid:
        db.add(BitrixProductLink(bitrix_product_id=bitrix_pid, product_id=product.id,
                                  bitrix_product_name=pname))

    return product


def _price_and_discount(row: dict) -> tuple:
    """(цена до скидки, % скидки) из товарной строки сделки.

    PRICE в Bitrix24 — это цена УЖЕ со скидкой, поэтому позиция со 100% скидкой
    (подарок, дегустационный образец) приезжала в TMS с нулевой ценой и без следа
    того, что скидка вообще была. Цена до скидки лежит в PRICE_BRUTTO (с налогом)
    или PRICE_NETTO (без него) — какое из полей соответствует PRICE, говорит флаг
    TAX_INCLUDED.

    Процент считаем из самих цен, а не из DISCOUNT_RATE: скидка бывает и
    абсолютной (DISCOUNT_TYPE_ID = 1), и тогда DISCOUNT_RATE приходит нулевым.
    """
    final = float(row.get("PRICE") or 0)
    tax_included = str(row.get("TAX_INCLUDED") or "Y").upper() != "N"
    base = float(row.get("PRICE_BRUTTO" if tax_included else "PRICE_NETTO") or 0)

    # Цена до скидки не заполнена или противоречит итоговой — берём как есть.
    if base <= 0 or base < final:
        return final, 0.0

    return base, round((1 - final / base) * 100, 2)


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

    # Идемпотентность: сделка уже приводила к созданию заказа — не дублируем.
    # Но реквизиты контрагента могли заполниться в CRM уже ПОСЛЕ первого вебхука
    # (менеджер вписал ИНН/банк позже) — при повторном пуше робота на ту же
    # стадию дозаливаем пустые поля из свежих данных Bitrix + DaData.
    existing_order = db.query(Order).filter(Order.bitrix_deal_id == deal_id).first()
    if existing_order:
        cp = existing_order.counterparty
        if cp and cp.external_id_bitrix:
            try:
                from datetime import datetime as _dt
                with client:
                    if refresh_counterparty_requisites(client, cp):
                        cp.synced_to_bitrix_at = _dt.now()
                        db.commit()
            except BitrixError as e:
                logger.warning("Bitrix24: дозаливка реквизитов при повторном пуше сделки %s: %s", deal_id, e)
        return _json(True, order_id=existing_order.id, order_number=existing_order.number,
                     message="Заказ по этой сделке уже создан ранее")

    try:
        with client:
            deal = client.get_deal(deal_id)
            if not deal:
                return _json(False, error=f"Сделка {deal_id} не найдена в Bitrix24")

            cp_data = extract_counterparty_data(client, deal)
            delivery = extract_delivery_from_deal(client, deal)
            products = []
            try:
                products = client.get_deal_products(deal_id)
            except BitrixError as e:
                logger.warning("Bitrix24: не удалось получить товары сделки %s: %s", deal_id, e)

            # Код/артикул (XML_ID) карточек товаров — для сопоставления по (2),
            # пока HTTP-сессия клиента ещё открыта. Не критично, если не удалось:
            # сопоставление просто откатится на имя.
            product_codes = {}
            for row in products:
                pid = str(row.get("PRODUCT_ID") or "").strip()
                if not pid or pid in product_codes:
                    continue
                try:
                    card = client.get_product(pid)
                    product_codes[pid] = (card.get("XML_ID") or "").strip()
                except BitrixError as e:
                    logger.warning("Bitrix24: не удалось получить карточку товара %s (сделка %s): %s",
                                   pid, deal_id, e)
                    product_codes[pid] = ""
    except BitrixError as e:
        logger.error("Bitrix24 webhook deal-approved %s: %s", deal_id, e)
        return _json(False, error=str(e))

    if not cp_data:
        return _json(False, error="У сделки не указана ни компания, ни контакт — контрагента создать не из чего")

    # Реквизиты, заполненные на самой сделке, — запасной источник для тех полей,
    # которые в карточке компании ещё пусты. Делаем это ДО поиска контрагента по
    # ИНН: иначе сделка с ИНН только в своём поле заведёт дубль контрагента.
    for _field in ("inn", "bank_bik", "bank_account"):
        if delivery.get(_field) and not cp_data.get(_field):
            cp_data[_field] = delivery[_field]

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
        # Дозаполняем ПУСТЫЕ реквизиты свежими данными из сделки. Bitrix24 —
        # источник истины там, где данные есть; введённое в TMS не затираем.
        for field in ("inn", "kpp", "ogrn", "phone", "email", "actual_address", "legal_address",
                      "bank_name", "bank_bik", "bank_account", "bank_corr_account"):
            val = cp_data.get(field)
            if val and not getattr(cp, field, None):
                setattr(cp, field, val)
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
        # Адрес доставки — поле «Адрес доставки» сделки (у клиента может быть
        # несколько точек), иначе фактический адрес компании из CRM.
        #
        # Намеренно НЕ откатываемся на cp.actual_address: у контрагентов,
        # заполненных кнопкой «по ИНН», он скопирован с юридического адреса, и
        # заказ уезжал на юр.адрес фирмы вместо точки. Пустой адрес логист
        # заметит и уточнит, подменённый — нет.
        delivery_address=(delivery.get("delivery_address")
                          or cp_data.get("actual_address") or None),
        delivery_contact=delivery.get("delivery_contact"),
        delivery_date=delivery.get("delivery_date"),
    )
    db.add(order)
    db.flush()

    matched, unmatched = [], []
    name_index = _build_product_name_index(db)
    for row in products:
        pname = (row.get("PRODUCT_NAME") or "").strip()
        if not pname:
            continue
        qty = float(row.get("QUANTITY") or 0)
        price, discount_pct = _price_and_discount(row)
        product = _match_bitrix_product(db, row, name_index, product_codes)
        if not product:
            # Сохраняем QTY/PRICE прямо в тексте — иначе при ручном добавлении
            # позиции менеджер вынужден гадать количество (товар мог появиться
            # в каталоге TMS уже ПОСЛЕ пуша сделки, минуты решают).
            qty_s = f"{qty:g}"
            price_s = f"{price:g}"
            disc_s = f" со скидкой {discount_pct:g}%" if discount_pct else ""
            unmatched.append(f"{pname} — {qty_s} шт. по {price_s} ₽{disc_s}")
            continue
        db.add(OrderItem(
            order_id=order.id, product_id=product.id, quantity=qty, price=price,
            discount_pct=discount_pct, vat_rate=product.vat_rate,
            amount=round(qty * price * (1 - discount_pct / 100), 2),
        ))
        matched.append(pname)
    if unmatched:
        order.notes += "\n\nНе удалось сопоставить товарные позиции (добавьте вручную): " + "; ".join(unmatched)

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
    # Через общий отправитель (SOCKS-прокси TMS_PROXY): напрямую
    # api.telegram.org с российского сервера недоступен. Отправка блокирующая,
    # поэтому уводим её в поток, чтобы не держать event loop.
    import asyncio
    from app.services.telegram_send import send_topic_message

    def _send():
        for chat_id in chat_ids:
            send_topic_message(int(chat_id), text, bot_token)

    try:
        await asyncio.to_thread(_send)
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

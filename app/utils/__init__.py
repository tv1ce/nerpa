from datetime import date, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import func


def add_banking_days(start: date, n: int) -> date:
    """Прибавляет n банковских (рабочих, пн–пт) дней к дате."""
    d = start
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:   # 0=пн … 4=пт
            added += 1
    return d


def compute_invoice_due_date(inv_date: date, contract, counterparty) -> date | None:
    """Срок оплаты счёта — по отсрочке договора, иначе по условиям оплаты контрагента.

    Договор «с отсрочкой» (payment_type=deferred) даёт payment_days календарных дней
    от даты счёта. Договор «по предоплате» — оплата в день выставления счёта.
    Без договора (или без указанных дней отсрочки) — используем условия оплаты
    контрагента (payment_delay_days/payment_delay_type, банковские либо календарные дни).
    """
    if not inv_date:
        return None
    if contract and contract.payment_type == "deferred" and contract.payment_days:
        return inv_date + timedelta(days=contract.payment_days)
    if contract and contract.payment_type == "prepay":
        return inv_date
    delay = (counterparty.payment_delay_days or 0) if counterparty else 0
    if not delay:
        return inv_date
    dtype = (counterparty.payment_delay_type or "banking") if counterparty else "banking"
    if dtype == "banking":
        return add_banking_days(inv_date, delay)
    return inv_date + timedelta(days=delay)


def log_action(
    db: Session,
    entity_type: str,
    entity_id: int,
    action: str,
    user_id: int,
    note: str,
    field: str = None,
    old_value: str = None,
    new_value: str = None,
) -> None:
    from app.models import AuditLog
    db.add(AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        field=field,
        old_value=old_value,
        new_value=new_value,
        note=note,
        user_id=user_id,
    ))


def sync_order_paid_status(db: Session, order, user_id: int = None) -> bool:
    """Подтягивает статус заказа по оплате связанных счетов.

    Если заказ по предоплате и его привязанные счета полностью оплачены — переводит
    заказ в «Оплачен» (с этого статуса он падает кладовщику на сборку). Только
    продвигает вперёд: заказы в «Собран»/«Передан»/«Доставлен»/«Отменён» не трогаем.
    Для заказов с отсрочкой шага «Оплачен» в цикле нет — статус не меняем.
    Возвращает True, если статус был изменён.
    """
    if not order or not order.is_prepay:
        return False
    if order.status not in ("draft", "confirmed"):
        return False
    invoices = [i for i in order.invoices if i.status != "cancelled"]
    if not invoices:
        return False
    order_total = order.total_amount or 0
    paid_total = sum((i.total_amount or 0) for i in invoices if i.status == "paid")
    if order_total > 0:
        is_paid = paid_total + 0.01 >= order_total
    else:
        is_paid = any(i.status == "paid" for i in invoices)
    if not is_paid:
        return False
    old = order.status
    order.status = "paid"
    log_action(db, "order", order.id, "status_changed", user_id,
               "Заказ переведён в «Оплачен» по оплате счёта",
               field="status", old_value=old, new_value="paid")
    return True


# ── Приём оплат счетов (Точка / 1С) ──────────────────────────────────────────
# Единый журнал `payments` — источник истины для суммы оплаты, частичной оплаты и
# дедупа между источниками. Всё проходит через apply_payment → recompute_invoice_payment.

import re as _re

# Номер счёта из назначения платежа: «оплата счёта №35», «счет № 123»,
# «сч. на оплату 77», «по сч. 60 от 20.06.2026».
#
# Сокращение «сч.» — без буквы «т» — встречается в выписках не реже полного
# слова, поэтому окончание целиком необязательное. Граница \b слева отсекает
# «раСЧетный», а (?!\d) справа — длинные числа вроде 20-значного р/счёта,
# у которого иначе откусывалось бы первые 7 цифр.
_INV_NUM_RE = _re.compile(
    r"\bсч(?:[её]т\w*)?\s*\.?\s*(?:на\s+оплату\s*)?(?:№|N|#)?\s*(\d{1,7})(?!\d)",
    _re.IGNORECASE,
)
_ANY_NUM_RE = _re.compile(r"№\s*(\d{1,7})(?!\d)")


def _extract_invoice_numbers(purpose: str) -> list[str]:
    """Достаёт номера счетов из назначения платежа (сначала после слова «счёт»)."""
    if not purpose:
        return []
    nums = _INV_NUM_RE.findall(purpose)
    if not nums:
        nums = _ANY_NUM_RE.findall(purpose)
    return nums


def match_invoice_for_payment(db: Session, payer_inn: str, amount: float,
                              purpose: str, on_date: date):
    """Подбирает счёт ТМС для входящего платежа.

    Сначала — по № счёта из назначения платежа, затем — по сумме; в рамках
    контрагента(ов) с этим ИНН, среди неоплаченных/частично оплаченных счетов за
    последний месяц. Возвращает Invoice или None (не найдено / неоднозначно)."""
    from app.models import Invoice, Counterparty
    if not payer_inn:
        return None
    cp_ids = [r[0] for r in db.query(Counterparty.id)
              .filter(Counterparty.inn == payer_inn).all()]
    if not cp_ids:
        return None
    cutoff = (on_date or date.today()) - timedelta(days=31)
    candidates = (db.query(Invoice)
                  .filter(Invoice.counterparty_id.in_(cp_ids),
                          Invoice.status.in_(["issued", "overdue", "partial"]),
                          Invoice.date >= cutoff)
                  .order_by(Invoice.date.desc())
                  .all())
    if not candidates:
        return None
    # (а) по номеру счёта из назначения платежа — сначала точная строка, затем по
    # цифровому хвосту (в ТМС номера часто с префиксом: «НФНФ-000056» в назначении
    # платежа указывают как просто «56»).
    for num in _extract_invoice_numbers(purpose):
        for inv in candidates:
            if str(inv.number).strip() == num:
                return inv
        num_digits = num.lstrip("0") or "0"
        for inv in candidates:
            inv_digits = _re.sub(r"\D", "", str(inv.number or "")).lstrip("0") or "0"
            if inv_digits == num_digits:
                return inv
    # (б) по сумме — полная сумма счёта либо непокрытый остаток; только при однозначности
    amt = round(float(amount or 0), 2)
    by_amount = [inv for inv in candidates
                 if abs(round(inv.total_amount or 0, 2) - amt) < 0.01
                 or abs(round((inv.total_amount or 0) - (inv.paid_amount or 0), 2) - amt) < 0.01]
    return by_amount[0] if len(by_amount) == 1 else None


def recompute_invoice_payment(db: Session, invoice, user_id: int = None) -> None:
    """Пересчитывает paid_amount/статус счёта по журналу платежей.

    Сумма поплатежей ≥ итога → «Оплачен» (+ каскад на заказ/Bitrix при первом
    переходе). Есть платежи, но меньше итога → «Частично оплачено». Нет платежей —
    возвращаем «Выставлен». Статусы draft/cancelled не трогаем."""
    from app.models import Payment
    if not invoice or invoice.status in ("cancelled", "draft"):
        return
    paid = db.query(func.coalesce(func.sum(Payment.amount), 0.0)) \
             .filter(Payment.invoice_id == invoice.id).scalar() or 0.0
    paid = round(paid, 2)
    invoice.paid_amount = paid
    total = round(invoice.total_amount or 0, 2)
    was_paid = invoice.status == "paid"

    if total > 0 and paid + 0.01 >= total:
        invoice.status = "paid"
        if invoice.paid_date is None:
            last = db.query(func.max(Payment.date)) \
                     .filter(Payment.invoice_id == invoice.id).scalar()
            invoice.paid_date = last or date.today()
        if not was_paid:
            _on_invoice_paid(db, invoice, user_id)
    elif paid > 0:
        invoice.status = "partial"
        invoice.paid_date = None
    else:
        if invoice.status in ("paid", "partial"):
            invoice.status = "issued"
        invoice.paid_date = None


def _on_invoice_paid(db: Session, invoice, user_id: int = None) -> None:
    """Каскад при полной оплате счёта: заказ-предоплата → «Оплачен», push в Bitrix.
    Ошибки внешних систем не должны срывать проведение платежа — гасим их."""
    if not invoice.order:
        return
    try:
        sync_order_paid_status(db, invoice.order, user_id)
    except Exception:
        pass
    if getattr(invoice.order, "bitrix_deal_id", None):
        try:
            from app.models import CompanySettings
            from app.services.bitrix_client import push_order_event
            company = db.query(CompanySettings).first()
            if company:
                push_order_event(invoice.order, company, "paid", db=db)
        except Exception:
            pass


def apply_payment(db: Session, *, invoice, amount, pay_date, source: str,
                  external_id: str = None, purpose: str = "",
                  payer_inn: str = "", payer_name: str = "",
                  counterparty_id: int = None, user_id: int = None) -> bool:
    """Проводит банковское поступление в журнал `payments` и пересчитывает счёт.

    Идемпотентно по (source, external_id). Кросс-дедуп «Точка ↔ 1С»: та же оплата
    (та же сумма ±0.01 и дата ±3 дня) по этому счёту из другого источника не заводится
    повторно. invoice=None → платёж сохраняется непривязанным (для ручного разбора).
    Возвращает True, если создана новая запись."""
    from app.models import Payment
    amount = round(float(amount or 0), 2)
    if amount <= 0:
        return False
    pay_date = pay_date or date.today()

    # 1. Идемпотентность приёма
    if external_id:
        if db.query(Payment).filter(Payment.source == source,
                                    Payment.external_id == external_id).first():
            return False
    # 2. Кросс-дедуп с другим источником (по этому счёту)
    if invoice is not None:
        others = db.query(Payment).filter(Payment.invoice_id == invoice.id,
                                          Payment.source != source).all()
        for p in others:
            if (abs((p.amount or 0) - amount) < 0.01 and p.date
                    and abs((p.date - pay_date).days) <= 3):
                return False

    db.add(Payment(
        invoice_id=(invoice.id if invoice is not None else None),
        counterparty_id=(invoice.counterparty_id if invoice is not None else counterparty_id),
        amount=amount, date=pay_date,
        purpose=(purpose or None), payer_inn=(payer_inn or "")[:12] or None,
        payer_name=(payer_name or "")[:200] or None,
        source=source, external_id=(external_id or None),
    ))
    db.flush()
    if invoice is not None:
        recompute_invoice_payment(db, invoice, user_id)
    return True


def get_balance(db: Session, product_id: int) -> float:
    from app.models import Product, StockMovement
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        return 0.0
    in_qty = db.query(func.sum(StockMovement.quantity)).filter(
        StockMovement.product_id == product_id,
        StockMovement.movement_type == "in",
    ).scalar() or 0.0
    out_qty = db.query(func.sum(StockMovement.quantity)).filter(
        StockMovement.product_id == product_id,
        StockMovement.movement_type == "out",
    ).scalar() or 0.0
    # adjustment: quantity хранится со знаком (+ излишки, - недостача)
    adj_qty = db.query(func.sum(StockMovement.quantity)).filter(
        StockMovement.product_id == product_id,
        StockMovement.movement_type == "adjustment",
    ).scalar() or 0.0
    return round((p.initial_stock or 0) + in_qty - out_qty + adj_qty, 3)


def get_balances(db: Session) -> dict:
    """Остатки всех активных товаров одним запросом (без N+1).
    Логика adjustment: со знаком (+ излишки, - недостача)."""
    from app.models import Product, StockMovement
    # Агрегируем движения по товару и типу одним GROUP BY
    rows = db.query(
        StockMovement.product_id,
        StockMovement.movement_type,
        func.sum(StockMovement.quantity),
    ).group_by(StockMovement.product_id, StockMovement.movement_type).all()

    moves: dict[int, dict] = {}
    for pid, mtype, qty in rows:
        moves.setdefault(pid, {})[mtype] = qty or 0.0

    products = db.query(Product).filter(Product.is_active == True).all()
    result = {}
    for p in products:
        m = moves.get(p.id, {})
        bal = (p.initial_stock or 0) + m.get("in", 0.0) - m.get("out", 0.0) + m.get("adjustment", 0.0)
        result[p.id] = round(bal, 3)
    return result


def maybe_notify_low_stock(db: Session, product_id: int) -> None:
    from app.models import Product, Notification
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p or not p.min_stock or p.min_stock <= 0:
        return
    balance = get_balance(db, product_id)
    if balance <= p.min_stock:
        existing = db.query(Notification).filter(
            Notification.product_id == product_id,
            Notification.type == "low_stock",
            Notification.is_read == False,
        ).first()
        if not existing:
            db.add(Notification(
                type="low_stock",
                title=f"Низкий остаток: {p.name}",
                body=f"Текущий остаток {balance} {p.unit} ≤ минимум {p.min_stock} {p.unit}",
                product_id=product_id,
                link="/warehouse/",
            ))
    else:
        db.query(Notification).filter(
            Notification.product_id == product_id,
            Notification.type == "low_stock",
            Notification.is_read == False,
        ).update({"is_read": True})

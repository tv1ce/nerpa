"""Рекламации в разрезе точек.

Рекламация в NERPA привязана к **точке** — конкретному адресу доставки, а не
только к контрагенту. У сетевого клиента адресов бывает несколько десятков, и
претензия «по контрагенту» не отвечает на главный вопрос разбора: где именно
случился брак. Плюс два брака по одной кофейне и два по разным — это разные
истории: первое означает проблему у клиента (хранение, витрина, персонал),
второе — у нас (партия, доставка).

Ключ точки — тот же нормализованный `address_key` («улица:дом»), что и в
аналитике точек (services/outlets), поэтому рекламации ложатся на уже готовую
ось: карточка точки, сети, прогнозы расхода.

Здесь собрано всё, что нужно и реестру, и карточке заказа, и карточке клиента:
список точек контрагента для выбора, группировка, сроки разбора и сводка.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.models import (CLAIM_SEVERITIES, CLAIM_SLA_DAYS, CLAIM_STATUSES,
                        Claim, Comment, AuditLog, Order, User)
from app.services.outlets import DELIVERED_STATUSES, address_label, normalize_address
from app.tz import now as msk_now

OPEN_STATUSES = ("new", "in_progress")

# Цвет критичности — один и тот же в реестре, в баннере заказа и в карточке
SEVERITY_COLORS = {"low": "secondary", "normal": "warning", "critical": "danger"}

# Заказы, по которым вообще имеет смысл заводить рекламацию: товар уехал клиенту.
# Черновик или отменённый заказ рекламации не создаёт.
CLAIMABLE_STATUSES = DELIVERED_STATUSES


# ── Точки контрагента ────────────────────────────────────────────────────────

def counterparty_outlets(db: Session, counterparty_id: int) -> list[dict]:
    """Точки контрагента для выбора в форме рекламации.

    Считается по его же заказам (точка не имеет строки в базе — она выводится из
    адресов доставки), поэтому запрос лёгкий: только заказы одного клиента, без
    полного пересчёта аналитики по всем точкам.

    Для каждой точки возвращаем последнюю поставку и историю рекламаций — именно
    это менеджер и хочет видеть в момент выбора: «по этой кофейне жалуются
    третий раз за месяц» видно ещё до сохранения.
    """
    if not counterparty_id:
        return []
    orders = (db.query(Order)
              .filter(Order.counterparty_id == counterparty_id,
                      Order.status.in_(CLAIMABLE_STATUSES))
              .order_by(Order.date.desc(), Order.id.desc())
              .all())

    claim_counts: dict[str, int] = defaultdict(int)
    open_counts: dict[str, int] = defaultdict(int)
    for c in db.query(Claim).filter(Claim.counterparty_id == counterparty_id).all():
        if not c.address_key:
            continue
        claim_counts[c.address_key] += 1
        if c.status in OPEN_STATUSES:
            open_counts[c.address_key] += 1

    points: dict[str, dict] = {}
    for order in orders:
        key = normalize_address(order.delivery_address)
        if not key:
            continue
        p = points.setdefault(key, {
            "address_key": key,
            "label": address_label(key),
            "raw_addresses": [],
            "orders": [],
            "last_date": None,
            "claims_total": claim_counts.get(key, 0),
            "claims_open": open_counts.get(key, 0),
        })
        raw = (order.delivery_address or "").strip()
        if raw and raw not in p["raw_addresses"]:
            p["raw_addresses"].append(raw)
        p["orders"].append(order)
        if p["last_date"] is None or (order.date and order.date > p["last_date"]):
            p["last_date"] = order.date

    # Сначала точки с открытыми рекламациями, потом по свежести поставки:
    # разбираться идут именно туда, где уже горит.
    return sorted(points.values(),
                  key=lambda p: (-p["claims_open"], -(p["last_date"].toordinal() if p["last_date"] else 0)))


def outlet_label_of(claim: Claim) -> str:
    """Подпись точки рекламации для списков и баннеров."""
    if claim.address_key:
        return address_label(claim.address_key)
    return "по клиенту в целом"


# ── Сроки разбора ────────────────────────────────────────────────────────────

def sla_state(claim: Claim, today: date | None = None) -> dict:
    """Сколько рекламация висит и не просрочена ли она.

    Срок зависит от критичности (CLAIM_SLA_DAYS): критичную разбираем в день
    обращения, мелкую — за неделю. Закрытые рекламации не «просрочены» — по ним
    считаем факт: за сколько дней закрыли.
    """
    today = today or date.today()
    started = claim.date or (claim.created_at.date() if claim.created_at else today)
    if claim.status in OPEN_STATUSES:
        age = (today - started).days
        limit = CLAIM_SLA_DAYS.get(claim.severity or "normal", 3)
        return {
            "age_days": age,
            "limit_days": limit,
            "overdue": age > limit,
            "overdue_by": max(0, age - limit),
            "closed_in": None,
        }
    closed = claim.resolved_at.date() if claim.resolved_at else None
    return {
        "age_days": (closed - started).days if closed else None,
        "limit_days": CLAIM_SLA_DAYS.get(claim.severity or "normal", 3),
        "overdue": False,
        "overdue_by": 0,
        "closed_in": (closed - started).days if closed else None,
    }


def is_overdue(claim: Claim, today: date | None = None) -> bool:
    return sla_state(claim, today)["overdue"]


# ── Группировка для реестра и карточки клиента ───────────────────────────────

def group_by_outlet(claims: list[Claim]) -> list[dict]:
    """Рекламации → группы по точкам, самые больные точки сверху.

    Так реестр и карточка клиента перестают быть плоской простыней: у клиента с
    двадцатью адресами видно не «12 рекламаций», а «Гончарная 2 — 4, из них 2
    открытых».
    """
    groups: dict[str | None, dict] = {}
    for c in claims:
        key = c.address_key or None
        g = groups.setdefault(key, {
            "address_key": key,
            "label": address_label(key) if key else "Без точки (по клиенту)",
            "claims": [],
            "open": 0,
            "overdue": 0,
            "amount": 0.0,
            "last_date": None,
        })
        g["claims"].append(c)
        if c.status in OPEN_STATUSES:
            g["open"] += 1
            if is_overdue(c):
                g["overdue"] += 1
        g["amount"] += c.amount or 0
        if g["last_date"] is None or (c.date and c.date > g["last_date"]):
            g["last_date"] = c.date
    return sorted(groups.values(),
                  key=lambda g: (-g["overdue"], -g["open"], -len(g["claims"])))


def group_by_client(claims: list[Claim]) -> list[dict]:
    """Рекламации → контрагент → точки. Основной вид реестра.

    Двухуровневая группировка — единственная, которая честно отражает работу:
    звонить будешь клиенту, а разбираться по адресу.
    """
    by_cp: dict[int, list[Claim]] = defaultdict(list)
    for c in claims:
        by_cp[c.counterparty_id].append(c)

    result = []
    for cp_id, items in by_cp.items():
        cp = items[0].counterparty
        open_items = [c for c in items if c.status in OPEN_STATUSES]
        result.append({
            "counterparty": cp,
            "counterparty_id": cp_id,
            "name": (cp.trade_name or cp.name) if cp else f"Контрагент #{cp_id}",
            "network": cp.network.name if cp and cp.network_id and cp.network else "",
            "claims": items,
            "outlets": group_by_outlet(items),
            "open": len(open_items),
            "overdue": sum(1 for c in open_items if is_overdue(c)),
            "amount": sum(c.amount or 0 for c in items),
            "last_date": max((c.date for c in items if c.date), default=None),
        })
    return sorted(result, key=lambda g: (-g["overdue"], -g["open"], -len(g["claims"])))


def kanban(claims: list[Claim]) -> list[dict]:
    """Колонки канбана по статусам, внутри — критичные и просроченные сверху."""
    rank = {"critical": 0, "normal": 1, "low": 2}
    cols = []
    for status, label in CLAIM_STATUSES.items():
        items = [c for c in claims if c.status == status]
        items.sort(key=lambda c: (not is_overdue(c), rank.get(c.severity or "normal", 1),
                                  -(c.date.toordinal() if c.date else 0)))
        cols.append({
            "status": status,
            "label": label,
            "claims": items,
            "amount": sum(c.amount or 0 for c in items),
        })
    return cols


# ── Сводка для плиток ────────────────────────────────────────────────────────

def summary(claims: list[Claim]) -> dict:
    """KPI реестра: что горит, где повторы, сколько денег на кону."""
    open_items = [c for c in claims if c.status in OPEN_STATUSES]
    closed = [c for c in claims if c.status in ("resolved", "rejected")]
    closed_days = [d for d in (sla_state(c)["closed_in"] for c in closed) if d is not None]

    # Повторные точки: где рекламация не первая — это и есть системная проблема
    per_outlet: dict[str, int] = defaultdict(int)
    for c in claims:
        if c.address_key:
            per_outlet[c.address_key] += 1
    repeats = sum(1 for n in per_outlet.values() if n > 1)

    return {
        "total": len(claims),
        "open": len(open_items),
        "overdue": sum(1 for c in open_items if is_overdue(c)),
        "critical_open": sum(1 for c in open_items if c.severity == "critical"),
        "amount_open": sum(c.amount or 0 for c in open_items),
        "outlets": len(per_outlet),
        "repeat_outlets": repeats,
        "avg_close_days": round(sum(closed_days) / len(closed_days), 1) if closed_days else None,
    }


# ── Контекст точки: история и соседи ─────────────────────────────────────────

def outlet_history(db: Session, address_key: str, exclude_id: int = 0,
                   counterparty_id: int = 0) -> list[Claim]:
    """Другие рекламации той же точки — контекст для карточки.

    Один и тот же адрес могут занимать два разных заведения (разные контрагенты),
    поэтому при указанном counterparty_id историю сужаем до него: чужие претензии
    в разборе только мешают.
    """
    if not address_key:
        return []
    q = db.query(Claim).filter(Claim.address_key == address_key)
    if counterparty_id:
        q = q.filter(Claim.counterparty_id == counterparty_id)
    if exclude_id:
        q = q.filter(Claim.id != exclude_id)
    return q.order_by(Claim.date.desc(), Claim.id.desc()).all()


def outlet_claim_stats(db: Session) -> dict[str, dict]:
    """Рекламации по всем точкам: {address_key: {total, open, last_date}}.

    Нужна списку и карточке точек в аналитике — одним запросом, без N+1.
    """
    stats: dict[str, dict] = {}
    for c in db.query(Claim).filter(Claim.address_key.isnot(None)).all():
        s = stats.setdefault(c.address_key, {"total": 0, "open": 0, "last_date": None,
                                             "claims": []})
        s["total"] += 1
        if c.status in OPEN_STATUSES:
            s["open"] += 1
        if s["last_date"] is None or (c.date and c.date > s["last_date"]):
            s["last_date"] = c.date
        s["claims"].append(c)
    for s in stats.values():
        s["claims"].sort(key=lambda c: (c.date or date.min), reverse=True)
    return stats


# ── Разделение для карточки заказа ───────────────────────────────────────────

def claims_for_order(db: Session, order: Order, exclude_own: bool = True) -> dict:
    """Открытые рекламации, относящиеся к заказу: по его точке и остальные.

    Это и есть лечение «хуёвого отображения»: в заказе на Гончарную 2 в глаза
    бьют претензии по Гончарной 2, а претензии по другим кофейням того же клиента
    остаются свёрнутым хвостом «ещё N по другим точкам» — не мешают, но и не
    теряются.
    """
    if not order or not order.counterparty_id:
        return {"same": [], "other": [], "total": 0}
    q = db.query(Claim).filter(
        Claim.counterparty_id == order.counterparty_id,
        Claim.status.in_(OPEN_STATUSES),
    )
    if exclude_own and order.id:
        q = q.filter((Claim.order_id.is_(None)) | (Claim.order_id != order.id))
    if order.created_at:
        # Только те, что появились раньше заказа: по последующим претензиям
        # предупреждать в уже уехавшем заказе бессмысленно.
        q = q.filter(Claim.created_at <= order.created_at)
    items = q.order_by(Claim.date.desc(), Claim.id.desc()).all()

    key = normalize_address(order.delivery_address)
    same = [c for c in items if key and c.address_key == key]
    other = [c for c in items if not (key and c.address_key == key)]
    return {"same": same, "other": other, "total": len(items)}


# ── Таймлайн карточки ────────────────────────────────────────────────────────

def timeline(db: Session, claim: Claim) -> list[dict]:
    """Комментарии + смены статуса одной лентой, свежие сверху.

    Рекламация — процесс: звонок клиенту, забор образца, решение цеха. Одного
    поля «резолюция», которое перезаписывается, для этого мало.
    """
    events: list[dict] = []
    users = {u.id: u for u in db.query(User).all()}

    for cm in (db.query(Comment)
               .filter(Comment.entity_type == "claim", Comment.entity_id == claim.id)
               .all()):
        events.append({
            "kind": "comment", "at": cm.created_at, "text": cm.body,
            "user": users.get(cm.created_by_id), "id": cm.id,
        })

    for al in (db.query(AuditLog)
               .filter(AuditLog.entity_type == "claim", AuditLog.entity_id == claim.id)
               .all()):
        events.append({
            "kind": "status" if al.field == "status" else "event",
            "at": al.created_at, "text": al.note or "",
            "user": users.get(al.user_id), "id": None,
            "new_value": al.new_value,
        })

    events.append({
        "kind": "created", "at": claim.created_at,
        "text": f"Рекламация {claim.number} заведена",
        "user": users.get(claim.created_by_id), "id": None,
    })
    events.sort(key=lambda e: e["at"] or datetime.min, reverse=True)
    return events


def mark_status(claim: Claim, status: str) -> None:
    """Проставляет статус вместе с датой закрытия (или снимает её при возврате)."""
    claim.status = status
    if status in ("resolved", "rejected"):
        claim.resolved_at = claim.resolved_at or msk_now()
    else:
        claim.resolved_at = None

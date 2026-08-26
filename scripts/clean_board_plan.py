"""Чистка плановой отгрузки на табло цеха.

Зачем: в плане производства годами копились две категории мусора —
  * протухшие брони кабинета (`shop_bookings`), по которым заказ так и не
    вернулся из Bitrix24: сделку удалили, робот сломался, или это был тест;
  * заказы-черновики (`draft`) на будущие даты отгрузки, которые менеджер так
    и не подтвердил.

С версии, где план собирается по CONFIRMED_STATUSES, ни то ни другое на табло
уже не попадает. Но протухшие брони продолжают ЕСТЬ мощность даты в кабинете
клиента (`date_load`), из-за чего свободные дни показываются занятыми, — их
имеет смысл удалить физически.

Черновики скрипт НЕ трогает: подтверждать или отменять заказ — решение
менеджера, а не скрипта. Он только показывает их списком.

Запуск на сервере:
    # что нашлось (ничего не меняет)
    sudo -u tms /opt/tms/.venv/bin/python /opt/tms/scripts/clean_board_plan.py

    # удалить протухшие брони
    sudo -u tms /opt/tms/.venv/bin/python /opt/tms/scripts/clean_board_plan.py --apply
"""
import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal                      # noqa: E402
from app.models import Counterparty, Order, ShopBooking    # noqa: E402
from app.routers.shop import _BOOKING_TTL_HOURS, shipment_date  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Чистка плановой отгрузки на табло цеха")
    ap.add_argument("--apply", action="store_true",
                    help="удалить протухшие брони (без флага — только показать)")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        today = date.today()
        fresh_after = datetime.now() - timedelta(hours=_BOOKING_TTL_HOURS)
        known_deals = {str(x) for (x,) in db.query(Order.bitrix_deal_id)
                       .filter(Order.bitrix_deal_id.isnot(None)).all()}

        # ── Протухшие брони: заказ по сделке не приехал, TTL вышел ──────────
        stale = [
            b for b in db.query(ShopBooking).order_by(ShopBooking.ship_date).all()
            if str(b.bitrix_deal_id or "") not in known_deals
            and (b.created_at is None or b.created_at < fresh_after)
        ]
        print(f"Протухшие брони кабинета: {len(stale)}")
        for b in stale:
            cp = db.get(Counterparty, b.counterparty_id)
            future = " ← БУДУЩАЯ ДАТА" if b.ship_date and b.ship_date >= today else ""
            print(f"  #{b.id}  отгрузка {b.ship_date}  {b.qty} шт  "
                  f"сделка {b.bitrix_deal_id or '—'}  "
                  f"{cp.name if cp else '?'}  создана {b.created_at}{future}")

        # ── Черновики на будущие даты: их менеджер не подтвердил ────────────
        drafts = [o for o in db.query(Order).filter(Order.status == "draft").all()
                  if (d := shipment_date(o)) and d >= today]
        print(f"\nЧерновики заказов на будущие отгрузки: {len(drafts)} "
              f"(скрипт их НЕ трогает — подтвердить или отменить должен менеджер)")
        for o in drafts:
            cp = db.get(Counterparty, o.counterparty_id)
            print(f"  {o.number}  отгрузка {shipment_date(o)}  источник {o.source}  "
                  f"сделка {o.bitrix_deal_id or '—'}  {cp.name if cp else '?'}")

        if not args.apply:
            print(f"\nЭто был просмотр. Чтобы удалить {len(stale)} протухших броней, "
                  f"перезапустите с --apply")
            return 0

        for b in stale:
            db.delete(b)
        db.commit()
        print(f"\nУдалено броней: {len(stale)}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

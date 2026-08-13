import asyncio
import logging
import os
import time as _time
from contextlib import asynccontextmanager
from datetime import date as _date
from dotenv import load_dotenv
load_dotenv()  # загружаем .env до инициализации всего остального

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from app.routers import auth, dashboard, counterparties, networks, analytics, products, orders, invoices, contracts, settings, reports, warehouse, warehouse_shipping, warehouse_receiving, warehouse_transfers, warehouse_writeoffs, receivables, notifications, claims, activity, audit_log, board, logistics, leads, recon, field, files, public, sync_1c, sourcing, api_1c, api_sbis, api_saby_tms, api_bitrix, api_tochka, api_carrier, api_metafora, hr, hr_metrics, shop, landing
from app.database import init_db

logger = logging.getLogger(__name__)

# Миграции запускаются при каждом старте (в т.ч. при --reload)
init_db()

# Фиксируем момент старта для /health → uptime
_APP_START = _time.monotonic()


# ── Авто-перевод просроченных счетов в статус overdue ────────────────────────

def _mark_overdue_invoices() -> int:
    """Переводит счета issued→overdue если due_date < сегодня.
    Возвращает количество обновлённых записей."""
    from app.database import SessionLocal
    from app.models import Invoice
    from sqlalchemy import and_

    today = _date.today()
    db = SessionLocal()
    try:
        updated = (
            db.query(Invoice)
            .filter(
                Invoice.status.in_(["issued", "partial"]),
                Invoice.due_date != None,
                Invoice.due_date < today,
            )
            .all()
        )
        for inv in updated:
            inv.status = "overdue"
        if updated:
            db.commit()
            logger.info("Авто-просрочка: %d счетов → overdue", len(updated))
        return len(updated)
    except Exception as e:
        logger.error("Ошибка авто-просрочки счетов: %s", e)
        db.rollback()
        return 0
    finally:
        db.close()


def _mark_expired_contracts() -> int:
    """Переводит договора active→expired если end_date < сегодня."""
    from app.database import SessionLocal
    from app.models import Contract

    today = _date.today()
    db = SessionLocal()
    try:
        updated = (
            db.query(Contract)
            .filter(
                Contract.status == "active",
                Contract.end_date.isnot(None),
                Contract.end_date < today,
            )
            .update({"status": "expired"}, synchronize_session=False)
        )
        if updated:
            db.commit()
            logger.info("Договора: переведено в expired: %d", updated)
        return updated
    except Exception as e:
        logger.error("_mark_expired_contracts: %s", e)
        db.rollback()
        return 0
    finally:
        db.close()


def _notify_expiring_contracts() -> int:
    """Создаёт уведомления о договорах, истекающих в ближайшие N дней.
    N берётся из CompanySettings.notify_contract_days (0 = выключено).
    Дедуп: не плодим повтор, если по этому договору уже есть непрочитанное."""
    from datetime import timedelta
    from app.database import SessionLocal
    from app.models import Contract, Notification, CompanySettings

    today = _date.today()
    db = SessionLocal()
    try:
        company = db.query(CompanySettings).first()
        days = (company.notify_contract_days if company else 14) or 0
        if days <= 0:
            return 0
        horizon = today + timedelta(days=days)
        created = 0
        contracts = (
            db.query(Contract)
            .filter(
                Contract.status == "active",
                Contract.end_date.isnot(None),
                Contract.end_date >= today,
                Contract.end_date <= horizon,
            )
            .all()
        )
        for c in contracts:
            link = f"/contracts/{c.id}"
            exists = db.query(Notification).filter(
                Notification.type == "contract_expiry",
                Notification.link == link,
                Notification.is_read == False,
            ).first()
            if exists:
                continue
            left = (c.end_date - today).days
            cp = c.counterparty
            db.add(Notification(
                type="contract_expiry",
                title=f"Договор №{c.number} истекает через {left} дн.",
                body=f"Контрагент: {cp.name if cp else '—'}. Дата окончания: {c.end_date.strftime('%d.%m.%Y')}.",
                link=link,
            ))
            created += 1
        if created:
            db.commit()
            logger.info("Уведомления об истечении договоров: создано %d", created)
        return created
    except Exception as e:
        logger.error("_notify_expiring_contracts: %s", e)
        db.rollback()
        return 0
    finally:
        db.close()


def _notify_due_invoices() -> int:
    """Создаёт уведомления о счетах, у которых дедлайн оплаты в ближайшие N дней.
    N из CompanySettings.notify_invoice_days (0 = выключено). Дедлайн с учётом отсрочки КА."""
    from datetime import timedelta
    from app.database import SessionLocal
    from app.models import Invoice, Notification, CompanySettings
    from app.routers.receivables import overdue_deadline

    today = _date.today()
    db = SessionLocal()
    try:
        company = db.query(CompanySettings).first()
        days = (company.notify_invoice_days if company else 3) or 0
        if days <= 0:
            return 0
        created = 0
        invoices = (
            db.query(Invoice)
            .filter(Invoice.status.in_(["issued", "partial"]), Invoice.due_date.isnot(None))
            .all()
        )
        for inv in invoices:
            deadline = overdue_deadline(inv)
            if not deadline:
                continue
            left = (deadline - today).days
            if left < 0 or left > days:
                continue  # уже просрочен или ещё далеко
            link = f"/invoices/{inv.id}"
            exists = db.query(Notification).filter(
                Notification.type == "invoice_due",
                Notification.link == link,
                Notification.is_read == False,
            ).first()
            if exists:
                continue
            cp = inv.counterparty
            when = "сегодня" if left == 0 else f"через {left} дн."
            db.add(Notification(
                type="invoice_due",
                title=f"Счёт №{inv.number}: оплата {when}",
                body=f"Контрагент: {cp.name if cp else '—'}. Сумма: {inv.total_amount:,.0f} ₽. Срок: {deadline.strftime('%d.%m.%Y')}.".replace(",", " "),
                link=link,
            ))
            created += 1
        if created:
            db.commit()
            logger.info("Напоминания об оплате счетов: создано %d", created)
        return created
    except Exception as e:
        logger.error("_notify_due_invoices: %s", e)
        db.rollback()
        return 0
    finally:
        db.close()


async def _overdue_loop():
    """Фоновая задача: просрочка счетов/договоров + напоминания, каждый час."""
    while True:
        try:
            _mark_overdue_invoices()
            _mark_expired_contracts()
            _notify_expiring_contracts()
            _notify_due_invoices()
            _notify_unfilled_metrics()
        except Exception as e:
            logger.error("overdue_loop: %s", e)
        await asyncio.sleep(3600)  # раз в час


def _escalate_bitrix_alerts(threshold_minutes: int = 10) -> int:
    """Повторно шлёт Telegram-напоминание по непрочитанным уведомлениям bitrix_order —
    чтобы менеджер точно не пропустил новый заказ, даже если не открывал TMS в браузере.
    Не чаще раза в threshold_minutes на одно уведомление (escalated_at)."""
    import os
    from datetime import timedelta
    from app.database import SessionLocal
    from app.models import Notification, CompanySettings

    db = SessionLocal()
    try:
        company = db.query(CompanySettings).first()
        chat_ids = [c.strip() for c in (company.bitrix_alert_chat_ids or "").split(",") if c.strip()] if company else []
        bot_token = ((company.tg_bot_token or "").strip() if company else "") or os.getenv("TMS_BOT_TOKEN", "").strip()
        if not chat_ids or not bot_token:
            return 0

        from datetime import datetime
        now = datetime.now()
        threshold = now - timedelta(minutes=threshold_minutes)
        pending = db.query(Notification).filter(
            Notification.type == "bitrix_order",
            Notification.is_read == False,
            Notification.created_at < threshold,
        ).filter(
            (Notification.escalated_at.is_(None)) | (Notification.escalated_at < threshold)
        ).all()

        if not pending:
            return 0

        import httpx
        sent = 0
        for n in pending:
            text = f"⏰ Напоминание: заказ из Bitrix24 всё ещё не обработан!\n{n.title}\nОткройте TMS → Уведомления."
            try:
                with httpx.Client(timeout=10.0) as client:
                    for chat_id in chat_ids:
                        client.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                    json={"chat_id": chat_id, "text": text})
                n.escalated_at = now
                sent += 1
            except Exception as e:
                logger.warning("Эскалация bitrix_order #%s: %s", n.id, e)
        if sent:
            db.commit()
            logger.info("Эскалация Bitrix24-уведомлений: отправлено %d", sent)
        return sent
    except Exception as e:
        logger.error("_escalate_bitrix_alerts: %s", e)
        db.rollback()
        return 0
    finally:
        db.close()


async def _bitrix_escalation_loop():
    """Фоновая задача: проверка непрочитанных заказов из Bitrix24, каждые 5 минут."""
    while True:
        try:
            _escalate_bitrix_alerts()
        except Exception as e:
            logger.error("bitrix_escalation_loop: %s", e)
        await asyncio.sleep(300)


def _notify_unfilled_metrics() -> int:
    """Понедельник: напоминание HR/админам о метриках, не заполненных за прошлую
    неделю. Уведомление внутреннее (колокольчик) — рассылку руководителям HR
    инициирует сам, кнопкой «Отчёт недели» в разделе метрик.

    Дедуп по ссылке с датой недели: за одну неделю напоминаем один раз."""
    from datetime import timedelta
    from app.database import SessionLocal
    from app.models import HrEmployee, HrMetric, HrMetricValue, Notification, User

    db = SessionLocal()
    try:
        today = _date.today()
        if today.weekday() != 0:   # напоминаем в понедельник, когда неделя уже закрыта
            return 0
        week = today - timedelta(days=7)   # понедельник прошлой недели
        link = f"/hr/metrics?week={week.isoformat()}"
        if db.query(Notification).filter(
                Notification.type == "hr_metrics_missing",
                Notification.link == link).first():
            return 0

        metric_ids = [m.id for m in db.query(HrMetric)
                      .join(HrEmployee, HrMetric.employee_id == HrEmployee.id)
                      .filter(HrMetric.is_active == True, HrEmployee.is_active == True).all()]
        if not metric_ids:
            return 0
        filled = {v.metric_id for v in db.query(HrMetricValue).filter(
            HrMetricValue.metric_id.in_(metric_ids),
            HrMetricValue.week_start == week,
            HrMetricValue.value.isnot(None)).all()}
        missing = len(metric_ids) - len(filled)
        if not missing:
            return 0

        recipients = db.query(User).filter(
            User.role.in_(("hr", "admin")), User.is_active == True).all()
        for user in recipients:
            db.add(Notification(
                type="hr_metrics_missing",
                title=f"Метрики за прошлую неделю: не заполнено {missing} из {len(metric_ids)}",
                body=f"Неделя с {week.strftime('%d.%m.%Y')}. Напомните руководителям подразделений.",
                link=link,
                user_id=user.id,
            ))
        db.commit()
        logger.info("Напоминание о метриках за неделю %s: не заполнено %d", week, missing)
        return missing
    except Exception as e:
        logger.error("_notify_unfilled_metrics: %s", e)
        db.rollback()
        return 0
    finally:
        db.close()


def _rotate_generated(max_age_days: int = 90) -> int:
    """Удаляет файлы из generated/ старше max_age_days дней.
    Возвращает количество удалённых файлов."""
    import glob as _glob
    from pathlib import Path

    cutoff = _time.time() - max_age_days * 86400
    deleted = 0
    for path in (Path(__file__).parent.parent / "generated").glob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        except Exception as e:
            logger.warning("Не удалось удалить %s: %s", path, e)
    if deleted:
        logger.info("Ротация generated/: удалено %d файлов старше %d дней", deleted, max_age_days)
    return deleted


def _run_1c_sync_job():
    """Фоновая задача APScheduler (раз в 15 минут): сверка оплат (Точка → 1С) +
    тяжёлые/нечастые импорты из 1С (каталоги, счета, документы). Задачи,
    важные кладовщику «прямо сейчас» (приёмка/перемещение/остатки),
    вынесены в отдельную частую задачу — см. _run_1c_fast_sync_job.

    Порядок сверки важен: СНАЧАЛА банк «Точка», ПОТОМ 1С. Так оплата, уже
    разнесённая по банковской выписке, при последующей сверке 1С не задваивается
    (дедуп в apply_payment по этому и заботится)."""
    from app.database import SessionLocal
    from app.services.tochka_client import sync_payments_from_tochka
    from app.services.onec_client import (
        sync_products_from_1c, sync_payments_from_1c, sync_invoices_from_1c,
        sync_shipments_from_1c, sync_documents_from_1c, retry_unpushed_orders,
        sync_warehouses_from_1c, sync_categories_from_1c,
    )
    db = SessionLocal()
    try:
        rt = sync_payments_from_tochka(db)   # 1) банк «Точка» — приоритетный источник оплат
        r0 = retry_unpushed_orders(db)       # самовосстановление пропущенного push заказов
        rw = sync_warehouses_from_1c(db)
        rc = sync_categories_from_1c(db)
        r1 = sync_products_from_1c(db)
        r3 = sync_invoices_from_1c(db)
        sync_shipments_from_1c(db)
        rd = sync_documents_from_1c(db)
        r2 = sync_payments_from_1c(db)        # 2) 1С — добор того, чего не было в банке
        logger.info(
            "auto-sync: tochka m=%s u=%s; orders_pushed=%s; warehouses c=%s u=%s; categories c=%s u=%s; "
            "products c=%s u=%s; invoices c=%s u=%s; docs a=%s; payments_1c u=%s",
            rt.get("matched"), rt.get("unmatched"), r0.get("pushed"),
            rw.get("created"), rw.get("updated"), rc.get("created"), rc.get("updated"),
            r1.get("created"), r1.get("updated"),
            r3.get("created"), r3.get("updated"), rd.get("attached"), r2.get("updated"),
        )
    except Exception as e:
        logger.error("auto-sync job error: %s", e)
    finally:
        db.close()


def _run_1c_fast_sync_job():
    """Фоновая задача APScheduler (раз в минуту): только то, что кладовщик
    ждёт «прямо сейчас» — задачи на приёмку/перемещение из 1С и остатки по
    складам. Специально отделено от тяжёлого 15-минутного _run_1c_sync_job
    (каталоги/счета/файлы), чтобы новая приходная накладная или перемещение,
    созданные в 1С, попадали кладовщику в TMS быстро, а не раз в 15 минут."""
    from app.database import SessionLocal
    from app.services.onec_client import (
        sync_receiving_tasks_from_1c, sync_transfer_tasks_from_1c, sync_stock_balances_from_1c,
    )
    from app.services.bitrix_client import push_stock_to_bitrix
    db = SessionLocal()
    try:
        rr = sync_receiving_tasks_from_1c(db)
        rtr = sync_transfer_tasks_from_1c(db)
        rb = sync_stock_balances_from_1c(db)
        # Свежий остаток сразу уезжает в карточку товара Bitrix24 (PROPERTY_119):
        # пишутся только изменившиеся значения, поэтому обычный такт молчит.
        bx = push_stock_to_bitrix(db)
        if rr.get("created") or rtr.get("created") or rr.get("errors") or rtr.get("errors") or rb.get("errors") \
                or bx.get("pushed") or bx.get("errors"):
            logger.info(
                "fast-sync: receiving c=%s u=%s errs=%s; transfers c=%s u=%s errs=%s; "
                "balances u=%s errs=%s; bitrix-stock p=%s errs=%s",
                rr.get("created"), rr.get("updated"), rr.get("errors"),
                rtr.get("created"), rtr.get("updated"), rtr.get("errors"),
                rb.get("updated"), rb.get("errors"),
                bx.get("pushed"), bx.get("errors"),
            )
    except Exception as e:
        logger.error("fast-sync job error: %s", e)
    finally:
        db.close()


def _run_bitrix_lead_retry_job():
    """Фоновая задача APScheduler: повторно пробует выгрузить в Bitrix24 точки
    в статусе 'deal', для которых авто-выгрузка не сработала с первого раза
    (сбой сети/вебхука в момент смены статуса)."""
    from app.database import SessionLocal
    from app.services.bitrix_client import retry_unpushed_leads
    db = SessionLocal()
    try:
        r = retry_unpushed_leads(db)
        if r["pushed"] or r["failed"]:
            logger.info("bitrix lead retry: pushed=%s failed=%s", r["pushed"], r["failed"])
    except Exception as e:
        logger.error("bitrix lead retry job error: %s", e)
    finally:
        db.close()


def _run_saby_status_job():
    """Фоновая задача APScheduler: опрашивает статусы заказов-заявок и ЭТрН в Saby
    (СБИС.СписокИзменений) и обновляет их в TMS, уведомляя менеджера при
    утверждении/отклонении перевозчиком."""
    from app.database import SessionLocal
    from app.services.saby_tms_client import poll_saby_tms_statuses
    db = SessionLocal()
    try:
        r = poll_saby_tms_statuses(db)
        if r["updated"]:
            logger.info("saby tms статусы: checked=%s updated=%s", r["checked"], r["updated"])
    except Exception as e:
        logger.error("saby tms status job error: %s", e)
    finally:
        db.close()


def _run_sbis_edo_status_job():
    """Фоновая задача APScheduler: опрашивает статусы счетов/УПД в СБИС ЭДО
    (СБИС.ПрочитатьДокумент) и уведомляет менеджера при подписании/отклонении."""
    from app.database import SessionLocal
    from app.services.sbis_client import poll_sbis_edo_statuses
    db = SessionLocal()
    try:
        r = poll_sbis_edo_statuses(db)
        if r["updated"]:
            logger.info("sbis edo статусы: checked=%s updated=%s", r["checked"], r["updated"])
    except Exception as e:
        logger.error("sbis edo status job error: %s", e)
    finally:
        db.close()


def _run_versta_status_job():
    """Фоновая задача APScheduler: опрашивает статус доставки заказов, оформленных
    через экспедитора Versta24 (POST /Track по номеру заказа Versta), и обновляет
    статус/курьера в TMS."""
    from app.database import SessionLocal
    from app.services.versta_client import poll_versta_statuses
    db = SessionLocal()
    try:
        r = poll_versta_statuses(db)
        if r["updated"] or r["delivered"]:
            logger.info("versta статусы: checked=%s updated=%s delivered=%s",
                        r["checked"], r["updated"], r["delivered"])
    except Exception as e:
        logger.error("versta status job error: %s", e)
    finally:
        db.close()


def _run_outlets_digest_job():
    """Фоновая задача APScheduler: ежедневная ИИ-сводка «кому звонить сегодня»
    по точкам — собирается и уходит в Telegram в заданное время (см.
    app/services/outlets_digest.py)."""
    from app.services.outlets_digest import run_scheduled
    run_scheduled()


def _run_bitrix_cp_requisites_job():
    """Фоновая задача APScheduler: дозаливает ИНН/банковские реквизиты контрагентов,
    созданных из Bitrix24. Робот стадии «Заказ согласован» дёргает вебхук раньше,
    чем менеджер успевает вписать реквизиты в карточку CRM, — поэтому периодически
    возвращаемся и добираем пустые поля из свежих данных Bitrix + DaData."""
    from app.database import SessionLocal
    from app.services.bitrix_client import retry_bitrix_counterparty_requisites
    db = SessionLocal()
    try:
        r = retry_bitrix_counterparty_requisites(db)
        if r["updated"]:
            logger.info("bitrix cp requisites: checked=%s updated=%s", r["checked"], r["updated"])
    except Exception as e:
        logger.error("bitrix cp requisites job error: %s", e)
    finally:
        db.close()


async def _resubscribe_tochka_webhook() -> None:
    """Переподписка вебхука Точки в фоне, с потолком по времени.

    Блокирующий httpx уводим в поток, чтобы он не занимал event loop, и режем
    по таймауту: недоступный банк — это повод для строчки в логе, а не для
    неподнявшегося сервера."""
    def _work():
        from app.database import SessionLocal
        from app.services.tochka_client import ensure_webhook_saved
        db = SessionLocal()
        try:
            return ensure_webhook_saved(db)
        finally:
            db.close()

    try:
        r = await asyncio.wait_for(asyncio.to_thread(_work), timeout=60)
        if r.get("ok"):
            logger.info("Точка: вебхук переподписан (%s)", r.get("url"))
    except asyncio.TimeoutError:
        logger.warning("Точка: переподписка вебхука не уложилась в 60с — пропускаем")
    except Exception as e:
        logger.warning("Точка: переподписка вебхука не выполнена: %s", e)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """FastAPI lifespan: заменяет устаревший @app.on_event('startup')."""
    # ── startup ──────────────────────────────────────────────────────────────
    _mark_overdue_invoices()          # перевести просроченные счета
    _mark_expired_contracts()         # перевести истёкшие договора
    _notify_expiring_contracts()      # уведомления об истечении договоров
    _notify_due_invoices()            # напоминания об оплате счетов
    _rotate_generated(max_age_days=90)  # удалить старые docx
    asyncio.create_task(_overdue_loop())  # фоновый цикл каждый час
    asyncio.create_task(_bitrix_escalation_loop())  # напоминания о необработанных заказах Bitrix24
    board.start_now_playing()         # поллер «сейчас играет» на табло

    # APScheduler: поллинг 1С каждые 15 минут (отключён если onec_enabled=False)
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        _scheduler = BackgroundScheduler(timezone="Europe/Moscow")
        _scheduler.add_job(_run_1c_sync_job, "interval", minutes=15, id="1c_sync",
                           misfire_grace_time=60)
        _scheduler.add_job(_run_1c_fast_sync_job, "interval", minutes=1, id="1c_fast_sync",
                           misfire_grace_time=20)
        _scheduler.add_job(_run_bitrix_lead_retry_job, "interval", minutes=15, id="bitrix_lead_retry",
                           misfire_grace_time=60)
        _scheduler.add_job(_run_bitrix_cp_requisites_job, "interval", minutes=10, id="bitrix_cp_requisites",
                           misfire_grace_time=60)
        _scheduler.add_job(_run_saby_status_job, "interval", minutes=20, id="saby_tms_status",
                           misfire_grace_time=60)
        _scheduler.add_job(_run_sbis_edo_status_job, "interval", minutes=20, id="sbis_edo_status",
                           misfire_grace_time=60)
        _scheduler.add_job(_run_versta_status_job, "interval", minutes=30, id="versta_status",
                           misfire_grace_time=60)
        # Сводка по точкам: проверяем каждые 5 минут — время отправки задаётся в
        # настройках и может меняться без перезапуска сервера
        _scheduler.add_job(_run_outlets_digest_job, "interval", minutes=5, id="outlets_digest",
                           misfire_grace_time=300)
        _scheduler.start()
        logger.info("APScheduler: задачи 1c_sync, 1c_fast_sync, bitrix_lead_retry, bitrix_cp_requisites, saby_tms_status, sbis_edo_status, versta_status, outlets_digest запущены")
    except ImportError:
        logger.warning("apscheduler не установлен — автосинхронизация 1С выключена")

    # Переподписка вебхука Точки по сохранённому адресу (переживает рестарт/деплой).
    #
    # УХОДИТ В ФОН НАМЕРЕННО. Это сетевой вызов к чужому API, и раньше он висел
    # прямо в lifespan: 13.08.2026 у сервера отвалилась исходящая сеть, вызов не
    # вернулся, startup не завершился — и uvicorn не открыл порт. Приложение
    # числилось «active» у systemd, планировщик крутился, а сайт лежал целиком
    # из-за необязательной переподписки вебхука.
    #
    # Ничто, что зависит от третьей стороны, не должно решать, поднимется ли TMS.
    asyncio.create_task(_resubscribe_tochka_webhook())

    yield
    # ── shutdown (ничего освобождать не нужно) ────────────────────────────────


app = FastAPI(
    title="TMS — Управление поставками",
    lifespan=lifespan,
    # /docs и /redoc закрыты в production — схема API не должна быть публичной
    docs_url=None,
    redoc_url=None,
)


# Сессия живёт 30 дней — чтобы мобильное приложение/браузер «помнили» пользователя
_session_secret = os.environ.get("SECRET_KEY")
if not _session_secret:
    import secrets
    _session_secret = secrets.token_hex(32)
    logger.warning(
        "SECRET_KEY не задан в .env — используется временный ключ. "
        "Все сессии сбросятся при перезапуске. Добавьте SECRET_KEY в .env"
    )

app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    max_age=60 * 60 * 24 * 30,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Папка с дистрибутивом Android-приложения (APK + version.json)
APP_DIST_DIR = "app_dist"


# ── PWA: манифест и service worker (нужны на корне, без авторизации) ──────────

@app.get("/manifest.webmanifest", include_in_schema=False)
async def pwa_manifest():
    return FileResponse(
        "app/static/manifest.webmanifest",
        media_type="application/manifest+json",
    )


@app.get("/sw.js", include_in_schema=False)
async def pwa_service_worker():
    # SW обязан отдаваться с корня, чтобы его scope покрывал весь сайт ('/')
    return FileResponse(
        "app/static/js/sw.js",
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )


# ── Автообновление Android-приложения ────────────────────────────────────────
# Приложение при запуске запрашивает /app/version.json и сравнивает versionCode
# с установленным. Если на сервере новее — скачивает APK с /app/download.

@app.get("/health", include_in_schema=False)
async def health():
    """
    Расширенный health-check.

    Возвращает HTTP 200 когда всё OK, HTTP 503 если БД недоступна.
    Используется nginx upstream_check, Docker HEALTHCHECK, мониторингом.

    Поля ответа:
      status   — "ok" | "degraded"
      db       — "ok" | "error"
      db_ms    — время ответа БД в мс
      uptime_s — секунд с момента запуска процесса
    """
    from sqlalchemy import text
    from app.database import SessionLocal

    uptime_s = int(_time.monotonic() - _APP_START)

    db_ok = False
    db_ms = 0.0
    try:
        t0 = _time.monotonic()
        _db = SessionLocal()
        _db.execute(text("SELECT 1"))
        _db.close()
        db_ok = True
        db_ms = round((_time.monotonic() - t0) * 1000, 1)
    except Exception:
        pass

    payload = {
        "status":   "ok" if db_ok else "degraded",
        "db":       "ok" if db_ok else "error",
        "db_ms":    db_ms,
        "uptime_s": uptime_s,
    }
    return JSONResponse(content=payload, status_code=200 if db_ok else 503)


@app.get("/app/version.json", include_in_schema=False)
async def app_version():
    path = os.path.join(APP_DIST_DIR, "version.json")
    if os.path.exists(path):
        return FileResponse(path, media_type="application/json",
                            headers={"Cache-Control": "no-cache"})
    # Нет опубликованной версии — обновлений нет
    return JSONResponse({"versionCode": 0, "versionName": "", "notes": ""})


@app.get("/app/download", include_in_schema=False)
async def app_download():
    path = os.path.join(APP_DIST_DIR, "tms-sklad.apk")
    if os.path.exists(path):
        return FileResponse(
            path,
            media_type="application/vnd.android.package-archive",
            filename="tms-sklad.apk",
        )
    return JSONResponse({"error": "apk not found"}, status_code=404)

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(counterparties.router)
app.include_router(networks.router)   # сети заведений — группировка контрагентов одной вывески
app.include_router(analytics.router)  # аналитика по точкам (адресам доставки) + ИИ
app.include_router(products.router)
app.include_router(orders.router)
app.include_router(invoices.router)
app.include_router(contracts.router)
app.include_router(settings.router)
app.include_router(reports.router)
app.include_router(warehouse.router)
app.include_router(warehouse_shipping.router)
app.include_router(warehouse_receiving.router)
app.include_router(warehouse_transfers.router)
app.include_router(warehouse_writeoffs.router)
app.include_router(receivables.router)
app.include_router(notifications.router)
app.include_router(claims.router)
app.include_router(activity.router)
app.include_router(audit_log.router)
app.include_router(board.router)
app.include_router(logistics.router)
app.include_router(leads.router)
app.include_router(recon.router)
app.include_router(field.router)
app.include_router(files.router)
app.include_router(public.router)
app.include_router(shop.router)   # клиентский кабинет заказа /shop/{token}
app.include_router(landing.router)  # публичный лендинг заявки /order
app.include_router(sync_1c.router)
app.include_router(sourcing.router)
app.include_router(api_1c.router)     # приём документов из 1С (push, вариант A)
app.include_router(api_sbis.router)   # СБИС ЭПД/ЭТРН (вариант C)
app.include_router(api_saby_tms.router)  # Saby «Управление транспортом» — заказы на перевозку (ЭЗЗ) + ЭТрН
app.include_router(api_bitrix.router) # Bitrix24 CRM — приём сделок + настройка маппинга
app.include_router(api_tochka.router) # Банк «Точка» — вебхук/сверка входящих оплат
app.include_router(api_carrier.router)  # «Помощник логиста» — подтверждение доставки (бот бота не слышит)
app.include_router(api_metafora.router) # Метафора — вебхук статусов перевозки от перевозчика
app.include_router(hr.router)         # HR-отчётность (Teamly)
app.include_router(hr_metrics.router) # Метрика сотрудников — недельный срез


# ── Jinja2 фильтры ───────────────────────────────────────────────────────────

def _fmt_money(value):
    if value is None:
        return "0,00"
    return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")


def _fmt_date(value):
    if not value:
        return "—"
    from datetime import date, datetime
    if isinstance(value, (date, datetime)):
        return value.strftime("%d.%m.%Y")
    return str(value)


def _fmt_datetime(value):
    """Дата + время с точностью до минуты — для журналов действий (кто/что/когда),
    где важно отследить последовательность событий, а не только день."""
    if not value:
        return "—"
    from datetime import date, datetime
    if isinstance(value, (date, datetime)):
        return value.strftime("%d.%m.%Y %H:%M")
    return str(value)


# Регистрируем фильтры во всех шаблонах через Jinja2Templates
from datetime import date as _date
import secrets as _secrets

def _get_csrf_token(request) -> str:
    """Возвращает CSRF-токен из сессии, при необходимости создаёт новый."""
    token = request.session.get("csrf_token")
    if not token:
        token = _secrets.token_hex(32)
        request.session["csrf_token"] = token
    return token

_templates = Jinja2Templates(directory="app/templates")
_templates.env.filters["money"] = _fmt_money
_templates.env.filters["date_fmt"] = _fmt_date
_templates.env.filters["datetime_fmt"] = _fmt_datetime
_templates.env.filters["format_number"] = lambda v: f"{int(v):,}".replace(",", " ")
# today — прокси-объект, который всегда возвращает ТЕКУЩУЮ дату.
# Шаблоны используют его без скобок: {{ today }}, today <= date, today.isoformat() —
# всё работает как с обычным date-объектом, но значение свежее при каждом рендере.
class _TodayProxy:
    """Прокси вокруг date.today() — обновляется при каждом обращении к атрибуту."""
    def __getattr__(self, name):
        return getattr(_date.today(), name)
    def __str__(self):   return str(_date.today())
    def __repr__(self):  return repr(_date.today())
    def __format__(self, fmt): return format(_date.today(), fmt)
    def __eq__(self, other):   return _date.today() == other
    def __lt__(self, other):   return _date.today() <  other
    def __le__(self, other):   return _date.today() <= other
    def __gt__(self, other):   return _date.today() >  other
    def __ge__(self, other):   return _date.today() >= other
    def __hash__(self):        return hash(_date.today())

_templates.env.globals["today"] = _TodayProxy()
_templates.env.globals["csrf_token"] = _get_csrf_token


def _hr_mobile(request) -> bool:
    """Показывать ли HR-раздел мобильным кабинетом (base_hr.html).

    По умолчанию мобильный вид у роли hr — телефон её основной инструмент.
    Остальные роли (админ, руководитель) заходят с компьютера и включают
    мобильный вид вручную кнопкой в меню. Выбор хранится в сессии, чтобы
    держался между страницами; переключает POST /hr/toggle-view.
    """
    session = getattr(request, "session", {}) or {}
    default = "mobile" if session.get("user_role") == "hr" else "desktop"
    return session.get("hr_view", default) == "mobile"

_templates.env.globals["hr_mobile"] = _hr_mobile

# Справочники рекламаций доступны всем шаблонам: напоминание о нерешённой
# рекламации показывается не только в разделе «Рекламации», но и в карточке
# и форме заказа. Явно переданный контекст по-прежнему имеет приоритет.
from app.models import CLAIM_STATUSES as _CLAIM_STATUSES, CLAIM_TYPES as _CLAIM_TYPES
_templates.env.globals["claim_statuses"] = _CLAIM_STATUSES
_templates.env.globals["claim_types"] = _CLAIM_TYPES


def _safe_url(v):
    """Безопасный href: рабочий URL или '#'. Не-URL текст (мусор в полях
    соцсетей/сайта) не превращается в относительную ссылку — иначе клик уводит
    на /recon/<текст> и ломает роут."""
    if not v:
        return "#"
    v = str(v).strip()
    if v.startswith(("http://", "https://", "mailto:", "tel:")):
        return v
    if "." in v and " " not in v and "@" not in v:
        return "https://" + v.lstrip("/")
    return "#"


_templates.env.filters["safe_url"] = _safe_url


def _from_json(v):
    """Парсит JSON-строку в объект (для полей вроде FieldVisit.photos). Безопасно."""
    if not v:
        return []
    if isinstance(v, (list, dict)):
        return v
    import json as _json
    try:
        return _json.loads(v)
    except (ValueError, TypeError):
        return []


_templates.env.filters["from_json"] = _from_json


def _fmt_filesize(value):
    """Человекочитаемый размер файла: 2,4 МБ / 512 КБ / 320 Б."""
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    if n < 1024:
        return f"{int(n)} Б"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} КБ"
    return f"{n / 1024 / 1024:.1f} МБ".replace(".", ",")


_templates.env.filters["filesize"] = _fmt_filesize

# Патчим все роутеры, чтобы они использовали тот же env
import app.routers.auth as _r_auth
import app.routers.dashboard as _r_dash
import app.routers.counterparties as _r_cp
import app.routers.networks as _r_net
import app.routers.analytics as _r_analytics
import app.routers.products as _r_prod
import app.routers.orders as _r_ord
import app.routers.invoices as _r_inv
import app.routers.contracts as _r_con
import app.routers.settings as _r_set
import app.routers.reports as _r_rep
import app.routers.warehouse as _r_wh
import app.routers.warehouse_shipping as _r_wh_ship
import app.routers.warehouse_receiving as _r_wh_recv
import app.routers.warehouse_transfers as _r_wh_trans
import app.routers.warehouse_writeoffs as _r_wh_wo
import app.routers.receivables as _r_rec
import app.routers.notifications as _r_notif
import app.routers.claims as _r_claims
import app.routers.activity as _r_act
import app.routers.audit_log as _r_audit
import app.routers.board as _r_board
import app.routers.logistics as _r_logistics
import app.routers.leads as _r_leads
import app.routers.recon as _r_recon
import app.routers.field as _r_field
import app.routers.files as _r_files
import app.routers.public as _r_public
import app.routers.shop as _r_shop
import app.routers.landing as _r_landing
import app.routers.sync_1c as _r_sync_1c
import app.routers.sourcing as _r_sourcing
import app.routers.api_sbis as _r_api_sbis
import app.routers.hr as _r_hr
import app.routers.hr_metrics as _r_hr_metrics

for _mod in [_r_auth, _r_dash, _r_cp, _r_net, _r_analytics, _r_prod, _r_ord, _r_inv, _r_con, _r_set, _r_rep, _r_wh, _r_wh_ship, _r_wh_recv, _r_wh_trans, _r_wh_wo, _r_rec, _r_notif, _r_claims, _r_act, _r_audit, _r_board, _r_logistics, _r_leads, _r_recon, _r_field, _r_files, _r_public, _r_shop, _r_landing, _r_sync_1c, _r_sourcing, _r_api_sbis, _r_hr, _r_hr_metrics]:
    _mod.templates = _templates

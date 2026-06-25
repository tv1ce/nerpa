"""
TMS Telegram Bot — ежедневные/еженедельные/ежемесячные отчёты.

Запуск:
    python bot/main.py

Переменные окружения (.env):
    TMS_BOT_TOKEN   — токен бота от @BotFather
    TMS_CHAT_IDS    — список chat_id через запятую (напр. 123456789,987654321)
    TMS_DAILY_TIME  — время ежедневного отчёта, формат HH:MM (по умолчанию 20:00)
    TMS_WEEKLY_TIME — время пятничного отчёта, формат HH:MM (по умолчанию 18:00)
    TMS_MONTHLY_TIME— время отчёта в последний день месяца (по умолчанию 20:00)
    TMS_TZ          — временная зона (по умолчанию Europe/Moscow)
"""
from __future__ import annotations

import calendar
import logging
import os
import sys
from datetime import date, time, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.request import HTTPXRequest

# Подключаем корень проекта для импорта app.*
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import SessionLocal
from bot.metrics import (
    get_daily_metrics, get_weekly_metrics, get_monthly_metrics,
    get_callbacks_today,
)
from bot.formatters import format_daily, format_weekly, format_monthly, _esc as _esc_md

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Конфигурация ──────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("TMS_BOT_TOKEN", "")


def _parse_chat_ids(raw: str) -> list[int]:
    """Парсит TMS_CHAT_IDS, пропуская нечисловые значения без падения."""
    result = []
    for x in raw.split(","):
        x = x.strip()
        if not x:
            continue
        try:
            result.append(int(x))
        except ValueError:
            logger.warning("TMS_CHAT_IDS: пропущено нечисловое значение %r", x)
    return result


CHAT_IDS = _parse_chat_ids(os.getenv("TMS_CHAT_IDS", ""))

TZ_NAME = os.getenv("TMS_TZ", "Europe/Moscow")
try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:
    logger.warning("Неизвестный часовой пояс %r, используется Europe/Moscow", TZ_NAME)
    TZ_NAME = "Europe/Moscow"
    TZ = ZoneInfo(TZ_NAME)


def _parse_time(env_var: str, default: str) -> time:
    """Парсит HH:MM из env. При ошибке — использует значение по умолчанию."""
    raw = os.getenv(env_var, default)
    try:
        h, m = map(int, raw.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(f"время вне диапазона: {raw}")
    except (ValueError, AttributeError):
        logger.warning("%s=%r — неверный формат (ожидается HH:MM), используется %s",
                       env_var, raw, default)
        h, m = map(int, default.split(":"))
    return time(hour=h, minute=m, tzinfo=TZ)


DAILY_TIME    = _parse_time("TMS_DAILY_TIME",    "20:00")
WEEKLY_TIME   = _parse_time("TMS_WEEKLY_TIME",   "18:00")
MONTHLY_TIME  = _parse_time("TMS_MONTHLY_TIME",  "20:00")
CALLBACK_TIME = _parse_time("TMS_CALLBACK_TIME", "09:30")


# ── Отправка сообщения всем подписчикам ───────────────────────────────────────

def _report_chat_ids() -> list[int]:
    """Получатели отчётов: сначала из настроек компании (БД), иначе — из .env.
    Читается при каждой отправке, поэтому смена в настройках применяется без рестарта бота."""
    db = SessionLocal()
    try:
        from app.models import CompanySettings
        company = db.query(CompanySettings).first()
        raw = (company.tg_report_chat_ids or "") if company else ""
    except Exception as e:
        logger.error("Не удалось прочитать chat_id из настроек: %s", e)
        raw = ""
    finally:
        db.close()
    ids = _parse_chat_ids(raw)
    return ids or CHAT_IDS


def _callback_settings() -> tuple[bool, list[int]]:
    """Возвращает (enabled, chat_ids) для напоминаний о прозвонах.
    Читается при каждой отправке — изменения применяются без рестарта бота.
    Если chat_ids не заданы — падает на _report_chat_ids()."""
    db = SessionLocal()
    try:
        from app.models import CompanySettings
        company = db.query(CompanySettings).first()
        if not company:
            return True, []
        enabled = company.tg_callback_enabled
        if enabled is None:
            enabled = True
        raw = company.tg_callback_chat_ids or ""
        ids = _parse_chat_ids(raw)
        return bool(enabled), ids
    except Exception as e:
        logger.error("Не удалось прочитать настройки прозвонов: %s", e)
        return True, []
    finally:
        db.close()


async def broadcast(bot: Bot, text: str) -> None:
    for chat_id in _report_chat_ids():
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                read_timeout=20, write_timeout=20, connect_timeout=10,
            )
        except Exception as e:
            logger.error("Ошибка отправки в chat_id=%s: %s", chat_id, e)


async def broadcast_callbacks(bot: Bot, text: str) -> None:
    """Рассылка напоминаний о прозвонах — в отдельный чат (или в чат отчётов если не задан)."""
    enabled, ids = _callback_settings()
    if not enabled:
        logger.info("Напоминания о прозвонах отключены в настройках — пропуск")
        return
    targets = ids or _report_chat_ids()
    for chat_id in targets:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                read_timeout=20, write_timeout=20, connect_timeout=10,
            )
        except Exception as e:
            logger.error("Ошибка отправки прозвонов в chat_id=%s: %s", chat_id, e)


def _authorized(update: Update) -> bool:
    """Команды бота доступны только подписчикам из TMS_CHAT_IDS.
    Если список пуст — доступ запрещён всем (безопасно по умолчанию)."""
    chat = update.effective_chat
    return bool(chat and chat.id in CHAT_IDS)


async def _deny(update: Update) -> None:
    if update.message:
        await update.message.reply_text(
            "⛔ Доступ запрещён. Этот бот обслуживает только сотрудников компании.\n"
            "Сообщите администратору свой chat_id, чтобы получить доступ."
        )


# ── Генераторы отчётов ────────────────────────────────────────────────────────

def _daily_text(day: date | None = None) -> str:
    db = SessionLocal()
    try:
        return format_daily(get_daily_metrics(db, day))
    finally:
        db.close()


def _weekly_text(ref: date | None = None) -> str:
    db = SessionLocal()
    try:
        return format_weekly(get_weekly_metrics(db, ref))
    finally:
        db.close()


def _monthly_text(ref: date | None = None) -> str:
    db = SessionLocal()
    try:
        return format_monthly(get_monthly_metrics(db, ref))
    finally:
        db.close()


def _callbacks_text() -> str:
    db = SessionLocal()
    try:
        items = get_callbacks_today(db)
    finally:
        db.close()
    if not items:
        return "📞 *Перезвоны на сегодня*\n\nНа сегодня перезвонов нет 👍"
    lines = [f"📞 *Перезвоны на сегодня* — {len(items)}\n"]
    # группируем по менеджерам
    by_mgr: dict[str, list] = {}
    for it in items:
        by_mgr.setdefault(it["manager"] or "Без менеджера", []).append(it)
    for mgr, rows in by_mgr.items():
        lines.append(f"\n👤 *{_esc_md(mgr)}*")
        for r in rows:
            flag = "🔴 " if r["overdue"] else ""
            phone = f" — `{r['phone']}`" if r["phone"] else ""
            lines.append(f"  {flag}{_esc_md(r['name'])}{phone}")
    return "\n".join(lines)


async def _send_backup_file(bot: Bot, chat_id: int | str, backup_date: str) -> bool:
    """Создаёт и отправляет бекап БД в Telegram. Возвращает True если успешно."""
    import os
    from telegram.error import TelegramError
    from app.database import make_backup_copy

    tmp_path = None
    try:
        # Целостная копия через VACUUM INTO (включает данные WAL).
        # Раньше отправлялся сам tms.db без -wal — копия была устаревшей.
        tmp_path = make_backup_copy()

        # Подпись в MarkdownV2: дефисы в дате обязательно экранировать,
        # иначе Telegram отклоняет всё сообщение и бекап не уходит.
        filename = f"tms-backup-{backup_date}.db"
        caption = f"📦 *Бэкап БД* от {_esc_md(backup_date)}"
        with open(tmp_path, "rb") as f:
            await bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=filename,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        logger.info("Бекап БД отправлен в Telegram (chat_id=%s)", chat_id)
        return True
    except TelegramError as e:
        logger.error("Ошибка отправки бекапа в Telegram: %s", e)
        return False
    except Exception as e:
        logger.error("Ошибка при создании бекапа: %s", e)
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ── Scheduled callbacks ────────────────────────────────────────────────────────

async def cb_daily(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Отправка ежедневного отчёта")
    await broadcast(context.bot, _daily_text())


async def cb_weekly(context: ContextTypes.DEFAULT_TYPE) -> None:
    # Проверяем день недели в нужном часовом поясе
    # (python-telegram-bot может проверять в UTC, поэтому проверяем сами)
    from datetime import datetime
    now = datetime.now(tz=TZ)
    if now.weekday() != 4:  # 4 = Friday
        logger.debug("Сегодня не пятница (%s), пропускаем еженедельный отчёт", now.strftime("%A"))
        return
    logger.info("Отправка еженедельного отчёта")
    await broadcast(context.bot, _weekly_text())


async def cb_callbacks(context: ContextTypes.DEFAULT_TYPE) -> None:
    # Напоминания о прозвонах отправляются только в будни (пн-пт)
    from datetime import datetime
    now = datetime.now(tz=TZ)
    if now.weekday() > 4:  # 5=Saturday, 6=Sunday
        logger.debug("Выходной день (%s), пропускаем напоминание о прозвонах", now.strftime("%A"))
        return
    logger.info("Отправка напоминания о перезвонах")
    await broadcast_callbacks(context.bot, _callbacks_text())


async def cb_monthly_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запускается каждый день в MONTHLY_TIME; отправляет отчёт только в последний день месяца."""
    from datetime import datetime
    now = datetime.now(tz=TZ)
    today = now.date()
    last_day = calendar.monthrange(today.year, today.month)[1]
    if today.day == last_day:
        logger.info("Отправка ежемесячного отчёта (последний день месяца)")
        await broadcast(context.bot, _monthly_text())


async def cb_backup_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверяет нужно ли отправить бекап БД в зависимости от частоты и дня недели."""
    from datetime import datetime
    db = SessionLocal()
    try:
        from app.models import CompanySettings
        settings = db.query(CompanySettings).first()
        if not settings or not settings.backup_enabled or not settings.tg_backup_chat_id:
            return

        now = datetime.now(tz=TZ)
        today = now.date()
        frequency = settings.backup_frequency or "weekly"

        # Проверяем нужно ли отправлять бекап в зависимости от частоты
        should_backup = False
        if frequency == "daily":
            should_backup = True
        elif frequency == "weekly" and today.weekday() == 4:  # Friday
            should_backup = True
        elif frequency == "monthly":
            last_day = calendar.monthrange(today.year, today.month)[1]
            should_backup = today.day == last_day

        if should_backup:
            logger.info("Отправка бекапа БД (частота: %s)", frequency)
            await _send_backup_file(context.bot, settings.tg_backup_chat_id, today.isoformat())
    except Exception as e:
        logger.error("Ошибка при отправке бекапа: %s", e)
    finally:
        db.close()


# ── Команды бота ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    await update.message.reply_text(
        "👋 *TMS Report Bot*\n\n"
        "Доступные команды:\n"
        "/daily — отчёт за сегодня\n"
        "/weekly — отчёт за текущую неделю\n"
        "/monthly — отчёт за текущий месяц\n"
        "/callbacks — перезвоны на сегодня\n"
        "/status — статус бота и расписание\n\n"
        "📎 Пришлите файл \\(Счёт/УПД/XML\\) — приложу к заказу по номеру в имени файла "
        "\\(или укажите номер в подписи\\)\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _safe_reply(update: Update, text_fn) -> None:
    """Вызывает генератор текста отчёта с обработкой ошибок БД."""
    try:
        text = text_fn()
    except Exception as e:
        logger.exception("Ошибка формирования отчёта: %s", e)
        await update.message.reply_text(
            "⚠️ Не удалось сформировать отчёт. Попробуйте позже или обратитесь к администратору."
        )
        return
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    await _safe_reply(update, _daily_text)


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    await _safe_reply(update, _weekly_text)


async def cmd_monthly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    await _safe_reply(update, _monthly_text)


async def cmd_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    await _safe_reply(update, _callbacks_text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    today = date.today()
    last_day = calendar.monthrange(today.year, today.month)[1]
    text = (
        f"✅ *Бот запущен*\n\n"
        f"🕐 Часовой пояс: `{TZ_NAME}`\n"
        f"👥 Подписчиков: `{len(CHAT_IDS)}`\n\n"
        f"⏰ *Расписание:*\n"
        f"  Ежедневно: `{DAILY_TIME.strftime('%H:%M')}`\n"
        f"  По пятницам (недельный): `{WEEKLY_TIME.strftime('%H:%M')}`\n"
        f"  Последний день месяца (сейчас {today.day}/{last_day}): `{MONTHLY_TIME.strftime('%H:%M')}`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


# ── Приём документов из 1С (Счёт / УПД / XML) ────────────────────────────────

def _intake_channel() -> str:
    """Текущий канал приёма документов из настроек компании (off/telegram/...)."""
    db = SessionLocal()
    try:
        from app.models import CompanySettings
        c = db.query(CompanySettings).first()
        return (c.doc_intake_channel if c and c.doc_intake_channel else "off")
    except Exception:
        return "off"
    finally:
        db.close()


def _only_digits(s) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def _doc_tokens(text: str) -> list[str]:
    """Кандидаты-номера из текста: после «№», вида «НФНФ-0001», просто числа."""
    import re
    text = text or ""
    toks: list[str] = []
    toks += re.findall(r"№\s*([A-Za-zА-Яа-яЁё0-9\-]+)", text)
    toks += re.findall(r"[А-Яа-яA-Za-zЁё]{2,}-\d+", text)
    toks += re.findall(r"\d{1,}", text)
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t); out.append(t)
    return out


def _classify(filename: str) -> tuple[str, str]:
    """По имени файла → (file_type, расширение)."""
    import os as _os
    low = (filename or "").lower()
    ext = _os.path.splitext(filename or "")[1].lower() or ".pdf"
    if ext == ".xml":
        return "upd_xml", ".xml"
    if "упд" in low or "универсальн" in low:
        return "upd", ext
    if "торг" in low or ("накладн" in low and "сч" not in low):
        return "tn", ext
    if "сч" in low or "оферт" in low:
        return "invoice", ext
    return "other", ext


def _find_order(db, caption: str, filename: str):
    """Ищет заказ по номеру в подписи (приоритет) или имени файла.
    Сначала по счёту (Invoice.number), затем по номеру заказа."""
    from app.models import Invoice, Order
    for tok in _doc_tokens(caption) + _doc_tokens(filename):
        inv = db.query(Invoice).filter(Invoice.number == tok).first()
        if not inv:
            d = _only_digits(tok)
            if d:
                di = int(d)
                for cand in db.query(Invoice).all():
                    cd = _only_digits(cand.number)
                    if cd and int(cd) == di:
                        inv = cand
                        break
        if inv and inv.order_id:
            o = db.query(Order).filter(Order.id == inv.order_id).first()
            if o:
                return o, inv
        o = db.query(Order).filter(Order.number == tok).first()
        if o:
            return o, None
    return None, None


_FTYPE_LABEL = {"invoice": "Счёт", "upd": "УПД", "upd_xml": "УПД (XML)",
                "tn": "ТН", "other": "Документ"}


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Принимает файл, находит заказ по номеру и прикладывает документ."""
    if not _authorized(update):
        return await _deny(update)
    if _intake_channel() != "telegram":
        await update.message.reply_text(
            "📥 Приём документов через Telegram выключен.\n"
            "Включите в TMS: Настройки → Интеграция 1С → «Приём документов» → Telegram."
        )
        return

    doc = update.message.document
    if not doc:
        return
    filename = doc.file_name or "document"
    caption = update.message.caption or ""

    file_type, ext = _classify(filename)
    db = SessionLocal()
    try:
        order, inv = _find_order(db, caption, filename)
        if not order:
            await update.message.reply_text(
                "🤔 Не нашёл заказ по этому файлу.\n"
                "Добавьте в подпись к файлу номер счёта или заказа "
                "(например: «НФНФ-000022» или «заказ 27») и пришлите снова."
            )
            return

        # Скачиваем файл из Telegram (до 20 МБ)
        tg_file = await context.bot.get_file(doc.file_id)
        data = bytes(await tg_file.download_as_bytearray())
        if not data:
            await update.message.reply_text("⚠️ Пустой файл, не сохранил.")
            return

        from app.services.onec_client import _save_order_file
        label = f"{_FTYPE_LABEL.get(file_type, 'Документ')} {order.number}{ext}"
        key = f"tg:{file_type}:{order.id}"
        _save_order_file(db, order, file_type=file_type, ext=ext,
                         external_key=key, data=data, original_name=label,
                         source="telegram")
        db.commit()

        await update.message.reply_text(
            f"✅ Готово. «{_FTYPE_LABEL.get(file_type, 'Документ')}» приложен к заказу "
            f"№{order.number} ({order.counterparty.name if order.counterparty else '—'})."
        )
    except Exception as e:
        db.rollback()
        logger.exception("on_document: %s", e)
        await update.message.reply_text("⚠️ Не удалось сохранить файл. Попробуйте ещё раз.")
    finally:
        db.close()


# ── Точка входа ───────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        logger.error("TMS_BOT_TOKEN не задан. Укажите токен в файле .env")
        sys.exit(1)

    if not CHAT_IDS:
        logger.warning("TMS_CHAT_IDS не задан — отчёты никуда не отправятся")

    # SOCKS5-прокси через Shadowsocks (обход блокировки Telegram в РФ).
    # sslocal слушает на 127.0.0.1:1080 (systemd-сервис shadowsocks.service).
    proxy_url = os.getenv("TMS_PROXY", "socks5://127.0.0.1:1080")
    request = HTTPXRequest(proxy=proxy_url) if proxy_url else None

    builder = Application.builder().token(BOT_TOKEN)
    if request:
        builder = builder.request(request)
    app = builder.build()

    # Команды
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("daily",   cmd_daily))
    app.add_handler(CommandHandler("weekly",  cmd_weekly))
    app.add_handler(CommandHandler("monthly", cmd_monthly))
    app.add_handler(CommandHandler("callbacks", cmd_callbacks))
    app.add_handler(CommandHandler("status",  cmd_status))

    # Приём документов из 1С (Счёт/УПД/XML) — кидаешь файл боту, он цепляет к заказу
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))

    jq = app.job_queue

    # Напоминание о перезвонах — каждое утро в CALLBACK_TIME (будни)
    jq.run_daily(cb_callbacks, time=CALLBACK_TIME, days=(0, 1, 2, 3, 4), name="callbacks")

    # Ежедневный отчёт — каждый день в DAILY_TIME
    jq.run_daily(cb_daily, time=DAILY_TIME, name="daily_report")

    # Еженедельный отчёт — каждую пятницу (weekday=4) в WEEKLY_TIME
    jq.run_daily(cb_weekly, time=WEEKLY_TIME, days=(4,), name="weekly_report")

    # Ежемесячный: проверяем каждый день в MONTHLY_TIME, шлём только в последний день месяца
    jq.run_daily(cb_monthly_check, time=MONTHLY_TIME, name="monthly_check")

    # Автобекап БД: проверяем каждый день в 21:00
    backup_time = time(hour=21, minute=0, tzinfo=TZ)
    jq.run_daily(cb_backup_check, time=backup_time, name="backup_check")

    logger.info(
        "Бот запущен. Ежедневно: %s, пятница: %s, конец месяца: %s, бекап: %s",
        DAILY_TIME.strftime("%H:%M"),
        WEEKLY_TIME.strftime("%H:%M"),
        MONTHLY_TIME.strftime("%H:%M"),
        backup_time.strftime("%H:%M"),
    )

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

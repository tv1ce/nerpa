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
from telegram.ext import Application, CommandHandler, ContextTypes

# Подключаем корень проекта для импорта app.*
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import SessionLocal
from bot.metrics import get_daily_metrics, get_weekly_metrics, get_monthly_metrics
from bot.formatters import format_daily, format_weekly, format_monthly

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Конфигурация ──────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("TMS_BOT_TOKEN", "")
CHAT_IDS = [
    int(x.strip())
    for x in os.getenv("TMS_CHAT_IDS", "").split(",")
    if x.strip()
]

TZ_NAME = os.getenv("TMS_TZ", "Europe/Moscow")
TZ = ZoneInfo(TZ_NAME)


def _parse_time(env_var: str, default: str) -> time:
    raw = os.getenv(env_var, default)
    h, m = map(int, raw.split(":"))
    return time(hour=h, minute=m, tzinfo=TZ)


DAILY_TIME   = _parse_time("TMS_DAILY_TIME",   "20:00")
WEEKLY_TIME  = _parse_time("TMS_WEEKLY_TIME",  "18:00")
MONTHLY_TIME = _parse_time("TMS_MONTHLY_TIME", "20:00")


# ── Отправка сообщения всем подписчикам ───────────────────────────────────────

async def broadcast(bot: Bot, text: str) -> None:
    for chat_id in CHAT_IDS:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.error("Ошибка отправки в chat_id=%s: %s", chat_id, e)


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


# ── Scheduled callbacks ────────────────────────────────────────────────────────

async def cb_daily(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Отправка ежедневного отчёта")
    await broadcast(context.bot, _daily_text())


async def cb_weekly(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Отправка еженедельного отчёта")
    await broadcast(context.bot, _weekly_text())


async def cb_monthly_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запускается каждый день в MONTHLY_TIME; отправляет отчёт только в последний день месяца."""
    today = date.today()
    last_day = calendar.monthrange(today.year, today.month)[1]
    if today.day == last_day:
        logger.info("Отправка ежемесячного отчёта (последний день месяца)")
        await broadcast(context.bot, _monthly_text())


# ── Команды бота ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *TMS Report Bot*\n\n"
        "Доступные команды:\n"
        "/daily — отчёт за сегодня\n"
        "/weekly — отчёт за текущую неделю\n"
        "/monthly — отчёт за текущий месяц\n"
        "/status — статус бота и расписание",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_daily_text(), parse_mode=ParseMode.MARKDOWN)


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_weekly_text(), parse_mode=ParseMode.MARKDOWN)


async def cmd_monthly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_monthly_text(), parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── Точка входа ───────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        logger.error("TMS_BOT_TOKEN не задан. Укажите токен в файле .env")
        sys.exit(1)

    if not CHAT_IDS:
        logger.warning("TMS_CHAT_IDS не задан — отчёты никуда не отправятся")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Команды
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("daily",   cmd_daily))
    app.add_handler(CommandHandler("weekly",  cmd_weekly))
    app.add_handler(CommandHandler("monthly", cmd_monthly))
    app.add_handler(CommandHandler("status",  cmd_status))

    jq = app.job_queue

    # Ежедневный отчёт — каждый день в DAILY_TIME
    jq.run_daily(cb_daily, time=DAILY_TIME, name="daily_report")

    # Еженедельный отчёт — каждую пятницу (weekday=4) в WEEKLY_TIME
    jq.run_daily(cb_weekly, time=WEEKLY_TIME, days=(4,), name="weekly_report")

    # Ежемесячный: проверяем каждый день в MONTHLY_TIME, шлём только в последний день месяца
    jq.run_daily(cb_monthly_check, time=MONTHLY_TIME, name="monthly_check")

    logger.info(
        "Бот запущен. Ежедневно: %s, пятница: %s, конец месяца: %s",
        DAILY_TIME.strftime("%H:%M"),
        WEEKLY_TIME.strftime("%H:%M"),
        MONTHLY_TIME.strftime("%H:%M"),
    )

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

"""Ежедневная сводка «кому звонить сегодня» в Telegram.

Разбор точек полезен ровно в тот момент, когда менеджер начинает день, — поэтому
сводка собирается по расписанию и уходит в чат отдела продаж сама. Кнопка в
интерфейсе делает ровно то же самое, только вручную.

Все цифры считает код (app/services/outlets.py), ИИ только расставляет приоритеты
и формулирует задачи — см. промпт в app/routers/analytics.py.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.models import CompanySettings, OutletInsight
from app.services.telegram_send import ai_text_to_mdv2, parse_chat_ids, send_markdown

logger = logging.getLogger(__name__)

PRIORITY_MARKS = {"high": "🔴", "mid": "🟡", "low": "⚪"}


def generate(db: Session, user_id: int | None = None) -> list[dict]:
    """Собирает сводку через OpenRouter и сохраняет её в историю. Бросает при сбое."""
    from app.routers.analytics import AI_DIGEST_PROMPT, _digest_table, _outlets
    from app.services import openrouter_client

    outlets = _outlets(db)
    if not outlets:
        return []

    data = openrouter_client.chat_json(AI_DIGEST_PROMPT, _digest_table(outlets))
    if isinstance(data, dict):          # модель иногда заворачивает список в объект
        data = next((v for v in data.values() if isinstance(v, list)), [])

    db.add(OutletInsight(address_key=None, scope="digest",
                         text=json.dumps(data, ensure_ascii=False),
                         model=openrouter_client.MODEL, created_by=user_id))
    db.commit()
    return data


def format_message(tasks: list[dict], company: CompanySettings | None = None) -> str:
    """Текст для Telegram (MarkdownV2). Пустой список — тоже новость: всё спокойно."""
    if not tasks:
        return ai_text_to_mdv2("**Точки: кому звонить сегодня**\n\nСрочных задач нет — "
                               "все точки в графике.")
    lines = ["**Точки: кому звонить сегодня**", ""]
    for i, t in enumerate(tasks, start=1):
        mark = PRIORITY_MARKS.get(str(t.get("priority", "")).lower(), "⚪")
        client = (t.get("client") or "").strip()
        outlet = (t.get("outlet") or "").strip()
        head = f"{mark} {i}. **{client or outlet}**"
        if client and outlet:
            head += f" — {outlet}"
        lines.append(head)
        if t.get("action"):
            lines.append(f"   {t['action']}")
        if t.get("why"):
            lines.append(f"   _{t['why']}_".replace("_", ""))
        lines.append("")
    url = (company.public_url or "").strip() if company and company.public_url else ""
    if url:
        lines.append(f"Подробнее: {url.rstrip('/')}/analytics/outlets")
    return ai_text_to_mdv2("\n".join(lines).strip())


def send(db: Session, user_id: int | None = None) -> dict:
    """Собирает сводку и отправляет в настроенные чаты. Возвращает итог для UI/лога."""
    company = db.query(CompanySettings).first()
    chat_ids = parse_chat_ids((company.outlets_digest_chat_ids or "") if company else "")
    if not chat_ids and company and company.tg_report_chat_ids:
        chat_ids = parse_chat_ids(company.tg_report_chat_ids)   # запасной — общий чат отчётов
    if not chat_ids:
        return {"ok": False, "error": "Не указан чат для сводки"}

    tasks = generate(db, user_id)
    send_markdown(chat_ids, format_message(tasks, company),
                  bot_token=(company.tg_bot_token or None) if company else None)

    if company:
        company.outlets_digest_last_sent = date.today()
        db.commit()
    return {"ok": True, "tasks": len(tasks), "chats": len(chat_ids)}


def due_now(company: CompanySettings | None, now: datetime) -> bool:
    """Пора ли отправлять: включено, время наступило, сегодня ещё не отправляли."""
    if not company or not company.outlets_digest_enabled:
        return False
    if company.outlets_digest_weekdays_only and now.weekday() >= 5:
        return False
    if company.outlets_digest_last_sent == now.date():
        return False
    raw = (company.outlets_digest_time or "09:30").strip()
    try:
        hh, mm = (int(x) for x in raw.split(":", 1))
    except ValueError:
        hh, mm = 9, 30
    return (now.hour, now.minute) >= (hh, mm)


def run_scheduled() -> None:
    """Задача APScheduler: раз в несколько минут проверяет, не пора ли отправить."""
    from app.database import SessionLocal
    from app.tz import now as msk_now

    db = SessionLocal()
    try:
        company = db.query(CompanySettings).first()
        if not due_now(company, msk_now()):
            return
        result = send(db)
        logger.info("сводка по точкам: %s", result)
    except Exception as e:
        logger.error("outlets digest job error: %s", e)
    finally:
        db.close()

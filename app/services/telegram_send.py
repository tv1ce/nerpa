"""Прямая отправка сообщений в Telegram Bot API — для вызова из веб-приложения,
где нет живого объекта Bot/Application (в отличие от bot/main.py)."""
from __future__ import annotations

import re

import httpx
from app.env import getenv as env_get

BOT_TOKEN = env_get("NERPA_BOT_TOKEN", "")

# api.telegram.org недоступен напрямую с российских серверов — ходим через тот же
# локальный SOCKS-прокси, что и остальные Telegram-запросы приложения (bot/main.py,
# routers/orders.py notify_carrier).
PROXY = env_get("NERPA_PROXY", "socks5://127.0.0.1:1080") or None

_MDV2_RESERVED = r"\_*[]()~`>#+-=|{}.!"


def ai_text_to_mdv2(text: str) -> str:
    """Конвертирует свободный текст от ИИ (с **bold**-разметкой) в Telegram MarkdownV2:
    экранирует все спецсимволы вне пар \\*\\*, а сами пары превращает в одинарное
    \\*bold\\* (принятое в MarkdownV2), с экранированием их содержимого."""
    def esc(s: str) -> str:
        return "".join("\\" + ch if ch in _MDV2_RESERVED else ch for ch in s)

    parts = re.split(r"\*\*(.+?)\*\*", text, flags=re.S)
    out = []
    for i, part in enumerate(parts):
        out.append(f"*{esc(part)}*" if i % 2 == 1 else esc(part))
    return "".join(out)


def parse_chat_ids(raw: str) -> list[int]:
    result = []
    for x in raw.split(","):
        x = x.strip()
        if x:
            try:
                result.append(int(x))
            except ValueError:
                pass
    return result


def _pack(units: list[str], sep: str, limit: int) -> list[str]:
    """Жадно объединяет units в чанки до limit символов, не разрывая ни один
    отдельный элемент (если он сам длиннее limit — идёт в чанк один)."""
    parts, buf, length = [], [], 0
    for u in units:
        if length + len(u) + len(sep) > limit and buf:
            parts.append(sep.join(buf))
            buf, length = [], 0
        buf.append(u)
        length += len(u) + len(sep)
    if buf:
        parts.append(sep.join(buf))
    return parts


def _hard_slice(s: str, limit: int) -> list[str]:
    """Режет строку жёстко по limit символов — на случай, если даже одна
    строка длиннее лимита. Не разрывает экранирующий '\\' от символа."""
    parts = []
    while s:
        cut = limit
        if cut < len(s) and s[cut - 1] == "\\":
            cut -= 1
        parts.append(s[:cut])
        s = s[cut:]
    return parts


def _split(text: str, limit: int = 3500) -> list[str]:
    """Режет длинный MarkdownV2-текст на чанки до limit символов — сначала по
    границам пустых строк, затем (если один «абзац» сам длиннее limit —
    например, блок с длинными ответами по гравитации) по одиночным строкам,
    и в крайнем случае жёстко по символам. Так ни один чанк не превысит
    ограничение Telegram (4096) даже при очень длинном абзаце."""
    if len(text) <= limit:
        return [text]

    parts = []
    for para in _pack(text.split("\n\n"), "\n\n", limit):
        if len(para) <= limit:
            parts.append(para)
            continue
        for chunk in _pack(para.split("\n"), "\n", limit):
            if len(chunk) <= limit:
                parts.append(chunk)
            else:
                parts.extend(_hard_slice(chunk, limit))
    return parts


def send_markdown(chat_ids: list[int], text: str, bot_token: str | None = None) -> None:
    token = (bot_token or BOT_TOKEN or "").strip()
    if not token:
        raise RuntimeError("Токен Telegram-бота не задан (NERPA_BOT_TOKEN или Настройки → Telegram-бот)")
    for chat_id in chat_ids:
        for chunk in _split(text):
            r = httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "MarkdownV2"},
                timeout=20,
                proxy=PROXY,
            )
            r.raise_for_status()


def send_topic_message(chat_id: int, text: str, bot_token: str | None = None,
                        thread_id: int | str | None = None) -> None:
    """Отправляет обычный (не MarkdownV2) текст в чат — опционально в конкретный
    топик супергруппы-форума (message_thread_id). Используется для простых
    шаблонных уведомлений склада, где не нужно экранирование разметки."""
    token = (bot_token or BOT_TOKEN or "").strip()
    if not token:
        raise RuntimeError("Токен Telegram-бота не задан (NERPA_BOT_TOKEN или Настройки → Telegram-бот)")
    for chunk in _split(text):
        payload = {"chat_id": chat_id, "text": chunk}
        if thread_id:
            payload["message_thread_id"] = int(thread_id)
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            # 5с, не 20: уведомление — не главное в запросе, и когда SOCKS-прокси
            # недоступен, ждать его четверть минуты незачем.
            timeout=5,
            proxy=PROXY,
        )
        r.raise_for_status()


def hr_survey_notify_target(db) -> tuple[list[int], str]:
    """(chat_id, токен бота) для уведомления «сотрудник прошёл опрос».

    Пустой список — уведомление выключено или не настроены чат/токен. Чат
    отдельный только если нужен; не задан — берём чат HR-отчёта, затем общий
    чат отчётов (та же лесенка, что у пятничных напоминаний по метрике).

    Читается в момент запроса, пока сессия БД жива: сама отправка уходит в
    фон и до БД уже не дотягивается (см. send_plain_safe)."""
    from app.models import CompanySettings
    s = db.query(CompanySettings).first()
    if not s or not s.hr_survey_notify_enabled:
        return [], ""
    ids = (parse_chat_ids(s.hr_survey_notify_chat_ids or "")
           or parse_chat_ids(s.tg_hr_report_chat_ids or "")
           or parse_chat_ids(s.tg_report_chat_ids or ""))
    token = (s.tg_bot_token or "").strip() or BOT_TOKEN
    return (ids, token) if ids and token else ([], "")


def send_plain_safe(chat_ids: list[int], text: str, bot_token: str) -> None:
    """Простой текст в несколько чатов; наружу не бросает.

    Для фоновых уведомлений, где сбой Telegram не должен влиять на действие
    пользователя. Telegram доступен только через SOCKS-прокси, а тот регулярно
    отваливается на минуту-другую — поэтому «не смогли отправить» здесь
    нормальная ситуация, которую достаточно записать в лог.

    Без MarkdownV2: в тексте ФИО и названия с «-», «_», «.», где одна
    пропущенная экранировка отменяет всё сообщение целиком."""
    import logging
    logger = logging.getLogger(__name__)
    for chat_id in chat_ids:
        try:
            send_topic_message(chat_id, text, bot_token=bot_token)
        except Exception:
            logger.error("send_plain_safe: не отправилось в chat_id=%s", chat_id, exc_info=True)


def notify_warehouse_group(db, topic: str, text: str) -> None:
    """Уведомление в складскую Telegram-супергруппу (топики «Поступления сырья» /
    «Собранные заказы» / «Отгрузки»). topic — 'receiving' / 'assembled' / 'shipped'.
    Никогда не бросает исключение наружу — сбой Telegram не должен ломать
    основной складской флоу (приёмку/сборку/отгрузку)."""
    import logging
    logger = logging.getLogger(__name__)
    try:
        from app.models import CompanySettings
        s = db.query(CompanySettings).first()
        if not s or not s.tg_warehouse_enabled or not s.tg_warehouse_chat_id:
            return
        token = (s.tg_bot_token or "").strip() or BOT_TOKEN
        if not token:
            return
        thread_id = {
            "receiving": s.tg_warehouse_topic_receiving,
            "assembled": s.tg_warehouse_topic_assembled,
            "shipped":   s.tg_warehouse_topic_shipped,
        }.get(topic)
        send_topic_message(int(s.tg_warehouse_chat_id), text, bot_token=token, thread_id=thread_id)
    except Exception:
        logger.error("notify_warehouse_group(%s) failed", topic, exc_info=True)


def notify_warehouse_group_bg(topic: str, text: str) -> None:
    """То же уведомление, но со своей сессией БД — для запуска из BackgroundTasks.

    Сессию запроса сюда передавать нельзя: фоновая задача стартует уже после
    ответа, а get_db() закрывает сессию в finally по завершении запроса. Своя
    сессия живёт ровно на время отправки и закрывается здесь же.
    """
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        notify_warehouse_group(db, topic, text)
    finally:
        db.close()


def fetch_recent_topic_updates(bot_token: str, limit: int = 50) -> list[dict]:
    """Диагностика: последние апдейты бота (getUpdates) — чтобы найти chat_id и
    message_thread_id нужных топиков супергруппы, не залезая в API вручную.
    Просит пользователя написать/переслать по одному сообщению в каждый топик,
    затем показывает (chat_id, топик/thread_id, текст, время) — administrator
    сам сопоставляет thread_id с названием топика по содержимому сообщения.
    Возвращает последние записи в обратном хронологическом порядке.
    """
    token = (bot_token or BOT_TOKEN or "").strip()
    if not token:
        raise RuntimeError("Токен Telegram-бота не задан")
    r = httpx.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params={"limit": limit, "timeout": 0},
        timeout=20,
        proxy=PROXY,
    )
    r.raise_for_status()
    data = r.json()
    out = []
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post")
        if not msg or "chat" not in msg:
            continue
        chat = msg["chat"]
        if chat.get("type") not in ("group", "supergroup"):
            continue
        out.append({
            "chat_id": chat.get("id"),
            "chat_title": chat.get("title"),
            "thread_id": msg.get("message_thread_id"),
            "topic_created_name": (msg.get("forum_topic_created") or {}).get("name"),
            "text": (msg.get("text") or "")[:80],
            "date": msg.get("date"),
        })
    out.sort(key=lambda x: x["date"] or 0, reverse=True)
    return out

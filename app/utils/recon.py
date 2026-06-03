"""Разведка ЛПР и обогащение базы прозвона по открытым источникам РФ.

Два движка:
  1. Локальное обогащение (`enrich_lead_local`) — без интернета: до-вытаскивает
     соцсети/сайт/почту/телефон из исходной строки импорта, нормализует телефон,
     находит ИНН в тексте, пересобирает признак «сеть».
  2. DaData (`dadata_suggest`, `enrich_lead_dadata`) — официальное API ЕГРЮЛ/ЕГРИП:
     по названию или ИНН возвращает руководителя (ЛПР), реквизиты, статус, ОКВЭД.

Плюс `build_source_links` — глубокие ссылки на Rusprofile / Checko / List-Org /
ЕГРЮЛ ФНС / 2ГИС / Яндекс.Карты / поиск соцсетей для ручной доразведки.
"""
import json
import os
import re
from datetime import datetime
from urllib.parse import quote_plus

import httpx

from app.routers.leads import (
    _extract_socials, _ensure_scheme, _norm_phone, _normalize_brand,
    PHONE_PATTERN,
)

DADATA_SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"

# ИНН: ЮЛ — 10 цифр, ИП/физлицо — 12 цифр
_INN_PATTERN = re.compile(r"\b(\d{10}|\d{12})\b")
_OGRN_PATTERN = re.compile(r"\b(\d{13}|\d{15})\b")

# Статусы DaData → человекочитаемо
_STATUS_RU = {
    "ACTIVE": "Действующая",
    "LIQUIDATING": "Ликвидируется",
    "LIQUIDATED": "Ликвидирована",
    "REORGANIZING": "Реорганизуется",
    "BANKRUPT": "Банкротство",
}
STATUS_BADGE = {
    "Действующая": "success",
    "Ликвидируется": "warning",
    "Ликвидирована": "danger",
    "Реорганизуется": "info",
    "Банкротство": "danger",
}

# Статусы, при которых точку автоматически убираем из активной базы прозвона
DEAD_STATUSES = {"Ликвидируется", "Ликвидирована", "Банкротство"}


# ── Телефон в красивый формат РФ ─────────────────────────────────────────────

def pretty_phone(phone: str) -> str:
    """+7 (XXX) XXX-XX-XX для 11-значных номеров, иначе как есть."""
    d = _norm_phone(phone)
    if len(d) == 11 and d[0] == "7":
        return f"+7 ({d[1:4]}) {d[4:7]}-{d[7:9]}-{d[9:11]}"
    return (phone or "").strip()


def _valid_inn(inn: str) -> bool:
    """Проверка контрольных цифр ИНН (10 или 12 знаков)."""
    if not inn or not inn.isdigit():
        return False
    if len(inn) == 10:
        w = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        c = sum(int(inn[i]) * w[i] for i in range(9)) % 11 % 10
        return c == int(inn[9])
    if len(inn) == 12:
        w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        c1 = sum(int(inn[i]) * w1[i] for i in range(10)) % 11 % 10
        c2 = sum(int(inn[i]) * w2[i] for i in range(11)) % 11 % 10
        return c1 == int(inn[10]) and c2 == int(inn[11])
    return False


def _raw_blob(lead) -> str:
    """Склеивает все текстовые поля лида + исходную строку импорта."""
    parts = [
        lead.name, lead.address, lead.phone, lead.email, lead.website,
        lead.vk, lead.instagram, lead.telegram, lead.whatsapp, lead.notes,
    ]
    if lead.raw:
        try:
            parts += [str(v) for v in json.loads(lead.raw).values()]
        except (ValueError, TypeError):
            parts.append(lead.raw)
    return " ".join(p for p in parts if p)


# ── 1. Локальное обогащение (без сети) ───────────────────────────────────────

_EMAIL_PATTERN = re.compile(r"[\w.\-+]+@([\w\-]+\.[a-zA-Zрф]{2,})", re.I)


def enrich_lead_local(lead) -> list[str]:
    """До-вытаскивает данные из уже импортированной строки. Меняет lead на месте.
    Возвращает список названий заполненных полей (для отчёта)."""
    blob = _raw_blob(lead)
    changed = []

    # Домены из e-mail — чтобы не принять их за «сайт»
    mail_domains = {m.group(1).lower() for m in _EMAIL_PATTERN.finditer(blob)}

    # E-mail (раньше сайта, т.к. от него зависит фильтр сайта)
    if not lead.email:
        m = _EMAIL_PATTERN.search(blob)
        if m:
            lead.email = m.group(0)[:150]
            changed.append("email")

    # Соцсети / сайт из всего текста строки
    socials = _extract_socials([blob])
    for key in ("vk", "instagram", "telegram", "whatsapp", "website"):
        val = socials.get(key)
        if not val or getattr(lead, key, None):
            continue
        # сайт не должен совпадать с доменом из e-mail
        if key == "website" and any(d in val.lower() for d in mail_domains):
            continue
        setattr(lead, key, _ensure_scheme(val)[:500])
        changed.append(key)

    # Телефон
    if not lead.phone:
        m = PHONE_PATTERN.search(blob)
        if m:
            lead.phone = m.group(0).strip()[:150]
            changed.append("phone")
    if lead.phone:
        pretty = pretty_phone(lead.phone)
        if pretty and pretty != lead.phone:
            lead.phone = pretty[:150]
            changed.append("phone_fmt")

    # ИНН прямо в тексте строки (если парсер занёс колонку с ИНН в raw)
    if not lead.inn:
        for m in _INN_PATTERN.finditer(blob):
            if _valid_inn(m.group(1)):
                lead.inn = m.group(1)
                changed.append("inn")
                break

    # Бренд для группировки сетей
    if not lead.brand and lead.name:
        lead.brand = (_normalize_brand(lead.name) or lead.name.lower().strip())[:300]
        changed.append("brand")

    if changed:
        lead.enrich_source = lead.enrich_source or "local"
    return changed


def _inn_ok(inn: str) -> bool:
    """ИНН пригоден как ключ группировки: 10 или 12 цифр.
    Контрольную сумму тут НЕ проверяем — для уже сохранённого ИНН достаточно
    точного совпадения строки (валидация нужна лишь при извлечении из текста)."""
    return bool(inn) and inn.isdigit() and len(inn) in (10, 12)


def network_key(lead) -> str:
    """Ключ объединения в сеть: приоритет — ИНН (одни реквизиты = одна сеть),
    иначе нормализованный бренд."""
    if _inn_ok(lead.inn or ""):
        return "инн:" + lead.inn
    return lead.brand or ""


def reclassify_networks(leads) -> int:
    """Пересобирает is_network / network_size. Объединяет точки по реквизитам:
    одинаковый ИНН → одна сеть (даже если названия слегка отличаются).
    Возвращает число точек, у которых признак изменился."""
    counts: dict[str, int] = {}
    for l in leads:
        k = network_key(l)
        if k:
            counts[k] = counts.get(k, 0) + 1
    touched = 0
    for l in leads:
        k = network_key(l)
        size = counts.get(k, 1) if k else 1
        is_net = bool(k) and size >= 2
        if l.is_network != is_net or (l.network_size or 1) != size:
            l.is_network = is_net
            l.network_size = size
            touched += 1
    return touched


# ── Скоринг лидов «теплота» (приоритизация прозвона) ─────────────────────────

def lead_score(lead) -> int:
    """0–100: насколько точка перспективна для звонка прямо сейчас."""
    if lead.company_status in DEAD_STATUSES:
        return 0
    s = 0
    if lead.director:                       s += 30   # есть ЛПР — знаем кому звонить
    if lead.phone:                          s += 25   # есть телефон
    if lead.company_status == "Действующая": s += 15
    if any((lead.vk, lead.instagram, lead.telegram, lead.whatsapp, lead.website)):
        s += 10                                       # есть онлайн-присутствие
    if (lead.call_status or "new") == "new": s += 15   # ещё не трогали
    if lead.email:                          s += 5
    return min(s, 100)


def score_label(score: int) -> tuple[str, str]:
    """(подпись, bootstrap-цвет) для бейджа температуры."""
    if score >= 75:  return "🔥 Горячий", "danger"
    if score >= 50:  return "Тёплый", "warning"
    if score >= 25:  return "Прохладный", "info"
    return "Холодный", "secondary"


# ── Дедупликация: поиск истинных дублей ──────────────────────────────────────

def _norm_addr(addr: str) -> str:
    a = (addr or "").lower().replace("ё", "е")
    a = re.sub(r"[^\wа-я0-9]+", " ", a, flags=re.U)
    return re.sub(r"\s+", " ", a).strip()


def dedup_key(lead):
    """Ключ истинного дубля точки:
    ИНН+адрес (одно юрлицо на одном адресе) ИЛИ нормализованный телефон.
    None — если уникальных признаков нет (не трогаем)."""
    inn = lead.inn if _inn_ok(lead.inn or "") else ""
    addr = _norm_addr(lead.address)
    if inn and addr:
        return ("inn_addr", inn, addr)
    ph = _norm_phone(lead.phone)
    if ph and len(ph) >= 10:
        return ("phone", ph)
    if inn and not addr:
        return ("inn", inn)
    return None


def _fill_score(lead) -> int:
    """Сколько полезных полей заполнено — для выбора «лучшей» записи при склейке."""
    return sum(1 for v in (
        lead.phone, lead.email, lead.director, lead.inn, lead.address,
        lead.vk, lead.instagram, lead.telegram, lead.whatsapp, lead.website,
        lead.contact_person, lead.notes,
    ) if v)


# Приоритет статусов прозвона при склейке (сохраняем самый «продвинутый»)
_STATUS_RANK = {
    "new": 0, "invalid": 1, "no_answer": 2, "refused": 3, "callback": 4,
    "thinking": 5, "interested": 6, "deal": 7,
}


def find_duplicate_groups(leads):
    """Группирует активные лиды по dedup_key, возвращает только группы с дублями:
    [(survivor, [dupes...]), ...]. survivor — запись с наибольшим числом полей."""
    from collections import defaultdict
    buckets = defaultdict(list)
    for l in leads:
        k = dedup_key(l)
        if k:
            buckets[k].append(l)
    groups = []
    for k, items in buckets.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda x: (_fill_score(x),
                                  _STATUS_RANK.get(x.call_status or "new", 0),
                                  -(x.id or 0)), reverse=True)
        groups.append((items[0], items[1:]))
    return groups


# ── Телефоны точки (для подсветки контактов ЛПР) ─────────────────────────────

def extract_phones(lead) -> list[str]:
    """Все телефоны точки из поля phone и исходной строки импорта,
    нормализованные и красиво отформатированные, без дублей."""
    candidates = [lead.phone or ""]
    if lead.raw:
        try:
            data = json.loads(lead.raw)
            for k, v in data.items():
                if v and any(w in str(k).lower()
                             for w in ("тел", "сотов", "phone", "моб", "контакт", "whats")):
                    candidates.append(str(v))
        except (ValueError, TypeError):
            pass
    blob = "  ".join(c for c in candidates if c)
    out, seen = [], set()
    for m in PHONE_PATTERN.finditer(blob):
        d = re.sub(r"\D", "", m.group(0))
        if len(d) == 10:
            d = "7" + d
        if len(d) == 11 and d[0] == "8":
            d = "7" + d[1:]
        if len(d) == 11 and d not in seen:
            seen.add(d)
            out.append(pretty_phone(d))
    return out


# ── 2. DaData — ЛПР и реквизиты ──────────────────────────────────────────────

def get_dadata_token(settings) -> str | None:
    """Токен DaData по приоритету:
    1) настройки раздела (CompanySettings.dadata_token),
    2) переменная окружения DADATA_TOKEN,
    3) общий ключ из модуля контрагентов (тот же аккаунт, что и автозаполнение по ИНН).
    """
    if settings and getattr(settings, "dadata_token", None):
        return settings.dadata_token.strip()
    if os.environ.get("DADATA_TOKEN"):
        return os.environ["DADATA_TOKEN"]
    try:
        from app.routers.counterparties import DADATA_TOKEN as _CP_TOKEN
        return _CP_TOKEN or None
    except Exception:
        return None


async def dadata_suggest(token: str, query: str, count: int = 5) -> list[dict]:
    """Запрос к suggestions/party. Возвращает список «сырых» suggestion'ов."""
    headers = {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"query": query, "count": count}
    async with httpx.AsyncClient(timeout=12) as client:
        r = await client.post(DADATA_SUGGEST_URL, json=payload, headers=headers)
        r.raise_for_status()
        return r.json().get("suggestions", [])


def parse_party(sug: dict) -> dict:
    """Разбирает suggestion DaData в плоский словарь полей ЛПР/реквизитов."""
    d = sug.get("data", {}) or {}
    mgmt = d.get("management") or {}
    fio = d.get("fio") or {}          # для ИП
    name = d.get("name") or {}
    state = d.get("state") or {}
    okveds = d.get("okved", "")
    okved_name = d.get("okved_type", "")

    # ЛПР: у ЮЛ — management, у ИП — сам ИП (fio)
    if mgmt.get("name"):
        director = mgmt.get("name")
        post = mgmt.get("post") or "Руководитель"
    elif fio:
        director = " ".join(p for p in (fio.get("surname"), fio.get("name"), fio.get("patronymic")) if p)
        post = "Индивидуальный предприниматель"
    else:
        director, post = "", ""

    status = _STATUS_RU.get(state.get("status", ""), state.get("status", "") or "")
    reg = state.get("registration_date")
    reg_str = ""
    if reg:
        try:
            reg_str = datetime.fromtimestamp(reg / 1000).strftime("%d.%m.%Y")
        except (ValueError, OSError, TypeError):
            reg_str = ""

    okved_full = okveds
    if okveds and d.get("okved"):
        # оквэд приходит кодом; описание DaData в suggestions не отдаёт стабильно —
        # сохраняем код, а название добьём из value, если оно есть
        okved_full = okveds

    return {
        "value": sug.get("value", ""),
        "inn": d.get("inn", ""),
        "kpp": d.get("kpp", ""),
        "ogrn": d.get("ogrn", ""),
        "company_name_full": (name.get("full_with_opf") or sug.get("value") or "")[:500],
        "director": (director or "")[:200],
        "director_post": (post or "")[:200],
        "company_status": status[:30],
        "okved": (okved_full or "")[:300],
        "registration_date": reg_str,
        "type": d.get("type", ""),     # LEGAL / INDIVIDUAL
        "address": (d.get("address") or {}).get("value", ""),
    }


def _suggestion_score(sug: dict, city: str) -> int:
    """Насколько результат DaData подходит точке: действующая + тот же город +
    есть руководитель → выше. Чтобы не цеплять однофамильцев-пустышек."""
    d = sug.get("data", {}) or {}
    score = 0
    if (d.get("state") or {}).get("status") == "ACTIVE":
        score += 4
    addr = ((d.get("address") or {}).get("value") or "").lower()
    if city and city.lower() in addr:
        score += 3
    if (d.get("management") or {}).get("name") or d.get("fio"):
        score += 2   # есть ЛПР — это и нужно
    return score


async def enrich_lead_dadata(token: str, lead) -> dict | None:
    """Ищет организацию по ИНН (приоритет) или названию+городу, выбирает
    лучший результат (действующая, тот же город, с руководителем) и заполняет ЛПР."""
    by_inn = bool(lead.inn and _valid_inn(lead.inn))
    query = lead.inn if by_inn else " ".join(p for p in (lead.name, lead.city) if p)
    if not query:
        return None
    sugs = await dadata_suggest(token, query, count=8)
    if not sugs:
        return None

    # По ИНН — это и есть точная организация. По названию — выбираем лучший матч.
    best = sugs[0] if by_inn else max(sugs, key=lambda s: _suggestion_score(s, lead.city or ""))
    parsed = parse_party(best)
    apply_party(lead, parsed)
    return parsed


def apply_party(lead, parsed: dict) -> None:
    """Записывает распарсенные данные DaData в лид (на месте).
    Выносится отдельно, чтобы переиспользовать кэш при пакетном обогащении
    (один запрос на сеть/компанию → применяем ко всем точкам с теми же реквизитами)."""
    lead.inn = parsed["inn"] or lead.inn
    lead.kpp = parsed["kpp"] or lead.kpp
    lead.ogrn = parsed["ogrn"] or lead.ogrn
    lead.company_name_full = parsed["company_name_full"] or lead.company_name_full
    lead.director = parsed["director"] or lead.director
    lead.director_post = parsed["director_post"] or lead.director_post
    lead.company_status = parsed["company_status"] or lead.company_status
    lead.okved = parsed["okved"] or lead.okved
    lead.registration_date = parsed["registration_date"] or lead.registration_date
    if not lead.contact_person and parsed["director"]:
        lead.contact_person = parsed["director"][:150]
    # Единый бренд по реквизитам — чтобы точки одной компании склеивались в сеть
    if parsed["inn"]:
        lead.brand = ("инн:" + parsed["inn"])
    lead.enriched_at = datetime.now()
    lead.enrich_source = "dadata"


# ── 3. Дип-линки на источники РФ (ручная доразведка) ─────────────────────────

def _ya(text: str) -> str:
    """Ссылка на поиск Яндекса по произвольному запросу."""
    return "https://yandex.ru/search/?text=" + quote_plus(text.strip())


def build_source_links(lead) -> list[dict]:
    """Готовые рабочие дип-ссылки для доразведки по конкретной точке.
    Все форматы проверены: реестры принимают GET-запрос и сразу открывают
    карточку/выдачу; соцсети ищем через Яндекс (site:) — надёжнее прямых
    URL соцсетей, которые требуют логина."""
    name = (lead.name or "").strip()
    city = (lead.city or "").strip()
    inn = (lead.inn or "").strip()
    director = (lead.director or "").strip()
    phone = _norm_phone(lead.phone or "")
    name_city = f"{name} {city}".strip()
    nc = quote_plus(name_city)
    # для реестров: по ИНН точнее, иначе по названию+городу
    reg = quote_plus(inn if inn else name_city)
    by_inn = bool(inn)
    sfx = " по ИНН" if by_inn else " по названию"
    links = []

    # ── Телефон ЛПР (приоритет) — целевой поиск личного номера руководителя ──
    if director:
        links += [
            {"group": "Телефон ЛПР", "title": f"«{director}» + телефон", "icon": "bi-telephone-plus",
             "url": _ya(f'"{director}" {name} {city} телефон')},
            {"group": "Телефон ЛПР", "title": f"«{director}» — контакты", "icon": "bi-person-lines-fill",
             "url": _ya(f'"{director}" {city} контакты email')},
            {"group": "Телефон ЛПР", "title": f"«{director}» в реестрах (ИП/учредитель)", "icon": "bi-person-vcard",
             "url": f"https://www.rusprofile.ru/search?query={quote_plus(director)}"},
            {"group": "Телефон ЛПР", "title": f"«{director}» — соцсети", "icon": "bi-people",
             "url": _ya(f'"{director}" {city} site:vk.com OR site:t.me')},
        ]

    # ── Реквизиты и ЛПР (все принимают GET, открывают карточку/выдачу) ──
    links += [
        {"group": "Реквизиты и ЛПР", "title": "Rusprofile" + sfx, "icon": "bi-building",
         "url": f"https://www.rusprofile.ru/search?query={reg}"},
        {"group": "Реквизиты и ЛПР", "title": "Checko" + sfx, "icon": "bi-clipboard-data",
         "url": f"https://checko.ru/search?query={reg}"},
        {"group": "Реквизиты и ЛПР", "title": "За Честный Бизнес" + sfx, "icon": "bi-shield-check",
         "url": f"https://zachestnyibiznes.ru/search?query={reg}"},
        {"group": "Реквизиты и ЛПР", "title": "List-Org" + sfx, "icon": "bi-list-columns",
         "url": f"https://www.list-org.com/search?type=all&val={reg}"},
    ]
    if by_inn:
        # СБИС открывает карточку контрагента прямо по ИНН
        links.append({"group": "Реквизиты и ЛПР", "title": "СБИС — карточка по ИНН",
                      "icon": "bi-card-checklist", "url": f"https://sbis.ru/contragents/{inn}"})
        # Официальная выписка ЕГРЮЛ — ФНС не принимает GET, поэтому ведём на
        # «Прозрачный бизнес» (выписку формируют по введённому ИНН вручную)
        links.append({"group": "Реквизиты и ЛПР", "title": "ЕГРЮЛ · ФНС (выписка, ввести ИНН)",
                      "icon": "bi-bank", "url": "https://egrul.nalog.ru/"})

    # ── Карты и контакты ──
    links += [
        {"group": "Карты и контакты", "title": "2ГИС", "icon": "bi-geo-alt",
         "url": f"https://2gis.ru/search/{nc}"},
        {"group": "Карты и контакты", "title": "Яндекс.Карты", "icon": "bi-pin-map",
         "url": f"https://yandex.ru/maps/?text={nc}"},
        {"group": "Карты и контакты", "title": "Сайт и телефон (Яндекс)", "icon": "bi-search",
         "url": _ya(f"{name_city} официальный сайт телефон")},
    ]
    if phone and len(phone) >= 10:
        pretty = pretty_phone(phone)
        links.append({"group": "Карты и контакты", "title": f"Поиск по телефону {pretty}",
                      "icon": "bi-telephone-inbound", "url": _ya(pretty)})

    # ── Соцсети и мессенджеры (через Яндекс site: — без логина) ──
    links += [
        {"group": "Соцсети и мессенджеры", "title": "ВКонтакте", "icon": "bi-stack",
         "url": _ya(f"{name_city} site:vk.com")},
        {"group": "Соцсети и мессенджеры", "title": "Telegram", "icon": "bi-telegram",
         "url": _ya(f"{name_city} site:t.me")},
        {"group": "Соцсети и мессенджеры", "title": "Instagram", "icon": "bi-instagram",
         "url": _ya(f"{name_city} site:instagram.com")},
    ]
    return links


def group_links(links: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for l in links:
        out.setdefault(l["group"], []).append(l)
    return out

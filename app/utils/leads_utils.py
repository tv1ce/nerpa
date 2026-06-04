"""Общие утилиты для работы с лидами (прозвон + разведка ЛПР).

Вынесены из app/routers/leads.py, чтобы app/utils/recon.py мог
импортировать их без циклической зависимости utils → router.
"""
import re

PHONE_PATTERN = re.compile(r"(?:\+?\d[\s\-()]?){7,15}\d")

URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?[\w\-]+\.[a-zA-Zрф]{2,}(?:/[\w.\-/?=&%]*)?",
    re.I,
)

SOCIAL_PATTERNS = {
    "vk":        re.compile(r"vk\.com/[\w.\-]+", re.I),
    "instagram": re.compile(r"instagram\.com/[\w.\-]+", re.I),
    "telegram":  re.compile(r"(?:t\.me|telegram\.me)/[\w.\-]+", re.I),
    "whatsapp":  re.compile(r"(?:wa\.me|whatsapp\.com)/[\w+.\-]+", re.I),
}

_BRAND_NOISE = re.compile(
    r'\b(ооо|оао|зао|пао|ип|ао|тд|тк|нко|общество|с ограниченной|ответственностью|'
    r'индивидуальный|предприниматель|кофейня|кафе|кондитерская|пекарня|ресторан|бар|'
    r'магазин|сеть|сети|филиал|точка|street|coffee|cafe|shop|bakery)\b',
    re.I,
)


def _normalize_brand(name: str) -> str:
    """Грубая нормализация названия в «бренд» для группировки сетей."""
    if not name:
        return ""
    s = name.lower().replace("ё", "е")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[«»\"'`]", " ", s)
    s = _BRAND_NOISE.sub(" ", s)
    s = re.sub(r"\d+", " ", s)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.U)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _extract_socials(cells: list[str]) -> dict:
    """Достаёт соцсети / сайт / телефон из всех ячеек строки."""
    blob = " ".join(c for c in cells if c)
    found = {}
    for key, pat in SOCIAL_PATTERNS.items():
        m = pat.search(blob)
        if m:
            url = m.group(0)
            if not url.startswith("http"):
                url = "https://" + url
            found[key] = url
    for m in URL_PATTERN.finditer(blob):
        url = m.group(0)
        low = url.lower()
        if any(d in low for d in ("vk.com", "vk.ru", "instagram.com", "t.me",
                                  "telegram.me", "wa.me", "whatsapp.com",
                                  "@", "mail.")):
            continue
        if not url.startswith("http"):
            url = "https://" + url
        found.setdefault("website", url)
        break
    return found


def _ensure_scheme(url: str) -> str:
    """Добавляет https:// к ссылке, если схемы нет."""
    url = (url or "").strip()
    if not url or ("@" in url and "/" not in url):
        return url
    if url.startswith(("http://", "https://")):
        return url
    if "." in url or url.startswith("t.me") or "vk.com" in url:
        return "https://" + url.lstrip("/")
    return url


def _norm_phone(phone: str) -> str:
    """Нормализует телефон до цифр. 8XXX → 7XXX."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    return digits

"""Перевод числа в рубли прописью (для счетов)."""

_ones = [
    "", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять",
    "десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
    "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать",
]
_ones_f = [
    "", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять",
    "десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
    "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать",
]
_tens = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_hundreds = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]


def _chunk(n: int, feminine: bool) -> str:
    parts = []
    h = n // 100
    rest = n % 100
    if h:
        parts.append(_hundreds[h])
    if rest < 20:
        word = (_ones_f if feminine else _ones)[rest]
        if word:
            parts.append(word)
    else:
        t = rest // 10
        o = rest % 10
        parts.append(_tens[t])
        word = (_ones_f if feminine else _ones)[o]
        if word:
            parts.append(word)
    return " ".join(parts)


def _plural(n: int, forms: tuple) -> str:
    n = abs(n) % 100
    n1 = n % 10
    if 11 <= n <= 19:
        return forms[2]
    if n1 == 1:
        return forms[0]
    if 2 <= n1 <= 4:
        return forms[1]
    return forms[2]


def amount_to_words(amount: float) -> str:
    rubles = int(amount)
    kopecks = round((amount - rubles) * 100)

    billions = rubles // 1_000_000_000
    millions = (rubles % 1_000_000_000) // 1_000_000
    thousands = (rubles % 1_000_000) // 1_000
    remainder = rubles % 1_000

    parts = []

    if billions:
        parts.append(f"{_chunk(billions, False)} {_plural(billions, ('миллиард', 'миллиарда', 'миллиардов'))}")
    if millions:
        parts.append(f"{_chunk(millions, False)} {_plural(millions, ('миллион', 'миллиона', 'миллионов'))}")
    if thousands:
        parts.append(f"{_chunk(thousands, True)} {_plural(thousands, ('тысяча', 'тысячи', 'тысяч'))}")
    if remainder or not parts:
        parts.append(f"{_chunk(remainder, False)} {_plural(remainder, ('рубль', 'рубля', 'рублей'))}")

    rub_str = " ".join(parts)
    # Capitalize first letter
    rub_str = rub_str[0].upper() + rub_str[1:] if rub_str else "Ноль рублей"
    kop_str = f"{kopecks:02d} {_plural(kopecks, ('копейка', 'копейки', 'копеек'))}"
    return f"{rub_str} {kop_str}"

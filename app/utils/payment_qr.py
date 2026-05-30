"""Платёжный QR-код по ГОСТ Р 56042-2014 (формат СБП/банковских реквизитов).

Возвращает PNG-картинку с QR-кодом, который банковские приложения распознают
как реквизиты для оплаты счёта. Если библиотека qrcode не установлена — модуль
тихо отключается (generate_payment_qr вернёт None), PDF при этом строится без QR.
"""
import io

try:
    import qrcode
    _HAS_QR = True
except Exception:          # pragma: no cover - библиотека опциональна
    _HAS_QR = False


def _clean(v) -> str:
    return str(v).strip() if v else ""


def build_payment_string(company, amount=None, purpose="") -> str:
    """Строка вида ST00012|Name=...|PersonalAcc=...|BankName=...|BIC=...|CorrespAcc=...

    amount — сумма в рублях (float); в QR записывается в копейках (целое).
    """
    fields = [
        ("Name",        _clean(company.name)),
        ("PersonalAcc", _clean(company.bank_account)),
        ("BankName",    _clean(company.bank_name)),
        ("BIC",         _clean(company.bank_bik)),
        ("CorrespAcc",  _clean(company.bank_corr_account)),
    ]
    if _clean(getattr(company, "inn", "")):
        fields.append(("PayeeINN", _clean(company.inn)))
    if _clean(getattr(company, "kpp", "")):
        fields.append(("KPP", _clean(company.kpp)))
    if amount:
        fields.append(("Sum", str(int(round(amount * 100)))))
    if purpose:
        fields.append(("Purpose", purpose))

    body = "|".join(f"{k}={v}" for k, v in fields if v)
    return "ST00012|" + body


def generate_payment_qr(company, amount=None, purpose="") -> bytes | None:
    """PNG-байты с платёжным QR или None, если qrcode недоступна / нет реквизитов."""
    if not _HAS_QR:
        return None
    # Минимально необходимые реквизиты для осмысленного QR
    if not (_clean(company.bank_account) and _clean(company.bank_bik)):
        return None
    payload = build_payment_string(company, amount=amount, purpose=purpose)
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10, border=1,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

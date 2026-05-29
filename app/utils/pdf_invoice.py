"""Генерация PDF счёта на оплату."""
import io, os
from datetime import date
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, Image
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

_F  = "Helvetica"
_FB = "Helvetica-Bold"

def _try_register_fonts():
    global _F, _FB
    for reg, bold, name, bname in [
        (r"C:\Windows\Fonts\arial.ttf",  r"C:\Windows\Fonts\arialbd.ttf",  "Arial",  "Arial-Bold"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVu", "DejaVu-Bold"),
    ]:
        if os.path.exists(reg) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont(name, reg))
                pdfmetrics.registerFont(TTFont(bname, bold))
                _F, _FB = name, bname
                return
            except Exception:
                pass

_try_register_fonts()

BORDER   = colors.HexColor("#333333")
HDR_BG   = colors.HexColor("#efefef")
ROW_BG   = colors.HexColor("#f8f8f8")
GREY     = colors.HexColor("#555555")

COND_TEXT = (
    "Оплата данного счёта означает согласие с условиями поставки товара. "
    "Уведомление об оплате обязательно, в противном случае не гарантируется "
    "наличие товара на складе. Товар отпускается по факту прихода денег на р/с "
    "Поставщика, самовывозом, при наличии доверенности и паспорта."
)

_MONTHS = ["января","февраля","марта","апреля","мая","июня",
           "июля","августа","сентября","октября","ноября","декабря"]


# ── helpers ──────────────────────────────────────────────────────────────────

def _s(sz=9, bold=False, align="LEFT", color=None, leading=None, name="s"):
    return ParagraphStyle(
        name, fontName=(_FB if bold else _F), fontSize=sz,
        leading=leading or max(sz * 1.3, sz + 2),
        alignment={"LEFT": 0, "CENTER": 1, "RIGHT": 2}.get(align, 0),
        textColor=color or colors.black,
    )

def _p(text, bold=False, align="LEFT", color=None, sz=8):
    return Paragraph(str(text), _s(sz=sz, bold=bold, align=align, color=color, name="_p"))

def _hline(width, thick=0.5, space_before=0):
    t = Table([[""]], colWidths=[width], rowHeights=[max(thick, 0.5)])
    t.setStyle(TableStyle([
        ("LINEABOVE",     (0,0),(-1,-1), thick, BORDER),
        ("TOPPADDING",    (0,0),(-1,-1), space_before),
        ("BOTTOMPADDING", (0,0),(-1,-1), 1),
    ]))
    return t

def _money(v):
    if v is None: return "0,00"
    return f"{v:,.2f}".replace(",", " ").replace(".", ",")

def _num(v):
    if v is None: return "0"
    return str(int(v)) if float(v) == int(float(v)) else str(v).replace(".", ",")

def _date_verbose(d):
    if not d: return "—"
    if isinstance(d, date):
        return f"{d.day} {_MONTHS[d.month-1]} {d.year} г."
    return str(d)

def _company_line(c):
    parts = [c.name or ""]
    if c.inn:           parts.append(f"ИНН {c.inn}")
    if c.kpp:           parts.append(f"КПП {c.kpp}")
    if c.legal_address: parts.append(c.legal_address)
    if c.phone:         parts.append(c.phone)
    return ",  ".join(filter(None, parts))

def _cp_line(cp):
    parts = [cp.name or ""]
    if cp.inn:           parts.append(f"ИНН {cp.inn}")
    if cp.kpp:           parts.append(f"КПП {cp.kpp}")
    if cp.legal_address: parts.append(cp.legal_address)
    if cp.phone:         parts.append(cp.phone)
    return ",  ".join(filter(None, parts))

def _invoice_basis(invoice):
    if getattr(invoice, "contract", None):
        c = invoice.contract
        return f"№ {c.number} от {c.date.strftime('%d.%m.%Y')} (руб.)"
    if invoice.notes:
        return invoice.notes
    return "Основной договор"

def _logo_cell(company, width):
    lp = getattr(company, "logo_path", None)
    if lp and os.path.exists(lp):
        try:
            img = Image(lp, width=width - 3*mm, height=20*mm)
            img.hAlign = "LEFT"
            return img
        except Exception:
            pass
    return Paragraph("", _s(9, name="nologo"))


def _qr_cell(company, amount, size):
    """Ячейка с платёжным QR-кодом и подписью. Пусто, если QR недоступен."""
    from app.utils.payment_qr import generate_payment_qr
    purpose = "Оплата по счёту"
    png = generate_payment_qr(company, amount=amount, purpose=purpose)
    if not png:
        return Paragraph("", _s(7, name="noqr"))
    img = Image(io.BytesIO(png), width=size, height=size)
    img.hAlign = "RIGHT"
    cap = Paragraph("Отсканируйте<br/>для оплаты", _s(6, align="CENTER", color=GREY, name="qrcap"))
    t = Table([[img], [cap]], colWidths=[size])
    t.setStyle(TableStyle([
        ("ALIGN",         (0,0),(-1,-1), "CENTER"),
        ("LEFTPADDING",   (0,0),(-1,-1), 0),
        ("RIGHTPADDING",  (0,0),(-1,-1), 0),
        ("TOPPADDING",    (0,0),(-1,-1), 0),
        ("BOTTOMPADDING", (0,0),(-1,-1), 0),
    ]))
    return t

def _build_bank_table(company, width):
    L = width * 0.52
    M = width * 0.13
    R = width - L - M
    inn = company.inn or "—"
    kpp = company.kpp or "—"
    data = [
        [Paragraph(company.bank_name or "—", _s(8, name="bn")),
         Paragraph("БИК",   _s(7, name="bl1")),
         Paragraph(company.bank_bik or "—", _s(8, name="bv1"))],
        ["",
         Paragraph("Сч. №", _s(7, name="bl2")),
         Paragraph(company.bank_corr_account or "—", _s(8, name="bv2"))],
        [Paragraph("<b>Банк получателя</b>", _s(7, bold=True, name="bpol")), "", ""],
        [Paragraph(f"ИНН {inn}   КПП {kpp}", _s(8, name="inn")),
         Paragraph("Сч. №", _s(7, name="bl3")),
         Paragraph(company.bank_account or "—", _s(8, name="bv3"))],
        [Paragraph(company.name or "—", _s(8, name="cname")), "", ""],
        [Paragraph("<b>Получатель</b>", _s(7, bold=True, name="pol")), "", ""],
    ]
    t = Table(data, colWidths=[L, M, R])
    t.setStyle(TableStyle([
        ("BOX",        (0,0),(-1,-1), 0.6, BORDER),
        ("LINEBEFORE", (1,0),(1,-1),  0.4, colors.grey),
        ("LINEBEFORE", (2,0),(2,-1),  0.4, colors.grey),
        ("LINEBELOW",  (0,1),(-1,1),  0.4, colors.grey),
        ("LINEBELOW",  (0,3),(-1,3),  0.4, colors.grey),
        ("SPAN",       (0,2),(2,2)),
        ("SPAN",       (0,4),(2,4)),
        ("SPAN",       (0,5),(2,5)),
        ("BACKGROUND", (0,2),(2,2), colors.HexColor("#f5f5f5")),
        ("BACKGROUND", (0,5),(2,5), colors.HexColor("#f5f5f5")),
        ("VALIGN",     (0,0),(-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0),(-1,-1), 2),
        ("BOTTOMPADDING",(0,0),(-1,-1), 2),
        ("LEFTPADDING",(0,0),(-1,-1), 4),
        ("RIGHTPADDING",(0,0),(-1,-1), 4),
    ]))
    return t


# ── main ─────────────────────────────────────────────────────────────────────

def generate_invoice_pdf(invoice, company) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=15*mm, rightMargin=15*mm,
                            topMargin=12*mm, bottomMargin=12*mm)
    W = A4[0] - 30*mm
    story = []
    cp = invoice.counterparty

    # 1. Bank header: логотип | банковские реквизиты | платёжный QR
    LOGO_W = 25*mm
    QR_W   = 26*mm
    BANK_W = W - LOGO_W - QR_W
    outer = Table([[
        _logo_cell(company, LOGO_W),
        _build_bank_table(company, BANK_W),
        _qr_cell(company, invoice.total_amount, QR_W - 2*mm),
    ]], colWidths=[LOGO_W, BANK_W, QR_W])
    outer.setStyle(TableStyle([
        ("VALIGN",        (0,0),(0,0),   "TOP"),
        ("VALIGN",        (1,0),(1,0),   "TOP"),
        ("VALIGN",        (2,0),(2,0),   "TOP"),
        ("ALIGN",         (2,0),(2,0),   "RIGHT"),
        ("LEFTPADDING",   (0,0),(-1,-1), 0),
        ("RIGHTPADDING",  (0,0),(-1,-1), 0),
        ("TOPPADDING",    (0,0),(-1,-1), 0),
        ("BOTTOMPADDING", (0,0),(-1,-1), 0),
    ]))
    story.append(outer)
    story.append(Spacer(1, 4*mm))

    # 2. Title
    story.append(Paragraph(
        f"Счёт на оплату № {invoice.number} от {_date_verbose(invoice.date)}",
        _s(14, bold=True, name="title"),
    ))
    story.append(Spacer(1, 1*mm))
    story.append(_hline(W, thick=1.5))
    story.append(_hline(W, thick=0.4, space_before=1))
    story.append(Spacer(1, 4*mm))

    # 3. Parties
    LW = 40*mm
    basis = _invoice_basis(invoice)
    parties = Table([
        [Paragraph("Поставщик<br/>(исполнитель):", _s(8, name="pl")),
         Paragraph(_company_line(company), _s(9, name="pv"))],
        [Paragraph("Покупатель<br/>(заказчик):",   _s(8, name="pl2")),
         Paragraph(_cp_line(cp),                   _s(9, name="pv2"))],
        [Paragraph("Основание:",                   _s(9, bold=True, name="bl")),
         Paragraph(basis,                          _s(9, name="bv"))],
    ], colWidths=[LW, W - LW])
    parties.setStyle(TableStyle([
        ("VALIGN",        (0,0),(-1,-1), "TOP"),
        ("TOPPADDING",    (0,0),(-1,-1), 2),
        ("BOTTOMPADDING", (0,0),(-1,-1), 2),
        ("LEFTPADDING",   (0,0),(-1,-1), 0),
        ("RIGHTPADDING",  (0,0),(-1,-1), 0),
    ]))
    story.append(parties)
    story.append(Spacer(1, 5*mm))

    # 4. Items table — 9 columns (с колонкой «Скидка», как в форме 1С)
    #    № | Товар (Услуга) | Код | Кол-во | Ед. | Цена | Сумма без скидки | Скидка | Сумма
    CW = [7*mm, 47*mm, 16*mm, 12*mm, 9*mm, 17*mm, 19*mm, 15*mm, 23*mm]  # = 165mm
    headers = [
        _p("№",                bold=True, align="CENTER"),
        _p("Товар (Услуга)",   bold=True),
        _p("Код",              bold=True),
        _p("Кол-во",           bold=True, align="RIGHT"),
        _p("Ед.",              bold=True, align="CENTER"),
        _p("Цена",             bold=True, align="RIGHT"),
        _p("Сумма<br/>без скидки", bold=True, align="RIGHT"),
        _p("Скидка",           bold=True, align="RIGHT"),
        _p("Сумма",            bold=True, align="RIGHT"),
    ]
    # строка с номерами колонок (1..9)
    colnums = [_p(str(i), align="CENTER", color=GREY, sz=7) for i in range(1, 10)]
    rows = [headers, colnums]

    total_qty   = 0.0
    total_gross = 0.0
    total_disc  = 0.0

    for i, item in enumerate(invoice.items, 1):
        code  = (item.product.article or "") if item.product else ""
        gross = item.price * item.quantity           # сумма без скидки
        disc  = round(gross - item.amount, 2)         # скидка = брутто − итог строки
        total_qty   += item.quantity
        total_gross += gross
        total_disc  += disc
        rows.append([
            _p(str(i),               align="CENTER"),
            _p(item.name),
            _p(code),
            _p(_num(item.quantity),  align="RIGHT"),
            _p(item.unit,            align="CENTER"),
            _p(_money(item.price),   align="RIGHT"),
            _p(_money(gross),        align="RIGHT"),
            _p(_money(disc) if disc else "—", align="RIGHT"),
            _p(_money(item.amount),  align="RIGHT"),
        ])

    # итоговая строка
    rows.append([
        "", "", "",
        _p(_num(total_qty), align="RIGHT"), "", "",
        _p(f"<b>{_money(total_gross)}</b>", align="RIGHT"),
        _p(f"<b>{_money(total_disc) if total_disc else '—'}</b>", align="RIGHT"),
        _p(f"<b>{_money(invoice.subtotal)}</b>", align="RIGHT"),
    ])

    n = len(rows)
    tbl = Table(rows, colWidths=CW, repeatRows=2)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",     (0,0),(-1,0),   HDR_BG),
        ("FONTNAME",       (0,0),(-1,0),   _FB),
        ("FONTNAME",       (0,2),(-1,-1),  _F),
        ("FONTSIZE",       (0,0),(-1,-1),  8),
        ("FONTSIZE",       (0,1),(-1,1),   7),
        ("GRID",           (0,0),(-1,n-2), 0.4, colors.HexColor("#cccccc")),
        ("LINEABOVE",      (0,n-1),(-1,n-1), 0.4, colors.grey),
        ("ROWBACKGROUNDS", (0,2),(-1,n-2), [colors.white, ROW_BG]),
        ("VALIGN",         (0,0),(-1,-1),  "MIDDLE"),
        ("TOPPADDING",     (0,0),(-1,-1),  3),
        ("BOTTOMPADDING",  (0,0),(-1,-1),  3),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 2*mm))

    # 5. Totals (right-aligned block) — отдельная таблица «метка | сумма»
    vat_str = _money(invoice.vat_amount) if invoice.vat_amount else "—"
    LBL_W = 45*mm
    VAL_W = 35*mm
    PAD_W = W - LBL_W - VAL_W
    totals_rows = [
        ["", _p("Итого:",            align="RIGHT"), _p(_money(invoice.subtotal), align="RIGHT")],
        ["", _p("Без налога (НДС):", align="RIGHT"), _p(vat_str,                  align="RIGHT")],
    ]
    if total_disc:
        totals_rows.append(
            ["", _p("Скидка:", align="RIGHT"), _p(_money(total_disc), align="RIGHT")])
    totals_rows.append([
        "",
        _p("<b>Всего к оплате (с учётом скидки):</b>" if total_disc else "<b>Всего к оплате:</b>",
           align="RIGHT", bold=True),
        _p(f"<b>{_money(invoice.total_amount)}</b>", align="RIGHT", bold=True)])
    last = len(totals_rows) - 1
    totals = Table(totals_rows, colWidths=[PAD_W, LBL_W, VAL_W])
    totals.setStyle(TableStyle([
        ("FONTSIZE",      (0,0),(-1,-1), 8),
        ("FONTNAME",      (0,0),(-1,-1), _F),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0),(-1,-1), 2),
        ("BOTTOMPADDING", (0,0),(-1,-1), 2),
        ("LINEABOVE",     (1,last),(-1,last),  0.8, BORDER),
    ]))
    story.append(totals)
    story.append(Spacer(1, 4*mm))

    # 6. Sum in words
    from app.utils.number_to_words import amount_to_words
    cnt = len(invoice.items)
    story.append(Paragraph(
        f"Всего наименований {cnt}, на сумму <b>{_money(invoice.total_amount)} руб.</b>",
        _s(9, name="cnt"),
    ))
    story.append(Paragraph(f"<b>{amount_to_words(invoice.total_amount)}</b>", _s(9, bold=True, name="aw")))
    story.append(Spacer(1, 4*mm))

    # 7. Conditions
    story.append(Paragraph(COND_TEXT, _s(8, color=GREY, name="cond")))
    story.append(Spacer(1, 5*mm))
    story.append(_hline(W, thick=0.7))
    story.append(Spacer(1, 4*mm))

    # 8. Signature
    is_ip  = (company.name or "").strip().upper().startswith("ИП")
    role   = "Предприниматель" if is_ip else "Руководитель"
    dname  = company.director or ""

    sign = Table([
        [_p(role, bold=True),
         _p("________________", align="CENTER"),
         _p(dname),
         _p("М.П.", align="CENTER")],
        ["",
         _p("подпись",              align="CENTER", color=GREY, sz=7),
         _p("расшифровка подписи",  align="CENTER", color=GREY, sz=7),
         ""],
    ], colWidths=[40*mm, 45*mm, 58*mm, 22*mm])
    sign.setStyle(TableStyle([
        ("FONTSIZE",      (0,0),(-1,-1), 9),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0),(-1,-1), 1),
        ("BOTTOMPADDING", (0,0),(-1,-1), 1),
        ("LINEBELOW",     (1,0),(1,0),   0.5, BORDER),
        ("LINEBELOW",     (2,0),(2,0),   0.5, BORDER),
    ]))
    story.append(sign)

    doc.build(story)
    return buf.getvalue()

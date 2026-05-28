"""Генерация PDF счёта на оплату через ReportLab."""
import io
from datetime import date
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, HRFlowable
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import os

# Пытаемся зарегистрировать шрифт с поддержкой кириллицы
_FONT_NAME = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"

def _try_register_fonts():
    global _FONT_NAME, _FONT_BOLD
    candidates = [
        (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf", "Arial", "Arial-Bold"),
        (r"C:\Windows\Fonts\times.ttf", r"C:\Windows\Fonts\timesbd.ttf", "Times", "Times-Bold"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "DejaVu", "DejaVu-Bold"),
    ]
    for reg, bold, name, bold_name in candidates:
        if os.path.exists(reg) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont(name, reg))
                pdfmetrics.registerFont(TTFont(bold_name, bold))
                _FONT_NAME = name
                _FONT_BOLD = bold_name
                return
            except Exception:
                continue

_try_register_fonts()

ACCENT = colors.HexColor("#f39c12")
DARK = colors.HexColor("#1e2a3a")


def _style(name="Normal", font=None, size=10, bold=False, align="LEFT", color=None, leading=None):
    s = ParagraphStyle(
        name,
        fontName=(_FONT_BOLD if bold else (_FONT_NAME if font is None else font)),
        fontSize=size,
        leading=leading or size * 1.3,
        alignment={"LEFT": 0, "CENTER": 1, "RIGHT": 2, "JUSTIFY": 4}.get(align, 0),
        textColor=color or colors.black,
    )
    return s


def generate_invoice_pdf(invoice, company) -> bytes:
    """Возвращает байты PDF счёта на оплату."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        rightMargin=15*mm, leftMargin=15*mm,
        topMargin=15*mm, bottomMargin=15*mm,
    )
    story = []

    # ── Шапка: реквизиты банка и получателя ──────────────────────────────────
    bank_info = (
        f"Банк получателя: {company.bank_name or '—'}\n"
        f"БИК: {company.bank_bik or '—'}   к/с: {company.bank_corr_account or '—'}\n"
        f"Р/с: {company.bank_account or '—'}"
    )
    header_data = [
        [Paragraph(bank_info, _style("bk", size=8)),
         Paragraph(f"<b>Счёт на оплату № {invoice.number}</b><br/>"
                   f"от {_fmt_date(invoice.date)}", _style("hn", size=12, bold=True, align="RIGHT"))],
    ]
    header_table = Table(header_data, colWidths=[95*mm, 85*mm])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 1, DARK),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 4*mm))

    # ── Поставщик / Покупатель ────────────────────────────────────────────────
    cp = invoice.counterparty
    supplier_str = _company_line(company)
    buyer_str = _counterparty_line(cp)

    parties_data = [
        [Paragraph("<b>Поставщик:</b>", _style("lbl", size=9, bold=True)),
         Paragraph(supplier_str, _style("val", size=9))],
        [Paragraph("<b>Покупатель:</b>", _style("lbl2", size=9, bold=True)),
         Paragraph(buyer_str, _style("val2", size=9))],
    ]
    parties_table = Table(parties_data, colWidths=[30*mm, 150*mm])
    parties_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(parties_table)
    story.append(Spacer(1, 5*mm))

    # ── Таблица позиций ───────────────────────────────────────────────────────
    col_headers = ["№", "Наименование товара / услуги", "Кол-во", "Ед.", "Цена", "Сумма"]
    col_widths = [8*mm, 77*mm, 15*mm, 12*mm, 22*mm, 26*mm]
    rows = [col_headers]
    for i, item in enumerate(invoice.items, 1):
        rows.append([
            str(i),
            item.name,
            _fmt_num(item.quantity),
            item.unit,
            _fmt_money(item.price),
            _fmt_money(item.amount),
        ])

    item_table = Table(rows, colWidths=col_widths, repeatRows=1)
    item_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), _FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (1, 1), (1, -1), _FONT_NAME),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9f9f9")]),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(item_table)
    story.append(Spacer(1, 3*mm))

    # ── Итоги ─────────────────────────────────────────────────────────────────
    vat_label = f"В том числе НДС 20%:" if invoice.vat_amount else "НДС: не облагается"
    totals = [
        ["", "", "", "", "Итого без НДС:", _fmt_money(invoice.subtotal)],
        ["", "", "", "", vat_label, _fmt_money(invoice.vat_amount)],
        ["", "", "", "", "ИТОГО:", _fmt_money(invoice.total_amount)],
    ]
    totals_table = Table(totals, colWidths=col_widths)
    totals_table.setStyle(TableStyle([
        ("FONTNAME", (4, 0), (-1, -1), _FONT_NAME),
        ("FONTNAME", (4, 2), (-1, 2), _FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
        ("LINEABOVE", (4, 2), (-1, 2), 1, DARK),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(totals_table)
    story.append(Spacer(1, 3*mm))

    # ── Сумма прописью ────────────────────────────────────────────────────────
    from app.utils.number_to_words import amount_to_words
    story.append(Paragraph(
        f"<b>Итого к оплате:</b> {amount_to_words(invoice.total_amount)}",
        _style("aw", size=9, bold=True)
    ))
    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Spacer(1, 4*mm))

    # ── Подписи ───────────────────────────────────────────────────────────────
    sign_data = [
        [Paragraph("Руководитель", _style("sl", size=9)),
         Paragraph("________________", _style("sl2", size=9, align="CENTER")),
         Paragraph(company.director or "", _style("sl3", size=9)),
         Paragraph("Бухгалтер", _style("sl4", size=9)),
         Paragraph("________________", _style("sl5", size=9, align="CENTER")),
         Paragraph(company.accountant or "", _style("sl6", size=9))],
    ]
    sign_table = Table(sign_data, colWidths=[25*mm, 35*mm, 40*mm, 22*mm, 35*mm, 23*mm])
    sign_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    story.append(sign_table)

    doc.build(story)
    return buf.getvalue()


def _fmt_date(d) -> str:
    if not d:
        return "—"
    if isinstance(d, date):
        return d.strftime("%d.%m.%Y")
    return str(d)


def _fmt_money(v) -> str:
    if v is None:
        return "0,00"
    return f"{v:,.2f}".replace(",", " ").replace(".", ",")


def _fmt_num(v) -> str:
    if v is None:
        return "0"
    if v == int(v):
        return str(int(v))
    return str(v).replace(".", ",")


def _company_line(c) -> str:
    parts = [c.name or ""]
    if c.inn:
        parts.append(f"ИНН: {c.inn}")
    if c.kpp:
        parts.append(f"КПП: {c.kpp}")
    if c.legal_address:
        parts.append(c.legal_address)
    if c.phone:
        parts.append(c.phone)
    return ", ".join(filter(None, parts))


def _counterparty_line(cp) -> str:
    parts = [cp.name or ""]
    if cp.inn:
        parts.append(f"ИНН: {cp.inn}")
    if cp.kpp:
        parts.append(f"КПП: {cp.kpp}")
    if cp.legal_address:
        parts.append(cp.legal_address)
    if cp.phone:
        parts.append(cp.phone)
    return ", ".join(filter(None, parts))

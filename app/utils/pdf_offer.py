"""Генерация PDF счёта-оферты (доставка за счёт покупателя / поставщика)."""
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

BORDER  = colors.HexColor("#333333")
HDR_BG  = colors.HexColor("#efefef")
ROW_BG  = colors.HexColor("#f8f8f8")
GREY    = colors.HexColor("#555555")

# ─── Условия оферты ──────────────────────────────────────────────────────────

_COND_COMMON = [
    ("1", "Настоящий счёт является офертой в соответствии со статьей 435 Гражданского Кодекса "
          "Российской Федерации, оплата данного счета является его акцептом (ст. 438 ГК РФ) "
          "и полное согласие с условиями, изложенным в нем."),
    ("2", "Оплата по счету производится в течении 3-х банковских дней после его выставления, "
          "в случае нарушения данного срока, счет считается недействительным, и цена товара "
          "подлежит пересмотру."),
    ("3", "ПОКУПАТЕЛЬ обязан предоставить ПОСТАВЩИКУ до получения либо в момент получения Товара "
          "надлежаще оформленный документ, подтверждающий полномочия представителя ПОКУПАТЕЛЯ "
          "на приемку Товара и подписание соответствующих документов. Если полномочия надлежащим "
          "образом не подтверждены, ПОСТАВЩИК имеет право не производить передачу Товара либо "
          "передать Товар в месте доставки лицу, полномочия которого явствуют из обстановки "
          "на момент передачи Товара."),
    ("4", "Товарные накладные / УПД подписываются между сторонами в день фактической передачи товара."),
    ("5", "ПОКУПАТЕЛЬ обязан в момент получения Товара проверить количество Товара на основании "
          "ТН/ТТН, а также наличие видимых недостатков упаковки. Претензии в отношении количества "
          "и упаковки должны быть заявлены ПОКУПАТЕЛЕМ в день фактической поставки партии Товара."),
    ("6", "Приемка Товара по качеству (видимые дефекты) производится ПОКУПАТЕЛЕМ в течение десяти "
          "календарных дней с даты передачи Товара, в отношении скрытых недостатков – в течение "
          "срока годности. В случае возникновения у ПОКУПАТЕЛЯ претензий к качеству, он обязан "
          "составить рекламационный акт о выявленном несоответствии. Указанный акт подлежит "
          "передаче ПОСТАВЩИКУ любым способом, позволяющим установить факт его своевременного "
          "направления, не позднее 1 (одного) рабочего дня со дня выявления недостатков."),
    ("7", "ПОСТАВЩИК освобожден от ответственности за обнаруженные дефекты в качестве Товара, "
          "если указанные дефекты возникли в связи с несоблюдением инструкций по приемке и (или) "
          "хранению, и в иных случаях, предусмотренных действующим законодательством."),
]

_COND_TRANSPORT = (
    "8", "При необоснованном отказе ПОКУПАТЕЛЯ от приемки Товара надлежащего качества и "
         "поставленного в согласованные сторонами сроки, ПОКУПАТЕЛЬ возмещает ПОСТАВЩИКУ "
         "транспортные расходы, связанные с доставкой и возвратом Товара."
)

def _conditions(delivery: str) -> list:
    """Возвращает список кортежей (номер, текст) для нужного типа оферты."""
    conds = list(_COND_COMMON)
    if delivery == "supplier":
        conds.append(_COND_TRANSPORT)
        last_num = str(len(conds) + 1)
    else:
        last_num = str(len(conds) + 1)
    conds.append((last_num,
        "Стороны признают юридическую силу факсовых и (или) отсканированных "
        "вариантов данного счета – оферты."))
    return conds


# ─── Вспомогательные функции ─────────────────────────────────────────────────

_MONTHS = ["января","февраля","марта","апреля","мая","июня",
           "июля","августа","сентября","октября","ноября","декабря"]

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

def _entity_line(obj):
    parts = [obj.name or ""]
    if obj.inn:           parts.append(f"ИНН {obj.inn}")
    if obj.kpp:           parts.append(f"КПП {obj.kpp}")
    if obj.legal_address: parts.append(obj.legal_address)
    if obj.phone:         parts.append(obj.phone)
    return ",  ".join(filter(None, parts))

def _build_bank_block(company, width):
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


# ─── Главная функция ─────────────────────────────────────────────────────────

def generate_offer_pdf(invoice, company, delivery: str = "buyer") -> bytes:
    """
    delivery: 'buyer'    — доставка за счёт покупателя
              'supplier' — доставка за счёт поставщика
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=15*mm, rightMargin=15*mm,
                            topMargin=12*mm, bottomMargin=12*mm)
    W = A4[0] - 30*mm
    story = []
    cp = invoice.counterparty

    # 1. Банковский блок
    story.append(_build_bank_block(company, W))
    story.append(Spacer(1, 4*mm))

    # 2. Заголовок
    inv_date = invoice.date
    if inv_date:
        date_str = f"{inv_date.day} {_MONTHS[inv_date.month-1]} {inv_date.year}"
    else:
        date_str = "_____ ____________"
    story.append(Paragraph(
        f"Счёт-оферта № {invoice.number} от {date_str} г.",
        _s(14, bold=True, name="title"),
    ))
    story.append(Spacer(1, 1*mm))
    story.append(_hline(W, thick=1.5))
    story.append(_hline(W, thick=0.4, space_before=1))
    story.append(Spacer(1, 4*mm))

    # 3. Стороны
    LW = 40*mm
    parties = Table([
        [Paragraph("Поставщик<br/>(исполнитель):", _s(8, name="pl")),
         Paragraph(_entity_line(company), _s(9, name="pv"))],
        [Paragraph("Покупатель<br/>(заказчик):",   _s(8, name="pl2")),
         Paragraph(_entity_line(cp),               _s(9, name="pv2"))],
    ], colWidths=[LW, W - LW])
    parties.setStyle(TableStyle([
        ("VALIGN",        (0,0),(-1,-1), "TOP"),
        ("TOPPADDING",    (0,0),(-1,-1), 3),
        ("BOTTOMPADDING", (0,0),(-1,-1), 3),
        ("LEFTPADDING",   (0,0),(-1,-1), 0),
        ("RIGHTPADDING",  (0,0),(-1,-1), 0),
        ("LINEBELOW",     (0,0),(-1,0),  0.3, colors.HexColor("#cccccc")),
        ("LINEBELOW",     (0,1),(-1,1),  0.3, colors.HexColor("#cccccc")),
    ]))
    story.append(parties)
    story.append(Spacer(1, 5*mm))

    # 4. Таблица товаров: № | Товар (Услуга) | НДС | Кол-во | Ед. | Цена | Сумма
    CW = [8*mm, 72*mm, 16*mm, 16*mm, 10*mm, 22*mm, 21*mm]  # = 165 mm
    headers = [
        _p("№",              bold=True, align="CENTER"),
        _p("Товар (Услуга)", bold=True),
        _p("НДС",            bold=True, align="CENTER"),
        _p("Кол-во",         bold=True, align="RIGHT"),
        _p("Ед.",            bold=True, align="CENTER"),
        _p("Цена",           bold=True, align="RIGHT"),
        _p("Сумма",          bold=True, align="RIGHT"),
    ]
    colnums = [_p(str(i), align="CENTER", color=GREY, sz=7) for i in range(1, 8)]
    rows = [headers, colnums]

    for i, item in enumerate(invoice.items, 1):
        vat = item.vat_rate
        if vat == 0:
            vat_str = "Без НДС"
        else:
            vat_str = f"{int(vat)}%"
        rows.append([
            _p(str(i),              align="CENTER"),
            _p(item.name),
            _p(vat_str,             align="CENTER"),
            _p(_num(item.quantity), align="RIGHT"),
            _p(item.unit,           align="CENTER"),
            _p(_money(item.price),  align="RIGHT"),
            _p(_money(item.amount), align="RIGHT"),
        ])

    n = len(rows)
    tbl = Table(rows, colWidths=CW, repeatRows=2)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",     (0,0),(-1,0),   HDR_BG),
        ("FONTNAME",       (0,0),(-1,0),   _FB),
        ("FONTSIZE",       (0,0),(-1,-1),  8),
        ("FONTSIZE",       (0,1),(-1,1),   7),
        ("GRID",           (0,0),(-1,n-1), 0.4, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0,2),(-1,n-1), [colors.white, ROW_BG]),
        ("VALIGN",         (0,0),(-1,-1),  "MIDDLE"),
        ("TOPPADDING",     (0,0),(-1,-1),  3),
        ("BOTTOMPADDING",  (0,0),(-1,-1),  3),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 2*mm))

    # 5. Итог
    LBL_W = 55*mm
    VAL_W = 35*mm
    PAD_W = W - LBL_W - VAL_W
    vat_amount = invoice.vat_amount or 0.0
    vat_str_total = _money(vat_amount) if vat_amount else "Без НДС"
    totals = Table([
        ["", _p("Итого без НДС:",  align="RIGHT"), _p(_money(invoice.subtotal),    align="RIGHT")],
        ["", _p("НДС:",            align="RIGHT"), _p(vat_str_total,               align="RIGHT")],
        ["", _p("<b>Итого к оплате:</b>", align="RIGHT", bold=True),
              _p(f"<b>{_money(invoice.total_amount)}</b>", align="RIGHT", bold=True)],
    ], colWidths=[PAD_W, LBL_W, VAL_W])
    totals.setStyle(TableStyle([
        ("FONTSIZE",      (0,0),(-1,-1), 8),
        ("FONTNAME",      (0,0),(-1,-1), _F),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0),(-1,-1), 2),
        ("BOTTOMPADDING", (0,0),(-1,-1), 2),
        ("LINEABOVE",     (1,2),(-1,2),  0.8, BORDER),
    ]))
    story.append(totals)
    story.append(Spacer(1, 3*mm))

    # 6. Сумма прописью
    from app.utils.number_to_words import amount_to_words
    cnt = len(invoice.items)
    story.append(Paragraph(
        f"Всего наименований {cnt}, на сумму <b>{_money(invoice.total_amount)} руб.</b>",
        _s(9, name="cnt"),
    ))
    story.append(Paragraph(
        f"<b>{amount_to_words(invoice.total_amount)}</b>",
        _s(9, bold=True, name="aw"),
    ))
    story.append(Spacer(1, 4*mm))
    story.append(_hline(W, thick=0.7))
    story.append(Spacer(1, 3*mm))

    # 7. Условия оферты
    for num, text in _conditions(delivery):
        story.append(Paragraph(
            f"<b>{num}.</b> {text}",
            _s(7.5, color=GREY, name=f"cond{num}"),
        ))
        story.append(Spacer(1, 1.5*mm))

    story.append(Spacer(1, 4*mm))
    story.append(_hline(W, thick=0.7))
    story.append(Spacer(1, 4*mm))

    # 8. Подпись
    is_ip  = (company.name or "").strip().upper().startswith("ИП")
    role   = "Предприниматель" if is_ip else "Руководитель"
    dname  = company.director or ""

    sign = Table([
        [_p(role, bold=True),
         _p("________________", align="CENTER"),
         _p(dname),
         _p("М.П.", align="CENTER")],
        ["",
         _p("подпись",             align="CENTER", color=GREY, sz=7),
         _p("расшифровка подписи", align="CENTER", color=GREY, sz=7),
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

"""Генерация PDF УПД (Универсальный передаточный документ), статус 2."""
import io, os
from datetime import date
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ── fonts ─────────────────────────────────────────────────────────────────────
_F  = "Helvetica"
_FB = "Helvetica-Bold"

def _try_register():
    global _F, _FB
    for reg, bold, n, nb in [
        (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf", "Arial", "Arial-Bold"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVu", "DejaVu-Bold"),
    ]:
        if os.path.exists(reg) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont(n, reg))
                pdfmetrics.registerFont(TTFont(nb, bold))
                _F, _FB = n, nb
                return
            except Exception:
                pass

_try_register()

BORDER = colors.HexColor("#333333")
HDR_BG = colors.HexColor("#e8e8e8")
GREY   = colors.HexColor("#555555")

_MONTHS = ["января","февраля","марта","апреля","мая","июня",
           "июля","августа","сентября","октября","ноября","декабря"]


def _s(sz=7, bold=False, align="LEFT", color=None, name="s"):
    return ParagraphStyle(
        name, fontName=(_FB if bold else _F), fontSize=sz,
        leading=max(sz * 1.25, sz + 1),
        alignment={"LEFT": 0, "CENTER": 1, "RIGHT": 2}.get(align, 0),
        textColor=color or colors.black,
    )

def _p(txt, sz=7, bold=False, align="LEFT", color=None):
    return Paragraph(str(txt), _s(sz=sz, bold=bold, align=align, color=color, name="_p"))

def _money(v):
    if v is None: return "—"
    return f"{v:,.2f}".replace(",", " ").replace(".", ",")

def _date_v(d):
    if not d: return "—"
    if isinstance(d, date):
        return f"{d.day} {_MONTHS[d.month-1]} {d.year} г."
    return str(d)

def _date_s(d):
    if not d: return "—"
    if isinstance(d, date):
        return d.strftime("%d.%m.%Y")
    return str(d)

def _hline(w, thick=0.4):
    t = Table([[""]], colWidths=[w], rowHeights=[0.5])
    t.setStyle(TableStyle([
        ("LINEABOVE",     (0,0),(-1,-1), thick, BORDER),
        ("TOPPADDING",    (0,0),(-1,-1), 0),
        ("BOTTOMPADDING", (0,0),(-1,-1), 0),
    ]))
    return t


def generate_upd_pdf(invoice, company) -> bytes:
    """invoice — модель Invoice с items, counterparty, contract."""
    buf = io.BytesIO()
    PAGE = landscape(A4)           # 297 × 210 мм
    LM = RM = 10*mm
    TM = BM = 8*mm
    doc = SimpleDocTemplate(buf, pagesize=PAGE,
                            leftMargin=LM, rightMargin=RM,
                            topMargin=TM, bottomMargin=BM)
    W = PAGE[0] - LM - RM          # ~277 мм

    cp      = invoice.counterparty
    is_ip   = (company.name or "").strip().upper().startswith("ИП")
    role    = "Индивидуальный предприниматель" if is_ip else "Руководитель организации"
    dname   = company.director or (company.name or "")

    # Основание (договор)
    if getattr(invoice, "contract", None):
        basis = f"№ {invoice.contract.number} от {_date_s(invoice.contract.date)} (руб.)"
    elif invoice.notes:
        basis = invoice.notes
    else:
        basis = "—"

    # Данные для платёжного документа
    inv_num  = invoice.number
    inv_date = _date_s(invoice.date)

    story = []

    # ══════════════════════════════════════════════════════════════
    # 1. ЗАГОЛОВОК
    # ══════════════════════════════════════════════════════════════
    title_tbl = Table([
        [_p("Универсальный передаточный документ", sz=12, bold=True),
         _p("Статус:", sz=8),
         _p("2", sz=12, bold=True, align="CENTER")],
        ["",
         _p("2 – передаточный документ (акт)", sz=6, color=GREY),
         ""],
    ], colWidths=[W*0.65, W*0.25, W*0.10])
    title_tbl.setStyle(TableStyle([
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("LEFTPADDING",   (0,0),(-1,-1), 0),
        ("RIGHTPADDING",  (0,0),(-1,-1), 0),
        ("TOPPADDING",    (0,0),(-1,-1), 1),
        ("BOTTOMPADDING", (0,0),(-1,-1), 1),
    ]))
    story.append(title_tbl)
    story.append(_hline(W, 1.0))
    story.append(Spacer(1, 2*mm))

    # ══════════════════════════════════════════════════════════════
    # 2. ШАПКА: реквизиты (левая широкая) + ссылка на закон (правая)
    # ══════════════════════════════════════════════════════════════
    LW = W * 0.62
    RW = W - LW

    inn_kpp = company.inn or "—"
    if company.kpp:
        inn_kpp += f" / {company.kpp}"

    cp_addr = cp.legal_address or cp.actual_address or "—"
    gruzopoluchatel = f"{cp.name}; {cp_addr}"

    header_left = [
        [_p("Счёт-фактура №", sz=7, bold=True),
         _p(str(inv_num), sz=8, bold=True),
         _p("от", sz=7),
         _p(_date_v(invoice.date), sz=8, bold=True),
         _p("(1)", sz=6, color=GREY)],
        [_p("Исправление №", sz=7, color=GREY),
         _p("—", sz=7, color=GREY),
         _p("от", sz=7, color=GREY),
         _p("—", sz=7, color=GREY),
         _p("(1а)", sz=6, color=GREY)],
    ]
    sf_tbl = Table(header_left, colWidths=[25*mm, 20*mm, 8*mm, 40*mm, 10*mm])
    sf_tbl.setStyle(TableStyle([
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("LEFTPADDING",   (0,0),(-1,-1), 2),
        ("RIGHTPADDING",  (0,0),(-1,-1), 2),
        ("TOPPADDING",    (0,0),(-1,-1), 1),
        ("BOTTOMPADDING", (0,0),(-1,-1), 1),
    ]))

    def _row(lbl, val, ref=""):
        return [_p(lbl, sz=6, color=GREY), _p(val, sz=7), _p(ref, sz=6, color=GREY)]

    party_rows = [
        _row("Продавец:",                      company.name or "—"),
        _row("Адрес:",                          company.legal_address or "—"),
        _row("ИНН/КПП продавца:",              inn_kpp),
        _row("Грузоотправитель и его адрес:",   "он же"),
        _row("Грузополучатель и его адрес:",    gruzopoluchatel),
        _row("К платёжно-расчётному документу №:", f"{inv_num} от {inv_date}"),
        _row("Документ об отгрузке:",           f"Универсальный передаточный документ, №{inv_num} от {_date_v(invoice.date)}"),
        _row("Покупатель:",                     cp.name or "—"),
        _row("Адрес покупателя:",               cp_addr),
        _row("ИНН/КПП покупателя:",            (cp.inn or "—") + (f" / {cp.kpp}" if cp.kpp else "")),
        _row("Валюта:",                         "Российский рубль, 643"),
    ]
    party_tbl = Table(party_rows, colWidths=[LW*0.35, LW*0.60, LW*0.05])
    party_tbl.setStyle(TableStyle([
        ("FONTSIZE",      (0,0),(-1,-1), 7),
        ("VALIGN",        (0,0),(-1,-1), "TOP"),
        ("TOPPADDING",    (0,0),(-1,-1), 1),
        ("BOTTOMPADDING", (0,0),(-1,-1), 1),
        ("LEFTPADDING",   (0,0),(-1,-1), 2),
        ("RIGHTPADDING",  (0,0),(-1,-1), 2),
        ("LINEBELOW",     (0,-1),(-1,-1), 0.3, colors.lightgrey),
    ]))

    ref_text = (
        "Приложение № 1 к постановлению<br/>"
        "Правительства РФ от 26.12.2011 № 1137<br/>"
        "(в ред. от 23.01.2026 № 26)"
    )
    right_cell = _p(ref_text, sz=6, color=GREY)

    outer = Table([[
        Table([[sf_tbl], [party_tbl]], colWidths=[LW]),
        right_cell
    ]], colWidths=[LW, RW])
    outer.setStyle(TableStyle([
        ("BOX",           (0,0),(-1,-1), 0.6, BORDER),
        ("LINEBEFORE",    (1,0),(1,0),   0.4, colors.grey),
        ("VALIGN",        (0,0),(-1,-1), "TOP"),
        ("LEFTPADDING",   (0,0),(-1,-1), 0),
        ("RIGHTPADDING",  (0,0),(-1,-1), 0),
        ("TOPPADDING",    (0,0),(-1,-1), 0),
        ("BOTTOMPADDING", (0,0),(-1,-1), 0),
    ]))
    story.append(outer)
    story.append(Spacer(1, 2*mm))

    # ══════════════════════════════════════════════════════════════
    # 3. ТАБЛИЦА ПОЗИЦИЙ
    # ══════════════════════════════════════════════════════════════
    # Колонки: А, 1, 1а, 1б, 2, 2а, 3, 4, 5, 6, 7, 8, 9, 10, 10а, 11
    CW = [15*mm,  8*mm, 45*mm, 10*mm,  8*mm, 12*mm,
          13*mm, 18*mm, 18*mm, 14*mm,  9*mm, 15*mm,
          18*mm,  8*mm, 11*mm, 15*mm]   # = 237mm ≤ 277mm

    def _h(txt): return _p(txt, sz=6, bold=True, align="CENTER")

    hdrs = [
        [_h("Код\nтовара\n(А)"),
         _h("№\nп/п\n(1)"),
         _h("Наименование товара\n(описание работ, услуг)\n(1а)"),
         _h("Код\nвида\n(1б)"),
         _h("Код\nОКС\n(2)"),
         _h("Ед.\nизм.\n(2а)"),
         _h("Кол-во\n(3)"),
         _h("Цена\n(тариф)\n(4)"),
         _h("Стоимость\nбез налога\n(5)"),
         _h("Акциз\n(6)"),
         _h("Ставка\nНДС\n(7)"),
         _h("Сумма\nНДС\n(8)"),
         _h("Стоимость\nс налогом\n(9)"),
         _h("Страна\nкод\n(10)"),
         _h("Страна\nнаим.\n(10а)"),
         _h("№ деклар.\n(11)")],
    ]

    total_without_vat = 0.0
    total_vat         = 0.0
    total_with_vat    = 0.0

    for item in invoice.items:
        code    = (item.product.article or "—") if item.product else "—"
        okei    = "796"   # штука (стандартный ОКЕИ для шт/pc)
        vat_r   = item.vat_rate
        without = item.price * item.quantity
        vat_amt = without * vat_r / 100 if vat_r > 0 else 0
        with_t  = without + vat_amt
        total_without_vat += without
        total_vat         += vat_amt
        total_with_vat    += with_t

        vat_str  = f"Без НДС" if vat_r == 0 else f"{int(vat_r)}%"
        vat_sum  = "—" if vat_r == 0 else _money(vat_amt)

        hdrs.append([
            _p(code,              sz=7),
            _p(str(len(hdrs)),    sz=7, align="CENTER"),
            _p(item.name,         sz=7),
            _p("—",               sz=7, align="CENTER"),
            _p(okei,              sz=7, align="CENTER"),
            _p(item.unit,         sz=7, align="CENTER"),
            _p(_money(item.quantity).replace(",00",""), sz=7, align="RIGHT"),
            _p(_money(item.price), sz=7, align="RIGHT"),
            _p(_money(without),    sz=7, align="RIGHT"),
            _p("Без акциза",       sz=6, align="CENTER"),
            _p(vat_str,            sz=7, align="CENTER"),
            _p(vat_sum,            sz=7, align="RIGHT"),
            _p(_money(with_t),     sz=7, align="RIGHT"),
            _p("—",                sz=7, align="CENTER"),
            _p("—",                sz=7, align="CENTER"),
            _p("—",                sz=7, align="CENTER"),
        ])

    # итоговая строка
    hdrs.append([
        _p("Всего к оплате (9)", sz=7, bold=True), "", "", "", "", "", "", "",
        _p(_money(total_without_vat), sz=7, bold=True, align="RIGHT"),
        _p("Х", sz=7, align="CENTER"),
        _p("—",  sz=7, align="CENTER"),
        _p(_money(total_vat),       sz=7, bold=True, align="RIGHT"),
        _p(_money(total_with_vat),  sz=7, bold=True, align="RIGHT"),
        "", "", "",
    ])

    n = len(hdrs)
    tbl = Table(hdrs, colWidths=CW, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",  (0,0),(-1,0),   HDR_BG),
        ("GRID",        (0,0),(-1,n-2), 0.3, colors.HexColor("#bbbbbb")),
        ("LINEABOVE",   (0,n-1),(-1,n-1), 0.5, BORDER),
        ("SPAN",        (0,n-1),(7,n-1)),
        ("VALIGN",      (0,0),(-1,-1),  "MIDDLE"),
        ("TOPPADDING",  (0,0),(-1,-1),  2),
        ("BOTTOMPADDING",(0,0),(-1,-1), 2),
        ("LEFTPADDING", (0,0),(-1,-1),  2),
        ("RIGHTPADDING",(0,0),(-1,-1),  2),
        ("FONTSIZE",    (0,0),(-1,-1),  7),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 2*mm))

    # ══════════════════════════════════════════════════════════════
    # 4. НИЖНИЙ БЛОК: левая — продавец, правая — покупатель
    # ══════════════════════════════════════════════════════════════
    HW = W / 2

    # левая часть (продавец / грузоотправитель)
    seller_rows = [
        [_p(role, sz=7, bold=True),
         _p("или иное уполномоченное лицо", sz=6, color=GREY)],
        [_p("________________", sz=8, align="CENTER"),
         _p(dname, sz=7)],
        [_p("(подпись)", sz=6, color=GREY, align="CENTER"),
         _p("(ф.и.о.)", sz=6, color=GREY)],
        [_p(f"Дата отгрузки, передачи: {_date_v(invoice.date)}", sz=7), ""],
        [_p(f"Основание передачи: {basis}", sz=7), ""],
        [_p(f"Ответственный за правильность оформления:", sz=6, color=GREY), ""],
        [_p("________________", sz=8, align="CENTER"),
         _p(dname, sz=7)],
        [_p("(подпись)", sz=6, color=GREY, align="CENTER"),
         _p("(ф.и.о.)", sz=6, color=GREY)],
        [_p(f"Наименование составителя: {company.name or '—'}, ИНН {company.inn or '—'}", sz=6, color=GREY), ""],
        [_p("М.П.", sz=7, bold=True), ""],
    ]
    tbl_seller = Table(seller_rows, colWidths=[HW*0.4, HW*0.6])
    tbl_seller.setStyle(TableStyle([
        ("SPAN",        (0,3),(1,3)),
        ("SPAN",        (0,4),(1,4)),
        ("SPAN",        (0,5),(1,5)),
        ("SPAN",        (0,8),(1,8)),
        ("SPAN",        (0,9),(1,9)),
        ("LINEBELOW",   (0,1),(0,1), 0.5, BORDER),
        ("LINEBELOW",   (0,6),(0,6), 0.5, BORDER),
        ("VALIGN",      (0,0),(-1,-1), "MIDDLE"),
        ("TOPPADDING",  (0,0),(-1,-1), 1),
        ("BOTTOMPADDING",(0,0),(-1,-1), 1),
        ("LEFTPADDING", (0,0),(-1,-1), 2),
        ("RIGHTPADDING",(0,0),(-1,-1), 2),
    ]))

    # правая часть (покупатель / грузополучатель)
    buyer_rows = [
        [_p("Товар (груз) получил / услуги принял", sz=7, bold=True), ""],
        [_p("(должность)", sz=6, color=GREY),
         _p("Дата получения (приёмки): «___» __________ 20___ г.", sz=6, color=GREY)],
        [_p("________________", sz=8, align="CENTER"),
         _p("", sz=7)],
        [_p("(подпись)", sz=6, color=GREY, align="CENTER"),
         _p("(ф.и.о.)", sz=6, color=GREY)],
        [_p("Иные сведения о получении, приёмке:", sz=6, color=GREY), ""],
        [_p("Ответственный за правильность оформления:", sz=6, color=GREY), ""],
        [_p("________________", sz=8, align="CENTER"),
         _p("", sz=7)],
        [_p("(подпись)", sz=6, color=GREY, align="CENTER"),
         _p("(ф.и.о.)", sz=6, color=GREY)],
        [_p(f"Наименование составителя: {cp.name or '—'}, ИНН {cp.inn or '—'}", sz=6, color=GREY), ""],
        [_p("М.П.", sz=7, bold=True), ""],
    ]
    tbl_buyer = Table(buyer_rows, colWidths=[HW*0.4, HW*0.6])
    tbl_buyer.setStyle(TableStyle([
        ("SPAN",        (0,0),(1,0)),
        ("SPAN",        (0,4),(1,4)),
        ("SPAN",        (0,5),(1,5)),
        ("SPAN",        (0,8),(1,8)),
        ("SPAN",        (0,9),(1,9)),
        ("LINEBELOW",   (0,2),(0,2), 0.5, BORDER),
        ("LINEBELOW",   (0,6),(0,6), 0.5, BORDER),
        ("VALIGN",      (0,0),(-1,-1), "MIDDLE"),
        ("TOPPADDING",  (0,0),(-1,-1), 1),
        ("BOTTOMPADDING",(0,0),(-1,-1), 1),
        ("LEFTPADDING", (0,0),(-1,-1), 2),
        ("RIGHTPADDING",(0,0),(-1,-1), 2),
    ]))

    bottom = Table([[tbl_seller, tbl_buyer]], colWidths=[HW, HW])
    bottom.setStyle(TableStyle([
        ("BOX",       (0,0),(-1,-1), 0.6, BORDER),
        ("LINEBEFORE",(1,0),(1,0),   0.4, colors.grey),
        ("VALIGN",    (0,0),(-1,-1), "TOP"),
        ("LEFTPADDING", (0,0),(-1,-1), 0),
        ("RIGHTPADDING",(0,0),(-1,-1), 0),
        ("TOPPADDING",  (0,0),(-1,-1), 0),
        ("BOTTOMPADDING",(0,0),(-1,-1), 0),
    ]))
    story.append(bottom)

    doc.build(story)
    return buf.getvalue()

"""Генерация PDF УПД (Универсальный передаточный документ), статус 2.

Полная официальная форма по приложению № 1 к постановлению Правительства РФ
от 26.12.2011 № 1137 (в действующей редакции): счёт-фактура + передаточный
документ с нумерованными полями (1)–(11) и [8]–[19].
"""
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
        leading=max(sz * 1.2, sz + 1),
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


def _sign_block(width, cols, fld_no=""):
    """Подпись: верхняя строка — заполняемые поля с подчёркиванием, нижняя — пояснения.

    cols: список кортежей (frac, label, value).
    fld_no: номер поля формы ([10], [13] …) — печатается справа над блоком.
    """
    fracs  = [c[0] for c in cols]
    widths = [width * f for f in fracs]
    vals   = [_p(c[2] or " ", sz=7, align="CENTER") for c in cols]
    labs   = [_p(c[1], sz=5.5, color=GREY, align="CENTER") for c in cols]
    t = Table([vals, labs], colWidths=widths)
    style = [
        ("VALIGN",        (0,0),(-1,-1), "BOTTOM"),
        ("TOPPADDING",    (0,0),(-1,-1), 1),
        ("BOTTOMPADDING", (0,0),(-1,-1), 0),
        ("LEFTPADDING",   (0,0),(-1,-1), 2),
        ("RIGHTPADDING",  (0,0),(-1,-1), 2),
    ]
    for i in range(len(cols)):
        style.append(("LINEBELOW", (i,0),(i,0), 0.5, BORDER))
    t.setStyle(TableStyle(style))
    return t


def generate_upd_pdf(invoice, company) -> bytes:
    """invoice — модель Invoice с items, counterparty, contract."""
    buf = io.BytesIO()
    PAGE = landscape(A4)           # 297 × 210 мм
    LM = RM = 8*mm
    TM = BM = 7*mm
    doc = SimpleDocTemplate(buf, pagesize=PAGE,
                            leftMargin=LM, rightMargin=RM,
                            topMargin=TM, bottomMargin=BM,
                            title="Универсальный передаточный документ")
    W = PAGE[0] - LM - RM          # ~281 мм

    cp      = invoice.counterparty
    is_ip   = (company.name or "").strip().upper().startswith("ИП") \
              or "предприниматель" in (company.name or "").lower()
    dname   = company.director or (company.name or "")

    inn_kpp = company.inn or "—"
    if company.kpp:
        inn_kpp += f" / {company.kpp}"
    cp_inn_kpp = (cp.inn or "—") + (f" / {cp.kpp}" if cp.kpp else "")
    cp_addr = cp.legal_address or cp.actual_address or "—"

    if getattr(invoice, "contract", None):
        basis = f"№ {invoice.contract.number} от {_date_s(invoice.contract.date)} (руб.)"
    elif invoice.notes:
        basis = invoice.notes
    else:
        basis = "—"

    inv_num  = invoice.number
    inv_date = _date_s(invoice.date)

    story = []

    # ══════════════════════════════════════════════════════════════
    # 1. ЗАГОЛОВОК + статус + ссылка на постановление
    # ══════════════════════════════════════════════════════════════
    status_cell = Table([
        [_p("Статус:", sz=8), _p("2", sz=12, bold=True, align="CENTER")],
        [_p("1 – счёт-фактура и передаточный документ (акт)", sz=5.5, color=GREY), ""],
        [_p("2 – передаточный документ (акт)", sz=5.5, color=GREY), ""],
    ], colWidths=[None, 10*mm])
    status_cell.setStyle(TableStyle([
        ("SPAN", (1,0),(1,2)),
        ("BOX", (1,0),(1,2), 0.5, BORDER),
        ("VALIGN", (1,0),(1,2), "MIDDLE"),
        ("LEFTPADDING",(0,0),(-1,-1),1), ("RIGHTPADDING",(0,0),(-1,-1),1),
        ("TOPPADDING",(0,0),(-1,-1),0), ("BOTTOMPADDING",(0,0),(-1,-1),0),
    ]))

    ref_text = (
        "Приложение № 1<br/>к постановлению Правительства<br/>"
        "Российской Федерации<br/>от 26 декабря 2011 г. № 1137<br/>"
        "(в редакции постановления Правительства<br/>"
        "Российской Федерации от 23 января 2026 г. № 26)"
    )
    title_tbl = Table([[
        _p("Универсальный<br/>передаточный документ", sz=13, bold=True),
        status_cell,
        _p(ref_text, sz=6, color=GREY, align="RIGHT"),
    ]], colWidths=[W*0.40, W*0.32, W*0.28])
    title_tbl.setStyle(TableStyle([
        ("VALIGN", (0,0),(-1,-1), "TOP"),
        ("LEFTPADDING",(0,0),(-1,-1),0), ("RIGHTPADDING",(0,0),(-1,-1),0),
        ("TOPPADDING",(0,0),(-1,-1),0), ("BOTTOMPADDING",(0,0),(-1,-1),2),
    ]))
    story.append(title_tbl)

    # ── строка счёта-фактуры (1)/(1а) ──────────────────────────────
    sf_tbl = Table([
        [_p("Счёт-фактура №", sz=8, bold=True), _p(str(inv_num), sz=9, bold=True),
         _p("от", sz=8), _p(_date_v(invoice.date), sz=9, bold=True), _p("(1)", sz=6, color=GREY)],
        [_p("Исправление №", sz=7, color=GREY), _p("—", sz=7, color=GREY),
         _p("от", sz=7, color=GREY), _p("—", sz=7, color=GREY), _p("(1а)", sz=6, color=GREY)],
    ], colWidths=[26*mm, 22*mm, 8*mm, 45*mm, 12*mm])
    sf_tbl.setStyle(TableStyle([
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("LEFTPADDING",(0,0),(-1,-1),2), ("RIGHTPADDING",(0,0),(-1,-1),2),
        ("TOPPADDING",(0,0),(-1,-1),1), ("BOTTOMPADDING",(0,0),(-1,-1),1),
    ]))
    story.append(sf_tbl)
    story.append(Spacer(1, 1*mm))

    # ══════════════════════════════════════════════════════════════
    # 2. РЕКВИЗИТЫ СТОРОН — две колонки в общей рамке
    # ══════════════════════════════════════════════════════════════
    def _col(rows, w):
        """rows: список (label, value, ref). Возвращает вложенную таблицу."""
        data = [[_p(l, sz=6, color=GREY), _p(v, sz=7), _p(r, sz=6, color=GREY)] for l, v, r in rows]
        t = Table(data, colWidths=[w*0.32, w*0.62, w*0.06])
        t.setStyle(TableStyle([
            ("VALIGN",(0,0),(-1,-1),"TOP"),
            ("LEFTPADDING",(0,0),(-1,-1),2), ("RIGHTPADDING",(0,0),(-1,-1),2),
            ("TOPPADDING",(0,0),(-1,-1),1), ("BOTTOMPADDING",(0,0),(-1,-1),1),
            ("LINEBELOW",(0,0),(-1,-2),0.25,colors.lightgrey),
        ]))
        return t

    LW = W * 0.58
    RW = W - LW
    left_rows = [
        ("Продавец:",                       company.name or "—", "(2)"),
        ("Адрес:",                          company.legal_address or "—", "(2а)"),
        ("ИНН/КПП продавца:",               inn_kpp, "(2б)"),
        ("Грузоотправитель и его адрес:",   "он же", "(3)"),
        ("Грузополучатель и его адрес:",     f"{cp.name}; {cp_addr}", "(4)"),
        ("К платёжно-расчётному документу №:", f"{inv_num} от {inv_date}", "(5)"),
        ("Документ об отгрузке:",            f"Универсальный передаточный документ, № {inv_num} от {_date_v(invoice.date)}", "(5а)"),
    ]
    right_rows = [
        ("Покупатель:",                      cp.name or "—", "(6)"),
        ("Адрес:",                           cp_addr, "(6а)"),
        ("ИНН/КПП покупателя:",              cp_inn_kpp, "(6б)"),
        ("Валюта: наименование, код",        "Российский рубль, 643", "(7)"),
        ("Идентификатор гос. контракта, договора (соглашения) (при наличии):", "—", "(8)"),
    ]
    parties = Table([[_col(left_rows, LW), _col(right_rows, RW)]], colWidths=[LW, RW])
    parties.setStyle(TableStyle([
        ("BOX",(0,0),(-1,-1),0.6,BORDER),
        ("LINEBEFORE",(1,0),(1,0),0.4,colors.grey),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("LEFTPADDING",(0,0),(-1,-1),0), ("RIGHTPADDING",(0,0),(-1,-1),0),
        ("TOPPADDING",(0,0),(-1,-1),0), ("BOTTOMPADDING",(0,0),(-1,-1),0),
    ]))
    story.append(parties)
    story.append(Spacer(1, 1.5*mm))

    # ══════════════════════════════════════════════════════════════
    # 3. ТАБЛИЦА ПОЗИЦИЙ (сгруппированная шапка)
    # ══════════════════════════════════════════════════════════════
    #         А    1    1а    1б   2    2а    3    4    5    6    7    8    9   10  10а  11
    CW = [12*mm,7*mm,46*mm,11*mm,9*mm,14*mm,13*mm,15*mm,18*mm,12*mm,11*mm,16*mm,18*mm,10*mm,13*mm,17*mm]

    def _hc(txt, sz=5.6): return _p(txt, sz=sz, bold=True, align="CENTER")

    # row 0 — групповые заголовки
    h0 = [
        _hc("Код товара/ работ, услуг"), _hc("№ п/п"),
        _hc("Наименование товара (описание выполненных работ, оказанных услуг), имущественного права"),
        _hc("Код вида товара"),
        _hc("Единица измерения"), "",
        _hc("Коли-чество (объём)"),
        _hc("Цена (тариф) за единицу измерения"),
        _hc("Стоимость товаров (работ, услуг), имуществ. прав без налога – всего"),
        _hc("В том числе сумма акциза"),
        _hc("Нало-говая ставка"),
        _hc("Сумма налога, предъяв-ляемая покупателю"),
        _hc("Стоимость товаров (работ, услуг), имуществ. прав с налогом – всего"),
        _hc("Страна происхождения товара"), "",
        _hc("Рег. номер декларации на товары / партии товара"),
    ]
    # row 1 — подзаголовки сгруппированных колонок
    h1 = ["", "", "", "", _hc("код"), _hc("условное обозначение (нацио-нальное)"),
          "", "", "", "", "", "", "", _hc("цифро-вой код"), _hc("краткое наимено-вание"), ""]
    # row 2 — номера граф
    nums = ["А","1","1а","1б","2","2а","3","4","5","6","7","8","9","10","10а","11"]
    h2 = [_p(x, sz=5.5, color=GREY, align="CENTER") for x in nums]

    rows = [h0, h1, h2]

    total_without = total_vat = total_with = 0.0
    for idx, item in enumerate(invoice.items, 1):
        code    = (item.product.article or "—") if item.product else "—"
        vat_r   = item.vat_rate or 0
        without = item.price * item.quantity
        vat_amt = without * vat_r / 100 if vat_r > 0 else 0
        with_t  = without + vat_amt
        total_without += without
        total_vat     += vat_amt
        total_with    += with_t
        vat_str = "Без НДС" if vat_r == 0 else f"{int(vat_r)}%"
        vat_sum = "—" if vat_r == 0 else _money(vat_amt)
        rows.append([
            _p(code, sz=6.5),
            _p(str(idx), sz=7, align="CENTER"),
            _p(item.name, sz=7),
            _p("—", sz=7, align="CENTER"),
            _p("796", sz=7, align="CENTER"),
            _p(item.unit, sz=7, align="CENTER"),
            _p(_money(item.quantity).replace(",00",""), sz=7, align="RIGHT"),
            _p(_money(item.price), sz=7, align="RIGHT"),
            _p(_money(without), sz=7, align="RIGHT"),
            _p("Без акциза", sz=6, align="CENTER"),
            _p(vat_str, sz=7, align="CENTER"),
            _p(vat_sum, sz=7, align="RIGHT"),
            _p(_money(with_t), sz=7, align="RIGHT"),
            _p("—", sz=7, align="CENTER"),
            _p("—", sz=7, align="CENTER"),
            _p("—", sz=7, align="CENTER"),
        ])

    # итоговая строка «Всего к оплате (9)»
    rows.append([
        _p("Всего к оплате (9)", sz=7, bold=True), "", "", "", "", "", "", "",
        _p(_money(total_without), sz=7, bold=True, align="RIGHT"),
        _p("Х", sz=7, align="CENTER"),
        "",
        _p(_money(total_vat) if total_vat else "—", sz=7, bold=True, align="RIGHT"),
        _p(_money(total_with), sz=7, bold=True, align="RIGHT"),
        "", "", "",
    ])

    n = len(rows)
    tbl = Table(rows, colWidths=CW, repeatRows=3)
    tstyle = [
        ("BACKGROUND",  (0,0),(-1,2),   HDR_BG),
        ("GRID",        (0,0),(-1,n-2), 0.3, colors.HexColor("#999999")),
        ("LINEABOVE",   (0,n-1),(-1,n-1), 0.6, BORDER),
        ("LINEBELOW",   (0,n-1),(-1,n-1), 0.6, BORDER),
        ("BOX",         (0,n-1),(-1,n-1), 0.6, BORDER),
        ("SPAN",        (0,n-1),(7,n-1)),
        ("VALIGN",      (0,0),(-1,-1),  "MIDDLE"),
        ("TOPPADDING",  (0,0),(-1,-1),  1.5),
        ("BOTTOMPADDING",(0,0),(-1,-1), 1.5),
        ("LEFTPADDING", (0,0),(-1,-1),  2),
        ("RIGHTPADDING",(0,0),(-1,-1),  2),
        # групповые объединения шапки
        ("SPAN", (4,0),(5,0)),     # Единица измерения
        ("SPAN", (13,0),(14,0)),   # Страна происхождения
    ]
    # вертикальный rowspan для несгруппированных колонок (строки 0-1)
    for c in [0,1,2,3,6,7,8,9,10,11,12,15]:
        tstyle.append(("SPAN", (c,0),(c,1)))
    tbl.setStyle(TableStyle(tstyle))
    story.append(tbl)
    story.append(Spacer(1, 2*mm))

    # ══════════════════════════════════════════════════════════════
    # 4. НИЖНИЙ БЛОК (поля [8]–[19])
    # ══════════════════════════════════════════════════════════════
    story.append(_p("Документ составлен на 1 листе", sz=6, color=GREY))
    story.append(Spacer(1, 0.5*mm))

    HW = W / 2

    # — Руководитель / Главный бухгалтер —
    ruk = Table([[
        _p("Руководитель организации<br/>или иное уполномоченное лицо", sz=6),
        _sign_block(HW*0.55, [(0.5, "(подпись)", ""), (0.5, "(ф.и.о.)", "")]),
    ]], colWidths=[HW*0.45, HW*0.55])
    gb = Table([[
        _p("Главный бухгалтер<br/>или иное уполномоченное лицо", sz=6),
        _sign_block(HW*0.55, [(0.5, "(подпись)", ""), (0.5, "(ф.и.о.)", "")]),
    ]], colWidths=[HW*0.45, HW*0.55])
    for t in (ruk, gb):
        t.setStyle(TableStyle([
            ("VALIGN",(0,0),(-1,-1),"BOTTOM"),
            ("LEFTPADDING",(0,0),(-1,-1),0), ("RIGHTPADDING",(0,0),(-1,-1),2),
            ("TOPPADDING",(0,0),(-1,-1),0), ("BOTTOMPADDING",(0,0),(-1,-1),0),
        ]))
    rukgb = Table([[ruk, gb]], colWidths=[HW, HW])
    rukgb.setStyle(TableStyle([("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
                               ("TOPPADDING",(0,0),(-1,-1),1),("BOTTOMPADDING",(0,0),(-1,-1),1),
                               ("VALIGN",(0,0),(-1,-1),"BOTTOM")]))
    story.append(rukgb)

    # — Индивидуальный предприниматель + ОГРН —
    if is_ip:
        ip = Table([[
            _p("Индивидуальный предприниматель<br/>или иное уполномоченное лицо", sz=6),
            _sign_block(HW*0.55, [(0.5, "(подпись)", ""), (0.5, "(ф.и.о.)", dname)]),
        ]], colWidths=[HW*0.45, HW*0.55])
        ip.setStyle(TableStyle([
            ("VALIGN",(0,0),(-1,-1),"BOTTOM"),
            ("LEFTPADDING",(0,0),(-1,-1),0), ("RIGHTPADDING",(0,0),(-1,-1),2),
            ("TOPPADDING",(0,0),(-1,-1),0), ("BOTTOMPADDING",(0,0),(-1,-1),0),
        ]))
        ogrn = Table([
            [_p(f"ОГРНИП {company.ogrn or ''}, дата регистрации «__» __________ 20__ г." if hasattr(company,'ogrn') else
                "ОГРНИП , дата регистрации «__» __________ 20__ г.", sz=6.5)],
            [_p("(основной государственный регистрационный номер индивидуального предпринимателя и дата присвоения такого номера)",
                sz=5.5, color=GREY)],
        ], colWidths=[HW])
        ogrn.setStyle(TableStyle([
            ("LEFTPADDING",(0,0),(-1,-1),2), ("RIGHTPADDING",(0,0),(-1,-1),2),
            ("TOPPADDING",(0,0),(-1,-1),0), ("BOTTOMPADDING",(0,0),(-1,-1),0),
        ]))
        iprow = Table([[ip, ogrn]], colWidths=[HW, HW])
        iprow.setStyle(TableStyle([("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
                                   ("TOPPADDING",(0,0),(-1,-1),1),("BOTTOMPADDING",(0,0),(-1,-1),1),
                                   ("VALIGN",(0,0),(-1,-1),"BOTTOM")]))
        story.append(iprow)

    story.append(Spacer(1, 1*mm))

    # — Основание передачи [8] / Данные о транспортировке [9] —
    osn = Table([
        [_p("Основание передачи (сдачи) / получения (приёмки):", sz=7),
         _p(basis, sz=7), _p("[8]", sz=6, color=GREY)],
        [_p("(договор; доверенность и др.)", sz=5.5, color=GREY, align="CENTER"), "", ""],
    ], colWidths=[W*0.30, W*0.64, W*0.06])
    osn.setStyle(TableStyle([
        ("SPAN",(0,1),(2,1)),
        ("LINEBELOW",(1,0),(1,0),0.4,BORDER),
        ("VALIGN",(0,0),(-1,-1),"BOTTOM"),
        ("LEFTPADDING",(0,0),(-1,-1),2), ("RIGHTPADDING",(0,0),(-1,-1),2),
        ("TOPPADDING",(0,0),(-1,-1),1), ("BOTTOMPADDING",(0,0),(-1,-1),0),
    ]))
    story.append(osn)
    trn = Table([
        [_p("Данные о транспортировке и грузе:", sz=7), _p("", sz=7), _p("[9]", sz=6, color=GREY)],
        [_p("(транспортная накладная, поручение экспедитору, экспедиторская / складская расписка и др. / масса нетто/брутто груза)",
            sz=5.5, color=GREY, align="CENTER"), "", ""],
    ], colWidths=[W*0.30, W*0.64, W*0.06])
    trn.setStyle(TableStyle([
        ("SPAN",(0,1),(2,1)),
        ("LINEBELOW",(1,0),(1,0),0.4,BORDER),
        ("VALIGN",(0,0),(-1,-1),"BOTTOM"),
        ("LEFTPADDING",(0,0),(-1,-1),2), ("RIGHTPADDING",(0,0),(-1,-1),2),
        ("TOPPADDING",(0,0),(-1,-1),1), ("BOTTOMPADDING",(0,0),(-1,-1),0),
    ]))
    story.append(trn)
    story.append(Spacer(1, 1*mm))

    # — Передал / Получил (поля [10]–[19]) —
    def _hand_block(width, title, fld_top, resp_name, fld_resp, subj_name, fld_subj,
                    date_label, date_value, fld_date):
        rows = [
            [_p(title, sz=7, bold=True), _p(fld_top, sz=6, color=GREY, align="RIGHT")],
        ]
        t_head = Table(rows, colWidths=[width*0.9, width*0.1])
        t_head.setStyle(TableStyle([("LEFTPADDING",(0,0),(-1,-1),2),("RIGHTPADDING",(0,0),(-1,-1),2),
                                     ("TOPPADDING",(0,0),(-1,-1),1),("BOTTOMPADDING",(0,0),(-1,-1),0),
                                     ("VALIGN",(0,0),(-1,-1),"TOP")]))
        sig1 = _sign_block(width, [(0.45,"(должность)",""),(0.30,"(подпись)",""),(0.25,"(ф.и.о.)","")])
        dt = Table([[_p(date_label, sz=7), _p(date_value, sz=7), _p(fld_date, sz=6, color=GREY)]],
                   colWidths=[width*0.50, width*0.42, width*0.08])
        dt.setStyle(TableStyle([("LINEBELOW",(1,0),(1,0),0.4,BORDER),
                                 ("LEFTPADDING",(0,0),(-1,-1),2),("RIGHTPADDING",(0,0),(-1,-1),2),
                                 ("TOPPADDING",(0,0),(-1,-1),2),("BOTTOMPADDING",(0,0),(-1,-1),0),
                                 ("VALIGN",(0,0),(-1,-1),"BOTTOM")]))
        other = Table([[_p("Иные сведения:", sz=6, color=GREY)]], colWidths=[width])
        other.setStyle(TableStyle([("LINEBELOW",(0,0),(0,0),0.4,BORDER),
                                    ("LEFTPADDING",(0,0),(-1,-1),2),("TOPPADDING",(0,0),(-1,-1),3),
                                    ("BOTTOMPADDING",(0,0),(-1,-1),0)]))
        resp_lbl = Table([[_p("Ответственный за правильность оформления факта хозяйственной жизни", sz=6, color=GREY),
                           _p(fld_resp, sz=6, color=GREY, align="RIGHT")]], colWidths=[width*0.9, width*0.1])
        resp_lbl.setStyle(TableStyle([("LEFTPADDING",(0,0),(-1,-1),2),("RIGHTPADDING",(0,0),(-1,-1),2),
                                      ("TOPPADDING",(0,0),(-1,-1),2),("BOTTOMPADDING",(0,0),(-1,-1),0)]))
        sig2 = _sign_block(width, [(0.45,"(должность)",""),(0.30,"(подпись)",""),(0.25,"(ф.и.о.)",resp_name)])
        subj = Table([
            [_p("Наименование экономического субъекта – составителя документа", sz=6, color=GREY),
             _p(fld_subj, sz=6, color=GREY, align="RIGHT")],
            [_p(subj_name, sz=7), ""],
            [_p("М.П.", sz=7, bold=True), ""],
        ], colWidths=[width*0.9, width*0.1])
        subj.setStyle(TableStyle([("SPAN",(0,1),(1,1)),("SPAN",(0,2),(1,2)),
                                   ("LINEBELOW",(0,1),(1,1),0.4,BORDER),
                                   ("LEFTPADDING",(0,0),(-1,-1),2),("RIGHTPADDING",(0,0),(-1,-1),2),
                                   ("TOPPADDING",(0,0),(-1,-1),1),("BOTTOMPADDING",(0,0),(-1,-1),1)]))
        col = Table([[t_head],[sig1],[dt],[other],[resp_lbl],[sig2],[subj]], colWidths=[width])
        col.setStyle(TableStyle([("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
                                  ("TOPPADDING",(0,0),(-1,-1),1),("BOTTOMPADDING",(0,0),(-1,-1),1),
                                  ("VALIGN",(0,0),(-1,-1),"TOP")]))
        return col

    seller_full = (company.name or "—") + (f", ИНН {company.inn}" if company.inn else "")
    buyer_full  = (cp.name or "—") + (f", ИНН {cp.inn}" if cp.inn else "")

    left_block = _hand_block(
        HW - 2*mm,
        "Товар (груз) передал / услуги, результаты работ, права сдал", "[10]",
        resp_name=dname, fld_resp="[13]",
        subj_name=seller_full, fld_subj="[14]",
        date_label="Дата отгрузки, передачи (сдачи)",
        date_value=_date_v(invoice.date), fld_date="[11]")
    right_block = _hand_block(
        HW - 2*mm,
        "Товар (груз) получил / услуги, результаты работ, права принял", "[15]",
        resp_name="", fld_resp="[18]",
        subj_name=buyer_full, fld_subj="[19]",
        date_label="Дата получения (приёмки)",
        date_value="«__» __________ 20__ г.", fld_date="[16]")

    hands = Table([[left_block, right_block]], colWidths=[HW, HW])
    hands.setStyle(TableStyle([
        ("BOX",(0,0),(-1,-1),0.6,BORDER),
        ("LINEBEFORE",(1,0),(1,0),0.4,colors.grey),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("LEFTPADDING",(0,0),(-1,-1),2), ("RIGHTPADDING",(0,0),(-1,-1),2),
        ("TOPPADDING",(0,0),(-1,-1),1), ("BOTTOMPADDING",(0,0),(-1,-1),1),
    ]))
    story.append(hands)

    doc.build(story)
    return buf.getvalue()

"""Транспортная накладная — официальная форма ПП РФ № 2116 от 30.11.2021."""
import io, os
from datetime import date
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, PageBreak,
)
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

BLACK  = colors.HexColor("#000000")
LINE_C = colors.HexColor("#aaaaaa")   # soft underline for value rows
GREY   = colors.HexColor("#666666")   # hint text


def _s(sz=8, bold=False, align="LEFT", color=None):
    return ParagraphStyle(
        "_", fontName=(_FB if bold else _F), fontSize=sz,
        leading=max(sz + 2, sz * 1.3),
        alignment={"LEFT": 0, "CENTER": 1, "RIGHT": 2}.get(align, 0),
        textColor=color or colors.black,
        spaceAfter=0, spaceBefore=0,
    )


def _v(txt="", bold=False, align="LEFT", sz=8):
    """Value text (8 pt)."""
    return Paragraph(str(txt) if txt else " ", _s(sz=sz, bold=bold, align=align))


def _h(txt, sz=6):
    """Hint text (6 pt grey italic)."""
    return Paragraph(str(txt), _s(sz=sz, color=GREY))


def _sh(num, title, extra=""):
    """Section header paragraph."""
    if extra:
        return Paragraph(f"<b>{num}. {title}</b>  {extra}", _s(sz=8, bold=True))
    return Paragraph(f"<b>{num}. {title}</b>", _s(sz=8, bold=True))


def _date_s(d):
    if not d: return "—"
    return d.strftime("%d.%m.%Y") if isinstance(d, date) else str(d)


def _date_dm(d):
    if not d: return ""
    return d.strftime("%d.%m") if isinstance(d, date) else str(d)


# ── table style helpers ───────────────────────────────────────────────────────
_BASE = [
    ("FONTSIZE",       (0,0),(-1,-1), 8),
    ("FONTNAME",       (0,0),(-1,-1), _F),
    ("VALIGN",         (0,0),(-1,-1), "TOP"),
    ("TOPPADDING",     (0,0),(-1,-1), 1),
    ("BOTTOMPADDING",  (0,0),(-1,-1), 1),
    ("LEFTPADDING",    (0,0),(-1,-1), 2),
    ("RIGHTPADDING",   (0,0),(-1,-1), 2),
]


def _tbl(rows, cw, extra=()):
    t = Table(rows, colWidths=cw)
    t.setStyle(TableStyle(
        [("BOX", (0,0),(-1,-1), 0.5, BLACK)] + _BASE + list(extra)
    ))
    t.spaceAfter = 0
    t.spaceBefore = 0
    return t


def _lb(row, c1=0, c2=-1):
    """LINEBELOW — underline for a value row."""
    return ("LINEBELOW", (c1, row), (c2, row), 0.4, LINE_C)


def _lbv(row, c1=0, c2=-1):
    """LINEBELOW dark — for internal section header borders."""
    return ("LINEBELOW", (c1, row), (c2, row), 0.5, BLACK)


def _vline(col, r1=0, r2=-1):
    return ("LINEBEFORE", (col, r1), (col, r2), 0.5, BLACK)


def _span(row, c1, c2):
    return ("SPAN", (c1, row), (c2, row))


def _org(c):
    parts = []
    is_ip = (c.name or "").strip().upper().startswith("ИП")
    if is_ip:
        parts.append(f"Индивидуальный предприниматель {c.name.strip()[3:].strip()}")
    else:
        parts.append(c.name or "")
    if c.inn: parts.append(f"ИНН {c.inn}")
    if c.legal_address: parts.append(c.legal_address)
    return ", ".join(filter(None, parts))


def _cporg(cp):
    parts = [cp.name or ""]
    if cp.inn: parts.append(f"ИНН {cp.inn}")
    if cp.legal_address: parts.append(cp.legal_address)
    return ", ".join(filter(None, parts))


# ══════════════════════════════════════════════════════════════════════════════
# TnData
# ══════════════════════════════════════════════════════════════════════════════

class TnData:
    def __init__(self, order, company, **kw):
        self.order   = order
        self.company = company
        self.carrier_name    = kw.get("carrier_name", "")
        self.carrier_inn     = kw.get("carrier_inn", "")
        self.driver_name     = kw.get("driver_name", "")
        self.vehicle_type    = kw.get("vehicle_type", "")
        self.vehicle_plate   = kw.get("vehicle_plate", "")
        self.pickup_address  = kw.get("pickup_address",
                               company.actual_address or company.legal_address or "")
        self.pickup_date     = kw.get("pickup_date", order.date)
        self.cargo_name      = kw.get("cargo_name", "")
        self.cargo_places    = kw.get("cargo_places", "")
        self.cargo_weight    = kw.get("cargo_weight", "")
        self.cargo_volume    = kw.get("cargo_volume", "")
        self.cargo_value     = kw.get("cargo_value",
                               str(int(order.total_amount)) if order.total_amount else "")
        self.docs            = kw.get("docs", "")
        self.shipping_cost   = kw.get("shipping_cost", "")
        self.delivery_address = kw.get("delivery_address",
                                order.delivery_address or
                                (order.counterparty.legal_address if order.counterparty else ""))
        self.delivery_date   = kw.get("delivery_date", order.delivery_date)
        self.tn_number       = kw.get("tn_number", str(order.id))
        self.order_number    = kw.get("order_number", order.number)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def generate_tn_pdf(tn: TnData) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=8*mm, rightMargin=8*mm,
                            topMargin=7*mm, bottomMargin=7*mm)
    W = A4[0] - 16*mm       # ≈ 179 mm

    order   = tn.order
    company = tn.company
    cp      = order.counterparty

    # Auto-fill cargo from order items
    if not tn.cargo_name and order.items:
        names = list(dict.fromkeys(
            item.product.name for item in order.items if item.product
        ))
        tn.cargo_name = ", ".join(names[:3]) + ("..." if len(names) > 3 else "")
    if not tn.cargo_places and order.items:
        q = sum(item.quantity for item in order.items)
        tn.cargo_places = str(int(q)) if q == int(q) else str(q)

    sender_str  = _org(company)
    carrier_str = tn.carrier_name + (f", ИНН {tn.carrier_inn}" if tn.carrier_inn else "")
    cp_str      = _cporg(cp)

    wt_str = ""
    if tn.cargo_weight: wt_str += f"{tn.cargo_weight} кг"
    if tn.cargo_volume: wt_str += f"  {tn.cargo_volume} м³"

    story = []

    # ══════════════════════════════════════════════════════
    # TOP-RIGHT ANNOTATION
    # ══════════════════════════════════════════════════════
    ann = _tbl([[
        "",
        _h("Приложение № 4\nк Правилам перевозок грузов автомобильным транспортом\n"
           "(в редакции постановления Правительства Российской Федерации\n"
           "от 30 ноября 2021 г. № 2116)", sz=6),
    ]], [W*0.42, W*0.58],
        extra=[("BOX",(0,0),(-1,-1),0,BLACK),   # no outer border here
               ("ALIGN",(1,0),(1,0),"RIGHT")])
    story.append(ann)

    story.append(Paragraph("<b>Транспортная накладная (форма)</b>",
                           _s(10, bold=True, align="CENTER")))
    story.append(Spacer(1, 2*mm))

    # ══════════════════════════════════════════════════════
    # DOCUMENT NUMBERS HEADER
    # ══════════════════════════════════════════════════════
    HW = W / 2
    # Two top cells: "Транспортная накладная" and "Заказ (заявка)"
    # Then date/number rows below each
    doc_hdr = _tbl([
        # row 0: titles
        [_v("Транспортная накладная", bold=True, align="CENTER"),
         _v("Заказ (заявка)", bold=True, align="CENTER")],
        # row 1: Дата / №
        [
            _tbl([[
                _v(f"Дата   {_date_s(tn.pickup_date)}"),
                _v("№"),
                _v(tn.tn_number),
            ]], [HW*0.55, HW*0.08, HW*0.37],
                extra=[("BOX",(0,0),(-1,-1),0,BLACK),
                       ("TOPPADDING",(0,0),(-1,-1),0),
                       ("BOTTOMPADDING",(0,0),(-1,-1),0)]),
            _tbl([[
                _v(f"Дата   {_date_s(tn.pickup_date)}"),
                _v("№"),
                _v(tn.order_number),
            ]], [HW*0.55, HW*0.08, HW*0.37],
                extra=[("BOX",(0,0),(-1,-1),0,BLACK),
                       ("TOPPADDING",(0,0),(-1,-1),0),
                       ("BOTTOMPADDING",(0,0),(-1,-1),0)]),
        ],
        # row 2: Экземпляр
        [_v("Экземпляр № 1"), ""],
    ], [HW, HW],
        extra=[_lbv(0), _vline(1), _lb(1), _lb(2)])
    story.append(doc_hdr)

    # ══════════════════════════════════════════════════════
    # SECTIONS 1 + 1а  (side by side)
    # ══════════════════════════════════════════════════════
    s1_rows = [
        # header
        [_sh("1", "Грузоотправитель"),
         _v("является экспедитором", sz=7)],
        # sender name
        [_v(sender_str, bold=True), ""],
        [_h("(реквизиты, позволяющие идентифицировать Грузоотправителя)"), ""],
        # payment basis
        [_v(""), ""],
        [_h("реквизиты документа, определяющего основания осуществления платежей "
            "по договору перевозки иным лицом, отличным от грузоотправителя (при наличии)"), ""],
        # 1а header
        ["",
         _sh("1а", "«Заказчик услуг, связанных с перевозкой груза (при наличии)»")],
        ["", _v("")],
        ["", _h("(реквизиты, позволяющие идентифицировать Заказчика услуг, "
                "связанных с перевозкой груза)")],
        ["", _v("")],
        ["", _h("(реквизиты договора на выполнение услуг, связанных с перевозкой груза)")],
    ]
    s1 = _tbl(s1_rows, [HW, HW], extra=[
        _span(0, 0, 0),  # left header spans only left col
        _span(1, 0, 1),  # sender spans both cols
        _span(2, 0, 1),
        _span(3, 0, 0),  # empty field left only
        _span(4, 0, 0),
        _vline(1, 5, 9),  # vertical divider for 1а starts at row 5
        _lbv(0),
        _lb(1), _lb(3),
        _lbv(5), _lb(6), _lb(8),
    ])
    story.append(s1)

    # ══════════════════════════════════════════════════════
    # SECTION 2: ГРУЗОПОЛУЧАТЕЛЬ
    # ══════════════════════════════════════════════════════
    s2 = _tbl([
        [_sh("2", "Грузополучатель")],
        [_v(cp_str, bold=True)],
        [_h("(реквизиты, позволяющие идентифицировать Грузополучателя)")],
        [_v(tn.delivery_address or "")],
        [_h("(адрес места доставки груза)")],
    ], [W], extra=[_lbv(0), _lb(1), _lb(3)])
    story.append(s2)

    # ══════════════════════════════════════════════════════
    # SECTION 3: ГРУЗ
    # ══════════════════════════════════════════════════════
    GW = W * 0.62
    GR = W - GW
    s3 = _tbl([
        # row 0: header
        [_sh("3", "Груз"), ""],
        # row 1: cargo name + places
        [_v(tn.cargo_name or ""), _v(tn.cargo_places or "")],
        [_h("(отгрузочное наименование груза (для опасных грузов – в соответствии с ДОПОГ), "
            "его состояние и другая необходимая информация о грузе)"),
         _h("(количество грузовых мест; маркировка, вид тары и способ упаковки)")],
        # row 3: mass (full width)
        [_v(wt_str or ""), ""],
        [_h("(масса груза брутто в килограммах, масса груза нетто в килограммах "
            "(при возможности ее определения), размеры (высота, ширина, длина) в метрах "
            "(при перевозке крупногабаритного груза), объём груза в кубических метрах)"), ""],
        # row 5: dangerous goods left / declared value right
        [_v(""), _v(tn.cargo_value or "")],
        [_h("(в случае перевозки опасного груза – информация по каждому опасному веществу, "
            "материалу или изделию в соответствии с пунктом 5.4.1 ДОПОГ)"),
         _h("(объявленная стоимость (ценность) груза (при необходимости))")],
    ], [GW, GR], extra=[
        _span(0, 0, 1), _span(3, 0, 1), _span(4, 0, 1),
        _vline(1, 1, 2), _vline(1, 5, 6),
        _lbv(0), _lb(1), _lb(3), _lb(5),
    ])
    story.append(s3)

    # ══════════════════════════════════════════════════════
    # SECTION 4: СОПРОВОДИТЕЛЬНЫЕ ДОКУМЕНТЫ
    # ══════════════════════════════════════════════════════
    s4 = _tbl([
        [_sh("4", "Сопроводительные документы на груз (при наличии)")],
        [_v(tn.docs or "")],
        [_h("(перечень прилагаемых к транспортной накладной документов, предусмотренных "
            "Соглашением о международной дорожной перевозке опасных грузов (ДОПОГ), "
            "санитарными, таможенными, карантинными, иными правилами в соответствии с "
            "законодательством Российской Федерации, либо регистрационные номера указанных "
            "документов, если такие документы содержатся в государственных информационных системах)")],
        [_v("")],
        [_h("(перечень прилагаемых к грузу сертификатов, паспортов качества, удостоверений, "
            "разрешений, инструкций, товарораспорядительных и других документов, наличие которых "
            "установлено законодательством Российской Федерации)")],
        [_v("")],
        [_h("(реквизиты документа, подтверждающего об отгрузке товаров: наименование, "
            "номер, дата документа об отгрузке, наименование и ИНН продавца и покупателя)")],
    ], [W], extra=[_lbv(0), _lb(1), _lb(3), _lb(5)])
    story.append(s4)

    # ══════════════════════════════════════════════════════
    # SECTION 5: УКАЗАНИЯ ГРУЗООТПРАВИТЕЛЯ
    # ══════════════════════════════════════════════════════
    s5 = _tbl([
        [_sh("5", "Указания грузоотправителя по особым условиям перевозки"), ""],
        [_v(""), _v("")],
        [_h("(маршрут перевозки, дата и время/сроки доставки груза (при необходимости))"),
         _h("(контактная информация о лицах, по указанию которых может осуществляться переадресовка)")],
        [_v(""), _v("")],
        [_h("(указания, необходимые для выполнения фитосанитарных, санитарных, карантинных, "
            "таможенных и прочих требований, установленных законодательством)"),
         _h("(температурный режим перевозки груза (при необходимости), сведения о "
            "запорно-пломбировочных устройствах, запрещение перегрузки груза)")],
    ], [HW, HW], extra=[_span(0,0,1), _vline(1,1,4), _lbv(0), _lb(1), _lb(3)])
    story.append(s5)

    # ══════════════════════════════════════════════════════
    # SECTION 6: ПЕРЕВОЗЧИК
    # ══════════════════════════════════════════════════════
    s6 = _tbl([
        [_sh("6", "Перевозчик"), ""],
        [_v(carrier_str or ""), _v(tn.driver_name or "")],
        [_h("(реквизиты, позволяющие идентифицировать Перевозчика)"),
         _h("(реквизиты, позволяющие идентифицировать водителя(ей))")],
    ], [HW, HW], extra=[_span(0,0,1), _vline(1,1,2), _lbv(0), _lb(1)])
    story.append(s6)

    # ══════════════════════════════════════════════════════
    # SECTION 7: ТРАНСПОРТНОЕ СРЕДСТВО
    # ══════════════════════════════════════════════════════
    # reg number in a box on the right
    VW = W * 0.62
    VR = W - VW
    s7 = _tbl([
        [_sh("7", "Транспортное средство"), ""],
        [_v(tn.vehicle_type or ""), _v(tn.vehicle_plate or "")],
        [_h("(тип, марка, грузоподъёмность (в тоннах), вместимость (в кубических метрах))"),
         _h("(регистрационный номер транспортного средства)")],
        [_v("Тип владения:  1 – собственность;  2 – совместная собственность супругов;  "
            "3 – аренда;  4 – лизинг"), ""],
        [_v(""), _v("")],
        [_h("(реквизиты документа(ов), подтверждающего(их) основание владения "
            "грузовым автомобилем (тягачом, а также прицепом (полуприцепом)) (для аренды и лизинга))"),
         _h("(номер, дата и срок действия специального разрешения, установленный маршрут движения "
            "тяжеловесного и (или) крупногабаритного транспортного средства)")],
    ], [VW, VR], extra=[
        _span(0,0,1), _span(3,0,1),
        _vline(1,1,2), _vline(1,4,5),
        _lbv(0), _lb(1), _lb(3), _lb(4),
    ])
    story.append(s7)

    # ══════════════════════════════════════════════════════
    # PAGE BREAK → page 2 (оборотная сторона)
    # ══════════════════════════════════════════════════════
    story.append(PageBreak())

    # Page 2 annotation
    ann2 = _tbl([[
        "",
        _h("Продолжение приложения № 4\nОборотная сторона", sz=6),
    ]], [W*0.6, W*0.4],
        extra=[("BOX",(0,0),(-1,-1),0,BLACK),
               ("ALIGN",(1,0),(1,0),"RIGHT")])
    story.append(ann2)
    story.append(Spacer(1, 1*mm))

    # ══════════════════════════════════════════════════════
    # SECTION 8: ПРИЁМ ГРУЗА
    # ══════════════════════════════════════════════════════
    PW = HW   # left half
    PR = HW   # right half

    s8 = _tbl([
        # row 0: header
        [_sh("8", "Прием груза"), ""],
        # row 1: who loaded
        [_v(company.name or ""), ""],
        [_h("(реквизиты лица, действующего по поручению грузоотправителя, осуществившего "
            "погрузку груза с указанием реквизитов документа, подтверждающего полномочия; "
            "в случае осуществления погрузки грузоотправителем – реквизиты грузоотправителя)"), ""],
        # row 3: infrastructure
        [_v(""), ""],
        [_h("(наименование организаций-владельцев объектов инфраструктуры пунктов погрузки "
            "и основания беспрепятственного доступа к таким объектам для погрузки груза "
            "в транспортное средство)"), ""],
        # row 5: address | date
        [_v(tn.pickup_address or ""), _v(_date_s(tn.pickup_date))],
        [_h("(адрес места погрузки)"), _h("(заявленные дата и время подачи транспортного "
                                           "средства под погрузку)")],
        # row 7: actual arrival | actual departure
        [_v(""), _v("")],
        [_h("(фактические дата и время прибытия под погрузку)"),
         _h("(фактические дата и время убытия)")],
        # row 9: mass
        [_v(tn.cargo_weight or ""), ""],
        [_h("(масса груза брутто и метод её определения: определение разницы между массой "
            "транспортного средства после погрузки и перед погрузкой, взвешиванием поосно "
            "или расчётная масса груза)"), ""],
        # row 11: places | packaging
        [_v(tn.cargo_places or ""), _v("")],
        [_h("(количество грузовых мест)"), _h("(тара, упаковка (при наличии))")],
        # row 13: notes
        [_v(""), ""],
        [_h("(оговорки и замечания перевозчика (при наличии) о дате и времени прибытия/убытия, "
            "о состоянии, креплении груза, тары, упаковки, маркировки, опломбирования, "
            "о массе груза и количестве грузовых мест, о проведении погрузочных работ)"), ""],
        # row 15: shipper signature | driver signature
        [_v(""), _v(tn.driver_name or "")],
        [_h("(Подпись, расшифровка подписи лица, осуществившего погрузку груза или "
            "уполномоченного лица с указанием реквизитов документа, подтверждающего "
            "полномочия лица на погрузку груза)"),
         _h("(подпись, расшифровка подписи водителя, принявшего груз для перевозки)")],
    ], [PW, PR], extra=[
        _span(0,0,1), _span(1,0,1), _span(2,0,1),
        _span(3,0,1), _span(4,0,1),
        _span(9,0,1), _span(10,0,1),
        _span(13,0,1), _span(14,0,1),
        _vline(1, 5, 8), _vline(1, 11, 12), _vline(1, 15, 16),
        _lbv(0),
        _lb(1), _lb(3),
        _lb(5), _lb(7),
        _lb(9),
        _lb(11), _lb(13), _lb(15),
    ])
    story.append(s8)

    # ══════════════════════════════════════════════════════
    # SECTION 9: ПЕРЕАДРЕСОВКА
    # ══════════════════════════════════════════════════════
    s9 = _tbl([
        [_sh("9", "Переадресовка (при наличии)"), ""],
        [_v(""), _v("")],
        [_h("(дата, вид переадресовки на бумажном носителе или в электронном виде "
            "(с указанием вида доставки документа))"),
         _h("(адрес нового пункта выгрузки, новые дата и время подачи транспортного "
            "средства под выгрузку)")],
        [_v(""), _v("")],
        [_h("(реквизиты лица, от которого получено указание на переадресовку)"),
         _h("(при изменении получателя груза – реквизиты нового получателя)")],
    ], [HW, HW], extra=[_span(0,0,1), _vline(1,1,4), _lbv(0), _lb(1), _lb(3)])
    story.append(s9)

    # ══════════════════════════════════════════════════════
    # SECTION 10: ВЫДАЧА ГРУЗА
    # ══════════════════════════════════════════════════════
    s10 = _tbl([
        [_sh("10", "Выдача груза"), ""],
        # row 1: address | scheduled date
        [_v(tn.delivery_address or ""), _v(_date_s(tn.delivery_date))],
        [_h("(адрес места выгрузки)"),
         _h("(заявленные дата и время подачи транспортного средства под выгрузку)")],
        # row 3: actual arrival | actual departure
        [_v(""), _v("")],
        [_h("(фактические дата и время прибытия)"),
         _h("(фактические дата и время убытия)")],
        # row 5: cargo state | places  — по образцу: рядом, не на всю ширину
        [_v(""), _v(tn.cargo_places or "")],
        [_h("(фактическое состояние груза, тары, упаковки, маркировки, опломбирования)"),
         _h("(количество грузовых мест)")],
        # row 7: mass | remarks
        [_v(tn.cargo_weight or ""), _v("")],
        [_h("(масса груза брутто в килограммах, масса груза нетто в килограммах "
            "(при возможности её определения))"),
         _h("(оговорки и замечания перевозчика о дате/времени, состоянии груза, тары, "
            "упаковки, маркировки, опломбирования, о массе и кол-ве мест)")],
        # row 9: recipient signature | driver signature
        [_v(cp.name or ""), _v(tn.driver_name or "")],
        [_h("(должность, подпись, расшифровка подписи грузополучателя или уполномоченного "
            "грузоотправителем лица)"),
         _h("(подпись, расшифровка подписи водителя, сдавшего груз грузополучателю "
            "или уполномоченному грузополучателем лицу)")],
    ], [HW, HW], extra=[
        _span(0,0,1),
        _vline(1, 1, 10),
        _lbv(0), _lb(1), _lb(3), _lb(5), _lb(7), _lb(9),
    ])
    story.append(s10)

    # ══════════════════════════════════════════════════════
    # SECTION 11: ОТМЕТКИ
    # ══════════════════════════════════════════════════════
    TW = W / 3
    s11 = _tbl([
        [_sh("11", "Отметки грузоотправителей, грузополучателей, перевозчиков (при необходимости)"),
         "", ""],
        [_v(""), _v(""), _v("")],
        [_h("(краткое описание обстоятельств, послуживших основанием для отметки, сведения о "
            "коммерческих и иных актах, в т. ч. о погрузке/выгрузке груза)"),
         _h("(Расчёт и размер штрафа)"),
         _h("(Подпись, дата)")],
    ], [TW, TW, TW], extra=[
        _span(0,0,2),
        _vline(1,1,2), _vline(2,1,2),
        _lbv(0), _lb(1),
    ])
    story.append(s11)

    # ══════════════════════════════════════════════════════
    # SECTION 12: СТОИМОСТЬ ПЕРЕВОЗКИ
    # ══════════════════════════════════════════════════════
    QW = W / 4
    LH = W / 2

    carrier_full = (tn.carrier_name or "—") + (f", ИНН {tn.carrier_inn}" if tn.carrier_inn else "")
    sender_full  = sender_str

    s12 = _tbl([
        # header
        [_sh("12", "Стоимость перевозки груза (установленная плата) в рублях (при необходимости)"),
         "", "", ""],
        # 4 columns: без налога / ставка / сумма налога / с налогом
        [_v(tn.shipping_cost or ""), _v(""), _v(""), _v("")],
        [_h("(стоимость услуг перевозки без налога – всего)"),
         _h("(налоговая ставка)"),
         _h("(сумма налога, предъявляемая покупателю)"),
         _h("(стоимость услуг перевозки с налогом – всего)")],
        # порядок расчёта (full width)
        [_v(""), "", "", ""],
        [_h("(порядок (механизм) расчёта (исчислений) платы) (при наличии порядка (механизма))"),
         "", "", ""],
        # Two bottom columns: Перевозчик | Грузоотправитель
        [_v(carrier_full, bold=True), "", _v(sender_full, bold=True), ""],
        [_h("(реквизиты, позволяющие идентифицировать Экономического субъекта, "
            "составляющего документ о факте хозяйственной жизни со стороны Перевозчика)"),
         "",
         _h("(реквизиты, позволяющие идентифицировать Экономического субъекта, "
            "составляющего документ о факте хозяйственной жизни со стороны Грузоотправителя)"),
         ""],
        [_v(""), "", _v(""), ""],
        [_h("(основание, по которому Экономический субъект является составителем "
            "документа о факте хозяйственной жизни)"),
         "",
         _h("(реквизиты, позволяющие идентифицировать лицо, от которого будут поступать "
            "денежные средства)"),
         ""],
        [_v(""), "", _v(""), ""],
        [_h("(подпись, расшифровка подписи лица, ответственного за оформление "
            "факта хозяйственной жизни со стороны Перевозчика (уполномоченного лица))"),
         "",
         _h("(подпись, расшифровка подписи лица, ответственного за оформление "
            "факта хозяйственной жизни со стороны Грузоотправителя (уполномоченного лица))"),
         ""],
        [_v(""), "", _v(""), ""],
        [_h("(должность, основание полномочий физического лица, уполномоченного Перевозчиком, "
            "дата подписания)"),
         "",
         _h("(должность, основание полномочий физического лица, уполномоченного "
            "Грузоотправителем, дата подписания)"),
         ""],
    ], [QW, QW, QW, QW], extra=[
        # header spans full width
        _span(0, 0, 3),
        # bottom halves span 2 cols each
        _span(3, 0, 3), _span(4, 0, 3),
        _span(5, 0, 1), _span(5, 2, 3),
        _span(6, 0, 1), _span(6, 2, 3),
        _span(7, 0, 1), _span(7, 2, 3),
        _span(8, 0, 1), _span(8, 2, 3),
        _span(9, 0, 1), _span(9, 2, 3),
        _span(10, 0, 1), _span(10, 2, 3),
        _span(11, 0, 1), _span(11, 2, 3),
        _span(12, 0, 1), _span(12, 2, 3),
        # vertical dividers
        _vline(1, 1, 2), _vline(2, 1, 2), _vline(3, 1, 2),
        _vline(2, 5, 12),
        _lbv(0),
        _lb(1), _lb(3),
        _lb(5), _lb(7), _lb(9), _lb(11),
    ])
    story.append(s12)

    doc.build(story)
    return buf.getvalue()

"""
Построение формализованного УПД (СЧФДОП) в формате ФНС, КНД 1115131, версия 5.03.

Собирается из данных заказа NERPA — БЕЗ 1С. Результат (windows-1251 XML) кладётся
вложением в СБИС.ЗаписатьДокумент, СБИС показывает карточку с контрагентом и
товарной таблицей. Эталон — реальная выгрузка 1С (ON_NSCHFDOPPR_…xml).

Вариант «без НДС» (продавец на спецрежиме) — как в реальной выгрузке. Если
появятся товары с НДС, ветку НалСт/СумНал надо будет расширить.
"""
import uuid
from datetime import datetime
from xml.sax.saxutils import escape

OKEI_SHT = "796"  # ОКЕИ штук


def _a(v) -> str:
    """Экранирование значения XML-атрибута (кавычки → &quot;)."""
    return escape("" if v is None else str(v), {'"': "&quot;"})


def _money(x) -> str:
    return f"{float(x or 0):.2f}"


def _num(x) -> str:
    """Число без лишних нулей: 42.0 → 42, 42.5 → 42.5."""
    return ("%g" % float(x or 0))


def _fio(full_name: str | None) -> tuple[str, str, str]:
    """«Фамилия Имя Отчество» из названия ИП (срезаем префикс ИП)."""
    name = (full_name or "").strip()
    for pref in ("Индивидуальный предприниматель ", "ИП ", "индивидуальный предприниматель ", "ип "):
        if name.startswith(pref):
            name = name[len(pref):]
            break
    parts = name.split()
    fam = parts[0] if len(parts) > 0 else "-"
    im = parts[1] if len(parts) > 1 else "-"
    ot = parts[2] if len(parts) > 2 else ""
    return fam, im, ot


def _is_ip(party) -> bool:
    et = getattr(party, "entity_type", None)
    if et == "ip":
        return True
    if et in ("ooo", "company"):
        return False
    inn = (getattr(party, "inn", "") or "").strip()
    return len(inn) == 12


def _idsv(party) -> str:
    """Блок <ИдСв> — идентификация продавца/покупателя (ИП или ЮЛ)."""
    inn = getattr(party, "inn", "") or ""
    name = getattr(party, "name", "") or ""
    if _is_ip(party):
        fam, im, ot = _fio(getattr(party, "signatory", None) or name)
        ogrn = getattr(party, "ogrn", "") or ""
        ogrn_attr = f' ОГРНИП="{_a(ogrn)}"' if ogrn else ""
        ot_attr = f' Отчество="{_a(ot)}"' if ot else ""
        return (f'<ИдСв><СвИП ИННФЛ="{_a(inn)}"{ogrn_attr}>'
                f'<ФИО Фамилия="{_a(fam)}" Имя="{_a(im)}"{ot_attr}/></СвИП></ИдСв>')
    kpp = getattr(party, "kpp", "") or ""
    org = getattr(party, "name", "") or ""
    return (f'<ИдСв><СвЮЛ ИННЮЛ="{_a(inn)}" КПП="{_a(kpp)}" '
            f'НаимОрг="{_a(org)}"/></ИдСв>')


def _adr(party) -> str:
    addr = getattr(party, "legal_address", None) or getattr(party, "actual_address", None) or ""
    return (f'<Адрес><АдрИнф КодСтр="643" НаимСтран="РОССИЯ" '
            f'АдрТекст="{_a(addr)}"/></Адрес>')


def _bank(party) -> str:
    acc = getattr(party, "bank_account", None)
    if not acc:
        return ""
    name = getattr(party, "bank_name", "") or ""
    bik = getattr(party, "bank_bik", "") or ""
    cor = getattr(party, "bank_corr_account", "") or ""
    return (f'<БанкРекв НомерСчета="{_a(acc)}">'
            f'<СвБанк НаимБанк="{_a(name)}" БИК="{_a(bik)}" КорСчет="{_a(cor)}"/>'
            f'</БанкРекв>')


def _d(d) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


def upd_filename(order, company) -> str:
    """Имя файла обмена ФНС: ON_NSCHFDOPPR_<покуп ИНН>_<прод ИНН>_<ГГГГММДД>_<guid>_0_0_0_0_0_00.xml"""
    buyer = (getattr(order.counterparty, "inn", "") or "0")
    seller = (getattr(company, "inn", "") or "0")
    d = (order.date or datetime.now().date()).strftime("%Y%m%d")
    return f"ON_NSCHFDOPPR_{buyer}_{seller}_{d}_{uuid.uuid4()}_0_0_0_0_0_00.xml"


def build_upd_xml(order, company) -> bytes:
    """Заказ NERPA → формализованный УПД (СЧФДОП, КНД 1115131) в windows-1251."""
    cp = order.counterparty
    now = datetime.now()
    fname = upd_filename(order, company)
    id_file = fname[:-4]  # без .xml

    doc_num = order.number or ""
    doc_date = _d(order.date)

    seller_name = getattr(company, "name", "") or ""

    # ── Товарная таблица ──
    rows = []
    total_bez = 0.0
    total_uch = 0.0
    total_qty = 0.0
    n = 0
    for it in order.items:
        if not it.product:
            continue
        n += 1
        qty = float(it.quantity or 0)
        price = float(it.price or 0)
        st_bez = round(float(it.amount or 0), 2)
        st_uch = st_bez  # без НДС: с налогом = без налога
        total_bez += st_bez
        total_uch += st_uch
        total_qty += qty
        unit = getattr(it.product, "unit", None) or "шт"
        code = getattr(it.product, "article", None) or ""
        code_attr = f' КодТов="{_a(code)}"' if code else ""
        rows.append(
            f'<СведТов НомСтр="{n}" НаимТов="{_a(it.product.name)}" ОКЕИ_Тов="{OKEI_SHT}" '
            f'НаимЕдИзм="{_a(unit)}" КолТов="{_num(qty)}" ЦенаТов="{_money(price)}" '
            f'СтТовБезНДС="{_money(st_bez)}" НалСт="без НДС" СтТовУчНал="{_money(st_uch)}">'
            f'<ДопСведТов ПрТовРаб="1"{code_attr}/>'
            f'<Акциз><БезАкциз>без акциза</БезАкциз></Акциз>'
            f'<СумНал><БезНДС>без НДС</БезНДС></СумНал>'
            f'</СведТов>'
        )

    table = (
        "".join(rows) +
        f'<ВсегоОпл СтТовБезНДСВсего="{_money(total_bez)}" '
        f'СтТовУчНалВсего="{_money(total_uch)}" КолНеттоВс="{_num(total_qty)}">'
        f'<СумНалВсего><СумНал>0.00</СумНал></СумНалВсего></ВсегоОпл>'
    )

    # ── Основание передачи (договор) ──
    osn = ""
    contract = getattr(order, "contract", None)
    if contract:
        osn = (f'<ОснПер РеквНаимДок="Договор" РеквНомерДок="{_a(contract.number)}" '
               f'РеквДатаДок="{_a(_d(contract.date))}"/>')

    # ── Подписант (продавец) ──
    p_fam, p_im, p_ot = _fio(getattr(company, "director", None) or seller_name)
    p_ot_attr = f' Отчество="{_a(p_ot)}"' if p_ot else ""

    xml = (
        '<?xml version="1.0" encoding="windows-1251"?>'
        f'<Файл xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'ИдФайл="{_a(id_file)}" ВерсФорм="5.03" ВерсПрог="NERPA">'
        f'<Документ КНД="1115131" Функция="ДОП" '
        f'ПоФактХЖ="Документ об отгрузке товаров (выполнении работ), передаче имущественных прав (документ об оказании услуг)" '
        f'НаимДокОпр="Документ об отгрузке товаров (выполнении работ), передаче имущественных прав (Документ об оказании услуг)" '
        f'ДатаИнфПр="{now.strftime("%d.%m.%Y")}" ВремИнфПр="{now.strftime("%H.%M.%S")}" '
        f'НаимЭконСубСост="{_a(seller_name)}">'
        f'<СвСчФакт НомерДок="{_a(doc_num)}" ДатаДок="{_a(doc_date)}">'
        f'<СвПрод>{_idsv(company)}{_adr(company)}{_bank(company)}</СвПрод>'
        f'<ГрузОт><ОнЖе>он же</ОнЖе></ГрузОт>'
        f'<ГрузПолуч>{_idsv(cp)}{_adr(cp)}{_bank(cp)}</ГрузПолуч>'
        f'<ДокПодтвОтгрНом РеквНаимДок="Универсальный передаточный документ" '
        f'РеквНомерДок="{_a(doc_num)}" РеквДатаДок="{_a(doc_date)}"/>'
        f'<СвПокуп>{_idsv(cp)}{_adr(cp)}{_bank(cp)}</СвПокуп>'
        f'<ДенИзм КодОКВ="643" НаимОКВ="Российский рубль" КурсВал="1.00"/>'
        f'<ИнфПолФХЖ1>'
        f'<ТекстИнф Идентиф="ВидСчетаФактуры" Значен="Реализация"/>'
        f'<ТекстИнф Идентиф="ТолькоУслуги" Значен="false"/>'
        f'</ИнфПолФХЖ1>'
        f'</СвСчФакт>'
        f'<ТаблСчФакт>{table}</ТаблСчФакт>'
        f'<СвПродПер><СвПер СодОпер="Товары переданы." ВидОпер="Продажа" '
        f'ДатаПер="{_a(doc_date)}">{osn}</СвПер></СвПродПер>'
        f'<Подписант ТипПодпис="2" СпосПодтПолном="1">'
        f'<ФИО Фамилия="{_a(p_fam)}" Имя="{_a(p_im)}"{p_ot_attr}/></Подписант>'
        f'</Документ></Файл>'
    )
    return xml.encode("windows-1251", errors="replace")

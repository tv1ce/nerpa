"""Генерация XML УПД для ЭДО по формату ФНС.

ЧЕРНОВИК (v0.1) — формат 5.03, приказ ФНС России от 19.12.2023 № ЕД-7-26/970@
(КНД 1115131, «счёт-фактура и документ об отгрузке товаров»).

⚠️  Это первая версия без валидации по официальной XSD-схеме и без реальных
идентификаторов оператора ЭДО. Перед боевой отправкой нужно:
  • прогнать через XSD конкретного оператора (Диадок/СБИС/Такском);
  • подставить идентификаторы участников ЭДО (СвУчастЭДО, имя файла);
  • уточнить блок «Подписант» и сведения о передаче под вашу подпись (УКЭП).

Файл отдаётся в кодировке windows-1251 — этого требует формат ФНС.
"""
import uuid
from datetime import date, datetime


def _fmt_date(d) -> str:
    if isinstance(d, (date, datetime)):
        return d.strftime("%d.%m.%Y")
    return str(d or "")


def _num(v) -> str:
    """Число для ФНС: точка-разделитель, без лишних нулей у целых."""
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return "0"
    if f == int(f):
        return str(int(f))
    return f"{f:.2f}"


def _money(v) -> str:
    try:
        return f"{float(v or 0):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _esc(s) -> str:
    """Экранирование для XML-атрибутов/текста."""
    s = "" if s is None else str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def _split_fio(name: str):
    """Грубо разбирает строку имени ИП на Фамилию/Имя/Отчество.

    'ИП Грачева М. С.' -> ('Грачева', 'М.', 'С.')
    """
    s = (name or "").strip()
    for pref in ("Индивидуальный предприниматель", "ИП "):
        if s.startswith(pref):
            s = s[len(pref):].strip()
    parts = s.split()
    fam = parts[0] if len(parts) > 0 else ""
    im  = parts[1] if len(parts) > 1 else ""
    ot  = " ".join(parts[2:]) if len(parts) > 2 else ""
    return fam, im, ot


def _is_ip(entity_type: str | None, name: str | None) -> bool:
    if (entity_type or "").lower() == "ip":
        return True
    n = (name or "").strip().lower()
    return n.startswith("ип ") or "индивидуальный предприниматель" in n


def _party_ident(inn, kpp, name, is_ip, indent="          ") -> str:
    """Блок <ИдСв> идентификации участника (ЮЛ или ИП)."""
    if is_ip:
        fam, im, ot = _split_fio(name)
        fio = f'<Фамилия>{_esc(fam)}</Фамилия><Имя>{_esc(im)}</Имя>'
        if ot:
            fio += f'<Отчество>{_esc(ot)}</Отчество>'
        return (f'{indent}<ИдСв>\n'
                f'{indent}  <СвИП ИННФЛ="{_esc(inn)}"><ФИО>{fio}</ФИО></СвИП>\n'
                f'{indent}</ИдСв>')
    return (f'{indent}<ИдСв>\n'
            f'{indent}  <СвЮЛУч НаимОрг="{_esc(name)}" ИННЮЛ="{_esc(inn)}" КПП="{_esc(kpp)}"/>\n'
            f'{indent}</ИдСв>')


def _address(addr, indent="          ") -> str:
    """Блок <Адрес> (используем текстовый вариант АдрИнф)."""
    return (f'{indent}<Адрес>\n'
            f'{indent}  <АдрИнф КодСтр="643" НаимСтран="РОССИЯ" АдрТекст="{_esc(addr)}"/>\n'
            f'{indent}</Адрес>')


def generate_upd_xml(invoice, company):
    """Возвращает (filename, xml_bytes в windows-1251).

    invoice — модель Invoice с items, counterparty, contract.
    """
    cp = invoice.counterparty
    seller_ip = _is_ip(getattr(company, "entity_type", None), company.name)
    buyer_ip  = _is_ip(getattr(cp, "entity_type", None), cp.name)

    now = datetime.now()
    guid = str(uuid.uuid4()).upper()
    # Идентификаторы участников ЭДО оператор присваивает сам — пока используем ИНН.
    id_sender = (company.inn or "0000000000")
    id_recv   = (cp.inn or "0000000000")
    file_id = f"ON_NSCHFDOPPR_{id_recv}_{id_sender}_{now:%Y%m%d}_{guid}"
    filename = f"{file_id}.xml"

    seller_addr = company.legal_address or ""
    buyer_addr  = cp.legal_address or cp.actual_address or ""

    # ── позиции ──────────────────────────────────────────────────────────────
    rows = []
    total_without = total_with = 0.0
    has_vat = False
    for idx, item in enumerate(invoice.items, 1):
        vat_r   = item.vat_rate or 0
        without = (item.price or 0) * (item.quantity or 0)
        vat_amt = without * vat_r / 100 if vat_r > 0 else 0
        with_t  = without + vat_amt
        total_without += without
        total_with    += with_t
        if vat_r > 0:
            has_vat = True
            nal_st  = f"{int(vat_r)}%"
            sum_nal = f'<СумНал СумНал="{_money(vat_amt)}"/>'
        else:
            nal_st  = "без НДС"
            sum_nal = '<СумНал><БезНДС>без НДС</БезНДС></СумНал>'
        rows.append(
            f'      <СведТов НомСтр="{idx}" НаимТов="{_esc(item.name)}" '
            f'ОКЕИ_Тов="796" КолТов="{_num(item.quantity)}" '
            f'ЦенаТов="{_money(item.price)}" СтТовБезНДС="{_money(without)}" '
            f'НалСт="{nal_st}" СтТовУчНал="{_money(with_t)}">\n'
            f'        <Акциз><БезАкциз>без акциза</БезАкциз></Акциз>\n'
            f'        {sum_nal}\n'
            f'      </СведТов>'
        )

    total_vat = total_with - total_without
    if has_vat:
        sum_nal_total = f'<СумНалВсего СумНал="{_money(total_vat)}"/>'
    else:
        sum_nal_total = '<СумНалВсего><БезНДС>без НДС</БезНДС></СумНалВсего>'

    # ── основание передачи ───────────────────────────────────────────────────
    osn = ""
    if getattr(invoice, "contract", None):
        osn = (f'        <ОснПер РеквНаимДок="Договор" '
               f'РеквНомерДок="{_esc(invoice.contract.number)}" '
               f'РеквДатаДок="{_fmt_date(invoice.contract.date)}"/>\n')

    director = company.director or company.name or ""
    d_fam, d_im, d_ot = _split_fio(director)

    # ── сборка ────────────────────────────────────────────────────────────────
    xml = f'''<?xml version="1.0" encoding="windows-1251"?>
<Файл ИдФайл="{file_id}" ВерсПрог="TMS" ВерсФорм="5.03">
  <СвУчастЭДО>
    <СвОтпр ИдЭДО="{_esc(id_sender)}"/>
    <СвПол ИдЭДО="{_esc(id_recv)}"/>
  </СвУчастЭДО>
  <Документ КНД="1115131" Функция="ДОП" ДатаИнфПр="{now:%d.%m.%Y}" ВремИнфПр="{now:%H.%M.%S}" НаимЭконСубСост="{_esc(company.name)}" ПоФактХЖ="Документ об отгрузке товаров (выполнении работ), передаче имущественных прав (документ об оказании услуг)" НаимДокОпр="Универсальный передаточный документ">
    <СвСчФакт НомерДок="{_esc(invoice.number)}" ДатаДок="{_fmt_date(invoice.date)}">
      <СвПрод>
{_party_ident(company.inn, company.kpp, company.name, seller_ip)}
{_address(seller_addr)}
      </СвПрод>
      <ГрузОтпр>
        <ОнЖе>он же</ОнЖе>
      </ГрузОтпр>
      <ГрузПолуч>
{_party_ident(cp.inn, cp.kpp, cp.name, buyer_ip)}
{_address(buyer_addr)}
      </ГрузПолуч>
      <СвПокуп>
{_party_ident(cp.inn, cp.kpp, cp.name, buyer_ip)}
{_address(buyer_addr)}
      </СвПокуп>
      <ДенИзм КодОКВ="643" НаимОКВ="Российский рубль"/>
    </СвСчФакт>
    <ТаблСчФакт>
{chr(10).join(rows)}
      <ВсегоОпл СтТовБезНДСВсего="{_money(total_without)}" СтТовУчНалВсего="{_money(total_with)}">
        {sum_nal_total}
      </ВсегоОпл>
    </ТаблСчФакт>
    <СвПродПер>
      <СвПер СодОпер="Товары переданы">
{osn}        <ДатаПер>{_fmt_date(invoice.date)}</ДатаПер>
      </СвПер>
    </СвПродПер>
    <Подписант Должн="{_esc('Индивидуальный предприниматель' if seller_ip else 'Руководитель')}" Статус="1">
      <ФИО Фамилия="{_esc(d_fam)}" Имя="{_esc(d_im)}" Отчество="{_esc(d_ot)}"/>
    </Подписант>
  </Документ>
</Файл>'''

    return filename, xml.encode("windows-1251", errors="replace")

"""
Сборка подстановок («ключ-значение») для транспортных документов Saby.

Отдаётся в СБИС.СгенерироватьВложение — Saby сам собирает XML по утверждённому
формату. Мы НЕ строим XML руками, только маппим данные заказа NERPA в структуру
подстановки согласно документации Saby «Управление транспортом».

build_transport_order_substitution — ЭЗЗ (заказ-заявка перевозчику), КНД 1110361.

Роли в нашей схеме доставки:
  Грузоотправитель — наша компания (организатор перевозки, шлёт заявку);
  Грузоперевозчик  — order.carrier;
  Заказчик груза / грузополучатель — order.counterparty (клиент);
  Маршрут: пункт погрузки (наш склад) → пункт выгрузки (адрес клиента).
"""
import math
from datetime import date, datetime


def compute_gross_mass_kg(order, unit_weight_g: float) -> float:
    """Масса брутто груза, кг: сумма количеств по позициям × вес единицы (граммы).
    Вес единицы задаётся в настройках (для орешков ~20 г/шт)."""
    total_qty = sum((i.quantity or 0) for i in order.items)
    return round(total_qty * (unit_weight_g or 0) / 1000.0, 3)


def compute_cargo_places(order) -> int:
    """Оценка числа грузомест (коробок) из позиций заказа: по каждой позиции —
    округление вверх quantity / units_per_box. Служит авто-подсказкой, когда
    менеджер не указал фактическое число мест вручную."""
    total = 0
    for i in order.items:
        per_box = (getattr(i.product, "units_per_box", None) or 1) if i.product else 1
        per_box = per_box if per_box > 0 else 1
        total += math.ceil((i.quantity or 0) / per_box)
    return int(total) or (1 if order.items else 0)


def _d(d) -> str:
    """date/datetime → «ДД.ММ.ГГГГ»."""
    if isinstance(d, (date, datetime)):
        return d.strftime("%d.%m.%Y")
    return ""


def _dt(d, time_str: str = "09:00:00") -> str:
    """date → формат подачи ТС Saby: «ДД.ММ.ГГГГTЧЧ:ММ:СС+03:00»."""
    if not isinstance(d, (date, datetime)):
        return ""
    return f"{d.strftime('%d.%m.%Y')}T{time_str}+03:00"


def _contacts(phone: str | None, email: str | None) -> dict:
    """Блок «Контакты» — только непустые каналы (пустые массивы Saby не любит)."""
    c = {}
    if phone:
        c["Телефон"] = [{"Значение": str(phone)}]
    if email:
        c["ЭлектроннаяПочта"] = [{"Значение": str(email)}]
    return c


def _ul(name: str | None, inn: str | None, kpp: str | None = None) -> dict:
    """Реквизиты юрлица/ИП. КПП добавляем только если есть (у ИП его нет)."""
    ul = {"Наименование": name or "", "ИНН": inn or ""}
    if kpp:
        ul["КПП"] = kpp
    return {"ЮЛ": ul}


def build_transport_order_substitution(order, company) -> dict:
    """Заказ NERPA → подстановка «ЗаказЗаявка» (КНД 1110361) для СгенерироватьВложение.

    Заполняем то, что достоверно есть в заказе; необязательные блоки (условия
    перевозки, договор, промежуточные пункты) опускаем — Saby подставит по формату.
    """
    carrier = order.carrier
    cp      = order.counterparty

    pickup_addr   = order.pickup_address or company.actual_address or company.legal_address or ""
    delivery_addr = order.delivery_address or (cp.actual_address if cp else "") or (cp.legal_address if cp else "") or ""
    подача_dt     = _dt(order.delivery_date or order.date)

    # Консолидированный груз: одна позиция — общее наименование + фактические
    # места/паллеты (ручной ввод в заказе; иначе места считаем из коробок).
    cargo_name = (order.cargo_name or "").strip() \
        or (company.saby_cargo_name or "").strip() or "Груз"
    places = order.cargo_places if order.cargo_places else compute_cargo_places(order)
    mass_kg = compute_gross_mass_kg(order, getattr(company, "saby_unit_weight_g", None) or 20.0)
    params = {
        "КоличествоМест": str(int(places or 0)),
        "Масса": {"Брутто": str(mass_kg)},
    }
    if order.cargo_pallets:
        params["КоличествоПаллет"] = str(int(order.cargo_pallets))
    positions = [{"Наименование": cargo_name, "Параметры": params}]

    # Пункт выгрузки — адрес клиента + организация-грузополучатель (если известна)
    пункт_выгрузки = {
        "КодСтраны": "643",
        "АдресТекст": delivery_addr,
        "Операция": {"Тип": "Выгрузка", "ДатаВремя": подача_dt},
    }
    if cp:
        пункт_выгрузки["Организация"] = {
            "Название": cp.trade_name or cp.name or "",
            "ИНН": cp.inn or "",
        }

    sub = {
        "Документ": {
            "Номер": order.number or "",
            "Дата": _d(order.date),
        },
        # Грузоотправитель — мы
        "Грузоотправитель": {
            "Реквизиты": _ul(company.name, company.inn, company.kpp),
            "Адрес": {"АдресТекст": company.actual_address or company.legal_address or "", "КодСтраны": "643"},
            "Контакты": _contacts(company.phone, company.email),
        },
        # Маршрут: погрузка у нас → выгрузка у клиента
        "Маршрут": {
            "Отправление": {
                "КодСтраны": "643",
                "АдресТекст": pickup_addr,
                "ПодачаТС": {"ДатаВремя": подача_dt},
            },
            "КонечныйПункт": {"Название": delivery_addr},
            "Пункт": [пункт_выгрузки],
        },
        "Груз": {"Позиция": positions},
    }

    # Грузоперевозчик — если назначен
    if carrier:
        sub["Грузоперевозчик"] = {
            "Реквизиты": _ul(carrier.trade_name or carrier.name, carrier.inn, carrier.kpp),
            "Адрес": {"АдресТекст": carrier.legal_address or carrier.actual_address or "", "КодСтраны": "643"},
            "Контакты": _contacts(carrier.phone, carrier.email),
        }

    # Параметры ТС — из полей доставки заказа
    ts = {}
    if order.vehicle_type:
        ts["Тип"] = order.vehicle_type
    if ts:
        sub["ПараметрыТС"] = ts

    return sub


# ── ЭТрН: титул грузоотправителя (КНД 1110339) ───────────────────────────────

def _fio(full_name: str | None) -> dict:
    """«Фамилия Имя Отчество» → {Фамилия, Имя, Отчество}. Отчество опционально."""
    parts = (full_name or "").split()
    fio = {}
    if len(parts) >= 1:
        fio["Фамилия"] = parts[0]
    if len(parts) >= 2:
        fio["Имя"] = parts[1]
    if len(parts) >= 3:
        fio["Отчество"] = parts[2]
    return fio


def _adr(text: str | None) -> dict:
    """Адрес в свободной форме (АдрИнф) — без разбора на город/дом/индекс."""
    return {"АдрИнф": {"АдрТекст": text or "", "КодСтр": "643"}}


def _tlf(phone: str | None) -> dict:
    return {"Тлф": [{"value": str(phone)}]} if phone else {}


def _phone_from_contact(text: str | None) -> str:
    """Из строки контакта («79119244416 Ольга») вытаскивает телефон (первая
    последовательность цифр/+/скобок/дефисов)."""
    import re
    if not text:
        return ""
    m = re.search(r"[+\d][\d\-\s()]{5,}", str(text))
    return m.group(0).strip() if m else ""


def _find_vehicle(order):
    """Ищет запись водителя/ТС перевозчика по госномеру (или по ФИО) заказа —
    источник ИНН/телефона/удостоверения водителя для ЭТрН."""
    carrier = order.carrier
    if not carrier or not getattr(carrier, "vehicles", None):
        return None
    plate = (order.vehicle_plate or "").replace(" ", "").lower()
    name = (order.driver_name or "").strip().lower()
    for v in carrier.vehicles:
        if plate and (v.vehicle_plate or "").replace(" ", "").lower() == plate:
            return v
        if name and (v.driver_name or "").strip().lower() == name:
            return v
    return None


def _id_sv(party) -> dict:
    """Идентификационные сведения контрагента: ЮЛ (СвЮЛУч) или ИП (СвИП).
    Через getattr — работает и для Counterparty, и для CompanySettings."""
    inn  = getattr(party, "inn", "") or ""
    kpp  = getattr(party, "kpp", "") or ""
    ogrn = getattr(party, "ogrn", "") or ""
    name = getattr(party, "trade_name", None) or getattr(party, "name", "") or ""
    if getattr(party, "entity_type", "ooo") == "ip":
        sv = {"ИННФЛ": inn}
        if ogrn:
            sv["ОГРНИП"] = ogrn
        fio = _fio(getattr(party, "signatory", None) or getattr(party, "name", ""))
        if fio:
            sv["ФИО"] = fio
        return {"СвИП": sv}
    return {"СвЮЛУч": {"ИННЮЛ": inn, "КПП": kpp, "НаимОрг": name}}


def _rek_ident(party, address: str) -> dict:
    """Блок «РекИдент…» — идентификация + адрес + контакт участника перевозки."""
    block = {"ИдСв": _id_sv(party), "Адрес": _adr(address)}
    contact = _tlf(getattr(party, "phone", None))
    if contact:
        block["Контакт"] = contact
    return block


def build_etran_shipper_title(order, company) -> dict:
    """Заказ NERPA → подстановка титула грузоотправителя ЭТрН (КНД 1110339).

    Формат ФНС: заполняем то, что достоверно есть в заказе. Часть обязательных
    полей (ИНН/ВИН/грузоподъёмность ТС, ИНН водителя) в NERPA отсутствует —
    Saby укажет их в ошибке валидации, добираем итеративно.
    """
    carrier = order.carrier
    cp      = order.counterparty

    delivery_addr = order.delivery_address or (cp.actual_address if cp else "") \
        or (cp.legal_address if cp else "") or ""
    our_addr = company.actual_address or company.legal_address or ""

    cargo_name = (order.cargo_name or "").strip() \
        or (company.saby_cargo_name or "").strip() or "Груз"
    places = order.cargo_places if order.cargo_places else compute_cargo_places(order)
    mass_kg = compute_gross_mass_kg(order, getattr(company, "saby_unit_weight_g", None) or 20.0)

    сод_инф = {
        "ДатаЗак": _d(order.date),
        "ДатаТрН": _d(order.date),
        "НомЗак": order.number or "",
        "НомерТрН": order.number or "",
        # Грузоотправитель — мы
        "СвГО": {
            "ГОЭксп": "0",
            "РекИдентГО": _rek_ident(company, our_addr),
        },
        # Грузополучатель — клиент
        "СвГП": {
            "РекИдентГП": _rek_ident(cp, delivery_addr),
            "АдресДостГр": {"АдресИнф": {"АдрТекст": delivery_addr, "КодСтр": "643"}},
        },
        # Груз — консолидированный
        "СвГруз": {
            "ОпГруз": [{
                "НаимГруз": cargo_name,
                "КолМестГр": str(int(places or 0)),
                "СостГруз": "Новый",
                "СпУпак": "Отсутствует",
                "ПлМасГруз": {"МасБрутЗнач": str(mass_kg)},
            }],
        },
    }
    # РекИдентГО у нас (грузоотправитель) — юрлицо: НаимОрг/КПП обязательны,
    # контакт-телефон отправителя берём из реквизитов компании.
    сод_инф["СвГО"]["РекИдентГО"]["ИдСв"] = {"СвЮЛУч": {
        "ИННЮЛ": company.inn or "", "КПП": company.kpp or "", "НаимОрг": company.name or "",
    }}
    if company.phone:
        сод_инф["СвГО"]["РекИдентГО"]["Контакт"] = _tlf(company.phone)

    # Номер получателя — из контакта доставки заказа (иначе телефон клиента)
    receiver_phone = _phone_from_contact(getattr(order, "delivery_contact", None)) \
        or (cp.phone if cp else "")
    if receiver_phone:
        сод_инф["СвГП"]["РекИдентГП"]["Контакт"] = _tlf(receiver_phone)

    # Сведения о погрузке: место погрузки + подача ТС (дата отправления, время 11:00)
    подача = _dt(order.dispatch_date or order.delivery_date or order.date, "11:00:00")
    погруз = {
        "КолМестПрием": str(int(places or 0)),
        "МасБрутОтгр": str(mass_kg),
        "МетОпрМасс": "01",
        "ФАдресПогр": {"АдресИнф": {
            "АдрТекст": order.pickup_address or company.actual_address or company.legal_address or "",
            "КодСтр": "643",
        }},
        # Лицо, ответственное за погрузку, и владелец инфраструктуры — грузоотправитель (мы)
        "СвЛицПогрГр": {"СовпГОП": "1", "ИдентРекГО": {"ИННЮЛ": company.inn or ""}},
        "ВладИнфр": {"СовпГОВ": "1", "ИдентРекГО": {"ИННЮЛ": company.inn or ""}},
    }
    if подача:
        погруз["ЗаявПогр"] = подача
    сод_инф["СвПогруз"] = погруз

    # Перевозчик
    if carrier:
        сод_инф["СвПер"] = _rek_ident(carrier, carrier.legal_address or carrier.actual_address or "")

    # Водитель — ФИО из заказа + ИНН/телефон из карточки водителя перевозчика
    vehicle = _find_vehicle(order)
    if order.driver_name or vehicle:
        driver = {"ФИО": _fio(order.driver_name or (vehicle.driver_name if vehicle else ""))}
        d_inn = (vehicle.driver_inn if vehicle else None)
        d_tel = (vehicle.driver_phone if vehicle else None)
        if d_inn:
            driver["ИННФЛ"] = d_inn
        if d_tel:
            driver.update(_tlf(d_tel))
        сод_инф["СвВодит"] = driver

    # ТС
    if order.vehicle_plate or order.vehicle_type:
        ts = {}
        if order.vehicle_plate:
            ts["РегНомер"] = order.vehicle_plate
        if order.vehicle_type:
            ts["ПарТС"] = {"Марка": order.vehicle_type}
        сод_инф["СвТС"] = {"ТС": ts}

    return {
        "1110339": {
            "Файл": {
                "Документ": {
                    "НаимЭкСубСост": company.name or "",
                    "СодИнфГО": сод_инф,
                }
            }
        }
    }

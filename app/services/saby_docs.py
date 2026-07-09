"""
Сборка подстановок («ключ-значение») для транспортных документов Saby.

Отдаётся в СБИС.СгенерироватьВложение — Saby сам собирает XML по утверждённому
формату. Мы НЕ строим XML руками, только маппим данные заказа TMS в структуру
подстановки согласно документации Saby «Управление транспортом».

build_transport_order_substitution — ЭЗЗ (заказ-заявка перевозчику), КНД 1110361.

Роли в нашей схеме доставки:
  Грузоотправитель — наша компания (организатор перевозки, шлёт заявку);
  Грузоперевозчик  — order.carrier;
  Заказчик груза / грузополучатель — order.counterparty (клиент);
  Маршрут: пункт погрузки (наш склад) → пункт выгрузки (адрес клиента).
"""
from datetime import date, datetime


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
    """Заказ TMS → подстановка «ЗаказЗаявка» (КНД 1110361) для СгенерироватьВложение.

    Заполняем то, что достоверно есть в заказе; необязательные блоки (условия
    перевозки, договор, промежуточные пункты) опускаем — Saby подставит по формату.
    """
    carrier = order.carrier
    cp      = order.counterparty

    pickup_addr   = order.pickup_address or company.actual_address or company.legal_address or ""
    delivery_addr = order.delivery_address or (cp.actual_address if cp else "") or (cp.legal_address if cp else "") or ""
    подача_dt     = _dt(order.delivery_date or order.date)

    # Позиции груза
    positions = []
    for i in order.items:
        if not i.product:
            continue
        positions.append({
            "Наименование": i.product.name,
            "Параметры": {
                "КоличествоМест": str(int(i.quantity)),
                "Масса": {"Брутто": "0"},
            },
        })

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

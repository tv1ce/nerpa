"""Действия в Bitrix24 по итогам разговора: запись полей, комментарий, задача.

Менеджер заполняет поля прямо внутри шага скрипта («Бюджет клиента», «Когда
перезвонить»), и эти значения должны оказаться в карточке CRM, а не остаться
в NERPA. Здесь собраны сами действия — по одному на операцию, все поверх общего
клиента (вебхук или OAuth, клиенту всё равно).

Разделение сделано ради ТЗ: следующим шагом к записи полей добавляются
постановка задачи, смена стадии и запись результата звонка. Каждое действие —
отдельная функция с одинаковой формой ответа, поэтому новое добавляется рядом,
не трогая режим прохождения скрипта.

Ошибки не выбрасываются наружу: разговор менеджера не должен падать из-за того,
что портал недоступен. Функции возвращают (успех, сообщение), вызывающий код
решает, что с этим делать — обычно записать в script_runs и жить дальше.
"""
import logging

from app.services.bitrix_client import BitrixError

logger = logging.getLogger(__name__)

# Тип сущности CRM → метод обновления. Ключи те же, что приходят из плейсмента
# карточки (см. _B24_ENTITIES в app/routers/scripts.py).
UPDATE_METHODS = {
    "deal": "crm.deal.update",
    "lead": "crm.lead.update",
    "contact": "crm.contact.update",
    "company": "crm.company.update",
}

# Значение ENTITY_TYPE для таймлайна — там сущность называется строкой
TIMELINE_TYPES = {"deal": "deal", "lead": "lead", "contact": "contact", "company": "company"}


def update_entity_fields(client, entity: str, entity_id, fields: dict) -> tuple[bool, str]:
    """Пишет значения в поля карточки CRM.

    fields — уже готовая карта {код поля: значение}, где код взят из настройки
    шага скрипта (например UF_CRM_BUDGET). Проверять существование поля здесь
    незачем: портал сам ответит ошибкой, а её текст полезнее нашей догадки.
    """
    method = UPDATE_METHODS.get((entity or "").lower())
    if not method:
        return False, f"Неизвестный тип сущности CRM: {entity!r}"
    if not entity_id:
        return False, "Не передан идентификатор карточки CRM"
    if not fields:
        return True, "Нечего записывать"

    try:
        client.call(method, id=entity_id, fields=fields)
    except BitrixError as e:
        logger.warning("Bitrix24: не удалось записать поля в %s %s — %s", entity, entity_id, e)
        return False, str(e)
    logger.info("Bitrix24: в %s %s записаны поля %s", entity, entity_id, list(fields))
    return True, f"Записано полей: {len(fields)}"


def add_timeline_comment(client, entity: str, entity_id, text: str) -> tuple[bool, str]:
    """Оставляет комментарий в таймлайне карточки — итог разговора видно в CRM."""
    entity_type = TIMELINE_TYPES.get((entity or "").lower())
    if not entity_type or not entity_id or not (text or "").strip():
        return False, "Недостаточно данных для комментария"
    try:
        client.call("crm.timeline.comment.add", fields={
            "ENTITY_ID": entity_id,
            "ENTITY_TYPE": entity_type,
            "COMMENT": text,
        })
    except BitrixError as e:
        logger.warning("Bitrix24: комментарий в %s %s не добавлен — %s", entity, entity_id, e)
        return False, str(e)
    return True, "Комментарий добавлен"


def set_deal_stage(client, deal_id, stage_id: str) -> tuple[bool, str]:
    """Двигает сделку на стадию — для сценариев вида «договорились о встрече»."""
    if not deal_id or not stage_id:
        return False, "Не заданы сделка или стадия"
    try:
        client.call("crm.deal.update", id=deal_id, fields={"STAGE_ID": stage_id})
    except BitrixError as e:
        return False, str(e)
    return True, f"Сделка переведена на {stage_id}"


def create_task(client, title: str, responsible_id, description: str = "",
                deadline: str = "") -> tuple[bool, str]:
    """Ставит задачу по итогам разговора (например «перезвонить в четверг»)."""
    if not title or not responsible_id:
        return False, "Не заданы название задачи или ответственный"
    fields = {"TITLE": title[:250], "RESPONSIBLE_ID": responsible_id}
    if description:
        fields["DESCRIPTION"] = description
    if deadline:
        fields["DEADLINE"] = deadline
    try:
        client.call("tasks.task.add", fields=fields)
    except BitrixError as e:
        return False, str(e)
    return True, "Задача создана"


def fields_from_run_values(values: dict) -> dict:
    """Достаёт из значений прохождения только те, у которых задано поле CRM.

    Формат values приходит из режима прохождения:
        {"budget": {"value": 500000, "crm_field": "UF_CRM_BUDGET"}, ...}
    Поля без crm_field остаются только в NERPA — так и задумано: не каждый
    вопрос скрипта обязан иметь пару в карточке.
    """
    out = {}
    for item in (values or {}).values():
        if not isinstance(item, dict):
            continue
        code = (item.get("crm_field") or "").strip()
        if not code:
            continue
        value = item.get("value")
        if value in (None, "", False):
            continue
        out[code] = True if value is True else value
    return out

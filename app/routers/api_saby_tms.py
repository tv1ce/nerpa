"""
Saby «Управление транспортом» — заказы на перевозку (ЭЗЗ) и ЭТрН через tms.saby.ru.

Пока — только проверка связи: подтверждаем, что сессия СБИС (логин/пароль из
Настроек) валидна против выделенного транспортного эндпоинта tms.saby.ru/service/.
Read-only, ничего не создаёт — безопасно на боевом аккаунте.

GET /api/saby-tms/test — авторизация + СБИС.СписокИзменений (последний месяц), admin
"""
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth import role_required
from app.database import get_db
from app.models import CompanySettings, Order
from app.services.saby_tms_client import (
    SabyTmsError, get_saby_tms_client, our_org_from_company, state_label,
    DOC_TRANSPORT_ORDER, VLOZH_TYPE_ORDER, VLOZH_SUBTYPE_ORDER,
    DOC_CONSIGNMENT_NOTE, VLOZH_TYPE_ETRAN, ETRAN_TITLE_SHIPPER,
)
from app.services.saby_docs import (
    build_transport_order_substitution, build_etran_shipper_title,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/saby-tms", tags=["saby_tms"])


def _json(ok: bool, **kw):
    return JSONResponse({"ok": ok, **kw})


def _doc_link(result: dict, doc_id: str) -> str:
    """Ссылка на документ в кабинете Saby: приоритет — «СсылкаДляНашаОрганизация»."""
    return (
        result.get("СсылкаДляНашаОрганизация")
        or result.get("СсылкаВКабинет")
        or (f"https://online.sbis.ru/opendoc.html?guid={doc_id}" if doc_id else "")
    )


@router.get("/test")
@role_required("admin")
async def test_connection(request: Request, db: Session = Depends(get_db)):
    """Проверка связи с транспортным API Saby: авторизация + чтение списка заказов
    на перевозку за последний месяц. Ничего не создаёт и не отправляет."""
    company = db.query(CompanySettings).first()
    client = get_saby_tms_client(company)
    if not client:
        return _json(False, message="СБИС не настроен — укажите логин и пароль в Настройках → Интеграции")
    try:
        with client:
            client.authenticate()
            result = client.list_changes(DOC_TRANSPORT_ORDER, page_size=1)
        docs = (result or {}).get("Документ") or []
        return _json(
            True,
            message="Связь с Saby «Управление транспортом» есть, сессия принята транспортным эндпоинтом.",
            sample_count=len(docs),
        )
    except SabyTmsError as e:
        return _json(False, message=str(e))
    except Exception as e:
        logger.exception("Saby NERPA test error")
        return _json(False, message=f"Ошибка: {e}")


# ── Заказ-заявка перевозчику (ЭЗЗ) ──────────────────────────────────────────

@router.post("/transport-order/{order_id}")
@role_required("manager")
async def create_transport_order(request: Request, order_id: int, db: Session = Depends(get_db)):
    """Создаёт ЧЕРНОВИК заказа-заявки на перевозку в Saby из заказа NERPA.

    Флоу: СгенерироватьВложение → ЗаписатьДокумент. Документ остаётся черновиком
    в состоянии «редактируется» — подписание и отправку менеджер делает вручную
    в кабинете Saby (физический сертификат, подпись с сервера невозможна)."""
    company = db.query(CompanySettings).first()
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return _json(False, error="Заказ не найден")
    if not company or not company.inn:
        return _json(False, error="Не заполнены реквизиты организации (ИНН) в Настройках")

    client = get_saby_tms_client(company)
    if not client:
        return _json(False, error="СБИС не настроен — укажите логин и пароль в Настройках → Интеграции")

    substitution = build_transport_order_substitution(order, company)
    try:
        with client:
            attachment = client.generate_attachment(VLOZH_TYPE_ORDER, VLOZH_SUBTYPE_ORDER, substitution)
            result = client.write_document(
                DOC_TRANSPORT_ORDER, "Заказ на перевозку",
                our_org_from_company(company), attachment,
            )
        doc_id = result.get("Идентификатор")
        if not doc_id:
            return _json(False, error="Saby не вернул идентификатор документа", raw=result)

        order.transport_order_id = doc_id
        order.transport_order_status = state_label((result.get("Состояние") or {}).get("Код", "0"))
        order.transport_order_url = _doc_link(result, doc_id)
        db.commit()
        return _json(
            True,
            id=doc_id,
            url=order.transport_order_url,
            status=order.transport_order_status,
            message="Черновик заказа-заявки создан в Saby. Подпишите и отправьте перевозчику в кабинете Saby по ссылке.",
        )
    except SabyTmsError as e:
        logger.error("Saby ЭЗЗ ошибка для заказа #%s: %s", order.number, e)
        order.transport_order_status = "ошибка"
        db.commit()
        return _json(False, error=str(e))
    except Exception as e:
        logger.exception("Saby ЭЗЗ unexpected error order_id=%s", order_id)
        return _json(False, error=f"Непредвиденная ошибка: {e}")


@router.get("/transport-order/{order_id}/status")
@role_required("manager")
async def transport_order_status(request: Request, order_id: int, db: Session = Depends(get_db)):
    """Опрашивает статус заказа-заявки через СБИС.СписокИзменений (за последний месяц)."""
    company = db.query(CompanySettings).first()
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return _json(False, error="Заказ не найден")
    if not order.transport_order_id:
        return _json(False, error="Заказ-заявка ещё не создан")

    client = get_saby_tms_client(company)
    if not client:
        return _json(False, error="СБИС не настроен")

    try:
        with client:
            result = client.list_changes(DOC_TRANSPORT_ORDER, page_size=50)
    except SabyTmsError as e:
        return _json(False, error=str(e))

    match = next(
        (d for d in (result or {}).get("Документ", [])
         if d.get("Идентификатор") == order.transport_order_id),
        None,
    )
    if not match:
        return _json(True, status=order.transport_order_status, note="документ не найден в списке за период")
    code = (match.get("Состояние") or {}).get("Код", "")
    order.transport_order_status = state_label(code)
    db.commit()
    return _json(True, status=order.transport_order_status, code=code, url=order.transport_order_url)


# ── ЭТрН (электронная транспортная накладная) ───────────────────────────────

@router.post("/etran/{order_id}")
@role_required("manager")
async def create_etran(request: Request, order_id: int, db: Session = Depends(get_db)):
    """Создаёт ЧЕРНОВИК ЭТрН (титул грузоотправителя) в Saby из заказа NERPA.

    Флоу: СгенерироватьВложение (титул 1110339) → ЗаписатьДокумент. Последующие
    титулы (перевозчик/грузополучатель) и подписание — в кабинете Saby."""
    company = db.query(CompanySettings).first()
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return _json(False, error="Заказ не найден")
    if not company or not company.inn:
        return _json(False, error="Не заполнены реквизиты организации (ИНН) в Настройках")

    client = get_saby_tms_client(company)
    if not client:
        return _json(False, error="СБИС не настроен — укажите логин и пароль в Настройках → Интеграции")

    substitution = build_etran_shipper_title(order, company)
    try:
        with client:
            attachment = client.generate_attachment(VLOZH_TYPE_ETRAN, ETRAN_TITLE_SHIPPER, substitution)
            result = client.write_document(
                DOC_CONSIGNMENT_NOTE, "Транспортная накладная",
                our_org_from_company(company), attachment,
            )
        doc_id = result.get("Идентификатор")
        if not doc_id:
            return _json(False, error="Saby не вернул идентификатор документа", raw=result)

        order.etran_id = doc_id
        order.etran_status = state_label((result.get("Состояние") or {}).get("Код", "0"))
        order.etran_url = _doc_link(result, doc_id)
        db.commit()
        return _json(
            True,
            id=doc_id,
            url=order.etran_url,
            status=order.etran_status,
            message="Черновик ЭТрН создан в Saby. Титулы перевозчика/грузополучателя и подписание — в кабинете Saby.",
        )
    except SabyTmsError as e:
        logger.error("Saby ЭТрН ошибка для заказа #%s: %s", order.number, e)
        order.etran_status = "ошибка"
        db.commit()
        return _json(False, error=str(e))
    except Exception as e:
        logger.exception("Saby ЭТрН unexpected error order_id=%s", order_id)
        return _json(False, error=f"Непредвиденная ошибка: {e}")


@router.get("/etran/{order_id}/status")
@role_required("manager")
async def etran_status(request: Request, order_id: int, db: Session = Depends(get_db)):
    """Опрашивает статус ЭТрН через СБИС.СписокИзменений."""
    company = db.query(CompanySettings).first()
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return _json(False, error="Заказ не найден")
    if not order.etran_id:
        return _json(False, error="ЭТрН ещё не создан")

    client = get_saby_tms_client(company)
    if not client:
        return _json(False, error="СБИС не настроен")

    try:
        with client:
            result = client.list_changes(DOC_CONSIGNMENT_NOTE, page_size=50)
    except SabyTmsError as e:
        return _json(False, error=str(e))

    match = next(
        (d for d in (result or {}).get("Документ", [])
         if d.get("Идентификатор") == order.etran_id),
        None,
    )
    if not match:
        return _json(True, status=order.etran_status, note="документ не найден в списке за период")
    code = (match.get("Состояние") or {}).get("Код", "")
    order.etran_status = state_label(code)
    db.commit()
    return _json(True, status=order.etran_status, code=code, url=order.etran_url)

"""Хранилище файлов в карточках контрагентов и заказов.

Документы (договора, счета, УПД, ТН) приватные — лежат вне app/static, в каталоге
uploads/{counterparties|orders}/{id}/ и отдаются только через защищённый роут.
При загрузке PDF сжимается в фоне (best-effort), Word сохраняется как есть.
"""
import os
import uuid
from fastapi import APIRouter, Request, Depends, Form, File, UploadFile, BackgroundTasks
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db, SessionLocal
from app.auth import login_required, role_required
from app.models import AttachedFile, Counterparty, Order
from app.utils import log_action
from app.utils.file_compress import compress_file

router = APIRouter(prefix="/files", tags=["files"])
templates = Jinja2Templates(directory="app/templates")

UPLOAD_ROOT = "uploads"
ALLOWED_EXT = {".pdf", ".doc", ".docx"}
MAX_BYTES = 20 * 1024 * 1024  # 20 МБ до сжатия

# Типы файлов по сущности (значение — человекочитаемая подпись)
FILE_TYPES = {
    "counterparty": {"contract": "Договор", "extra": "Доп. соглашение", "other": "Прочее"},
    "order":        {"invoice": "Счёт", "upd": "УПД", "tn": "ТН", "other": "Прочее"},
}

# Имя каталога на диске по типу сущности
_DIR_NAMES = {"counterparty": "counterparties", "order": "orders"}


def _redirect_for(entity_type: str, entity_id: int) -> str:
    base = "/counterparties" if entity_type == "counterparty" else "/orders"
    return f"{base}/{entity_id}"


def files_for(db: Session, entity_type: str, entity_id: int) -> list[AttachedFile]:
    """Список прикреплённых файлов сущности (новые сверху). Для виджета в карточке."""
    return (
        db.query(AttachedFile)
        .filter(AttachedFile.entity_type == entity_type, AttachedFile.entity_id == entity_id)
        .order_by(AttachedFile.uploaded_at.desc())
        .all()
    )


def _compress_and_record(file_id: int, path: str, ext: str) -> None:
    """Фоновая задача: сжать файл и записать итоговый размер."""
    new_size = compress_file(path, ext)
    db = SessionLocal()
    try:
        af = db.query(AttachedFile).filter(AttachedFile.id == file_id).first()
        if af:
            af.size_compressed = new_size
            db.commit()
    finally:
        db.close()


@router.post("/upload")
@role_required("manager")
async def upload_files(
    request: Request,
    background: BackgroundTasks,
    entity_type: str = Form(...),
    entity_id: int = Form(...),
    file_type: str = Form("other"),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    if entity_type not in FILE_TYPES:
        return RedirectResponse(url="/", status_code=302)
    model = Counterparty if entity_type == "counterparty" else Order
    if not db.query(model.id).filter(model.id == entity_id).first():
        return RedirectResponse(url="/", status_code=302)
    if file_type not in FILE_TYPES[entity_type]:
        file_type = "other"

    dest_dir = os.path.join(UPLOAD_ROOT, _DIR_NAMES[entity_type], str(entity_id))
    os.makedirs(dest_dir, exist_ok=True)
    uid = request.session.get("user_id")

    saved = 0
    for f in files or []:
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_EXT:
            continue
        data = await f.read()
        if not data or len(data) > MAX_BYTES:
            continue
        stored_path = os.path.join(dest_dir, f"{uuid.uuid4().hex}{ext}")
        with open(stored_path, "wb") as out:
            out.write(data)
        # Санитизируем имя: убираем path-separators, управляющие символы и переносы строк
        import re as _re
        safe_name = _re.sub(r'[\x00-\x1f\x7f\\/:"*?<>|]', "_", f.filename)[:300]
        af = AttachedFile(
            entity_type=entity_type,
            entity_id=entity_id,
            file_type=file_type,
            original_name=safe_name,
            stored_path=stored_path.replace("\\", "/"),
            size_original=len(data),
            size_compressed=len(data),
            uploaded_by_id=uid,
        )
        db.add(af)
        db.flush()  # получить af.id для фоновой задачи
        background.add_task(_compress_and_record, af.id, stored_path, ext)
        saved += 1

    if saved:
        log_action(db, entity_type, entity_id, "updated", uid,
                   f"Прикреплено документов: {saved}")
        db.commit()
    return RedirectResponse(url=_redirect_for(entity_type, entity_id), status_code=302)


@router.get("/{file_id}/download")
@login_required
async def download_file(request: Request, file_id: int, db: Session = Depends(get_db)):
    af = db.query(AttachedFile).filter(AttachedFile.id == file_id).first()
    if not af or not os.path.exists(af.stored_path):
        return RedirectResponse(url="/", status_code=302)
    return FileResponse(af.stored_path, filename=af.original_name)


@router.post("/{file_id}/delete")
@role_required("manager")
async def delete_file(request: Request, file_id: int, db: Session = Depends(get_db)):
    af = db.query(AttachedFile).filter(AttachedFile.id == file_id).first()
    if not af:
        return RedirectResponse(url="/", status_code=302)
    entity_type, entity_id, name = af.entity_type, af.entity_id, af.original_name
    try:
        if os.path.exists(af.stored_path):
            os.remove(af.stored_path)
    except OSError:
        pass
    db.delete(af)
    log_action(db, entity_type, entity_id, "updated",
               request.session.get("user_id"), f"Удалён документ: {name}")
    db.commit()
    return RedirectResponse(url=_redirect_for(entity_type, entity_id), status_code=302)

"""Best-effort сжатие загруженных документов.

PDF сжимается через pikepdf (если установлен) — пересборка с object streams и
рекомпрессией. Если pikepdf недоступен (например, нет колёс под текущий Python)
или файл не сжимается — возвращаем исходный размер, файл остаётся как есть.

Word (.docx/.doc) не конвертируем: надёжная конвертация в PDF требует LibreOffice
на сервере, что слишком тяжело и хрупко. .docx и так zip — выигрыш минимален.
Поэтому Word сохраняется без сжатия.
"""
import logging
import os

logger = logging.getLogger(__name__)


def compress_pdf(path: str) -> int:
    """Пробует сжать PDF на месте. Возвращает итоговый размер файла в байтах.

    Никогда не бросает исключение наружу — при любой ошибке оставляет файл как есть.
    """
    try:
        import pikepdf  # noqa: PLC0415  — опциональная зависимость
    except ImportError:
        return _safe_size(path)

    tmp = path + ".tmp"
    try:
        original = _safe_size(path)
        with pikepdf.open(path) as pdf:
            pdf.save(
                tmp,
                compress_streams=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                recompress_flate=True,
            )
        new_size = _safe_size(tmp)
        # Берём сжатую версию только если она реально меньше
        if 0 < new_size < original:
            os.replace(tmp, path)
            return new_size
        os.remove(tmp)
        return original
    except Exception as e:  # noqa: BLE001 — сжатие не должно ронять загрузку
        logger.warning("compress_pdf(%s): %s", path, e)
        _cleanup(tmp)
        return _safe_size(path)


def compress_file(path: str, ext: str) -> int:
    """Диспетчер сжатия по расширению. Возвращает итоговый размер в байтах."""
    if ext.lower() == ".pdf":
        return compress_pdf(path)
    return _safe_size(path)


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _cleanup(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass

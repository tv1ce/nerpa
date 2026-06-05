"""
Точка входа TMS-сервера.

Логи пишутся одновременно в консоль (journald/stdout) и в ротируемые файлы:
  logs/tms.log        — основные логи приложения (10 МБ × 5 файлов)
  logs/tms_access.log — HTTP access log uvicorn   (10 МБ × 5 файлов)

На Linux через systemd stdout идёт в journald; файлы нужны для быстрого
просмотра без journalctl и для Windows-деплоя.

Просмотр (Linux):  journalctl -u tms -f
                   tail -f logs/tms.log
Просмотр (Win):    Get-Content logs\tms.log -Wait
"""
import logging
import logging.handlers
from pathlib import Path


def setup_logging() -> None:
    """Настраивает ротируемые логи приложения и HTTP access-лог."""
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    fmt_app = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fmt_access = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    def _rotating(filename: str, formatter: logging.Formatter) -> logging.Handler:
        h = logging.handlers.RotatingFileHandler(
            logs_dir / filename,
            maxBytes=10 * 1024 * 1024,  # 10 МБ
            backupCount=5,
            encoding="utf-8",
        )
        h.setFormatter(formatter)
        return h

    console = logging.StreamHandler()
    console.setFormatter(fmt_app)

    # Корневой логгер — INFO в файл + консоль
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(_rotating("tms.log", fmt_app))
    root.addHandler(console)

    # HTTP access-лог uvicorn — в отдельный файл, не засоряет основной
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.propagate = False          # не дублировать в root
    access_logger.addHandler(_rotating("tms_access.log", fmt_access))
    access_logger.addHandler(console)

    # Заглушаем слишком шумные библиотечные логгеры
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


if __name__ == "__main__":
    setup_logging()

    import uvicorn
    from app.database import init_db

    init_db()

    logging.getLogger(__name__).info("TMS server starting on 127.0.0.1:8080 (за nginx)")

    uvicorn.run(
        "app.main:app",
        # Слушаем только localhost — наружу приложение отдаёт ТОЛЬКО nginx (с TLS).
        # Так бэкенд не торчит в интернет в обход HTTPS, и боты не стучатся прямо в uvicorn.
        host="127.0.0.1",
        port=8080,
        reload=False,
        # log_config=None — говорим uvicorn не сбрасывать нашу конфигурацию logging.
        # uvicorn будет писать через стандартные логгеры "uvicorn" и "uvicorn.access",
        # которые мы уже настроили выше.
        log_config=None,
    )

"""Настройка логирования PrintFlow (идея 11).

Два контура, как и было, но содержимое стало пригодным для разбора:

* **консоль** — человекочитаемая строка (оператор смотрит в терминал);
* **файл** — JSON по строке: его можно грепать, скормить jq или загрузить
  в любой просмотрщик логов, не изобретая парсер под «свободный текст».

Каждой записи проставляется `request_id` — идентификатор запроса, который
проходит от HTTP-обработчика до SQL и фоновых задач. По нему видно всю
цепочку одного действия: раньше событие «списание пластика» и запись в
журнале цеха никак не связывались между собой.

Отдельный логгер `printflow.workshop` — события цеха (печать, склад,
деньги) в противовес техническому шуму. Фильтруется уровнем INFO.
"""
from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import time

from .config import LOG_FILE

# Идентификатор текущего запроса/задачи. contextvars, а не threading.local:
# работает и в потоках, и если когда-нибудь появится asyncio.
REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("pf_request_id", default="")
ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar("pf_actor", default="")


def set_request_context(request_id: str = "", actor: str = "") -> None:
    """Задать контекст запроса (вызывается из HTTP-обработчика)."""
    if request_id:
        REQUEST_ID.set(request_id)
    if actor:
        ACTOR.set(actor)


def clear_request_context() -> None:
    REQUEST_ID.set("")
    ACTOR.set("")


def request_id() -> str:
    return REQUEST_ID.get() or ""


class JsonFormatter(logging.Formatter):
    """Одна запись — одна JSON-строка."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        rid = REQUEST_ID.get()
        if rid:
            payload["request_id"] = rid
        actor = ACTOR.get()
        if actor:
            payload["actor"] = actor
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "pf_extra", None)
        if isinstance(extra, dict):
            payload["data"] = {k: v for k, v in extra.items() if not k.startswith("_")}
        return json.dumps(payload, ensure_ascii=False, default=str)


class PlainFormatter(logging.Formatter):
    """Консольный формат: коротко и с идентификатором запроса, если он есть."""

    def format(self, record: logging.LogRecord) -> str:
        rid = REQUEST_ID.get()
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        prefix = f"{stamp} {record.levelname[:4]} {record.name}"
        if rid:
            prefix += f" [{rid[:8]}]"
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        return f"{prefix}: {message}"


def setup_logging(verbose: bool = False, json_file: bool = True) -> None:
    """Собрать обработчики. Идемпотентно: повторный вызов не дублирует их."""
    root = logging.getLogger("printflow")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(PlainFormatter())
    root.addHandler(console)

    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter() if json_file else PlainFormatter())
        root.addHandler(file_handler)
    except Exception:
        # Лог в файл — удобство, а не условие работы: каталог может быть
        # недоступен (флешка только для чтения), система обязана стартовать.
        pass


def log() -> logging.Logger:
    """Технический логгер: подключения, ошибки, фоновые задачи."""
    return logging.getLogger("printflow")


def workshop_log() -> logging.Logger:
    """Логгер событий цеха: печать, склад, документы, деньги."""
    return logging.getLogger("printflow.workshop")


def event(logger: logging.Logger, message: str, **data) -> None:
    """Записать структурированное событие: log.event('scrap', grams=12)."""
    logger.info(message, extra={"pf_extra": data})

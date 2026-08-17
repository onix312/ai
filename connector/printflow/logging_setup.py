"""Настройка логирования PrintFlow: консоль + файл с ротацией.

Файл лога лежит в каталоге данных рядом с базой (connector.log),
крутится по 2 МБ, хранится 3 копии. Так любую аварию можно разобрать
задним числом, даже если консоль уже закрыта.
"""
from __future__ import annotations

import logging
import logging.handlers

from .config import LOG_FILE


def setup_logging(verbose: bool = False) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def log() -> logging.Logger:
    return logging.getLogger("printflow")

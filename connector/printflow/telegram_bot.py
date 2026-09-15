"""Совместимый мост: бот сотрудников переехал в пакет `staffbot` (18.1).

Весь код — в `connector/printflow/staffbot/`; здесь остаётся только
реэкспорт, чтобы `manager` и внешние вызовы не заметили переезда.
"""
from __future__ import annotations

from .staffbot import (  # noqa: F401
    HELP,
    REPLY_ALIASES,
    STATE_RU,
    StaffBot,
    TelegramBot,
    suggest_command,
)

"""Локальные настройки PySide6 UI."""
from __future__ import annotations

from PySide6.QtCore import QSettings


ORG = "NOZZA"
APP = "Assistant"


def settings() -> QSettings:
    return QSettings(ORG, APP)


def hotkey() -> str:
    return str(settings().value("hotkey", "Ctrl+Shift+Space"))


def set_hotkey(value: str) -> None:
    settings().setValue("hotkey", value)

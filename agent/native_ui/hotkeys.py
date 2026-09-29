"""Глобальный хоткей Windows через RegisterHotKey; без сторонних пакетов."""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
QUICK_HOTKEY_ID = 0x4E5A
STOP_HOTKEY_ID = 0x4E5B


class HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callback: Callable[[], None], hotkey_id: int = 0x4E5A) -> None:
        super().__init__()
        self.callback = callback
        self.hotkey_id = hotkey_id

    def nativeEventFilter(self, eventType: QByteArray, message: int):
        if sys.platform != "win32":
            return False, 0
        try:
            msg = ctypes.wintypes.MSG.from_address(int(message))
        except Exception:
            return False, 0
        if msg.message == WM_HOTKEY and int(msg.wParam) == self.hotkey_id:
            self.callback()
            return True, 0
        return False, 0


def register_default(window_handle: int, hotkey_id: int = QUICK_HOTKEY_ID) -> bool:
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    # Ctrl+Shift+Space
    return bool(user32.RegisterHotKey(window_handle, hotkey_id, MOD_CONTROL | MOD_SHIFT, 0x20))


def register_stop(window_handle: int, hotkey_id: int = STOP_HOTKEY_ID) -> bool:
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    # Ctrl+Alt+Shift+Space: intentionally separate from the quick panel.
    return bool(user32.RegisterHotKey(
        window_handle, hotkey_id, MOD_CONTROL | MOD_ALT | MOD_SHIFT, 0x20
    ))


def unregister(window_handle: int, hotkey_id: int = QUICK_HOTKEY_ID) -> None:
    if sys.platform == "win32":
        ctypes.windll.user32.UnregisterHotKey(window_handle, hotkey_id)

"""Буфер обмена (18.17): история текстов, чтение/запись через ctypes.

Без внешних зависимостей, только Windows. На других ОС — отказ с причиной.
История хранится в своей SQLite (clipboard_history), а не в памяти.
"""
from __future__ import annotations

import sys
import ctypes

IS_WINDOWS = sys.platform.startswith("win")


def get_text() -> tuple[str, str]:
    if not IS_WINDOWS:
        return "", "Буфер обмена читается только в Windows"
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        CF_UNICODETEXT = 13
        user32.OpenClipboard(0)
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            user32.CloseClipboard()
            return "", "Буфер пуст или не текст"
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            user32.CloseClipboard()
            return "", "Не удалось заблокировать буфер"
        text = ctypes.wstring_at(ptr)
        kernel32.GlobalUnlock(handle)
        user32.CloseClipboard()
        return text[:8000], ""
    except Exception as exc:
        try:
            ctypes.windll.user32.CloseClipboard()
        except Exception:
            pass
        return "", f"Буфер не прочитался: {exc}"


def set_text(text: str) -> tuple[bool, str]:
    if not IS_WINDOWS:
        return False, "Буфер пишется только в Windows"
    if not text:
        return False, "Пустой текст"
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        data = (str(text) + "\0").encode("utf-16-le")
        user32.OpenClipboard(0)
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            user32.CloseClipboard()
            return False, "Не удалось выделить память"
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(CF_UNICODETEXT, handle)
        user32.CloseClipboard()
        return True, ""
    except Exception as exc:
        try:
            ctypes.windll.user32.CloseClipboard()
        except Exception:
            pass
        return False, f"Буфер не записался: {exc}"

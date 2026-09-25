"""Экран и поиск по нему (18.17): область, поиск текста, клик по найденному.

Без внешних зависимостей кроме Pillow/mss (опционально). На не-Windows — отказ.
"""
from __future__ import annotations

import sys

IS_WINDOWS = sys.platform.startswith("win")


def region_shot(left: int, top: int, right: int, bottom: int, max_side: int = 800) -> tuple[bytes, str]:
    if not IS_WINDOWS:
        return b"", "Снимок области возможен только в Windows"
    try:
        from PIL import ImageGrab
    except ImportError:
        return b"", "Нет Pillow — снимок области недоступен"
    try:
        left = int(left); top = int(top); right = int(right); bottom = int(bottom)
        if right <= left or bottom <= top:
            return b"", "Неверные координаты области"
        # ограничим размер
        w = min(right - left, 2000)
        h = min(bottom - top, 2000)
        bbox = (left, top, left + w, top + h)
        img = ImageGrab.grab(bbox=bbox)
        img.thumbnail((max_side, max_side))
        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), ""
    except Exception as exc:
        return b"", f"Снимок области не получился: {exc}"


def find_text_on_screen(text: str) -> tuple[dict, str]:
    """Заглушка поиска текста: без OCR ищем только в заголовках окон."""
    if not IS_WINDOWS:
        return {}, "Поиск по экрану возможен только в Windows"
    if not text:
        return {}, "Пустой запрос"
    try:
        from .winapi import list_windows
        titles, reason = list_windows(limit=100)
        if reason:
            return {}, reason
        needle = str(text).casefold()
        for title in titles:
            if needle in title.casefold():
                return {"found": True, "where": "window_title", "title": title, "hint": "Найдено в заголовке окна — клик по окну возможен через window.focus"}, ""
        return {}, f"Текст «{text}» на экране не найден (OCR отсутствует — ищем только в заголовках окон)"
    except Exception as exc:
        return {}, f"Поиск не удался: {exc}"

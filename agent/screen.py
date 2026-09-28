"""Screen region capture and compatibility entry point for perception search."""
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
    """Compatibility entry point for the desktop perception search."""
    from .perception import find_text
    return find_text(text)

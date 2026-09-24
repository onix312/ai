"""Окна и ввод в чужих приложениях через Win32 (ctypes — stdlib).

Здесь только то, что реально работает без внешних зависимостей:
  * имя активного окна и список видимых окон (user32 EnumWindows);
  * клик по экранным координатам и ввод текста (SendInput).

Чего здесь нет намеренно:
  * распознавания текста на снимке — OCR требует внешнюю библиотеку, и пока её
    нет, агент не угадывает, куда кликать: он показывает снимок человеку и ждёт
    явного подтверждения координат (см. `agent/server.py`);
  * ввода без подтверждения — каждое действие проходит через окно «Подтвердить»,
    иначе чужая программа получала бы клавиатуру от сети.

На любой ОС кроме Windows функции честно возвращают отказ с причиной: агент
запускается и в Linux (для отладки речи), просто без управления окнами.
"""
from __future__ import annotations

import ctypes
import sys
import time

IS_WINDOWS = sys.platform.startswith("win")

# Коды клавиш для латиницы и цифр: русский текст вводится через буфер обмена
# в `type_text` (VK + Unicode-событие), поэтому таблица нужна только служебная.
VK = {"enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08, "space": 0x20,
      "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
      "f9": 0x78, "f10": 0x79}

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo",
                                            ctypes.POINTER(ctypes.c_ulong))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


def _user32():
    return ctypes.windll.user32  # type: ignore[attr-defined]


def active_window() -> tuple[str, str]:
    """Имя активного окна и причина, если узнать его не удалось."""
    if not IS_WINDOWS:
        return "", "Окна читаются только в Windows"
    try:
        user32 = _user32()
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "", "Активное окно не определено"
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        name = ctypes.create_unicode_buffer(512)
        user32.GetClassNameW(hwnd, name, 512)
        return title or name.value or "", ""
    except OSError as exc:
        return "", f"Win32 не ответил: {exc}"


def list_windows(limit: int = 40) -> tuple[list[str], str]:
    """Видимые окна с заголовками: агент показывает их человеку, а не выбирает сам."""
    if not IS_WINDOWS:
        return [], "Окна читаются только в Windows"
    titles: list[str] = []
    try:
        user32 = _user32()
        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def collect(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value.strip():
                titles.append(buffer.value.strip()[:200])
            return len(titles) < limit

        user32.EnumWindows(EnumProc(collect), 0)
        return titles, ""
    except OSError as exc:
        return [], f"Win32 не ответил: {exc}"


def window_rect(title_part: str) -> tuple[dict, str]:
    """Прямоугольник окна, в заголовке которого есть `title_part`."""
    if not IS_WINDOWS:
        return {}, "Окна читаются только в Windows"
    try:
        user32 = _user32()
        # Перебираем видимые окна: FindWindow требует точный заголовок,
        # а человек называет приложение своими словами.
        target = title_part.strip().lower()
        found = []

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def collect(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if target in buffer.value.strip().lower():
                rect = RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                found.append({"title": buffer.value.strip()[:200],
                              "left": rect.left, "top": rect.top,
                              "right": rect.right, "bottom": rect.bottom})
            return not found

        user32.EnumWindows(EnumProc(collect), 0)
        if not found:
            return {}, f"Окно «{title_part}» не найдено"
        return found[0], ""
    except OSError as exc:
        return {}, f"Win32 не ответил: {exc}"


def click(x: int, y: int) -> tuple[bool, str]:
    """Левый клик по экранным координатам. Только после подтверждения человека."""
    if not IS_WINDOWS:
        return False, "Клик возможен только в Windows"
    try:
        user32 = _user32()
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.05)
        events = (_INPUT * 2)()
        events[0].type = INPUT_MOUSE
        events[0].u.mi.dwFlags = MOUSEEVENTF_LEFTDOWN
        events[1].type = INPUT_MOUSE
        events[1].u.mi.dwFlags = MOUSEEVENTF_LEFTUP
        user32.SendInput(2, events, ctypes.sizeof(_INPUT))
        return True, ""
    except OSError as exc:
        return False, f"Win32 не ответил: {exc}"


def type_text(text: str) -> tuple[bool, str]:
    """Ввод строки в активное окно: Unicode-события, поэтому язык любой."""
    if not IS_WINDOWS:
        return False, "Ввод возможен только в Windows"
    if not text:
        return False, "Пустой текст"
    try:
        user32 = _user32()
        size = ctypes.sizeof(_INPUT)
        for char in str(text)[:2000]:
            events = (_INPUT * 2)()
            events[0].type = INPUT_KEYBOARD
            events[0].u.ki.wScan = ord(char)
            events[0].u.ki.dwFlags = KEYEVENTF_UNICODE
            events[1].type = INPUT_KEYBOARD
            events[1].u.ki.wScan = ord(char)
            events[1].u.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
            user32.SendInput(2, events, size)
            time.sleep(0.01)
        return True, ""
    except OSError as exc:
        return False, f"Win32 не ответил: {exc}"


def press_key(name: str) -> tuple[bool, str]:
    """Служебная клавиша: Enter, Tab, Esc, стрелки."""
    if not IS_WINDOWS:
        return False, "Ввод возможен только в Windows"
    code = VK.get(str(name or "").strip().lower())
    if not code:
        return False, f"Клавиша «{name}» агенту не известна"
    try:
        user32 = _user32()
        events = (_INPUT * 2)()
        events[0].type = INPUT_KEYBOARD
        events[0].u.ki.wVk = code
        events[1].type = INPUT_KEYBOARD
        events[1].u.ki.wVk = code
        events[1].u.ki.dwFlags = KEYEVENTF_KEYUP
        user32.SendInput(2, events, ctypes.sizeof(_INPUT))
        return True, ""
    except OSError as exc:
        return False, f"Win32 не ответил: {exc}"


def grab_screen(max_side: int = 1600) -> tuple[bytes, str]:
    """Снимок экрана в PNG. Требует Pillow — внешняя зависимость агента.

    Снимок никуда не уходит: он возвращается тому, кто спросил, по loopback, и
    не сохраняется на диске.
    """
    if not IS_WINDOWS:
        return b"", "Снимок экрана возможен только в Windows"
    try:
        import mss  # noqa: F401  — внешний, ставится установщиком агента
    except ImportError:
        mss = None
    try:
        from PIL import Image, ImageGrab
    except ImportError:
        return b"", "Нет Pillow/mss: установите зависимости агента (agent/README.md)"
    try:
        if mss:
            with mss.mss() as shot:
                monitor = shot.monitors[0]
                raw = shot.grab(monitor)
                image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        else:
            image = ImageGrab.grab()
        image.thumbnail((max_side, max_side))
        import io
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue(), ""
    except OSError as exc:
        return b"", f"Снимок не получился: {exc}"

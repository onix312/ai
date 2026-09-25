"""Руки помощника на компьютере (18.21): окна, клавиши, звук, питание, программы, речь.

Зачем отдельный модуль, если есть `winapi.py`. В 18.17 навыки ПК были
объявлены, но половина из них возвращала «заглушку»: громкость меняла только
волну старого микшера (не общий звук Windows), фокус искал окно по точному
заголовку, «элементы окна» отдавали список окон, питание отвечало «требует
Windows» даже в Windows, а озвучка возвращала текст вместо голоса. Здесь то же
на настоящих вызовах системы — и всё ещё на stdlib (ctypes), без pip-пакетов.

Правила модуля:
  * **честный отказ** — на Linux и macOS функции работают там, где у системы
    есть штатный способ (`/proc`, `espeak`, `xdg-open`), а иначе отвечают
    причиной; агент запускается везде;
  * **белый список запуска** — `open_target` открывает только известные
    программы, http(s)-адреса и папки внутри разрешённых корней; произвольный
    исполняемый файл с аргументами не запускается никогда;
  * **чистые помощники отдельно** — разбор сочетаний клавиш, выбор окна по
    словам человека, расчёт загрузки процессора сделаны функциями без
    побочных эффектов, чтобы их проверял тест без Windows.

Каждая функция возвращает `(значение, причина)`: пустая причина значит успех.
"""
from __future__ import annotations

import ctypes
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import uuid
import webbrowser
from typing import Any

from . import winapi

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# Окна самого помощника: их не сворачиваем и не считаем «окном человека»,
# когда команда пришла из окна помощника («сверни окно» — это про соседнее).
OWN_WINDOW_MARKERS = ("ассистент nozza", "помощник nozza", "nozza · помощник",
                      "printflow · помощник", "помощник printflow")

# ---------------------------------------------------------------------------
# Клавиши
# ---------------------------------------------------------------------------

_VK_BASE: dict[str, int] = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08, "space": 0x20,
    "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "printscreen": 0x2C, "capslock": 0x14, "apps": 0x5D,
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
    "media_next": 0xB0, "media_prev": 0xB1, "media_stop": 0xB2, "media_play_pause": 0xB3,
}
_VK_BASE.update({f"f{n}": 0x6F + n for n in range(1, 25)})
_VK_BASE.update({chr(code).lower(): code for code in range(ord("A"), ord("Z") + 1)})
_VK_BASE.update({str(n): 0x30 + n for n in range(10)})

# Как клавиши называют люди: по-русски, по-английски, сокращённо.
_KEY_ALIASES: dict[str, str] = {
    "control": "ctrl", "контрол": "ctrl", "контрл": "ctrl", "ктрл": "ctrl", "cntrl": "ctrl",
    "шифт": "shift", "альт": "alt", "option": "alt",
    "windows": "win", "вин": "win", "виндовс": "win", "пуск": "win", "super": "win", "cmd": "win",
    "return": "enter", "энтер": "enter", "ввод": "enter",
    "escape": "esc", "эскейп": "esc", "эскейт": "esc",
    "таб": "tab", "пробел": "space", "спейс": "space",
    "del": "delete", "делит": "delete", "удаление": "delete",
    "ins": "insert", "бэкспейс": "backspace", "бекспейс": "backspace", "backspace": "backspace",
    "pgup": "pageup", "pgdn": "pagedown", "pagedn": "pagedown",
    "вверх": "up", "вниз": "down", "влево": "left", "вправо": "right",
    "prtsc": "printscreen", "prtscr": "printscreen", "принтскрин": "printscreen",
    "хоум": "home", "энд": "end",
}
# Кириллица на тех же клавишах, что латиница (раскладка ЙЦУКЕН): «ctrl+с» —
# это Ctrl+C, человек просто не переключил язык.
_CYR_TO_LAT = dict(zip("йцукенгшщзфывапролдячсмить", "qwertyuiopasdfghjklzxcvbnm"))
# Клавиши, которым нужен флаг «расширенная»: без него стрелки и Home в части
# программ приходят как цифровой блок.
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5D,
             0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3}
_MODIFIERS = ("ctrl", "shift", "alt", "win")
# Сочетания, которые помощник не нажимает никогда: выход из системы, диспетчер
# безопасности, закрытие всего подряд. Для них есть отдельные навыки с
# подтверждением (`system.power`) или их делает человек руками.
_FORBIDDEN_COMBOS = ({"ctrl", "alt", "delete"}, {"win", "l"}, {"alt", "f4"})


def key_name(raw: str) -> str:
    """Имя клавиши в словаре помощника: «Контрл» → ctrl, «с» → c, «F5» → f5."""
    text = str(raw or "").strip().casefold().replace("ё", "е")
    text = text.strip(" .,'\"")
    if not text:
        return ""
    if text in _VK_BASE:
        return text
    if text in _KEY_ALIASES:
        return _KEY_ALIASES[text]
    if len(text) == 1 and text in _CYR_TO_LAT:
        return _CYR_TO_LAT[text]
    return ""


def parse_combo(text: str) -> tuple[list[str], str]:
    """«ctrl+shift+esc», «Win D», «контрл с» → список имён клавиш и причина отказа."""
    raw = str(text or "").strip()
    if not raw:
        return [], "Не сказано, какие клавиши нажать"
    lowered = raw.casefold()
    for spaced, joined in (("page up", "pageup"), ("page down", "pagedown"),
                           ("print screen", "printscreen"), ("caps lock", "capslock")):
        lowered = lowered.replace(spaced, joined)
    parts = [part for part in re.split(r"\s*(?:\+|\s|-(?=\w))\s*", lowered) if part]
    names: list[str] = []
    for part in parts:
        name = key_name(part)
        if not name:
            return [], f"Клавиша «{part}» помощнику не известна"
        if name not in names:
            names.append(name)
    if len(names) > 4:
        return [], "Больше четырёх клавиш сразу — это уже макрос, а не сочетание"
    if set(names) in [set(combo) for combo in _FORBIDDEN_COMBOS]:
        return [], ("Это сочетание помощник не нажимает: выход, блокировку и закрытие "
                    "программ делают отдельные навыки с подтверждением")
    # Модификаторы нажимаются первыми, отпускаются последними.
    names.sort(key=lambda name: (0 if name in _MODIFIERS else 1))
    return names, ""


def _send_keys(codes: list[int]) -> tuple[bool, str]:
    """Нажать коды по порядку и отпустить в обратном — как человек."""
    if not IS_WINDOWS:
        return False, "Клавиши нажимаются только в Windows"
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        size = ctypes.sizeof(winapi._INPUT)
        events = (winapi._INPUT * (len(codes) * 2))()
        for index, code in enumerate(codes):
            events[index].type = winapi.INPUT_KEYBOARD
            events[index].u.ki.wVk = code
            events[index].u.ki.dwFlags = 0x0001 if code in _EXTENDED else 0
        for offset, code in enumerate(reversed(codes)):
            slot = len(codes) + offset
            events[slot].type = winapi.INPUT_KEYBOARD
            events[slot].u.ki.wVk = code
            events[slot].u.ki.dwFlags = winapi.KEYEVENTF_KEYUP | (0x0001 if code in _EXTENDED else 0)
        sent = user32.SendInput(len(events), events, size)
        if sent != len(events):
            return False, "Windows не принял нажатие (окно администратора или экран блокировки)"
        return True, ""
    except OSError as exc:
        return False, f"Win32 не ответил: {exc}"


def press_combo(text: str) -> tuple[list[str], str]:
    """Нажать сочетание в активном окне. Возвращает нажатые клавиши и причину."""
    names, reason = parse_combo(text)
    if reason:
        return [], reason
    ok, reason = _send_keys([_VK_BASE[name] for name in names])
    return (names if ok else []), reason


MEDIA_ACTIONS: dict[str, tuple[str, str]] = {
    "play_pause": ("media_play_pause", "Пауза или продолжение воспроизведения"),
    "next": ("media_next", "Следующий трек"),
    "prev": ("media_prev", "Предыдущий трек"),
    "stop": ("media_stop", "Остановить воспроизведение"),
    "mute": ("volume_mute", "Звук выключен или включён"),
    "volume_up": ("volume_up", "Громче"),
    "volume_down": ("volume_down", "Тише"),
}


def media(action: str, times: int = 1) -> tuple[str, str]:
    """Медиаклавиша: пауза, трек, звук. Работает с любым плеером, который их слушает."""
    key = str(action or "").strip().casefold()
    if key not in MEDIA_ACTIONS:
        return "", "Действие не из списка: " + ", ".join(MEDIA_ACTIONS)
    name, title = MEDIA_ACTIONS[key]
    for _ in range(max(1, min(50, int(times or 1)))):
        ok, reason = _send_keys([_VK_BASE[name]])
        if not ok:
            return "", reason
    return title, ""


# ---------------------------------------------------------------------------
# Окна: поиск по словам человека
# ---------------------------------------------------------------------------

# Как люди называют программы. Ключ — слово во фразе, значение — чем программа
# представляется системе (имя процесса и слова заголовка).
WINDOW_WORDS: dict[str, tuple[str, ...]] = {
    "блокнот": ("notepad", "блокнот"),
    "телеграм": ("telegram",), "телега": ("telegram",), "тг": ("telegram",),
    "хром": ("chrome",), "гугл": ("chrome",), "браузер": ("chrome", "msedge", "firefox", "browser", "yandex", "opera"),
    "эдж": ("msedge", "edge"), "яндекс": ("browser", "yandex"), "фаерфокс": ("firefox",),
    "проводник": ("explorer", "проводник"), "папка": ("explorer",),
    "калькулятор": ("calculator", "calc", "калькулятор"),
    "бамбу": ("bambu",), "bambu": ("bambu",), "орка": ("orca",), "орку": ("orca",), "orca": ("orca",),
    "слайсер": ("bambu", "orca", "prusa", "cura", "slicer"),
    "ворд": ("winword", "word"), "эксель": ("excel",),
    "панель": ("printflow", "nozza"), "printflow": ("printflow",), "принтфлоу": ("printflow",),
    "ютуб": ("youtube",), "авито": ("avito", "авито"), "почта": ("outlook", "mail", "почта"),
    "диспетчер": ("taskmgr", "диспетчер"), "терминал": ("windowsterminal", "cmd", "powershell"),
    "ватсап": ("whatsapp",), "whatsapp": ("whatsapp",), "вайбер": ("viber",),
    "фотошоп": ("photoshop",), "блендер": ("blender",), "фьюжн": ("fusion",),
}


def _norm(text: str) -> str:
    return " ".join(str(text or "").casefold().replace("ё", "е").split())


def is_own_window(title: str) -> bool:
    """Окно самого помощника — его команды «сверни окно» не касаются."""
    text = _norm(title)
    return any(marker in text for marker in OWN_WINDOW_MARKERS)


def window_score(query: str, row: dict[str, Any]) -> int:
    """Насколько окно похоже на то, что назвал человек. 0 — не оно.

    Чистая функция: тест проверяет выбор без Windows. Порядок признаков —
    точное имя, начало заголовка, слово заголовка, имя процесса, синоним.
    """
    wanted = _norm(query)
    if not wanted:
        return 0
    title = _norm(row.get("title"))
    process = _norm(row.get("process"))
    score = 0
    if title == wanted:
        score = 100
    elif title.startswith(wanted):
        score = 80
    elif wanted in title:
        score = 60
    elif process and (wanted == process or process.startswith(wanted)):
        score = 55
    words = [word for word in re.split(r"[^\w]+", wanted) if len(word) >= 2]
    for word in words:
        stem = word[:-1] if len(word) > 4 else word
        for key, hints in WINDOW_WORDS.items():
            if key.startswith(stem) or stem.startswith(key):
                if any(hint in title or hint in process for hint in hints):
                    score = max(score, 50)
        if len(stem) >= 3 and (stem in title or stem in process):
            score = max(score, 40)
    if score and row.get("minimized"):
        score -= 1  # при равенстве видимое окно лучше свёрнутого
    return max(0, score)


def best_window(query: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Лучшее окно из списка по словам человека. Окна помощника не выбираются."""
    ranked = sorted(((window_score(query, row), index, row) for index, row in enumerate(rows)
                     if not is_own_window(str(row.get("title") or ""))),
                    key=lambda item: (-item[0], item[1]))
    if not ranked or ranked[0][0] <= 0:
        return None
    return ranked[0][2]


def _process_name(kernel32: Any, pid: int) -> str:
    if not pid:
        return ""
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(520)
        buffer = ctypes.create_unicode_buffer(520)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return pathlib.Path(buffer.value).stem
        return ""
    finally:
        kernel32.CloseHandle(handle)


def windows(limit: int = 60) -> tuple[list[dict[str, Any]], str]:
    """Видимые окна верхнего уровня в порядке Z (сверху вниз) с процессом."""
    if not IS_WINDOWS:
        return [], "Окна читаются только в Windows"
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        for name in ("IsWindowVisible", "GetWindowTextLengthW", "IsIconic", "IsZoomed"):
            getattr(user32, name).argtypes = (wintypes.HWND,)
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindow.restype = wintypes.HWND
        user32.GetWindow.argtypes = (wintypes.HWND, ctypes.c_uint)
        rows: list[dict[str, Any]] = []
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def collect(hwnd: Any, _lparam: Any) -> bool:
            if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # GW_OWNER
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title or title in ("Program Manager",):
                return True
            klass = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, klass, 256)
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            rows.append({"hwnd": int(hwnd), "title": title[:200], "class": klass.value[:80],
                         "pid": int(pid.value), "process": _process_name(kernel32, int(pid.value)),
                         "minimized": bool(user32.IsIconic(hwnd)),
                         "maximized": bool(user32.IsZoomed(hwnd))})
            return len(rows) < max(1, int(limit))

        user32.EnumWindows(proc(collect), 0)
        return rows, ""
    except (OSError, AttributeError) as exc:
        return [], f"Win32 не ответил: {exc}"


def find_window(query: str) -> tuple[dict[str, Any] | None, str]:
    """Окно по словам человека: «телеграм», «блокнот», «Bambu». Причина — если нет."""
    wanted = str(query or "").strip()
    rows, reason = windows(80)
    if reason:
        return None, reason
    if not wanted:
        return user_window(rows), ""
    row = best_window(wanted, rows)
    if row is None:
        titles = ", ".join(f"«{item['title'][:40]}»" for item in rows[:6]
                           if not is_own_window(item["title"]))
        return None, f"Окно «{wanted}» не нашлось" + (f". Открыты: {titles}" if titles else "")
    return row, ""


def user_window(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Окно, с которым работает человек: верхнее видимое, не помощник и не свёрнутое."""
    for row in rows:
        if not row.get("minimized") and not is_own_window(str(row.get("title") or "")):
            return row
    return None


def _user32_typed() -> Any:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.BringWindowToTop.argtypes = (wintypes.HWND,)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
    user32.PostMessageW.argtypes = (wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)
    user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_uint)
    user32.SystemParametersInfoW.argtypes = (ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint)
    user32.EnumChildWindows.argtypes = (wintypes.HWND, ctypes.c_void_p, wintypes.LPARAM)
    user32.SendMessageTimeoutW.argtypes = (wintypes.HWND, ctypes.c_uint, wintypes.WPARAM,
                                           ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                                           ctypes.POINTER(ctypes.c_size_t))
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    return user32


def focus(query: str) -> tuple[dict[str, Any] | None, str]:
    """Вывести окно на передний план: развернуть свёрнутое и отдать ему ввод."""
    row, reason = find_window(query)
    if row is None:
        return None, reason or "Окно не найдено"
    try:
        from ctypes import wintypes

        user32 = _user32_typed()
        kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
        hwnd = wintypes.HWND(row["hwnd"])
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # Windows отдаёт передний план только «своему» потоку: на мгновение
        # присоединяемся к потоку текущего окна, иначе окно лишь мигнёт в панели задач.
        foreground = user32.GetForegroundWindow()
        mine = kernel32.GetCurrentThreadId()
        theirs = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        attached = bool(theirs and theirs != mine and user32.AttachThreadInput(mine, theirs, True))
        try:
            user32.BringWindowToTop(hwnd)
            done = bool(user32.SetForegroundWindow(hwnd))
        finally:
            if attached:
                user32.AttachThreadInput(mine, theirs, False)
        if not done:
            # Запасной приём: нажатие Alt снимает запрет на смену окна.
            _send_keys([_VK_BASE["alt"]])
            done = bool(user32.SetForegroundWindow(hwnd))
        return row, "" if done else "Windows не дал переключить окно — щёлкните по нему один раз"
    except OSError as exc:
        return None, f"Win32 не ответил: {exc}"


ARRANGE_ACTIONS = {"minimize": (6, "свёрнуто"), "maximize": (3, "развёрнуто на весь экран"),
                   "restore": (9, "восстановлено")}


def arrange(query: str, action: str) -> tuple[dict[str, Any] | None, str]:
    """Свернуть, развернуть или восстановить окно. Пустой запрос — окно человека."""
    key = str(action or "").strip().casefold()
    if key not in ARRANGE_ACTIONS:
        return None, "Действие не из списка: " + ", ".join(ARRANGE_ACTIONS)
    row, reason = find_window(query)
    if row is None:
        return None, reason or "Нет окна, с которым можно это сделать"
    try:
        from ctypes import wintypes

        user32 = _user32_typed()
        user32.ShowWindow(wintypes.HWND(row["hwnd"]), ARRANGE_ACTIONS[key][0])
        return {**row, "done": ARRANGE_ACTIONS[key][1]}, ""
    except OSError as exc:
        return None, f"Win32 не ответил: {exc}"


def close_window(query: str) -> tuple[dict[str, Any] | None, str]:
    """Попросить окно закрыться (WM_CLOSE): программа сама спросит про несохранённое."""
    if not str(query or "").strip():
        return None, "Скажите, какое окно закрыть: закрывать «что-нибудь» помощник не будет"
    row, reason = find_window(query)
    if row is None:
        return None, reason or "Окно не найдено"
    try:
        from ctypes import wintypes

        user32 = _user32_typed()
        user32.PostMessageW(wintypes.HWND(row["hwnd"]), 0x0010, 0, 0)  # WM_CLOSE
        return row, ""
    except OSError as exc:
        return None, f"Win32 не ответил: {exc}"


def work_area() -> tuple[dict[str, int], str]:
    """Рабочая область главного монитора без панели задач."""
    if not IS_WINDOWS:
        return {}, "Только Windows"
    try:
        from ctypes import wintypes

        user32 = _user32_typed()
        rect = wintypes.RECT()
        if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            return {}, "Windows не отдал рабочую область"
        return {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom}, ""
    except OSError as exc:
        return {}, f"Win32 не ответил: {exc}"


def snap(left_query: str, right_query: str) -> tuple[dict[str, Any], str]:
    """Два окна рядом 50/50 в рабочей области (без панели задач)."""
    left, reason = find_window(left_query)
    if left is None:
        return {}, reason
    right, reason = find_window(right_query)
    if right is None:
        return {}, reason
    if left["hwnd"] == right["hwnd"]:
        return {}, "Оба названия указывают на одно окно — назовите два разных"
    area, reason = work_area()
    if reason:
        return {}, reason
    try:
        from ctypes import wintypes

        user32 = _user32_typed()
        width = (area["right"] - area["left"]) // 2
        height = area["bottom"] - area["top"]
        for row, x in ((left, area["left"]), (right, area["left"] + width)):
            hwnd = wintypes.HWND(row["hwnd"])
            user32.ShowWindow(hwnd, 9)
            user32.SetWindowPos(hwnd, None, x, area["top"], width, height, 0x0004 | 0x0040)
        return {"left": left, "right": right, "area": area}, ""
    except OSError as exc:
        return {}, f"Win32 не ответил: {exc}"


def controls(query: str = "", limit: int = 80) -> tuple[list[dict[str, Any]], str]:
    """Дочерние элементы окна: класс, текст и прямоугольник (кнопки, поля, надписи).

    Это не полный UI Automation, но у классических программ (Блокнот, диалоги,
    слайсеры на Qt/wx частично) кнопки и поля видны именно так — и это честнее
    прежнего «списка окон вместо элементов».
    """
    row, reason = find_window(query)
    if row is None:
        return [], reason or "Окно не найдено"
    try:
        from ctypes import wintypes

        user32 = _user32_typed()
        found: list[dict[str, Any]] = []
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def collect(hwnd: Any, _lparam: Any) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            klass = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, klass, 128)
            text = _control_text(user32, hwnd, 2000)
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            if text or klass.value.lower() in ("button", "edit", "combobox", "listbox"):
                found.append({"class": klass.value[:60], "text": text[:2000],
                              "rect": {"left": rect.left, "top": rect.top,
                                       "right": rect.right, "bottom": rect.bottom}})
            return len(found) < max(1, int(limit))

        callback = proc(collect)  # держим ссылку, пока Windows зовёт обратно
        user32.EnumChildWindows(wintypes.HWND(row["hwnd"]), ctypes.cast(callback, ctypes.c_void_p), 0)
        return found, ""
    except OSError as exc:
        return [], f"Win32 не ответил: {exc}"


def _control_text(user32: Any, hwnd: Any, limit: int) -> str:
    """Текст элемента через WM_GETTEXT с таймаутом: зависшая программа не держит помощника."""
    length = ctypes.c_size_t(0)
    if not user32.SendMessageTimeoutW(hwnd, 0x000E, 0, None, 0x0002, 200, ctypes.byref(length)):
        return ""
    size = min(int(length.value), limit) + 1
    if size <= 1:
        return ""
    buffer = ctypes.create_unicode_buffer(size)
    result = ctypes.c_size_t(0)
    if not user32.SendMessageTimeoutW(hwnd, 0x000D, size, ctypes.cast(buffer, ctypes.c_void_p),
                                      0x0002, 300, ctypes.byref(result)):
        return ""
    return " ".join(buffer.value.split())


def window_text(query: str = "") -> tuple[dict[str, Any], str]:
    """Заголовок окна и видимый текст его элементов (поля ввода, надписи)."""
    items, reason = controls(query, 120)
    row, _why = find_window(query)
    if reason and row is None:
        return {}, reason
    texts = [item["text"] for item in items if item.get("text")]
    return {"title": (row or {}).get("title", ""), "process": (row or {}).get("process", ""),
            "text": "\n".join(texts)[:8000], "controls": len(items)}, ""


# ---------------------------------------------------------------------------
# Звук: общий уровень Windows (Core Audio), а не волна старого микшера
# ---------------------------------------------------------------------------

_CLSID_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_ENDPOINT_VOLUME = "{5CDF2C82-841E-4546-9722-0CF74078229A}"


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def _vcall(pointer: ctypes.c_void_p, index: int, argtypes: tuple, *args: Any) -> int:
    """Вызов метода COM-интерфейса по номеру в таблице (HRESULT)."""
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)  # type: ignore[attr-defined]
    return int(prototype(table[index])(pointer, *args))


class _EndpointVolume:
    """IAudioEndpointVolume устройства вывода по умолчанию. Живёт один вызов."""

    def __init__(self) -> None:
        self.ole32 = ctypes.WinDLL("ole32")  # type: ignore[attr-defined]
        hr = self.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        # S_OK, S_FALSE и RPC_E_CHANGED_MODE — COM в потоке уже готов.
        self.uninit = hr in (0, 1)
        self.enumerator = ctypes.c_void_p()
        self.device = ctypes.c_void_p()
        self.volume = ctypes.c_void_p()
        clsid, iid = _guid(_CLSID_ENUMERATOR), _guid(_IID_ENUMERATOR)
        hr = self.ole32.CoCreateInstance(ctypes.byref(clsid), None, 0x17, ctypes.byref(iid),
                                         ctypes.byref(self.enumerator))
        if hr != 0 or not self.enumerator:
            raise OSError(f"MMDeviceEnumerator недоступен (0x{hr & 0xFFFFFFFF:08X})")
        hr = _vcall(self.enumerator, 4, (ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)),
                    0, 0, ctypes.byref(self.device))  # eRender, eConsole
        if hr != 0 or not self.device:
            raise OSError("Нет устройства вывода звука по умолчанию")
        iid_volume = _guid(_IID_ENDPOINT_VOLUME)
        hr = _vcall(self.device, 3, (ctypes.POINTER(_GUID), ctypes.c_ulong, ctypes.c_void_p,
                                     ctypes.POINTER(ctypes.c_void_p)),
                    ctypes.byref(iid_volume), 0x17, None, ctypes.byref(self.volume))
        if hr != 0 or not self.volume:
            raise OSError("Устройство не отдало регулятор громкости")

    def get(self) -> tuple[int, bool]:
        level = ctypes.c_float(0.0)
        muted = ctypes.c_int(0)
        _vcall(self.volume, 9, (ctypes.POINTER(ctypes.c_float),), ctypes.byref(level))
        _vcall(self.volume, 15, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(muted))
        return int(round(level.value * 100)), bool(muted.value)

    def set(self, percent: int) -> None:
        hr = _vcall(self.volume, 7, (ctypes.c_float, ctypes.c_void_p),
                    ctypes.c_float(max(0, min(100, int(percent))) / 100.0), None)
        if hr != 0:
            raise OSError(f"Громкость не принята (0x{hr & 0xFFFFFFFF:08X})")

    def mute(self, on: bool) -> None:
        _vcall(self.volume, 14, (ctypes.c_int, ctypes.c_void_p), 1 if on else 0, None)

    def close(self) -> None:
        for pointer in (self.volume, self.device, self.enumerator):
            if pointer:
                _vcall(pointer, 2, ())  # IUnknown::Release
        if self.uninit:
            self.ole32.CoUninitialize()


def volume_get() -> tuple[dict[str, Any], str]:
    """Общая громкость и признак «звук выключен»."""
    if not IS_WINDOWS:
        return {}, "Громкость читается только в Windows"
    try:
        endpoint = _EndpointVolume()
        try:
            level, muted = endpoint.get()
        finally:
            endpoint.close()
        return {"level": level, "muted": muted, "method": "core_audio"}, ""
    except OSError as exc:
        return {}, f"Громкость не прочитана: {exc}"


def volume_set(level: int) -> tuple[dict[str, Any], str]:
    """Поставить общую громкость 0–100. Запасной путь — медиаклавиши по 2%."""
    target = max(0, min(100, int(level)))
    if not IS_WINDOWS:
        return {}, "Громкость меняется только в Windows"
    try:
        endpoint = _EndpointVolume()
        try:
            endpoint.set(target)
            if target > 0:
                endpoint.mute(False)
            level_now, muted = endpoint.get()
        finally:
            endpoint.close()
        return {"level": level_now, "muted": muted, "method": "core_audio"}, ""
    except OSError as exc:
        # Без Core Audio (урезанная система, служба) — клавиши: 50 раз «тише»
        # до нуля, затем нужное число «громче». Грубо, но общий звук меняется.
        _title, reason = media("volume_down", 50)
        if reason:
            return {}, f"Громкость не изменена: {exc}"
        if target:
            _title, reason = media("volume_up", max(1, round(target / 2)))
        return {"level": target, "muted": False, "method": "keys",
                "approximate": True}, reason


def mute_set(on: bool | None = None) -> tuple[dict[str, Any], str]:
    """Выключить или включить звук. `None` — переключить."""
    if not IS_WINDOWS:
        return {}, "Звук переключается только в Windows"
    try:
        endpoint = _EndpointVolume()
        try:
            level, muted = endpoint.get()
            wanted = (not muted) if on is None else bool(on)
            endpoint.mute(wanted)
        finally:
            endpoint.close()
        return {"level": level, "muted": wanted, "method": "core_audio"}, ""
    except OSError:
        title, reason = media("mute")
        return ({"muted": None, "method": "keys", "done": title}, reason)


# ---------------------------------------------------------------------------
# Питание
# ---------------------------------------------------------------------------

POWER_ACTIONS = {
    "lock": "Заблокировать экран",
    "sleep": "Отправить компьютер в сон",
    "restart": "Перезагрузить через минуту",
    "shutdown": "Выключить через минуту",
    "cancel": "Отменить перезагрузку или выключение",
    "screen_off": "Погасить монитор",
}


def power_command(action: str, platform: str = sys.platform) -> list[str]:
    """Команда ОС для питания. Чистая функция — её проверяет тест на любой ОС."""
    key = str(action or "").strip().casefold()
    if platform.startswith("win"):
        return {"restart": ["shutdown", "/r", "/t", "60", "/c", "Перезагрузка по команде помощника NOZZA"],
                "shutdown": ["shutdown", "/s", "/t", "60", "/c", "Выключение по команде помощника NOZZA"],
                "cancel": ["shutdown", "/a"]}.get(key, [])
    if platform == "darwin":
        return {"sleep": ["pmset", "sleepnow"], "screen_off": ["pmset", "displaysleepnow"],
                "restart": ["osascript", "-e", 'tell app "System Events" to restart'],
                "shutdown": ["osascript", "-e", 'tell app "System Events" to shut down']}.get(key, [])
    return {"lock": ["loginctl", "lock-session"], "sleep": ["systemctl", "suspend"],
            "restart": ["shutdown", "-r", "+1"], "shutdown": ["shutdown", "-h", "+1"],
            "cancel": ["shutdown", "-c"], "screen_off": ["xset", "dpms", "force", "off"]}.get(key, [])


def power(action: str) -> tuple[str, str]:
    """Выполнить действие питания. Вызывается только после подтверждения человека."""
    key = str(action or "").strip().casefold()
    if key not in POWER_ACTIONS:
        return "", "Действие питания не из списка: " + ", ".join(POWER_ACTIONS)
    title = POWER_ACTIONS[key]
    if IS_WINDOWS and key in ("lock", "sleep", "screen_off"):
        try:
            if key == "lock":
                ok = bool(ctypes.windll.user32.LockWorkStation())  # type: ignore[attr-defined]
            elif key == "sleep":
                ok = bool(ctypes.windll.powrprof.SetSuspendState(False, False, False))  # type: ignore[attr-defined]
            else:
                # WM_SYSCOMMAND / SC_MONITORPOWER / 2 — выключить монитор всем окнам.
                ctypes.windll.user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)  # type: ignore[attr-defined]
                ok = True
            return (title, "") if ok else ("", "Windows отказал в действии питания")
        except OSError as exc:
            return "", f"Win32 не ответил: {exc}"
    command = power_command(key)
    if not command or not shutil.which(command[0]):
        return "", f"«{title}» на этой системе не поддерживается"
    try:
        done = subprocess.run(command, capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", f"Команда питания не выполнилась: {exc}"
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or b"").decode("utf-8", "replace").strip()[:200]
        return "", f"Команда питания вернула {done.returncode}" + (f": {tail}" if tail else "")
    return title, ""


# ---------------------------------------------------------------------------
# Здоровье компьютера
# ---------------------------------------------------------------------------

def cpu_from_samples(first: tuple[int, int], second: tuple[int, int]) -> float:
    """Загрузка процессора между двумя замерами (занято, всего). Чистая функция."""
    busy = second[0] - first[0]
    total = second[1] - first[1]
    if total <= 0:
        return 0.0
    return round(max(0.0, min(100.0, busy * 100.0 / total)), 1)


def parse_proc_stat(text: str) -> tuple[int, int]:
    """Первая строка /proc/stat → (занято, всего) в тиках."""
    for line in str(text or "").splitlines():
        if line.startswith("cpu "):
            values = [int(part) for part in line.split()[1:] if part.isdigit()]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values)
            return total - idle, total
    return 0, 0


def parse_meminfo(text: str) -> dict[str, float]:
    """/proc/meminfo → всего и занято в гигабайтах, загрузка в процентах."""
    values: dict[str, int] = {}
    for line in str(text or "").splitlines():
        name, _, rest = line.partition(":")
        digits = re.findall(r"\d+", rest)
        if digits:
            values[name.strip()] = int(digits[0])
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    if not total:
        return {}
    used = total - available
    return {"total_gb": round(total / 1048576, 1), "used_gb": round(used / 1048576, 1),
            "load": round(used * 100 / total)}


def _cpu_sample() -> tuple[int, int]:
    if IS_WINDOWS:
        from ctypes import wintypes

        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),  # type: ignore[attr-defined]
                                              ctypes.byref(user))

        def ticks(value: Any) -> int:
            return (value.dwHighDateTime << 32) | value.dwLowDateTime

        total = ticks(kernel) + ticks(user)  # kernel уже включает простой
        return total - ticks(idle), total
    try:
        return parse_proc_stat(pathlib.Path("/proc/stat").read_text(encoding="utf-8"))
    except OSError:
        return 0, 0


def health(sample_sec: float = 0.25) -> tuple[dict[str, Any], str]:
    """Процессор, память, диски, время работы и батарея — одним снимком."""
    out: dict[str, Any] = {"cpu_percent": None, "memory": {}, "disks": [], "uptime_hours": None,
                           "battery": None, "cores": os.cpu_count() or 0}
    first = _cpu_sample()
    time.sleep(max(0.05, min(1.0, float(sample_sec))))
    second = _cpu_sample()
    if second[1]:
        out["cpu_percent"] = cpu_from_samples(first, second)
    if IS_WINDOWS:
        try:
            class _MEMSTAT(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            stat = _MEMSTAT()
            stat.dwLength = ctypes.sizeof(_MEMSTAT)
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            if kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                total = stat.ullTotalPhys / 1073741824
                out["memory"] = {"total_gb": round(total, 1),
                                 "used_gb": round((stat.ullTotalPhys - stat.ullAvailPhys) / 1073741824, 1),
                                 "load": int(stat.dwMemoryLoad)}
            kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            out["uptime_hours"] = round(kernel32.GetTickCount64() / 3600000, 1)

            class _POWER(ctypes.Structure):
                _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                            ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                            ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

            power_state = _POWER()
            if kernel32.GetSystemPowerStatus(ctypes.byref(power_state)) and power_state.BatteryFlag != 128 \
                    and power_state.BatteryLifePercent <= 100:
                out["battery"] = {"percent": int(power_state.BatteryLifePercent),
                                  "plugged": power_state.ACLineStatus == 1}
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                root = f"{letter}:\\"
                if kernel32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
                    out["disks"].append(_disk(root))
        except OSError as exc:
            out["note"] = f"Часть данных Windows не прочиталась: {exc}"
    else:
        try:
            out["memory"] = parse_meminfo(pathlib.Path("/proc/meminfo").read_text(encoding="utf-8"))
        except OSError:
            pass
        try:
            seconds = float(pathlib.Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
            out["uptime_hours"] = round(seconds / 3600, 1)
        except (OSError, ValueError, IndexError):
            pass
        for battery in sorted(pathlib.Path("/sys/class/power_supply").glob("BAT*")):
            try:
                percent = int((battery / "capacity").read_text(encoding="utf-8").strip())
                status = (battery / "status").read_text(encoding="utf-8").strip().lower()
                out["battery"] = {"percent": percent, "plugged": status != "discharging"}
                break
            except (OSError, ValueError):
                continue
        out["disks"].append(_disk("/"))
    out["disks"] = [disk for disk in out["disks"] if disk]
    return out, ""


def _disk(root: str) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(root)
    except OSError:
        return {}
    total = usage.total / 1073741824
    return {"mount": root, "total_gb": round(total, 1), "free_gb": round(usage.free / 1073741824, 1),
            "used_percent": round((usage.total - usage.free) * 100 / usage.total) if usage.total else 0}


def health_warnings(state: dict[str, Any]) -> list[str]:
    """Что в снимке здоровья стоит сказать вслух. Чистая функция."""
    notes: list[str] = []
    cpu = state.get("cpu_percent")
    if isinstance(cpu, (int, float)) and cpu >= 85:
        notes.append(f"процессор загружен на {cpu:.0f}%")
    memory = state.get("memory") or {}
    if memory.get("load", 0) >= 85:
        notes.append(f"память занята на {memory['load']}%")
    for disk in state.get("disks") or []:
        if disk.get("free_gb", 99) < 5 or disk.get("used_percent", 0) >= 95:
            notes.append(f"на диске {disk.get('mount')} свободно {disk.get('free_gb')} ГБ")
    battery = state.get("battery") or {}
    if battery and not battery.get("plugged") and battery.get("percent", 100) <= 20:
        notes.append(f"батарея {battery['percent']}% без зарядки")
    return notes


# ---------------------------------------------------------------------------
# Программы, папки, сайты — только из белого списка
# ---------------------------------------------------------------------------

APPS: dict[str, dict[str, Any]] = {
    "notepad": {"title": "Блокнот", "words": ("блокнот", "notepad", "заметки"),
                "win": ["notepad.exe"], "linux": ["gnome-text-editor", "gedit", "xed", "mousepad", "kate"],
                "mac": ["open", "-a", "TextEdit"]},
    "calculator": {"title": "Калькулятор", "words": ("калькулятор", "calculator", "calc"),
                   "win": ["calc.exe"], "linux": ["gnome-calculator", "kcalc", "galculator"],
                   "mac": ["open", "-a", "Calculator"]},
    "explorer": {"title": "Проводник", "words": ("проводник", "explorer", "файлы", "мой компьютер"),
                 "win": ["explorer.exe"], "linux": ["xdg-open", "~"], "mac": ["open", "~"]},
    "taskmgr": {"title": "Диспетчер задач", "words": ("диспетчер задач", "диспетчер", "task manager", "taskmgr"),
                "win": ["taskmgr.exe"], "linux": ["gnome-system-monitor", "ksysguard"],
                "mac": ["open", "-a", "Activity Monitor"]},
    "paint": {"title": "Paint", "words": ("paint", "пейнт", "паинт", "рисовалк"),
              "win": ["mspaint.exe"], "linux": ["pinta", "kolourpaint"], "mac": []},
    "settings": {"title": "Параметры Windows", "words": ("параметры", "настройки windows", "настройки компьютера"),
                 "win": ["uri:ms-settings:"], "linux": ["gnome-control-center"], "mac": ["open", "-b", "com.apple.systempreferences"]},
    "control": {"title": "Панель управления", "words": ("панель управления", "control panel"),
                "win": ["control.exe"], "linux": [], "mac": []},
    "terminal": {"title": "Терминал", "words": ("терминал", "командная строка", "консоль", "powershell", "cmd"),
                 "win": ["wt.exe|cmd.exe"], "linux": ["x-terminal-emulator", "gnome-terminal", "konsole"],
                 "mac": ["open", "-a", "Terminal"]},
    "snipping": {"title": "Ножницы", "words": ("ножницы", "snipping", "вырезка экрана"),
                 "win": ["uri:ms-screenclip:"], "linux": ["gnome-screenshot", "spectacle"], "mac": []},
    "browser": {"title": "Браузер", "words": ("браузер", "browser", "интернет"),
                "url": "about:blank"},
    "panel": {"title": "Панель PrintFlow", "words": ("панель", "printflow", "принтфлоу", "nozza", "ноза"),
              "panel": "/"},
    "assistant_panel": {"title": "Помощник в панели", "words": ("помощник в панели", "страница помощника"),
                        "panel": "/assistant.html"},
    "bambu": {"title": "Bambu Studio", "words": ("bambu", "бамбу", "бамбу студио", "bambu studio"),
              "win_paths": ("Bambu Studio\\bambu-studio.exe",), "linux": ["bambu-studio"],
              "mac": ["open", "-a", "BambuStudio"]},
    "orca": {"title": "OrcaSlicer", "words": ("orca", "орка", "орку", "orcaslicer", "орка слайсер"),
             "win_paths": ("OrcaSlicer\\orca-slicer.exe",), "linux": ["orca-slicer"],
             "mac": ["open", "-a", "OrcaSlicer"]},
    "telegram": {"title": "Telegram", "words": ("телеграм", "telegram", "телегу", "телега"),
                 "win_paths": ("Telegram Desktop\\Telegram.exe",), "appdata": True,
                 "linux": ["telegram-desktop"], "mac": ["open", "-a", "Telegram"]},
    "word": {"title": "Word", "words": ("ворд", "word"), "win": ["uri:ms-word:"], "linux": ["libreoffice", "--writer"], "mac": []},
    "excel": {"title": "Excel", "words": ("эксель", "excel", "таблиц"), "win": ["uri:ms-excel:"],
              "linux": ["libreoffice", "--calc"], "mac": []},
}

SITES: dict[str, tuple[str, tuple[str, ...]]] = {
    "avito": ("https://www.avito.ru/", ("авито", "avito")),
    "youtube": ("https://www.youtube.com/", ("ютуб", "youtube", "ютюб")),
    "yandex": ("https://ya.ru/", ("яндекс", "yandex")),
    "google": ("https://www.google.com/", ("гугл", "google")),
    "makerworld": ("https://makerworld.com/", ("makerworld", "мейкерворлд")),
    "printables": ("https://www.printables.com/", ("printables", "принтаблс")),
    "thingiverse": ("https://www.thingiverse.com/", ("thingiverse", "тингиверс")),
    "telegram_web": ("https://web.telegram.org/", ("веб телеграм", "telegram web")),
    "mail": ("https://mail.yandex.ru/", ("почту", "почта")),
}

_URL_RE = re.compile(r"^(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/:?#]\S*)?$", re.IGNORECASE)


def resolve_app(target: str) -> tuple[str, dict[str, Any]] | None:
    """Программа из белого списка по словам человека. Чистая функция."""
    text = _norm(target)
    if not text:
        return None
    best: tuple[int, str, dict[str, Any]] | None = None
    for key, app in APPS.items():
        for word in app["words"]:
            if text == word or text.startswith(word) or (len(word) >= 4 and word in text):
                weight = len(word) + (100 if text == word else 0)
                if best is None or weight > best[0]:
                    best = (weight, key, app)
    return (best[1], best[2]) if best else None


def resolve_site(target: str) -> str:
    """Адрес сайта: известное имя («авито») или похожее на домен («avito.ru»)."""
    text = _norm(target)
    for url, words in SITES.values():
        if any(text == word or text == f"сайт {word}" for word in words):
            return url
    if _URL_RE.match(text) and " " not in text:
        return text if text.startswith(("http://", "https://")) else f"https://{text}"
    return ""


def _windows_app_path(app: dict[str, Any]) -> str:
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             os.environ.get("LOCALAPPDATA", "")]
    if app.get("appdata"):
        roots.insert(0, os.environ.get("APPDATA", ""))
    for root in roots:
        for rel in app.get("win_paths") or ():
            if root and os.path.isfile(os.path.join(root, rel)):
                return os.path.join(root, rel)
    return ""


def launch_plan(target: str, panel_url: str = "", roots: tuple[str, ...] = (),
                platform: str = sys.platform) -> tuple[dict[str, Any], str]:
    """Что именно будет открыто — без запуска. Чистая часть `open_target` для тестов."""
    text = str(target or "").strip()
    if not text:
        return {}, "Скажите, что открыть: программу, папку или сайт"
    app = resolve_app(text)
    if app:
        key, spec = app
        if spec.get("url"):
            return {"kind": "url", "title": spec["title"], "target": spec["url"], "app": key}, ""
        if spec.get("panel"):
            base = str(panel_url or "http://127.0.0.1:8765").rstrip("/")
            return {"kind": "url", "title": spec["title"], "target": base + spec["panel"], "app": key}, ""
        if platform.startswith("win"):
            if spec.get("win_paths"):
                return {"kind": "exe", "title": spec["title"], "app": key, "target": "",
                        "win_paths": list(spec["win_paths"])}, ""
            command = list(spec.get("win") or [])
        elif platform == "darwin":
            command = list(spec.get("mac") or [])
        else:
            command = list(spec.get("linux") or [])
        if not command:
            return {}, f"«{spec['title']}» на этой системе не открывается"
        return {"kind": "command", "title": spec["title"], "command": command, "app": key}, ""
    url = resolve_site(text)
    if url:
        return {"kind": "url", "title": url, "target": url}, ""
    path = pathlib.Path(os.path.expanduser(text))
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if path.is_absolute() or text.startswith("~"):
        allowed = [pathlib.Path(root).expanduser().resolve() for root in roots if root]
        inside = any(resolved == root or root in resolved.parents for root in allowed)
        if not inside:
            return {}, "Эта папка вне разрешённых помощнику (загрузки, документы, знания цеха)"
        if not resolved.exists():
            return {}, f"«{resolved}» не существует"
        if resolved.is_file() and resolved.suffix.lower() in (".exe", ".bat", ".cmd", ".ps1", ".vbs",
                                                              ".msi", ".com", ".scr", ".js", ".sh"):
            return {}, "Исполняемые файлы помощник не запускает — только документы и папки"
        return {"kind": "path", "title": resolved.name or str(resolved), "target": str(resolved)}, ""
    return {}, (f"«{text}» нет в списке программ помощника. Могу открыть: "
                + ", ".join(spec["title"] for spec in APPS.values()) + ", сайт по адресу или папку")


def open_url(url: str) -> tuple[bool, str]:
    """Открыть адрес (раздел панели цеха) в браузере по умолчанию.

    Только http(s). Без рабочего стола (служба, контейнер, SSH) браузер не
    запускаем вовсе: `webbrowser` там ищет текстовые браузеры и может занять
    терминал — вместо этого окно покажет ссылку.
    """
    if not re.match(r"^https?://[^\s]+$", str(url or "")):
        return False, "Это не адрес страницы"
    try:
        if IS_WINDOWS:
            os.startfile(url)  # type: ignore[attr-defined]
            return True, ""
        if not IS_MAC and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False, "Нет рабочего стола — открыть браузер негде"
        return (True, "") if webbrowser.open(url) else (False, "Браузер не открылся")
    except Exception as exc:  # noqa: BLE001 — причина уходит человеку
        return False, f"Браузер не открылся: {exc.__class__.__name__}"


def open_target(target: str, panel_url: str = "", roots: tuple[str, ...] = ()) -> tuple[dict[str, Any], str]:
    """Открыть программу, сайт или папку. Только белый список (см. `launch_plan`)."""
    plan, reason = launch_plan(target, panel_url, roots)
    if reason:
        return {}, reason
    try:
        if plan["kind"] == "url":
            if IS_WINDOWS:
                os.startfile(plan["target"])  # type: ignore[attr-defined]
            elif not webbrowser.open(plan["target"]):
                return {}, "Браузер не открылся"
        elif plan["kind"] == "path":
            if IS_WINDOWS:
                os.startfile(plan["target"])  # type: ignore[attr-defined]
            else:
                opener = "open" if IS_MAC else "xdg-open"
                if not shutil.which(opener):
                    return {}, f"Нет {opener} — папку открыть нечем"
                _spawn([opener, plan["target"]])
        elif plan["kind"] == "exe":
            path = _windows_app_path(APPS[plan["app"]])
            if not path:
                return {}, f"{plan['title']} не установлен (или стоит не в обычной папке)"
            _spawn([path])
            plan["target"] = path
        else:
            command = [os.path.expanduser(part) for part in plan["command"]]
            head = command[0]
            if head.startswith("uri:"):
                os.startfile(head[4:])  # type: ignore[attr-defined]
            else:
                choices = head.split("|")
                found = next((choice for choice in choices if shutil.which(choice)), "")
                if not found and len(command) == 1 and IS_WINDOWS:
                    found = choices[-1]
                if not found:
                    # Первая доступная программа из списка для этой системы.
                    alt = next((part for part in command if shutil.which(part)), "")
                    if not alt:
                        return {}, f"Для «{plan['title']}» на этой системе нет программы"
                    command = [alt]
                else:
                    command[0] = found
                _spawn(command)
    except OSError as exc:
        return {}, f"Не открылось: {exc}"
    return plan, ""


def _spawn(command: list[str]) -> None:
    flags = 0
    if IS_WINDOWS:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=flags,
                     start_new_session=not IS_WINDOWS, close_fds=True)


# ---------------------------------------------------------------------------
# Голос: озвучка средствами системы
# ---------------------------------------------------------------------------

def speech_engine() -> str:
    """Чем озвучивать на этой системе: SAPI в Windows, say в macOS, espeak в Linux."""
    if IS_WINDOWS:
        return "sapi" if shutil.which("powershell") or shutil.which("powershell.exe") else ""
    if IS_MAC and shutil.which("say"):
        return "say"
    for name in ("espeak-ng", "espeak", "spd-say"):
        if shutil.which(name):
            return name
    return ""


_SAPI_SCRIPT = (
    "Add-Type -AssemblyName System.Speech;"
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "try { $v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'ru*' } |"
    " Select-Object -First 1; if ($v) { $s.SelectVoice($v.VoiceInfo.Name) } } catch {};"
    "$s.Rate = [int]$env:PF_TTS_RATE; $s.Volume = [int]$env:PF_TTS_VOLUME;"
    "$s.Speak($env:PF_TTS_TEXT)"
)


def speak(text: str, rate: int = 0, volume: int = 100) -> tuple[dict[str, Any], str]:
    """Сказать вслух. Текст — данные (stdin или переменная окружения), не команда.

    В Windows текст передаётся переменной окружения: PowerShell читает stdin в
    кодировке консоли (cp866), и кириллица превращалась бы в «кракозябры», а
    окружение Windows — Unicode. В строку команды текст не попадает нигде.
    """
    clean = " ".join(str(text or "").split())[:1500]
    if not clean:
        return {}, "Пустой текст для озвучки"
    engine = speech_engine()
    if not engine:
        return {}, ("Нет движка озвучки: в Windows нужен PowerShell (есть по умолчанию), "
                    "в Linux — espeak-ng")
    rate = max(-10, min(10, int(rate)))
    volume = max(0, min(100, int(volume)))
    env = os.environ.copy()
    if engine == "sapi":
        command = ["powershell", "-NoProfile", "-NonInteractive", "-Command", _SAPI_SCRIPT]
        env.update(PF_TTS_RATE=str(rate), PF_TTS_VOLUME=str(volume), PF_TTS_TEXT=clean)
    elif engine == "say":
        command = ["say"]
    elif engine == "spd-say":
        command = ["spd-say", "-l", "ru", "-e"]
    else:
        command = [engine, "-v", "ru", "-s", str(170 + rate * 12), "-a", str(volume * 2), "--stdin"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, env=env, creationflags=flags)
        if process.stdin is not None:
            if engine != "sapi":
                process.stdin.write(clean.encode("utf-8"))
            process.stdin.close()
    except (OSError, ValueError) as exc:
        return {}, f"Озвучка не запустилась: {exc}"
    return {"engine": engine, "chars": len(clean), "pid": process.pid}, ""

"""Системные действия ассистента (18.17): автозагрузка, процессы, звук, громкость.

Всё через stdlib (ctypes, winreg, subprocess) — без внешних зависимостей.
На не-Windows функции возвращают отказ с причиной, агент остаётся живым.
"""
from __future__ import annotations

import os
import sys
import subprocess
import pathlib
import ctypes

IS_WINDOWS = sys.platform.startswith("win")


def autostart_status(app_name: str = "PrintFlowAssistant") -> tuple[bool, str]:
    if not IS_WINDOWS:
        return False, "Автозагрузка настраивается только в Windows"
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, app_name)
            winreg.CloseKey(key)
            return True, ""
        except FileNotFoundError:
            winreg.CloseKey(key)
            return False, ""
    except OSError as exc:
        return False, f"Реестр не ответил: {exc}"


def autostart_enable(app_name: str = "PrintFlowAssistant", exe_path: str = "") -> tuple[bool, str]:
    if not IS_WINDOWS:
        return False, "Автозагрузка настраивается только в Windows"
    try:
        import winreg
        if not exe_path:
            exe_path = str(pathlib.Path(sys.executable).resolve())
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_WRITE)
        winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, f'"{exe_path}" -m agent')
        winreg.CloseKey(key)
        return True, ""
    except OSError as exc:
        return False, f"Не удалось включить автозагрузку: {exc}"


def autostart_disable(app_name: str = "PrintFlowAssistant") -> tuple[bool, str]:
    if not IS_WINDOWS:
        return False, "Автозагрузка настраивается только в Windows"
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_WRITE)
        try:
            winreg.DeleteValue(key, app_name)
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
        return True, ""
    except OSError as exc:
        return False, f"Не удалось выключить автозагрузку: {exc}"


def process_list(limit: int = 20) -> tuple[list[dict], str]:
    """Топ процессов по CPU/RAM: Windows tasklist, Linux ps, без внешних зависимостей."""
    limit = max(1, min(100, int(limit or 20)))
    procs: list[dict] = []
    try:
        if IS_WINDOWS:
            # tasklist csv
            out = subprocess.check_output(["tasklist", "/FO", "CSV", "/NH"], text=True, encoding="utf-8", errors="ignore", timeout=5)
            for line in out.splitlines()[:limit*2]:
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 2:
                    procs.append({"name": parts[0][:120], "pid": parts[1][:20], "mem": parts[4] if len(parts) > 4 else ""})
                if len(procs) >= limit:
                    break
        else:
            out = subprocess.check_output(["ps", "-eo", "pid,comm,%cpu,%mem", "--sort=-%cpu"], text=True, encoding="utf-8", errors="ignore", timeout=5)
            for line in out.splitlines()[1:limit+1]:
                parts = line.strip().split(None, 3)
                if len(parts) >= 2:
                    procs.append({"pid": parts[0], "name": parts[1][:120], "cpu": parts[2] if len(parts) > 2 else "", "mem": parts[3] if len(parts) > 3 else ""})
        return procs[:limit], ""
    except Exception as exc:
        return [], f"Не удалось получить список процессов: {exc}"


def audio_devices() -> tuple[list[dict], str]:
    """Список устройств вывода: Windows — через реестр, иначе заглушка."""
    if not IS_WINDOWS:
        return [], "Переключение звука — только Windows"
    try:
        import winreg
        # Перечисляем звуковые устройства из реестра — без COM
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
        devices = []
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
        except OSError:
            # попробуем HKCU
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base)
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(key, i)
                i += 1
                subkey = winreg.OpenKey(key, sub + r"\Properties")
                try:
                    # {a45c254e-df1c-4efd-8020-67d146a850e0},2 — FriendlyName
                    name, _ = winreg.QueryValueEx(subkey, "{a45c254e-df1c-4efd-8020-67d146a850e0},2")
                    devices.append({"id": sub[:60], "name": str(name)[:200]})
                except OSError:
                    devices.append({"id": sub[:60], "name": sub[:60]})
                winreg.CloseKey(subkey)
                if len(devices) >= 20:
                    break
            except OSError:
                break
        winreg.CloseKey(key)
        return devices, ""
    except Exception as exc:
        return [], f"Не удалось получить устройства: {exc}"


def set_volume(level: int) -> tuple[bool, str]:
    """Громкость 0-100 через ctypes (Windows Core Audio упрощённо — через nircmd fallback)."""
    if not IS_WINDOWS:
        return False, "Громкость меняется только в Windows"
    level = max(0, min(100, int(level)))
    try:
        # Простейший способ — SendMessage WM_APPCOMMAND
        # Для точности используем winmm waveOutSetVolume (устарело, но работает без COM)
        winmm = ctypes.windll.winmm
        # 0xFFFF = max, делаем одинаковый для левого и правого
        vol = int(level / 100 * 0xFFFF)
        packed = vol | (vol << 16)
        winmm.waveOutSetVolume(0, packed)
        return True, ""
    except Exception as exc:
        return False, f"Не удалось изменить громкость: {exc}"


def get_volume() -> tuple[int, str]:
    if not IS_WINDOWS:
        return 0, "Громкость читается только в Windows"
    try:
        winmm = ctypes.windll.winmm
        vol = ctypes.c_ulong()
        winmm.waveOutGetVolume(0, ctypes.byref(vol))
        low = vol.value & 0xFFFF
        level = int(low / 0xFFFF * 100)
        return level, ""
    except Exception as exc:
        return 0, f"Не удалось прочитать громкость: {exc}"

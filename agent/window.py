"""Запуск пользовательского окна NOZZA.

Основной интерфейс теперь нативный PySide6 (`python -m agent.native_ui`).
Старая HTML-страница `/ui` и pywebview остаются fallback: они полезны для
диагностики и для машин, где PySide6 не установлен.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import threading
from typing import Any


def url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}/ui"


def _native_available() -> bool:
    return importlib.util.find_spec("PySide6") is not None


def _open_native() -> dict[str, Any]:
    try:
        subprocess.Popen(
            [sys.executable, "-m", "agent.native_ui"],
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except (OSError, RuntimeError) as exc:
        return {"ok": False, "opened": False,
                "reason": f"Нативный интерфейс не запустился: {exc.__class__.__name__}"}
    return {"ok": True, "opened": True, "reason": "",
            "hint": "Нативный интерфейс NOZZA запущен"}


def _open_webview(address: str, title: str, width: int, height: int) -> dict[str, Any]:
    try:
        import webview
    except ImportError:
        return {"ok": False, "opened": False, "address": address,
                "reason": "Нет PySide6 и pywebview",
                "hint": f"Fallback доступен в браузере: {address}"}

    def start() -> None:
        try:
            webview.create_window(title, address, width=int(width), height=int(height),
                                  resizable=True, min_size=(420, 560))
            webview.start()
        except Exception as exc:
            print(f"Fallback-окно не открылось ({exc.__class__.__name__}): {address}",
                  flush=True)

    threading.Thread(target=start, daemon=True, name="assistant-window-fallback").start()
    return {"ok": True, "opened": True, "address": address, "reason": "",
            "hint": f"Открыт fallback-интерфейс: {address}"}


def open_window(address: str, title: str = "NOZZA Assistant",
                width: int = 1080, height: int = 820) -> dict[str, Any]:
    """Открыть нативный UI; при отсутствии PySide6 — старый /ui."""
    address = str(address or "")
    if not address.startswith("http://127.0.0.1"):
        return {"ok": False, "opened": False, "address": address,
                "reason": "Окно открывается только для 127.0.0.1", "hint": address}
    if _native_available():
        result = _open_native()
        return {**result, "address": address}
    return _open_webview(address, title, width, height)

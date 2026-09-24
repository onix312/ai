"""Окно ассистента поверх страницы (18.14, идея И139).

Слои разделены сознательно. Содержимое окна отдаёт сам агент (`ui.py`, маршрут
`/ui` на порту 8799, только loopback), а здесь — необязательная обёртка:
`pywebview`, если он установлен в окружение агента. Без него ассистент не
ломается: адрес страницы печатается в консоль и её открывает любой браузер на
этом же компьютере.

Почему не tkinter и не собственное окно на Win32: окно должно показывать реестр
навыков, журнал и подтверждение — то есть разметку, которую страница уже умеет,
а дублировать её вторым стеком значит получить два разных интерфейса.

Трей (`pystray`) в 18.14 не делается: `capabilities.detect()` честно говорит
«нет pystray», и это видно в окне и в `/capabilities`. Иконка в трее появится
вместе с постоянным режимом агента (идея И159), когда у него будет что
показывать в меню.
"""
from __future__ import annotations

from typing import Any


def url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}/ui"


def open_window(address: str, title: str = "Помощник NOZZA",
                width: int = 1080, height: int = 820) -> dict[str, Any]:
    """Открыть страницу ассистента окном. Отказ всегда с причиной и советом."""
    address = str(address or "")
    if not address.startswith("http://127.0.0.1"):
        return {"ok": False, "opened": False, "address": address,
                "reason": "Окно открывается только для адреса этого компьютера (127.0.0.1)",
                "hint": address}
    try:
        import webview  # pywebview: необязательная зависимость окружения агента
    except ImportError:
        return {"ok": False, "opened": False, "address": address,
                "reason": "Нет pywebview — отдельного окна не будет",
                "hint": f"Откройте страницу ассистента в браузере: {address}"}
    try:
        import threading

        def start() -> None:
            try:
                window = webview.create_window(title, address,
                                               width=int(width), height=int(height),
                                               resizable=True)
                webview.start(window)
            except Exception as exc:  # нет дисплея: служба Windows, SSH, контейнер
                # Окно — удобный слой, а не обязательный: страница по тому же
                # адресу остаётся доступна, поэтому здесь достаточно сказать.
                print(f"Окно ассистента не открылось ({exc.__class__.__name__}): "
                      f"{address}", flush=True)

        threading.Thread(target=start, daemon=True, name="assistant-window").start()
    except (OSError, RuntimeError) as exc:
        return {"ok": False, "opened": False, "address": address,
                "reason": f"Окно не запустилось: {exc.__class__.__name__}",
                "hint": address}
    return {"ok": True, "opened": True, "address": address, "reason": "",
            "hint": f"Окно ассистента открыто: {address}"}

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Нативное окно PrintFlow — как у 1С/Photoshop.

Открывает локальный сервер и показывает панель в системном WebView
(Windows WebView2, macOS WKWebView, Linux WebKitGTK). Если pywebview
не установлен — падает в обычный браузер.

Запуск:  python pf.py app

Требует: pip install pywebview
"""
from __future__ import annotations

import sys
import time
import subprocess
import socket
import webbrowser
from pathlib import Path

from . import APP_VERSION

# пути как в pf.py
ROOT = Path(__file__).resolve().parents[2]
# Порт окна совпадает с портом лаунчера (`pf.DEFAULT_PORT`): окно открывает
# localhost, а касса на телефоне ищет сервер по 8765 — держать здесь своё число
# значит снова получить «касса не подключается». Импортировать pf.py целиком
# нельзя (окно запускается и без репозитория), поэтому берём то же значение и
# закрываем расхождение контрактом в тестах.
DEFAULT_PORT = 8765


def find_free_port(start=DEFAULT_PORT):
    for p in range(start, start+20):
        try:
            with socket.create_connection(("127.0.0.1", p), timeout=0.3):
                continue
        except OSError:
            return p
    return start


def probe_health(port, attempts=3, pause=0.4):
    """Отвечает ли на порту PrintFlow. Повторы обязательны (18.23).

    Одна секунда на проверку — мало: занятый сервер под нагрузкой не успевает
    ответить, и окно помощника запускало ВТОРОЙ сервер на той же базе — сайт
    после этого «плохо работал» (двойной MQTT, конфликты SQLite).
    """
    import urllib.request, json
    for attempt in range(max(1, int(attempts))):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                pass
        except OSError:
            return False  # порт свободен — это точно не работающий PrintFlow
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
                if json.loads(r.read().decode()).get("version"):
                    return True
        except Exception:
            pass
        if attempt + 1 < attempts:
            time.sleep(pause)
    return False


def find_printflow(start, span=9, attempts=1):
    """Уже запущенный PrintFlow рядом с ожидаемым портом — привязаться, не поднимать второй."""
    for port in range(start, start + span):
        if probe_health(port, attempts=attempts):
            return port
    return None


def pick_port(start):
    """(порт, нужно ли запускать сервер). Второй сервер на той же базе не появляется никогда."""
    found = find_printflow(start, attempts=3) or find_printflow(max(DEFAULT_PORT, start - 4), attempts=1)
    if found:
        return found, False
    try:
        with socket.create_connection(("127.0.0.1", start), timeout=0.4):
            # порт занят чужой программой — берём свободный рядом
            return find_free_port(start + 1), True
    except OSError:
        return start, True


def assistant_window_argv(port, attach_only=True):
    """Аргументы окна помощника (`pf.py assistant`).

    `attach_only=True` — окно открывается у УЖЕ работающей панели и не имеет
    права запускать свой сервер (док-строка `cmd_assistant` обещала это, а
    флаг `--no-server` до 18.23 забывали передать).
    """
    argv = ["--port", str(port), "--path", "/assistant.html"]
    if attach_only:
        argv.append("--no-server")
    return argv


def wait_for_port(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.8):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def start_server(port, lan=True):
    """Запустить коннектор как дочерний процесс."""
    # используем то же окружение что pf.py
    try:
        import pf
        py = pf.interpreter(quiet=True)
        host = "0.0.0.0" if lan else "127.0.0.1"
        cmd = [str(py), str(pf.ENTRYPOINT), "--host", host, "--port", str(port), "--no-browser", "--no-banner"]
    except Exception:
        # fallback — системный python
        py = Path(sys.executable)
        host = "0.0.0.0" if lan else "127.0.0.1"
        cmd = [str(py), str(ROOT / "connector" / "printflow_connector.py"), "--host", host, "--port", str(port), "--no-browser", "--no-banner"]
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc


def open_native_window(url: str, title: str | None = None):
    try:
        import webview  # pywebview
    except ImportError:
        webbrowser.open(url)
        return 0
    # нативное окно
    webview.create_window(
        title or f"NOZZA · PrintFlow {APP_VERSION}",
        url,
        width=1280,
        height=840,
        min_size=(1024, 640),
        text_select=True,
    )
    # меню как у 1С — лёгкое
    webview.start(debug=False)
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="PrintFlow — нативное окно")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--local", action="store_true", help="только 127.0.0.1")
    ap.add_argument("--no-server", action="store_true", help="не запускать сервер, только окно")
    # 18.13: окно умеет открывать не только панель. Помощник — та же страница
    # сервера в своём окне (`pf.py assistant`), поэтому путь задаётся, а
    # отдельное приложение с собственным HTTP-клиентом не появляется.
    ap.add_argument("--path", default="/", help="адрес внутри сервера (например /assistant.html)")
    ap.add_argument("--title", default="", help="заголовок окна")
    args = ap.parse_args(argv)

    port = args.port
    proc = None
    if args.no_server:
        # Окно-приложение к работающей панели: свой сервер не нужен (18.23).
        if not probe_health(port, attempts=3):
            print(f"PrintFlow не отвечает на {port}. Сначала запустите панель: python pf.py")
            return 1
    else:
        # Ищем уже работающий PrintFlow рядом и ТОЛЬКО при полном отсутствии
        # поднимаем свой — иначе получался второй сервер с той же базой.
        port, need_start = pick_port(port)
        if not need_start:
            print(f"PrintFlow уже работает на {port} — открываю окно без второго сервера")
        if need_start:
            print(f"Запускаю сервер на порту {port}…")
            proc = start_server(port, lan=not args.local)
            if not wait_for_port(port, timeout=12):
                print("Сервер не поднялся — страницу не открыть. Проверьте журнал PrintFlow.")
                return 1


    page = str(args.path or "/").strip()
    if not page.startswith("/"):
        page = "/" + page
    url = f"http://localhost:{port}{page}"
    print(f"Открываю окно {url}")
    try:
        return open_native_window(url, args.title or None)
    finally:
        if proc and proc.poll() is None:
            # при закрытии окна — спросить? пока оставляем висеть в фоне 2 сек и гасим
            # чтобы не терять печать, оставляем сервер жить
            pass

if __name__ == "__main__":
    raise SystemExit(main())

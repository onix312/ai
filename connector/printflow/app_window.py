#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Нативное окно PrintFlow 8.1 — как у 1С/Photoshop.

Открывает локальный сервер и показывает панель в системном WebView
(Windows WebView2, macOS WKWebView, Linux WebKitGTK). Если pywebview
не установлен — падает в обычный браузер.

Запуск:  python -m printflow.app_window  или  python pf.py app

Требует: pip install pywebview
"""
from __future__ import annotations

import os
import sys
import time
import threading
import subprocess
import socket
import webbrowser
from pathlib import Path

# пути как в pf.py
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PORT = 8080


def find_free_port(start=DEFAULT_PORT):
    import socket as s
    for p in range(start, start+20):
        try:
            with s.create_connection(("127.0.0.1", p), timeout=0.3):
                continue
        except OSError:
            return p
    return start


def wait_for_port(port, timeout=15):
    import socket, time
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


def open_native_window(url: str, title: str = "NOZZA · PrintFlow 8.0"):
    try:
        import webview  # pywebview
    except ImportError:
        webbrowser.open(url)
        return 0
    # нативное окно
    window = webview.create_window(
        title,
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
    args = ap.parse_args(argv)

    port = args.port
    # если порт занят и там не PrintFlow — взять свободный
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            # порт занят — проверить health
            import urllib.request, json
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
                    data = json.loads(r.read().decode())
                    if data.get("version"):
                        print(f"PrintFlow уже работает на {port}")
                    else:
                        port = find_free_port(port+1)
            except Exception:
                port = find_free_port(port+1)
    except OSError:
        pass

    proc = None
    if not args.no_server:
        # если сервер уже отвечает — не стартуем второй
        need_start = True
        try:
            import urllib.request, json
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
                if json.loads(r.read().decode()).get("version"):
                    need_start = False
        except Exception:
            need_start = True
        if need_start:
            print(f"Запускаю сервер на порту {port}…")
            proc = start_server(port, lan=not args.local)
            if not wait_for_port(port, timeout=12):
                print("Сервер не поднялся, открываю браузер как fallback")
                webbrowser.open(f"http://localhost:{port}/")
                return 1

    url = f"http://localhost:{port}/"
    print(f"Открываю окно {url}")
    try:
        return open_native_window(url)
    finally:
        if proc and proc.poll() is None:
            # при закрытии окна — спросить? пока оставляем висеть в фоне 2 сек и гасим
            # чтобы не терять печать, оставляем сервер жить
            pass

if __name__ == "__main__":
    raise SystemExit(main())

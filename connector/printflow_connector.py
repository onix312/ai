#!/usr/bin/env python3
"""PrintFlow — локальный сервер управления 3D-производством.

Раздаёт сайт, хранит все данные в собственной базе и связывает браузер с
принтерами Bambu Lab по локальным протоколам (MQTT/TLS, камера, FTPS).

Секреты (Access Code, серийные номера, Telegram-токен) хранятся только в
каталоге данных пользователя и никогда не попадают в репозиторий.

Запуск:
    python connector/printflow_connector.py
    python connector/printflow_connector.py --port 8080 --no-browser
"""
from __future__ import annotations

import argparse
import sys
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from printflow import APP_VERSION  # noqa: E402
from printflow.api import serve  # noqa: E402
from printflow.config import DATA_DIR  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="PrintFlow — локальный сервер производства")
    parser.add_argument("--host", default="127.0.0.1",
                        help="адрес прослушивания; оставьте 127.0.0.1 для безопасности")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    parser.add_argument("--verbose", action="store_true", help="подробный журнал запросов")
    args = parser.parse_args()

    flags = ["--verbose"] if args.verbose else []
    try:
        server = serve(args.host, args.port, flags)
    except OSError as exc:
        print(f"  Не удалось занять порт {args.port}: {exc}")
        print("  Возможно, PrintFlow уже запущен. Закройте старое окно или укажите --port.")
        return 1

    url = f"http://localhost:{args.port}/"
    print(f"  PrintFlow {APP_VERSION}")
    print(f"  Сайт:   {url}")
    print(f"  Данные: {DATA_DIR}")
    print("  Остановка: Ctrl+C")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Останавливаем PrintFlow...")
    finally:
        try:
            server.shutdown()
            Handler_api = getattr(server.RequestHandlerClass, "api", None)
            if Handler_api:
                Handler_api.manager.shutdown()
                Handler_api.db.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

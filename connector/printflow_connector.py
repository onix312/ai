#!/usr/bin/env python3
"""PrintFlow — локальный сервер управления 3D-производством.

Раздаёт сайт, хранит все данные в собственной базе и связывает браузер с
принтерами Bambu Lab по локальным протоколам (MQTT/TLS, камера, FTPS).

Секреты (Access Code, серийные номера, Telegram-токен) хранятся только в
каталоге данных пользователя и никогда не попадают в репозиторий.

Запуск:
    python connector/printflow_connector.py
    python connector/printflow_connector.py --host 0.0.0.0 --port 8765
    python connector/printflow_connector.py --lan  # то же что --host 0.0.0.0

Порт по умолчанию — 8765: он же в подсказках мобильной кассы и пульта, и он же
у UDP-маяка автопоиска (`printflow/discovery.py`), поэтому телефону не нужно
угадывать адрес сервера.

Обычный путь запуска — лаунчер в корне репозитория: ``python pf.py``.
"""
from __future__ import annotations

import argparse
import signal
import sqlite3
import sys
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from printflow import APP_VERSION  # noqa: E402
from printflow import discovery  # noqa: E402
from printflow.api import serve  # noqa: E402
from printflow.config import DATA_DIR, get_local_ips  # noqa: E402
from printflow.logging_setup import setup_logging  # noqa: E402

# Порт по умолчанию задаётся один раз здесь и повторяется в лаунчере (`pf.py`),
# в подсказках мобильной кассы и в UDP-маяке. Держать его в одном месте важнее,
# чем «привычные» 8080: касса ищет сервер именно по этому номеру.
DEFAULT_PORT = 8765


def print_banner(host: str, port: int, data_dir: Path, lan_ips: list[str]) -> None:
    """Адреса и «что делать с телефоном» — для запуска коннектора напрямую.

    Тот же смысл, что у баннера лаунчера: один раз показать владельцу, куда
    зайти с телефона. Адреса телефона печатаем и тогда, когда сервер поднят
    только на localhost, — иначе «не открывается с телефона» выглядит загадкой.
    """
    line = "─" * 58
    print()
    print(f"  {line}")
    print(f"  PrintFlow {APP_VERSION} — Bambu Lab + AMS")
    print(f"  {line}")
    print()
    print(f"  Папка данных: {data_dir}")
    print()

    print("  Панель владельца (на этом компьютере):")
    print(f"    → http://localhost:{port}/")
    print()

    if host in ("127.0.0.1", "localhost"):
        print("  ⚠ Сервер слушает ТОЛЬКО localhost (127.0.0.1):")
        print("    с телефона по сети зайти не получится.")
        print("    Запустите с доступом по сети — лаунчером:")
        print("      python pf.py")
        print("    или вручную:")
        print(f"      python connector/printflow_connector.py --host 0.0.0.0 --port {port}")
        print()
    else:
        print("  Телефон и планшет в той же Wi-Fi сети:")
        if lan_ips:
            for ip in lan_ips:
                print(f"    панель  → http://{ip}:{port}/")
                print(f"    касса   → http://{ip}:{port}/cashier.html")
                print(f"    пульт   → http://{ip}:{port}/pult")
        else:
            print("    сетевой IP не определился — подключите Wi-Fi или кабель")
        print()
        print(f"  Сервер объявляет себя в сети (UDP {discovery.BEACON_PORT}):")
        print("    приложение кассы находит его само — «Найти сервер в сети».")
        print("    Если не находит: разрешите порты "
              f"{port}/TCP и {discovery.BEACON_PORT}/UDP в брандмауэре")
        print("    и убедитесь, что телефон не в гостевой сети Wi-Fi.")
    print()
    print(f"  {line}")
    print("  Не закрывайте это окно: без него сайт не сохраняет данные.")
    print("  Для остановки нажмите Ctrl+C")
    print(f"  {line}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="PrintFlow — локальный сервер производства")
    parser.add_argument("--host", default="127.0.0.1",
                        help="адрес прослушивания (127.0.0.1 — только этот ПК, 0.0.0.0 — вся локалка)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"порт панели (по умолчанию {DEFAULT_PORT})")
    parser.add_argument("--lan", action="store_true",
                        help="короткий флаг для --host 0.0.0.0 (доступ по сети)")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    parser.add_argument("--no-banner", action="store_true",
                        help="не печатать баннер с адресами (его показывает лаунчер pf.py)")
    parser.add_argument("--verbose", action="store_true", help="подробный журнал запросов")
    args = parser.parse_args()

    if args.lan:
        args.host = "0.0.0.0"

    flags = ["--verbose"] if args.verbose else []
    setup_logging(args.verbose)
    from printflow.logging_setup import log
    # Отложенное восстановление базы: маркер из панели «Настройки → Данные».
    # Выполняется до открытия базы, чтобы не подменять файл под живым соединением.
    from printflow.db import (DatabaseRecoveryError, apply_pending_restore,
                              friendly_sqlite_error)
    restore = apply_pending_restore()
    if restore is not None:
        if restore.get("error"):
            log().error("Откат базы не удался: %s", restore["error"])
            print(f"  ❌ Откат базы не удался: {restore['error']}")
        else:
            log().info("База восстановлена из копии %s", restore["restored"])
            print(f"  ✓ База восстановлена из копии {restore['restored']}")
    log().info("PrintFlow %s стартует: %s:%s (данные: %s)", APP_VERSION, args.host, args.port, DATA_DIR)
    try:
        server = serve(args.host, args.port, flags)
    except DatabaseRecoveryError as exc:
        log().error("Безопасное восстановление базы не удалось: %s", exc)
        print(f"\n  ❌ {exc}")
        print("  Запустите диагностику: python pf.py doctor")
        return 1
    except sqlite3.DatabaseError as exc:
        message = friendly_sqlite_error(exc)
        log().exception("Не удалось открыть базу данных")
        print(f"\n  ❌ {message}")
        return 1
    except OSError as exc:
        print(f"\n  ❌ Не удалось занять {args.host}:{args.port}: {exc}")
        print("  Возможно, PrintFlow уже запущен. Закройте старое окно или укажите --port.")
        print("  Пример: --port 9000")
        return 1

    lan_ips = get_local_ips()
    url = f"http://localhost:{args.port}/"
    if not args.no_banner:
        print_banner(args.host, args.port, DATA_DIR, lan_ips)
    else:
        # Запуск из лаунчера: адреса и QR уже показаны им, здесь только факт старта.
        print(f"  PrintFlow {APP_VERSION} слушает {args.host}:{args.port}", flush=True)

    # Открываем именно localhost — он всегда работает, даже если привязаны к 0.0.0.0
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    # Доп. подсказка: первый IP для быстрого копирования
    if lan_ips and args.host == "0.0.0.0" and not args.no_banner:
        print("  Быстрая ссылка для телефона в той же Wi-Fi сети:")
        print(f"  http://{lan_ips[0]}:{args.port}/")
        print()

    stop_requested = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True

    # systemd, launchd и Планировщик завершают foreground-сервис через SIGTERM.
    # Без обработчика процесс обрывался до server.shutdown() и закрытия базы.
    try:
        signal.signal(signal.SIGTERM, request_stop)
    except (AttributeError, OSError, ValueError):
        pass

    try:
        while not stop_requested:
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop_requested = True
    finally:
        if stop_requested:
            print("\n  Останавливаем PrintFlow...", flush=True)
        try:
            server.shutdown()
            handler_api = getattr(server.RequestHandlerClass, "api", None)
            if handler_api:
                handler_api.live.shutdown()
                handler_api.updater.shutdown()
                handler_api.manager.shutdown()
                handler_api.db.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

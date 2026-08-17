#!/usr/bin/env python3
"""PrintFlow — локальный сервер управления 3D-производством.

Раздаёт сайт, хранит все данные в собственной базе и связывает браузер с
принтерами Bambu Lab по локальным протоколам (MQTT/TLS, камера, FTPS).

Секреты (Access Code, серийные номера, Telegram-токен) хранятся только в
каталоге данных пользователя и никогда не попадают в репозиторий.

Запуск:
    python connector/printflow_connector.py
    python connector/printflow_connector.py --host 0.0.0.0 --port 8080
    python connector/printflow_connector.py --lan  # то же что --host 0.0.0.0
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from printflow import APP_VERSION  # noqa: E402
from printflow.api import serve  # noqa: E402
from printflow.logging_setup import setup_logging  # noqa: E402
from printflow.config import DATA_DIR  # noqa: E402


def get_local_ips() -> list[str]:
    """Вернуть список IPv4-адресов этого ПК в локальной сети.

    Метод устойчив к отсутствию интернета: пробуем несколько способов.
    """
    ips: set[str] = set()

    # 1) через hostname
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if ip and not ip.startswith("127.") and "." in ip:
                ips.add(ip)
    except Exception:
        pass

    # 2) через исходящий маршрут (не отправляет данных)
    for target in [("8.8.8.8", 80), ("1.1.1.1", 80), ("192.168.1.1", 80)]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.8)
            s.connect(target)
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127.") and "." in ip:
                ips.add(ip)
        except Exception:
            pass

    # Фильтруем link-local и оставляем приоритет: 192.168.* > 10.* > 172.16-31.* > прочее
    def sort_key(ip: str):
        if ip.startswith("192.168."):
            return (0, ip)
        if ip.startswith("10."):
            return (1, ip)
        if ip.startswith("172."):
            try:
                second = int(ip.split(".")[1])
                if 16 <= second <= 31:
                    return (2, ip)
            except Exception:
                pass
        if ip.startswith("169.254."):
            return (99, ip)
        return (3, ip)

    cleaned = [ip for ip in ips if not ip.startswith("169.254.")]
    if not cleaned:
        cleaned = list(ips)
    return sorted(cleaned, key=sort_key)


def print_banner(host: str, port: int, data_dir: Path, lan_ips: list[str]) -> None:
    line = "─" * 58
    print()
    print(f"  {line}")
    print(f"  PrintFlow {APP_VERSION} — Bambu Lab + AMS")
    print(f"  {line}")
    print()
    print(f"  Папка данных: {data_dir}")
    print()

    local_url = f"http://localhost:{port}/"
    # Для красоты показываем и 127.0.0.1
    print("  ЛОКАЛЬНЫЙ ДОСТУП (на этом компьютере):")
    print(f"    → {local_url}")
    if port != 8080:
        print(f"    → http://127.0.0.1:{port}/")
    print()

    if lan_ips:
        print("  СЕТЬ Wi-Fi / LAN (для телефона, планшета, другого ПК):")
        for ip in lan_ips:
            print(f"    → http://{ip}:{port}/")
        print()
    else:
        print("  Сетевой IP не определился (нет сети). Подключитесь к Wi-Fi/LAN.")
        print()

    if host in ("127.0.0.1", "localhost"):
        print("  ⚠ Сейчас сервер слушает ТОЛЬКО localhost (127.0.0.1).")
        print("    С других устройств по сетевому IP зайти НЕ получится.")
        print("    Для доступа по сети запустите:")
        print(f"      python connector/printflow_connector.py --host 0.0.0.0 --port {port}")
        print("    Или используйте:")
        print("      site/ЗАПУСТИТЬ-Windows.bat")
        print("      site/ЗАПУСТИТЬ-Mac-Linux.command")
        print("    (они уже запускают с --host 0.0.0.0)")
    else:
        print(f"  Сервер слушает {host}:{port} (все интерфейсы)" if host == "0.0.0.0" else f"  Сервер слушает {host}:{port}")
        print(f"  Если не открывается с другого устройства:")
        print(f"    - проверьте что оба устройства в одной Wi-Fi сети")
        print(f"    - разрешите порт {port} в Брандмауэре Windows / Firewall")
        print(f"    - попробуйте выключить VPN")
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
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--lan", action="store_true",
                        help="короткий флаг для --host 0.0.0.0 (доступ по сети)")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    parser.add_argument("--verbose", action="store_true", help="подробный журнал запросов")
    args = parser.parse_args()

    if args.lan:
        args.host = "0.0.0.0"

    flags = ["--verbose"] if args.verbose else []
    setup_logging(args.verbose)
    from printflow.logging_setup import log
    log().info("PrintFlow %s стартует: %s:%s (данные: %s)", APP_VERSION, args.host, args.port, DATA_DIR)
    try:
        server = serve(args.host, args.port, flags)
    except OSError as exc:
        print(f"\n  ❌ Не удалось занять {args.host}:{args.port}: {exc}")
        print(f"  Возможно, PrintFlow уже запущен. Закройте старое окно или укажите --port.")
        print(f"  Пример: --port 9000")
        return 1

    lan_ips = get_local_ips()
    url = f"http://localhost:{args.port}/"
    print_banner(args.host, args.port, DATA_DIR, lan_ips)

    # Открываем именно localhost — он всегда работает, даже если привязаны к 0.0.0.0
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    # Доп. подсказка: первый IP для быстрого копирования
    if lan_ips and args.host == "0.0.0.0":
        print(f"  Быстрая ссылка для телефона в той же Wi-Fi сети:")
        print(f"  http://{lan_ips[0]}:{args.port}/")
        print()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Останавливаем PrintFlow...")
    finally:
        try:
            server.shutdown()
            handler_api = getattr(server.RequestHandlerClass, "api", None)
            if handler_api:
                handler_api.manager.shutdown()
                handler_api.db.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

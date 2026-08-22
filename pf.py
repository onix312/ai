#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PrintFlow — единая точка входа: запуск, окно, установка, обслуживание.

Заменяет собой четыре старых скрипта (ЗАПУСТИТЬ-*.bat/.command и
СОБРАТЬ-EXE-*). Ничего, кроме стандартной библиотеки Python, не требует.

    python pf.py                 запустить панель (доступна в локальной сети)
    python pf.py gui             окно управления вместо чёрной консоли
    python pf.py install         ярлык на рабочем столе и автозапуск
    python pf.py doctor          диагностика: что не так и что делать

Полный список команд: python pf.py help
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import plistlib
import shutil
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONNECTOR = ROOT / "connector"
REQUIREMENTS = CONNECTOR / "requirements.txt"
ENTRYPOINT = CONNECTOR / "printflow_connector.py"
SPEC_FILE = CONNECTOR / "pyinstaller.spec"

MIN_PYTHON = (3, 10)
DEFAULT_PORT = 8080
APP_NAME = "PrintFlow"

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# Каталоги те же, что использует сам коннектор (connector/printflow/config.py),
# поэтому переход со старых .bat/.command не теряет ни базу, ни окружение.
if IS_WINDOWS:
    DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "PrintFlow"
    STATE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PrintFlow"
else:
    DATA_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "printflow"
    STATE_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "printflow"

VENV_DIR = STATE_DIR / "venv"
BUILD_VENV_DIR = STATE_DIR / "build-venv"
DEPS_MARKER = VENV_DIR / ".printflow-deps"
DB_FILE = DATA_DIR / "printflow.sqlite3"
LOG_FILE = DATA_DIR / "connector.log"
RUN_LOG = DATA_DIR / "launcher.log"
PID_FILE = STATE_DIR / "printflow.pid"
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_KEEP = 20


# ─────────────────────────────────────────────────────────── вывод в терминал
class Style:
    """ANSI-оформление. Отключается само, если вывод перенаправлен в файл."""

    enabled = False
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

    @classmethod
    def setup(cls) -> None:
        try:  # блочные символы QR и кириллица требуют UTF-8
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        cls.enabled = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
        if cls.enabled and IS_WINDOWS:
            try:  # включаем обработку ANSI в консоли Windows 10+
                import ctypes

                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            except Exception:
                cls.enabled = False

    @classmethod
    def paint(cls, text: str, *codes: str) -> str:
        if not cls.enabled or not codes:
            return text
        return "".join(codes) + text + cls.RESET


def say(text: str = "", *codes: str) -> None:
    # flush обязателен: вывод часто уходит в трубу (окно лаунчера, журнал),
    # а пользователь должен видеть адреса сразу, а не после остановки.
    print(Style.paint(text, *codes), flush=True)


def ok(text: str) -> None:
    say(f"  ✓ {text}", Style.GREEN)


def warn(text: str) -> None:
    say(f"  ⚠ {text}", Style.YELLOW)


def fail(text: str) -> None:
    say(f"  ✗ {text}", Style.RED)


def step(text: str) -> None:
    say(f"  → {text}", Style.DIM)


def rule(char: str = "─", width: int = 62) -> str:
    return char * width


def header(title: str, subtitle: str = "") -> None:
    say()
    say("  " + rule(), Style.MAGENTA)
    say(f"  {title}", Style.BOLD, Style.MAGENTA)
    if subtitle:
        say(f"  {subtitle}", Style.DIM)
    say("  " + rule(), Style.MAGENTA)
    say()


def ask(question: str, default: bool = True) -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        return default
    hint = "Д/н" if default else "д/Н"
    try:
        answer = input(Style.paint(f"  {question} [{hint}] ", Style.CYAN)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        say()
        return False
    if not answer:
        return default
    return answer[0] in "yд1"


# ───────────────────────────────────────────────────────────────── окружение
def app_version() -> str:
    """Версия из пакета — без импорта, чтобы не тянуть зависимости."""
    init = CONNECTOR / "printflow" / "__init__.py"
    try:
        for line in init.read_text(encoding="utf-8").splitlines():
            if line.startswith("APP_VERSION"):
                return line.split("=", 1)[1].strip().strip('"\'')
    except OSError:
        pass
    return "?"


def venv_python(venv: Path = VENV_DIR) -> Path:
    return venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def requirements_fingerprint() -> str:
    payload = REQUIREMENTS.read_bytes() if REQUIREMENTS.exists() else b""
    version = f"{sys.version_info.major}.{sys.version_info.minor}".encode()
    return hashlib.sha256(payload + b"|" + version).hexdigest()[:16]


def ensure_venv(force_deps: bool = False, quiet: bool = False) -> Path:
    """Создать окружение и поставить зависимости — но только когда нужно.

    Старые .bat/.command дёргали pip при каждом запуске: лишние секунды и
    обязательный интернет. Здесь запоминается отпечаток requirements.txt,
    и установка повторяется, только если он изменился.
    """
    python = venv_python()
    if not python.exists():
        if not quiet:
            step(f"Создаю окружение: {VENV_DIR}")
        VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)])
        if result.returncode != 0 or not python.exists():
            fail("Не удалось создать виртуальное окружение Python")
            say("    Убедитесь, что установлен пакет python3-venv "
                "(Linux) или полный дистрибутив Python (Windows/macOS).")
            raise SystemExit(1)

    fingerprint = requirements_fingerprint()
    installed = DEPS_MARKER.read_text(encoding="utf-8").strip() if DEPS_MARKER.exists() else ""
    if installed == fingerprint and not force_deps:
        return python

    if not quiet:
        step("Проверяю зависимости (нужен интернет только в первый раз)…")
    result = subprocess.run([str(python), "-m", "pip", "install",
                             "--disable-pip-version-check", "-q",
                             "-r", str(REQUIREMENTS)])
    if result.returncode != 0:
        warn("Зависимости поставить не удалось — работаем на том, что есть")
        say("    Проверьте интернет и повторите: python pf.py deps")
    else:
        DEPS_MARKER.write_text(fingerprint, encoding="utf-8")
        if not quiet:
            ok("Зависимости готовы")
    return python


def interpreter(system: bool = False, quiet: bool = False) -> Path:
    """Python, которым запускать сервер: из окружения либо системный."""
    if system:
        return Path(sys.executable)
    return ensure_venv(quiet=quiet)


def check_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        fail(f"Нужен Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} или новее, "
             f"а сейчас {platform.python_version()}")
        say("    Скачать: https://python.org/downloads/")
        raise SystemExit(1)


# ────────────────────────────────────────────────────────────── сеть и статус
def local_ips() -> list[str]:
    """Адреса этого компьютера в локальной сети — для телефона и планшета."""
    found: set[str] = set()
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip and not ip.startswith("127."):
                found.add(ip)
    except Exception:
        pass
    for target in (("8.8.8.8", 80), ("1.1.1.1", 80), ("192.168.1.1", 80)):
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.settimeout(0.6)
            probe.connect(target)
            ip = probe.getsockname()[0]
            probe.close()
            if ip and not ip.startswith("127."):
                found.add(ip)
        except Exception:
            pass

    def rank(ip: str) -> tuple:
        if ip.startswith("192.168."):
            return (0, ip)
        if ip.startswith("10."):
            return (1, ip)
        if ip.startswith("172."):
            try:
                if 16 <= int(ip.split(".")[1]) <= 31:
                    return (2, ip)
            except (IndexError, ValueError):
                pass
        if ip.startswith("100."):
            try:
                if 64 <= int(ip.split(".")[1]) <= 127:
                    return (3, ip)  # CGNAT/Tailscale
            except (IndexError, ValueError):
                pass
        return (99, ip)

    # Не показываем APIPA 169.254/16 и публичные интерфейсы как адрес панели:
    # первый означает сломанный DHCP, второй нельзя рекламировать для LAN-сервера.
    return sorted((ip for ip in found if rank(ip)[0] < 99), key=rank)


def port_busy(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((host, port)) == 0


def free_port(start: int, attempts: int = 20) -> int:
    for port in range(start, start + attempts):
        if not port_busy(port):
            return port
    return start


def health(port: int, timeout: float = 1.2) -> dict | None:
    """Ответ /api/health, если на порту действительно PrintFlow."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data if isinstance(data, dict) and data.get("version") else None
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def read_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def write_pid(pid: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid), encoding="utf-8")


def clear_pid() -> None:
    try:
        PID_FILE.unlink()
    except OSError:
        pass


def running_port() -> int | None:
    """Найти порт работающего PrintFlow: сначала обычный, потом соседние."""
    for port in [DEFAULT_PORT, *range(DEFAULT_PORT + 1, DEFAULT_PORT + 11), 8000, 9000]:
        if port_busy(port) and health(port):
            return port
    return None


# ─────────────────────────────────────────────────────────────────── баннер
def qr_lines(url: str) -> list[str]:
    """QR-код ссылки: навёл телефон — панель открылась."""
    try:
        sys.path.insert(0, str(CONNECTOR))
        from printflow import qrgen  # локальный модуль, без зависимостей

        return qrgen.terminal(url, border=2).splitlines()
    except Exception:
        return []


def banner(host: str, port: int, ips: list[str], show_qr: bool = True) -> None:
    version = app_version()
    say()
    say("  " + rule("━"), Style.MAGENTA)
    say(f"  NOZZA · PrintFlow {version}", Style.BOLD, Style.MAGENTA)
    say("  Управление 3D-производством · Bambu Lab + AMS", Style.DIM)
    say("  " + rule("━"), Style.MAGENTA)
    say()

    phone_url = f"http://{ips[0]}:{port}/" if ips else f"http://localhost:{port}/"
    lines = [
        (Style.paint("  Этот компьютер", Style.BOLD)),
        f"    {Style.paint(f'http://localhost:{port}/', Style.CYAN)}",
    ]
    if host in ("0.0.0.0", "::"):
        lines.append("")
        lines.append(Style.paint("  Телефон и планшет в той же Wi-Fi сети", Style.BOLD))
        if ips:
            for ip in ips:
                lines.append(f"    {Style.paint(f'http://{ip}:{port}/', Style.CYAN)}")
        else:
            lines.append(Style.paint("    сеть не определилась — проверьте Wi-Fi", Style.YELLOW))
    else:
        lines.append("")
        lines.append(Style.paint("  Режим «только этот компьютер»", Style.YELLOW))
        lines.append(Style.paint("    по сети зайти нельзя — запустите без --local", Style.DIM))
    lines.append("")
    lines.append(f"  {Style.paint('Данные:', Style.DIM)} {DATA_DIR}")
    lines.append(f"  {Style.paint('Остановить:', Style.DIM)} Ctrl+C")

    art = qr_lines(phone_url) if (show_qr and ips and host in ("0.0.0.0", "::")) else []
    if art:
        pad = max(len(line) for line in lines) if lines else 0
        for index in range(max(len(art), len(lines))):
            left = lines[index] if index < len(lines) else ""
            visible = len(strip_ansi(left))
            right = art[index] if index < len(art) else ""
            print(left + " " * max(2, pad - visible + 2) + right, flush=True)
        say()
        say(f"  Наведите камеру телефона на код → {phone_url}", Style.DIM)
    else:
        for line in lines:
            print(line, flush=True)
    say()


def strip_ansi(text: str) -> str:
    out, skip = [], False
    for char in text:
        if char == "\033":
            skip = True
        elif skip:
            if char.isalpha():
                skip = False
        else:
            out.append(char)
    return "".join(out)


# ──────────────────────────────────────────────────────────────── команды
def cmd_start(args: argparse.Namespace) -> int:
    check_python_version()
    host = "127.0.0.1" if args.local else "0.0.0.0"
    port = args.port

    if port_busy(port):
        alive = health(port)
        if alive:
            say()
            ok(f"PrintFlow {alive.get('version', '')} уже работает на порту {port}")
            say(f"    Открываю http://localhost:{port}/", Style.DIM)
            if not args.no_browser:
                webbrowser.open(f"http://localhost:{port}/")
            return 0
        if args.auto_port:
            port = free_port(port + 1)
            warn(f"Порт {args.port} занят другой программой — беру {port}")
        else:
            fail(f"Порт {port} занят другой программой")
            say("    Свободный порт:  python pf.py start --auto-port")
            say(f"    Или вручную:      python pf.py start --port {free_port(port + 1)}")
            return 1

    python = interpreter(args.system)
    command = [str(python), str(ENTRYPOINT), "--host", host, "--port", str(port),
               "--no-browser", "--no-banner"]
    if args.verbose:
        command.append("--verbose")

    if args.background:
        return start_background(command, host, port, args)

    ips = local_ips() if host == "0.0.0.0" else []
    banner(host, port, ips, show_qr=not args.no_qr)

    process = subprocess.Popen(command, cwd=str(ROOT))
    write_pid(process.pid)
    if not args.no_browser:
        wait_for_server(port)
        webbrowser.open(f"http://localhost:{port}/")
    try:
        return process.wait()
    except KeyboardInterrupt:
        say()
        step("Останавливаю PrintFlow…")
        try:
            process.wait(timeout=10)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            process.kill()
        ok("Остановлено. Данные сохранены.")
        return 0
    finally:
        clear_pid()


def start_background(command: list[str], host: str, port: int, args: argparse.Namespace) -> int:
    """Запуск без окна: сервер живёт сам, консоль можно закрыть."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log = open(RUN_LOG, "ab", buffering=0)
    log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} запуск {' '.join(command)}\n"
              .encode("utf-8"))
    creation = 0
    kwargs: dict = {}
    if IS_WINDOWS:
        creation = 0x00000008 | 0x08000000  # DETACHED_PROCESS | NO_WINDOW
        kwargs["creationflags"] = creation
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, cwd=str(ROOT), stdout=log, stderr=log,
                               stdin=subprocess.DEVNULL, **kwargs)
    write_pid(process.pid)
    if wait_for_server(port, attempts=40):
        ok(f"PrintFlow работает в фоне (pid {process.pid})")
        say(f"    Панель:     http://localhost:{port}/")
        for ip in (local_ips() if host == "0.0.0.0" else []):
            say(f"    С телефона: http://{ip}:{port}/")
        say("    Остановить: python pf.py stop", Style.DIM)
        if not args.no_browser:
            webbrowser.open(f"http://localhost:{port}/")
        return 0
    fail("Сервер не поднялся — смотрите журнал")
    say(f"    {RUN_LOG}")
    return 1


def wait_for_server(port: int, attempts: int = 30) -> bool:
    for _ in range(attempts):
        if health(port, timeout=0.5):
            return True
        time.sleep(0.25)
    return False


def cmd_stop(args: argparse.Namespace) -> int:
    managed, stopped, detail = stop_autostart_runtime()
    if managed:
        if stopped:
            clear_pid()
            ok("Системный сервис остановлен до следующего входа в систему")
            return 0
        fail(f"Не удалось остановить системный сервис: {detail}")
        return 1

    pid = read_pid()
    if pid is None:
        port = running_port()
        if port is None:
            say()
            ok("PrintFlow не запущен")
            return 0
        warn(f"PrintFlow отвечает на порту {port}, но запущен не через pf.py")
        say("    Закройте его окно вручную или завершите процесс Python.")
        return 1
    step(f"Останавливаю процесс {pid}…")
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
    else:
        import signal

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(20):
            if not pid_alive(pid):
                break
            time.sleep(0.25)
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    clear_pid()
    ok("Остановлено. Данные сохранены.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    header("Состояние PrintFlow")
    port = running_port()
    if port is None:
        warn("Сервер не запущен")
        say("    Запуск: python pf.py")
        say()
        return 1
    info = health(port) or {}
    uptime = int(info.get("uptime") or 0)
    ok(f"Работает: версия {info.get('version', '?')}, порт {port}")
    say(f"    Аптайм:     {uptime // 3600} ч {uptime % 3600 // 60} мин")
    say(f"    Панель:     http://localhost:{port}/")
    for ip in local_ips():
        say(f"    С телефона: http://{ip}:{port}/")
    pid = read_pid()
    if pid:
        say(f"    Процесс:    pid {pid}")
    say()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    header("Диагностика PrintFlow", "проверяем то, что обычно и ломается")
    problems = 0

    say("  Python", Style.BOLD)
    if sys.version_info >= MIN_PYTHON:
        ok(f"{platform.python_version()} — подходит ({sys.executable})")
    else:
        fail(f"{platform.python_version()} — нужен {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+")
        problems += 1

    python = venv_python()
    if python.exists():
        ok(f"Окружение: {VENV_DIR}")
        modules = {"paho.mqtt.client": "управление принтером (обязательно)",
                   "PIL": "спагетти-детект по камере (необязательно)"}
        for module, what in modules.items():
            check = subprocess.run([str(python), "-c", f"import {module}"],
                                   capture_output=True)
            if check.returncode == 0:
                ok(f"{module} — {what}")
            elif "обязательно)" in what and "необ" not in what:
                fail(f"{module} не установлен — {what}")
                say("      Починить: python pf.py deps")
                problems += 1
            else:
                warn(f"{module} не установлен — {what}")
    else:
        warn("Окружение ещё не создано (создастся при первом запуске)")

    say()
    say("  Данные", Style.BOLD)
    if DATA_DIR.exists():
        ok(f"Каталог: {DATA_DIR}")
        if os.access(DATA_DIR, os.W_OK):
            ok("Права на запись есть")
        else:
            fail("Нет прав на запись — база не сохранится")
            problems += 1
    else:
        warn(f"Каталог ещё не создан: {DATA_DIR}")

    if DB_FILE.exists():
        size = DB_FILE.stat().st_size
        ok(f"База: {size / 1048576:.1f} МБ")
        try:
            import sqlite3

            conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            counts = {}
            for table in ("orders", "customers", "print_jobs", "documents"):
                try:
                    counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.Error:
                    pass
            conn.close()
            if result == "ok":
                ok("Проверка целостности пройдена")
            else:
                fail(f"База повреждена: {result}")
                say("      Откат к копии: python pf.py restore")
                problems += 1
            if counts:
                say("      " + ", ".join(f"{k}: {v}" for k, v in counts.items()), Style.DIM)
        except Exception as exc:
            try:
                from connector.printflow.db import friendly_sqlite_error
                message = friendly_sqlite_error(exc)
            except Exception:
                message = "База не читается"
            fail(message)
            say("      Перезапустите PrintFlow для автоматического восстановления")
            problems += 1
    else:
        warn("Базы ещё нет — создастся при первом запуске")

    backups = sorted(BACKUP_DIR.glob("*.sqlite3")) if BACKUP_DIR.exists() else []
    if backups:
        newest = max(backups, key=lambda p: p.stat().st_mtime)
        age_days = (time.time() - newest.stat().st_mtime) / 86400
        message = f"Копий: {len(backups)}, последняя {age_days:.1f} дн. назад"
        ok(message) if age_days < 8 else warn(message + " — пора сделать свежую")
    else:
        warn("Резервных копий нет — сделайте: python pf.py backup")

    try:
        usage = shutil.disk_usage(DATA_DIR if DATA_DIR.exists() else Path.home())
        free_gb = usage.free / 1024 ** 3
        (ok if free_gb > 2 else fail)(f"Свободно на диске: {free_gb:.1f} ГБ")
        problems += 0 if free_gb > 2 else 1
    except OSError:
        pass

    say()
    say("  Сеть", Style.BOLD)
    ips = local_ips()
    if ips:
        for ip in ips:
            ok(f"Адрес в сети: {ip}")
    else:
        warn("Локальный IP не определился — телефон не подключится")
    port = args.port
    active = health(port)
    if active:
        ok(f"PrintFlow отвечает на порту {port} (версия {active.get('version')})")
    elif port_busy(port):
        fail(f"Порт {port} занят другой программой")
        say(f"      Свободный: {free_port(port + 1)}")
        problems += 1
    else:
        ok(f"Порт {port} свободен")

    printers = read_printers()
    if printers:
        say()
        say("  Принтеры", Style.BOLD)
        for printer in printers:
            name, ip = printer.get("name") or "принтер", printer.get("ip") or ""
            if not ip:
                warn(f"{name}: не указан IP")
                continue
            checks = {"MQTT 8883": 8883, "камера 6000": 6000, "файлы 990": 990}
            results = []
            for label, tcp_port in checks.items():
                reachable = probe_tcp(ip, tcp_port)
                results.append(f"{'✓' if reachable else '✗'} {label}")
            line = f"{name} ({ip}): " + "  ".join(results)
            (ok if all("✓" in r for r in results) else warn)(line)
            if not probe_tcp(ip, 8883):
                say("      Принтер выключен, в другой сети или включён облачный режим",
                    Style.DIM)

    say()
    if problems:
        fail(f"Найдено проблем: {problems}")
    else:
        ok("Всё в порядке")
    say()
    return 1 if problems else 0


def probe_tcp(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def read_printers() -> list[dict]:
    if not DB_FILE.exists():
        return []
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT name, ip FROM printers").fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception:
        return []


def configured_backup_keep() -> int:
    """Лимит копий из SQLite; без базы или при ошибке — безопасный default."""
    value: object = BACKUP_KEEP
    if DB_FILE.exists():
        try:
            import sqlite3

            conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key='backup_keep'").fetchone()
                if row:
                    value = json.loads(row[0])
            finally:
                conn.close()
        except Exception:
            pass
    try:
        return max(1, min(200, int(float(value))))
    except (TypeError, ValueError, OverflowError):
        return BACKUP_KEEP


def rotate_backups(keep: int | None = None) -> list[Path]:
    """Единый лимит для ручных, автоматических и страховочных копий."""
    keep = configured_backup_keep() if keep is None else max(1, int(keep))
    items = sorted(
        BACKUP_DIR.glob("*.sqlite3"),
        key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    removed = items[keep:]
    for old in removed:
        old.unlink(missing_ok=True)
    return removed


def cmd_backup(args: argparse.Namespace) -> int:
    header("Резервная копия базы")
    if not DB_FILE.exists():
        warn("Базы ещё нет — копировать нечего")
        return 1
    from connector.printflow.db import backup_database_file, friendly_sqlite_error

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"printflow-{stamp}.sqlite3"
    index = 2
    while target.exists():
        target = BACKUP_DIR / f"printflow-{stamp}-{index}.sqlite3"
        index += 1
    try:
        backup_database_file(DB_FILE, target)
    except Exception as exc:
        target.unlink(missing_ok=True)
        fail(f"Копия не создана. {friendly_sqlite_error(exc)}")
        say("    Перезапустите PrintFlow: он попробует восстановить исправную базу.")
        return 1
    ok(f"Копия готова и проверена: {target}")
    say(f"    Размер: {target.stat().st_size / 1048576:.1f} МБ")

    keep = configured_backup_keep()
    extra = rotate_backups(keep)
    if extra:
        step(f"Удалил старых копий: {len(extra)} (держим последние {keep})")
    say()
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    header("Восстановление из копии")
    if running_port():
        fail("Сначала остановите PrintFlow: python pf.py stop")
        return 1
    backups = sorted(BACKUP_DIR.glob("*.sqlite3"),
                     key=lambda p: p.stat().st_mtime, reverse=True) if BACKUP_DIR.exists() else []
    if args.file:
        chosen = Path(args.file).expanduser()
        if not chosen.exists():
            fail(f"Файл не найден: {chosen}")
            return 1
    else:
        if not backups:
            fail("Копий нет. Сделайте: python pf.py backup")
            return 1
        say("  Доступные копии:", Style.BOLD)
        for index, item in enumerate(backups[:10], 1):
            when = time.strftime("%d.%m.%Y %H:%M", time.localtime(item.stat().st_mtime))
            say(f"    {index}. {item.name}  {when}  "
                f"{item.stat().st_size / 1048576:.1f} МБ")
        say()
        try:
            raw = input(Style.paint("  Номер копии (Enter — самая свежая): ", Style.CYAN)).strip()
        except (EOFError, KeyboardInterrupt):
            return 1
        index = int(raw) - 1 if raw.isdigit() else 0
        if not 0 <= index < len(backups):
            fail("Такой копии нет")
            return 1
        chosen = backups[index]

    from connector.printflow.db import (backup_database_file, database_integrity,
                                        install_database_copy,
                                        preserve_damaged_database)

    chosen_check = database_integrity(chosen, ignore_sidecars=True, thorough=True)
    if not chosen_check["ok"]:
        fail("Выбранная копия повреждена или не читается — база не изменена")
        return 1
    if not ask(f"Заменить базу файлом {chosen.name}?", default=False):
        say("  Отменено.")
        return 1

    if DB_FILE.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safety = BACKUP_DIR / f"before-restore-{stamp}.sqlite3"
        index = 2
        while safety.exists():
            safety = BACKUP_DIR / f"before-restore-{stamp}-{index}.sqlite3"
            index += 1
        try:
            backup_database_file(DB_FILE, safety)
            step(f"Текущая база сохранена и проверена: {safety.name}")
        except Exception:
            # Повреждённый файл нельзя класть рядом с исправными копиями: иначе
            # следующий откат снова выберет его как самый свежий.
            try:
                quarantined = preserve_damaged_database(DB_FILE, BACKUP_DIR)
                step(f"Повреждённая текущая база изолирована: {quarantined.parent}")
            except Exception:
                fail("Не удалось сохранить текущую базу — восстановление отменено")
                return 1
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        install_database_copy(chosen, DB_FILE)
    except Exception as exc:
        fail(f"Не удалось установить копию: {exc}")
        return 1
    ok("База восстановлена и проверена. Старые WAL/SHM-журналы удалены.")
    say("    Запускайте: python pf.py")
    say()
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    header("Обновление PrintFlow")
    if not (ROOT / ".git").exists():
        fail("Это не git-копия — обновляйтесь через панель: Настройки → Обновления")
        return 1
    if not shutil.which("git"):
        fail("Не найден git")
        return 1

    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT),
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        fail("Есть незакоммиченные изменения — обновление остановлено")
        say("    Сохраните или отмените их; список покажет: git status")
        return 1

    fetched = subprocess.run(["git", "fetch", "--quiet"], cwd=str(ROOT))
    if fetched.returncode != 0:
        fail("Не удалось получить сведения об обновлении")
        say("    Проверьте интернет и настройку git remote -v")
        return 1
    count = subprocess.run(["git", "rev-list", "--count", "HEAD..@{u}"],
                           cwd=str(ROOT), capture_output=True, text=True)
    behind = count.stdout.strip()
    if count.returncode != 0 or not behind.isdigit():
        fail("У текущей ветки не настроена ветка обновлений")
        say("    Проверьте: git branch -vv")
        return 1
    if behind == "0":
        ok("У вас последняя версия")
        say()
        return 0
    say(f"  Новых изменений: {behind}", Style.BOLD)
    log = subprocess.run(["git", "log", "--oneline", "--no-decorate", "-10", "HEAD..@{u}"],
                         cwd=str(ROOT), capture_output=True, text=True).stdout.strip()
    for line in log.splitlines():
        say(f"    • {line}", Style.DIM)
    say()
    if not ask("Обновиться сейчас?"):
        say("  Отменено.")
        return 1

    if DB_FILE.exists():
        cmd_backup(args)
    result = subprocess.run(["git", "pull", "--ff-only"], cwd=str(ROOT))
    if result.returncode != 0:
        fail("Не удалось обновиться автоматически (расходятся ветки)")
        say("    Разберитесь вручную: git status")
        return 1
    ensure_venv(force_deps=True)
    installed = (DEPS_MARKER.read_text(encoding="utf-8").strip()
                 if DEPS_MARKER.exists() else "")
    if installed != requirements_fingerprint():
        fail("Код обновлён, но зависимости не установились")
        say("    До перезапуска выполните: python pf.py deps")
        return 1
    ok(f"Обновлено до версии {app_version()}")
    if running_port():
        warn("Перезапустите PrintFlow, чтобы изменения вступили в силу:")
        say("    python pf.py stop && python pf.py")
    say()
    return 0


def cmd_deps(args: argparse.Namespace) -> int:
    header("Переустановка зависимостей")
    ensure_venv(force_deps=True)
    installed = (DEPS_MARKER.read_text(encoding="utf-8").strip()
                 if DEPS_MARKER.exists() else "")
    say()
    if installed != requirements_fingerprint():
        fail("Зависимости не установлены")
        return 1
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    header("Сборка автономной программы", "PyInstaller: запуск без установленного Python")
    check_python_version()
    python = venv_python(BUILD_VENV_DIR)
    if not python.exists():
        step(f"Создаю окружение сборки: {BUILD_VENV_DIR}")
        BUILD_VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
        if subprocess.run([sys.executable, "-m", "venv", str(BUILD_VENV_DIR)]).returncode != 0:
            fail("Не удалось создать окружение сборки")
            return 1
    step("Ставлю PyInstaller и зависимости (нужен интернет)…")
    install = subprocess.run([str(python), "-m", "pip", "install", "-q",
                              "--disable-pip-version-check", "--upgrade",
                              "pip", "pyinstaller", "-r", str(REQUIREMENTS)])
    if install.returncode != 0:
        fail("Не удалось поставить PyInstaller")
        return 1
    step("Собираю (5–10 минут, сообщения ниже — нормально)…")
    build = subprocess.run([str(python), "-m", "PyInstaller", str(SPEC_FILE),
                            "--noconfirm", "--distpath", str(ROOT / "dist"),
                            "--workpath", str(ROOT / "build")], cwd=str(ROOT))
    binary = ROOT / "dist" / APP_NAME / (f"{APP_NAME}.exe" if IS_WINDOWS else APP_NAME)
    if build.returncode != 0 or not binary.exists():
        fail("Сборка не удалась — смотрите сообщения выше")
        say("    Частые причины: нет интернета, антивирус блокирует PyInstaller")
        return 1
    say()
    ok(f"Готово: {binary}")
    say(f"    Папку {binary.parent} можно целиком перенести")
    say("    на другой компьютер с той же системой — Python там не нужен.")
    say("    Данные останутся в " + str(DATA_DIR))
    say()
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    lines = args.lines
    shown = False
    for path in (LOG_FILE, RUN_LOG):
        if not path.exists():
            continue
        shown = True
        header(f"Журнал: {path.name}", str(path))
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            fail(str(exc))
            continue
        for line in content[-lines:]:
            print("  " + line)
        say()
    if not shown:
        warn("Журналов пока нет — запустите PrintFlow хотя бы раз")
    return 0


# ─────────────────────────────────────────── установка и системный автозапуск
AUTOSTART_CONFIG = STATE_DIR / "autostart.json"
AUTOSTART_TASK = "PrintFlow Autostart"
AUTOSTART_LABEL = "ru.nozza.printflow"
AUTOSTART_UNIT = "printflow.service"


def _windows_known_folder(csidl: int, fallback: Path) -> Path:
    """Путь Known Folder без предположений о языке Windows и OneDrive."""
    if IS_WINDOWS:
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(32768)
            result = ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buffer)
            if result == 0 and buffer.value:
                return Path(buffer.value)
        except (AttributeError, OSError):
            pass
    return fallback


def windows_programs_dir() -> Path:
    fallback = (Path(os.environ.get("APPDATA", Path.home())) /
                "Microsoft/Windows/Start Menu/Programs")
    return _windows_known_folder(0x0002, fallback)  # CSIDL_PROGRAMS


def windows_startup_dir() -> Path:
    return _windows_known_folder(0x0007, windows_programs_dir() / "Startup")  # CSIDL_STARTUP


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))


def desktop_dir() -> Path | None:
    """Рабочий стол пользователя или None, если его нет (сервер, WSL, док)."""
    if IS_WINDOWS:
        candidate = _windows_known_folder(0x0010, Path.home() / "Desktop")
        return candidate if candidate.is_dir() else None
    try:  # у локализованных систем каталог называется по-своему
        result = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True,
                                text=True, timeout=3)
        path = Path(result.stdout.strip())
        if result.returncode == 0 and path.is_dir() and path != Path.home():
            return path
    except (OSError, subprocess.SubprocessError):
        pass
    for name in ("Desktop", "Рабочий стол"):
        candidate = Path.home() / name
        if candidate.is_dir():
            return candidate
    return None


def launcher_command(mode: str = "gui", background: bool = False) -> list[str]:
    """Команда для ярлыка (без строковой склейки и shell)."""
    python = sys.executable
    if IS_WINDOWS and mode in ("gui", "service"):
        pythonw = Path(python).with_name("pythonw.exe")
        if pythonw.exists():
            python = str(pythonw)
    command = [python, str(ROOT / "pf.py"), mode]
    if background:
        command.append("--background")
    return command


def service_command(args: argparse.Namespace) -> list[str]:
    """Команда автозапуска. Все важные параметры фиксируются при установке."""
    command = launcher_command("service")
    command += ["--port", str(args.port), "--startup-delay", str(args.startup_delay)]
    if args.local:
        command.append("--local")
    if args.system:
        command.append("--system")
    if args.verbose:
        command.append("--verbose")
    return command


def cmd_service(args: argparse.Namespace) -> int:
    """Внутренний foreground-режим для systemd, launchd и Планировщика.

    После подготовки окружения процесс заменяется коннектором через exec: ОС
    отслеживает реальный сервер, а не промежуточный launcher и не двойной daemon.
    """
    check_python_version()
    delay = max(0, min(int(args.startup_delay), 300))
    if delay:
        time.sleep(delay)
    if health(args.port):
        return 0  # уже запущен вручную; следующая сессия попробует снова
    if port_busy(args.port):
        fail(f"Автозапуск: порт {args.port} занят другой программой")
        return 1
    python = interpreter(args.system, quiet=True)
    if IS_WINDOWS and Path(sys.executable).name.lower() == "pythonw.exe":
        pythonw = Path(python).with_name("pythonw.exe")
        if pythonw.exists():
            python = pythonw
    host = "127.0.0.1" if args.local else "0.0.0.0"
    command = [str(python), str(ENTRYPOINT), "--host", host,
               "--port", str(args.port), "--no-browser", "--no-banner"]
    if args.verbose:
        command.append("--verbose")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_pid(os.getpid())
    os.chdir(ROOT)
    try:
        os.execv(str(python), command)
    except OSError as exc:
        clear_pid()
        fail(f"Автозапуск: не удалось запустить сервер: {exc}")
        return 1
    return 1  # pragma: no cover — успешный exec не возвращается


def _atomic_write(path: Path, content: str | bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if isinstance(content, bytes):
        temporary.write_bytes(content)
    else:
        temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
    if mode is not None:
        path.chmod(mode)


def _desktop_quote(value: str | Path) -> str:
    """Кавычки поля Exec по Desktop Entry Specification."""
    value = str(value)
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("`", "\\`").replace("$", "\\$").replace("%", "%%"))
    return f'"{escaped}"'


def _desktop_string(value: str | Path) -> str:
    """Экранирование обычного string-поля .desktop (кавычки там не синтаксис)."""
    return (str(value).replace("\\", "\\\\").replace("\n", "\\n")
            .replace("\r", "\\r").replace("\t", "\\t"))


def _systemd_quote(value: str | Path) -> str:
    # В unit-файлах % — specifier даже внутри кавычек, поэтому удваиваем его.
    value = str(value).replace("%", "%%")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _systemd_path(value: str | Path) -> str:
    """Экранирование path-directive: WorkingDirectory не снимает кавычки."""
    output: list[str] = []
    for char in str(value):
        if char == "%":
            output.append("%%")
        elif char == "\\":
            output.append("\\\\")
        elif char.isspace() or char in ('"', "'", ";", "#"):
            output.extend(f"\\x{byte:02x}" for byte in char.encode("utf-8"))
        else:
            output.append(char)
    return "".join(output)


def render_xdg_entry(command: list[str], *, name: str = "PrintFlow") -> str:
    executable = " ".join(_desktop_quote(part) for part in command)
    return textwrap.dedent(f"""\
        [Desktop Entry]
        Type=Application
        Version=1.0
        Name={name}
        GenericName=Управление 3D-производством
        Comment=NOZZA · заказы, склад и принтеры Bambu Lab
        Exec={executable}
        Path={_desktop_string(ROOT)}
        Terminal=false
        Categories=Office;Utility;
        X-GNOME-Autostart-enabled=true
        """)


def render_systemd_unit(command: list[str]) -> str:
    executable = " ".join(_systemd_quote(part) for part in command)
    return textwrap.dedent(f"""\
        [Unit]
        Description=PrintFlow — локальный сервер 3D-производства
        Wants=network-online.target
        After=network-online.target
        StartLimitIntervalSec=120
        StartLimitBurst=5

        [Service]
        Type=simple
        WorkingDirectory={_systemd_path(ROOT)}
        ExecStart={executable}
        Restart=on-failure
        RestartSec=10
        TimeoutStopSec=30
        Environment=PYTHONUNBUFFERED=1

        [Install]
        WantedBy=default.target
        """)


def launchd_configuration(command: list[str]) -> dict:
    return {
        "Label": AUTOSTART_LABEL,
        "ProgramArguments": command,
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(RUN_LOG),
        "StandardErrorPath": str(RUN_LOG),
    }


def _powershell_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _powershell_executable() -> str | None:
    return shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")


def run_powershell(script: str) -> subprocess.CompletedProcess:
    executable = _powershell_executable()
    if not executable:
        return subprocess.CompletedProcess([], 127, "", "PowerShell не найден")
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return subprocess.run([executable, "-NoProfile", "-NonInteractive",
                           "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                          capture_output=True, text=True)


def create_windows_shortcut(path: Path, command: list[str], description: str) -> tuple[bool, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    script = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$shell = New-Object -ComObject WScript.Shell",
        f"$link = $shell.CreateShortcut({_powershell_literal(path)})",
        f"$link.TargetPath = {_powershell_literal(command[0])}",
        f"$link.Arguments = {_powershell_literal(subprocess.list2cmdline(command[1:]))}",
        f"$link.WorkingDirectory = {_powershell_literal(ROOT)}",
        f"$link.Description = {_powershell_literal(description)}",
        "$link.Save()",
    ])
    result = run_powershell(script)
    error = (result.stderr or result.stdout or "неизвестная ошибка").strip()
    return result.returncode == 0 and path.exists(), error


def render_windows_task_script(command: list[str], start_now: bool = True) -> str:
    arguments = subprocess.list2cmdline(command[1:])
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name",
        f"$action = New-ScheduledTaskAction -Execute {_powershell_literal(command[0])} "
        f"-Argument {_powershell_literal(arguments)} -WorkingDirectory {_powershell_literal(ROOT)}",
        "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user",
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -RestartCount 3 "
        "-RestartInterval (New-TimeSpan -Minutes 1) "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew",
        "$principal = New-ScheduledTaskPrincipal -UserId $user "
        "-LogonType Interactive -RunLevel Limited",
        f"Register-ScheduledTask -TaskName {_powershell_literal(AUTOSTART_TASK)} "
        "-Action $action -Trigger $trigger -Settings $settings -Principal $principal "
        "-Description 'PrintFlow — надёжный запуск локального сервера при входе' "
        "-Force | Out-Null",
    ]
    if start_now:
        lines.append(f"Start-ScheduledTask -TaskName {_powershell_literal(AUTOSTART_TASK)}")
    return "\n".join(lines)


def _autostart_config(args: argparse.Namespace, mechanism: str) -> dict:
    return {
        "version": 1,
        "mechanism": mechanism,
        "root": str(ROOT),
        "port": int(args.port),
        "local": bool(args.local),
        "system": bool(args.system),
        "verbose": bool(args.verbose),
        "startup_delay": int(args.startup_delay),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def load_autostart_config() -> dict:
    try:
        config = json.loads(AUTOSTART_CONFIG.read_text(encoding="utf-8"))
        return config if isinstance(config, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _enable_windows_autostart(args: argparse.Namespace) -> tuple[bool, str, str]:
    command = service_command(args)
    startup = windows_startup_dir() / f"{APP_NAME}.lnk"
    # Удаляем ярлык старой реализации, иначе при входе будут два запуска.
    startup.unlink(missing_ok=True)
    result = run_powershell(render_windows_task_script(command, start_now=not bool(health(args.port))))
    if result.returncode == 0:
        return True, "windows-task", f"Планировщик заданий: {AUTOSTART_TASK}"

    # На урезанных Windows ScheduledTasks может отсутствовать. Перед fallback
    # обязательно убираем возможную старую задачу, иначе получатся два запуска.
    cleanup = run_powershell(
        f"Unregister-ScheduledTask -TaskName {_powershell_literal(AUTOSTART_TASK)} "
        "-Confirm:$false -ErrorAction SilentlyContinue")
    if cleanup.returncode == 0:
        success, shortcut_error = create_windows_shortcut(
            startup, command, "PrintFlow — автозапуск при входе")
        if success:
            detail = "папка Startup (Планировщик недоступен)"
            return True, "windows-startup", detail
    else:
        shortcut_error = (cleanup.stderr or cleanup.stdout or
                          "не удалось удалить старую задачу").strip()
    error = (result.stderr or result.stdout or shortcut_error or "неизвестная ошибка").strip()
    return False, "windows-task", error


def _enable_macos_autostart(args: argparse.Namespace) -> tuple[bool, str, str]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    plist = Path.home() / "Library/LaunchAgents" / f"{AUTOSTART_LABEL}.plist"
    _atomic_write(plist, plistlib.dumps(launchd_configuration(service_command(args)),
                                       fmt=plistlib.FMT_XML, sort_keys=False))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{AUTOSTART_LABEL}"],
                   capture_output=True, text=True)
    subprocess.run(["launchctl", "enable", f"{domain}/{AUTOSTART_LABEL}"],
                   capture_output=True, text=True)
    result = subprocess.run(["launchctl", "bootstrap", domain, str(plist)],
                            capture_output=True, text=True)
    if result.returncode != 0:  # macOS 10.13 и старые окружения
        result = subprocess.run(["launchctl", "load", "-w", str(plist)],
                                capture_output=True, text=True)
    if result.returncode == 0:
        return True, "launchd", str(plist)
    error = (result.stderr or result.stdout or "launchctl отказал").strip()
    plist.unlink(missing_ok=True)
    return False, "launchd", error


def _systemd_available() -> tuple[bool, str]:
    if not shutil.which("systemctl"):
        return False, "systemctl не найден"
    probe = subprocess.run(["systemctl", "--user", "show-environment"],
                           capture_output=True, text=True)
    return probe.returncode == 0, (probe.stderr or probe.stdout).strip()


def _enable_linux_autostart(args: argparse.Namespace) -> tuple[bool, str, str]:
    command = service_command(args)
    unit = xdg_config_home() / "systemd/user" / AUTOSTART_UNIT
    xdg = xdg_config_home() / "autostart/printflow.desktop"
    available, reason = _systemd_available()
    if available:
        _atomic_write(unit, render_systemd_unit(command))
        reload_result = subprocess.run(["systemctl", "--user", "daemon-reload"],
                                       capture_output=True, text=True)
        enable_result = subprocess.run(
            ["systemctl", "--user", "enable", "--now", AUTOSTART_UNIT],
            capture_output=True, text=True)
        if reload_result.returncode == 0 and enable_result.returncode == 0:
            xdg.unlink(missing_ok=True)
            return True, "systemd-user", f"systemd --user: {AUTOSTART_UNIT}"
        reason = (enable_result.stderr or reload_result.stderr or
                  enable_result.stdout or reload_result.stdout).strip()
        subprocess.run(["systemctl", "--user", "disable", "--now", AUTOSTART_UNIT],
                       capture_output=True, text=True)
        unit.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)

    # Графические сессии без user systemd (часть WSL, контейнеров, старых дистрибутивов).
    try:
        _atomic_write(xdg, render_xdg_entry(command), mode=0o755)
    except OSError as exc:
        return False, "xdg-autostart", f"{reason}; XDG: {exc}"
    return True, "xdg-autostart", f"XDG Autostart: {xdg} ({reason or 'systemd недоступен'})"


def enable_autostart(args: argparse.Namespace) -> tuple[bool, str, str]:
    try:
        if IS_WINDOWS:
            success, mechanism, detail = _enable_windows_autostart(args)
        elif IS_MACOS:
            success, mechanism, detail = _enable_macos_autostart(args)
        else:
            success, mechanism, detail = _enable_linux_autostart(args)
        if success:
            try:
                _atomic_write(AUTOSTART_CONFIG,
                              json.dumps(_autostart_config(args, mechanism), ensure_ascii=False,
                                         indent=2) + "\n")
            except OSError as exc:
                # Не оставляем включённый сервис без метаданных для status/repair/uninstall.
                if IS_WINDOWS:
                    _disable_windows_autostart()
                elif IS_MACOS:
                    _disable_macos_autostart()
                else:
                    _disable_linux_autostart()
                return False, mechanism, f"не удалось сохранить настройки: {exc}"
        return success, mechanism, detail
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "unknown", str(exc)


def stop_autostart_runtime() -> tuple[bool, bool, str]:
    """Остановить управляемый экземпляр, не выключая запуск при следующем входе."""
    try:
        if IS_WINDOWS:
            query = run_powershell(
                f"$task = Get-ScheduledTask -TaskName {_powershell_literal(AUTOSTART_TASK)} "
                "-ErrorAction SilentlyContinue; if ($task) { $task.State }")
            if query.returncode != 0 or query.stdout.strip().lower() != "running":
                return False, True, ""
            result = run_powershell(
                f"Stop-ScheduledTask -TaskName {_powershell_literal(AUTOSTART_TASK)} "
                "-ErrorAction Stop")
            return True, result.returncode == 0, (result.stderr or result.stdout).strip()
        if IS_MACOS:
            plist = Path.home() / "Library/LaunchAgents" / f"{AUTOSTART_LABEL}.plist"
            if not plist.exists():
                return False, True, ""
            domain = f"gui/{os.getuid()}"
            loaded = subprocess.run(
                ["launchctl", "print", f"{domain}/{AUTOSTART_LABEL}"],
                capture_output=True, text=True).returncode == 0
            if not loaded:
                return False, True, ""
            result = subprocess.run(
                ["launchctl", "bootout", f"{domain}/{AUTOSTART_LABEL}"],
                capture_output=True, text=True)
            return True, result.returncode == 0, (result.stderr or result.stdout).strip()

        unit = xdg_config_home() / "systemd/user" / AUTOSTART_UNIT
        if not unit.exists() or not shutil.which("systemctl"):
            return False, True, ""
        active = subprocess.run(["systemctl", "--user", "is-active", "--quiet",
                                 AUTOSTART_UNIT], capture_output=True).returncode == 0
        if not active:
            return False, True, ""
        result = subprocess.run(["systemctl", "--user", "stop", AUTOSTART_UNIT],
                                capture_output=True, text=True)
        return True, result.returncode == 0, (result.stderr or result.stdout).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return True, False, str(exc)


def _disable_windows_autostart() -> tuple[bool, str]:
    startup = windows_startup_dir() / f"{APP_NAME}.lnk"
    startup.unlink(missing_ok=True)
    script = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$task = Get-ScheduledTask -TaskName {_powershell_literal(AUTOSTART_TASK)} "
        "-ErrorAction SilentlyContinue",
        "if ($null -ne $task) {",
        "  if ($task.State -eq 'Running') {",
        f"    Stop-ScheduledTask -TaskName {_powershell_literal(AUTOSTART_TASK)}",
        "  }",
        f"  Unregister-ScheduledTask -TaskName {_powershell_literal(AUTOSTART_TASK)} "
        "-Confirm:$false",
        "}",
    ])
    result = run_powershell(script)
    if (result.returncode == 127 and
            load_autostart_config().get("mechanism") == "windows-startup"):
        return True, ""
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def _disable_macos_autostart() -> tuple[bool, str]:
    plist = Path.home() / "Library/LaunchAgents" / f"{AUTOSTART_LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    loaded = subprocess.run(["launchctl", "print", f"{domain}/{AUTOSTART_LABEL}"],
                            capture_output=True, text=True).returncode == 0
    if loaded:
        result = subprocess.run(["launchctl", "bootout", f"{domain}/{AUTOSTART_LABEL}"],
                                capture_output=True, text=True)
        if result.returncode != 0 and plist.exists():
            result = subprocess.run(["launchctl", "unload", "-w", str(plist)],
                                    capture_output=True, text=True)
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "launchctl отказал").strip()
    plist.unlink(missing_ok=True)
    return True, ""


def _disable_linux_autostart() -> tuple[bool, str]:
    unit = xdg_config_home() / "systemd/user" / AUTOSTART_UNIT
    xdg = xdg_config_home() / "autostart/printflow.desktop"
    if unit.exists():
        if not shutil.which("systemctl"):
            return False, "unit найден, но systemctl недоступен; сервис мог остаться запущенным"
        result = subprocess.run(["systemctl", "--user", "disable", "--now", AUTOSTART_UNIT],
                                capture_output=True, text=True)
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "systemctl отказал").strip()
    unit.unlink(missing_ok=True)
    xdg.unlink(missing_ok=True)
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "reset-failed", AUTOSTART_UNIT],
                       capture_output=True)
    return True, ""


def disable_autostart() -> tuple[bool, str]:
    try:
        if IS_WINDOWS:
            success, detail = _disable_windows_autostart()
        elif IS_MACOS:
            success, detail = _disable_macos_autostart()
        else:
            success, detail = _disable_linux_autostart()
        if success:
            AUTOSTART_CONFIG.unlink(missing_ok=True)
        return success, detail
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def autostart_status() -> dict:
    config = load_autostart_config()
    config_detail = ""
    try:
        port = int(config.get("port", DEFAULT_PORT))
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        port = DEFAULT_PORT
        config_detail = "файл настроек повреждён: использую порт 8080"
    status = {
        "installed": False,
        "enabled": False,
        "running": bool(health(port)),
        "mechanism": config.get("mechanism", "—"),
        "port": port,
        "root_matches": not config or config.get("root") == str(ROOT),
        "detail": config_detail,
    }
    try:
        if IS_WINDOWS:
            script = (f"$task = Get-ScheduledTask -TaskName {_powershell_literal(AUTOSTART_TASK)} "
                      "-ErrorAction SilentlyContinue; if ($task) { $task.State }")
            result = run_powershell(script)
            startup = windows_startup_dir() / f"{APP_NAME}.lnk"
            task_state = result.stdout.strip()
            task = result.returncode == 0 and bool(task_state)
            task_enabled = task and task_state.lower() != "disabled"
            status.update(installed=task or startup.exists(),
                          enabled=task_enabled or startup.exists(),
                          mechanism="windows-task" if task else
                          ("windows-startup" if startup.exists() else "—"),
                          detail=task_state)
        elif IS_MACOS:
            plist = Path.home() / "Library/LaunchAgents" / f"{AUTOSTART_LABEL}.plist"
            loaded = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{AUTOSTART_LABEL}"],
                capture_output=True, text=True).returncode == 0
            status.update(installed=plist.exists(), enabled=plist.exists(),
                          mechanism="launchd" if plist.exists() else "—",
                          detail="загружен" if loaded else "будет загружен при следующем входе")
        else:
            unit = xdg_config_home() / "systemd/user" / AUTOSTART_UNIT
            xdg = xdg_config_home() / "autostart/printflow.desktop"
            if unit.exists() and shutil.which("systemctl"):
                enabled = subprocess.run(["systemctl", "--user", "is-enabled", "--quiet",
                                          AUTOSTART_UNIT], capture_output=True).returncode == 0
                active = subprocess.run(["systemctl", "--user", "is-active", "--quiet",
                                         AUTOSTART_UNIT], capture_output=True).returncode == 0
                status.update(installed=True, enabled=enabled, mechanism="systemd-user",
                              detail="сервис активен" if active else "сервис сейчас не активен")
            elif unit.exists():
                status.update(installed=True, enabled=False, mechanism="systemd-user",
                              detail="unit найден, но systemctl недоступен")
            elif xdg.exists():
                status.update(installed=True, enabled=True, mechanism="xdg-autostart",
                              detail="запускается графической сессией")
    except (OSError, subprocess.SubprocessError) as exc:
        status["detail"] = str(exc)
    if config_detail and config_detail not in status["detail"]:
        status["detail"] = "; ".join(filter(None, (config_detail, status["detail"])))
    return status


def _print_autostart_status(status: dict) -> None:
    printer = ok if status["installed"] and status["enabled"] else warn
    printer("Автозапуск включён" if status["enabled"] else "Автозапуск выключен")
    say(f"    Механизм: {status['mechanism']}")
    say(f"    Порт:     {status['port']}")
    say(f"    Сервер:   {'работает' if status['running'] else 'сейчас не запущен'}")
    if status["detail"]:
        say(f"    Система:  {status['detail']}", Style.DIM)
    if not status["root_matches"]:
        warn("PrintFlow перенесён в другую папку — выполните autostart repair")


def cmd_autostart(args: argparse.Namespace) -> int:
    action = args.autostart_action
    if action == "status":
        header("Системный автозапуск PrintFlow")
        status = autostart_status()
        _print_autostart_status(status)
        say()
        say("  Изменить: python pf.py autostart enable|disable|repair", Style.DIM)
        say()
        return 0 if status["enabled"] and status["root_matches"] else 1
    if action == "disable":
        header("Отключение автозапуска")
        success, detail = disable_autostart()
        if success:
            ok("Автозапуск отключён; ярлыки и данные сохранены")
            if detail:
                say(f"    {detail}", Style.DIM)
            return 0
        fail(f"Не удалось полностью отключить автозапуск: {detail}")
        return 1

    header("Восстановление автозапуска" if action == "repair" else "Включение автозапуска")
    if action == "repair":
        # repair предназначен прежде всего для переноса каталога/обновления launcher:
        # не сбрасываем ранее выбранные порт и сетевой режим на значения CLI по умолчанию.
        saved = load_autostart_config()
        try:
            saved_port = int(saved.get("port", args.port))
            saved_delay = int(saved.get("startup_delay", args.startup_delay))
            if 1 <= saved_port <= 65535:
                args.port = saved_port
            if 0 <= saved_delay <= 300:
                args.startup_delay = saved_delay
        except (TypeError, ValueError):
            warn("Сохранённые параметры повреждены — использую безопасные значения")
        for name in ("local", "system", "verbose"):
            if isinstance(saved.get(name), bool):
                setattr(args, name, saved[name])
    if not args.system:
        ensure_venv()
    success, _mechanism, detail = enable_autostart(args)
    if not success:
        fail(f"Автозапуск не установлен: {detail}")
        return 1
    ok(f"Автозапуск настроен: {detail}")
    _print_autostart_status(autostart_status())
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    header("Установка PrintFlow в систему", "ярлык, меню программ и автозапуск")
    if not args.system:
        ensure_venv()
    created: list[str] = []
    errors: list[str] = []
    if IS_WINDOWS:
        items, failures = install_windows(args)
    elif IS_MACOS:
        items, failures = install_macos(args)
    else:
        items, failures = install_linux(args)
    created += items
    errors += failures

    if args.no_autostart:
        success, detail = disable_autostart()
        if success:
            created.append("Автозапуск отключён (--no-autostart)")
        else:
            errors.append(f"автозапуск не отключён: {detail}")
    else:
        success, _mechanism, detail = enable_autostart(args)
        if success:
            created.append(f"Автозапуск: {detail}")
        else:
            errors.append(f"автозапуск: {detail}")

    say()
    for item in created:
        ok(item)
    for item in errors:
        fail(item)
    say()
    say("  Проверить: python pf.py autostart status", Style.DIM)
    say("  Удалить всё: python pf.py uninstall", Style.DIM)
    say("  Данные и база при удалении не трогаются.", Style.DIM)
    say()
    return 1 if errors else 0


def install_windows(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    done: list[str] = []
    errors: list[str] = []
    command = launcher_command("gui")
    shortcuts: dict[Path, str] = {}
    desktop = desktop_dir()
    if desktop:
        shortcuts[desktop / f"{APP_NAME}.lnk"] = "Ярлык на рабочем столе"
    start_menu = windows_programs_dir()
    shortcuts[start_menu / f"{APP_NAME}.lnk"] = "Пункт в меню «Пуск»"
    for path, label in shortcuts.items():
        success, detail = create_windows_shortcut(
            path, command, "PrintFlow — управление 3D-производством")
        if success:
            done.append(f"{label}: {path}")
        else:
            errors.append(f"не удалось создать {path}: {detail}")
    return done, errors


def install_macos(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    bundle = Path.home() / "Applications" / f"{APP_NAME}.app"
    macos_dir = bundle / "Contents/MacOS"
    try:
        macos_dir.mkdir(parents=True, exist_ok=True)
        info = {
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": "NOZZA PrintFlow",
            "CFBundleIdentifier": AUTOSTART_LABEL,
            "CFBundleVersion": app_version(),
            "CFBundleExecutable": APP_NAME,
            "CFBundlePackageType": "APPL",
            "LSMinimumSystemVersion": "10.13",
        }
        _atomic_write(bundle / "Contents/Info.plist",
                      plistlib.dumps(info, fmt=plistlib.FMT_XML, sort_keys=False))
        runner = macos_dir / APP_NAME
        # argv передаётся напрямую через exec; shell-экранирование обрабатывает пробелы/кавычки.
        import shlex
        run = " ".join(shlex.quote(part) for part in launcher_command("gui"))
        _atomic_write(runner, f"#!/bin/sh\ncd {shlex.quote(str(ROOT))}\nexec {run}\n", mode=0o755)
        return [f"Программа: {bundle}"], []
    except OSError as exc:
        return [], [f"не удалось создать {bundle}: {exc}"]


def install_linux(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    done: list[str] = []
    errors: list[str] = []
    entry = xdg_data_home() / "applications/printflow.desktop"
    try:
        _atomic_write(entry, render_xdg_entry(launcher_command("gui")), mode=0o755)
        done.append(f"Пункт в меню программ: {entry}")
    except OSError as exc:
        errors.append(f"не удалось создать {entry}: {exc}")
        return done, errors
    desktop = desktop_dir()
    if desktop:
        shortcut = desktop / "PrintFlow.desktop"
        try:
            shutil.copy2(entry, shortcut)
            shortcut.chmod(0o755)
            done.append(f"Ярлык на рабочем столе: {shortcut}")
        except OSError as exc:
            errors.append(f"не удалось создать {shortcut}: {exc}")
    return done, errors


def cmd_uninstall(args: argparse.Namespace) -> int:
    header("Удаление ярлыков и автозапуска", "база и настройки остаются на месте")
    removed: list[str] = []
    errors: list[str] = []
    disabled, detail = disable_autostart()
    if not disabled:
        errors.append(f"автозапуск: {detail}")

    desktop = desktop_dir() or Path.home()
    candidates = [
        desktop / f"{APP_NAME}.lnk",
        desktop / "PrintFlow.desktop",
        xdg_data_home() / "applications/printflow.desktop",
        windows_programs_dir() / f"{APP_NAME}.lnk",
    ]
    bundle = Path.home() / "Applications" / f"{APP_NAME}.app"
    if bundle.exists():
        try:
            shutil.rmtree(bundle)
            removed.append(str(bundle))
        except OSError as exc:
            errors.append(f"не удалось удалить {bundle}: {exc}")
    for path in candidates:
        if path.exists():
            try:
                path.unlink()
                removed.append(str(path))
            except OSError as exc:
                errors.append(f"не удалось удалить {path}: {exc}")
    say()
    for item in removed:
        ok(f"Удалено: {item}")
    if disabled:
        ok("Автозапуск отключён")
    for item in errors:
        fail(item)
    if not removed and not errors:
        warn("Ярлыки не найдены — повторное удаление безопасно")
    say()
    say(f"  Данные остались: {DATA_DIR}", Style.DIM)
    say(f"  Окружение осталось: {VENV_DIR}", Style.DIM)
    say()
    return 1 if errors else 0


# ─────────────────────────────────────────────────────────── окно управления
def cmd_gui(args: argparse.Namespace) -> int:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        warn("Графическая оболочка недоступна (нет модуля tkinter)")
        say("    Linux: sudo apt install python3-tk")
        say("    Открываю текстовое меню.")
        return cmd_menu(args)
    from launcher_window import run_window  # локальный модуль рядом с pf.py

    return run_window(args)


def cmd_app(args: argparse.Namespace) -> int:
    """Нативное окно как у 1С/Photoshop (pywebview)."""
    try:
        from connector.printflow.app_window import main as app_main
    except ImportError as e:
        fail(f"Не удалось загрузить нативное окно: {e}")
        say("    Установите pywebview: pip install pywebview")
        say("    Fallback — открываю в браузере…")
        return cmd_start(args)
    # пробуем pywebview, если нет — fallback в браузер
    try:
        import webview  # noqa: F401
    except ImportError:
        warn("pywebview не установлен — ставлю…")
        try:
            import subprocess, sys
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pywebview"], check=False)
            import webview  # noqa: F401
        except Exception:
            warn("Не удалось поставить pywebview — открываю в браузере")
            return cmd_start(args)
    argv = []
    argv += ["--port", str(args.port)]
    if args.local:
        argv += ["--local"]
    return app_main(argv)


def cmd_menu(args: argparse.Namespace) -> int:
    """Текстовое меню — запасной вариант, когда окно недоступно."""
    actions = [
        ("Запустить панель", lambda: cmd_start(args)),
        ("Состояние", lambda: cmd_status(args)),
        ("Остановить", lambda: cmd_stop(args)),
        ("Диагностика", lambda: cmd_doctor(args)),
        ("Резервная копия", lambda: cmd_backup(args)),
        ("Обновление", lambda: cmd_update(args)),
        ("Собрать автономную программу", lambda: cmd_build(args)),
        ("Установить ярлык и автозапуск", lambda: cmd_install(args)),
    ]
    while True:
        header(f"NOZZA · PrintFlow {app_version()}", "выберите действие")
        for index, (label, _) in enumerate(actions, 1):
            say(f"    {index}. {label}")
        say("    0. Выход")
        say()
        try:
            choice = input(Style.paint("  Номер: ", Style.CYAN)).strip()
        except (EOFError, KeyboardInterrupt):
            say()
            return 0
        if choice in ("0", "q", ""):
            return 0
        if choice.isdigit() and 1 <= int(choice) <= len(actions):
            actions[int(choice) - 1][1]()
            try:
                input(Style.paint("  Enter — вернуться в меню ", Style.DIM))
            except (EOFError, KeyboardInterrupt):
                return 0


# ──────────────────────────────────────────────────────────────────── помощь
HELP = """
  ЗАПУСК
    python pf.py                    запустить панель (видна в локальной сети)
    python pf.py app                нативное окно как у 1С/Photoshop (если установлен pywebview)
    python pf.py --local            только этот компьютер, без доступа по сети
    python pf.py --port 9000        другой порт
    python pf.py --background       запустить в фоне, консоль можно закрыть
    python pf.py gui                окно управления вместо консоли (tkinter)

  УПРАВЛЕНИЕ
    python pf.py status             работает ли сервер, на каком порту
    python pf.py stop               остановить фоновый сервер
    python pf.py logs               последние строки журнала

  ОБСЛУЖИВАНИЕ
    python pf.py doctor             диагностика: Python, база, сеть, принтеры
    python pf.py backup             резервная копия базы
    python pf.py restore            восстановить базу из копии
    python pf.py update             обновиться из репозитория (с копией базы)
    python pf.py deps               переустановить зависимости

  УСТАНОВКА
    python pf.py install            ярлык, меню программ, автозапуск
    python pf.py install --no-autostart    ярлыки и явное отключение автозапуска
    python pf.py autostart status   состояние системного автозапуска
    python pf.py autostart enable   включить системный автозапуск
    python pf.py autostart disable  отключить, сохранив ярлыки и данные
    python pf.py autostart repair   пересоздать конфигурацию после переноса папки
    python pf.py uninstall          убрать ярлыки и автозапуск (данные не трогаются)
    python pf.py build              автономная программа без Python (PyInstaller)
"""


def cmd_help(args: argparse.Namespace) -> int:
    header(f"NOZZA · PrintFlow {app_version()}", "локальная система 3D-производства")
    print(HELP)
    say(f"  Данные:     {DATA_DIR}", Style.DIM)
    say(f"  Окружение:  {VENV_DIR}", Style.DIM)
    say()
    return 0


def startup_delay_value(value: str) -> int:
    try:
        delay = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("задержка должна быть целым числом") from exc
    if not 0 <= delay <= 300:
        raise argparse.ArgumentTypeError("задержка должна быть от 0 до 300 секунд")
    return delay


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pf", add_help=False,
        description="PrintFlow — запуск и обслуживание локальной системы 3D-производства")
    parser.add_argument("command", nargs="?", default="start",
                        choices=["start", "service", "gui", "app", "menu", "stop", "status",
                                 "doctor", "backup", "restore", "update", "deps", "build",
                                 "install", "uninstall", "autostart", "logs", "help"])
    parser.add_argument("autostart_action", nargs="?", default="status",
                        choices=["status", "enable", "disable", "repair"],
                        help="действие для команды autostart")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="порт панели")
    parser.add_argument("--local", action="store_true",
                        help="слушать только 127.0.0.1 (без доступа с телефона)")
    parser.add_argument("--background", action="store_true", help="запуск в фоне")
    parser.add_argument("--auto-port", action="store_true",
                        help="занять следующий свободный порт, если основной занят")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    parser.add_argument("--no-qr", action="store_true", help="не рисовать QR-код")
    parser.add_argument("--no-autostart", action="store_true",
                        help="при install — отключить автозапуск, установить только ярлыки")
    parser.add_argument("--startup-delay", type=startup_delay_value, default=10,
                        help="задержка автозапуска в секундах (0–300, по умолчанию 10)")
    parser.add_argument("--system", action="store_true",
                        help="запускать текущим Python, без отдельного окружения")
    parser.add_argument("--verbose", action="store_true", help="подробный журнал")
    parser.add_argument("--lines", type=int, default=40, help="сколько строк журнала показать")
    parser.add_argument("--file", default="", help="путь к файлу копии для restore")
    parser.add_argument("-h", "--help", action="store_const", const=True, dest="want_help")
    return parser


COMMANDS = {
    "start": cmd_start,
    "service": cmd_service,
    "gui": cmd_gui,
    "app": cmd_app,
    "menu": cmd_menu,
    "stop": cmd_stop,
    "status": cmd_status,
    "doctor": cmd_doctor,
    "backup": cmd_backup,
    "restore": cmd_restore,
    "update": cmd_update,
    "deps": cmd_deps,
    "build": cmd_build,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "autostart": cmd_autostart,
    "logs": cmd_logs,
    "help": cmd_help,
}


def main(argv: list[str] | None = None) -> int:
    Style.setup()
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if getattr(args, "want_help", False):
        return cmd_help(args)
    if not ENTRYPOINT.exists():
        fail(f"Не найден {ENTRYPOINT}")
        say("    Запускайте pf.py из папки репозитория PrintFlow.")
        return 1
    try:
        return COMMANDS[args.command](args)
    except KeyboardInterrupt:
        say()
        say("  Прервано.", Style.DIM)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

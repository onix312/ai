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
import hashlib
import json
import os
import platform
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
            return (2, ip)
        if ip.startswith("169.254."):
            return (9, ip)
        return (3, ip)

    usable = [ip for ip in found if not ip.startswith("169.254.")] or list(found)
    return sorted(usable, key=rank)


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
        say(f"    Остановить: python pf.py stop", Style.DIM)
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
            fail(f"База не читается: {exc}")
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


def cmd_backup(args: argparse.Namespace) -> int:
    header("Резервная копия базы")
    if not DB_FILE.exists():
        warn("Базы ещё нет — копировать нечего")
        return 1
    import sqlite3

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"printflow-{stamp}.sqlite3"
    source = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)  # консистентно даже при работающем сервере
    finally:
        destination.close()
        source.close()
    ok(f"Копия готова: {target}")
    say(f"    Размер: {target.stat().st_size / 1048576:.1f} МБ")

    extra = sorted(BACKUP_DIR.glob("printflow-*.sqlite3"),
                   key=lambda p: p.stat().st_mtime)[:-BACKUP_KEEP]
    for old in extra:
        old.unlink(missing_ok=True)
    if extra:
        step(f"Удалил старых копий: {len(extra)} (держим последние {BACKUP_KEEP})")
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

    if DB_FILE.exists():
        safety = BACKUP_DIR / f"before-restore-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3"
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DB_FILE, safety)
        step(f"Текущая база сохранена: {safety.name}")
    if not ask(f"Заменить базу файлом {chosen.name}?", default=False):
        say("  Отменено.")
        return 1
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(chosen, DB_FILE)
    ok("База восстановлена. Запускайте: python pf.py")
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

    subprocess.run(["git", "fetch", "--quiet"], cwd=str(ROOT))
    behind = subprocess.run(["git", "rev-list", "--count", "HEAD..@{u}"],
                            cwd=str(ROOT), capture_output=True, text=True).stdout.strip()
    if behind in ("", "0"):
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
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT),
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        warn("Есть незакоммиченные правки — обновление их не тронет")
    result = subprocess.run(["git", "pull", "--ff-only"], cwd=str(ROOT))
    if result.returncode != 0:
        fail("Не удалось обновиться автоматически (расходятся ветки)")
        say("    Разберитесь вручную: git status")
        return 1
    ensure_venv(force_deps=True)
    ok(f"Обновлено до версии {app_version()}")
    if running_port():
        warn("Перезапустите PrintFlow, чтобы изменения вступили в силу:")
        say("    python pf.py stop && python pf.py")
    say()
    return 0


def cmd_deps(args: argparse.Namespace) -> int:
    header("Переустановка зависимостей")
    ensure_venv(force_deps=True)
    say()
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


# ────────────────────────────────────────────────────── установка в систему
def desktop_dir() -> Path | None:
    """Рабочий стол пользователя или None, если его нет (сервер, WSL, док)."""
    if IS_WINDOWS:
        candidate = Path(os.path.expanduser("~/Desktop"))
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
    python = sys.executable
    if IS_WINDOWS and mode == "gui":
        pythonw = Path(python).with_name("pythonw.exe")
        if pythonw.exists():
            python = str(pythonw)
    command = [python, str(ROOT / "pf.py"), mode]
    if background:
        command.append("--background")
    return command


def cmd_install(args: argparse.Namespace) -> int:
    header("Установка PrintFlow в систему", "ярлык, меню программ и автозапуск")
    ensure_venv()
    created: list[str] = []
    if IS_WINDOWS:
        created += install_windows(args)
    elif IS_MACOS:
        created += install_macos(args)
    else:
        created += install_linux(args)
    say()
    if created:
        for item in created:
            ok(item)
        say()
        say("  Удалить всё это: python pf.py uninstall", Style.DIM)
        say("  Данные и база при удалении не трогаются.", Style.DIM)
    else:
        warn("Ничего не установлено")
    say()
    return 0


def install_windows(args: argparse.Namespace) -> list[str]:
    done = []
    command = launcher_command("gui")
    target, arguments = command[0], subprocess.list2cmdline(command[1:])
    desktop = desktop_dir()
    shortcuts = {}
    if desktop:
        shortcuts[desktop / f"{APP_NAME}.lnk"] = "Ярлык на рабочем столе"
    start_menu = Path(os.environ.get("APPDATA", Path.home())) / \
        "Microsoft/Windows/Start Menu/Programs"
    shortcuts[start_menu / f"{APP_NAME}.lnk"] = "Пункт в меню «Пуск»"
    if not args.no_autostart:
        startup = start_menu / "Startup"
        shortcuts[startup / f"{APP_NAME}.lnk"] = "Автозапуск при входе в систему"

    for path, label in shortcuts.items():
        autostart = "Startup" in str(path)
        run = launcher_command("start", background=True) if autostart else command
        script = (
            "$shell = New-Object -ComObject WScript.Shell; "
            f"$link = $shell.CreateShortcut('{path}'); "
            f"$link.TargetPath = '{run[0]}'; "
            f"$link.Arguments = '{subprocess.list2cmdline(run[1:])}'; "
            f"$link.WorkingDirectory = '{ROOT}'; "
            f"$link.Description = 'PrintFlow — управление 3D-производством'; "
            "$link.Save()"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                capture_output=True, text=True)
        if result.returncode == 0 and path.exists():
            done.append(f"{label}: {path}")
        else:
            warn(f"Не удалось создать: {path}")
    return done


def install_macos(args: argparse.Namespace) -> list[str]:
    done = []
    apps = Path.home() / "Applications"
    bundle = apps / f"{APP_NAME}.app"
    macos_dir = bundle / "Contents/MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)
    (bundle / "Contents/Info.plist").write_text(textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0"><dict>
          <key>CFBundleName</key><string>{APP_NAME}</string>
          <key>CFBundleDisplayName</key><string>NOZZA PrintFlow</string>
          <key>CFBundleIdentifier</key><string>ru.nozza.printflow</string>
          <key>CFBundleVersion</key><string>{app_version()}</string>
          <key>CFBundleExecutable</key><string>{APP_NAME}</string>
          <key>CFBundlePackageType</key><string>APPL</string>
          <key>LSMinimumSystemVersion</key><string>10.13</string>
        </dict></plist>
        """), encoding="utf-8")
    runner = macos_dir / APP_NAME
    runner.write_text("#!/bin/bash\n"
                      f'cd "{ROOT}"\n'
                      f'exec "{sys.executable}" "{ROOT / "pf.py"}" gui\n', encoding="utf-8")
    runner.chmod(0o755)
    done.append(f"Программа: {bundle}")

    if not args.no_autostart:
        agents = Path.home() / "Library/LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        plist = agents / "ru.nozza.printflow.plist"
        command = launcher_command("start")
        arguments = "".join(f"    <string>{part}</string>\n" for part in command)
        plist.write_text(textwrap.dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
              "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
            <plist version="1.0"><dict>
              <key>Label</key><string>ru.nozza.printflow</string>
              <key>ProgramArguments</key><array>
            {arguments}  </array>
              <key>WorkingDirectory</key><string>{ROOT}</string>
              <key>RunAtLoad</key><true/>
              <key>StandardOutPath</key><string>{RUN_LOG}</string>
              <key>StandardErrorPath</key><string>{RUN_LOG}</string>
            </dict></plist>
            """), encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        subprocess.run(["launchctl", "load", str(plist)], capture_output=True)
        done.append(f"Автозапуск при входе: {plist}")
    return done


def install_linux(args: argparse.Namespace) -> list[str]:
    done = []
    apps = Path.home() / ".local/share/applications"
    apps.mkdir(parents=True, exist_ok=True)
    entry = apps / "printflow.desktop"
    command = " ".join(f'"{part}"' for part in launcher_command("gui"))
    entry.write_text(textwrap.dedent(f"""\
        [Desktop Entry]
        Type=Application
        Name=PrintFlow
        GenericName=Управление 3D-производством
        Comment=NOZZA · заказы, склад и принтеры Bambu Lab
        Exec={command}
        Path={ROOT}
        Terminal=false
        Categories=Office;Utility;
        """), encoding="utf-8")
    entry.chmod(0o755)
    done.append(f"Пункт в меню программ: {entry}")

    desktop = desktop_dir()
    if desktop:
        shortcut = desktop / "PrintFlow.desktop"
        try:
            shutil.copy2(entry, shortcut)
            shortcut.chmod(0o755)
            done.append(f"Ярлык на рабочем столе: {shortcut}")
        except OSError:
            pass

    if not args.no_autostart and shutil.which("systemctl"):
        units = Path.home() / ".config/systemd/user"
        units.mkdir(parents=True, exist_ok=True)
        unit = units / "printflow.service"
        exec_start = " ".join(f'"{part}"' for part in launcher_command("start"))
        unit.write_text(textwrap.dedent(f"""\
            [Unit]
            Description=PrintFlow — локальный сервер 3D-производства
            After=network-online.target

            [Service]
            Type=simple
            WorkingDirectory={ROOT}
            ExecStart={exec_start}
            Restart=on-failure
            RestartSec=10

            [Install]
            WantedBy=default.target
            """), encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        result = subprocess.run(["systemctl", "--user", "enable", "printflow.service"],
                                capture_output=True, text=True)
        if result.returncode == 0:
            done.append("Автозапуск при входе: systemd --user (printflow.service)")
            say("    Запустить прямо сейчас: systemctl --user start printflow", Style.DIM)
        else:
            warn("systemd отказал в автозапуске — ярлык всё равно работает")
    return done


def cmd_uninstall(args: argparse.Namespace) -> int:
    header("Удаление ярлыков и автозапуска", "база и настройки остаются на месте")
    removed = []
    desktop = desktop_dir() or Path.home()
    candidates = [
        desktop / f"{APP_NAME}.lnk",
        desktop / "PrintFlow.desktop",
        Path.home() / ".local/share/applications/printflow.desktop",
        Path.home() / "Library/LaunchAgents/ru.nozza.printflow.plist",
        Path(os.environ.get("APPDATA", Path.home())) /
        "Microsoft/Windows/Start Menu/Programs" / f"{APP_NAME}.lnk",
        Path(os.environ.get("APPDATA", Path.home())) /
        "Microsoft/Windows/Start Menu/Programs/Startup" / f"{APP_NAME}.lnk",
    ]
    plist = Path.home() / "Library/LaunchAgents/ru.nozza.printflow.plist"
    if plist.exists():
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    unit = Path.home() / ".config/systemd/user/printflow.service"
    if unit.exists():
        subprocess.run(["systemctl", "--user", "disable", "--now", "printflow.service"],
                       capture_output=True)
        candidates.append(unit)
    bundle = Path.home() / "Applications" / f"{APP_NAME}.app"
    if bundle.exists():
        shutil.rmtree(bundle, ignore_errors=True)
        removed.append(str(bundle))
    for path in candidates:
        if path.exists():
            try:
                path.unlink()
                removed.append(str(path))
            except OSError:
                warn(f"Не удалось удалить: {path}")
    say()
    for item in removed:
        ok(f"Удалено: {item}")
    if not removed:
        warn("Ярлыки не найдены — похоже, установка не выполнялась")
    say()
    say(f"  Данные остались: {DATA_DIR}", Style.DIM)
    say(f"  Окружение осталось: {VENV_DIR}", Style.DIM)
    say()
    return 0


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
    python pf.py --local            только этот компьютер, без доступа по сети
    python pf.py --port 9000        другой порт
    python pf.py --background       запустить в фоне, консоль можно закрыть
    python pf.py gui                окно управления вместо консоли

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
    python pf.py install --no-autostart    только ярлык
    python pf.py uninstall          убрать ярлыки (данные не трогаются)
    python pf.py build              автономная программа без Python (PyInstaller)
"""


def cmd_help(args: argparse.Namespace) -> int:
    header(f"NOZZA · PrintFlow {app_version()}", "локальная система 3D-производства")
    print(HELP)
    say(f"  Данные:     {DATA_DIR}", Style.DIM)
    say(f"  Окружение:  {VENV_DIR}", Style.DIM)
    say()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pf", add_help=False,
        description="PrintFlow — запуск и обслуживание локальной системы 3D-производства")
    parser.add_argument("command", nargs="?", default="start",
                        choices=["start", "gui", "menu", "stop", "status", "doctor",
                                 "backup", "restore", "update", "deps", "build",
                                 "install", "uninstall", "logs", "help"])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="порт панели")
    parser.add_argument("--local", action="store_true",
                        help="слушать только 127.0.0.1 (без доступа с телефона)")
    parser.add_argument("--background", action="store_true", help="запуск в фоне")
    parser.add_argument("--auto-port", action="store_true",
                        help="занять следующий свободный порт, если основной занят")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    parser.add_argument("--no-qr", action="store_true", help="не рисовать QR-код")
    parser.add_argument("--no-autostart", action="store_true",
                        help="при install — не добавлять автозапуск")
    parser.add_argument("--system", action="store_true",
                        help="запускать текущим Python, без отдельного окружения")
    parser.add_argument("--verbose", action="store_true", help="подробный журнал")
    parser.add_argument("--lines", type=int, default=40, help="сколько строк журнала показать")
    parser.add_argument("--file", default="", help="путь к файлу копии для restore")
    parser.add_argument("-h", "--help", action="store_const", const=True, dest="want_help")
    return parser


COMMANDS = {
    "start": cmd_start,
    "gui": cmd_gui,
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

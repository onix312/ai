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
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import time
import traceback
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
# Порт по умолчанию — 8765. Это тот же номер, что написан в подсказке экрана
# выбора сервера в мобильной кассе и в приложении пульта, и он же занят UDP-маяком
# автопоиска (`connector/printflow/discovery.py`). Пока лаунчер слушал 8080, а
# касса показывала 8765, «касса не подключается» была не ошибкой кассира, а
# расхождением двух чисел в одной системе.
DEFAULT_PORT = 8765
# Порты, на которых лаунчер узнаёт «свой» сервер: текущий, старый (8080 —
# установки до 17.0.27) и те, что встречаются в подсказках двух приложений.
PORT_CANDIDATES = (8765, 8080, 8766, 8790, 8000, 9000)
# UDP-порт маяка автопоиска: по нему телефон находит сервер, не зная адреса.
DISCOVERY_PORT = 8765
# Что открывать с телефона: путь → как это называется владельцу.
PHONE_PAGES = (("/", "панель владельца"),
               ("/cashier.html", "касса на телефоне"),
               ("/pult", "пульт цеха"))
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
        # pythonw.exe (ярлык с рабочего стола, автозапуск Windows) стартует без
        # консоли: sys.stdout там None. Раньше на этой строке main() падал с
        # AttributeError в первый же момент запуска — окно не появлялось вовсе,
        # а трассировку было некуда напечатать.
        cls.enabled = (sys.stdout is not None and sys.stdout.isatty()
                       and os.environ.get("NO_COLOR") is None)
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
    except (EOFError, KeyboardInterrupt, RuntimeError):
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


def ensure_crypto_prerequisites(python: Path, quiet: bool = False) -> None:
    """Автоустановка cryptography и pyOpenSSL при их отсутствии."""
    check = subprocess.run([str(python), "-c", "import cryptography"],
                           capture_output=True)
    if check.returncode == 0:
        return
    if not quiet:
        step("Установка библиотек безопасности (cryptography, OpenSSL)…")
    cmd = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-q",
           "cryptography>=41.0", "pyOpenSSL>=23.0"]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0:
        cmd_break = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                     "--break-system-packages", "cryptography>=41.0", "pyOpenSSL>=23.0"]
        subprocess.run(cmd_break, capture_output=True)
    if not quiet:
        ok("Библиотеки cryptography и OpenSSL готовы")


def interpreter(system: bool = False, quiet: bool = False) -> Path:
    """Python, которым запускать сервер: из окружения либо системный."""
    if system:
        py = Path(sys.executable)
        ensure_crypto_prerequisites(py, quiet=quiet)
        return py
    py = ensure_venv(quiet=quiet)
    ensure_crypto_prerequisites(py, quiet=quiet)
    return py


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
    """Найти порт работающего PrintFlow: сначала «свой», потом соседние.

    Порядок важен для владельца: если сервер поднят на порту из автозапуска,
    показывать надо его, а не первый свободный. 8080 в списке остаётся столько
    же, сколько живут установки, сделанные до 17.0.27: обновление лаунчера не
    должно «терять» работающую кассу.
    """
    for port in port_candidates():
        if port_busy(port) and health(port):
            return port
    return None


def port_candidates() -> list[int]:
    """Порты для поиска своего сервера — без повторов, в осмысленном порядке."""
    ordered: list[int] = []
    for port in (saved_port(), DEFAULT_PORT, *range(DEFAULT_PORT + 1, DEFAULT_PORT + 5),
                 *PORT_CANDIDATES):
        if port and 0 < port < 65536 and port not in ordered:
            ordered.append(port)
    return ordered


def saved_port() -> int | None:
    """Порт, записанный в конфигурации автозапуска (`pf.py install --port N`)."""
    try:
        port = int(load_autostart_config().get("port") or 0)
    except Exception:
        return None
    return port if 0 < port < 65536 else None


def resolve_port(requested: int | None = None) -> int:
    """Какой порт брать при запуске: явный → из автозапуска → обычный.

    Без этого `pf.py install --port 9000` и следующий `pf.py` поднимали сервер
    на разных портах: ярлык вёл на 9000, а консоль — на 8080. Теперь порт
    установки считается «своим», пока владелец не скажет иначе.
    """
    if requested:
        return int(requested)
    return saved_port() or DEFAULT_PORT


def port_owner(port: int) -> str:
    """«Кто занял порт» одной строкой: имя процесса и его номер.

    Нужно ровно в тот момент, когда владелец видит «порт занят другой
    программой» и не знает, что закрывать. Пустая строка — определить не удалось
    (нет прав, нет утилиты): тогда честно скажем «не удалось определить».
    """
    if IS_WINDOWS:
        try:
            listing = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                                     text=True, encoding="cp866", errors="replace",
                                     timeout=8).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
        pids = _pids_from_netstat(listing or "", port)
        for pid in pids:
            try:
                tasks = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                       capture_output=True, text=True, encoding="cp866",
                                       errors="replace", timeout=8).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            name = _name_from_tasklist(tasks or "")
            if name:
                return f"{name} (pid {pid})"
        return ""
    for command in (["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                    ["ss", "-ltnp", f"sport = :{port}"]):
        try:
            listing = subprocess.run(command, capture_output=True, text=True,
                                     errors="replace", timeout=8).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        owner = _owner_from_listing(listing or "")
        if owner:
            return owner
    return ""


def _pids_from_netstat(text: str, port: int) -> list[int]:
    """PID слушающих процессов по выводу `netstat -ano`. Разбор — отдельно от сети."""
    pids: list[int] = []
    marker = f":{port}"
    for line in text.splitlines():
        parts = line.split()
        # «TCP  0.0.0.0:8765  0.0.0.0:0  LISTENING  12345»
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if "LISTEN" not in parts[3].upper():
            continue
        local = parts[1]
        if not (local.endswith(marker) or f"{marker}]" in local):
            continue
        pid = parts[-1]
        if pid.isdigit() and int(pid) not in pids:
            pids.append(int(pid))
    unique: list[int] = []
    for pid in pids:
        if pid and pid not in unique:
            unique.append(pid)
    return unique


def _name_from_tasklist(text: str) -> str:
    """Имя процесса из вывода `tasklist /NH`: первая колонка строки."""
    for line in text.splitlines():
        fields = line.strip().split()
        if fields and fields[0].lower().endswith(".exe"):
            return fields[0]
    return ""


def _owner_from_listing(text: str) -> str:
    """«имя (pid N)» из вывода lsof или ss — для Linux и macOS.

    У lsof имя и pid стоят первыми колонками, у ss — в хвосте строки
    (`users:(("python3",pid=4321,fd=3))`), а первая колонка занята состоянием
    сокета. Общий разбор здесь не годится: «LISTEN (pid 4321)» бесполезнее, чем
    пустая строка, поэтому имя берём из кавычек, если они есть.
    """
    for line in text.splitlines():
        if not line.strip():
            continue
        quoted = re.search(r'"([^"]{1,64})"', line)
        pid = re.search(r"pid=(\d+)", line)
        if quoted is not None:
            name = quoted.group(1)
            if pid is not None:
                return f"{name} (pid {pid.group(1)})"
            continue
        fields = line.split()
        if len(fields) >= 2 and fields[1].isdigit() and not fields[0].isdigit():
            return f"{fields[0]} (pid {fields[1]})"
    return ""


def firewall_state(port: int) -> dict:
    """Разрешён ли порт во входящих правилах брандмауэра.

    Именно это правило (а не «Wi-Fi не тот») чаще всего объясняет «телефон не
    видит сервер». Проверка необязательная: если система не дала посмотреть
    правила, возвращаем ``known=False`` и молчим — пугать владельца нечем.
    """
    if IS_WINDOWS:
        fix = (f'netsh advfirewall firewall add rule name="PrintFlow {port}" '
               f'dir=in action=allow protocol=TCP localport={port}')
    elif IS_MACOS:
        fix = ("Системные настройки → Сеть → Брандмауэр → Параметры: "
               "разрешить входящие для Python")
    else:
        fix = (f"sudo ufw allow {port}/tcp   (или: "
               f"sudo firewall-cmd --add-port={port}/tcp --permanent)")
    if not IS_WINDOWS:
        return {"known": False, "allowed": None, "fix": fix}
    try:
        result = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule",
                                 "dir=in", "status=enabled"],
                                capture_output=True, text=True, encoding="cp866",
                                errors="replace", timeout=8)
    except (OSError, subprocess.SubprocessError):
        return {"known": False, "allowed": None, "fix": fix}
    allowed = firewall_allows(result.stdout or "", port)
    return {"known": allowed is not None, "allowed": allowed, "fix": fix}


def firewall_allows(text: str, port: int) -> bool | None:
    """Разбор вывода `netsh advfirewall firewall show rule`: есть ли разрешение.

    None — ни одного разбираемого правила (чужая локаль, другой файрвол):
    «неизвестно» честнее, чем «запрещено».
    """
    if not text.strip():
        return None
    seen = False
    allowed = False
    for block in re.split(r"\n\s*\n", text):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            fields[key.strip().lower()] = value.strip().lower()
        port_field = fields.get("localport") or fields.get("локальный порт")
        if port_field is None:
            continue
        seen = True
        enabled = fields.get("enabled") or fields.get("включено") or ""
        action = fields.get("action") or fields.get("действие") or ""
        protocol = fields.get("protocol") or fields.get("протокол") or ""
        if not enabled.startswith(("yes", "да", "true", "включ")):
            continue
        if not action.startswith(("allow", "разреш")):
            continue
        if protocol and not protocol.startswith(("tcp", "any", "любой")):
            continue
        if _port_field_matches(port_field, port):
            allowed = True
    return allowed if seen else None


def _port_field_matches(value: str, port: int) -> bool:
    """`Локальный порт: 8765`, `8000-9000, 8765` → попадает ли в них порт."""
    for chunk in value.replace(" ", "").split(","):
        if not chunk:
            continue
        if chunk.isdigit() and int(chunk) == port:
            return True
        if "-" in chunk:
            start, _, end = chunk.partition("-")
            if start.isdigit() and end.isdigit() and int(start) <= port <= int(end):
                return True
    return False


# ─────────────────────────────────────────────────────────────────── баннер
def qr_lines(url: str) -> list[str]:
    """QR-код ссылки: навёл телефон — панель открылась."""
    try:
        sys.path.insert(0, str(CONNECTOR))
        from printflow import qrgen  # локальный модуль, без зависимостей

        return qrgen.terminal(url, border=2).splitlines()
    except Exception:
        return []


def banner(host: str, port: int, ips: list[str], show_qr: bool = True,
           *, report: dict | None = None) -> None:
    """Что видит владелец при запуске: адреса, телефон, куда смотреть дальше.

    Баннер — единственное место, где владелец узнаёт адрес для телефона, поэтому
    здесь три вещи, а не одна: панель на этом компьютере, страницы для телефона
    (касса и пульт) и напоминание, что приложение находит сервер само. Строки
    «данные», «журнал» и «остановить» стоят слева от QR-кода — так они не
    разъезжаются по экрану и не мешают коду.
    """
    version = app_version()
    report = report or {}
    say()
    say("  " + rule("━"), Style.MAGENTA)
    say(f"  NOZZA · PrintFlow {version}", Style.BOLD, Style.MAGENTA)
    say("  Управление 3D-производством · Bambu Lab + AMS", Style.DIM)
    say("  " + rule("━"), Style.MAGENTA)
    say()

    local_url = f"http://localhost:{port}/"
    lan = host in ("0.0.0.0", "::")
    urls = report.get("urls") or phone_urls(port, ips)
    phone_url = urls.get("kassa") or f"http://localhost:{port}/"

    lines = [f"  {Style.paint('Панель на этом компьютере', Style.BOLD)}",
             f"    {Style.paint(local_url, Style.CYAN)}"]
    if lan and ips:
        lines.append("")
        lines.append(f"  {Style.paint('Телефон и планшет в той же Wi-Fi сети', Style.BOLD)}")
        lines.append(f"    касса  {Style.paint(urls['kassa'], Style.CYAN)}")
        lines.append(f"    пульт  {Style.paint(urls['pult'], Style.CYAN)}")
        if len(ips) > 1:
            extra = ", ".join(ips[1:])
            lines.append(f"    {Style.paint(f'другие адреса: {extra}', Style.DIM)}")
        lines.append("")
        lines.append(f"    {Style.paint('Адрес вводить не нужно:', Style.DIM)} в приложении "
                     f"кассы есть «Найти сервер в сети»")
        lines.append(f"    {Style.paint(f'Сервер объявляет себя по UDP {DISCOVERY_PORT}', Style.DIM)}")
        apk = report.get("apk") or {}
        if apk.get("available"):
            apk_url = f"http://{ips[0]}{apk['url']}"
            apk_note = f"v{apk.get('version') or '?'} · {apk.get('size_mb', 0)} МБ"
            lines.append(f"    приложение: {Style.paint(apk_url, Style.CYAN)}"
                         f"  {Style.paint(apk_note, Style.DIM)}")
        else:
            lines.append("    " + Style.paint(
                "приложения кассы на сервере нет — собрать: ./scripts/android-build.sh",
                Style.DIM))
    elif lan:
        lines.append("")
        lines.append(f"  {Style.paint('Сеть не определилась — телефон не подключится', Style.YELLOW)}")
        lines.append(f"    {Style.paint('подключите Wi-Fi или кабель и запустите снова', Style.DIM)}")
    else:
        lines.append("")
        lines.append(f"  {Style.paint('Режим «только этот компьютер»', Style.YELLOW)}")
        lines.append(f"    {Style.paint('по сети зайти нельзя — запустите без --local', Style.DIM)}")
    lines.append("")
    lines.append(f"  {Style.paint('Данные:', Style.DIM)}     {DATA_DIR}")
    lines.append(f"  {Style.paint('Журнал:', Style.DIM)}     {LOG_FILE}")
    lines.append(f"  {Style.paint('Остановить:', Style.DIM)} Ctrl+C"
                 f"   {Style.paint('(или закрыть окно)', Style.DIM)}")
    lines.append(f"  {Style.paint('Адрес ещё раз:', Style.DIM)} python pf.py net")

    art = qr_lines(phone_url) if (show_qr and ips and lan) else []
    if art:
        pad = max(len(strip_ansi(line)) for line in lines)
        for index in range(max(len(art), len(lines))):
            left = lines[index] if index < len(lines) else ""
            visible = len(strip_ansi(left))
            right = art[index] if index < len(art) else ""
            print(left + " " * max(2, pad - visible + 2) + right, flush=True)
        say()
        say(f"  Камера телефона → касса: {Style.paint(phone_url, Style.CYAN)}", Style.DIM)
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


# ─────────────────────────────────────────── телефон: адреса, поиск, файрвол
def phone_urls(port: int, ips: list[str] | None = None) -> dict:
    """Адреса для телефона: панель, касса, пульт — по каждому сетевому IP."""
    addresses = list(ips if ips is not None else local_ips())
    return {
        "ips": addresses,
        "primary": addresses[0] if addresses else "",
        "manual": f"{addresses[0]}:{port}" if addresses else "",
        "panel": f"http://{addresses[0]}:{port}/" if addresses else "",
        "kassa": f"http://{addresses[0]}:{port}/cashier.html" if addresses else "",
        "pult": f"http://{addresses[0]}:{port}/pult" if addresses else "",
        "by_ip": {ip: {path: f"http://{ip}:{port}{path}" for path, _ in PHONE_PAGES}
                  for ip in addresses},
    }


def apk_info(target: str = "kassa") -> dict:
    """Что лежит на раздачу телефону: APK кассы из `site/app` (или пусто).

    Импорт пакета коннектора — как в `qr_lines`: только когда он реально нужен,
    чтобы лаунчер оставался работоспособен и без окружения.
    """
    empty = {"available": False, "url": "", "file": "", "size_mb": 0.0,
             "size_bytes": 0, "version": ""}
    try:
        sys.path.insert(0, str(CONNECTOR))
        from printflow import app_shell

        return dict(empty, **app_shell.status(target=target))
    except Exception:
        return empty


def auto_discovery(timeout: float = 0.9) -> list[dict]:
    """Проверка автопоиска вживую: спросить «кто здесь?» и посмотреть ответы.

    Так владелец видит на ПК ровно то, что увидит телефон нажатием кнопки
    «Найти сервер в сети», — вместо надежды «должно работать».
    """
    try:
        sys.path.insert(0, str(CONNECTOR))
        from printflow import discovery

        return discovery.probe(port=DISCOVERY_PORT, timeout=timeout)
    except Exception:
        return []


def json_requested(args: argparse.Namespace) -> bool:
    """Просили ли `--json`. Одна проверка на все команды: имя атрибута argparse
    зависит от того, как объявлен флаг, и `getattr` здесь дешевле, чем шишка."""
    return bool(getattr(args, "json", None))


def discovery_bases(hits: list[dict]) -> list[str]:
    """Адреса из ответов маяка — по одному на порт, без «проверочных» адресов.

    Когда `pf.py` сам спрашивает «кто здесь?», он отправляет запрос и на
    127.0.0.1, и в сеть: без фильтра в ответе оказывались оба адреса, и владелец
    видел «найдено: 127.0.0.1, 169.254.0.21» вместо своего 192.168.x.x. Телефону
    нужен адрес в локальной сети, поэтому loopback и APIPA отбрасываем, если
    есть что-то лучше.
    """
    usable = [hit for hit in hits
              if hit.get("base") and not hit["base"].startswith("http://127.")
              and not hit["base"].startswith("http://169.254.")]
    chosen = usable or list(hits)
    seen: dict[int, str] = {}
    for hit in chosen:
        port = int(hit.get("port") or 0)
        seen.setdefault(port, str(hit.get("base") or ""))
    return [base for base in seen.values() if base]


def net_report(port: int | None = None, *, deep: bool = True) -> dict:
    """Всё, что нужно, чтобы телефон нашёл этот сервер. Готово и к `--json`.

    Один сборщик данных на баннер запуска, команду `net`, `status` и `doctor`:
    иначе они начинают расходиться в мелочах («в баннере адрес один, в
    диагностике другой»), а именно по этому выводу владелец и настраивает кассу.
    """
    running = running_port()
    port = int(port or running or resolve_port(None))
    ips = local_ips()
    urls = phone_urls(port, ips)
    active = health(port) if port else None
    # Достижимость по сетевому адресу проверяем сразу: /api/health отвечает и
    # тогда, когда сервер слушает только 127.0.0.1, а телефон в этом случае не
    # подключится никогда. Свой же LAN-IP — честная проверка «как у телефона».
    lan_reachable = bool(ips and active and any(
        probe_tcp(ip, port, timeout=1.2) for ip in ips))
    report = {
        "port": port,
        "running": bool(active),
        "version": str((active or {}).get("version") or ""),
        "lan_reachable": lan_reachable,
        "local_only": bool(active and ips and not lan_reachable),
        "urls": urls,
        "apk": apk_info("kassa"),
        "apk_pult": apk_info("pult"),
        "discovery": [],
        "checks": [],
    }
    checks = report["checks"]

    def add(state: str, text: str, hint: str = "") -> None:
        checks.append({"state": state, "text": text, "hint": hint})

    if not ips:
        add("bad", "У компьютера нет сетевого адреса",
            "Подключите Wi-Fi или кабель: телефон входит по локальной сети.")
    else:
        add("ok", f"Адреса компьютера: {', '.join(ips)}")
    if active:
        add("ok", f"Сервер отвечает на порту {port} (версия {report['version']})")
        if report["local_only"]:
            add("bad", "Сервер слушает только этот компьютер — телефон не подключится",
                "Запустите без --local: python pf.py")
    elif port_busy(port):
        owner = port_owner(port)
        add("bad", f"Порт {port} занят чужой программой" + (f": {owner}" if owner else ""),
            f"Возьмите свободный: python pf.py --port {free_port(port + 1)}")
    else:
        add("warn", f"Сервер не запущен (порт {port} свободен)",
            "Запуск: python pf.py")

    # Достижимость по сетевому адресу: проверяем свой же LAN-IP, а не localhost.
    # Если тут не отвечает — привязка к localhost или режет файрвол, и никакие
    # адреса в приложении уже не помогут.
    if ips and active:
        add("ok" if lan_reachable else "bad",
            f"Порт {port} отвечает по сетевому адресу {urls['primary']}"
            if lan_reachable else
            f"Порт {port} не отвечает по сетевому адресу {urls['primary']}",
            "" if lan_reachable else
            "Запустите без --local и разрешите порт в брандмауэре")

    if deep and active:
        report["discovery"] = auto_discovery()
        if report["discovery"]:
            found = ", ".join(discovery_bases(report["discovery"]))
            add("ok", f"Автопоиск работает (UDP {DISCOVERY_PORT}): {found}",
                "В приложении кассы достаточно «Найти сервер в сети».")
        else:
            add("warn", f"Автопоиск (UDP {DISCOVERY_PORT}) не ответил",
                "Приложению придётся перебирать адреса или вводить их вручную; "
                f"разрешите UDP {DISCOVERY_PORT} в брандмауэре.")
        report["firewall"] = firewall_state(port)
        if report["firewall"].get("allowed") is True:
            add("ok", f"В брандмауэре есть разрешение для порта {port}")
        elif report["firewall"].get("allowed") is False:
            add("bad", f"В брандмауэре нет разрешения для порта {port}",
                str(report["firewall"].get("fix") or ""))
    elif deep:
        # Сервер не отвечает — правила брандмауэра всё равно показываем: именно
        # с них начинается «телефон не видит ПК», когда сервер ещё не поднят.
        report["firewall"] = {"known": False, "allowed": None,
                              "fix": str(firewall_state(port).get("fix") or "")}
    return report


def print_net_report(report: dict, show_qr: bool = True) -> None:
    """Человеческий вывод отчёта: «что нажать на телефоне» и что проверить."""
    port = report["port"]
    urls = report["urls"]
    running = report["running"]
    say()
    if running:
        ok(f"Сервер работает: порт {port}"
           + (f", версия {report['version']}" if report["version"] else ""))
    else:
        warn(f"Сервер сейчас не запущен (порт {port} закреплён за PrintFlow)")
    say()

    if urls["primary"]:
        say("  Что открыть с телефона в той же Wi-Fi сети", Style.BOLD)
        for ip in urls["ips"]:
            say(f"    {Style.paint(ip, Style.BOLD)}")
            for path, title in PHONE_PAGES:
                url = urls["by_ip"][ip][path]
                say(f"      {title:<18} {Style.paint(url, Style.CYAN)}")
        say()
        say("  Адрес для приложения кассы", Style.BOLD)
        say(f"    {Style.paint(urls['manual'], Style.CYAN)}"
            f"   {Style.paint('(или нажать «Найти сервер в сети»)', Style.DIM)}")
        say()
        apk = report["apk"]
        if apk.get("available"):
            version = str(apk.get("version") or "?")
            apk_url = f"http://{urls['primary']}{apk['url']}"
            size = f"({apk.get('size_mb', 0)} МБ)"
            say(f"  Приложение кассы v{version}: {Style.paint(apk_url, Style.CYAN)}"
                f"  {Style.paint(size, Style.DIM)}")
        else:
            say("  Приложения кассы на сервере нет — собрать на ПК: "
                "./scripts/android-build.sh", Style.DIM)
        say()
    else:
        warn("Сетевого адреса нет: телефон подключить не к чему")
        say("    Подключите компьютер к Wi-Fi или кабелю и повторите.")
        say()

    say("  Проверки", Style.BOLD)
    marks = {"ok": ("✓", Style.GREEN), "warn": ("⚠", Style.YELLOW),
             "bad": ("✗", Style.RED)}
    for check in report.get("checks", []):
        mark, color = marks.get(check["state"], ("•", Style.DIM))
        say(f"   {Style.paint(mark, color)} {check['text']}")
        if check.get("hint"):
            say(f"     {Style.paint(check['hint'], Style.DIM)}")
    say()

    if show_qr and urls["primary"] and running:
        target = urls["kassa"] if report["discovery"] or not report["local_only"] else urls["panel"]
        art = qr_lines(target)
        if art:
            say(f"  Наведите камеру телефона: {Style.paint(target, Style.CYAN)}")
            for line in art:
                print(line, flush=True)
            say()
    say(f"  {Style.paint('Адрес ещё раз:', Style.DIM)} python pf.py net")
    if running and report.get("firewall", {}).get("allowed") is False:
        say(f"  {Style.paint('Разрешить порт:', Style.DIM)} "
            f"{report['firewall']['fix']}")
    say()



# ──────────────────────────────────────────────────────────────── команды
def open_in_browser(url: str) -> bool:
    """Открыть адрес в браузере и честно ответить, получилось ли.

    Раньше результат `webbrowser.open()` никого не интересовал: при сбое
    («браузер по умолчанию не задан») лаунчер молча завершался с кодом 0 —
    владелец видел только мелькнувшее чёрное окно.
    """
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def cmd_start(args: argparse.Namespace) -> int:
    """Запустить сервер и показать владельцу, что с этим делать.

    Три вещи, ради которых команда переписана: порт берётся из установки
    (``resolve_port``), а не из «привычного» числа; при занятом порту сразу
    видно, кто его занял («порт занят» без имени процесса бесполезно); живой
    PrintFlow на другом порту — это тот же сервер, а не повод поднять второй
    экземпляр в ту же базу (``--force`` отключает проверку осознанно).
    """
    check_python_version()
    host = "127.0.0.1" if args.local else "0.0.0.0"
    port = resolve_port(args.port)

    # Живой PrintFlow ищем ДО занятия порта: если сервер уже работает на другом
    # порту (старая установка на 8080, запуск из ярлыка с --port), второй
    # экземпляр писал бы в ту же базу — это удвоенные задания и порча учёта.
    already = running_port()
    if already is not None and already != port and not getattr(args, "force", False):
        version = str((health(already) or {}).get("version") or "").strip()
        title = f"PrintFlow {version}" if version else "PrintFlow"
        say()
        ok(f"{title} уже работает на порту {already}")
        say("    Второй сервер в ту же базу запускать нельзя.", Style.DIM)
        say("    Остановить: python pf.py stop   ·   Другой порт: python pf.py --port N",
            Style.DIM)
        say_phone_hint(already, host)
        if not args.no_browser and not open_in_browser(f"http://localhost:{already}/"):
            warn("Браузер не открылся — наберите адрес из списка выше вручную")
            hold_console()
            return 1
        return 0

    if port_busy(port):
        alive = health(port)
        if alive:
            running = running_port() or port
            say()
            ok(f"PrintFlow {alive.get('version', '')} уже работает на порту {running}")
            say_phone_hint(running, host)
            if not args.no_browser and not open_in_browser(f"http://localhost:{running}/"):
                warn("Браузер не открылся — наберите адрес из списка выше вручную")
                hold_console()
                return 1
            return 0
        owner = port_owner(port)
        if args.auto_port:
            port = free_port(port + 1)
            warn(f"Порт {args.port} занят" + (f" ({owner})" if owner else "")
                 + f" другой программой — беру {port}")
        else:
            fail(f"Порт {port} занят" + (f": {owner}" if owner else " другой программой"))
            if not owner:
                say("    Определить занявший процесс не удалось (нет прав?)", Style.DIM)
            say("    Свободный порт:  python pf.py --auto-port")
            say(f"    Или вручную:     python pf.py --port {free_port(port + 1)}")
            say("    Другой PrintFlow ищется сам: python pf.py status", Style.DIM)
            return 1

    if port < 1024 and not IS_WINDOWS:
        warn(f"Порт {port} меньше 1024 — на Linux/macOS нужны права администратора")

    python = interpreter(args.system)
    command = [str(python), str(ENTRYPOINT), "--host", host, "--port", str(port),
               "--no-browser", "--no-banner"]
    if args.verbose:
        command.append("--verbose")

    if args.background:
        return start_background(command, host, port, args)

    ips = local_ips() if host == "0.0.0.0" else []
    # Баннер показывает то же, что и `pf.py net`: адреса для телефона и наличие
    # APK. Собираем отчёт без глубоких проверок (они ждут файрвола и UDP), а
    # файрвол спрашиваем один раз — он и есть причина «телефон не видит ПК».
    report = {"urls": phone_urls(port, ips), "apk": apk_info("kassa"),
              "firewall": firewall_state(port)}
    banner(host, port, ips, show_qr=not args.no_qr, report=report)
    if ips and report["firewall"].get("allowed") is False:
        hint = report["firewall"]["fix"]
        say(f"  {Style.paint('Брандмауэр:', Style.YELLOW)} порт {port} не разрешён — "
            f"телефон может не подключиться")
        say(f"    {hint}", Style.DIM)
        say()

    process = subprocess.Popen(command, cwd=str(ROOT))
    write_pid(process.pid)
    if not args.no_browser:
        if not wait_for_server(port):
            warn("Сервер не ответил вовремя — открываю панель, она может быть пустой")
            say("    Если страница пустая: python pf.py logs", Style.DIM)
        if not open_in_browser(f"http://localhost:{port}/"):
            # Сервер уже работает, поэтому это не провал запуска, а только
            # подсказка: иначе владелец решит, что «PrintFlow не открылся».
            warn("Браузер не открылся — наберите http://localhost:"
                 f"{port}/ вручную")
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


def say_phone_hint(port: int, host: str = "0.0.0.0") -> None:
    """Короткая шпаргалка после старта: панель, касса и адрес для телефона."""
    say(f"    Панель:     http://localhost:{port}/")
    if host in ("0.0.0.0", "::"):
        for ip in local_ips():
            say(f"    Касса:      http://{ip}:{port}/cashier.html")
            say(f"    Пульт:      http://{ip}:{port}/pult")
            manual = f"{ip}:{port}"
            note = f"Адрес для приложения: {manual} (или «Найти сервер в сети»)"
            say(f"    {Style.paint(note, Style.DIM)}")
    say(f"    {Style.paint('Подробнее о телефоне: python pf.py net', Style.DIM)}")


def cmd_net(args: argparse.Namespace) -> int:
    """«Как подключить телефон» — адреса, автопоиск, файрвол, APK.

    Отдельная команда нужна потому, что адрес сервера требуется не один раз:
    телефон купили позже, роутер выдал новый IP, кассир переустановил приложение.
    Печатать для этого окно запуска заново — плохой совет (на нём висит сервер).
    """
    report = net_report(resolve_port(args.port))
    if json_requested(args):
        # Только JSON и ничего больше: вывод читают скрипты и панель, а
        # заголовок с рамкой ломает разбор.
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return 0 if (report["running"] and report["urls"]["primary"]) else 1
    header("Телефон и планшет", "адреса, автопоиск, брандмауэр")
    print_net_report(report, show_qr=not args.no_qr)
    return 0 if (report["running"] and report["urls"]["primary"]) else 1


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
    """Что происходит сейчас: версия, порт, адреса, данные, автопоиск.

    Раньше команда отвечала «работает / не работает». Этого мало: владелец
    приходит сюда, когда «телефон не видит кассу», — значит в ответе должны быть
    адреса для телефона и результат автопоиска, а не только номер порта.
    """
    if json_requested(args):
        report = net_report(resolve_port(args.port))
        pid = read_pid()
        if pid:
            report["pid"] = pid
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return 0 if report["running"] else 1

    header("Состояние PrintFlow")
    port = running_port()
    if port is None:
        warn("Сервер не запущен")
        say("    Запуск:            python pf.py")
        say("    Порт из установки: " + str(resolve_port(args.port)), Style.DIM)
        say("    Автозапуск:        python pf.py install", Style.DIM)
        say()
        return 1
    info = health(port) or {}
    uptime = int(info.get("uptime") or 0)
    ok(f"Работает: версия {info.get('version', '?')}, порт {port}")
    days, rest = divmod(uptime, 86400)
    hours, rest = divmod(rest, 3600)
    parts = []
    if days:
        parts.append(f"{days} дн")
    if hours or days:
        parts.append(f"{hours} ч")
    parts.append(f"{rest // 60} мин")
    say(f"    Аптайм:     {' '.join(parts)}")
    say(f"    Панель:     http://localhost:{port}/")
    for ip in local_ips():
        say(f"    Касса:      http://{ip}:{port}/cashier.html")
        say(f"    Пульт:      http://{ip}:{port}/pult")
    pid = read_pid()
    if pid:
        say(f"    Процесс:    pid {pid}")
    else:
        say("    Процесс:    запущен не через pf.py (остановить — закрыть его окно)",
            Style.DIM)
    if DB_FILE.exists():
        say(f"    База:       {DB_FILE.stat().st_size / 1048576:.1f} МБ — {DB_FILE}")
    say(f"    Данные:     {DATA_DIR}")
    saved = saved_port()
    if saved and saved != port:
        say(f"    Автозапуск настроен на порт {saved} — не совпадает с текущим",
            Style.YELLOW)
    found = auto_discovery(timeout=0.6)
    if found:
        bases = ", ".join(discovery_bases(found)) or found[0]["base"]
        say(f"    Автопоиск:  работает — телефон найдёт сервер сам ({bases})")
    else:
        say("    Автопоиск:  не ответил (UDP " + str(DISCOVERY_PORT)
            + ") — телефону придётся ввести адрес вручную", Style.YELLOW)
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
    say("  Сеть и мобильная касса", Style.BOLD)
    report = net_report(resolve_port(args.port))
    for check in report["checks"]:
        state = check["state"]
        (ok if state == "ok" else warn if state == "warn" else fail)(check["text"])
        if check.get("hint"):
            say(f"      {check['hint']}", Style.DIM)
        if state == "bad":
            problems += 1
    firewall = report.get("firewall") or {}
    if firewall.get("allowed") is False:
        fail(f"Брандмауэр не разрешает порт {report['port']} на вход")
        say(f"      {firewall['fix']}")
        problems += 1
    elif firewall.get("allowed") is True:
        ok(f"Брандмауэр: порт {report['port']} разрешён")
    apk = report.get("apk") or {}
    if apk.get("available"):
        version = apk.get("version") or "?"
        ok(f"Приложение кассы на раздачу: v{version} · {apk.get('size_mb')} МБ")
    else:
        warn("Сборки APK нет — телефону ставить нечего "
             "(./scripts/android-build.sh на ПК)")
    if report["urls"]["primary"]:
        say(f"      Адрес для приложения: {report['urls']['manual']}", Style.DIM)
        say(f"      Страница кассы:       {report['urls']['kassa']}", Style.DIM)
        say("      Если касса не находит сервер: телефон и ПК должны быть в одной "
            "сети, а гостевая Wi-Fi-сеть (изоляция клиентов) — не подходит.", Style.DIM)

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

    # Автозапуск — предупреждения, не ошибки: работать можно и без него.
    say()
    say("  Автозапуск", Style.BOLD)
    try:
        autostart_problems = autostart_verify()
        status = autostart_status()
        if status["installed"] and status["enabled"]:
            ok(f"Механизм: {status['mechanism']} · порт {status['port']}"
               + (" · сервер работает" if status["running"] else " · ждёт входа/старта"))
        if not autostart_problems:
            if not status["installed"]:
                say("      Не установлен — включить: python pf.py install", Style.DIM)
        else:
            for problem in autostart_problems:
                warn(problem)
    except Exception as exc:  # диагностика не должна падать на экзотических ОС
        warn(f"Не удалось проверить автозапуск: {exc}")

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


def _connector_command(args: argparse.Namespace) -> list[str]:
    """Команда запуска коннектора для автозапуска (без exec-специфики)."""
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
    return command


def cmd_service(args: argparse.Namespace) -> int:
    """Внутренний foreground-режим для systemd, launchd и Планировщика.

    После подготовки окружения процесс заменяется коннектором через exec: ОС
    отслеживает реальный сервер, а не промежуточный launcher и не двойной daemon.
    Режим ``--watchdog`` — супервизор для механизмов без собственного контроля
    (XDG Autostart, папка Startup): коннектор запускается дочерним процессом
    и поднимается обратно при падении, с нарастающей паузой между попытками.
    """
    check_python_version()
    delay = max(0, min(int(args.startup_delay), 300))
    if delay:
        time.sleep(delay)
    if getattr(args, "watchdog", False):
        return _service_watchdog(args)
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


def _service_watchdog(args: argparse.Namespace) -> int:
    """Супервизор для XDG Autostart и папки Startup: перезапуск при падении.

    systemd, launchd и Планировщик заданий Windows сами перезапускают службу.
    Графическая автозагрузка таких гарантий не даёт — поэтому здесь коннектор
    работает дочерним процессом: упал → пауза (растёт до 10 минут) → снова
    старт. Панель, поднятая вручную, не трогается: если порт уже отвечает,
    watchdog молчит и ждёт.
    """
    import signal

    state = {"stop": False}
    child: subprocess.Popen | None = None

    def _terminate(_signum, _frame) -> None:
        state["stop"] = True
        if child is not None and child.poll() is None:
            child.terminate()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _terminate)
        except (ValueError, OSError):
            pass

    command = _connector_command(args)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chdir(ROOT)
    write_pid(os.getpid())
    backoff = 15
    try:
        while not state["stop"]:
            if health(args.port):
                time.sleep(30)  # панель уже работает (вручную или копией)
                continue
            if port_busy(args.port):
                fail(f"Автозапуск: порт {args.port} занят другой программой — жду")
                time.sleep(60)
                continue
            log = open(RUN_LOG, "ab", buffering=0)
            log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} watchdog:"
                      f" {' '.join(command)}\n".encode("utf-8"))
            popen_kwargs: dict = {}
            if IS_WINDOWS:
                popen_kwargs["creationflags"] = 0x00000008 | 0x08000000
            else:
                popen_kwargs["start_new_session"] = True
            try:
                child = subprocess.Popen(command, cwd=str(ROOT), stdout=log,
                                         stderr=log, stdin=subprocess.DEVNULL,
                                         **popen_kwargs)
            except OSError as exc:
                fail(f"Автозапуск: не удалось запустить сервер: {exc}")
                time.sleep(60)
                continue
            code = child.wait()
            if state["stop"]:
                break
            if health(args.port):
                backoff = 15  # кто-то поднял панель заново — не мешаем
                continue
            minutes = round(backoff / 60, 1)
            warn(f"Автозапуск: сервер остановился (код {code})."
                 f" Перезапуск через {minutes} мин")
            time.sleep(backoff)
            backoff = min(int(backoff * 1.5) + 5, 600)
    finally:
        clear_pid()
    return 0


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
        "-StartWhenAvailable "  # проспали триггер (сон/занятость) — стартуем позже
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
        # Startup сам не перезапускает сервер — добавляем встроенный watchdog.
        success, shortcut_error = create_windows_shortcut(
            startup, command + ["--watchdog"], "PrintFlow — автозапуск при входе")
        if success:
            detail = "папка Startup + watchdog (Планировщик недоступен)"
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
        # XDG ничего не перезапускает при падении — включаем встроенный watchdog.
        _atomic_write(xdg, render_xdg_entry(command + ["--watchdog"]), mode=0o755)
    except OSError as exc:
        return False, "xdg-autostart", f"{reason}; XDG: {exc}"
    return True, "xdg-autostart", f"XDG Autostart: {xdg} + watchdog ({reason or 'systemd недоступен'})"


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


def autostart_verify() -> list[str]:
    """Проверить автозапуск и вернуть список проблем (пустой — всё хорошо).

    Требовать работающий сервер не будем: после перезагрузки задача может
    стартовать с задержкой. Проверяем установку, включённость, путь и порт.
    """
    problems: list[str] = []
    status = autostart_status()
    if not status["installed"]:
        problems.append("автозапуск не установлен — python pf.py install")
    elif not status["enabled"]:
        problems.append("автозапуск установлен, но выключен — python pf.py autostart enable")
    if status["installed"] and not status["root_matches"]:
        problems.append("PrintFlow перенесён в другую папку — python pf.py autostart repair")
    port = status["port"]
    if port_busy(port) and not health(port):
        problems.append(f"порт {port} занят другой программой — проверьте: python pf.py doctor")
    return problems


def cmd_autostart(args: argparse.Namespace) -> int:
    action = args.autostart_action
    if action == "status":
        status = autostart_status()
        if json_requested(args):
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0 if status["enabled"] and status["root_matches"] else 1
        header("Системный автозапуск PrintFlow")
        _print_autostart_status(status)
        say()
        say("  Изменить: python pf.py autostart enable|disable|repair|verify", Style.DIM)
        say()
        return 0 if status["enabled"] and status["root_matches"] else 1
    if action == "verify":
        header("Проверка автозапуска", "механизм, путь, порт и состояние")
        problems = autostart_verify()
        if not problems:
            ok("Автозапуск в порядке")
        else:
            for problem in problems:
                warn(problem)
        return 0 if not problems else 1
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
    """Окно управления вместо консоли.

    Правило простое: окно не поднялось — панель всё равно должна открыться.
    Иначе владелец видит ровно то, с чего начиналась эта правка: тёмная
    панель мелькнула и закрылась, а PrintFlow как не работал, так и не работает.
    """
    try:
        import tkinter  # noqa: F401
    except ImportError:
        warn("Графическая оболочка недоступна (нет модуля tkinter)")
        say("    Linux: sudo apt install python3-tk")
        if not interactive_console():
            # Запуск из ярлыка (pythonw): текстовое меню никто не увидит, а
            # процесс молча завершится — в этом случае панель поднимаем в браузере.
            say("    Консоли для меню нет — открываю панель в браузере.")
            return cmd_start(args)
        say("    Открываю текстовое меню.")
        return cmd_menu(args)
    try:
        from launcher_window import run_window  # локальный модуль рядом с pf.py

        return run_window(args)
    except Exception as exc:
        log = write_crash_log(exc, "gui")
        warn(f"Окно управления не открылось: {exc}")
        say(f"    Трассировка: {log}", Style.DIM)
        say("    Открываю панель в браузере…")
        return cmd_start(args)


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
    if not interactive_console():
        # Меню читает ввод. Под pythonw.exe ввода нет: раньше input() получал
        # конец файла, команда возвращала 0 — и консоль исчезала без объяснения.
        fail("Текстовое меню требует консоли, а её нет")
        say("    Панель в браузере: python pf.py", Style.DIM)
        return 1
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
        except (EOFError, KeyboardInterrupt, RuntimeError):
            say()
            return 0
        if choice in ("0", "q", ""):
            return 0
        if choice.isdigit() and 1 <= int(choice) <= len(actions):
            actions[int(choice) - 1][1]()
            try:
                input(Style.paint("  Enter — вернуться в меню ", Style.DIM))
            except (EOFError, KeyboardInterrupt, RuntimeError):
                return 0


# ──────────────────────────────────────────────────────────────────── помощь
HELP = """
  ЗАПУСК
    python pf.py                    запустить панель (видна в локальной сети)
    python pf.py app                нативное окно как у 1С/Photoshop (нужен pywebview)
    python pf.py gui                окно управления вместо консоли (tkinter)
    python pf.py --local            только этот компьютер, без доступа по сети
    python pf.py --port 9000        другой порт
    python pf.py --background       запустить в фоне, консоль можно закрыть

  ТЕЛЕФОН И ПЛАНШЕТ
    python pf.py net                адреса для кассы и пульта, QR, проверки
    python pf.py status             что запущено: версия, порт, адреса, автопоиск
    python pf.py status --json      то же машинночитаемо (для скриптов)
    python pf.py --no-qr            запустить без QR-кода в консоли

    Порт по умолчанию — 8765: он же в подсказке приложения кассы, и по нему
    сервер находят кнопкой «Найти сервер в сети» (UDP-маяк, ничего вводить не
    надо). После `pf.py install --port N` обычный запуск берёт порт установки.

  УПРАВЛЕНИЕ
    python pf.py stop               остановить фоновый сервер
    python pf.py logs               последние строки журнала
    python pf.py logs --lines 100   больше строк

  ОБСЛУЖИВАНИЕ
    python pf.py doctor             диагностика: Python, база, сеть, касса, принтеры
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

  Если касса не находит сервер — начните с `python pf.py net`: там адрес для
  телефона, проверка автопоиска и готовое правило для брандмауэра.
"""


def cmd_help(args: argparse.Namespace) -> int:
    header(f"NOZZA · PrintFlow {app_version()}", "локальная система 3D-производства")
    port = running_port()
    if port:
        ok(f"Сервер уже запущен: http://localhost:{port}/")
        for ip in local_ips():
            say(f"    Касса на телефоне: http://{ip}:{port}/cashier.html")
    else:
        say("  Сервер не запущен: python pf.py", Style.DIM)
    print(HELP)
    say(f"  Данные:     {DATA_DIR}", Style.DIM)
    say(f"  Окружение:  {VENV_DIR}", Style.DIM)
    say(f"  Порт:       {resolve_port(getattr(args, 'port', None))}"
        f"   {Style.paint('(python pf.py net — адреса для телефона)', Style.DIM)}")
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
    # choices здесь намеренно не заданы: argparse на незнакомую команду печатает
    # английское «invalid choice» и стену вариантов. Проверяем сами — с
    # подсказкой «похоже на status» (см. main).
    parser.add_argument("command", nargs="?", default="start",
                        metavar="КОМАНДА",
                        help="что сделать (python pf.py help — весь список)")
    parser.add_argument("autostart_action", nargs="?", default="status",
                        choices=["status", "enable", "disable", "repair", "verify"],
                        help="действие для команды autostart")
    parser.add_argument("--port", type=int, default=None,
                        help=f"порт панели (по умолчанию {DEFAULT_PORT}, "
                             "а при установленном автозапуске — порт установки)")
    parser.add_argument("--local", action="store_true",
                        help="слушать только 127.0.0.1 (без доступа с телефона)")
    parser.add_argument("--background", action="store_true", help="запуск в фоне")
    parser.add_argument("--watchdog", action="store_true",
                        help="сервисный режим: перезапускать коннектор при падении "
                             "(для XDG Autostart и папки Startup)")
    parser.add_argument("--json", action="store_true",
                        help="машиночитаемый вывод: status, net, autostart (для скриптов)")
    parser.add_argument("--auto-port", action="store_true",
                        help="занять следующий свободный порт, если основной занят")
    parser.add_argument("--force", action="store_true",
                        help="запустить второй сервер, даже если PrintFlow уже работает")
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
    "net": cmd_net,
    "phone": cmd_net,
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


# ─────────────────────────────────────── сбой, который нельзя потерять (18.6.4)
# Окно, открытое двойным кликом, живёт ровно столько, сколько процесс: закрылся
# pf.py — закрылась и консоль, а трассировка ушла вместе с ней. Отсюда и
# «чёрная панель появилась и сразу закрылась» — единственное, что видел
# владелец. Теперь сбой показывается тремя способами сразу: в консоль (если
# она есть), в журнал запуска (если консоль уже закрылась) и системным окном —
# когда PrintFlow поднят через pythonw.exe и консоли нет вовсе.


def console_alive() -> bool:
    """Есть ли консоль, куда можно печатать.

    `pythonw.exe` (ярлык с рабочего стола и автозапуск Windows) запускается без
    консоли: `sys.stdout` там `None`. Именно из-за этого `Style.setup()` падал
    в первой же строке `main()`, а окно управления не появлялось вообще.
    """
    return sys.stdout is not None


def interactive_console() -> bool:
    """Консоль, в которой можно задать вопрос (текстовое меню, «нажмите Enter»)."""
    try:
        return sys.stdin is not None and bool(sys.stdin.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def write_crash_log(exc: BaseException, command: str = "") -> Path:
    """Записать трассировку в журнал запуска: окно закроется, файл останется."""
    try:
        RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
        trace = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
        with RUN_LOG.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                         f"pf.py {command or '—'}: {type(exc).__name__}: {exc}\n")
            handle.write(f"  Python {platform.python_version()} · {platform.platform()}\n")
            handle.write(trace + "\n")
    except (OSError, ValueError):
        pass
    return RUN_LOG


def show_message(title: str, text: str) -> bool:
    """Системное окно с сообщением — когда печатать некуда (pythonw.exe)."""
    try:
        import tkinter
        from tkinter import messagebox
    except Exception:
        return False
    try:
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(title, text)
        root.destroy()
        return True
    except Exception:
        return False


def report_failure(exc: BaseException | None, command: str = "",
                   log: Path | None = None) -> None:
    """Показать сбой так, чтобы его нельзя было не заметить."""
    reason = "" if exc is None else (str(exc).strip() or type(exc).__name__)
    say()
    fail(f"PrintFlow не смог выполнить «{command}»" + (f": {reason}" if reason else ""))
    if log is not None:
        say(f"    Трассировка: {log}", Style.DIM)
    for line in ("Диагностика: python pf.py doctor",
                 "Журнал:      python pf.py logs"):
        say(f"    {line}", Style.DIM)
    if not console_alive():
        lines = [reason] if reason else []
        if log is not None:
            lines.append(f"Трассировка: {log}")
        lines.append("Диагностика: python pf.py doctor")
        show_message(f"PrintFlow · {command or 'запуск'}", "\n".join(lines))


def _parent_pid() -> int:
    """PID родительского процесса (Windows: чтобы отличить двойной клик)."""
    if not IS_WINDOWS:
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        class _BasicInformation(ctypes.Structure):
            _fields_ = [("reserved1", ctypes.c_void_p), ("peb", ctypes.c_void_p),
                        ("reserved2", ctypes.c_void_p * 2), ("pid", ctypes.c_void_p),
                        ("parent", ctypes.c_void_p)]

        info = _BasicInformation()
        status = ctypes.windll.ntdll.NtQueryInformationProcess(
            ctypes.windll.kernel32.GetCurrentProcess(), 0, ctypes.byref(info),
            ctypes.sizeof(info), ctypes.byref(wintypes.ULONG()))
        return int(info.parent or 0) if status == 0 else 0
    except Exception:
        return 0


def parent_process_name() -> str:
    """Имя родителя: `explorer.exe` значит, что pf.py открыт двойным кликом."""
    pid = _parent_pid()
    if not pid:
        return ""
    try:
        listing = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=10).stdout
        return listing.split(",")[0].strip('"').strip().lower()
    except Exception:
        return ""


def hold_console(reason: str = "") -> None:
    """Подержать консоль открытой, если её открыл Проводник, а не человек.

    Двойной клик по pf.py: Windows создаёт консоль сама и убивает её вместе с
    процессом — текст ошибки не успевают прочитать. Из cmd или PowerShell ждать
    не нужно: там и так всё видно, а лишний Enter только мешает.
    """
    if not IS_WINDOWS or not interactive_console():
        return
    if parent_process_name() != "explorer.exe":
        return
    try:
        say()
        input(Style.paint(f"  {reason or 'Нажмите Enter, чтобы закрыть окно…'}",
                          Style.DIM))
    except (EOFError, KeyboardInterrupt, OSError, RuntimeError):
        pass


def suggest_command(typed: str) -> str:
    """Ближайшая команда к тому, что набрали: «statsu» → «status»."""
    import difflib

    matches = difflib.get_close_matches(str(typed), sorted(COMMANDS), n=1, cutoff=0.5)
    return matches[0] if matches else ""


def main(argv: list[str] | None = None) -> int:
    Style.setup()
    parser = build_parser()
    raw = list(argv if argv is not None else sys.argv[1:])
    try:
        args = parser.parse_args(raw)
    except SystemExit as exc:
        # Ошибки значений (например, --startup-delay 301) argparse печатает сам;
        # добавляем только человеческую строку «что дальше».
        if exc.code == 2:
            say()
            say("  Проверьте команду и параметры: python pf.py help", Style.YELLOW)
            say()
        return 2
    if args.command not in COMMANDS:
        say()
        fail(f"Не знаю команду «{args.command}»")
        guess = suggest_command(args.command)
        if guess:
            say(f"    Похоже на «{guess}» — попробуйте: python pf.py {guess}", Style.DIM)
        say("    Все команды: python pf.py help", Style.DIM)
        say()
        return 2
    if getattr(args, "want_help", False):
        return cmd_help(args)
    if not ENTRYPOINT.exists():
        fail(f"Не найден {ENTRYPOINT}")
        say("    Запускайте pf.py из папки репозитория PrintFlow.")
        return 1
    # Порт решаем один раз для всех команд: явный --port → порт установки →
    # обычный. Дальше по коду args.port читается как «тот самый порт».
    args.port = resolve_port(getattr(args, "port", None))
    try:
        return COMMANDS[args.command](args)
    except KeyboardInterrupt:
        say()
        say("  Прервано.", Style.DIM)
        return 130
    except SystemExit as request:  # команда сама решила остановиться (нет окружения…)
        code = request.code
        code = 0 if code is None else (code if isinstance(code, int) else 1)
        if code:
            if not console_alive():
                show_message(f"PrintFlow · {args.command}",
                             "Не удалось выполнить команду. "
                             "Запустите в консоли: python pf.py doctor")
            hold_console()
        return code
    except Exception as exc:  # окно двойного клика иначе скроет трассировку
        log = write_crash_log(exc, args.command)
        report_failure(exc, args.command, log)
        hold_console()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

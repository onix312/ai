"""Шлюз Bambu Studio: PrintFlow выглядит как принтер в LAN.

Studio находит устройство по SSDP (urn:bambulab-com:device:3dprinter:1),
заливает файл implicit FTPS :990 и шлёт project_file по MQTT/TLS :8883.
PrintFlow кладёт файл в библиотеку и очередь (estimate, preflight, AMS-map);
на физический принтер уходит уже проверенное задание. Это не прозрачный
прокси и не виртуальный принтер из virtual.py.

Автостарт печати — только при studio_gateway_mode=autostart
и studio_gateway_autostart и unattended_dangerous_actions.

Тесты передают bind=False: сокеты не открываются, хуки identity /
ssdp_notify / ingest_bytes / mqtt_handle_packet / ftp_command работают
без сети.
"""
from __future__ import annotations

import errno
import json
import re
import secrets
import socket
import ssl
import struct
import threading
import time
from pathlib import Path

from .config import UPLOAD_DIR, get_local_ips, now_iso
from .studio_mqtt import (
    CONNECT,
    DISCONNECT,
    PINGREQ,
    PUBLISH,
    SUBSCRIBE,
    UNSUBSCRIBE,
    decode_connect,
    decode_publish,
    decode_subscribe,
    decode_unsubscribe,
    encode_connack,
    encode_pingresp,
    encode_puback,
    encode_publish,
    encode_suback,
    encode_unsuback,
    parse_fixed_header,
    read_packet,
)

SSDP_NT = "urn:bambulab-com:device:3dprinter:1"
SSDP_GROUP = "239.255.255.250"
# Настоящие принтеры шлют NOTIFY широковещательно на 255.255.255.255:2021,
# а сетевой плагин Studio слушает UDP :2021. Дополнительно рассылаем в
# группу 239.255.255.250 — так шлюз видят и старые клиенты (1990/1900).
SSDP_BROADCAST = "255.255.255.255"
SSDP_PORTS = (2021, 1990, 1900)
# Порты, которые шлюз СЛУШАЕТ. UDP 2021 шлюз не занимает и занимать не должен:
# на нём слушает сам сетевой плагин Bambu Studio. Две розетки на одном порту с
# SO_REUSEADDR в Windows делят входящий трафик непредсказуемо — шлюз,
# поднявшийся раньше Studio, отбирал у неё единственный источник адреса
# принтера, Studio получала пустой dev_ip и отдавала «код=-1» до MQTT.
SSDP_LISTEN_PORTS = (1900,)
SSDP_NOTIFY_PERIOD = 5.0  # принтеры анонсируют себя раз в ~5 секунд
SSDP_NOTIFY_LOOPBACK = "127.0.0.1"  # адрес, по которому Studio видит шлюз всегда


def directed_broadcast(ip: str) -> str:
    """Направленный широковещательный адрес для /24: 192.168.1.50 → .255.

    Пакет на такой адрес доходит до соседей по подсети на домашних роутерах
    надёжнее, чем на «общий» 255.255.255.255: последний часть оборудования
    режет. Если адрес не IPv4 — пустая строка (рассылать некуда).
    """
    parts = str(ip or "").strip().split(".")
    if len(parts) != 4:
        return ""
    for part in parts:
        if not part.isdigit() or not 0 <= int(part) <= 255:
            return ""
    return ".".join(parts[:3] + ["255"])


MQTT_PORT = 8883
FTP_PORT = 990
# Порт, на котором Studio спрашивает личность принтера ДО MQTT
# (bambu_network_bind_detect): обычный TCP, один кадр login/detect.
# Нет ответа — Studio не добавляет принтер и пишет «Сбой подключения, код=-1».
BIND_PORT_PLAIN = 3000
BIND_PORT_TLS = 3002
BIND_MAGIC_HEAD = b"\xa5\xa5"
BIND_MAGIC_TAIL = b"\xa7\xa7"
# Прошивка, которую шлюз называет Studio (SSDP DevVersion и login/detect).
FIRMWARE_VERSION = "01.07.00.00"
MQTT_USER = "bblp"

DEV_MODELS = {
    "P1S": "C12",
    "P1P": "C11",
    "X1C": "BL-P001",
    "X1 Carbon": "BL-P001",
    "X1": "BL-P002",
    "X1E": "C13",
    "A1": "N2S",
    "A1 mini": "N1",
    "A1 Mini": "N1",
}

_PLATE_RE = re.compile(r"plate[_\-]?(\d+)", re.I)


def _dev_model(model: str) -> str:
    name = (model or "").strip()
    if name in DEV_MODELS:
        return DEV_MODELS[name]
    upper = name.upper()
    for key, value in DEV_MODELS.items():
        if key.upper() == upper:
            return value
    return "C12"


def _new_serial() -> str:
    return "01P00A" + secrets.token_hex(5)[:9].upper()


def encode_bind_frame(payload: dict) -> bytes:
    """Кадр порта 3000/3002: A5A5 + длина (u16 LE) + JSON + A7A7.

    Длина считается по всему кадру вместе с обеими магиями и полем длины —
    так же, как её кладёт прошивка принтера (и как читает плагин Studio).
    """
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    total = len(BIND_MAGIC_HEAD) + 2 + len(body) + len(BIND_MAGIC_TAIL)
    if total > 0xFFFF:
        raise ValueError("кадр шлюза слишком большой для поля длины")
    return BIND_MAGIC_HEAD + struct.pack("<H", total) + body + BIND_MAGIC_TAIL


def decode_bind_frame(chunk: bytes) -> tuple[dict | None, bytes]:
    """Разобрать первый кадр из буфера: (payload, остаток).

    Возвращает ``(None, rest)``, если кадра нет целиком или он битый —
    вызывающий копит буфер дальше. Это тот же разбор, что делает
    Bambu-плагин (ищет магию, читает длину, проверяет хвост), только без
    молчаливого выбрасывания «лишних» байт.
    """
    data = bytes(chunk or b"")
    head = BIND_MAGIC_HEAD
    start = data.find(head)
    if start < 0:
        return None, b""
    if start:
        data = data[start:]
    if len(data) < 6:
        return None, data
    total = struct.unpack_from("<H", data, 2)[0]
    if total < 6 or len(data) < total:
        return None, data
    if data[total - 2:total] != BIND_MAGIC_TAIL:
        return None, data[2:]
    try:
        payload = json.loads(data[4:total - 2].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, data[total:]
    if not isinstance(payload, dict):
        return None, data[total:]
    return payload, data[total:]


def _filename_from_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    text = text.split("?", 1)[0]
    return Path(text.replace("\\", "/").rstrip("/")).name


class StudioGateway:
    """Эмулятор принтера Bambu для Studio → очередь PrintFlow."""

    def __init__(self, db, manager, bus=None, bind: bool = True):
        self.db = db
        self.manager = manager
        self.bus = bus
        self.bind = bool(bind)
        self.last_error = ""
        # Диагностика 18.7: сбой каждого сервиса отдельно (SSDP/MQTT/FTPS),
        # счётчики подключений и неудачных авторизаций, адрес последнего
        # клиента. Видны в /api/studio/status и в журнале коннектора.
        self._errors: dict[str, str] = {"ssdp": "", "mqtt": "", "ftps": "", "bind": ""}
        self._counters: dict[str, int] = {
            "mqtt_connections": 0,
            "mqtt_auth_failures": 0,
            "ftp_connections": 0,
            "ftp_auth_failures": 0,
            "bind_requests": 0,
            "bind_detects": 0,
        }
        self._last_client = ""
        self._last_auth_fail_at = ""
        self._tls_ctx = None
        self._bind_socks: list = []
        self._data_conn = None
        self._data_ready = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._incoming: dict[str, bytes] = {}
        self._mqtt_authed = False
        self._ftp_user = ""
        self._ftp_authed = False
        self._ftp_cwd = "/"
        self._stor_name = ""
        self._ssdp_sock = None
        self._ssdp_bound_port = 0
        self._ssdp_note = ""
        self._mqtt_sock = None
        self._ftp_sock = None
        self._threads: list[threading.Thread] = []
        self._last_notify = 0.0
        self._host_cache = ""
        self._ips_cache: list[str] = []
        self._ips_cache_at = 0.0
        self._pending_confirm: dict[str, dict] = {}

    # ----------------------------------------------------------- настройки
    def _enabled(self) -> bool:
        try:
            return bool(self.db.setting("studio_gateway_enabled", False))
        except Exception:
            return False

    def _access_code(self) -> str:
        try:
            return str(self.db.setting("studio_gateway_access_code", "") or "")
        except Exception:
            return ""

    def _autostart_allowed(self) -> bool:
        try:
            mode = str(self.db.setting("studio_gateway_mode", "confirm") or "confirm").strip().lower()
            return (
                mode == "autostart"
                and bool(self.db.setting("studio_gateway_autostart", False))
                and bool(self.db.setting("unattended_dangerous_actions", False))
            )
        except Exception:
            return False

    def _mode(self) -> str:
        try:
            return str(self.db.setting("studio_gateway_mode", "confirm") or "confirm").strip().lower()
        except Exception:
            return "confirm"

    def _ensure_identity(self) -> None:
        try:
            serial = str(self.db.setting("studio_gateway_serial", "") or "").strip()
        except Exception:
            return
        patch: dict[str, object] = {}
        if not serial:
            patch["studio_gateway_serial"] = _new_serial()
        try:
            need_code = not self._access_code()
        except Exception:
            need_code = False
        if need_code:
            patch["studio_gateway_access_code"] = secrets.token_hex(4)
        if patch:
            try:
                self.db.set_settings(patch)
            except Exception:
                pass

    def _host_ip(self) -> str:
        pinned = self._pinned_host()
        if pinned:
            return pinned
        if not self.bind:
            return "127.0.0.1"
        if self._host_cache:
            return self._host_cache
        try:
            ips = get_local_ips()
            if ips:
                self._host_cache = ips[0]
                return self._host_cache
        except Exception:
            pass
        return "127.0.0.1"

    def _pinned_host(self) -> str:
        """Адрес из настройки studio_gateway_host (пусто = авто).

        Владелец закрепляет адрес, когда на компьютере есть VirtualBox /
        Hyper-V / Docker / VPN: их виртуальные 192.168.* оказываются первыми
        в get_local_ips(), Studio получает их в SSDP и подключается не туда.
        Непохожее на IPv4 значение игнорируется, но попадает в диагностику.
        """
        try:
            raw = str(self.db.setting("studio_gateway_host", "") or "").strip()
        except Exception:
            return ""
        if not raw:
            # пустая настройка — сбрасываем прошлую ошибку хоста, если она была
            self._errors.pop("host", None)
            return ""
        parts = raw.split(".")
        ok = len(parts) == 4
        if ok:
            for part in parts:
                if not part.isdigit() or not 0 <= int(part) <= 255:
                    ok = False
                    break
        if not ok:
            message = f"настройка studio_gateway_host не похожа на IPv4: {raw!r}"
            if self._errors.get("host") != message:
                self._errors["host"] = message
            return ""
        self._errors["host"] = ""
        return raw

    def _bound_printer(self):
        if not self.manager or not hasattr(self.manager, "get"):
            return None
        try:
            pid = str(self.db.setting("studio_gateway_printer_id", "") or "").strip()
        except Exception:
            pid = ""
        if pid:
            return self.manager.get(pid)
        try:
            from .virtual import VIRTUAL_ID
        except Exception:
            VIRTUAL_ID = ""
        printers = getattr(self.manager, "printers", {}) or {}
        for printer in printers.values():
            record = getattr(printer, "record", {}) or {}
            if str(record.get("id") or "") == VIRTUAL_ID:
                continue
            return printer
        return self.manager.get()

    def _printer_model(self) -> str:
        printer = self._bound_printer()
        if printer:
            record = getattr(printer, "record", {}) or {}
            model = str(record.get("model") or "").strip()
            if model:
                return model
        return "P1S"

    def identity(self) -> dict:
        """Публичная личность шлюза. Access Code сюда не входит."""
        try:
            self._ensure_identity()
        except Exception:
            pass
        try:
            model = self._printer_model()
        except Exception:
            model = "P1S"
        try:
            serial = str(self.db.setting("studio_gateway_serial", "") or "").strip()
        except Exception:
            # БД закрыта (завершение процесса / тест) — не роняем SSDP-поток.
            # Серийник остаётся прежним; если его не знаем — временный, без записи в БД.
            serial = ""
        try:
            name = str(self.db.setting("studio_gateway_name", "NOZZA-PrintFlow")
                       or "NOZZA-PrintFlow").strip() or "NOZZA-PrintFlow"
        except Exception:
            name = "NOZZA-PrintFlow"
        if not serial:
            # В нормальном старте сюда не попадаем: _ensure_identity уже создала серийник.
            # При закрытой БД возвращаем временный, чтобы объявление не было пустым.
            serial = _new_serial()
        try:
            host = self._host_ip()
        except Exception:
            host = "127.0.0.1"
        return {
            "serial": serial,
            "name": name,
            "model": model,
            "dev_model": _dev_model(model),
            "host": host,
            "mqtt_port": MQTT_PORT,
            "ftp_port": FTP_PORT,
            "bind_port": BIND_PORT_PLAIN,
            "version": FIRMWARE_VERSION,
        }

    def _note_auth_failure(self, channel: str) -> None:
        """Засчитать отказ авторизации (MQTT или FTP) и показать в журнале.

        Studio, которой ввели неверный Access Code, молча стучится снова и
        снова — без счётчика владелец видит только «код=-1» без причины.
        """
        key = f"{channel}_auth_failures"
        with self._lock:
            self._counters[key] = int(self._counters.get(key, 0)) + 1
            self._last_auth_fail_at = now_iso()
            total = self._counters[key]
        try:
            from .logging_setup import log
            log().warning(
                "Шлюз Bambu Studio: %s: клиент не прошёл Access Code (раз: %d)",
                "MQTT" if channel == "mqtt" else "FTPS", total)
        except Exception:
            pass

    def _record_error(self, service: str, exc: Exception | str) -> None:
        """Сохранить сбой сервиса шлюза и продублировать его в журнал.

        Раньше last_error перезаписывался следующим сервисом: при занятых
        8883 и 990 владелец видел только «FTPS: …» и не знал про MQTT.
        """
        text = str(exc).strip() or "неизвестная ошибка"
        self._errors[service.lower()] = text
        self.last_error = f"{service}: {text}"
        try:
            from .logging_setup import log
            log().warning("Шлюз Bambu Studio: %s: %s", service, text)
        except Exception:
            pass

    def status(self) -> dict:
        # identity может упасть на закрытой БД — статус всё равно отдаём, без падения карточки.
        try:
            ident = self.identity()
        except Exception:
            ident = {
                "serial": "", "name": "NOZZA-PrintFlow", "model": "P1S", "dev_model": "C12",
                "host": "127.0.0.1", "mqtt_port": MQTT_PORT, "ftp_port": FTP_PORT,
                "bind_port": BIND_PORT_PLAIN, "version": FIRMWARE_VERSION,
            }
        try:
            pinned_raw = str(self.db.setting("studio_gateway_host", "") or "").strip()
        except Exception:
            pinned_raw = ""
        with self._lock:
            counters = dict(self._counters)
            last_client = self._last_client
            last_auth_fail_at = self._last_auth_fail_at
        # настройки — каждая с защитой от закрытой БД
        try:
            mode = str(self.db.setting("studio_gateway_mode", "queue") or "queue")
        except Exception:
            mode = "queue"
        try:
            autostart = bool(self.db.setting("studio_gateway_autostart", False))
        except Exception:
            autostart = False
        try:
            printer_id = str(self.db.setting("studio_gateway_printer_id", "") or "")
        except Exception:
            printer_id = ""
        try:
            ssdp_targets = [f"{host}:{port}" for host, port in self._notify_targets()]
        except Exception:
            ssdp_targets = [f"{SSDP_NOTIFY_LOOPBACK}:{SSDP_PORTS[0]}"]
        try:
            has_code = bool(self._access_code())
        except Exception:
            has_code = False
        out = {
            "enabled": self._enabled(),
            "running": bool(self._mqtt_sock or self._ftp_sock or self._ssdp_sock
                            or self._bind_socks),
            "mqtt_running": bool(self._mqtt_sock),
            "ftp_running": bool(self._ftp_sock),
            "ssdp_running": bool(self._ssdp_sock),
            "bind": self.bind,
            "mode": mode,
            "autostart": autostart,
            "autostart_allowed": self._autostart_allowed(),
            "printer_id": printer_id,
            "mqtt_port": MQTT_PORT,
            "ftp_port": FTP_PORT,
            "bind_port": BIND_PORT_PLAIN,
            "bind_tls_port": BIND_PORT_TLS,
            "bind_ports": [BIND_PORT_PLAIN, BIND_PORT_TLS],
            "bind_running": bool(self._bind_socks),
            "bind_requests": int(counters.get("bind_requests", 0)),
            "bind_detects": int(counters.get("bind_detects", 0)),
            "dropped_connections": int(counters.get("dropped_connections", 0)),
            "ssdp_ports": list(SSDP_PORTS),
            "ssdp_listen_ports": list(SSDP_LISTEN_PORTS),
            "ssdp_bound_port": int(self._ssdp_bound_port),
            "ssdp_note": self._ssdp_note,
            "ssdp_targets": ssdp_targets,
            "last_error": self.last_error,
            "errors": {key: value for key, value in self._errors.items() if value},
            "host_pinned": bool(pinned_raw),
            "mqtt_connections": int(counters.get("mqtt_connections", 0)),
            "mqtt_auth_failures": int(counters.get("mqtt_auth_failures", 0)),
            "ftp_connections": int(counters.get("ftp_connections", 0)),
            "ftp_auth_failures": int(counters.get("ftp_auth_failures", 0)),
            "last_client": last_client,
            "last_auth_fail_at": last_auth_fail_at,
            "has_access_code": has_code,
            "urn": SSDP_NT,
        }
        out.update(ident)
        return out

    # ----------------------------------------------------------- SSDP
    def _ssdp_headers(self) -> str:
        """Поля объявления, которые разбирает сетевой плагин Studio.

        DevVersion / DevCap / Devseclink / DevInf не косметика: плагин
        собирает из них JSON для DeviceManager и падает на отсутствующих
        ключах, а по DevVersion Studio решает, как общаться с устройством.
        """
        ident = self.identity()
        return (
            f"DevModel.bambu.com: {ident['dev_model']}\r\n"
            f"DevName.bambu.com: {ident['name']}\r\n"
            "DevSignal.bambu.com: -44\r\n"
            "DevConnect.bambu.com: lan\r\n"
            "DevBind.bambu.com: free\r\n"
            "Devseclink.bambu.com: secure\r\n"
            "DevInf.bambu.com: eth0\r\n"
            f"DevVersion.bambu.com: {ident['version']}\r\n"
            "DevCap.bambu.com: 1\r\n"
        )

    def ssdp_notify(self) -> str:
        ident = self.identity()
        return (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {SSDP_GROUP}:1900\r\n"
            "Server: Buildroot/2018.02-rc3 UPnP/1.0 ssdpd/1.8\r\n"
            f"Location: {ident['host']}\r\n"
            f"NT: {SSDP_NT}\r\n"
            "NTS: ssdp:alive\r\n"
            f"USN: {ident['serial']}\r\n"
            "Cache-Control: max-age=1800\r\n"
            + self._ssdp_headers()
            + "\r\n"
        )

    def ssdp_search_response(self) -> str:
        ident = self.identity()
        return (
            "HTTP/1.1 200 OK\r\n"
            f"ST: {SSDP_NT}\r\n"
            f"USN: {ident['serial']}\r\n"
            f"Location: {ident['host']}\r\n"
            "Cache-Control: max-age=1800\r\n"
            "Server: Buildroot/2018.02-rc3 UPnP/1.0 ssdpd/1.8\r\n"
            + self._ssdp_headers()
            + "\r\n"
        )

    # ----------------------------------------------------------- ingest
    def ingest_bytes(self, filename: str, data: bytes, command: dict | None = None) -> dict:
        """Принять байты от Studio: библиотека + очередь. Без сокета."""
        if not data:
            raise ValueError("Пустой файл")
        name = Path(str(filename or "project.gcode.3mf").replace("\\", "/")).name
        from .library import FileLibrary
        from .estimate import auto_ams_map, estimate_file
        from .sd_browser import can_print

        lib = FileLibrary(self.db)
        rec = lib.put(name, data, source="studio-gateway")
        estimate: dict = {}
        try:
            local = UPLOAD_DIR / rec["upload_name"]
            if local.is_file():
                estimate = estimate_file(local) or {}
        except Exception:
            estimate = {}
        cmd = command or {}
        print_cmd = cmd.get("print") if isinstance(cmd.get("print"), dict) else cmd
        if not isinstance(print_cmd, dict):
            print_cmd = {}
        plate = 1
        param = str(print_cmd.get("param") or "")
        match = _PLATE_RE.search(param)
        if match:
            plate = max(1, int(match.group(1)))
        mapping = print_cmd.get("ams_mapping")
        if not isinstance(mapping, list):
            mapping = []
            filaments = estimate.get("filaments") or []
            printer = self._bound_printer()
            try:
                ams_auto = bool(self.db.setting("ams_auto_map", True))
            except Exception:
                ams_auto = True
            if filaments and printer and ams_auto:
                try:
                    snap = printer.snapshot()
                    trays = ((snap.get("ams") or {}).get("trays") or [])
                    mapping = auto_ams_map(filaments, trays)
                except Exception:
                    mapping = []
        printer = self._bound_printer()
        printer_id = ""
        if printer:
            printer_id = str((getattr(printer, "record", {}) or {}).get("id") or "")
        try:
            setting_pid = str(self.db.setting("studio_gateway_printer_id", "") or "").strip()
        except Exception:
            setting_pid = ""
        if setting_pid:
            printer_id = setting_pid
        mode = self._mode()
        autostart = self._autostart_allowed()
        job = None
        error = ""
        payload = {
            "file": rec["upload_name"],
            "name": Path(name).stem,
            "source": "studio-gateway",
            "printer_id": printer_id,
            "plate": plate,
            "use_ams": bool(print_cmd.get("use_ams", True)),
            "bed_level": bool(print_cmd.get("bed_leveling", True)),
            "flow_cali": bool(print_cmd.get("flow_cali", False)),
            "timelapse": bool(print_cmd.get("timelapse", False)),
            "ams_mapping": mapping,
            "est_grams": estimate.get("total_grams") or estimate.get("grams") or 0,
            "est_minutes": estimate.get("total_minutes") or estimate.get("minutes") or 0,
            "no_auto": 0 if autostart else 1,
            "allow_auto_start": False,
        }
        # В режиме queue или autostart сразу ставим в очередь;
        # В режиме confirm ждём решения оператора в модальном окне
        if mode in ("queue", "autostart") and self.manager and hasattr(self.manager, "enqueue") and can_print(name):
            try:
                job = self.manager.enqueue(payload)
            except Exception as exc:
                error = str(exc)
                job = None
            if (autostart and job and not error
                    and hasattr(self.manager, "start_job")):
                try:
                    check = {}
                    if hasattr(self.manager, "preflight"):
                        check = self.manager.preflight(
                            printer_id, rec["upload_name"], plate, mapping) or {}
                    if not check.get("blocks"):
                        self.manager.start_job(job.get("id") or "", printer_id)
                        job = dict(job)
                        job["autostart"] = True
                except Exception as exc:
                    error = str(exc)
        # Сохранение входящего проекта для окна подтверждения оператора
        pending_id = f"st_{int(time.time()*1000)}"
        pending_item = {
            "id": pending_id,
            "filename": name,
            "upload_name": rec["upload_name"],
            "library_id": rec.get("id"),
            "job_id": (job or {}).get("id"),
            "printer_id": printer_id,
            "plate": plate,
            "payload": payload,
            "ams_mapping": mapping,
            "est_grams": estimate.get("total_grams") or estimate.get("grams") or 0,
            "est_minutes": estimate.get("total_minutes") or estimate.get("minutes") or 0,
            "filaments": estimate.get("filaments") or [],
            "thumbnails": estimate.get("thumbnails") or {},
            "at": now_iso(),
            "mode": mode,
            "autostart": autostart,
            "error": error,
        }
        with self._lock:
            self._pending_confirm[pending_id] = pending_item
            if len(self._pending_confirm) > 30:
                oldest = sorted(self._pending_confirm.keys())[:10]
                for k in oldest:
                    self._pending_confirm.pop(k, None)

        try:
            self.db.add_event(
                "studio", "Файл из Bambu Studio",
                name, printer_id,
                {"library_id": rec.get("id"), "job_id": (job or {}).get("id"),
                 "pending_id": pending_id, "autostart": autostart},
            )
        except Exception:
            pass
        if self.bus:
            try:
                self.bus.publish("studio", {
                    "file": name, "library_id": rec.get("id"),
                    "job_id": (job or {}).get("id"),
                    "pending_id": pending_id,
                    "pending": pending_item,
                })
            except Exception:
                pass
        return {
            "library": rec,
            "job": job,
            "estimate": estimate,
            "autostart": autostart,
            "pending_id": pending_id,
            "pending": pending_item,
            "error": error,
        }

    def pending_list(self) -> list[dict]:
        with self._lock:
            return sorted(self._pending_confirm.values(), key=lambda x: str(x.get("at") or ""), reverse=True)

    def pending_get(self, pending_id: str) -> dict | None:
        with self._lock:
            return self._pending_confirm.get(pending_id)

    def pending_dismiss(self, pending_id: str) -> bool:
        with self._lock:
            return bool(self._pending_confirm.pop(pending_id, None))

    def _file_bytes(self, filename: str) -> bytes | None:
        name = Path(str(filename or "").replace("\\", "/")).name
        if not name:
            return None
        with self._lock:
            if name in self._incoming:
                return self._incoming[name]
            for key, value in self._incoming.items():
                if Path(key).name == name:
                    return value
        try:
            from .library import FileLibrary
            for row in FileLibrary(self.db).list(q=name, limit=8):
                if row.get("name") == name or row.get("upload_name") == name:
                    path = Path(row.get("path") or "")
                    if path.is_file():
                        return path.read_bytes()
        except Exception:
            pass
        upload = UPLOAD_DIR / name
        if upload.is_file():
            return upload.read_bytes()
        return None

    # ------------------------------------------------- порт 3000/3002: detect
    def bind_reply(self, payload: dict) -> dict | None:
        """Ответ шлюза на кадр порта 3000/3002 (проба личности от Studio).

        Studio спрашивает принтер ДО MQTT и ДО FTPS: ``bind_detect``
        открывает обычный TCP на :3000, шлёт ``login/detect`` и ждёт
        серийник, модель и состояние привязки. Раньше на :3000 nobody не
        слушал — соединение сбрасывалось, плагин возвращал
        «socket connect failed» и Studio показывала «код=-1».
        """
        if not isinstance(payload, dict):
            return None
        login = payload.get("login")
        if not isinstance(login, dict):
            return None
        command = str(login.get("command") or "").strip().lower()
        ident = self.identity()
        with self._lock:
            if command == "detect":
                self._counters["bind_requests"] = int(
                    self._counters.get("bind_requests", 0)) + 1
                self._counters["bind_detects"] = int(
                    self._counters.get("bind_detects", 0)) + 1
        if command == "detect":
            return {"login": {
                "bind": "free",
                "command": "detect",
                "connect": "lan",
                "dev_cap": 1,
                "id": ident["serial"],
                "model": ident["dev_model"],
                "name": ident["name"],
                "sequence_id": 3021,   # принтер отвечает числом, не эхом строки
                "version": ident["version"],
            }}
        if command == "login":
            # Привязка к аккаунту — облачная история (тикет + подтверждение в
            # Bambu Cloud), её шлюз не изображает. Локальное подключение по
            # Access Code этим не пользуется, поэтому честно отвечаем успехом
            # LAN-входа: Studio не висит в ожидании ответа до таймаута.
            return {"login": {
                "command": "login_report",
                "sequence_id": -1,
                "session_id": 0,
                "status": "SUCCESS",
            }}
        return None

    def bind_handle_bytes(self, chunk: bytes) -> bytes:
        """Кадр порта 3000/3002 без сети → готовый кадр с ответом (или b'')."""
        payload, _rest = decode_bind_frame(chunk)
        if payload is None:
            return b""
        reply = self.bind_reply(payload)
        if not reply:
            return b""
        return encode_bind_frame(reply)

    def _bind_client(self, conn) -> None:
        """Один клиент порта 3000/3002: один запрос — один ответ, как у станка."""
        try:
            conn.settimeout(15)
            try:
                peer = conn.getpeername()[0]
            except OSError:
                peer = ""
            if peer:
                with self._lock:
                    self._last_client = peer
            buf = b""
            deadline = time.time() + 15
            while time.time() < deadline and len(buf) < 65536:
                try:
                    piece = conn.recv(4096)
                except (socket.timeout, TimeoutError):
                    continue
                except OSError:
                    return
                if not piece:
                    return
                buf += piece
                payload, buf = decode_bind_frame(buf)
                if payload is None:
                    continue
                reply = self.bind_reply(payload)
                if reply:
                    conn.sendall(encode_bind_frame(reply))
                return   # принтер закрывает сессию после одного обмена
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _listen_tcp(self, port: int, backlog: int = 8, tls: bool = False,
                    cert=None, key=None):
        """Открыть слушающий TCP-порт шлюза, закрыв розетку при сбое.

        Без этого занятый порт (второй PrintFlow, Bambu Connect, Orca) оставлял
        висящую розетку — в журнале владелец видел ResourceWarning вместо
        причины, а шлюз продолжал считать себя поднятым.
        """
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            raw.bind(("0.0.0.0", port))
            raw.listen(backlog)
            raw.settimeout(1.0)
            if tls:
                ctx = self._tls_ctx or self._tls_context(cert, key)
                return ctx.wrap_socket(raw, server_side=True)
            return raw
        except BaseException:
            try:
                raw.close()
            except OSError:
                pass
            raise

    def _start_bind(self, cert, key) -> None:
        """Поднять :3000 (TCP) и :3002 (TLS) — пробу личности Studio."""
        for port in (BIND_PORT_PLAIN, BIND_PORT_TLS):
            try:
                sock = self._listen_tcp(port, 8, tls=(port == BIND_PORT_TLS),
                                        cert=cert, key=key)
                self._bind_socks.append(sock)
                self._spawn(f"pf-studio-bind-{port}", self._accept_loop,
                            sock, self._bind_client)
            except Exception as exc:
                self._record_error(
                    "bind", f"порт {port}: {exc}" if isinstance(exc, OSError) else exc)

    # ----------------------------------------------------------- MQTT
    def handle_mqtt_request(self, payload: dict, topic: str = "") -> list[dict]:
        """JSON-команда Studio → список отчётов на device/{serial}/report."""
        if not isinstance(payload, dict):
            return []
        reports: list[dict] = []
        print_obj = payload.get("print")
        if isinstance(print_obj, dict):
            reports.extend(self._handle_print_cmd(print_obj))
        pushing = payload.get("pushing")
        if isinstance(pushing, dict) and str(pushing.get("command") or "") in (
                "pushall", "start", "pushsta"):
            reports.append(self._push_status(str(pushing.get("sequence_id") or "0")))
        info = payload.get("info")
        if isinstance(info, dict) and info.get("command") == "get_version":
            reports.append(self._version_report(str(info.get("sequence_id") or "0")))
        system = payload.get("system")
        if isinstance(system, dict) and system.get("command"):
            command = str(system.get("command") or "")
            reply = {
                "system": {
                    "command": command,
                    "sequence_id": str(system.get("sequence_id") or "0"),
                    "result": "success",
                }
            }
            # Сетевой плагин спрашивает код сразу после подключения — без
            # поля access_code Studio считает ответ пустым и не показывает
            # принтер как доступный.
            if command == "get_access_code":
                reply["system"]["access_code"] = self._access_code()
            reports.append(reply)
        return reports

    def mqtt_handle(self, payload: dict, topic: str = "") -> list[dict]:
        return self.handle_mqtt_request(payload, topic)

    def _handle_print_cmd(self, cmd: dict) -> list[dict]:
        name = str(cmd.get("command") or "")
        seq = str(cmd.get("sequence_id") or "0")
        if name in ("push_status", "pushall"):
            return [self._push_status(seq)]
        if name == "project_file":
            return self._handle_project_file(cmd, seq)
        if name in ("pause", "resume", "stop"):
            printer = self._bound_printer()
            if printer and hasattr(printer, "command"):
                try:
                    printer.command(name)
                except Exception:
                    pass
            return [{"print": {"command": name, "sequence_id": seq, "result": "success"}}]
        if name == "gcode_file":
            fake = dict(cmd)
            fake["url"] = str(cmd.get("param") or cmd.get("url") or "")
            return self._handle_project_file(fake, seq)
        return [{"print": {"command": name or "unknown", "sequence_id": seq, "result": "success"}}]

    def _handle_project_file(self, cmd: dict, seq: str) -> list[dict]:
        filename = _filename_from_url(str(cmd.get("url") or ""))
        if not filename:
            filename = Path(str(cmd.get("param") or "project.gcode.3mf")).name
        data = self._file_bytes(filename)
        result = "fail"
        reason = "file not found"
        if data:
            try:
                ingested = self.ingest_bytes(filename, data, {"print": cmd})
                if ingested.get("error") and not ingested.get("job"):
                    reason = ingested["error"]
                else:
                    result = "success"
                    reason = ""
            except Exception as exc:
                reason = str(exc)
        report = {
            "print": {
                "command": "project_file",
                "sequence_id": seq,
                "result": result,
                **({"reason": reason} if reason else {}),
            }
        }
        # Studio получила success — дальше очередь, шлюз снова IDLE.
        idle = self._push_status(seq)
        idle["print"]["gcode_state"] = "IDLE"
        return [report, idle]

    def _ams_status(self) -> dict:
        printer = self._bound_printer()
        if printer:
            try:
                snap = printer.snapshot()
                trays = (snap.get("ams") or {}).get("trays") or []
                if trays:
                    from .materials import bambu_filament_preset
                    tray_list = []
                    exist_bits = 0
                    bbl_bits = 0
                    for t in trays:
                        slot_n = int(t.get("slot") or 0)
                        if t.get("present"):
                            exist_bits |= (1 << slot_n)
                        if t.get("bambulab"):
                            bbl_bits |= (1 << slot_n)
                        preset = bambu_filament_preset(str(t.get("type") or "PLA"), "")
                        raw_color = str(t.get("color") or "FFFFFFFF").lstrip("#").upper()
                        if len(raw_color) == 6:
                            raw_color += "FF"
                        tray_list.append({
                            "id": slot_n,
                            "tray_type": preset["tray_type"],
                            "tray_color": raw_color,
                            "nozzle_temp_min": int(t.get("nozzle_min") or preset["nozzle_temp_min"]),
                            "nozzle_temp_max": int(t.get("nozzle_max") or preset["nozzle_temp_max"]),
                            "tray_info_idx": str(t.get("tray_info_idx") or preset["tray_info_idx"]),
                            "remain": int(t.get("remain") if t.get("remain") is not None else 100),
                            "tray_uuid": str(t.get("uuid") or ""),
                        })
                    return {
                        "ams": [{"id": 0, "humidity": 3, "temp": "24.0", "tray": tray_list}],
                        "ams_exist_bits": "1",
                        "tray_exist_bits": str(exist_bits),
                        "tray_is_bbl_bits": str(bbl_bits),
                        "tray_now": "255",
                    }
            except Exception:
                pass
        return {"ams": [], "ams_exist_bits": "0", "tray_now": "255"}

    def _push_status(self, seq: str = "0") -> dict:
        ident = self.identity()
        return {"print": {
            "command": "push_status",
            "sequence_id": str(seq),
            # msg: 0 — полный снимок состояния (не дифференциальное
            # обновление): Studio по нему целиком пересобирает карточку
            # принтера после pushall.
            "msg": 0,
            "gcode_state": "IDLE",
            "mc_percent": 0,
            "mc_remaining_time": 0,
            "wifi_signal": "-44dBm",
            "print_error": 0,
            "bed_temper": 0.0,
            "bed_target_temper": 0.0,
            "nozzle_temper": 0.0,
            "nozzle_target_temper": 0.0,
            "cooling_fan_speed": 0,
            "spd_mag": 100,
            "spd_lvl": 2,
            "lifecycle": "idle",
            "stg_cur": 0,
            "subtask_name": "",
            "layer_num": 0,
            "total_layer_num": 0,
            "hw_switch_state": 0,
            "home_flag": 0,
            "sdcard": True,
            "online": {"ahb": False, "rfid": False, "version": 0},
            "ams": self._ams_status(),
            "ipcam": {"ipcam_dev": "0", "ipcam_record": "disable"},
            "lights_report": [{"node": "chamber_light", "mode": "off"}],
            "upgrade_state": {"status": "IDLE"},
            "device": {"mode": ident["dev_model"]},
        }}

    def _version_report(self, seq: str) -> dict:
        serial = self.identity()["serial"]
        return {"info": {
            "command": "get_version",
            "sequence_id": str(seq),
            "result": "success",
            "module": [
                {"name": "ota", "sw_ver": "01.07.00.00", "hw_ver": "AP05", "sn": serial},
                {"name": "esp32", "sw_ver": "00.00.00.00", "hw_ver": "AP05", "sn": serial},
            ],
        }}

    def mqtt_handle_packet(self, packet: bytes) -> list[bytes]:
        """Разобрать один MQTT-пакет, вернуть список ответных пакетов."""
        try:
            ptype, flags, payload = parse_fixed_header(packet)
        except Exception:
            return []
        ident = self.identity()
        report_topic = f"device/{ident['serial']}/report"
        if ptype == CONNECT:
            try:
                info = decode_connect(payload)
            except Exception:
                self._mqtt_authed = False
                return [encode_connack(5)]
            user_ok = info.get("username") == MQTT_USER
            expected = self._access_code()
            pass_ok = bool(expected) and info.get("password") == expected
            rc = 0 if user_ok and pass_ok else 4
            self._mqtt_authed = rc == 0
            if rc == 0:
                with self._lock:
                    self._counters["mqtt_connections"] = \
                        int(self._counters.get("mqtt_connections", 0)) + 1
            else:
                self._note_auth_failure("mqtt")
            return [encode_connack(rc)]
        if ptype == PINGREQ:
            return [encode_pingresp()]
        if ptype == DISCONNECT:
            self._mqtt_authed = False
            return []
        if ptype == SUBSCRIBE:
            try:
                sub = decode_subscribe(payload)
            except Exception:
                return []
            qos = [0] * len(sub.get("filters") or [0])
            return [encode_suback(sub["packet_id"], qos or [0])]
        if ptype == UNSUBSCRIBE:
            try:
                unsub = decode_unsubscribe(payload)
            except Exception:
                return []
            return [encode_unsuback(unsub["packet_id"])]
        if ptype == PUBLISH:
            if not self._mqtt_authed and self.bind:
                return []
            try:
                pub = decode_publish(flags, payload)
            except Exception:
                return []
            replies: list[bytes] = []
            if pub.get("qos"):
                replies.append(encode_puback(pub["packet_id"]))
            raw = pub.get("payload") or b""
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            try:
                body = json.loads(text.strip("\x00") or "{}")
            except json.JSONDecodeError:
                body = {}
            if isinstance(body, dict):
                for report in self.handle_mqtt_request(body, pub.get("topic") or ""):
                    replies.append(encode_publish(
                        report_topic,
                        json.dumps(report, ensure_ascii=False, separators=(",", ":")),
                    ))
            return replies
        return []

    # ----------------------------------------------------------- FTP
    def ftp_command(self, line: str) -> str:
        """Одна FTP-команда → ответ. Без сокета."""
        text = (line or "").strip()
        if not text:
            return "500 Empty"
        parts = text.split(" ", 1)
        cmd = parts[0].upper()
        arg = parts[1] if len(parts) > 1 else ""
        if cmd == "USER":
            self._ftp_user = arg.strip()
            self._ftp_authed = False
            with self._lock:
                self._counters["ftp_connections"] = \
                    int(self._counters.get("ftp_connections", 0)) + 1
            return "331 Password required"
        if cmd == "PASS":
            expected = self._access_code()
            user_ok = self._ftp_user == MQTT_USER
            pass_ok = bool(expected) and arg == expected
            self._ftp_authed = user_ok and pass_ok
            if self._ftp_authed:
                return "230 Login successful"
            self._note_auth_failure("ftp")
            return "530 Login incorrect"
        if cmd == "QUIT":
            self._ftp_authed = False
            return "221 Goodbye"
        if cmd in ("AUTH", "PBSZ", "PROT"):
            return "234 OK" if cmd == "AUTH" else "200 OK"
        if cmd == "FEAT":
            return "211-Features\r\n UTF8\r\n PASV\r\n SIZE\r\n211 End"
        if cmd == "SYST":
            return "215 UNIX Type: L8"
        if cmd == "TYPE":
            return "200 Type set"
        if cmd == "NOOP":
            return "200 OK"
        if cmd in ("OPTS", "MODE", "STRU", "ALLO", "PORT"):
            return "200 OK"
        if cmd == "PWD" or cmd == "XPWD":
            return '257 "/"'
        if cmd in ("CWD", "XCWD"):
            self._ftp_cwd = "/" + arg.strip("/ ")
            return "250 Directory changed"
        if cmd == "PASV":
            return self._pasv_reply(20)
        if cmd == "EPSV":
            return "229 Entering Extended Passive Mode (|||20|)"
        if cmd in ("LIST", "NLST"):
            return "226 Transfer complete"
        if cmd == "SIZE":
            data = self._file_bytes(arg)
            if data is None:
                return "550 File not found"
            return f"213 {len(data)}"
        if cmd in ("STOR", "APPE", "STOU"):
            if not self._ftp_authed and self.bind:
                return "530 Please login"
            self._stor_name = Path(arg.replace("\\", "/")).name or "upload.bin"
            return "150 Ok to send data"
        if cmd == "DELE":
            return "250 Deleted"
        if cmd in ("MKD", "XMKD"):
            return '257 "/"'
        if cmd == "RNFR":
            return "350 Ready"
        if cmd == "RNTO":
            return "250 Renamed"
        return "502 Command not implemented"

    def ftp_apply(self, filename: str, data: bytes) -> dict:
        """STOR без сети: сохранить во входящие и ingest."""
        name = Path(str(filename or "upload.bin").replace("\\", "/")).name
        with self._lock:
            self._incoming[name] = data
            if len(self._incoming) > 32:
                extra = list(self._incoming)[:-24]
                for key in extra:
                    self._incoming.pop(key, None)
        return self.ingest_bytes(name, data)

    # ----------------------------------------------------------- жизненный цикл
    def start(self) -> None:
        if not self.bind or not self._enabled():
            return
        if self._mqtt_sock or self._ftp_sock or self._ssdp_sock or self._bind_socks:
            return
        self._stop.clear()
        self._ensure_identity()
        self._errors = {"ssdp": "", "mqtt": "", "ftps": "", "bind": ""}
        self.last_error = ""
        self._ssdp_note = ""
        self._ssdp_bound_port = 0
        try:
            self._start_ssdp()
        except Exception as exc:
            self._record_error("SSDP", exc)
        cert = key = None
        try:
            from .studio_tls import ensure_certificate
            # Сертификат — на серийный номер: так же делает прошивка принтера
            # (leaf CN=<serial>), и Studio при желании сверяет именно его.
            cert, key = ensure_certificate(self.identity()["serial"])
        except Exception as exc:
            self._record_error("TLS", exc)
            return
        self._tls_ctx = self._tls_context(cert, key)
        try:
            self._start_bind(cert, key)
        except Exception as exc:
            self._record_error("bind", exc)
        try:
            self._start_mqtt(cert, key)
        except Exception as exc:
            self._record_error("MQTT", exc)
        try:
            self._start_ftp(cert, key)
        except Exception as exc:
            self._record_error("FTPS", exc)

    def stop(self) -> None:
        self._stop.set()
        for sock in (self._ssdp_sock, self._mqtt_sock, self._ftp_sock,
                     *self._bind_socks):
            if sock is None:
                continue
            try:
                sock.close()
            except Exception:
                pass
        self._ssdp_sock = self._mqtt_sock = self._ftp_sock = None
        self._ssdp_bound_port = 0
        self._bind_socks = []
        self._threads = []

    def reload(self) -> None:
        self.stop()
        if self._enabled() and self.bind:
            self.start()

    def _spawn(self, name: str, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _start_ssdp(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        # Слушаем только 1900 (стандартный порт SSDP, туда Studio и прочие
        # искалки шлют M-SEARCH). UDP 2021 не занимаем никогда: там слушает
        # сетевой плагин Studio, и вторая розетка на том же порту отбирает у
        # него входящие объявления — Studio остаётся без адреса принтера и
        # отдаёт «код=-1». Если 1900 занят целиком (Windows-служба SSDP
        # Discovery без SO_REUSEADDR), берём произвольный порт и честно
        # показываем это в диагностике.
        bound_port = 0
        for port in SSDP_LISTEN_PORTS:
            try:
                sock.bind(("", port))
                bound_port = port
                break
            except OSError:
                continue
        if not bound_port:
            sock.bind(("", 0))
            bound_port = int(sock.getsockname()[1])
            # Это не сбой: объявления (главный канал) уходят как обычно,
            # теряется только ответ на чужой поиск M-SEARCH.
            self._ssdp_note = (
                "порт 1900 занят другой программой — M-SEARCH слушаем "
                f"на :{bound_port}, объявления Studio всё равно получает")
        self._ssdp_bound_port = bound_port
        try:
            mreq = struct.pack("4s4s", socket.inet_aton(SSDP_GROUP), socket.inet_aton("0.0.0.0"))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass
        sock.settimeout(1.0)
        self._ssdp_sock = sock
        self._spawn("pf-studio-ssdp", self._ssdp_loop, sock)

    def _ssdp_loop(self, sock) -> None:
        while not self._stop.is_set():
            # Рассылка объявления — не должна ронять цикл при ошибке БД/кодирования.
            try:
                self._broadcast_notify()
            except Exception:
                # _broadcast_notify уже логирует, но падение потока недопустимо
                pass
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception:
                continue
            text = data.decode("utf-8", "replace")
            if "M-SEARCH" not in text:
                continue
            if SSDP_NT not in text and "ssdp:all" not in text.lower():
                continue
            try:
                sock.sendto(self.ssdp_search_response().encode("utf-8"), addr)
            except (OSError, Exception):
                continue

    def _local_ips(self) -> list[str]:
        """Все IPv4-адреса этого ПК, кроме loopback (для адресной рассылки).

        Список кэшируется: get_local_ips() пробует соединения с таймаутами, а
        _notify_targets() спрашивают и рассылка, и карточка настроек.
        """
        now = time.time()
        if self._ips_cache and now - self._ips_cache_at < 30.0:
            return self._ips_cache
        try:
            from .config import get_local_ips
            ips = [ip for ip in get_local_ips() if ip and not ip.startswith("127.")]
        except Exception:
            ips = []
        self._ips_cache = ips
        self._ips_cache_at = now
        return ips

    def _notify_targets(self) -> list[tuple[str, int]]:
        """Куда шлём NOTIFY, чтобы Studio увидел шлюз на любой машине.

        Порядок — от самого надёжного канала к самому капризному:

        1. **loopback 127.0.0.1:2021** — сюда смотрит сетевой плагин Studio,
           когда Studio стоит на том же компьютере, что и PrintFlow. Loopback
           не фильтруется брандмауэром Windows вообще, поэтому объявление
           доходит при любых правилах. Это же делает известный обходной путь
           «отправить фальшивое объявление на 127.0.0.1:2021» — только здесь
           отправляет сам шлюз, а не сторонний скрипт;
        2. **свой сетевой адрес :2021** — та же доставка, но через интерфейс
           LAN (Windows считает трафик на собственный адрес локальным и
           брандмауэром его не режет);
        3. **направленный broadcast x.y.z.255:2021** и **255.255.255.255:2021**
           — как шлёт настоящий станок, чтобы Studio увидел шлюз и с другого
           компьютера в сети;
        4. **группа 239.255.255.250** (2021/1990/1900) — для старых клиентов
           и сторонних искалок (OrcaSlicer, Home Assistant).
        """
        host = self._host_ip()
        targets: list[tuple[str, int]] = [
            (SSDP_NOTIFY_LOOPBACK, SSDP_PORTS[0]),
            (SSDP_NOTIFY_LOOPBACK, SSDP_PORTS[2]),
        ]
        if host and not host.startswith("127."):
            targets.append((host, SSDP_PORTS[0]))
        for ip in self._local_ips():
            targets.append((ip, SSDP_PORTS[0]))
        directed = directed_broadcast(host)
        if directed:
            targets.append((directed, SSDP_PORTS[0]))
        targets.append((SSDP_BROADCAST, SSDP_PORTS[0]))
        targets.extend((SSDP_GROUP, port) for port in SSDP_PORTS)
        unique: list[tuple[str, int]] = []
        for target in targets:
            if target not in unique:
                unique.append(target)
        return unique

    def _broadcast_notify(self) -> None:
        now = time.time()
        if now - self._last_notify < SSDP_NOTIFY_PERIOD:
            return
        self._last_notify = now
        try:
            payload = self.ssdp_notify().encode("utf-8")
        except Exception:
            # БД закрыта или другая ошибка — пропустим этот цикл, но не роняем SSDP
            return
        # Сокет один на весь цикл: объявление уходит на десяток адресов
        # (loopback, свой адрес, broadcast, группа) — открывать по сокету на
        # каждый адрес незачем. SO_BROADCAST нужен только широковещательным
        # адресам, для loopback и группы он безвреден.
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        except OSError:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        try:
            for host, port in self._notify_targets():
                try:
                    sock.sendto(payload, (host, port))
                except OSError:
                    # Один недостижимый адрес (например, направленный
                    # broadcast в чужой подсети) не должен отменять остальные.
                    continue
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _tls_context(self, cert, key):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        try:
            # Studio приходит с набором, где есть AES-GCM; на сборках OpenSSL
            # с жёстким DEFAULT (и на Windows) без явного перечисления
            # рукопожатие может не состояться — тогда Studio молчит «код=-1».
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.set_ciphers("DEFAULT:AES256-GCM-SHA384:AES128-GCM-SHA256")
        except (ssl.SSLError, ValueError):
            pass
        return ctx

    def _start_mqtt(self, cert, key) -> None:
        sock = self._listen_tcp(MQTT_PORT, 8, tls=True, cert=cert, key=key)
        self._mqtt_sock = sock
        self._spawn("pf-studio-mqtt", self._accept_loop, sock, self._mqtt_client)

    def _start_ftp(self, cert, key) -> None:
        sock = self._listen_tcp(FTP_PORT, 4, tls=True, cert=cert, key=key)
        self._ftp_sock = sock
        self._spawn("pf-studio-ftps", self._accept_loop, sock, self._ftp_client)

    def _note_dropped_connection(self, exc: Exception | str) -> None:
        """Соединение отброшено (не TLS, обрыв, сбой рукопожатия) — не сбой службы."""
        with self._lock:
            self._counters["dropped_connections"] = int(
                self._counters.get("dropped_connections", 0)) + 1
        try:
            from .logging_setup import log
            log().debug("Шлюз Bambu Studio: соединение отброшено: %s", exc)
        except Exception:
            pass

    def _accept_loop(self, sock, handler) -> None:
        """Приём соединений: сбой одного клиента не должен гасить службу.

        accept() на TLS-розетке сам выполняет рукопожатие. Клиент, пришедший
        без TLS (проверка «открыт ли порт», сканер сети, антивирус, оборванное
        соединение Studio), раньше убивал цикл целиком: порт оставался в
        состоянии LISTEN, но новых клиентов уже не принимал — Studio висела по
        таймауту и показывала «код=-1» на исправном шлюзе. Теперь такое
        соединение просто отбрасывается, а счётчик виден в статусе.
        """
        fatal_errno = {errno.EBADF, errno.EINVAL, errno.ENOTSOCK, errno.EOPNOTSUPP}
        consecutive = 0
        while not self._stop.is_set():
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                consecutive = 0
                continue
            except (ssl.SSLError, ConnectionError) as exc:
                # Почти всегда: клиент пришёл без TLS или оборвал рукопожатие.
                self._note_dropped_connection(exc)
                consecutive += 1
                if consecutive >= 100:
                    self._record_error("bind" if handler == self._bind_client else "mqtt",
                                       f"приём соединений срывается: {exc}")
                    break
                continue
            except OSError as exc:
                if self._stop.is_set() or getattr(exc, "errno", None) in fatal_errno:
                    break
                self._note_dropped_connection(exc)
                consecutive += 1
                if consecutive >= 100:
                    break
                continue
            consecutive = 0
            self._spawn("pf-studio-conn", handler, conn)

    def _mqtt_client(self, conn) -> None:
        self._mqtt_authed = False
        try:
            with self._lock:
                try:
                    self._last_client = conn.getpeername()[0]
                except Exception:
                    pass
            conn.settimeout(90)
            while not self._stop.is_set():
                try:
                    ptype, flags, payload = read_packet(conn.recv)
                except (socket.timeout, TimeoutError):
                    continue
                except (ConnectionError, OSError, ssl.SSLError):
                    break
                header = bytes([((ptype & 0x0F) << 4) | (flags & 0x0F)])
                from .studio_mqtt import encode_remaining_length
                packet = header + encode_remaining_length(len(payload)) + payload
                for reply in self.mqtt_handle_packet(packet):
                    try:
                        conn.sendall(reply)
                    except OSError:
                        return
                if ptype == DISCONNECT:
                    break
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _ftp_readline(self, conn) -> str:
        buf = bytearray()
        while len(buf) < 4096:
            chunk = conn.recv(1)
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b"\n"):
                break
        return buf.decode("utf-8", "replace").strip()

    def _pasv_reply(self, port: int) -> str:
        """Ответ на PASV: адрес из _host_ip() (закреплённый или авто) и порт.

        Если объявить в PASV виртуальный адрес, Studio зальёт файл не туда —
        поэтому здесь тот же источник адреса, что и в SSDP.
        """
        host = self._host_ip().split(".")
        if len(host) != 4:
            host = ["127", "0", "0", "1"]
        p1, p2 = divmod(int(port) % 65536, 256)
        return (
            f"227 Entering Passive Mode "
            f"({host[0]},{host[1]},{host[2]},{host[3]},{p1},{p2})"
        )

    def _data_channel(self, conn, timeout: int = 30, peek_timeout: float = 2.0):
        """Канал данных PASV: TLS, если клиент начинает рукопожатие.

        Клиенты Bambu расходятся: одни поднимают TLS на канале данных сразу
        после connect, другие (например P2S) только дожидаются ``150`` и
        начинают рукопожатие потом. Поэтому тип канала определяем по первому
        байту, не съедая его (MSG_PEEK): начинается с 0x16 — оборачиваем в
        TLS, иначе читаем как есть. Ожидание первого байта короткое: клиент
        подключается к каналу данных только когда готов слать, но ждать его
        секундами нельзя — иначе заливка встанет на таймауте.
        """
        try:
            conn.settimeout(timeout)
        except OSError:
            return conn
        if self._tls_ctx is None:
            return conn
        try:
            conn.settimeout(peek_timeout)
            first = conn.recv(1, socket.MSG_PEEK)
            conn.settimeout(timeout)
        except (socket.timeout, TimeoutError, OSError):
            return conn
        if not first or first[0] != 0x16:
            return conn
        try:
            return self._tls_ctx.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError):
            return conn

    def _accept_data(self, sock) -> None:
        """Принять канал данных PASV в фоне (клиент может прийти до STOR)."""
        try:
            conn, _addr = sock.accept()
        except (socket.timeout, TimeoutError, OSError):
            return
        conn = self._data_channel(conn)
        with self._lock:
            self._data_conn = conn
        self._data_ready.set()

    def _ftp_client(self, conn) -> None:
        self._ftp_user = ""
        self._ftp_authed = False
        pasv_sock = None
        try:
            conn.settimeout(90)
            conn.sendall(b"220 PrintFlow Studio Gateway\r\n")
            stor_name = ""
            while not self._stop.is_set():
                try:
                    line = self._ftp_readline(conn)
                except (socket.timeout, TimeoutError):
                    continue
                except (ConnectionError, OSError, ssl.SSLError):
                    break
                if not line:
                    break
                reply = self.ftp_command(line)
                cmd = line.split(" ", 1)[0].upper()
                arg = line.split(" ", 1)[1] if " " in line else ""
                if cmd == "PASV":
                    if pasv_sock:
                        try:
                            pasv_sock.close()
                        except Exception:
                            pass
                    self._data_conn = None
                    self._data_ready.clear()
                    pasv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    pasv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    pasv_sock.bind(("0.0.0.0", 0))
                    pasv_sock.listen(1)
                    pasv_sock.settimeout(30)
                    port = pasv_sock.getsockname()[1]
                    reply = self._pasv_reply(port)
                    # Клиент может подключиться к каналу данных и до STOR —
                    # приём ждёт в отдельном потоке, иначе рукопожатие
                    # раннего клиента подвиснет до нашего accept().
                    self._spawn("pf-studio-pasv", self._accept_data, pasv_sock)
                if cmd in ("STOR", "APPE"):
                    stor_name = Path(arg.replace("\\", "/")).name or "upload.bin"
                try:
                    conn.sendall((reply + "\r\n").encode("utf-8"))
                except OSError:
                    break
                if cmd in ("STOR", "APPE") and pasv_sock and reply.startswith("150"):
                    data_conn = None
                    try:
                        self._data_ready.wait(30)
                        data_conn = self._data_conn
                        self._data_conn = None
                        if data_conn is None:
                            raise ConnectionError("клиент не открыл канал данных")
                        chunks = []
                        while True:
                            chunk = data_conn.recv(65536)
                            if not chunk:
                                break
                            chunks.append(chunk)
                        blob = b"".join(chunks)
                        self.ftp_apply(stor_name, blob)
                        conn.sendall(b"226 Transfer complete\r\n")
                    except Exception:
                        try:
                            conn.sendall(b"426 Transfer aborted\r\n")
                        except OSError:
                            break
                    finally:
                        if data_conn:
                            try:
                                data_conn.close()
                            except Exception:
                                pass
                if cmd == "QUIT":
                    break
        except Exception:
            pass
        finally:
            if self._data_conn:
                try:
                    self._data_conn.close()
                except Exception:
                    pass
            self._data_conn = None
            if pasv_sock:
                try:
                    pasv_sock.close()
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass

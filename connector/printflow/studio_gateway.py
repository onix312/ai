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

import json
import re
import secrets
import socket
import ssl
import struct
import threading
import time
from pathlib import Path

from .config import UPLOAD_DIR, get_local_ips
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
SSDP_PORTS = (1900, 1990, 2021)
MQTT_PORT = 8883
FTP_PORT = 990
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
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._incoming: dict[str, bytes] = {}
        self._mqtt_authed = False
        self._ftp_user = ""
        self._ftp_authed = False
        self._ftp_cwd = "/"
        self._stor_name = ""
        self._ssdp_sock = None
        self._mqtt_sock = None
        self._ftp_sock = None
        self._threads: list[threading.Thread] = []
        self._last_notify = 0.0
        self._host_cache = ""

    # ----------------------------------------------------------- настройки
    def _enabled(self) -> bool:
        return bool(self.db.setting("studio_gateway_enabled", False))

    def _access_code(self) -> str:
        return str(self.db.setting("studio_gateway_access_code", "") or "")

    def _autostart_allowed(self) -> bool:
        mode = str(self.db.setting("studio_gateway_mode", "queue") or "queue").strip().lower()
        return (
            mode == "autostart"
            and bool(self.db.setting("studio_gateway_autostart", False))
            and bool(self.db.setting("unattended_dangerous_actions", False))
        )

    def _ensure_identity(self) -> None:
        serial = str(self.db.setting("studio_gateway_serial", "") or "").strip()
        patch: dict[str, object] = {}
        if not serial:
            patch["studio_gateway_serial"] = _new_serial()
        if not self._access_code():
            patch["studio_gateway_access_code"] = secrets.token_hex(4)
        if patch:
            self.db.set_settings(patch)

    def _host_ip(self) -> str:
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

    def _bound_printer(self):
        if not self.manager or not hasattr(self.manager, "get"):
            return None
        pid = str(self.db.setting("studio_gateway_printer_id", "") or "").strip()
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
        self._ensure_identity()
        model = self._printer_model()
        serial = str(self.db.setting("studio_gateway_serial", "") or "").strip()
        name = str(self.db.setting("studio_gateway_name", "NOZZA-PrintFlow")
                   or "NOZZA-PrintFlow").strip() or "NOZZA-PrintFlow"
        return {
            "serial": serial,
            "name": name,
            "model": model,
            "dev_model": _dev_model(model),
            "host": self._host_ip(),
            "mqtt_port": MQTT_PORT,
            "ftp_port": FTP_PORT,
        }

    def status(self) -> dict:
        ident = self.identity()
        out = {
            "enabled": self._enabled(),
            "running": bool(self._mqtt_sock or self._ftp_sock or self._ssdp_sock),
            "bind": self.bind,
            "mode": str(self.db.setting("studio_gateway_mode", "queue") or "queue"),
            "autostart": bool(self.db.setting("studio_gateway_autostart", False)),
            "autostart_allowed": self._autostart_allowed(),
            "printer_id": str(self.db.setting("studio_gateway_printer_id", "") or ""),
            "mqtt_port": MQTT_PORT,
            "ftp_port": FTP_PORT,
            "ssdp_ports": list(SSDP_PORTS),
            "last_error": self.last_error,
            "has_access_code": bool(self._access_code()),
            "urn": SSDP_NT,
        }
        out.update(ident)
        return out

    # ----------------------------------------------------------- SSDP
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
            f"DevModel.bambu.com: {ident['dev_model']}\r\n"
            f"DevName.bambu.com: {ident['name']}\r\n"
            "DevSignal.bambu.com: -44\r\n"
            "DevConnect.bambu.com: lan\r\n"
            "DevBind.bambu.com: free\r\n"
            "\r\n"
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
            f"DevModel.bambu.com: {ident['dev_model']}\r\n"
            f"DevName.bambu.com: {ident['name']}\r\n"
            "DevSignal.bambu.com: -44\r\n"
            "DevConnect.bambu.com: lan\r\n"
            "DevBind.bambu.com: free\r\n"
            "\r\n"
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
            if filaments and printer and bool(self.db.setting("ams_auto_map", True)):
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
        setting_pid = str(self.db.setting("studio_gateway_printer_id", "") or "").strip()
        if setting_pid:
            printer_id = setting_pid
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
        if self.manager and hasattr(self.manager, "enqueue") and can_print(name):
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
        try:
            self.db.add_event(
                "studio", "Файл из Bambu Studio",
                name, printer_id,
                {"library_id": rec.get("id"), "job_id": (job or {}).get("id"),
                 "autostart": autostart},
            )
        except Exception:
            pass
        if self.bus:
            try:
                self.bus.publish("studio", {
                    "file": name, "library_id": rec.get("id"),
                    "job_id": (job or {}).get("id"),
                })
            except Exception:
                pass
        return {
            "library": rec,
            "job": job,
            "estimate": estimate,
            "autostart": autostart,
            "error": error,
        }

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
            reports.append({
                "system": {
                    "command": system.get("command"),
                    "sequence_id": str(system.get("sequence_id") or "0"),
                    "result": "success",
                }
            })
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

    def _push_status(self, seq: str = "0") -> dict:
        ident = self.identity()
        return {"print": {
            "command": "push_status",
            "sequence_id": str(seq),
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
            "ams": {"ams": [], "ams_exist_bits": "0", "tray_now": "255"},
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
            return "331 Password required"
        if cmd == "PASS":
            expected = self._access_code()
            user_ok = self._ftp_user == MQTT_USER
            pass_ok = bool(expected) and arg == expected
            self._ftp_authed = user_ok and pass_ok
            return "230 Login successful" if self._ftp_authed else "530 Login incorrect"
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
            return "227 Entering Passive Mode (127,0,0,1,0,20)"
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
        if self._mqtt_sock or self._ftp_sock or self._ssdp_sock:
            return
        self._stop.clear()
        self._ensure_identity()
        self.last_error = ""
        try:
            self._start_ssdp()
        except Exception as exc:
            self.last_error = f"SSDP: {exc}"
        cert = key = None
        try:
            from .studio_tls import ensure_certificate
            cert, key = ensure_certificate(self.identity()["name"])
        except Exception as exc:
            self.last_error = str(exc)
            return
        try:
            self._start_mqtt(cert, key)
        except Exception as exc:
            self.last_error = f"MQTT: {exc}"
        try:
            self._start_ftp(cert, key)
        except Exception as exc:
            self.last_error = f"FTPS: {exc}"

    def stop(self) -> None:
        self._stop.set()
        for sock in (self._ssdp_sock, self._mqtt_sock, self._ftp_sock):
            if sock is None:
                continue
            try:
                sock.close()
            except Exception:
                pass
        self._ssdp_sock = self._mqtt_sock = self._ftp_sock = None
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
        try:
            sock.bind(("", 1900))
        except OSError:
            sock.bind(("", 0))
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
            try:
                self._broadcast_notify()
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", "replace")
            if "M-SEARCH" not in text:
                continue
            if SSDP_NT not in text and "ssdp:all" not in text.lower():
                continue
            try:
                sock.sendto(self.ssdp_search_response().encode("utf-8"), addr)
            except OSError:
                continue

    def _broadcast_notify(self) -> None:
        now = time.time()
        if now - self._last_notify < 12:
            return
        self._last_notify = now
        payload = self.ssdp_notify().encode("utf-8")
        for port in SSDP_PORTS:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.sendto(payload, (SSDP_GROUP, port))
                sock.close()
            except OSError:
                continue

    def _tls_context(self, cert, key):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        return ctx

    def _start_mqtt(self, cert, key) -> None:
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        raw.bind(("0.0.0.0", MQTT_PORT))
        raw.listen(8)
        raw.settimeout(1.0)
        ctx = self._tls_context(cert, key)
        sock = ctx.wrap_socket(raw, server_side=True)
        self._mqtt_sock = sock
        self._spawn("pf-studio-mqtt", self._accept_loop, sock, self._mqtt_client)

    def _start_ftp(self, cert, key) -> None:
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        raw.bind(("0.0.0.0", FTP_PORT))
        raw.listen(4)
        raw.settimeout(1.0)
        ctx = self._tls_context(cert, key)
        sock = ctx.wrap_socket(raw, server_side=True)
        self._ftp_sock = sock
        self._spawn("pf-studio-ftps", self._accept_loop, sock, self._ftp_client)

    def _accept_loop(self, sock, handler) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._spawn("pf-studio-conn", handler, conn)

    def _mqtt_client(self, conn) -> None:
        self._mqtt_authed = False
        try:
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
                    pasv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    pasv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    pasv_sock.bind(("0.0.0.0", 0))
                    pasv_sock.listen(1)
                    pasv_sock.settimeout(30)
                    host = self._host_ip().split(".")
                    if len(host) != 4:
                        host = ["127", "0", "0", "1"]
                    port = pasv_sock.getsockname()[1]
                    p1, p2 = divmod(port, 256)
                    reply = (
                        f"227 Entering Passive Mode "
                        f"({host[0]},{host[1]},{host[2]},{host[3]},{p1},{p2})"
                    )
                if cmd in ("STOR", "APPE"):
                    stor_name = Path(arg.replace("\\", "/")).name or "upload.bin"
                try:
                    conn.sendall((reply + "\r\n").encode("utf-8"))
                except OSError:
                    break
                if cmd in ("STOR", "APPE") and pasv_sock and reply.startswith("150"):
                    data_conn = None
                    try:
                        data_conn, _ = pasv_sock.accept()
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
            if pasv_sock:
                try:
                    pasv_sock.close()
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass

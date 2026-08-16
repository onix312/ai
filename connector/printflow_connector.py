#!/usr/bin/env python3
"""PrintFlow local connector for Bambu Lab P1S.

Serves the static site and bridges browser requests to the printer's local
MQTT/TLS and camera protocols. Secrets are stored in the user's application
data directory, never in the repository or browser localStorage.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import socket
import ssl
import struct
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import paho.mqtt.client as mqtt
except ImportError:  # shown as a useful health error in the UI
    mqtt = None

APP_VERSION = "0.1.0"
ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
if os.name == "nt":
    DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "PrintFlow"
else:
    DATA_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "printflow"
CONFIG_FILE = DATA_DIR / "config.json"
HISTORY_FILE = DATA_DIR / "printer-history.jsonl"
DEFAULT_CONFIG = {
    "host": "", "serial": "", "access_code": "", "name": "Bambu Lab P1S",
    "telegram_enabled": False, "telegram_token": "", "telegram_chat_id": "",
    "notify_complete": True, "notify_error": True, "notify_pause": True,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_config() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_FILE.read_text("utf-8"))
        return {**DEFAULT_CONFIG, **data}
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe = {k: data.get(k, v) for k, v in DEFAULT_CONFIG.items()}
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(safe, ensure_ascii=False, indent=2), "utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(CONFIG_FILE)


def deep_merge(base: dict, patch: dict) -> dict:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def as_num(value: Any, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class CameraWorker:
    def __init__(self, bridge: "PrinterBridge"):
        self.bridge = bridge
        self.frame: bytes | None = None
        self.frame_at = 0.0
        self.error = ""
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="P1-camera", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    @staticmethod
    def _auth(code: str) -> bytes:
        return struct.pack("<IIII", 0x40, 0x3000, 0, 0) + b"bblp".ljust(32, b"\0") + code.encode("ascii").ljust(32, b"\0")

    def _run(self):
        while not self.stop_event.is_set():
            cfg = self.bridge.config
            if not cfg.get("host") or not cfg.get("access_code"):
                self.stop_event.wait(2)
                continue
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                with socket.create_connection((cfg["host"], 6000), timeout=8) as raw:
                    with context.wrap_socket(raw, server_hostname=cfg["host"]) as sock:
                        sock.settimeout(10)
                        sock.sendall(self._auth(cfg["access_code"]))
                        buf = bytearray()
                        self.error = ""
                        while not self.stop_event.is_set():
                            chunk = sock.recv(65536)
                            if not chunk:
                                raise ConnectionError("камера закрыла соединение")
                            buf.extend(chunk)
                            while True:
                                start = buf.find(b"\xff\xd8\xff")
                                if start < 0:
                                    if len(buf) > 2_000_000:
                                        del buf[:-4]
                                    break
                                end = buf.find(b"\xff\xd9", start + 3)
                                if end < 0:
                                    if start:
                                        del buf[:start]
                                    break
                                self.frame = bytes(buf[start:end + 2])
                                self.frame_at = time.time()
                                del buf[:end + 2]
            except Exception as exc:
                self.error = str(exc)
                self.stop_event.wait(3)


class PrinterBridge:
    def __init__(self):
        self.lock = threading.RLock()
        self.config = load_config()
        self.client = None
        self.connected = False
        self.connecting = False
        self.last_message = 0.0
        self.last_error = ""
        self.raw: dict[str, Any] = {}
        self.version: list[dict] = []
        self.events = deque(maxlen=500)
        self.previous_state = ""
        self.previous_error = ""
        self.print_session: dict[str, Any] | None = None
        self.sequence = 0
        self.job_links: dict[str, dict] = {}
        self.command_results = deque(maxlen=50)
        self.camera = CameraWorker(self)
        self._load_history()
        if self.config.get("host") and self.config.get("serial") and self.config.get("access_code"):
            self.connect()
        self.camera.start()

    def _load_history(self):
        try:
            for line in HISTORY_FILE.read_text("utf-8").splitlines()[-500:]:
                self.events.append(json.loads(line))
        except Exception:
            pass

    def add_event(self, kind: str, title: str, detail: str = "", data: dict | None = None):
        event = {"id": f"{int(time.time()*1000)}-{kind}", "at": now_iso(), "kind": kind, "title": title, "detail": detail, "data": data or {}}
        self.events.append(event)
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with HISTORY_FILE.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass
        if kind == "complete" and self.config.get("notify_complete"):
            self.notify_telegram(f"✅ {title}\n{detail}")
        elif kind == "error" and self.config.get("notify_error"):
            self.notify_telegram(f"🚨 {title}\n{detail}")
        elif kind == "pause" and self.config.get("notify_pause"):
            self.notify_telegram(f"⏸ {title}\n{detail}")

    def notify_telegram(self, text: str):
        cfg = self.config
        if not (cfg.get("telegram_enabled") and cfg.get("telegram_token") and cfg.get("telegram_chat_id")):
            return
        def send():
            try:
                body = json.dumps({"chat_id": cfg["telegram_chat_id"], "text": "PrintFlow · " + text}).encode()
                req = urllib.request.Request(f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage", body, {"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10).read()
            except Exception as exc:
                self.last_error = "Telegram: " + str(exc)
        threading.Thread(target=send, daemon=True).start()

    def public_config(self):
        cfg = dict(self.config)
        cfg["access_code"] = "" if not cfg.get("access_code") else "••••••••"
        cfg["telegram_token"] = "" if not cfg.get("telegram_token") else "••••••••"
        cfg["has_access_code"] = bool(self.config.get("access_code"))
        cfg["has_telegram_token"] = bool(self.config.get("telegram_token"))
        return cfg

    def set_config(self, incoming: dict):
        cfg = dict(self.config)
        for key in DEFAULT_CONFIG:
            if key not in incoming:
                continue
            value = incoming[key]
            if key in ("access_code", "telegram_token") and (not value or value == "••••••••"):
                continue
            cfg[key] = value
        cfg["host"] = str(cfg["host"]).strip()
        cfg["serial"] = str(cfg["serial"]).strip()
        save_config(cfg)
        self.config = cfg
        self.disconnect()
        self.camera.stop()
        self.camera = CameraWorker(self)
        self.camera.start()
        if cfg["host"] and cfg["serial"] and cfg["access_code"]:
            self.connect()
        return self.public_config()

    def connect(self):
        if mqtt is None:
            self.last_error = "Не установлен paho-mqtt. Запустите BAT-файл повторно или выполните pip install -r connector/requirements.txt"
            return
        self.disconnect()
        cfg = self.config
        if not all(cfg.get(x) for x in ("host", "serial", "access_code")):
            return
        try:
            self.connecting = True
            try:
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"printflow-{int(time.time())}", protocol=mqtt.MQTTv311)
            except (AttributeError, TypeError):
                client = mqtt.Client(client_id=f"printflow-{int(time.time())}", protocol=mqtt.MQTTv311)
            client.username_pw_set("bblp", cfg["access_code"])
            client.tls_set(cert_reqs=ssl.CERT_NONE)
            client.tls_insecure_set(True)
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message
            self.client = client
            client.connect_async(cfg["host"], 8883, keepalive=30)
            client.loop_start()
        except Exception as exc:
            self.connecting = False
            self.last_error = str(exc)

    def disconnect(self):
        old, self.client = self.client, None
        self.connected = False
        self.connecting = False
        if old:
            try:
                old.disconnect(); old.loop_stop()
            except Exception:
                pass

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        ok = int(reason_code) == 0
        self.connected, self.connecting = ok, False
        if not ok:
            self.last_error = f"MQTT отклонил подключение: {reason_code}"
            return
        self.last_error = ""
        client.subscribe(f"device/{self.config['serial']}/report")
        self.publish({"pushing": {"sequence_id": self.next_seq(), "command": "pushall", "version": 1, "push_target": 1}})
        self.publish({"info": {"sequence_id": self.next_seq(), "command": "get_version"}})
        self.add_event("online", "Принтер подключён", self.config.get("name", "P1S"))

    def _on_disconnect(self, client, userdata, disconnect_flags=None, reason_code=0, properties=None):
        self.connected = False
        if int(reason_code or 0) != 0:
            self.last_error = f"Соединение потеряно: {reason_code}"

    def _on_message(self, client, userdata, message):
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except Exception:
            return
        with self.lock:
            if isinstance(payload.get("print"), dict):
                deep_merge(self.raw.setdefault("print", {}), payload["print"])
            if isinstance(payload.get("info", {}).get("module"), list):
                self.version = payload["info"]["module"]
            for group, value in payload.items():
                if group not in ("print",) and isinstance(value, dict):
                    deep_merge(self.raw.setdefault(group, {}), value)
            self.last_message = time.time()
            self._detect_events()

    def _detect_events(self):
        p = self.raw.get("print", {})
        state = str(p.get("gcode_state", "")).upper()
        name = p.get("subtask_name") or p.get("gcode_file") or "Задание печати"
        if state != self.previous_state:
            old = self.previous_state
            self.previous_state = state
            if state in ("RUNNING", "PREPARE"):
                if not self.print_session:
                    self.print_session = {
                        "started_ts": time.time(), "started_at": now_iso(), "task": name,
                        "order": self.current_link(name), "start_layer": p.get("layer_num", 0),
                        "nozzle_target": p.get("nozzle_target_temper"), "bed_target": p.get("bed_target_temper"),
                        "ams_tray": (p.get("ams") or {}).get("tray_now") if isinstance(p.get("ams"), dict) else None,
                    }
                    self.add_event("start", "Печать началась", name, self.print_session)
            elif state in ("PAUSE", "PAUSED"):
                self.add_event("pause", "Печать приостановлена", name, self._session_data(p))
            elif state in ("FINISH", "COMPLETED"):
                data = self._session_data(p)
                self.add_event("complete", "Печать завершена", self._session_detail(name, data), data)
                self.print_session = None
            elif state in ("FAILED", "ERROR"):
                data = self._session_data(p)
                self.add_event("error", "Ошибка печати", self._session_detail(name, data), data)
            elif state == "IDLE" and old in ("RUNNING", "PAUSE", "PAUSED"):
                data = self._session_data(p)
                self.add_event("stop", "Печать остановлена", self._session_detail(name, data), data)
                self.print_session = None
        err = str(p.get("print_error") or "")
        if err and err not in ("0", self.previous_error):
            self.previous_error = err
            self.add_event("error", "Принтер сообщил об ошибке", err, {"hms": p.get("hms", [])})

    def _session_data(self, print_data: dict) -> dict:
        data = dict(self.print_session or {})
        if data.get("started_ts"):
            data["duration_min"] = round((time.time() - data["started_ts"]) / 60, 1)
        data.update({
            "ended_at": now_iso(), "progress": print_data.get("mc_percent"),
            "layer": print_data.get("layer_num"), "total_layers": print_data.get("total_layer_num"),
            "nozzle": print_data.get("nozzle_temper"), "bed": print_data.get("bed_temper"),
        })
        task = print_data.get("subtask_name") or print_data.get("gcode_file") or data.get("task", "")
        if not data.get("order"):
            data["order"] = self.current_link(task)
        return data

    @staticmethod
    def _session_detail(name: str, data: dict) -> str:
        duration = data.get("duration_min")
        return f"{name} · {duration:.0f} мин" if isinstance(duration, (int, float)) else name

    def current_link(self, filename: str):
        clean = str(filename).lower()
        for key, value in self.job_links.items():
            if key.lower() in clean:
                return value
        return {}

    def link_job(self, filename: str, order_id: str, order_number: str, product: str):
        key = filename.strip() or order_number.strip()
        if not key:
            raise ValueError("Укажите имя файла или номер заказа")
        self.job_links[key] = {"order_id": order_id, "order_number": order_number, "product": product, "linked_at": now_iso()}
        self.add_event("link", "Печать связана с заказом", f"{key} → №{order_number} {product}", self.job_links[key])
        return self.job_links[key]

    def next_seq(self):
        self.sequence += 1
        return str(self.sequence)

    def publish(self, payload: dict):
        if not (self.client and self.connected):
            raise ConnectionError("Принтер не подключён")
        result = self.client.publish(f"device/{self.config['serial']}/request", json.dumps(payload), qos=0)
        if result.rc != 0:
            raise ConnectionError(f"MQTT publish: {result.rc}")

    def command(self, name: str, value: Any = None):
        seq = self.next_seq()
        if name in ("pause", "resume", "stop"):
            payload = {"print": {"sequence_id": seq, "command": name, "param": ""}}
        elif name == "speed":
            level = int(value)
            if level not in (1, 2, 3, 4): raise ValueError("Допустимый режим скорости: 1–4")
            payload = {"print": {"sequence_id": seq, "command": "print_speed", "param": str(level)}}
        elif name == "light":
            mode = "on" if bool(value) else "off"
            payload = {"system": {"sequence_id": seq, "command": "ledctrl", "led_node": "chamber_light", "led_mode": mode, "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}}
        elif name in ("nozzle_temp", "bed_temp"):
            temp = int(value)
            limit = 300 if name == "nozzle_temp" else 120
            if temp < 0 or temp > limit: raise ValueError(f"Температура должна быть от 0 до {limit} °C")
            gcode = f"M104 S{temp}" if name == "nozzle_temp" else f"M140 S{temp}"
            payload = {"print": {"sequence_id": seq, "command": "gcode_line", "param": gcode, "user_id": "printflow"}}
        elif name in ("part_fan", "aux_fan", "chamber_fan"):
            percent = max(0, min(100, int(value)))
            pin = {"part_fan": 1, "aux_fan": 2, "chamber_fan": 3}[name]
            pwm = round(percent * 255 / 100)
            payload = {"print": {"sequence_id": seq, "command": "gcode_line", "param": f"M106 P{pin} S{pwm}", "user_id": "printflow"}}
        elif name == "refresh":
            payload = {"pushing": {"sequence_id": seq, "command": "pushall", "version": 1, "push_target": 1}}
        else:
            raise ValueError("Команда не разрешена")
        self.publish(payload)
        self.add_event("command", "Команда отправлена", name)
        return {"ok": True, "sequence_id": seq}

    def normalized(self):
        with self.lock:
            p = copy.deepcopy(self.raw.get("print", {}))
            version = copy.deepcopy(self.version)
        ams = p.get("ams") if isinstance(p.get("ams"), dict) else {}
        units = ams.get("ams") if isinstance(ams.get("ams"), list) else []
        trays = []
        for unit in units:
            for tray in unit.get("tray", []):
                color = tray.get("tray_color") or ""
                trays.append({
                    "id": f"{unit.get('id','0')}-{tray.get('id','0')}", "unit": unit.get("id"), "slot": tray.get("id"),
                    "type": tray.get("tray_type") or tray.get("tray_sub_brands") or "", "color": "#" + color[:6] if len(color) >= 6 else "#d1d5db",
                    "remain": tray.get("remain"), "active": str(ams.get("tray_now")) in (str(tray.get("id")), f"{unit.get('id')}{tray.get('id')}")
                })
        fans = {
            "part": round(as_num(p.get("cooling_fan_speed")) / 15 * 100) if as_num(p.get("cooling_fan_speed")) <= 15 else round(as_num(p.get("cooling_fan_speed")) / 255 * 100),
            "aux": round(as_num(p.get("big_fan1_speed")) / 15 * 100) if as_num(p.get("big_fan1_speed")) <= 15 else round(as_num(p.get("big_fan1_speed")) / 255 * 100),
            "chamber": round(as_num(p.get("big_fan2_speed")) / 15 * 100) if as_num(p.get("big_fan2_speed")) <= 15 else round(as_num(p.get("big_fan2_speed")) / 255 * 100),
        }
        state = str(p.get("gcode_state") or ("OFFLINE" if not self.connected else "IDLE")).upper()
        task = p.get("subtask_name") or p.get("gcode_file") or ""
        firmware = next((x.get("sw_ver", "") for x in version if x.get("name") == "ota"), "")
        hms = p.get("hms") if isinstance(p.get("hms"), list) else []
        light = next((x.get("mode", "off") for x in p.get("lights_report", []) if x.get("node") == "chamber_light"), "off")
        return {
            "connector": {"version": APP_VERSION, "mqtt_available": mqtt is not None},
            "connection": {"connected": self.connected, "connecting": self.connecting, "last_message": self.last_message, "last_error": self.last_error, "host": self.config.get("host", ""), "name": self.config.get("name", "P1S")},
            "printer": {"state": state, "task": task, "progress": as_num(p.get("mc_percent")), "remaining_min": as_num(p.get("mc_remaining_time")), "layer": int(as_num(p.get("layer_num"))), "total_layers": int(as_num(p.get("total_layer_num"))), "speed_level": int(as_num(p.get("spd_lvl") or p.get("speed_level"), 2)), "wifi": p.get("wifi_signal", ""), "firmware": firmware, "print_error": p.get("print_error", 0), "hms": hms, "linked_order": self.current_link(task)},
            "temperature": {"nozzle": as_num(p.get("nozzle_temper")), "nozzle_target": as_num(p.get("nozzle_target_temper")), "bed": as_num(p.get("bed_temper")), "bed_target": as_num(p.get("bed_target_temper")), "chamber": as_num(p.get("chamber_temper"))},
            "fans": fans, "light": light, "ams": {"units": len(units), "humidity": units[0].get("humidity") if units else None, "temperature": units[0].get("temp") if units else None, "trays": trays},
            "camera": {"available": bool(self.camera.frame), "age": time.time() - self.camera.frame_at if self.camera.frame_at else None, "error": self.camera.error},
            "updated_at": now_iso(),
        }


BRIDGE = PrinterBridge()


def discover_printers(timeout=3.0):
    message = "M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\nST: urn:bambulab-com:device:3dprinter:1\r\n\r\n".encode()
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(.35)
    try:
        sock.sendto(message, ("239.255.255.250", 1900))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                continue
            text = data.decode(errors="ignore")
            headers = {}
            for line in text.splitlines()[1:]:
                if ":" in line:
                    k, v = line.split(":", 1); headers[k.strip().lower()] = v.strip()
            if "bambu" in text.lower():
                serial = headers.get("usn", "").split("::")[0]
                found[addr[0]] = {"host": addr[0], "serial": serial, "name": headers.get("devname.bambu.com", "Bambu Lab"), "model": headers.get("devmodel.bambu.com", "")}
    finally:
        sock.close()
    return list(found.values())


class Handler(SimpleHTTPRequestHandler):
    server_version = "PrintFlowConnector/" + APP_VERSION

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE), **kwargs)

    def log_message(self, fmt, *args):
        if self.path.startswith("/api/printer/status"):
            return
        super().log_message(fmt, *args)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000: raise ValueError("Слишком большой запрос")
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def origin_ok(self):
        origin = self.headers.get("Origin", "")
        return not origin or bool(re.match(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$", origin))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/printer/status": return self.send_json(BRIDGE.normalized())
        if path == "/api/printer/config": return self.send_json(BRIDGE.public_config())
        if path == "/api/printer/events": return self.send_json({"events": list(BRIDGE.events)[::-1]})
        if path == "/api/printer/discover":
            try: return self.send_json({"printers": discover_printers()})
            except Exception as exc: return self.send_json({"error": str(exc)}, 500)
        if path == "/api/printer/camera.jpg":
            frame = BRIDGE.camera.frame
            if not frame: return self.send_json({"error": BRIDGE.camera.error or "Кадр ещё не получен"}, 503)
            self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.send_header("Content-Length", str(len(frame))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(frame); return
        if path == "/api/health": return self.send_json({"ok": True, "version": APP_VERSION, "data_dir": str(DATA_DIR), "mqtt": mqtt is not None})
        return super().do_GET()

    def do_POST(self):
        if not self.origin_ok(): return self.send_json({"error": "Запрос отклонён: недопустимый Origin"}, 403)
        try:
            data = self.read_json(); path = self.path.split("?", 1)[0]
            if path == "/api/printer/config": return self.send_json({"ok": True, "config": BRIDGE.set_config(data)})
            if path == "/api/printer/connect": BRIDGE.connect(); return self.send_json({"ok": True})
            if path == "/api/printer/command": return self.send_json(BRIDGE.command(str(data.get("command", "")), data.get("value")))
            if path == "/api/printer/link": return self.send_json({"ok": True, "link": BRIDGE.link_job(str(data.get("filename", "")), str(data.get("order_id", "")), str(data.get("order_number", "")), str(data.get("product", "")))})
            if path == "/api/printer/telegram-test": BRIDGE.notify_telegram("🔔 Тестовое уведомление успешно отправлено"); return self.send_json({"ok": True})
            return self.send_json({"error": "Маршрут не найден"}, 404)
        except (ValueError, ConnectionError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            return self.send_json({"error": str(exc)}, 500)


def main():
    parser = argparse.ArgumentParser(description="PrintFlow local Bambu connector")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address; keep 127.0.0.1 for safety")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"PrintFlow {APP_VERSION}: http://localhost:{args.port}")
    print(f"Настройки и история: {DATA_DIR}")
    if mqtt is None: print("ВНИМАНИЕ: paho-mqtt не установлен — интеграция с принтером отключена.")
    if not args.no_browser:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{args.port}/#printer")).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: BRIDGE.disconnect(); BRIDGE.camera.stop(); server.server_close()


if __name__ == "__main__":
    main()

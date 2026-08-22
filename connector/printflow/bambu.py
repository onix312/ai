"""MQTT/TLS-мост к принтеру Bambu Lab (P1/X1/A1).

Один экземпляр BambuPrinter обслуживает один физический принтер: держит
подключение, нормализует телеметрию, отправляет проверенные команды и
сообщает наружу о событиях печати.

Безопасность: все команды проходят через белый список с серверными
пределами. Ничего, что может повредить принтер незаметно, не выполняется
без явного подтверждения на стороне интерфейса.
"""
from __future__ import annotations

import copy
import json
import socket
import ssl
import threading
import time
from typing import Any, Callable

try:
    import paho.mqtt.client as mqtt
except ImportError:  # интерфейс покажет понятную ошибку
    mqtt = None

from .camera import CameraWorker
from .ftps import PrinterFiles
from .hms import decode_list, worst

STATE_NAMES = {
    "IDLE": "Готов", "RUNNING": "Печать", "PREPARE": "Подготовка",
    "PAUSE": "Пауза", "FINISH": "Завершено", "FAILED": "Ошибка",
    "SLICING": "Слайсинг", "OFFLINE": "Не в сети",
}

SPEED_LEVELS = {1: "Тихий", 2: "Стандарт", 3: "Спорт", 4: "Ludicrous"}


def as_num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def deep_merge(base: dict, patch: dict) -> dict:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def rc_value(reason_code: Any) -> int:
    """Код результата MQTT как число.

    paho-mqtt 1.x передаёт в колбэки int, а 2.x — объект ReasonCode,
    который нельзя привести через int(): получается TypeError прямо
    внутри _on_connect, флаг connected не выставляется, и принтер
    навсегда остаётся «не в сети», хотя соединение реально установлено.
    """
    if reason_code is None:
        return 0
    value = getattr(reason_code, "value", reason_code)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def fan_percent(raw: Any) -> int:
    """Bambu отдаёт скорость вентилятора либо 0..15, либо 0..255."""
    value = as_num(raw)
    if value <= 15:
        return round(value / 15 * 100)
    return round(value / 255 * 100)


class BambuPrinter:
    """Подключение и телеметрия одного принтера.

    Режим связи (record["mode"]):
      * "cloud" (по умолчанию для новых принтеров) — команды и телеметрия
        идут через облачный брокер Bambu (CloudBridge). Принтер остаётся
        в обычном облачном режиме, LAN Only Mode / Developer Mode не нужны.
      * "lan" — локальный MQTT принтера:8883 (прежнее поведение).
    """

    def __init__(self, record: dict, on_event: Callable[[str, str, str, dict], None] | None = None):
        self.id = record["id"]
        self.record = dict(record)
        self.on_event = on_event or (lambda *a, **k: None)
        self.lock = threading.RLock()
        self.mode = str(record.get("mode") or "cloud")
        self.bridge = None  # CloudBridge для режима cloud
        self._bridge_report = None
        self._bridge_status = None
        self.client = None
        self.connected = False
        self.connecting = False
        # Момент последнего подключения. Нужен менеджеру, чтобы не считать
        # «свежая связь + пустая телеметрия» поводом закрыть висящий заказ:
        # после сбоя питания принтер сначала присылает пустой gcode_state,
        # а факт печати приходит уже через несколько секунд.
        self.connected_since = 0.0
        self.last_error = ""
        self.last_message = 0.0
        self.raw: dict[str, Any] = {}
        self.version: list[dict] = []
        self.sequence = 0
        self.previous_state = ""
        self.previous_error = ""
        self.previous_power_loss_signal = False
        self.session: dict[str, Any] | None = None
        self.camera = CameraWorker(lambda: {"host": self.record.get("host"),
                                            "access_code": self.record.get("access_code"),
                                            "demo": bool(self.record.get("camera_demo")),
                                            "cloud": self.mode == "cloud"})
        self._stop = threading.Event()
        self._watchdog: threading.Thread | None = None

    # ------------------------------------------------------------- жизненный цикл
    @property
    def files(self) -> PrinterFiles:
        return PrinterFiles(
            self.record.get("host", ""), self.record.get("access_code", ""),
            timeout=int(self.record.get("ftps_timeout") or 8),
            retries=int(self.record.get("ftps_retries") or 3),
            block_size=int(self.record.get("ftps_block_kb") or 256) * 1024,
        )

    def update_record(self, record: dict) -> None:
        keys = ["host", "serial", "access_code", "enabled", "mode", "mqtt_keepalive"]
        if str(record.get("mode") or "cloud") == "cloud":
            keys += ["cloud_token", "cloud_uid", "cloud_region"]
        restart = any(self.record.get(k) != record.get(k) for k in keys)
        self.record = dict(record)
        if restart:
            self.reconnect()

    def start(self) -> None:
        self._stop.clear()
        self.camera.start()
        self.connect()
        if not self._watchdog or not self._watchdog.is_alive():
            self._watchdog = threading.Thread(target=self._watch, name=f"pf-watch-{self.id}", daemon=True)
            self._watchdog.start()

    def _watch(self) -> None:
        """Переподключение, если принтер молчит дольше 90 секунд. 8.0: backoff."""
        backoff = 1.0
        import random
        while not self._stop.wait(20):
            if not self.record.get("enabled", 1):
                continue
            if self.mode == "cloud":
                # Облачный мост (CloudBridge) держит соединение и
                # переподключается сам; здесь только первичная подписка.
                if not self.bridge and self.ready:
                    self.connect()
                continue
            if self.connected and self.last_message and time.time() - self.last_message > 90:
                self.last_error = "Нет данных от принтера, переподключаемся"
                self.reconnect()
                backoff = min(backoff * 1.8, 30.0)
            elif not self.connected and not self.connecting and self.ready:
                use_backoff = bool(self.record.get("mqtt_backoff", True))
                if use_backoff and backoff > 1.0:
                    time.sleep(backoff + random.uniform(0, 1))
                    backoff = min(backoff * 1.6, 30.0)
                self.connect()
                if not use_backoff:
                    backoff = 1.0
            elif self.connected:
                backoff = 1.0

    @property
    def ready(self) -> bool:
        if self.mode == "cloud":
            return bool(self.record.get("serial") and self.record.get("enabled", 1)
                        and self.record.get("cloud_token"))
        return bool(self.record.get("host") and self.record.get("serial")
                    and self.record.get("access_code") and self.record.get("enabled", 1))

    def connect(self) -> None:
        if mqtt is None:
            self.last_error = ("Не установлен paho-mqtt. Запустите файл запуска повторно "
                               "или выполните: pip install -r connector/requirements.txt")
            return
        if not self.ready:
            if self.mode == "cloud" and not self.record.get("cloud_token"):
                self.last_error = ("Режим «Облако»: выполните вход в аккаунт Bambu "
                                   "(Настройки → Bambu Cloud)")
            return
        self.disconnect()
        if self.mode == "cloud":
            self._connect_cloud()
            return
        self._connect_lan()

    def _connect_cloud(self) -> None:
        """Подключение через общий облачный мост аккаунта Bambu."""
        from .cloud_bridge import CloudBridge
        serial = self.record["serial"]
        bridge = CloudBridge.shared(
            str(self.record.get("cloud_region") or "global"),
            str(self.record.get("cloud_uid") or ""),
            str(self.record.get("cloud_token") or ""))
        self.bridge = bridge
        self._bridge_report = lambda _serial, payload: self.handle_report(payload)
        self._bridge_status = self._on_cloud_status
        bridge.attach(serial, self._bridge_report, self._bridge_status)
        was = self.connected
        self.connected = bridge.connected
        if self.connected and not was:
            self.connected_since = time.time()
        if not bridge.connected and not bridge.connecting:
            bridge.connect()
            was = self.connected
            self.connected = bridge.connected
            if self.connected and not was:
                self.connected_since = time.time()

    def _disconnect_context(self) -> dict:
        """Последний известный snapshot перед разрывом связи."""
        p = self.raw.get("print", {}) if isinstance(self.raw.get("print"), dict) else {}
        return {
            "last_state": str(p.get("gcode_state") or self.previous_state or "").upper(),
            "task": p.get("subtask_name") or p.get("gcode_file") or "",
            "remote_task_id": str(p.get("subtask_id") or p.get("task_id") or ""),
            "progress": as_num(p.get("mc_percent")),
            "layer": int(as_num(p.get("layer_num"))),
            "total_layers": int(as_num(p.get("total_layer_num"))),
        }

    def _on_cloud_status(self, connected: bool, error: str) -> None:
        was = self.connected
        self.connected = connected
        if connected and not was:
            self.connected_since = time.time()
        if connected:
            self.last_error = ""
            try:
                self.push_all()
                self.publish({"info": {"sequence_id": self.next_seq(),
                                       "command": "get_version"}})
            except Exception:
                pass
            if not was:
                self.on_event("online", "Принтер подключён (Bambu Cloud)",
                              self.record.get("name", ""), {})
        else:
            self.last_error = error or self.last_error or "Облачное соединение потеряно"
            if was:
                self.on_event("offline", "Связь с принтером потеряна (Bambu Cloud)",
                              self.record.get("name", ""), self._disconnect_context())

    def _connect_lan(self) -> None:
        try:
            self.connecting = True
            try:
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                     client_id=f"printflow-{self.id}-{int(time.time())}",
                                     protocol=mqtt.MQTTv311)
            except (AttributeError, TypeError):
                client = mqtt.Client(client_id=f"printflow-{self.id}-{int(time.time())}",
                                     protocol=mqtt.MQTTv311)
            client.username_pw_set("bblp", self.record["access_code"])
            client.tls_set(cert_reqs=ssl.CERT_NONE)
            client.tls_insecure_set(True)
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message
            self.client = client
            keepalive = max(10, min(300, int(self.record.get("mqtt_keepalive") or 30)))
            client.connect_async(self.record["host"], 8883, keepalive=keepalive)
            client.loop_start()
        except Exception as exc:
            self.connecting = False
            self.last_error = str(exc)

    def reconnect(self) -> None:
        self.disconnect()
        self.camera.stop()
        self.camera = CameraWorker(lambda: {"host": self.record.get("host"),
                                            "access_code": self.record.get("access_code"),
                                            "demo": bool(self.record.get("camera_demo")),
                                            "cloud": self.mode == "cloud"})
        if self.record.get("enabled", 1):
            self.camera.start()
            self.connect()

    def disconnect(self) -> None:
        old, self.client = self.client, None
        self.connected = False
        self.connecting = False
        if old:
            try:
                old.disconnect()
                old.loop_stop()
            except Exception:
                pass
        if self.bridge and self._bridge_report:
            try:
                self.bridge.detach(self.record.get("serial", ""))
            except Exception:
                pass
            self._bridge_report = None
            self._bridge_status = None
            self.bridge = None

    def shutdown(self) -> None:
        self._stop.set()
        self.disconnect()
        self.camera.stop()

    # -------------------------------------------------------------- MQTT-события
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        rc = rc_value(reason_code)
        ok = rc == 0
        was = self.connected
        self.connected, self.connecting = ok, False
        if ok and not was:
            self.connected_since = time.time()
        if not ok:
            # paho 2.x отдаёт MQTT5-коды: 134 = неверный логин/пароль,
            # 135 = не авторизован (аналог старых 4 и 5).
            if rc in (4, 5, 134, 135):
                self.last_error = f"Принтер отклонил подключение (код {rc}). Включите LAN Only + Developer Mode в настройках принтера: Settings → WLAN → LAN Only → Developer Mode ON. Проверьте Access Code и серийный номер."
                self.on_event("need_developer_mode", "Требуется Developer Mode", f"Код {rc} — включите LAN Only + Developer Mode", {"reason_code": rc})
            else:
                self.last_error = f"Принтер отклонил подключение (код {rc}). Проверьте Access Code и серийный номер."
            return
        self.last_error = ""
        client.subscribe(f"device/{self.record['serial']}/report")
        self.push_all()
        self.publish({"info": {"sequence_id": self.next_seq(), "command": "get_version"}})
        self.on_event("online", "Принтер подключён", self.record.get("name", ""), {})

    def _on_disconnect(self, client, userdata, disconnect_flags=None, reason_code=0, properties=None):
        was = self.connected
        self.connected = False
        if rc_value(reason_code) != 0:
            self.last_error = f"Соединение потеряно (код {reason_code})"
            if was:
                self.on_event("offline", "Связь с принтером потеряна", self.record.get("name", ""),
                              self._disconnect_context())

    def _on_message(self, client, userdata, message):
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except Exception:
            return
        self.handle_report(payload)

    def handle_report(self, payload: dict) -> None:
        """Разбор одного отчёта принтера — общий для LAN-MQTT и облака."""
        with self.lock:
            if isinstance(payload.get("print"), dict):
                deep_merge(self.raw.setdefault("print", {}), payload["print"])
            if isinstance(payload.get("info", {}).get("module"), list):
                self.version = payload["info"]["module"]
            for group, value in payload.items():
                if group != "print" and isinstance(value, dict):
                    deep_merge(self.raw.setdefault(group, {}), value)
            self.last_message = time.time()
            self._detect_events()

    @staticmethod
    def _has_power_loss_signal(print_report: dict) -> bool:
        """Распознать только явный firmware/bridge power-loss marker.

        Offline/online и ``PAUSE`` намеренно не входят в этот список: они
        одинаково возникают при сетевом обрыве, ручном reconnect и обычной
        паузе. Разные bridge/прошивки используют разные имена поля, поэтому
        принимаем только явно названные power-loss/recovery значения.
        """
        keys = ("power_loss", "power_loss_recovery", "poweroff_recovery",
                "power_loss_recovery_state", "power_loss_state")
        for key in keys:
            value = print_report.get(key)
            if isinstance(value, bool) and value:
                return True
            if isinstance(value, (int, float)) and value == 1:
                return True
            if isinstance(value, dict):
                value = value.get("state") or value.get("status") or value.get("reason") or value.get("active")
            text = str(value or "").strip().lower().replace("-", "_")
            if text in ("power_loss", "power_loss_recovery", "poweroff",
                        "poweroff_recovery", "recovered", "resume"):
                return True
        return False

    def _detect_events(self) -> None:
        p = self.raw.get("print", {})
        state = str(p.get("gcode_state", "")).upper()
        signal = self._has_power_loss_signal(p) or any(
            self._has_power_loss_signal(value)
            for key, value in self.raw.items()
            if key != "print" and isinstance(value, dict)
        )
        if signal and not self.previous_power_loss_signal:
            self.on_event(
                "power_loss_confirmed", "Принтер подтвердил восстановление питания",
                str(p.get("subtask_name") or p.get("gcode_file") or "Печать"),
                {"source": "printer_report", "power_loss_recovery": True})
        self.previous_power_loss_signal = signal

        name = p.get("subtask_name") or p.get("gcode_file") or "Задание печати"
        if state and state != self.previous_state:
            previous, self.previous_state = self.previous_state, state
            if state in ("RUNNING", "PREPARE") and not self.session:
                self.session = {
                    "started_ts": time.time(), "task": name,
                    "remote_task_id": str(p.get("subtask_id") or p.get("task_id") or ""),
                    "total_layers": int(as_num(p.get("total_layer_num"))),
                    "ams_tray": (p.get("ams") or {}).get("tray_now") if isinstance(p.get("ams"), dict) else None,
                }
                self.on_event("start", "Печать началась", name, dict(self.session))
            elif state == "PAUSE":
                if not self.session:
                    self.session = {
                        "started_ts": time.time(), "task": name,
                        "remote_task_id": str(p.get("subtask_id") or p.get("task_id") or ""),
                        "total_layers": int(as_num(p.get("total_layer_num"))),
                        "ams_tray": (p.get("ams") or {}).get("tray_now") if isinstance(p.get("ams"), dict) else None,
                    }
                self.on_event("pause", "Печать приостановлена", name, self._session_data(p))
            elif state == "FINISH":
                self.on_event("complete", "Печать завершена", name, self._session_data(p))
                self.session = None
            elif state == "FAILED":
                self.on_event("error", "Печать завершилась ошибкой", name, self._session_data(p))
                self.session = None
            elif state == "IDLE" and previous in ("RUNNING", "PAUSE", "PREPARE"):
                self.on_event("stop", "Печать остановлена", name, self._session_data(p))
                self.session = None
        err = str(p.get("print_error") or "")
        if err and err not in ("0", self.previous_error):
            self.previous_error = err
            self.on_event("error", "Принтер сообщил об ошибке", err, {"hms": p.get("hms", [])})

    def _session_data(self, p: dict) -> dict:
        data = dict(self.session or {})
        if data.get("started_ts"):
            data["duration_min"] = round((time.time() - data["started_ts"]) / 60, 1)
        data.update({
            "progress": as_num(p.get("mc_percent")),
            "layer": int(as_num(p.get("layer_num"))),
            "total_layers": int(as_num(p.get("total_layer_num"))),
            "task": p.get("subtask_name") or p.get("gcode_file") or data.get("task", ""),
            "remote_task_id": str(
                p.get("subtask_id") or p.get("task_id") or data.get("remote_task_id") or ""
            ),
            "weight": as_num(p.get("print_weight")),
        })
        return data

    # ----------------------------------------------------------------- команды
    def next_seq(self) -> str:
        self.sequence += 1
        return str(self.sequence)

    def publish(self, payload: dict) -> None:
        if self.mode == "cloud":
            if not (self.bridge and self.connected):
                raise ConnectionError("Принтер не подключён (нет связи с облаком Bambu)")
            self.bridge.publish(self.record["serial"], payload)
            return
        if not (self.client and self.connected):
            raise ConnectionError("Принтер не подключён")
        result = self.client.publish(f"device/{self.record['serial']}/request",
                                     json.dumps(payload), qos=0)
        if result.rc != 0:
            raise ConnectionError(f"Не удалось отправить команду (MQTT {result.rc})")

    def push_all(self) -> None:
        self.publish({"pushing": {"sequence_id": self.next_seq(), "command": "pushall",
                                  "version": 1, "push_target": 1}})

    def gcode(self, line: str) -> None:
        self.publish({"print": {"sequence_id": self.next_seq(), "command": "gcode_line",
                                "param": line if line.endswith("\n") else line + "\n",
                                "user_id": "printflow"}})

    def command(self, name: str, value: Any = None) -> dict:
        """Белый список команд с серверными пределами."""
        seq = self.next_seq()
        if name in ("pause", "resume", "stop"):
            self.publish({"print": {"sequence_id": seq, "command": name, "param": ""}})
        elif name == "refresh":
            self.push_all()
        elif name == "speed":
            level = int(as_num(value, 2))
            if level not in SPEED_LEVELS:
                raise ValueError("Режим скорости: 1–4")
            self.publish({"print": {"sequence_id": seq, "command": "print_speed", "param": str(level)}})
        elif name == "light":
            mode = "on" if value in (True, "on", 1, "1") else "off"
            self.publish({"system": {"sequence_id": seq, "command": "ledctrl",
                                     "led_node": "chamber_light", "led_mode": mode,
                                     "led_on_time": 500, "led_off_time": 500,
                                     "loop_times": 0, "interval_time": 0}})
        elif name in ("nozzle_temp", "bed_temp"):
            temp = int(as_num(value))
            limit = 300 if name == "nozzle_temp" else 110
            if not 0 <= temp <= limit:
                raise ValueError(f"Температура должна быть от 0 до {limit} °C")
            self.gcode(f"M104 S{temp}" if name == "nozzle_temp" else f"M140 S{temp}")
        elif name in ("part_fan", "aux_fan", "chamber_fan"):
            percent = max(0, min(100, int(as_num(value))))
            pin = {"part_fan": 1, "aux_fan": 2, "chamber_fan": 3}[name]
            self.gcode(f"M106 P{pin} S{round(percent * 255 / 100)}")
        elif name == "flow":
            # Поток филамента, % (M221): меньше — тоньше слой, больше — заливка.
            percent = max(50, min(150, int(as_num(value, 100))))
            self.gcode(f"M221 S{percent}")
        elif name == "speed_pct":
            # Скорость печати в процентах (M220) — тоньше, чем 4 режима.
            percent = max(10, min(400, int(as_num(value, 100))))
            self.gcode(f"M220 S{percent}")
        elif name == "home":
            self.gcode("G28")
        elif name == "move":
            axis = str((value or {}).get("axis", "")).upper()
            dist = as_num((value or {}).get("distance"), 0)
            if axis not in ("X", "Y", "Z"):
                raise ValueError("Ось: X, Y или Z")
            if abs(dist) > 100:
                raise ValueError("Максимальное перемещение за раз — 100 мм")
            speed = 900 if axis == "Z" else 3000
            self.gcode(f"M211 S\nM1002 push_ref_mode\nG91\nG1 {axis}{dist:.1f} F{speed}\nG90\nM1002 pop_ref_mode")
        elif name == "extrude":
            length = as_num(value, 10)
            if abs(length) > 100:
                raise ValueError("Максимум 100 мм за раз")
            self.gcode(f"M83\nG1 E{length:.1f} F180")
        elif name == "bed_level":
            self.publish({"print": {"sequence_id": seq, "command": "gcode_line",
                                    "param": "G29\n", "user_id": "printflow"}})
        elif name == "calibration":
            # bitmask: 1 = motor noise, 2 = bed level, 4 = vibration
            mask = int(as_num(value, 7)) & 7
            self.publish({"print": {"sequence_id": seq, "command": "calibration", "option": mask}})
        elif name == "unload_filament":
            self.publish({"print": {"sequence_id": seq, "command": "ams_change_filament",
                                    "target": 255, "curr_temp": 220, "tar_temp": 220}})
        elif name == "load_filament":
            slot = int(as_num(value, 0))
            if not 0 <= slot <= 15:
                raise ValueError("Слот AMS: 0–15")
            self.publish({"print": {"sequence_id": seq, "command": "ams_change_filament",
                                    "target": slot, "curr_temp": 220, "tar_temp": 220}})
        elif name == "ams_filament":
            data = value or {}
            self.publish({"print": {
                "sequence_id": seq, "command": "ams_filament_setting",
                "ams_id": int(as_num(data.get("ams_id"))), "tray_id": int(as_num(data.get("tray_id"))),
                "tray_color": str(data.get("color", "FFFFFFFF")).lstrip("#").upper().ljust(8, "F")[:8],
                "tray_type": str(data.get("type", "PLA")).upper(),
                "tray_info_idx": str(data.get("idx", "")),
                "nozzle_temp_min": int(as_num(data.get("temp_min"), 190)),
                "nozzle_temp_max": int(as_num(data.get("temp_max"), 240)),
                "setting_id": str(data.get("setting_id", "")),
            }})
        elif name == "timelapse":
            self.publish({"camera": {"sequence_id": seq, "command": "ipcam_timelapse",
                                     "control": "enable" if value else "disable"}})
        elif name == "print_gcode":
            # Запуск готового G-code с SD-карты (не требует подписи на
            # защищённых прошивках — в отличие от project_file для 3MF).
            path = str(value or "").strip()
            if not path:
                raise ValueError("Укажите путь к G-code на SD-карте")
            if not path.lower().endswith(".gcode"):
                raise ValueError("Ожидается файл .gcode")
            if path.startswith("/"):
                path = path.lstrip("/")
            self.publish({"print": {"sequence_id": seq, "command": "gcode_file",
                                    "param": path, "user_id": "printflow"}})
        elif name == "skip_objects":
            objects = [int(x) for x in (value or [])][:64]
            self.publish({"print": {"sequence_id": seq, "command": "skip_objects", "obj_list": objects}})
        elif name == "prompt_sound":
            self.publish({"print": {"sequence_id": seq, "command": "print_option",
                                    "sound_enable": bool(value)}})
        else:
            raise ValueError("Команда не разрешена")
        self.on_event("command", "Команда отправлена", name, {"value": value})
        return {"ok": True, "sequence_id": seq}

    def start_print(self, filename: str, plate: int = 1, use_ams: bool = True,
                    ams_mapping: list[int] | None = None, bed_level: bool = True,
                    flow_cali: bool = False, timelapse: bool = False,
                    subtask_name: str = "", cloud_url: str = "") -> dict:
        """Запуск печати файла, уже находящегося в памяти принтера.

        Для локального режима url = file://… на SD-карте. Если передан
        cloud_url (файл залит в облачное хранилище Bambu), принтер скачает
        его сам по своему облачному каналу.
        """
        if not filename:
            raise ValueError("Не указан файл для печати")
        state = str(self.raw.get("print", {}).get("gcode_state", "")).upper()
        if state in ("RUNNING", "PREPARE", "PAUSE"):
            raise ValueError("Принтер занят: дождитесь завершения или остановите текущую печать")
        clean = filename if filename.startswith("/") else "/" + filename.lstrip("/")
        if cloud_url:
            url = cloud_url
        elif self.mode == "cloud":
            # В облачном режиме обычный запуск идёт через POST /my/task
            # (manager/api вызывают bambu_cloud.create_task) — сюда попадаем
            # только для файла, уже лежащего на SD (гибрид ftp://).
            url = f"ftp://{clean}" if self.record.get("host") else f"file://{clean}"
        else:
            url = f"file://{clean}" if not clean.startswith("file:") else clean
        payload = {"print": {
            "sequence_id": self.next_seq(),
            "command": "project_file",
            "param": f"Metadata/plate_{max(1, int(plate))}.gcode",
            "project_id": "0", "profile_id": "0", "task_id": "0",
            "subtask_id": "0", "subtask_name": subtask_name or filename.rsplit("/", 1)[-1],
            "url": url,
            "timelapse": bool(timelapse),
            "bed_type": "auto",
            "bed_leveling": bool(bed_level),
            "flow_cali": bool(flow_cali),
            "vibration_cali": True,
            "layer_inspect": True,
            "use_ams": bool(use_ams),
            "ams_mapping": ams_mapping if ams_mapping else ([0] if use_ams else []),
        }}
        self.publish(payload)
        self.on_event("print_start", "Запущена печать", filename,
                      {"plate": plate, "use_ams": use_ams, "ams_mapping": ams_mapping})
        return {"ok": True, "file": filename}

    # ------------------------------------------------------------- телеметрия
    def snapshot(self) -> dict:
        with self.lock:
            p = copy.deepcopy(self.raw.get("print", {}))
            version = copy.deepcopy(self.version)
        ams_raw = p.get("ams") if isinstance(p.get("ams"), dict) else {}
        units = ams_raw.get("ams") if isinstance(ams_raw.get("ams"), list) else []
        tray_now = str(ams_raw.get("tray_now", ""))
        trays = []
        for unit in units:
            for tray in unit.get("tray", []) or []:
                color = str(tray.get("tray_color") or "").strip()
                global_id = f"{unit.get('id', 0)}{tray.get('id', 0)}"
                # Остаток катушки: на большинстве прошивок 0–100, но на части
                # приходит в десятых долях процента (0–1000). Нормализуем, иначе
                # в интерфейсе «1000%», а предупреждение о низком остатке не сработает.
                remain = None
                if tray.get("remain") is not None:
                    raw = as_num(tray.get("remain"), -1)
                    if raw >= 0:
                        if raw > 100:
                            raw = raw / 10.0
                        remain = round(max(0.0, min(100.0, raw)), 1)
                trays.append({
                    "id": global_id,
                    "unit": int(as_num(unit.get("id"))),
                    "slot": int(as_num(tray.get("id"))),
                    "label": f"AMS {int(as_num(unit.get('id'))) + 1} · слот {int(as_num(tray.get('id'))) + 1}",
                    "type": tray.get("tray_type") or tray.get("tray_sub_brands") or "",
                    "color": "#" + color[:6] if len(color) >= 6 else "#cbd5e1",
                    "remain": remain,
                    "uuid": tray.get("tray_uuid", ""),
                    "nozzle_min": tray.get("nozzle_temp_min"),
                    "nozzle_max": tray.get("nozzle_temp_max"),
                    "active": tray_now in (global_id, str(tray.get("id"))),
                })
        state = str(p.get("gcode_state") or ("OFFLINE" if not self.connected else "IDLE")).upper()
        task = p.get("subtask_name") or p.get("gcode_file") or ""
        firmware = next((x.get("sw_ver", "") for x in version if x.get("name") == "ota"), "")
        light = next((x.get("mode", "off") for x in p.get("lights_report", []) or []
                      if x.get("node") == "chamber_light"), "off")
        hms = p.get("hms") if isinstance(p.get("hms"), list) else []
        raw_error = p.get("print_error", 0)
        problems = decode_list(hms)
        if raw_error and str(raw_error) not in ("0", "0000-0000"):
            problems = decode_list([raw_error]) + problems
        remaining = as_num(p.get("mc_remaining_time"))
        return {
            "id": self.id,
            "name": self.record.get("name", "Принтер"),
            "model": self.record.get("model", "P1S"),
            "enabled": bool(self.record.get("enabled", 1)),
            "connection": {
                "connected": self.connected, "connecting": self.connecting,
                "configured": self.ready, "host": self.record.get("host", ""),
                "last_message": self.last_message, "last_error": self.last_error,
                "mode": self.mode,
            },
            "printer": {
                "state": state, "state_label": STATE_NAMES.get(state, state),
                "task": task,
                "file": p.get("gcode_file") or task,
                "remote_task_id": str(p.get("subtask_id") or p.get("task_id") or ""),
                "file_version": str(p.get("gcode_file_hash") or p.get("file_hash") or ""),
                "power_loss_recovery": self._has_power_loss_signal(p),
                "progress": as_num(p.get("mc_percent")),
                "remaining_min": remaining,
                "eta": time.time() + remaining * 60 if remaining else None,
                "layer": int(as_num(p.get("layer_num"))),
                "total_layers": int(as_num(p.get("total_layer_num"))),
                "speed_level": int(as_num(p.get("spd_lvl"), 2)),
                "speed_label": SPEED_LEVELS.get(int(as_num(p.get("spd_lvl"), 2)), ""),
                "speed_percent": int(as_num(p.get("spd_mag"), 100)),
                "wifi": p.get("wifi_signal", ""), "firmware": firmware,
                "print_error": p.get("print_error", 0), "hms": hms,
                "problems": problems,
                "severity": worst(problems),
                "sdcard": bool(p.get("sdcard", False)),
                "weight": as_num(p.get("print_weight")),
                "started_ts": (self.session or {}).get("started_ts"),
                "elapsed_min": round((time.time() - (self.session or {}).get("started_ts", 0)) / 60, 1)
                if (self.session or {}).get("started_ts") else 0,
            },
            "temperature": {
                "nozzle": as_num(p.get("nozzle_temper")),
                "nozzle_target": as_num(p.get("nozzle_target_temper")),
                "bed": as_num(p.get("bed_temper")),
                "bed_target": as_num(p.get("bed_target_temper")),
                "chamber": as_num(p.get("chamber_temper")),
            },
            "fans": {
                "part": fan_percent(p.get("cooling_fan_speed")),
                "aux": fan_percent(p.get("big_fan1_speed")),
                "chamber": fan_percent(p.get("big_fan2_speed")),
            },
            "light": light,
            "ams": {
                "units": len(units),
                "humidity": units[0].get("humidity") if units else None,
                "temperature": units[0].get("temp") if units else None,
                "trays": trays,
                "active_tray": tray_now,
            },
            "camera": self.camera.state(),
        }

    def health(self) -> dict:
        """Диагностика подключения для UI 8.0."""
        host = self.record.get("host", "")
        # Облачный режим: главное — мост аккаунта, порты принтера не нужны.
        if self.mode == "cloud":
            cloud = self.bridge.state() if self.bridge else {}
            ports = {}
            if host:  # локальные ускорители (камера/SD) — только если задан IP
                for label, port in [("ftps", 990), ("camera", 6000)]:
                    ports[label] = self._port_check(host, port)
            return {"mode": "cloud", "cloud": cloud, "ports": ports,
                    "connected": self.connected, "last_error": self.last_error,
                    "firmware": self._firmware_version()}
        # Локальный режим: проверка портов как раньше
        ports = {}
        for label, port in [("mqtt", 8883), ("ftps", 990), ("camera", 6000)]:
            if not host:
                ports[label] = {"ok": False, "ms": 0}
                continue
            ports[label] = self._port_check(host, port)
            # fallback 1883 для A1 mini
            if label == "mqtt" and not ports[label]["ok"]:
                ports["mqtt_fallback"] = self._port_check(host, 1883, timeout=1.0)
        # firmware
        needs_dev = False
        if " 5" in self.last_error or "Developer Mode" in self.last_error:
            needs_dev = True
        return {"mode": "lan", "ports": ports, "firmware": self._firmware_version(),
                "needs_developer_mode": needs_dev, "connected": self.connected,
                "last_error": self.last_error}

    @staticmethod
    def _port_check(host: str, port: int, timeout: float = 1.5) -> dict:
        import socket as socket_mod
        import time as time_mod
        start = time_mod.time()
        try:
            with socket_mod.create_connection((host, port), timeout=timeout):
                return {"ok": True, "ms": round((time_mod.time() - start) * 1000, 1)}
        except Exception:
            return {"ok": False, "ms": round((time_mod.time() - start) * 1000, 1)}

    def _firmware_version(self) -> str:
        return next((x.get("sw_ver", "") for x in self.version if x.get("name") == "ota"), "")

    @staticmethod
    def discover(timeout: float = 3.0) -> list[dict]:
        """SSDP-поиск принтеров Bambu в локальной сети."""
        message = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
                   "MAN: \"ssdp:discover\"\r\nMX: 2\r\n"
                   "ST: urn:bambulab-com:device:3dprinter:1\r\n\r\n").encode()
        found: dict[str, dict] = {}
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.4)
        try:
            for _ in range(2):
                sock.sendto(message, ("239.255.255.250", 1900))
            end = time.time() + timeout
            while time.time() < end:
                try:
                    data, addr = sock.recvfrom(8192)
                except socket.timeout:
                    continue
                text = data.decode(errors="ignore")
                if "bambu" not in text.lower():
                    continue
                headers = {}
                for line in text.splitlines()[1:]:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                found[addr[0]] = {
                    "host": addr[0],
                    "serial": headers.get("usn", "").split("::")[0],
                    "name": headers.get("devname.bambu.com", "Bambu Lab"),
                    "model": headers.get("devmodel.bambu.com", ""),
                }
        finally:
            sock.close()
        return list(found.values())

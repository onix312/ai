"""Облачный MQTT-мост Bambu: одно соединение на аккаунт для всех принтеров.

Принтер в обычном облачном режиме держит собственное соединение с брокером
Bambu. Клиент, вошедший в тот же аккаунт, подключается к тому же брокеру
(us/cn.mqtt.bambulab.com:8883) с логином ``u_{uid}`` и паролем-токеном —
это тот же канал, которым пользуется Bambu Handy, поэтому команды
принимаются без LAN Only Mode / Developer Mode. Формат топиков и сообщений
совпадает с локальным MQTT принтера: ``device/{serial}/report`` (чтение) и
``device/{serial}/request`` (команды).

Один экземпляр CloudBridge обслуживает все принтеры аккаунта: подписки по
serial, маршрутизация входящих сообщений по серийному номеру из топика.
"""
from __future__ import annotations

import json
import ssl
import threading
import time
from typing import Any, Callable

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from .bambu_cloud import mqtt_host

ReportHandler = Callable[[str, dict], None]
StatusHandler = Callable[[bool, str], None]


class CloudBridge:
    """Одно paho-соединение к облачному брокеру Bambu на аккаунт."""

    _shared: dict[str, "CloudBridge"] = {}
    _shared_lock = threading.Lock()

    @classmethod
    def shared(cls, region: str, uid: str, token: str) -> "CloudBridge":
        """Существующий мост для аккаунта или новый (с автозапуском)."""
        key = f"{region or 'global'}|{uid or ''}"
        with cls._shared_lock:
            bridge = cls._shared.get(key)
            if bridge is not None:
                bridge.update_creds(region, uid, token)
                return bridge
            bridge = cls(region, uid, token)
            cls._shared[key] = bridge
        bridge.start()
        return bridge

    @classmethod
    def shutdown_all(cls) -> None:
        with cls._shared_lock:
            bridges = list(cls._shared.values())
            cls._shared.clear()
        for bridge in bridges:
            bridge.shutdown()

    @classmethod
    def state_all(cls) -> dict:
        with cls._shared_lock:
            bridges = list(cls._shared.values())
        states = [b.state() for b in bridges]
        return {
            "bridges": len(states),
            "connected": any(s["connected"] for s in states),
            "attached": sum(s["attached"] for s in states),
            "errors": [s["error"] for s in states if s["error"]],
        }

    def __init__(self, region: str, uid: str, token: str):
        self.region = region or "global"
        self.uid = uid or ""
        self.token = token or ""
        self.connected = False
        self.connecting = False
        self.last_error = ""
        self.client = None
        self._attached: dict[str, tuple[ReportHandler, StatusHandler]] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._watchdog: threading.Thread | None = None
        self._last_message = 0.0

    # ----------------------------------------------------------- жизненный цикл
    def update_creds(self, region: str, uid: str, token: str) -> None:
        """Новый токен → переподключение. Токен меняется при входе заново."""
        changed = (self.region != region or self.uid != uid or self.token != token)
        self.region = region or "global"
        self.uid = uid or ""
        if token and token != self.token:
            self.token = token
            changed = True
        if changed:
            self.connect()

    def start(self) -> None:
        self._stop.clear()
        if not self._watchdog or not self._watchdog.is_alive():
            self._watchdog = threading.Thread(target=self._watch, name="pf-cloud-bridge",
                                              daemon=True)
            self._watchdog.start()

    def _watch(self) -> None:
        """Держит соединение: переподключает, если брокер молчит/упал."""
        while not self._stop.wait(15):
            if not self.token:
                continue
            if self.connected and self._last_message and time.time() - self._last_message > 120:
                self.last_error = "Облако Bambu молчит — переподключаемся"
                self.connect()
            elif not self.connected and not self.connecting:
                self.connect()

    def connect(self) -> None:
        if mqtt is None:
            self.last_error = ("Не установлен paho-mqtt. Выполните: "
                               "pip install -r connector/requirements.txt")
            return
        if not self.token or not self.uid:
            self.last_error = "Не выполнен вход в Bambu Cloud (нет токена)"
            return
        self.disconnect()
        try:
            self.connecting = True
            try:
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                     client_id=f"printflow-cloud-{int(time.time())}",
                                     protocol=mqtt.MQTTv311)
            except (AttributeError, TypeError):
                client = mqtt.Client(client_id=f"printflow-cloud-{int(time.time())}",
                                     protocol=mqtt.MQTTv311)
            client.username_pw_set(f"u_{self.uid}", self.token)
            client.tls_set(cert_reqs=ssl.CERT_NONE)
            client.tls_insecure_set(True)
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message
            try:
                client.reconnect_delay_set(min_delay=1, max_delay=30)
            except Exception:
                pass
            self.client = client
            client.connect_async(mqtt_host(self.region), 8883, keepalive=60)
            client.loop_start()
        except Exception as exc:
            self.connecting = False
            self.last_error = str(exc)

    def disconnect(self) -> None:
        old, self.client = self.client, None
        was = self.connected
        self.connected = False
        self.connecting = False
        if old:
            try:
                old.disconnect()
                old.loop_stop()
            except Exception:
                pass
        if was:
            with self._lock:
                handlers = list(self._attached.values())
            for _, on_status in handlers:
                try:
                    on_status(False, "Облачное соединение закрыто")
                except Exception:
                    pass

    def shutdown(self) -> None:
        self._stop.set()
        self.disconnect()
        with self._lock:
            self._attached.clear()

    # -------------------------------------------------------------- подписчики
    def attach(self, serial: str, on_report: ReportHandler, on_status: StatusHandler) -> None:
        with self._lock:
            self._attached[serial] = (on_report, on_status)
        if self.connected and self.client:
            try:
                self.client.subscribe(f"device/{serial}/report")
            except Exception:
                pass
        if self.connected:
            try:
                on_status(True, "")
            except Exception:
                pass

    def detach(self, serial: str) -> None:
        with self._lock:
            self._attached.pop(serial, None)
        if self.client:
            try:
                self.client.unsubscribe(f"device/{serial}/report")
            except Exception:
                pass

    def publish(self, serial: str, payload: dict) -> None:
        """Команда принтеру через облако (топик device/{serial}/request)."""
        if not (self.client and self.connected):
            raise ConnectionError("Нет связи с облаком Bambu: выполните вход в аккаунт")
        result = self.client.publish(f"device/{serial}/request",
                                     json.dumps(payload), qos=0)
        if getattr(result, "rc", 0) != 0:
            raise ConnectionError(f"Облако не приняло команду (MQTT {result.rc})")

    # -------------------------------------------------------------- paho-события
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        value = getattr(reason_code, "value", reason_code)
        try:
            rc = int(value)
        except (TypeError, ValueError):
            rc = 0
        self.connecting = False
        self.connected = rc == 0
        if not self.connected:
            self.last_error = (f"Bambu Cloud отклонил MQTT-подключение (код {rc}). "
                               f"Токен устарел или аккаунт заблокирован — войдите заново.")
            return
        self.last_error = ""
        with self._lock:
            handlers = list(self._attached.items())
        for serial, (_, on_status) in handlers:
            try:
                client.subscribe(f"device/{serial}/report")
            except Exception:
                pass
        for _, (_, on_status) in handlers:
            try:
                on_status(True, "")
            except Exception:
                pass

    def _on_disconnect(self, client, userdata, disconnect_flags=None, reason_code=0,
                       properties=None):
        was = self.connected
        self.connected = False
        if was:
            with self._lock:
                handlers = list(self._attached.values())
            for _, on_status in handlers:
                try:
                    on_status(False, f"Облачное соединение потеряно (код {reason_code})")
                except Exception:
                    pass

    def _on_message(self, client, userdata, message):
        parts = str(message.topic or "").split("/")
        if len(parts) < 3 or parts[0] != "device" or parts[2] != "report":
            return
        serial = parts[1]
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except Exception:
            return
        self._last_message = time.time()
        with self._lock:
            handler = self._attached.get(serial)
        if handler:
            try:
                handler[0](serial, payload)
            except Exception:
                pass

    def state(self) -> dict:
        with self._lock:
            attached = len(self._attached)
        return {
            "region": self.region,
            "uid": self.uid,
            "logged": bool(self.token and self.uid),
            "connected": self.connected,
            "connecting": self.connecting,
            "attached": attached,
            "last_error": self.last_error,
        }

"""Локальный JPEG-поток камеры Bambu Lab P1/X1 (порт 6000, TLS).

Работает только в локальной сети принтера. Кадры хранятся в памяти и
раздаются интерфейсу как обычный JPEG или MJPEG-поток.
"""
from __future__ import annotations

import socket
import ssl
import struct
import threading
import time


class CameraWorker:
    """Фоновый поток, который держит соединение и хранит последний кадр."""

    PORT = 6000

    def __init__(self, get_config):
        self.get_config = get_config
        self.frame: bytes | None = None
        self.frame_at = 0.0
        self.error = ""
        self.fps = 0.0
        self._frames_window: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._subscribers: set[threading.Event] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pf-camera", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def subscribe(self) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._subscribers.add(event)
        return event

    def unsubscribe(self, event: threading.Event) -> None:
        with self._lock:
            self._subscribers.discard(event)

    def _publish(self, frame: bytes) -> None:
        self.frame = frame
        self.frame_at = time.time()
        self._frames_window.append(self.frame_at)
        self._frames_window = [t for t in self._frames_window if self.frame_at - t <= 5]
        self.fps = round(len(self._frames_window) / 5.0, 1)
        with self._lock:
            for event in list(self._subscribers):
                event.set()

    @staticmethod
    def _auth_packet(code: str) -> bytes:
        return (struct.pack("<IIII", 0x40, 0x3000, 0, 0)
                + b"bblp".ljust(32, b"\0")
                + code.encode("ascii", "ignore").ljust(32, b"\0"))

    def _run(self) -> None:
        while not self._stop.is_set():
            cfg = self.get_config() or {}
            host, code = cfg.get("host"), cfg.get("access_code")
            if not host or not code:
                self._stop.wait(3)
                continue
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                with socket.create_connection((host, self.PORT), timeout=8) as raw:
                    with context.wrap_socket(raw, server_hostname=host) as sock:
                        sock.settimeout(12)
                        sock.sendall(self._auth_packet(code))
                        self.error = ""
                        buf = bytearray()
                        while not self._stop.is_set():
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
                                self._publish(bytes(buf[start:end + 2]))
                                del buf[:end + 2]
            except Exception as exc:  # соединение восстанавливается автоматически
                self.error = str(exc)
                self._stop.wait(4)

    def state(self) -> dict:
        return {
            "available": bool(self.frame),
            "age": round(time.time() - self.frame_at, 1) if self.frame_at else None,
            "fps": self.fps,
            "error": self.friendly_error(),
        }

    def friendly_error(self) -> str:
        """Понятный текст вместо системного сообщения сокета."""
        raw = (self.error or "").lower()
        if not raw:
            return ""
        if "timed out" in raw or "timeout" in raw:
            return "Принтер не отвечает по порту 6000"
        if "refused" in raw:
            return "Камера отключена в настройках принтера"
        if "unreachable" in raw or "not known" in raw or "resolve" in raw:
            return "Принтер недоступен в сети"
        if "закрыла соединение" in raw:
            return "Камера разорвала соединение, переподключаемся"
        return self.error

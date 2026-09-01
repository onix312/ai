"""Локальный JPEG-поток камеры Bambu Lab P1/X1 (порт 6000, TLS).

Работает только в локальной сети принтера. Кадры хранятся в памяти и
раздаются интерфейсу как обычный JPEG или MJPEG-поток.

Если принтер недоступен (например, интерфейс смотрят не из домашней сети),
включается демонстрационный режим: вместо живого видео проигрываются
заготовленные кадры из site/assets/demo. Это позволяет проверить интерфейс
без принтера, но честно помечается флагом ``demo``.
"""
from __future__ import annotations

import socket
import ssl
import struct
import threading
import time

from .config import SITE

DEMO_DIR = SITE / "assets" / "demo"


def demo_frames() -> list[bytes]:
    """Кадры демонстрационного потока, загруженные один раз."""
    global _DEMO_CACHE
    if _DEMO_CACHE is None:
        frames = []
        for path in sorted(DEMO_DIR.glob("cam-*.jpg")):
            try:
                frames.append(path.read_bytes())
            except OSError:
                continue
        _DEMO_CACHE = frames
    return _DEMO_CACHE


_DEMO_CACHE: list[bytes] | None = None


class CameraWorker:
    """Фоновый поток, который держит соединение и хранит последний кадр."""

    PORT = 6000

    def __init__(self, get_config):
        self.get_config = get_config
        self.frame: bytes | None = None
        self.frame_at = 0.0
        self.error = ""
        self.fps = 0.0
        self.demo = False          # показываем заготовленные кадры
        self._no_lan = False       # облачный режим без локального IP
        self.snapshots: list[dict] = []  # архив кадров печати (таймлапс)
        self._demo_index = 0
        self._frames_window: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._subscribers: set[threading.Event] = set()
        self._lock = threading.Lock()

    @staticmethod
    def _tls_context() -> ssl.SSLContext:
        """TLS-контекст для камеры.

        Принтеры Bambu отдают камеру со слабыми ключами: OpenSSL 3.x с
        SECLEVEL=2 по умолчанию отвергает рукопожатие («no suitable key
        share», «dh key too small») — и камера «ломается» после обновления
        Python, хотя принтер здоров. Для FTPS это уже чинили (ftps.py),
        здесь — тот же фикс во всех местах подключения.
        """
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            context.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
        except (AttributeError, ValueError):   # старые сборки ssl
            pass
        try:
            context.set_ciphers("DEFAULT@SECLEVEL=1")
        except ssl.SSLError:
            pass
        return context


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

    def _demo_tick(self) -> bool:
        """Показать следующий демо-кадр. False, если демо выключено."""
        cfg = self.get_config() or {}
        # Если принудительно включен демо-режим (например, камера недоступна),
        # показываем демо-кадры независимо от настройки cfg.demo.
        if not cfg.get("demo") and not self.demo:
            return False
        frames = demo_frames()
        if not frames:
            return False
        self.demo = True
        self.error = ""
        self._publish(frames[self._demo_index % len(frames)])
        self._demo_index += 1
        self._stop.wait(2.5)
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            cfg = self.get_config() or {}
            host, code = cfg.get("host"), cfg.get("access_code")
            if not host or not code:
                # Облачный режим без локального IP: камера физически работает
                # только по LAN (порт 6000) — объясняем это честно в статусе.
                self._no_lan = bool(cfg.get("cloud")) and not host
                if self._demo_tick():
                    continue
                self.demo = False
                self._stop.wait(3)
                continue
            self._no_lan = False
            try:
                with socket.create_connection((host, self.PORT), timeout=8) as raw:
                    with self._tls_context().wrap_socket(raw, server_hostname=host) as sock:
                        sock.settimeout(12)
                        sock.sendall(self._auth_packet(code))
                        self.error = ""
                        self.demo = False
                        buf = bytearray()
                        # Ограничитель FPS: камера отдаёт до ~30 к/с, для наблюдения
                        # достаточно camera_fps_max; лишние кадры отбрасываются до
                        # публикации — CPU ниже, последний кадр всегда свежий.
                        fps_max = 0.0
                        try:
                            fps_max = float((self.get_config() or {}).get("camera_fps_max") or 0)
                        except (TypeError, ValueError):
                            fps_max = 0.0
                        min_interval = (1.0 / fps_max) if fps_max > 0 else 0.0
                        last_pub = 0.0
                        while not self._stop.is_set():
                            chunk = sock.recv(262144)
                            if not chunk:
                                raise ConnectionError("камера закрыла соединение")
                            buf.extend(chunk)
                            while True:
                                start = buf.find(b"\xff\xd8\xff")
                                if start < 0:
                                    if len(buf) > 4_000_000:
                                        del buf[:-4]
                                    break
                                end = buf.find(b"\xff\xd9", start + 3)
                                if end < 0:
                                    if start:
                                        del buf[:start]
                                    break
                                frame = bytes(buf[start:end + 2])
                                del buf[:end + 2]
                                now = time.monotonic()
                                if now - last_pub >= min_interval:
                                    last_pub = now
                                    self._publish(frame)
            except Exception as exc:  # соединение восстанавливается автоматически
                self.error = str(exc)
                # Если камера недоступна, переходим в демо-режим,
                # если пользователь не отключил демо полностью.
                if not self.demo:
                    self.demo = True
                if not self._demo_tick():
                    self._stop.wait(4)

    # ------------------------------------------------------------ снимки
    def snapshot(self, note: str = "", job_id: str = "") -> dict:
        """Сохранить текущий кадр в архив (память, максимум 60 штук)."""
        if not self.frame:
            raise ValueError("Кадр ещё не получен — камера не отвечает")
        item = {
            "id": f"shot{int(time.time() * 1000)}",
            "at": time.time(),
            "note": note,
            "job_id": job_id,
            "frame": self.frame,
        }
        with self._lock:
            self.snapshots.append(item)
            del self.snapshots[:-60]
        return {k: v for k, v in item.items() if k != "frame"}

    def snapshot_list(self) -> list[dict]:
        with self._lock:
            return [{k: v for k, v in s.items() if k != "frame"}
                    for s in reversed(self.snapshots)]

    def snapshot_frame(self, shot_id: str) -> bytes | None:
        with self._lock:
            return next((s["frame"] for s in self.snapshots if s["id"] == shot_id), None)

    def state(self) -> dict:
        error = "" if self.demo else self.friendly_error()
        if not error and not self.frame and self._no_lan:
            error = ("Камера работает по локальной сети (порт 6000). "
                     "Укажите IP принтера в карточке или включите демо-режим.")
        return {
            "available": bool(self.frame),
            "age": round(time.time() - self.frame_at, 1) if self.frame_at else None,
            "fps": self.fps,
            "demo": self.demo,
            "shots": len(self.snapshots),
            "error": error,
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


# ------------------------------------------------- диагностика (роадмап 0.4)

def port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    """Открыт ли TCP-порт принтера."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def tls_handshake(host: str, port: int, timeout: float = 4.0) -> tuple[bool, str]:
    """TLS-рукопожатие с камерой (сертификат не проверяем — принтер свой)."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with CameraWorker._tls_context().wrap_socket(raw, server_hostname=host):
                return True, "камера отвечает по TLS"
    except OSError as exc:
        return False, str(exc)


def grab_frame(host: str, code: str, timeout: float = 6.0) -> tuple[bool, str]:
    """Авторизация и ожидание первого JPEG-кадра — честная проверка «до конца»."""
    try:
        with socket.create_connection((host, 6000), timeout=timeout) as raw:
            with CameraWorker._tls_context().wrap_socket(raw, server_hostname=host) as sock:
                sock.settimeout(timeout)
                sock.sendall(CameraWorker._auth_packet(code))
                buf = bytearray()
                deadline = time.time() + timeout
                while time.time() < deadline:
                    chunk = sock.recv(65536)
                    if not chunk:
                        return False, "камера закрыла соединение до кадра"
                    buf.extend(chunk)
                    if buf.find(b"\xff\xd9") >= 0:
                        return True, "первый кадр получен"
                return False, "соединение есть, но кадр не пришёл за отведённое время"
    except OSError as exc:
        return False, str(exc)


def diagnose(printer) -> dict:
    """Пошаговая диагностика камеры: каждый этап — честный вердикт.

    Этапы: данные для подключения → TCP-порт 6000 → TLS-рукопожатие →
    авторизация и первый кадр. Возвращает список шагов и сводку.
    """
    record = dict(getattr(printer, "record", {}) or {})
    host = str(record.get("host") or "").strip()
    code = str(record.get("access_code") or "").strip()
    steps: list[dict] = []

    steps.append({
        "step": "IP-адрес принтера", "ok": bool(host),
        "text": (f"указан: {host}" if host else
                 "не указан. Камера работает только по LAN (порт 6000). "
                 "Посмотрите IP на экране принтера (Настройки → WLAN) и "
                 "впишите его в карточку принтера.")})
    steps.append({
        "step": "Access Code", "ok": bool(code),
        "text": ("сохранён на сервере" if code else
                 "не сохранён — добавьте в карточку принтера. "
                 "Находится на экране принтера: Настройки → WLAN.")})
    if not host:
        steps.append({"step": "TCP-порт 6000", "ok": False, "text": "пропущено — нет IP"})
        steps.append({"step": "TLS-рукопожатие", "ok": False, "text": "пропущено — нет IP"})
    else:
        open_port = port_open(host, 6000)
        steps.append({
            "step": "TCP-порт 6000", "ok": open_port,
            "text": ("порт открыт" if open_port else
                     "порт закрыт: принтер не в сети, выключен, или камера "
                     "отключена (на принтере: Настройки → Камера / LAN Liveview).")})
        if open_port:
            ok_tls, text_tls = tls_handshake(host, 6000)
            steps.append({"step": "TLS-рукопожатие", "ok": ok_tls,
                          "text": text_tls if ok_tls else f"не удалось: {text_tls}"})
        else:
            steps.append({"step": "TLS-рукопожатие", "ok": False,
                          "text": "пропущено — порт не открылся"})
    if host and code and open_port and ok_tls:
        ok_frame, text_frame = grab_frame(host, code)
        steps.append({"step": "Первый кадр", "ok": ok_frame, "text": text_frame})
    elif host and code and open_port:
        steps.append({"step": "Первый кадр", "ok": False, "text": "пропущено — TLS не прошёл"})
    else:
        steps.append({"step": "Первый кадр", "ok": False,
                      "text": "пропущено — не хватает IP, кода или порта"})

    camera = getattr(printer, "camera", None)
    live = bool(camera and camera.frame)
    if live:
        steps.append({"step": "Живой поток в панели", "ok": True,
                      "text": "кадры уже идут — интерфейс показывает камеру"})
    ok_all = all(step["ok"] for step in steps)
    # X1 и новые прошивки отдают RTSP на порту 322 — подсказываем как запасной путь.
    if host:
        rtsp_open = port_open(host, 322)
        steps.append({
            "step": "RTSP (порт 322, фолбэк)", "ok": rtsp_open,
            "text": ("порт открыт — можно смотреть поток в VLC (кнопка RTSP)" if rtsp_open else
                     "не обязателен для P1S: RTSP-поток есть на X1 и новых прошивках")})
    return {"ok": ok_all, "steps": steps,
            "summary": ("Камера полностью исправна" if ok_all
                        else "Найдены проблемы — смотрите шаги выше")}


def rtsp_link(printer) -> str | None:
    """Ссылка на RTSP-поток для внешнего плеера (X1 / новые прошивки).

    Содержит Access Code — отдаётся только по явному запросу, не попадает
    в bootstrap и обычные ответы API.
    """
    record = dict(getattr(printer, "record", {}) or {})
    host = str(record.get("host") or "").strip()
    code = str(record.get("access_code") or "").strip()
    if not host or not code:
        return None
    return f"rtsps://bblp:{code}@{host}:322/streaming/live/1"

"""Фиксы камеры 15.2: TLS-контекст (SECLEVEL=1) и ограничитель FPS.

Причина «сломавшейся» камеры: OpenSSL 3.x с системным SECLEVEL=2 отвергает
рукопожатие Bambu со слабыми ключами — тот же корень, что чинили в ftps.py.
Здесь проверяется, что CameraWorker во всех местах подключения использует
контекст с SECLEVEL=1 и что лимитер кадров действительно прореживает поток.
"""
from __future__ import annotations

import pathlib
import ssl
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.camera import CameraWorker  # noqa: E402

JPEG = b"\xff\xd8\xff" + b"\x00" * 32 + b"\xff\xd9"   # минимальный JPEG-маркер


class TlsContextTests(unittest.TestCase):
    def test_context_ignores_certificate(self):
        ctx = CameraWorker._tls_context()
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertFalse(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)

    def test_weak_keys_are_allowed(self):
        """SECLEVEL=1 применён: security_level падает с системной двойки."""
        ctx = CameraWorker._tls_context()
        level = getattr(ctx, "security_level", None)
        if level is None:                      # совсем старый OpenSSL
            self.skipTest("ssl.security_level недоступен")
        self.assertEqual(level, 1)

    def test_minimum_version_lowered(self):
        ctx = CameraWorker._tls_context()
        if not hasattr(ssl, "TLSVersion"):
            self.skipTest("TLSVersion недоступен")
        self.assertEqual(ctx.minimum_version, ssl.TLSVersion.MINIMUM_SUPPORTED)

    def test_context_used_in_all_paths(self):
        """Фикс должен жить в _tls_context, а не быть забыт в одном месте."""
        source = pathlib.Path(CameraWorker.__module__.replace(".", "/") + ".py")
        for path in (ROOT / "connector" / "printflow" / "camera.py", source):
            text = path.read_text(encoding="utf-8")
            self.assertIn("_tls_context", text)
            self.assertNotIn("ssl._create_unverified_context", text)


class _FakeRaw:
    """Подставной TCP-сокет под with socket.create_connection(...)."""

    def __init__(self, calls: dict):
        self._calls = calls

    def settimeout(self, value):
        self._calls.setdefault("timeouts", []).append(value)

    def sendall(self, data):
        self._calls["raw_send"] = len(data)

    def close(self):
        self._calls["closed"] = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _FakeSSLSock:
    """Подставной TLS-сокет: сыплет заготовленными JPEG-кадрами."""

    def __init__(self, frames: list[bytes], calls: dict):
        self._chunks = list(frames)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._calls = calls

    def settimeout(self, value):
        self._calls["timeout"] = value

    def sendall(self, data):
        self._calls["auth"] = data

    def recv(self, bufsize):
        self._calls["bufsize"] = max(self._calls.get("bufsize", 0), bufsize)
        with self._lock:
            if self._chunks:
                return self._chunks.pop(0)
        self._stop.wait(1.0)                   # камера «молчит» до остановки
        return b""

    def close(self):
        self._stop.set()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FpsGateTests(unittest.TestCase):
    def _run_worker(self, fps_max: float, seconds: float, host: str = "printer.lan"
                    ) -> tuple[CameraWorker, dict]:
        frames = [JPEG + bytes([i]) for i in range(256)]
        calls: dict = {}
        fake = _FakeSSLSock(frames, calls)
        raw = _FakeRaw(calls)
        worker = CameraWorker(lambda: {"host": host, "access_code": "code",
                                       "camera_fps_max": fps_max})
        worker._tls_context = lambda: mock.Mock(
            wrap_socket=lambda sock, server_hostname=None: fake)
        with mock.patch("connector.printflow.camera.socket.create_connection",
                        lambda *a, **k: raw):
            worker.start()
            time.sleep(seconds)
            worker.stop()
            worker._thread.join(timeout=3)
        return worker, calls

    def test_fps_limiter_throttles_publish(self):
        """При camera_fps_max=5 за 0.9 с публикаций ≈4, а не весь поток."""
        worker, _ = self._run_worker(5.0, 0.9)
        self.assertIsNotNone(worker.frame)
        self.assertLessEqual(worker.fps, 6.5)          # окно 5 с, запас на рвани
        self.assertGreater(worker.fps, 0.0)

    def test_unlimited_still_publishes_fast(self):
        """Без лимита кадры летят без прореживания (fps заметно выше 5)."""
        worker, _ = self._run_worker(0.0, 0.9)
        self.assertIsNotNone(worker.frame)
        self.assertGreater(worker.fps, 5.0)

    def test_recv_buffer_enlarged(self):
        """Буфер приёма поднят до 256 КБ — камера не должна душить CPU."""
        _, calls = self._run_worker(0.0, 0.5)
        self.assertGreaterEqual(calls.get("bufsize", 0), 262144)

    def test_bad_fps_value_is_tolerated(self):
        """Мусор в настройке не роняет поток — работает как «без лимита»."""
        worker, _ = self._run_worker("абракадабра", 0.4)
        self.assertIsNotNone(worker.frame)

    def test_state_reports_fps(self):
        worker, _ = self._run_worker(0.0, 0.4)
        state = worker.state()
        self.assertTrue(state["available"])
        self.assertIn("fps", state)


class FpsConfigTests(unittest.TestCase):
    def test_default_setting_exists(self):
        from connector.printflow.config import DEFAULT_SETTINGS

        self.assertEqual(float(DEFAULT_SETTINGS.get("camera_fps_max", -1)), 0.0)

    def test_settings_schema_has_printers_field(self):
        from connector.printflow.settings_schema import GROUPS, META

        self.assertIn("camera_fps_max", META)
        group, label, opts = META["camera_fps_max"]
        self.assertEqual(group, "printers")
        self.assertIn("FPS", label)
        self.assertEqual(int(opts.get("min", -1)), 0)
        self.assertEqual(int(opts.get("max", -1)), 30)
        self.assertIn(group, GROUPS)               # группа описана в справочнике


if __name__ == "__main__":
    unittest.main()

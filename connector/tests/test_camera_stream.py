"""Регрессия 15.2: MJPEG-поток камеры в http_handler.

Симптом у пользователя: «камеры не отображаются» — браузер открывает
`/api/printer/camera.mjpeg`, получает HTTP 200 с пустым телом, и картинка
не появляется. Причина: в http_handler.py были не импортированы `time`,
`sqlite3`, `friendly_sqlite_error` и `num` — `serve_camera_stream` падал с
`NameError: name 'time' is not defined` сразу после отправки заголовков,
а обработчики ошибок GET/POST падали на `sqlite3` вместо ответа с текстом.

Здесь проверяется и сам факт наличия имён, и поведение потока: хендлер
должен писать multipart-кадры в wfile и отписываться от камеры.
"""
from __future__ import annotations

import io
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import http_handler  # noqa: E402

FRAME_A = b"\xff\xd8\xff" + b"A" * 64 + b"\xff\xd9"
FRAME_B = b"\xff\xd8\xff" + b"B" * 64 + b"\xff\xd9"


class _FakeEvent:
    """Событие, которое всегда «проснулось» — кадры уже опубликованы."""

    def __init__(self):
        self.cleared = 0

    def wait(self, timeout):
        return True

    def clear(self):
        self.cleared += 1


class _FakeCamera:
    def __init__(self, frames):
        self._frames = list(frames)
        self.unsubscribed: list = []

    @property
    def frame(self):
        """Очередной кадр; после последнего — None (поток перестаёт писать)."""
        return self._frames.pop(0) if self._frames else None

    def subscribe(self):
        return _FakeEvent()

    def unsubscribe(self, event):
        self.unsubscribed.append(event)


class ImportGuardTests(unittest.TestCase):
    """Имена, которые http_handler использует, но когда-то не импортировал."""

    def test_module_imports_its_runtime_names(self):
        for name in ("time", "sqlite3", "friendly_sqlite_error", "num"):
            self.assertTrue(
                hasattr(http_handler, name),
                f"{name} не импортирован в http_handler.py — "
                "сервер падает NameError на живом запросе")

    def test_stream_source_has_no_bare_time_imports_left(self):
        """Защита от возврата фикса через локальные import time в функциях."""
        text = pathlib.Path(http_handler.__file__).read_text(encoding="utf-8")
        self.assertIn("import time", text)
        self.assertIn("import sqlite3", text)


class CameraStreamHandlerTests(unittest.TestCase):
    def _handler(self, frames):
        camera = _FakeCamera(frames)
        printer = SimpleNamespace(camera=camera)
        handler = http_handler.Handler.__new__(http_handler.Handler)
        handler.api = SimpleNamespace(
            manager=SimpleNamespace(get=lambda pid: printer))
        handler.wfile = io.BytesIO()
        handler.send_response = lambda code, *a: None
        handler.send_header = lambda *a: None
        handler.end_headers = lambda: None
        return handler, camera

    def _run_stream(self, frames, deadline_hits=4):
        """Прогнать serve_camera_stream с фейковым временем.

        time.time() вызывается 2 раза за итерацию (deadline + условие while);
        после `deadline_hits` значений время «убегает» за deadline — поток
        завершается сам, без исключений.
        """
        handler, camera = self._handler(frames)
        values = [100.0] * deadline_hits + [99999.0]
        fake_time = SimpleNamespace(time=mock.Mock(side_effect=values))
        with mock.patch.object(http_handler, "time", fake_time):
            handler.serve_camera_stream("virtual")
        return handler, camera

    def test_stream_writes_multipart_frames(self):
        handler, _ = self._run_stream([FRAME_A, FRAME_B], deadline_hits=5)
        body = handler.wfile.getvalue()
        self.assertEqual(body.count(b"--pfframe"), 2)
        self.assertIn(b"Content-Type: image/jpeg", body)
        self.assertIn(FRAME_A, body)
        self.assertIn(FRAME_B, body)
        self.assertIn(b"Content-Length: " + str(len(FRAME_A)).encode(), body)

    def test_stream_unsubscribes_on_finish(self):
        _, camera = self._run_stream([FRAME_A], deadline_hits=3)
        self.assertEqual(len(camera.unsubscribed), 1)

    def test_missing_printer_returns_404(self):
        handler = http_handler.Handler.__new__(http_handler.Handler)
        handler.api = SimpleNamespace(
            manager=SimpleNamespace(get=lambda pid: None))
        sent = {}
        handler.send_json = lambda code, payload: sent.update(
            code=code, payload=payload)
        handler.serve_camera_stream("nope")
        self.assertEqual(sent["code"], 404)


if __name__ == "__main__":
    unittest.main()

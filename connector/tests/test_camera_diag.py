"""Диагностика камеры по этапам (0.4) и RTSP-фолбэк (0.3)."""
from __future__ import annotations

import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import camera  # noqa: E402


class FakePrinter:
    def __init__(self, host="192.168.1.42", code="ABCD1234", frame=False):
        self.record = {"host": host, "access_code": code, "name": "P1S"}
        self.camera = SimpleNamespace(frame=b"jpeg" if frame else None)


class DiagnoseTests(unittest.TestCase):
    def test_all_green_when_network_ok(self):
        printer = FakePrinter()
        with mock.patch.object(camera, "port_open", return_value=True), \
             mock.patch.object(camera, "tls_handshake",
                               return_value=(True, "камера отвечает по TLS")), \
             mock.patch.object(camera, "grab_frame",
                               return_value=(True, "первый кадр получен")):
            result = camera.diagnose(printer)
        self.assertTrue(result["ok"])
        self.assertTrue(all(step["ok"] for step in result["steps"]))

    def test_reports_missing_ip_honestly(self):
        printer = FakePrinter(host="", code="")
        result = camera.diagnose(printer)
        self.assertFalse(result["ok"])
        by_step = {step["step"]: step for step in result["steps"]}
        self.assertFalse(by_step["IP-адрес принтера"]["ok"])
        self.assertIn("не указан", by_step["IP-адрес принтера"]["text"])
        self.assertFalse(by_step["Access Code"]["ok"])
        self.assertIn("не сохранён", by_step["Access Code"]["text"])

    def test_closed_port_has_advice(self):
        printer = FakePrinter()
        with mock.patch.object(camera, "port_open", return_value=False):
            result = camera.diagnose(printer)
        port_step = next(s for s in result["steps"] if s["step"] == "TCP-порт 6000")
        self.assertFalse(port_step["ok"])
        self.assertIn("LAN Liveview", port_step["text"])

    def test_frame_failure_after_tls(self):
        printer = FakePrinter()
        with mock.patch.object(camera, "port_open", return_value=True), \
             mock.patch.object(camera, "tls_handshake",
                               return_value=(True, "ок")), \
             mock.patch.object(camera, "grab_frame",
                               return_value=(False, "авторизация не прошла")):
            result = camera.diagnose(printer)
        frame_step = next(s for s in result["steps"] if s["step"] == "Первый кадр")
        self.assertFalse(frame_step["ok"])
        self.assertFalse(result["ok"])

    def test_rtsp_link_contains_code_only_on_request(self):
        printer = FakePrinter()
        link = camera.rtsp_link(printer)
        self.assertEqual(link, "rtsps://bblp:ABCD1234@192.168.1.42:322/streaming/live/1")
        self.assertIsNone(camera.rtsp_link(FakePrinter(host="", code="")))
        self.assertIsNone(camera.rtsp_link(FakePrinter(code="")))


if __name__ == "__main__":
    unittest.main()

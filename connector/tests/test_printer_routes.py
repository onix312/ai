"""Маршруты принтеров в реестре: перенос из if-цепочки без смены поведения.

Шесть маршрутов чтения переехали из `Api.get()` в `routes_printers.py`
(17.0.20). Здесь проверяется то, что легко потерять при переносе: те же ключи
в ответе, те же параметры, та же ошибка на неизвестном принтере — и что
if-цепочка не осталась рядом мёртвой веткой.
"""
from __future__ import annotations

import pathlib
import tempfile
import types
import unittest

from connector.printflow.db import Database

ROOT = pathlib.Path(__file__).resolve().parents[2]
API_SOURCE = (ROOT / "connector" / "printflow" / "api.py").read_text(encoding="utf-8")

PORTED = ("/api/printers", "/api/printer/telemetry", "/api/printer/maintenance",
          "/api/printer/alerts", "/api/printer/shots", "/api/printer/health")


class _Guard:
    def __init__(self):
        self.telemetry_calls: list[tuple[str, int]] = []

    def telemetry(self, printer_id: str, minutes: int):
        self.telemetry_calls.append((printer_id, minutes))
        return [{"printer_id": printer_id, "minutes": minutes}]

    def maintenance(self, printer_id: str):
        return [{"printer_id": printer_id, "task": "смазка"}]

    def runtime_hours(self, printer_id: str):
        return {"nozzle": 12.5}

    def alerts(self, printer_id: str):
        return [{"printer_id": printer_id, "text": "износ сопла"}]


class _Printer:
    id = "p1"
    record = {"id": "p1", "name": "P1S"}

    def __init__(self, health=None):
        self.camera = types.SimpleNamespace(
            snapshot_list=lambda: [{"name": "shot_1.jpg"}])
        if health is not None:
            self.health = health


class PrinterRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(pathlib.Path(self.tmp.name) / "routes.sqlite3")
        self.addCleanup(self.db.close)
        self.db.upsert("printers", {"id": "p1", "name": "P1S", "enabled": 1,
                                    "position": 0})
        from connector.printflow.api import Api
        from connector.printflow import router as router_module
        router_module.register_all()
        self.api = Api.__new__(Api)
        self.api.db = self.db
        self.api.router = router_module.router
        self.api.repo = types.SimpleNamespace(
            printers=lambda: [{"id": "p1", "name": "P1S"}])
        self.guard = _Guard()
        self.printer = _Printer(health=lambda: {"ok": True, "nozzle": 12})
        self.api.manager = types.SimpleNamespace(guard=self.guard)
        self.api.printer_or_fail = lambda printer_id="": (
            self.printer if printer_id == "p1"
            else (_ for _ in ()).throw(ValueError("Принтер не настроен.")))

    def test_printers_list(self):
        code, payload = self.api.get("/api/printers", {})
        self.assertEqual(200, code)
        self.assertEqual([{"id": "p1", "name": "P1S"}], payload["printers"])

    def test_telemetry_keeps_default_window(self):
        code, payload = self.api.get("/api/printer/telemetry", {"printer_id": ["p1"]})
        self.assertEqual(200, code)
        self.assertEqual("points" in payload and len(payload["points"]), 1)
        self.assertEqual([("p1", 180)], self.guard.telemetry_calls)

    def test_telemetry_accepts_minutes(self):
        self.api.get("/api/printer/telemetry",
                     {"printer_id": ["p1"], "minutes": ["30"]})
        self.assertEqual([("p1", 30)], self.guard.telemetry_calls)

    def test_maintenance_returns_tasks_and_hours(self):
        code, payload = self.api.get("/api/printer/maintenance", {"printer_id": ["p1"]})
        self.assertEqual(200, code)
        self.assertEqual([{"printer_id": "p1", "task": "смазка"}], payload["tasks"])
        self.assertEqual({"nozzle": 12.5}, payload["hours"])

    def test_alerts(self):
        code, payload = self.api.get("/api/printer/alerts", {"printer_id": ["p1"]})
        self.assertEqual(200, code)
        self.assertEqual([{"printer_id": "p1", "text": "износ сопла"}], payload["alerts"])

    def test_shots(self):
        code, payload = self.api.get("/api/printer/shots", {"printer_id": ["p1"]})
        self.assertEqual(200, code)
        self.assertEqual([{"name": "shot_1.jpg"}], payload["shots"])

    def test_health_without_method(self):
        self.printer = _Printer(health=None)
        code, payload = self.api.get("/api/printer/health", {"printer_id": ["p1"]})
        self.assertEqual(200, code)
        self.assertEqual({"ok": False}, payload)

    def test_health_with_method(self):
        code, payload = self.api.get("/api/printer/health", {"printer_id": ["p1"]})
        self.assertEqual(200, code)
        self.assertEqual({"ok": True, "nozzle": 12}, payload)

    def test_unknown_printer_still_raises(self):
        for path in ("/api/printer/shots", "/api/printer/health"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    self.api.get(path, {"printer_id": ["нет"]})


class PortedBranchesAreGoneTests(unittest.TestCase):
    """Ветка if-цепочки после переноса мертва: до неё не доходит очередь."""

    def test_no_dead_if_branches_left(self):
        for path in PORTED:
            with self.subTest(path=path):
                self.assertNotIn(f'if path == "{path}":', API_SOURCE,
                                 "ветка осталась в if-цепочке и теперь мертва")

    def test_every_ported_path_is_in_the_registry(self):
        from connector.printflow import router as router_module
        router_module.register_all()
        declared = {(r["method"], r["path"]) for r in router_module.router.reference()}
        for path in PORTED:
            with self.subTest(path=path):
                self.assertIn(("GET", path), declared)

    def test_ported_routes_appear_in_the_spec(self):
        from connector.printflow import openapi as service
        from connector.printflow import router as router_module
        router_module.register_all()
        paths = (service.build().get("paths") or {})
        for path in PORTED:
            with self.subTest(path=path):
                self.assertIn(path, paths,
                              "перенесённый маршрут не попал в спецификацию")


if __name__ == "__main__":
    unittest.main()

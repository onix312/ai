"""Сводка пульта цеха: `GET /api/pult/summary` (18.0.4).

Пульт — телефон в руке у станка, ему нужны парк, очередь, память слотов AMS и
катушки склада. Раньше это были четыре запроса на каждое обновление; теперь
один. Здесь проверяется и служба (`printflow/pult.py`), и маршрут: форма
ответа должна совпадать со снимком парка, иначе страница отрисует пустоту.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.api import register_routes  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.pult import SPOOL_LIMIT, spools_for_binding, summary  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402
from connector.printflow.router import router  # noqa: E402


def printer_snapshot() -> dict:
    """Снимок одного принтера в том виде, что отдаёт manager.snapshot()."""
    return {
        "id": "prn1", "name": "Цех-1", "model": "P1S",
        "connection": {"connected": True, "configured": True, "mode": "local",
                       "last_error": ""},
        "printer": {"state": "RUNNING", "state_label": "Печатает", "task": "Подставка",
                    "progress": 42.0, "remaining_min": 65.0, "eta": 1789344000.0,
                    "layer": 84, "total_layers": 200, "speed_level": 2,
                    "elapsed_min": 40.0},
        "temperature": {"nozzle": 215.0, "bed": 60.0},
        "light": "on",
        "ams": {"trays": [
            {"id": "00", "slot": 0, "label": "Слот 1", "type": "PETG", "color": "#1F2937",
             "remain": 64, "uuid": "ABC", "active": True, "present": True},
            {"id": "01", "slot": 1, "label": "Слот 2", "type": "", "color": "#CBD5E1",
             "remain": None, "uuid": "", "active": False, "present": False},
        ], "active_tray": "0"},
        "camera": {"available": True, "demo": False, "error": "", "age": 1.5, "shots": 3},
        "guard": {"alerts": [{"kind": "filament", "severity": "warn",
                              "title": "Пластик заканчивается",
                              "reason": "Слот 1: осталось 12%."}]},
        "job": {"name": "stand.gcode.3mf",
                "order": {"number": "1042", "product": "Подставка"}},
        "maintenance": {"due": 0},
    }


class FakeManager:
    def __init__(self, snap: dict | None = None, queue: list | None = None):
        self.snap = snap if snap is not None else printer_snapshot()
        self.queue = queue if queue is not None else [{
            "id": "job1", "name": "Подставка", "file": "stand.gcode.3mf", "state": "queued",
            "plate": 1, "printer_id": "", "est_minutes": 90, "est_grams": 40, "priority": 5,
            "created_at": "2026-09-14T09:00:00+00:00", "order": {"number": "1042"},
        }]
        self.seen_printer_id = None

    def snapshot(self, printer_id: str = "") -> dict:
        self.seen_printer_id = printer_id
        return {
            "at": "2026-09-14T09:30:00+00:00",
            "printers": [self.snap] if self.snap else [],
            "active": self.snap,
            "queue": self.queue,
            "farm": {"total": 1, "online": 1, "printing": 1, "queued": 1},
            "quiet": None,
        }


class PultSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "t.sqlite3")
        self.repo = Repo(self.db)
        self.manager = FakeManager()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_summary_shape_matches_the_park_snapshot(self):
        """Плитки парка и очередь — в той же форме, что у /api/state."""
        data = summary(self.db, self.manager, "prn1")
        self.assertEqual("prn1", self.manager.seen_printer_id)
        printer = data["printers"][0]
        for key in ("id", "name", "connection", "printer", "temperature", "ams",
                    "camera", "guard", "job"):
            self.assertIn(key, printer, f"в плитке нет поля {key}")
        self.assertEqual("Печатает", printer["printer"]["state_label"])
        self.assertEqual(215.0, printer["temperature"]["nozzle"])
        self.assertEqual(2, len(printer["ams"]["trays"]))
        self.assertEqual(1, printer["ams"]["filled"])
        self.assertTrue(printer["camera"]["available"])
        self.assertEqual("Пластик заканчивается", printer["guard"]["alerts"][0]["title"])
        self.assertEqual("1042", printer["job"]["order"]["number"])
        # `at` — момент сборки ответа (свежесть данных для плашки пульта),
        # а не время внутри снимка: пульт показывает «обновлено в ЧЧ:ММ».
        self.assertRegex(data["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertEqual("P1S", printer["model"])
        self.assertEqual("on", printer["light"])

    def test_summary_counts_the_queue_and_alerts(self):
        data = summary(self.db, self.manager)
        self.assertEqual(1, len(data["queue"]))
        self.assertEqual("queued", data["queue"][0]["state"])
        self.assertEqual(90.0, data["queue"][0]["est_minutes"])
        self.assertEqual({"count": 1, "worst": "warn"}, data["alerts"])

    def test_summary_reports_an_error_alert_as_worst(self):
        snap = printer_snapshot()
        snap["guard"]["alerts"] = [{"severity": "warn", "title": "мелочь"},
                                   {"severity": "error", "title": "Печать не двигается"}]
        data = summary(self.db, FakeManager(snap))
        self.assertEqual(2, data["alerts"]["count"])
        self.assertEqual("error", data["alerts"]["worst"])

    def test_summary_carries_ams_memory_without_printer(self):
        """Память слотов приходит в сводке — даже когда принтер молчит."""
        self.repo.save_spool({"id": "sp_1", "material": "PETG", "color_name": "Чёрный",
                              "total_grams": 1000, "remaining_grams": 640,
                              "printer_id": "prn1", "ams_slot": "2"})
        silent = FakeManager()
        silent.snap = None
        silent.queue = []
        data = summary(self.db, silent, "prn1")
        slots = data["ams"]["slots"]
        self.assertEqual(1, len(slots))
        self.assertEqual("2", str(slots[0]["slot"]))
        self.assertEqual("PETG", slots[0]["material"])
        self.assertEqual("sp_1", slots[0]["spool_id"])
        self.assertIn("stale_min", data["ams"])
        self.assertEqual([], data["printers"])

    def test_spools_for_binding_is_short_and_free(self):
        for i in range(5):
            self.repo.save_spool({"id": f"sp_free{i}", "material": "PLA",
                                  "color_name": f"Цвет {i}", "total_grams": 1000,
                                  "remaining_grams": 100 + i * 50})
        self.repo.save_spool({"id": "sp_empty", "material": "PLA", "color_name": "Пустая",
                              "total_grams": 1000, "remaining_grams": 0})
        self.repo.save_spool({"id": "sp_arch", "material": "PLA", "color_name": "Архив",
                              "total_grams": 1000, "remaining_grams": 500, "archived": 1})
        self.repo.save_spool({"id": "sp_here", "material": "PETG", "color_name": "В слоте",
                              "total_grams": 1000, "remaining_grams": 300,
                              "printer_id": "prn1", "ams_slot": "1"})
        rows = spools_for_binding(self.db)
        ids = [r["id"] for r in rows]
        self.assertNotIn("sp_empty", ids, "пустая катушка не годится для привязки")
        self.assertNotIn("sp_arch", ids, "архивные катушки не показываем")
        self.assertEqual("sp_here", ids[0], "катушку с принтера показываем первой")
        self.assertEqual(6, len(ids))
        data = summary(self.db, self.manager, "prn1", spool_limit=2)
        self.assertEqual(2, len(data["spools"]), "лимит списка катушек не соблюдён")
        self.assertEqual(SPOOL_LIMIT, 60)

    def test_summary_does_not_write_anything(self):
        """Сводка — только чтение: ни событий, ни движений по складу."""
        before_events = self.db.query("SELECT * FROM events")
        before_spools = self.db.query("SELECT * FROM spools")
        summary(self.db, self.manager, "prn1")
        self.assertEqual(before_events, self.db.query("SELECT * FROM events"))
        self.assertEqual(before_spools, self.db.query("SELECT * FROM spools"))


class PultSummaryRouteTests(unittest.TestCase):
    def setUp(self):
        register_routes()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "t.sqlite3")
        self.repo = Repo(self.db)

        class Api:
            pass

        self.api = Api()
        self.api.db = self.db
        self.api.repo = self.repo
        self.api.manager = FakeManager()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_route_is_registered_and_dispatchable(self):
        route = next((r for r in router.reference() if r["path"] == "/api/pult/summary"), None)
        self.assertIsNotNone(route, "маршрут сводки не зарегистрирован")
        self.assertEqual("GET", route["method"])
        self.assertTrue(route["module"].endswith("routes_printers"),
                        f"сводка объявлена не в routes_printers: {route['module']}")

    def test_route_returns_summary_and_passes_printer(self):
        status, body = router.dispatch(self.api, "GET", "/api/pult/summary",
                                       query={"printer_id": ["prn1"]})
        self.assertEqual(200, status)
        self.assertEqual("prn1", self.api.manager.seen_printer_id)
        self.assertEqual(1, len(body["printers"]))
        self.assertEqual(1, len(body["queue"]))
        self.assertIn("ams", body)
        self.assertIn("spools", body)

    def test_route_works_without_printer_argument(self):
        status, body = router.dispatch(self.api, "GET", "/api/pult/summary")
        self.assertEqual(200, status)
        self.assertEqual("", self.api.manager.seen_printer_id)
        self.assertEqual("prn1", body["active_id"])


if __name__ == "__main__":
    unittest.main()

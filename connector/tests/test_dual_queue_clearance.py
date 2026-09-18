"""Тесты балансировки очереди на два принтера и подтверждения очистки стола по фото."""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.manager import PrinterManager
from connector.printflow.repo import Repo
from connector.tests.test_phase11 import make_db, _held


class FakeCam:
    def __init__(self):
        self.frame = b"fake-camera-frame"

    def snapshot(self, note=""):
        return {"ok": True}


class FakeBambuPrinter:
    def __init__(self, pid, name="P1S", trays=None):
        self.id = pid
        self.record = {"id": pid, "name": name, "model": "P1S", "enabled": 1}
        self.connected = True
        self.camera = FakeCam()
        self.trays = trays or [{"slot": 0, "type": "PLA", "color": "#FFFFFF", "present": True, "active": True}]
        self.cmds = []

    def snapshot(self):
        return {
            "id": self.id,
            "name": self.record["name"],
            "printer": {"state": "IDLE", "task": "", "progress": 0},
            "connection": {"connected": True},
            "ams": {"trays": self.trays},
            "camera": {"available": True},
            "guard": {"alerts": []},
        }

    def command(self, name, value=None):
        self.cmds.append(name)
        return {"ok": True}


class DualPrinterQueueAndClearanceTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.tmp = pathlib.Path(_held[-1].name)
        self.up_dir = self.tmp / "uploads"
        self.up_dir.mkdir(exist_ok=True)

        patch = mock.patch("connector.printflow.config.UPLOAD_DIR", self.up_dir)
        patch.start()
        self.addCleanup(patch.stop)

        self.db.set_settings({
            "auto_queue": True,
            "unattended_dangerous_actions": True,
            "bed_watch_enabled": True,
        })

        # Создаем двух фейковых принтеров
        self.p1 = FakeBambuPrinter("p1", "P1S #1", trays=[{"slot": 0, "type": "PLA", "present": True, "active": True}])
        self.p2 = FakeBambuPrinter("p2", "P1S #2", trays=[{"slot": 0, "type": "PETG", "present": True, "active": True}])

        self.repo = Repo(self.db)
        self.mgr = PrinterManager(self.db, repo=self.repo)
        self.mgr.printers = {"p1": self.p1, "p2": self.p2}

    def test_pool_queue_assignment_to_matching_ams(self):
        # Задание 1: PLA, без принтера (printer_id = '')
        job_pla = self.mgr.enqueue({
            "name": "PLA-Model",
            "file": "pla.gcode.3mf",
            "material": "PLA",
            "printer_id": "",
            "allow_auto_start": False,
        })
        # Задание 2: PETG, без принтера (printer_id = '')
        job_petg = self.mgr.enqueue({
            "name": "PETG-Model",
            "file": "petg.gcode.3mf",
            "material": "PETG",
            "printer_id": "",
            "allow_auto_start": False,
        })

        # Проверяем выбор следующего задания для p1 (заправлен PLA)
        snap1 = self.p1.snapshot()
        next_for_p1 = self.mgr.next_job("p1", snap1)
        self.assertIsNotNone(next_for_p1)
        self.assertEqual(next_for_p1["id"], job_pla["id"])

        # Проверяем выбор следующего задания для p2 (заправлен PETG)
        snap2 = self.p2.snapshot()
        next_for_p2 = self.mgr.next_job("p2", snap2)
        self.assertIsNotNone(next_for_p2)
        self.assertEqual(next_for_p2["id"], job_petg["id"])

    def test_bed_clearance_blocks_and_releases(self):
        # Если печать завершилась и стол помечен неочищенным
        self.mgr._bed_cleared["p1"] = False

        snap1 = self.p1.snapshot()
        gate_ok, reason = self.mgr._start_gate(
            {"id": "j1", "file": "test.gcode", "material": "PLA"},
            snap1,
            self.p1
        )
        self.assertFalse(gate_ok)
        self.assertIn("столе осталась деталь", reason)

        # Вызов part_removed (снятие детали человеком или через Telegram)
        res = self.mgr.part_removed("p1")
        self.assertTrue(res["ok"])
        self.assertTrue(self.mgr._bed_cleared["p1"])

        gate_ok2, _ = self.mgr._start_gate(
            {"id": "j1", "file": "test.gcode", "material": "PLA"},
            snap1,
            self.p1
        )
        self.assertTrue(gate_ok2)


if __name__ == "__main__":
    unittest.main()

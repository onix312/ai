"""Тесты авто-продолжения печати (Крым / сбои питания) и конвертации печати в заказ."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402
from connector.printflow.manager import PrinterManager  # noqa: E402
from connector.printflow.bambu import BambuPrinter  # noqa: E402


class MockPrinter:
    def __init__(self, pid="p_main", name="P1S Main", state="RUNNING", task="Dragon_Keychain_PLA.3mf"):
        self.id = pid
        self.record = {"id": pid, "name": name, "model": "P1S", "enabled": 1, "mode": "lan"}
        self.connected = True
        self.state = state
        self.task = task
        self.commands = []
        self.raw = {
            "print": {
                "gcode_state": state,
                "subtask_name": task,
                "mc_percent": 45,
                "layer_num": 120,
                "total_layer_num": 300,
                "mc_remaining_time": 40,
                "print_weight": 35.5,
                "ams": {
                    "tray_now": "0",
                    "ams": [{
                        "id": "0",
                        "tray": [{
                            "id": "0",
                            "tray_type": "PETG",
                            "tray_color": "000000FF",
                            "remain": 80,
                        }]
                    }]
                }
            }
        }

    def snapshot(self):
        return {
            "id": self.id,
            "name": self.record["name"],
            "model": "P1S",
            "connection": {"connected": self.connected, "mode": "lan"},
            "printer": {
                "state": self.state,
                "task": self.task,
                "progress": 45.0,
                "remaining_min": 40.0,
                "elapsed_min": 30.0,
                "layer": 120,
                "total_layers": 300,
                "weight": 35.5,
                "problems": [],
            },
            "ams": {
                "trays": [{
                    "id": "00", "slot": 0, "unit": 0, "type": "PETG",
                    "color": "#000000", "active": True,
                }]
            }
        }

    def command(self, name: str, value=None):
        self.commands.append((name, value))
        if name == "resume":
            self.state = "RUNNING"
            self.raw["print"]["gcode_state"] = "RUNNING"
        elif name == "pause":
            self.state = "PAUSE"
            self.raw["print"]["gcode_state"] = "PAUSE"
        return {"ok": True}

    def shutdown(self):
        pass


class ConvertPrintToOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "test.sqlite3"
        self.db = Database(self.db_path)
        self.repo = Repo(self.db)
        self.manager = PrinterManager(self.db, self.repo)
        self.mock_pr = MockPrinter("pr_test", "P1S Test", "RUNNING", "Organizer_Box_v2.3mf")
        self.manager.printers["pr_test"] = self.mock_pr

        from connector.printflow.api import Api
        self.api = Api.__new__(Api)
        self.api.db = self.db
        self.api.repo = self.repo
        self.api.manager = self.manager

    def tearDown(self):
        self.manager.shutdown()
        self.db.close()
        self.tmp.cleanup()

    def test_convert_active_to_order_success(self):
        res = self.manager.convert_active_to_order("pr_test")
        self.assertTrue(res["ok"])
        self.assertTrue(res["created"])
        order = res["order"]
        self.assertEqual(order["product"], "Organizer Box v2")
        self.assertEqual(order["material"], "PETG")
        self.assertEqual(order["status"], "printing")
        self.assertEqual(order["file"], "Organizer Box_v2.3mf".replace(" ", "_"))
        self.assertAlmostEqual(order["grams"], 35.5)

        # Проверяем связь задания с заказом
        job = self.db.one("SELECT * FROM print_jobs WHERE order_id=?", (order["id"],))
        self.assertIsNotNone(job)
        self.assertEqual(job["printer_id"], "pr_test")

        # Повторный вызов не дублирует заказ, а возвращает существующий
        res2 = self.manager.convert_active_to_order("pr_test")
        self.assertTrue(res2["ok"])
        self.assertFalse(res2["created"])
        self.assertEqual(res2["order"]["id"], order["id"])

    def test_api_convert_active_to_order(self):
        code, body = self.api.post("/api/printer/convert-to-order", {"printer_id": "pr_test"}, {})
        self.assertEqual(code, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body["order"]["product"], "Organizer Box v2")

    def test_convert_job_to_order(self):
        job = self.manager.enqueue({
            "name": "Custom_Bracket.gcode",
            "file": "Custom_Bracket.gcode",
            "printer_id": "pr_test",
        })
        res = self.manager.convert_job_to_order(job["id"])
        self.assertTrue(res["ok"])
        self.assertTrue(res["created"])
        order = res["order"]
        self.assertEqual(order["product"], "Custom Bracket")
        updated_job = self.db.one("SELECT order_id FROM print_jobs WHERE id=?", (job["id"],))
        self.assertEqual(updated_job["order_id"], order["id"])

    def test_api_convert_job_to_order(self):
        job = self.manager.enqueue({
            "name": "Grip_Handle.3mf",
            "file": "Grip_Handle.3mf",
            "printer_id": "pr_test",
        })
        code, body = self.api.post("/api/jobs/convert-to-order", {"job_id": job["id"]}, {})
        self.assertEqual(code, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body["order"]["product"], "Grip Handle")


class AutoResumePowerLossTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "test.sqlite3"
        self.db = Database(self.db_path)
        self.repo = Repo(self.db)
        self.manager = PrinterManager(self.db, self.repo)
        self.mock_pr = MockPrinter("pr_crimea", "P1S Crimea", "PAUSE", "Vase_Tall_PLA.3mf")
        self.manager.printers["pr_crimea"] = self.mock_pr

    def tearDown(self):
        self.manager.shutdown()
        self.db.close()
        self.tmp.cleanup()

    def test_auto_resume_on_startup_in_pause_state(self):
        # При старте скрипта принтер в фазе стопа/паузы -> продолжить без команд
        self.assertEqual(self.mock_pr.state, "PAUSE")
        acted = self.manager.check_auto_resume("pr_crimea")
        self.assertTrue(acted)
        self.assertIn(("resume", None), self.mock_pr.commands)
        self.assertEqual(self.mock_pr.state, "RUNNING")

        # Событие записано в журнал
        events = self.db.events(limit=5, kind="printer")
        resume_events = [e for e in events if "Авто-продолжение" in e["title"]]
        self.assertTrue(len(resume_events) > 0)

    def test_manual_user_pause_is_respected(self):
        # Если пользователь сам нажал паузу в UI -> авто-продолжение не вмешивается
        self.manager.mark_user_paused("pr_crimea")
        self.assertTrue(self.manager.is_user_paused("pr_crimea"))

        acted = self.manager.check_auto_resume("pr_crimea")
        self.assertFalse(acted)
        self.assertEqual(self.mock_pr.state, "PAUSE")

        # Когда пользователь нажимает продолжить -> флаг сбрасывается
        self.manager.clear_user_paused("pr_crimea")
        self.assertFalse(self.manager.is_user_paused("pr_crimea"))

    def test_auto_resume_setting_disabled(self):
        self.db.set_settings({"auto_resume_paused": False})
        acted = self.manager.check_auto_resume("pr_crimea")
        self.assertFalse(acted)
        self.assertEqual(self.mock_pr.state, "PAUSE")

    def test_auto_resume_does_not_resume_on_filament_runout(self):
        # Если закончился пластик (код 05002001) -> не продолжаем впустую
        snap = self.mock_pr.snapshot()
        snap["printer"]["problems"] = [{
            "code": "05002001",
            "title": "Закончился пластик",
            "severity": "error",
            "blocking": True,
        }]
        acted = self.manager.check_auto_resume("pr_crimea", snap)
        self.assertFalse(acted)
        self.assertEqual(self.mock_pr.state, "PAUSE")


if __name__ == "__main__":
    unittest.main()

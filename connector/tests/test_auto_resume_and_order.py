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


class SdFilesStub:
    """Заглушка files.read_head: отдаёт шапку G-code, как с SD-карты."""

    def __init__(self, head: bytes = b""):
        self.head = head
        self.calls = []

    def read_head(self, path, max_bytes=131072):
        self.calls.append(path)
        return self.head


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
        # Печать идёт: print_weight — частичный расход, а не вес изделия.
        # Раньше сюда молча писалось «30 г» или частичный вес — теперь честный 0
        # с пометкой в заметках, факт приедет по завершении печати.
        self.assertEqual(order["grams"], 0.0)
        self.assertIn("неизвестен", order["notes"].lower())
        # Время печати — из телеметрии: уже прошло 30 + осталось 40 минут.
        self.assertAlmostEqual(order["hours"], 70 / 60, places=2)

        # Проверяем связь задания с заказом
        job = self.db.one("SELECT * FROM print_jobs WHERE order_id=?", (order["id"],))
        self.assertIsNotNone(job)
        self.assertEqual(job["printer_id"], "pr_test")

        # Повторный вызов не дублирует заказ, а возвращает существующий
        res2 = self.manager.convert_active_to_order("pr_test")
        self.assertTrue(res2["ok"])
        self.assertFalse(res2["created"])
        self.assertEqual(res2["order"]["id"], order["id"])

    def test_convert_active_uses_sd_gcode_head(self):
        # Слайсер насчитал 213 г, файл лежит только на SD принтера (.gcode):
        # шапка G-code подтягивается по FTPS, а не подменяется «30 г».
        self.mock_pr.files = SdFilesStub(
            b"; Filament used [g]: 213.0\n; TIME: 9600\n; filament_type: PLA\n")
        self.mock_pr.state = "RUNNING"
        self.mock_pr.task = "Bowl_213g.gcode"
        res = self.manager.convert_active_to_order("pr_test")
        order = res["order"]
        self.assertAlmostEqual(order["grams"], 213.0)
        self.assertAlmostEqual(order["hours"], 160 / 60, places=2)
        self.assertIn("213.0", order["notes"])

    def test_convert_active_finished_uses_printer_weight(self):
        # Завершённая печать: print_weight — уже факт, его и берём.
        self.mock_pr.state = "FINISH"
        res = self.manager.convert_active_to_order("pr_test")
        self.assertEqual(res["order"]["grams"], 35.5)

    def test_convert_active_without_sources_no_fake_30(self):
        # Ни локального файла, ни SD, ни факта принтера — честный ноль и
        # пометка, а не магическое «30 г».
        self.mock_pr.files = SdFilesStub(b"")
        self.mock_pr.state = "RUNNING"
        self.mock_pr.task = "Unknown_Thing.3mf"
        res = self.manager.convert_active_to_order("pr_test")
        self.assertEqual(res["order"]["grams"], 0.0)
        self.assertIn("неизвестен", res["order"]["notes"].lower())

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


class FinishFactFillsOrderTests(unittest.TestCase):
    """Факт завершённой печати заполняет вес плиты в заказе, где сметы не было."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tmp.name) / "test.sqlite3"
        self.db = Database(self.db_path)
        self.repo = Repo(self.db)
        self.db.set_settings({"auto_accounting": True, "auto_consume_filament": False})
        from connector.printflow.accounting import Accounting
        self.acc = Accounting(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_finish_fact_fills_plan_grams_when_unknown(self):
        # Заказ из печати: сметы слайсера не было (grams == 0).
        order = self.repo.save_order({
            "product": "Тяжёлая деталь", "qty": 1, "status": "printing",
            "grams": 0.0, "hours": 0.0,
        })
        job = self.db.upsert("print_jobs", {
            "id": "job_fact", "order_id": order["id"], "printer_id": "pr_x",
            "name": "Heavy.gcode", "file": "Heavy.gcode", "state": "done",
            "grams": 213.0, "duration_min": 160.0,
        })
        self.acc.register_job_costs(job)
        updated = self.db.one("SELECT grams, hours, actual_grams, actual_hours"
                              " FROM orders WHERE id=?", (order["id"],))
        self.assertAlmostEqual(updated["actual_grams"], 213.0)
        self.assertAlmostEqual(updated["actual_hours"], 160.0 / 60.0, places=3)
        # Плановый вес появился из факта — в карточке заказа теперь 213 г.
        self.assertAlmostEqual(updated["grams"], 213.0)
        self.assertAlmostEqual(updated["hours"], 160.0 / 60.0, places=3)

    def test_finish_fact_does_not_overwrite_existing_plan(self):
        # Если смета была (например, 213 г из слайсера) — факт её не затирает.
        order = self.repo.save_order({
            "product": "Деталь со сметой", "qty": 1, "status": "printing",
            "grams": 213.0, "hours": 2.5,
        })
        job = self.db.upsert("print_jobs", {
            "id": "job_fact2", "order_id": order["id"], "printer_id": "pr_x",
            "name": "Planned.gcode", "file": "Planned.gcode", "state": "done",
            "grams": 215.0, "duration_min": 150.0,
        })
        self.acc.register_job_costs(job)
        updated = self.db.one("SELECT grams, hours FROM orders WHERE id=?", (order["id"],))
        self.assertAlmostEqual(updated["grams"], 213.0)
        self.assertAlmostEqual(updated["hours"], 2.5)


if __name__ == "__main__":
    unittest.main()

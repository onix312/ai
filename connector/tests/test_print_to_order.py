"""Печать без заказа: живая себестоимость и кнопка «в заказ»."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow.manager import PrinterManager  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402


class PrintToOrderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.repo = Repo(self.db)
        self.manager = PrinterManager(self.db, self.repo)

    def tearDown(self):
        self.manager.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _spool(self, price: float = 2000.0) -> str:
        row = self.db.upsert("spools", {
            "id": "sp1", "material": "PLA", "color_name": "Чёрный",
            "total_grams": 1000, "remaining_grams": 800, "price": price, "archived": 0,
        })
        return row["id"]

    def _snap(self, **printer):
        info = {"state": "RUNNING", "weight": 0, "progress": 50,
                "elapsed_min": 60, "remaining_min": 60}
        info.update(printer)
        return {"id": "p1", "printer": info, "ams": {"trays": []}}

    def test_job_summary_without_order_still_has_spent(self):
        self._spool(2000.0)
        self.db.upsert("print_jobs", {
            "id": "j1", "printer_id": "p1", "order_id": None, "state": "running",
            "spool_id": "sp1", "name": "demo.3mf",
        })
        summary = self.manager.job_summary(self._snap(weight=50, progress=50))
        self.assertGreaterEqual(summary["spent"], 100.0)
        self.assertGreater(summary["cost_total"], 0)
        self.assertFalse(summary["has_order"])
        self.assertEqual(summary["job_id"], "j1")
        self.assertEqual(summary["grams_source"], "printer")

    def test_job_summary_uses_est_grams_fallback(self):
        self.db.upsert("print_jobs", {
            "id": "j2", "printer_id": "p1", "order_id": None, "state": "running",
            "name": "x.3mf", "est_grams": 100,
        })
        summary = self.manager.job_summary(self._snap(weight=0, progress=40))
        self.assertAlmostEqual(summary["grams"], 40.0, places=1)
        self.assertEqual(summary["grams_source"], "estimate")
        self.assertGreater(summary["spent"], 0)
        self.assertGreater(summary["remaining_grams"], 0)

    def test_job_summary_suggested_price_present(self):
        self.db.upsert("print_jobs", {
            "id": "j3", "printer_id": "p1", "state": "running", "name": "y.3mf",
        })
        summary = self.manager.job_summary(self._snap(weight=20, progress=20, elapsed_min=30,
                                                      remaining_min=120))
        self.assertGreater(summary["suggested_price"], 0)
        self.assertIsNotNone(summary["profit_if_sold"])
        self.assertGreaterEqual(summary["suggested_price"], summary["cost_total"])

    def test_order_from_print_creates_and_links(self):
        self.db.upsert("print_jobs", {
            "id": "j4", "printer_id": "p1", "order_id": None, "state": "running",
            "name": "adresnik_barsik.3mf", "file": "adresnik_barsik.3mf",
            "est_grams": 42, "est_minutes": 90,
        })
        result = self.manager.order_from_print("p1", "j4", customer_name="Мария",
                                               channel="shop")
        self.assertTrue(result["ok"])
        self.assertTrue(result["created"])
        order = result["order"]
        self.assertEqual(order["status"], "printing")
        self.assertIn("adresnik", (order.get("product") or "").lower())
        self.assertEqual(order["customer_name"], "Мария")
        job = self.db.one("SELECT * FROM print_jobs WHERE id=?", ("j4",))
        self.assertEqual(job["order_id"], order["id"])

    def test_order_from_print_already_linked_returns_existing(self):
        saved = self.repo.save_order({"product": "уже есть", "status": "printing"})
        self.db.upsert("print_jobs", {
            "id": "j5", "printer_id": "p1", "order_id": saved["id"], "state": "running",
            "name": "x.3mf",
        })
        result = self.manager.order_from_print("p1", "j5")
        self.assertFalse(result["created"])
        self.assertEqual(result["order"]["id"], saved["id"])
        self.assertEqual(self.db.query("SELECT id FROM orders").__len__(), 1)

    def test_link_job_to_order(self):
        order = self.repo.save_order({"product": "органайзер", "status": "queue"})
        self.db.upsert("print_jobs", {
            "id": "j6", "printer_id": "p1", "order_id": None, "state": "done",
            "name": "org.3mf", "grams": 30, "duration_min": 80, "cost": 55,
        })
        result = self.manager.link_job_to_order("j6", order["id"])
        self.assertTrue(result["ok"])
        job = self.db.one("SELECT * FROM print_jobs WHERE id=?", ("j6",))
        self.assertEqual(job["order_id"], order["id"])
        updated = self.db.one("SELECT * FROM orders WHERE id=?", (order["id"],))
        self.assertEqual(updated["status"], "post")
        self.assertGreaterEqual(updated["actual_grams"], 30)

    def test_print_end_grams_fallback_without_order(self):
        self.db.upsert("print_jobs", {
            "id": "j7", "printer_id": "p1", "order_id": None, "state": "running",
            "name": "solo.3mf", "est_grams": 77, "est_minutes": 40,
        })
        self.manager._on_print_end("p1", "complete", "solo.3mf", {})
        job = self.db.one("SELECT * FROM print_jobs WHERE id=?", ("j7",))
        self.assertEqual(job["state"], "done")
        self.assertEqual(round(job["grams"], 1), 77.0)
        self.assertEqual(round(job["duration_min"], 1), 40.0)
        self.assertGreater(job["cost"], 0)

    def test_api_print_to_order_route(self):
        from connector.printflow.api import Api
        api = Api.__new__(Api)
        api.db = self.db
        api.manager = self.manager
        self.db.upsert("print_jobs", {
            "id": "j8", "printer_id": "p1", "state": "running", "name": "tag.3mf",
        })
        code, payload = api.post("/api/print/to-order",
                                 {"printer_id": "p1", "job_id": "j8"}, {})
        self.assertEqual(code, 200)
        self.assertTrue(payload["created"])
        self.assertTrue(payload["order"]["id"])

    def test_enough_filament_uses_est_grams(self):
        self._spool()
        self.db.execute("UPDATE spools SET remaining_grams=20, printer_id=?, ams_slot=? WHERE id=?",
                        ("p1", "0", "sp1"))
        job = {"id": "j9", "printer_id": "p1", "order_id": None, "est_grams": 80}
        snap = {"ams": {"trays": [{"active": True, "slot": "0", "type": "PLA", "uuid": ""}]}}
        ok, reason = self.manager._enough_filament(job, snap)
        self.assertFalse(ok)
        self.assertIn("80", reason)


if __name__ == "__main__":
    unittest.main()

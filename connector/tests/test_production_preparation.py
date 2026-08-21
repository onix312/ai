"""Подготовка заказа: готовность, резерв пластика очередью и идемпотентность."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.config import now_iso  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.manager import PrinterManager  # noqa: E402
from connector.printflow.production import ProductionPreparation  # noqa: E402


class FakeManager:
    def __init__(self, db: Database):
        self.db = db
        self.enqueued: list[dict] = []

    def get(self, printer_id=""):
        return None

    def enqueue(self, data: dict) -> dict:
        self.enqueued.append(dict(data))
        row = {
            "id": f"job-{len(self.enqueued)}", "state": "queued",
            "created_at": now_iso(), "queued_at": now_iso(),
            **data,
        }
        row.pop("allow_auto_start", None)
        return self.db.upsert("print_jobs", row)


class ProductionPreparationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "production.sqlite3")
        self.db.upsert("printers", {
            "id": "printer-1", "name": "P1S", "model": "P1S",
            "enabled": 1, "mode": "lan",
        })
        self.db.upsert("spools", {
            "id": "spool-black", "material": "PETG", "color_name": "Чёрный",
            "total_grams": 1000, "remaining_grams": 500, "price": 1500,
            "printer_id": "printer-1", "ams_slot": "0", "archived": 0,
        })
        self.manager = FakeManager(self.db)
        self.production = ProductionPreparation(self.db, self.manager)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def order(self, order_id="order-1", **overrides):
        row = {
            "id": order_id, "number": order_id, "product": "Адресник",
            "status": "new", "priority": "high", "qty": 3,
            "material": "PETG", "color": "Чёрный", "grams": 100,
            "hours": 0.5, "file": "tag.3mf", "created_at": now_iso(),
        }
        row.update(overrides)
        return self.db.upsert("orders", row)

    def test_readiness_multiplies_unit_norms_and_selects_ams_spool(self):
        self.order()
        ready = self.production.readiness("order-1")
        self.assertTrue(ready["ok"])
        self.assertEqual(ready["requirements"]["grams"], 300)
        self.assertEqual(ready["requirements"]["minutes"], 90)
        self.assertEqual(ready["selected_spool"]["id"], "spool-black")
        self.assertEqual(ready["selected_printer"]["id"], "printer-1")
        self.assertTrue(ready["selected_spool"]["in_ams"])

    def test_prepare_is_idempotent_and_never_allows_physical_autostart(self):
        self.order()
        first = self.production.prepare("order-1")
        second = self.production.prepare("order-1")
        self.assertFalse(first["already_queued"])
        self.assertTrue(second["already_queued"])
        self.assertEqual(len(self.manager.enqueued), 1)
        payload = self.manager.enqueued[0]
        self.assertIs(payload["allow_auto_start"], False)
        self.assertEqual(payload["spool_id"], "spool-black")
        self.assertEqual(payload["est_grams"], 300)
        order = self.db.one("SELECT status, spools FROM orders WHERE id='order-1'")
        self.assertEqual(order["status"], "queue")
        self.assertIn("spool-black", order["spools"])

    def test_active_queue_reservation_prevents_overcommit(self):
        self.order("order-first", qty=4)
        self.production.prepare("order-first")  # резерв 400 из 500 г
        self.order("order-second", qty=2)
        ready = self.production.readiness("order-second")
        self.assertFalse(ready["ok"])
        self.assertEqual(ready["spools"][0]["available_grams"], 100)
        self.assertTrue(any(item["code"] == "filament" for item in ready["blocks"]))

    def test_missing_production_data_blocks_prepare(self):
        self.order(file="", material="", grams=0)
        ready = self.production.readiness("order-1")
        codes = {item["code"] for item in ready["blocks"]}
        self.assertTrue({"file", "material", "grams"}.issubset(codes))
        with self.assertRaisesRegex(ValueError, "Нельзя подготовить"):
            self.production.prepare("order-1")
        self.assertEqual(self.manager.enqueued, [])

    def test_multicolor_order_requires_manual_ams_mapping(self):
        self.order(colors='[{"color":"Чёрный","grams":60},'
                          '{"color":"Белый","grams":40}]')
        ready = self.production.readiness("order-1")
        self.assertTrue(any(item["code"] == "multicolor" for item in ready["blocks"]))

    def test_manager_honors_explicit_no_autostart(self):
        self.db.set_settings({"auto_queue": True})
        manager = PrinterManager.__new__(PrinterManager)
        manager.db = self.db
        manager._maybe_start_next = mock.Mock()
        manager.enqueue({
            "name": "Безопасная очередь", "file": "tag.3mf",
            "printer_id": "printer-1", "source": "order-prepared",
            "allow_auto_start": False,
        })
        manager._maybe_start_next.assert_not_called()
        self.assertIsNone(manager.next_job("printer-1"))


if __name__ == "__main__":
    unittest.main()

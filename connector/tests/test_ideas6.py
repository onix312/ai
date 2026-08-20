"""Волна 6: пересчёт цен (C11), стоимость часа по факту (C3), брак в ₽ (C10)."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting, num  # noqa: E402
from connector.printflow.db import Database  # noqa: E402


class RecalcTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.acc = Accounting(self.db)
        self.db.upsert("catalog", {
            "id": "c1", "name": "адресник", "grams": 20, "hours": 0.5,
            "material": "PLA", "price": 100, "archived": 0})
        self.db.upsert("catalog", {
            "id": "c2", "name": "пустышка", "grams": 0, "hours": 0,
            "material": "PLA", "price": 50, "archived": 0})

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_preview_skips_items_without_parameters(self):
        result = self.acc.recalc_catalog(False)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["id"], "c1")

    def test_apply_updates_prices(self):
        preview = self.acc.recalc_catalog(False)
        new_price = preview["items"][0]["new_price"]
        applied = self.acc.recalc_catalog(True)
        self.assertEqual(applied["count"], 1)
        row = self.db.one("SELECT price FROM catalog WHERE id='c1'")
        self.assertEqual(num(row["price"]), new_price)

    def test_new_price_above_cost(self):
        preview = self.acc.recalc_catalog(False)
        item = preview["items"][0]
        self.assertGreater(item["new_price"], item["cost"],
                           "цена должна покрывать себестоимость с наценкой")


class HourCostTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.acc = Accounting(self.db)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_empty_period_verdict(self):
        result = self.acc.actual_hour_cost(30)
        self.assertEqual(result["hours"], 0.0)
        self.assertIn("нет часов", result["verdict"])

    def test_fact_higher_than_tariff(self):
        stamp = datetime.now().isoformat(timespec="seconds")
        self.db.upsert("print_jobs", {
            "id": "j1", "state": "done", "duration_min": 600, "grams": 100,
            "finished_at": stamp, "queued_at": stamp, "printer_id": ""})
        self.acc.add_transaction("expense", "filament", 5000, "пластик",
                                 at=stamp)
        result = self.acc.actual_hour_cost(30)
        self.assertEqual(result["hours"], 10.0)
        self.assertGreater(result["per_hour"], result["tariff"])
        self.assertGreater(result["diff_pct"], 0)
        self.assertIn("недооценена", result["verdict"])

    def test_defects_cost_money(self):
        stamp = datetime.now().isoformat(timespec="seconds")
        self.db.upsert("print_jobs", {
            "id": "j2", "state": "failed", "result": "error",
            "duration_min": 120, "grams": 200, "finished_at": stamp,
            "queued_at": stamp, "printer_id": ""})
        result = self.acc.defects_cost(30)
        self.assertEqual(result["count"], 1)
        self.assertGreater(result["cost"], 0)

    def test_summary_includes_defects_cost(self):
        stamp = datetime.now().isoformat(timespec="seconds")
        self.db.upsert("print_jobs", {
            "id": "j3", "state": "failed", "result": "error",
            "duration_min": 60, "grams": 100, "finished_at": stamp,
            "queued_at": stamp, "printer_id": ""})
        summary = self.acc.summary(30)
        self.assertIn("defects_cost", summary)
        self.assertGreater(summary["defects_cost"], 0)


if __name__ == "__main__":
    unittest.main()

"""Итерация 15.7: выбранные идеи банка склада.

Проверяем бэкенд:
- корректировки количества (−N/+N) с причинами и сводку списаний;
- план/факт пластика заказа (идеи 60/68);
- разбивку стоимости по катушкам и агрегацию дублей (63/65);
- итог обрезков на катушке (57).
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting
from connector.printflow.db import Database
from connector.printflow.repo import Repo
from connector.printflow.stock import Stock


class AdjustQuantityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "q.sqlite3")
        self.stock = Stock(self.db)
        self.db.upsert("warehouses", {"id": "wh", "name": "Магазин",
                                      "archived": 0, "kind": "shelf"})
        self.db.upsert("nomenclature", {"id": "n1", "name": "Адресник",
                                        "kind": "product", "unit": "шт", "archived": 0})
        self.stock.add_move("n1", "wh", 10, 1000.0, doc_kind="receipt")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_minus_n_cost_by_avg(self):
        move = self.stock.manual_adjust("n1", "wh", -4, reason="брак")
        self.assertEqual(move["qty"], -4.0)
        self.assertEqual(move["cost"], -400.0)
        self.assertEqual(self.stock.qty("n1", "wh"), 6.0)
        self.assertIn("[Брак]", move["note"])

    def test_plus_n(self):
        self.stock.manual_adjust("n1", "wh", 3, reason="найдено")
        self.assertEqual(self.stock.qty("n1", "wh"), 13.0)

    def test_stats_count_only_writeoffs(self):
        self.stock.manual_adjust("n1", "wh", 3, reason="найдено")  # +
        self.stock.manual_adjust("n1", "wh", -2, reason="брак")    # −
        s = self.stock.manual_stats(days=7)
        self.assertEqual(s["total_qty"], 2.0)
        self.assertEqual(s["per_nom"]["n1"]["qty"], 2.0)


class FilamentPlanFactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "f.sqlite3")
        self.acc = Accounting(self.db)
        self.db.upsert("spools", {"id": "sp", "material": "PLA",
                                  "color_name": "белый", "price": 1000,
                                  "total_grams": 1000, "remaining_grams": 800,
                                  "verified": 1, "archived": 0})
        self.db.execute(
            "INSERT INTO orders(id,number,product,spools) VALUES(?,?,?,?)",
            ("o1", "1001", "заказ",
             json.dumps([{"spool_id": "sp", "grams": 100}])))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_plan_then_actual(self):
        r = self.acc.filament_plan_vs_actual("o1")
        self.assertEqual(r["plan_grams"], 100.0)
        self.assertEqual(r["plan_cost"], 100.0)
        self.assertEqual(r["actual_grams"], 0.0)
        # факт списания как при печати
        self.acc.consume_filament(107, spool_id="sp", order_id="o1",
                                  note="факт", auto=False)
        r = self.acc.filament_plan_vs_actual("o1")
        self.assertEqual(r["actual_grams"], 107.0)
        self.assertEqual(r["diff_grams"], 7.0)

    def test_duplicate_spool_rows_aggregated(self):
        # одна катушка в двух строках — граммы суммируются, нехватка корректна
        info = self.acc.spools_filament_cost([
            {"spool_id": "sp", "grams": 400},
            {"spool_id": "sp", "grams": 450}])
        self.assertEqual(info["grams"], 850.0)
        self.assertEqual(info["shortage"], 50.0)  # на катушке 800

    def test_lines_for_breakdown(self):
        info = self.acc.spools_filament_cost([{"spool_id": "sp", "grams": 250}])
        self.assertEqual(info["cost"], 250.0)
        self.assertTrue(any("250 г" in l for l in info["lines"]))


class SpoolScrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "s.sqlite3")
        self.repo = Repo(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_decorate_has_scrap_grams(self):
        self.db.upsert("spools", {"id": "sp", "material": "PLA",
                                  "price": 1000, "total_grams": 1000,
                                  "remaining_grams": 900, "archived": 0})
        for g in (12, 8):
            self.db.upsert("filament_scrap", {
                "id": f"scr{g}", "at": "2026-09-01T10:00", "spool_id": "sp",
                "grams": g, "reason": "обрезки", "request_id": f"r{g}"})
        sp = self.repo.spool("sp")
        self.assertEqual(sp["scrap_grams"], 20.0)


if __name__ == "__main__":
    unittest.main()

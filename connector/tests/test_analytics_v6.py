"""Тесты аналитики PrintFlow 6.0: OEE, коррекция, P&L, аномалии, инвестиции, очередь."""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.analytics import Analytics  # noqa: E402
from connector.printflow.db import Database  # noqa: E402


class OEETests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.a = Analytics(self.db)

    def test_oee_empty(self):
        """Без данных OEE = 0, но не падает."""
        result = self.a.oee(days=30)
        self.assertIn("oee_pct", result)
        self.assertEqual(result["jobs_done"], 0)

    def test_oee_with_jobs(self):
        """OEE считается из журнала печати."""
        from connector.printflow.config import now_iso
        self.db.upsert("print_jobs", {
            "id": "j1", "state": "done", "duration_min": 120,
            "est_minutes": 110, "finished_at": now_iso(),
            "created_at": now_iso(), "grams": 50})
        self.db.upsert("print_jobs", {
            "id": "j2", "state": "done", "duration_min": 60,
            "est_minutes": 55, "finished_at": now_iso(),
            "created_at": now_iso(), "grams": 30})
        result = self.a.oee(days=30)
        self.assertEqual(result["jobs_done"], 2)
        self.assertGreater(result["oee_pct"], 0)
        self.assertIn("losses", result)


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.a = Analytics(self.db)

    def test_empty(self):
        result = self.a.correction_factors()
        self.assertFalse(result["found"])

    def test_factor_computed(self):
        from connector.printflow.config import now_iso
        self.db.upsert("print_jobs", {
            "id": "j1", "state": "done", "duration_min": 120,
            "est_minutes": 100, "finished_at": now_iso(),
            "created_at": now_iso()})
        result = self.a.correction_factors()
        self.assertTrue(result["found"])
        self.assertEqual(result["count"], 1)
        self.assertIn("_all", result["factors"])
        self.assertAlmostEqual(result["factors"]["_all"]["factor"], 1.2, delta=0.01)


class PnLProductTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.a = Analytics(self.db)

    def test_empty(self):
        result = self.a.pnl_by_product()
        self.assertEqual(len(result["products"]), 0)

    def test_with_orders(self):
        from connector.printflow.config import now_iso
        self.db.upsert("orders", {
            "id": "o1", "product": "Адресник", "price": 500,
            "grams": 30, "hours": 1, "qty": 1,
            "status": "done", "created_at": now_iso()})
        self.db.upsert("orders", {
            "id": "o2", "product": "Адресник", "price": 500,
            "grams": 30, "hours": 1, "qty": 1,
            "status": "done", "created_at": now_iso()})
        self.db.upsert("orders", {
            "id": "o3", "product": "Табличка", "price": 1200,
            "grams": 80, "hours": 3, "qty": 1,
            "status": "done", "created_at": now_iso()})
        result = self.a.pnl_by_product()
        self.assertEqual(len(result["products"]), 2)
        self.assertEqual(result["total_orders"], 3)


class AnomalyTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.a = Analytics(self.db)

    def test_no_anomalies_empty(self):
        result = self.a.detect_anomalies()
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 0)

    def test_detects_low_margin(self):
        """Заказ с маржой <5% — аномалия."""
        from connector.printflow.config import now_iso
        self.db.upsert("orders", {
            "id": "o1", "product": "Дешёвый", "price": 100,
            "grams": 100, "hours": 5, "qty": 1, "cost": 98,
            "status": "done", "created_at": now_iso()})
        result = self.a.detect_anomalies()
        low_margin = [a for a in result if a["kind"] == "low_margin"]
        self.assertGreater(len(low_margin), 0)


class InvestmentTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.a = Analytics(self.db)

    def test_basic(self):
        result = self.a.investment_calc(
            printer_cost=80000, extra_hours_month=100,
            profit_per_hour=250, extra_costs_month=5000)
        self.assertEqual(result["printer_cost"], 80000)
        self.assertEqual(result["monthly_revenue"], 25000)
        self.assertEqual(result["monthly_net"], 20000)
        self.assertAlmostEqual(result["payback_months"], 4.0, delta=0.1)
        self.assertEqual(result["verdict"], "ok")

    def test_zero_profit(self):
        result = self.a.investment_calc(
            printer_cost=80000, extra_hours_month=0, profit_per_hour=250)
        self.assertEqual(result["payback_months"], 0)


class DefectAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.a = Analytics(self.db)

    def test_empty(self):
        result = self.a.defect_analysis()
        self.assertEqual(result["total_defects"], 0)
        self.assertEqual(result["total_loss"], 0)


class SmartQueueTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.a = Analytics(self.db)

    def test_empty(self):
        result = self.a.smart_queue()
        self.assertEqual(len(result["queue"]), 0)

    def test_groups_by_material(self):
        """Задания группируются по материалу."""
        from connector.printflow.config import now_iso
        for i, mat in enumerate(["PLA", "PLA", "PETG", "PLA"]):
            self.db.upsert("orders", {
                "id": f"o{i}", "product": f"Item{i}", "material": mat,
                "status": "queue", "created_at": now_iso()})
            self.db.upsert("print_jobs", {
                "id": f"j{i}", "state": "queued", "file": f"file{i}.gcode",
                "order_id": f"o{i}", "created_at": now_iso()})
        result = self.a.smart_queue()
        self.assertEqual(len(result["queue"]), 4)
        # PLA задания идут подряд
        pla_positions = [j["position"] for j in result["queue"]
                         if j["group_material"] == "PLA"]
        self.assertEqual(pla_positions, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()

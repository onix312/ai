"""Тесты инсайтов PrintFlow 4.0: цель месяца, касса вперёд, налоговый календарь."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow.insights import Insights  # noqa: E402


class InsightsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "test.sqlite3")
        self.insights = Insights(self.db)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_goal_without_income_is_bad(self):
        self.db.set_settings({"goal_profit_month": 10000})
        goal = self.insights.goal_progress()
        self.assertEqual(goal["pct"], 0.0)
        self.assertEqual(goal["verdict"], "bad")
        self.assertIn("нужно", goal["verdict_text"])

    def test_goal_achieved_is_ok(self):
        self.db.set_settings({"goal_profit_month": 1000})
        # доход 5000 за текущий месяц
        from connector.printflow.accounting import Accounting
        from connector.printflow.config import now_iso
        acc = Accounting(self.db)
        acc.add_transaction("income", "sale", 5000, "тест", at=now_iso())
        goal = self.insights.goal_progress()
        self.assertEqual(goal["verdict"], "ok")
        self.assertGreaterEqual(goal["profit"], 5000)

    def test_cash_forecast_shows_burn(self):
        self.db.upsert("fixed_costs", {
            "id": "fix1", "name": "Аренда", "amount": 6000, "period": "month",
            "day": 1, "category": "rent", "active": 1, "deductible": 1})
        fc = self.insights.cash_forecast()
        self.assertEqual(fc["burn_monthly"], 6000.0)
        self.assertEqual(fc["verdict"], "bad")  # денег нет — касса уходит в минус

    def test_cash_forecast_healthy_when_no_costs(self):
        fc = self.insights.cash_forecast()
        self.assertEqual(fc["verdict"], "ok")
        self.assertEqual(fc["now"], 0.0)

    def test_tax_calendar_npd_has_payment(self):
        self.db.set_settings({"tax_mode": "npd"})
        cal = self.insights.tax_calendar()
        self.assertEqual(cal["mode"], "npd")
        self.assertEqual(cal["limit"], 2400000.0)
        self.assertTrue(cal["events"])

    def test_tax_calendar_none_has_no_events(self):
        cal = self.insights.tax_calendar()
        self.assertEqual(cal["mode"], "none")
        self.assertEqual(cal["events"], [])

    def test_all_returns_three_blocks(self):
        data = self.insights.all()
        self.assertIn("goal", data)
        self.assertIn("cash", data)
        self.assertIn("tax", data)


if __name__ == "__main__":
    unittest.main()

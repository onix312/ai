"""Мастер «Закрыть месяц» (H4): пять шагов, идемпотентность, корректировки."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import db as db_module  # noqa: E402
from connector.printflow.accounting import Accounting, num  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.month_close import MonthClose  # noqa: E402


class MonthCloseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(self._tmp.name)
        self.db = Database(tmp / "t.sqlite3")
        # Шаг «копия данных» пишет в каталог данных — направляем его в тестовый.
        self._patch_backup = mock.patch.object(db_module, "BACKUP_DIR",
                                               tmp / "backups")
        self._patch_backup.start()
        self.addCleanup(self._patch_backup.stop)
        self._patch_db = mock.patch.object(db_module, "DB_FILE", tmp / "t.sqlite3")
        self._patch_db.start()
        self.addCleanup(self._patch_db.stop)
        self.acc = Accounting(self.db)
        self.mc = MonthClose(self.db)
        self.key = "2026-07"

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _income(self, amount: float, payer: str = "person") -> dict:
        return self.acc.add_transaction(
            "income", "sale", amount, "продажа", payer=payer,
            account_id="cash", at=f"{self.key}-10T12:00:00")

    def test_step_fixed_charges_and_is_idempotent(self):
        self.db.upsert("fixed_costs", {
            "id": "fc1", "name": "Стол в магазине", "amount": 2000,
            "category": "rent", "period": "month", "day": 5,
            "active": 1, "account_id": "cash", "started_at": "",
            "last_charged": "", "deductible": 1})
        result = self.mc.run(self.key, "fixed")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["created"]), 1)
        again = self.mc.run(self.key, "fixed")
        self.assertFalse(again["ok"])
        self.assertTrue(again["done"])
        # Проводка не задвоилась
        rows = self.db.query(
            "SELECT * FROM transactions WHERE fixed_cost_id='fc1'")
        self.assertEqual(len(rows), 1)

    def test_fixed_costs_work_without_auto_flag(self):
        self.db.set_settings({"fixed_costs_auto": False})
        self.db.upsert("fixed_costs", {
            "id": "fc2", "name": "Интернет", "amount": 500,
            "category": "services", "period": "month", "day": 1,
            "active": 1, "account_id": "cash", "started_at": "",
            "last_charged": "", "deductible": 1})
        result = self.mc.run(self.key, "fixed")
        self.assertTrue(result["ok"], "мастер начисляет даже при выключенном автофлаге")

    def test_step_cash_adjusts_balance_without_touching_pnl(self):
        self.db.execute("UPDATE accounts SET opening_balance=1000 WHERE id='cash'")
        self._income(500)
        before = self.acc.accounts_state()
        balance_before = next(a for a in before["accounts"] if a["id"] == "cash")
        self.assertEqual(num(balance_before["balance"]), 1500)

        pnl_before = self.acc.pnl_month(self.key)["profit"]
        result = self.mc.run(self.key, "cash",
                             [{"id": "cash", "fact": 1490}])
        self.assertTrue(result["ok"])
        self.assertEqual(result["adjustments"][0]["diff"], -10)

        after = self.acc.accounts_state()
        balance_after = next(a for a in after["accounts"] if a["id"] == "cash")
        self.assertEqual(num(balance_after["balance"]), 1490)
        # Корректировка кассы — не доход и не расход бизнеса
        self.assertEqual(self.acc.pnl_month(self.key)["profit"], pnl_before)
        again = self.mc.run(self.key, "cash", [{"id": "cash", "fact": 1}])
        self.assertFalse(again["ok"])

    def test_step_tax_deposits_envelope(self):
        self.db.set_settings({"tax_mode": "npd", "npd_rate_person": 4.0})
        self._income(1000)
        result = self.mc.run(self.key, "tax")
        self.assertTrue(result["ok"])
        self.assertEqual(result["deposited"], 40.0)
        from connector.printflow.envelopes import Envelopes
        envs = Envelopes(self.db).list()
        tax_env = next(e for e in envs if "налог" in e["name"].lower())
        self.assertEqual(num(tax_env["balance"]), 40.0)
        again = self.mc.run(self.key, "tax")
        self.assertFalse(again["ok"])
        self.assertEqual(num(tax_env["balance"]), 40.0, "повторный запуск не дублирует резерв")

    def test_step_tax_zero_when_no_taxable_income(self):
        self.db.set_settings({"tax_mode": "npd"})
        result = self.mc.run(self.key, "tax")
        self.assertTrue(result["ok"])
        self.assertEqual(result["deposited"], 0.0)

    def test_step_report_and_backup(self):
        self._income(1000)
        report = self.mc.run(self.key, "report")
        self.assertTrue(report["ok"])
        self.assertIn("pnl", report["report"])
        self.assertEqual(num(report["report"]["pnl"]["income"]), 1000)
        backup = self.mc.run(self.key, "backup")
        self.assertTrue(backup["ok"])
        self.assertTrue(backup["file"])

    def test_failed_backup_does_not_complete_month_step(self):
        with mock.patch.object(db_module, "make_backup",
                               return_value={"ok": False, "error": "База повреждена"}):
            result = self.mc.run(self.key, "backup")

        self.assertFalse(result["ok"])
        self.assertIn("повреждена", result["error"])
        self.assertFalse(self.mc.state(self.key)["done"]["backup"])

    def test_state_tracks_progress(self):
        state = self.mc.state(self.key)
        self.assertEqual(state["next"], "fixed")
        self.mc.run(self.key, "fixed")
        state = self.mc.state(self.key)
        self.assertTrue(state["done"]["fixed"])
        self.assertEqual(state["next"], "cash")

    def test_invalid_month_rejected(self):
        with self.assertRaises(ValueError):
            self.mc.run("2026/07", "fixed")


if __name__ == "__main__":
    unittest.main()

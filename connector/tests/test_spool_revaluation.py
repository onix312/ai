"""Переоценка катушки при приёме (идея 30).

Цена катушки — это цена целой катушки, а себестоимость грамма считается
как цена / масса. Если новую партию купили по другой цене, старая цена
перестаёт соответствовать факту и маржа заказа скачет без причин. При
приёме цена пересчитывается по средневзвешенной, а разница возвращается
оператору отдельной строкой.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.db import Database  # noqa: E402


class SpoolRevaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "test.sqlite3")
        self.acc = Accounting(self.db)

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def spool(self, **extra):
        data = {"id": "sp1", "material": "PLA", "color_name": "Чёрный",
                "total_grams": 1000, "remaining_grams": 100, "price": 1600,
                "archived": 0, "ams_sync": 1, "verified": 1}
        data.update(extra)
        return self.db.upsert("spools", data)

    # --------------------------------------------- средневзвешенная цена
    def test_weighted_average_on_restock(self):
        """Остаток 100 г по 1.6 ₽/г + 1000 г по 2.0 ₽/г → 1.9636 ₽/г."""
        self.spool()
        res = self.acc.restock_spool("sp1", 1000, 2000)
        rev = res["revaluation"]
        self.assertTrue(rev["applied"])
        self.assertEqual(rev["price_before"], 1600.0)
        self.assertAlmostEqual(rev["price_after"], 1963.64, places=1)
        self.assertAlmostEqual(rev["per_gram_after"], 1.9636, places=3)
        self.assertAlmostEqual(rev["delta"], 363.64, places=1)
        row = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual(row["remaining_grams"], 1100.0)
        self.assertAlmostEqual(row["price"], 1963.64, places=1)

    def test_cheaper_purchase_lowers_price(self):
        self.spool(remaining_grams=500, price=2000)  # 2.0 ₽/г
        res = self.acc.restock_spool("sp1", 1000, 1000)  # 1.0 ₽/г
        # (500*2.0 + 1000*1.0) / 1500 = 1.3333 ₽/г → 1333.33 ₽ за катушку
        self.assertAlmostEqual(res["revaluation"]["per_gram_after"], 1.3333, places=3)
        self.assertLess(res["revaluation"]["delta"], 0)

    def test_unpriced_spool_takes_purchase_price(self):
        """Катушка из AMS без цены: старой цены нет — берём факт прихода."""
        self.spool(price=0, remaining_grams=750)
        res = self.acc.restock_spool("sp1", 250, 500)  # 2.0 ₽/г
        self.assertAlmostEqual(res["revaluation"]["per_gram_after"], 2.0, places=3)
        self.assertAlmostEqual(res["revaluation"]["price_after"], 2000.0, places=1)

    def test_zero_payment_keeps_price(self):
        self.spool()
        res = self.acc.restock_spool("sp1", 500, 0)
        self.assertFalse(res["revaluation"]["applied"])
        self.assertEqual(res["revaluation"]["price_after"], 1600.0)
        self.assertEqual(self.db.one("SELECT price FROM spools WHERE id='sp1'")["price"], 1600.0)

    def test_setting_disables_revaluation(self):
        self.db.set_settings({"spool_revalue_on_restock": False})
        self.spool()
        res = self.acc.restock_spool("sp1", 1000, 2000)
        self.assertFalse(res["revaluation"]["applied"])
        self.assertEqual(res["spool"]["price"], 1600.0)
        self.assertEqual(res["spool"]["remaining_grams"], 1100.0)

    def test_expense_recorded_once(self):
        self.spool()
        self.acc.restock_spool("sp1", 1000, 2000)
        rows = self.db.query("SELECT * FROM transactions WHERE category='filament'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], 2000.0)

    def test_zero_grams_rejected(self):
        self.spool()
        with self.assertRaises(ValueError):
            self.acc.restock_spool("sp1", 0, 100)

    def test_unknown_spool_rejected(self):
        with self.assertRaises(ValueError):
            self.acc.restock_spool("nope", 100, 100)

    def test_revaluation_is_auditable(self):
        self.spool()
        self.acc.restock_spool("sp1", 1000, 2000)
        events = self.db.query("SELECT * FROM events WHERE kind='spool'")
        self.assertTrue(any("переоцен" in str(e.get("title") or "").lower() for e in events))

    def test_repeat_request_is_safe(self):
        """Повторный запрос не должен быть «бесплатным дублем» расхода."""
        self.spool()
        self.acc.restock_spool("sp1", 500, 900)
        res = self.acc.restock_spool("sp1", 500, 900)
        rows = self.db.query("SELECT * FROM transactions WHERE category='filament'")
        self.assertEqual(len(rows), 2)
        self.assertEqual(res["spool"]["remaining_grams"], 1100.0)


if __name__ == "__main__":
    unittest.main()

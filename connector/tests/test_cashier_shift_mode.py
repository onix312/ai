"""Режим смен на кассе: «без смен» (auto) и пересчёт ящика.

Решение заказчика №6 — «на кассе без смен», но в коде И3/И4 смена стала
контейнером для двух полезных вещей: отмены наличной продажи (она удаляет
проводку и потому обязана идти в окне смены) и выемки. Поэтому в режиме
``auto`` смена не исчезает, а не видна: открывается сама на первой наличной
продаже, а «сверка» делается одним пересчётом ящика. ``manual`` возвращает
прежнее поведение — открыть и закрыть должен человек.
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
from connector.printflow.cashier import Cashier  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.staff import Staff  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "shift-mode.sqlite3")


def add_item(db: Database, qty: float = 10, price: float = 500) -> None:
    db.upsert("shelf_items", {"id": "s1", "name": "Адресник", "qty": qty,
                               "price": price, "cost_per_unit": 120, "active": 1})


class AutoShiftTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc)
        self.db.set_settings({"cashier_code": "1234"})
        add_item(self.db)
        self.token = self.cashier.login("1234")["token"]

    def tearDown(self):
        self.db.close()

    def _qty(self) -> float:
        return float(self.db.one("SELECT qty FROM shelf_items WHERE id='s1'")["qty"])

    def test_catalog_and_shift_carry_the_mode(self):
        self.assertEqual(self.cashier.catalog()["shift_mode"], "auto")
        self.assertEqual(self.cashier.current_shift(self.token)["mode"], "auto")

    def test_cash_sale_opens_a_shift_invisibly(self):
        self.cashier.sell([{"item_id": "s1", "qty": 2}], "cash", self.token)
        out = self.cashier.current_shift(self.token)
        self.assertTrue(out["open"])
        self.assertEqual(out["live"]["income_cash"], 1000.0)
        # в журнал и события автосмена не пишет — шуметь нечем
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) n FROM audit_log WHERE action='open_shift'")["n"], 0)

    def test_cancel_of_cash_sale_works_without_manual_open(self):
        """Ради этого автосмена и нужна: кассир отменяет ошибочный чек сам."""
        sale = self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash", self.token)
        self.assertEqual(self._qty(), 9)
        out = self.cashier.cancel_sale(sale["sale_id"], self.token)
        self.assertTrue(out["ok"])
        self.assertEqual(self._qty(), 10)
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) n FROM transactions")["n"], 0)

    def test_sbp_sale_alone_does_not_open_a_shift(self):
        """СБП-продажа наличные не трогает — смена-контейнер ей не нужна."""
        self.cashier.sell([{"item_id": "s1", "qty": 1}], "sbp", self.token)
        self.assertFalse(self.cashier.current_shift(self.token)["open"])

    def test_manual_mode_keeps_the_old_contract(self):
        self.db.set_settings({"cashier_shift_mode": "manual"})
        sale = self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash", self.token)
        self.assertFalse(self.cashier.current_shift(self.token)["open"])
        with self.assertRaisesRegex(ValueError, "не из текущей смены"):
            self.cashier.cancel_sale(sale["sale_id"], self.token)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc)
        self.db.set_settings({"cashier_code": "1234"})
        add_item(self.db, qty=50)
        self.token = self.cashier.login("1234")["token"]

    def tearDown(self):
        self.db.close()

    def _earn(self, qty: int = 2) -> None:
        self.cashier.sell([{"item_id": "s1", "qty": qty}], "cash", self.token)

    def test_recount_of_the_day_opens_the_first_count(self):
        """Открытой смены нет: пересчёт просто задаёт стартовый остаток."""
        out = self.cashier.reconcile(self.token, 3000, note="утро")
        self.assertTrue(out["open"])
        self.assertEqual(float(out["shift"]["open_cash"]), 3000.0)
        self.assertEqual(out["diff"], 0.0)

    def test_recount_closes_with_diff_and_reopens_from_the_fact(self):
        self._earn(2)                                  # 1000 ₽ в ящике
        out = self.cashier.reconcile(self.token, 900, note="раздача")
        self.assertEqual(out["expected"], 1000.0)
        self.assertEqual(out["diff"], -100.0)           # недостача видна, а не спрятана
        closed = self.db.one("SELECT * FROM cashier_shifts WHERE COALESCE(closed_at,'')<>''")
        self.assertEqual(float(closed["diff"]), -100.0)
        self.assertIn("пересчёт ящика", closed["note"] or "")
        # новый отсчёт начинается с факта, а не с нуля
        self.assertEqual(float(out["shift"]["open_cash"]), 900.0)
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) n FROM audit_log WHERE action='reconcile'")["n"], 1)

    def test_recount_is_money_neutral(self):
        """Пересчёт — сверка, а не проводка: выручка и склад не меняются."""
        self._earn(1)
        before = self.db.one("SELECT COALESCE(SUM(amount),0) s FROM transactions")["s"]
        self.cashier.reconcile(self.token, 500)
        self.assertEqual(before,
                         self.db.one("SELECT COALESCE(SUM(amount),0) s FROM transactions")["s"])

    def test_negative_count_rejected(self):
        with self.assertRaisesRegex(ValueError, "не может быть меньше нуля"):
            self.cashier.reconcile(self.token, -50)

    def test_other_cashiers_shift_needs_manager(self):
        staff = Staff(self.db)
        anna = staff.add("Анна", "employee", "")
        staff.set_pin(anna["id"], "1111")
        boris = staff.add("Борис", "employee", "")
        staff.set_pin(boris["id"], "2222")
        anna_token = self.cashier.login("1111")["token"]
        boris_token = self.cashier.login("2222")["token"]
        self.cashier.open_shift(anna_token, 0)
        self._earn(1)
        with self.assertRaisesRegex(ValueError, "чужая смена"):
            self.cashier.reconcile(boris_token, 1000)
        # старший может пересчитать чужой ящик — иначе сверку не сделать
        olga = staff.add("Оля", "manager", "")
        staff.set_pin(olga["id"], "9999")
        out = self.cashier.reconcile(self.cashier.login("9999")["token"], 1000)
        self.assertEqual(out["counted"], 1000.0)


if __name__ == "__main__":
    unittest.main()

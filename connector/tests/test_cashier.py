"""Мобильная касса в LAN (Касса 16.0): продажа и СБП без дублей и расхождений.

Проверяем: вход по коду, наличная продажа сразу в журнал (in_shop), СБП-продажа
не трогает склад и выручку до подтверждения, подтверждение в одной транзакции
списывает склад и пишет деньги на счёт СБП, повторные вызовы идемпотентны.
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

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "cashier.sqlite3")


def add_item(db: Database, item_id: str = "s1", qty: float = 10, price: float = 500) -> None:
    db.upsert("shelf_items", {
        "id": item_id, "name": "Адресник", "qty": qty, "price": price,
        "cost_per_unit": 120, "active": 1})


class CashierTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc)
        self.db.set_settings({"cashier_code": "1234"})
        add_item(self.db)
        self.token = self.cashier.login("1234")["token"]

    def tearDown(self):
        self.db.close()

    def qty(self, item_id: str = "s1") -> float:
        return float(self.db.one("SELECT qty FROM shelf_items WHERE id=?", (item_id,))["qty"])

    # ---------------------------------------------------------------- вход
    def test_login_requires_code_and_rejects_wrong(self):
        self.db.set_settings({"cashier_code": ""})
        with self.assertRaisesRegex(ValueError, "не задан"):
            Cashier(self.db, self.acc).login("1234")
        self.db.set_settings({"cashier_code": "5678"})
        with self.assertRaisesRegex(ValueError, "Неверный"):
            self.cashier.login("1111")

    def test_session_expires(self):
        self.cashier.require(self.token)  # ок
        self.cashier.logout(self.token)
        with self.assertRaisesRegex(ValueError, "истекла"):
            self.cashier.require(self.token)

    # ------------------------------------------------------- наличная продажа
    def test_cash_sale_writes_income_and_deducts_stock(self):
        r = self.cashier.sell([{"item_id": "s1", "qty": 2}], "cash", self.token)
        self.assertEqual(r["amount"], 1000)
        self.assertTrue(r["paid"])
        self.assertEqual(self.qty(), 8)
        tx = self.db.one("SELECT * FROM transactions WHERE kind='income'")
        self.assertIsNotNone(tx)
        self.assertEqual(tx["channel"], "shelf")
        self.assertEqual(tx["amount"], 1000)
        self.assertEqual(tx["account_id"], "cash")  # наличные — касса по умолчанию

    def test_cash_sale_idempotent_by_request_id(self):
        self.cashier.sell([{"item_id": "s1", "qty": 2}], "cash", self.token, request_id="r1")
        again = self.cashier.sell([{"item_id": "s1", "qty": 2}], "cash", self.token, request_id="r1")
        self.assertTrue(again.get("already_recorded"))
        self.assertEqual(self.qty(), 8)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM transactions")["n"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM cashier_sales")["n"], 1)

    def test_sell_rejects_oversell(self):
        with self.assertRaisesRegex(ValueError, "нельзя"):
            self.cashier.sell([{"item_id": "s1", "qty": 11}], "cash", self.token)

    # -------------------------------------------------------- СБП-продажа
    def test_sbp_sale_does_not_touch_stock_or_money_until_confirm(self):
        r = self.cashier.sell([{"item_id": "s1", "qty": 2}], "sbp", self.token)
        self.assertFalse(r["paid"])
        self.assertTrue(r["payment_id"])
        self.assertEqual(self.qty(), 10)  # склад не тронут
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))
        p = self.db.one("SELECT * FROM sbp_payments WHERE id=?", (r["payment_id"],))
        self.assertIn(p["status"], ("new", "pending"))
        self.assertEqual(p["amount"], 1000)

    def test_sbp_confirm_deducts_stock_and_writes_income_on_sbp_account(self):
        r = self.cashier.sell([{"item_id": "s1", "qty": 2}], "sbp", self.token)
        c = self.cashier.confirm_sbp(r["payment_id"], self.token)
        self.assertTrue(c["confirmed"])
        self.assertEqual(self.qty(), 8)
        tx = self.db.one("SELECT * FROM transactions WHERE kind='income'")
        self.assertEqual(tx["account_id"], "sbp")
        self.assertEqual(tx["amount"], 1000)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM transactions")["n"], 1)

    def test_sbp_confirm_is_idempotent(self):
        r = self.cashier.sell([{"item_id": "s1", "qty": 2}], "sbp", self.token)
        self.cashier.confirm_sbp(r["payment_id"], self.token)
        again = self.cashier.confirm_sbp(r["payment_id"], self.token)
        self.assertTrue(again.get("already_recorded"))
        self.assertEqual(self.qty(), 8)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM transactions")["n"], 1)
        self.assertEqual(
            self.db.one("SELECT COUNT(*) n FROM shelf_moves WHERE kind='sale'")["n"], 1)

    def test_sbp_reject_leaves_stock_and_money_untouched(self):
        r = self.cashier.sell([{"item_id": "s1", "qty": 2}], "sbp", self.token)
        self.cashier.reject_sbp(r["payment_id"], self.token, reason="не пришло")
        self.assertEqual(self.qty(), 10)
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))
        self.assertEqual(
            self.db.one("SELECT status FROM sbp_payments WHERE id=?", (r["payment_id"],))["status"],
            "rejected")

    def test_sbp_confirm_fails_cleanly_on_shortfall(self):
        """Полку продали во время сверки — подтверждение не должно ничего сломать."""
        r = self.cashier.sell([{"item_id": "s1", "qty": 5}], "sbp", self.token)
        self.db.execute("UPDATE shelf_items SET qty=3 WHERE id='s1'")  # конкурент продал
        with self.assertRaisesRegex(ValueError, "нельзя"):
            self.cashier.confirm_sbp(r["payment_id"], self.token)
        self.assertEqual(self.qty(), 3)
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))
        p = self.db.one("SELECT * FROM sbp_payments WHERE id=?", (r["payment_id"],))
        self.assertIn(p["status"], ("new", "pending"))  # деньги не подтверждены

    # ---------------------------------------------------------------- аудит
    def test_audit_covers_sell_confirm_reject(self):
        r = self.cashier.sell([{"item_id": "s1", "qty": 1}], "sbp", self.token)
        self.cashier.confirm_sbp(r["payment_id"], self.token)
        self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash", self.token)
        actions = [x["action"] for x in self.db.query(
            "SELECT action FROM audit_log WHERE entity='cashier_sale' ORDER BY id")]
        self.assertEqual(actions, ["sell", "confirm_sbp", "sell"])


class CashierRouteTests(unittest.TestCase):
    def setUp(self):
        from connector.printflow.api import register_routes
        register_routes()

    def test_routes_registered(self):
        from connector.printflow.router import router
        paths = router.paths()
        for path in ("/api/cashier/login", "/api/cashier/logout",
                     "/api/cashier/catalog", "/api/cashier/incoming",
                     "/api/cashier/sell", "/api/cashier/confirm-sbp",
                     "/api/cashier/reject-sbp"):
            self.assertIn(path, paths, path)


if __name__ == "__main__":
    unittest.main()

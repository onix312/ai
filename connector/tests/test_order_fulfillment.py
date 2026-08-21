"""Выдача заказа: явная оплата/долг, склад и атомарное закрытие."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting, num
from connector.printflow.config import now_iso
from connector.printflow.db import Database
from connector.printflow.fulfillment import OrderFulfillment
from connector.printflow.repo import Repo
from connector.printflow.stock import Stock


class OrderFulfillmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "fulfillment.sqlite3")
        self.repo = Repo(self.db)
        self.acc = Accounting(self.db)
        self.stock = Stock(self.db)
        self.service = OrderFulfillment(self.db, self.repo, self.stock, self.acc)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def order(self, order_id="order-1", **overrides):
        data = {
            "id": order_id, "number": "1001", "product": "Адресник",
            "customer_name": "Мария", "status": "ready", "quality": "passed",
            "qty": 1, "price": 1000, "paid": 300, "actual_grams": 50,
            "actual_hours": 1, "actual_cost": 150, "auto_cost": 1,
            "created_at": now_iso(), "updated_at": now_iso(),
        }
        data.update(overrides)
        return self.db.upsert("orders", data)

    def test_summary_requires_ready_status_and_finished_jobs(self):
        self.order(status="post")
        self.db.upsert("print_jobs", {
            "id": "job-1", "order_id": "order-1", "state": "running",
            "created_at": now_iso(),
        })
        summary = self.service.summary("order-1")
        self.assertFalse(summary["can_fulfill"])
        self.assertEqual({item["code"] for item in summary["blocks"]},
                         {"status", "active_jobs"})

    def test_payment_and_handoff_must_be_confirmed_explicitly(self):
        self.order()
        with self.assertRaisesRegex(ValueError, "передан"):
            self.service.fulfill("order-1", payment_action="received", payment_method="cash")
        with self.assertRaisesRegex(ValueError, "получена ли"):
            self.service.fulfill("order-1", handoff_confirmed=True, payment_action="none")
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM payments")["n"], 0)
        self.assertEqual(self.db.one("SELECT status FROM orders WHERE id='order-1'")["status"],
                         "ready")

    def test_received_payment_is_atomic_and_idempotent(self):
        self.order()
        first = self.service.fulfill(
            "order-1", handoff_confirmed=True, payment_action="received",
            payment_method="transfer",
        )
        second = self.service.fulfill(
            "order-1", handoff_confirmed=True, payment_action="received",
            payment_method="transfer",
        )
        self.assertEqual(first["collected"], 700)
        self.assertFalse(first["already_fulfilled"])
        self.assertTrue(second["already_fulfilled"])
        self.assertEqual(second["collected"], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM payments")["n"], 1)
        order = self.db.one("SELECT status,paid,closed_at FROM orders WHERE id='order-1'")
        self.assertEqual(order["status"], "done")
        self.assertEqual(num(order["paid"]), 1000)
        self.assertTrue(order["closed_at"])
        self.assertFalse(first["external_sent"])
        self.assertIn("передан", first["message"])

    def test_debt_choice_does_not_create_fake_payment_even_when_legacy_auto_is_on(self):
        self.db.set_settings({"auto_income_on_done": True})
        self.order()
        result = self.service.fulfill(
            "order-1", handoff_confirmed=True, payment_action="debt",
        )
        self.assertEqual(result["debt"], 700)
        self.assertEqual(result["collected"], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM payments")["n"], 0)
        order = self.db.one("SELECT status,paid FROM orders WHERE id='order-1'")
        self.assertEqual(order["status"], "done")
        self.assertEqual(num(order["paid"]), 300)
        self.assertIn("700 ₽", result["message"])

    def test_reserved_stock_is_sold_once(self):
        warehouse = self.db.upsert("warehouses", {
            "id": "warehouse-1", "name": "Полка", "kind": "retail",
            "retail": 1, "archived": 0, "position": 0,
        })
        item = self.db.upsert("nomenclature", {
            "id": "item-1", "code": "000001", "name": "Готовое изделие",
            "kind": "product", "unit": "шт", "archived": 0,
        })
        self.stock.add_move(item["id"], warehouse["id"], 5, 500,
                            doc_id="receipt", doc_kind="receipt")
        self.order(nom_id=item["id"], warehouse_id=warehouse["id"], reserved=1, qty=2)
        self.stock.reserve(item["id"], 2, "order-1", warehouse["id"], "заказ")

        self.service.fulfill("order-1", handoff_confirmed=True, payment_action="debt")
        self.service.fulfill("order-1", handoff_confirmed=True, payment_action="debt")

        self.assertEqual(self.stock.qty(item["id"], warehouse["id"]), 3)
        self.assertEqual(self.stock.reserved(item["id"], warehouse["id"]), 0)
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) n FROM stock_moves WHERE doc_id='order-1' AND doc_kind='sale'"
        )["n"], 1)

    def test_failure_after_payment_rolls_everything_back(self):
        self.order()
        with (
            mock.patch.object(self.repo, "save_order", side_effect=RuntimeError("db failure")),
            self.assertRaisesRegex(RuntimeError, "db failure"),
        ):
            self.service.fulfill(
                "order-1", handoff_confirmed=True, payment_action="received",
                payment_method="cash",
            )
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM payments")["n"], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM transactions")["n"], 0)
        order = self.db.one("SELECT status,paid FROM orders WHERE id='order-1'")
        self.assertEqual(order["status"], "ready")
        self.assertEqual(num(order["paid"]), 300)

    def test_final_status_cannot_bypass_fulfillment_service(self):
        order = self.repo.save_order({"product": "Новый", "status": "ready"})
        with self.assertRaisesRegex(ValueError, "Выдать заказ"):
            self.repo.save_order({"id": order["id"], "status": "done"})
        with self.assertRaisesRegex(ValueError, "Выдать заказ"):
            self.repo.save_order({"product": "Сразу закрытый", "status": "done"})


if __name__ == "__main__":
    unittest.main()

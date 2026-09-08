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

    def test_leftover_queued_job_is_cancelled_on_fulfill(self):
        self.order()
        self.db.upsert("print_jobs", {
            "id": "job-q", "order_id": "order-1", "state": "queued",
            "created_at": now_iso(),
        })
        summary = self.service.summary("order-1")
        self.assertTrue(summary["can_fulfill"])
        self.service.fulfill("order-1", handoff_confirmed=True, payment_action="debt")
        self.assertEqual(
            self.db.one("SELECT state FROM print_jobs WHERE id='job-q'")["state"],
            "cancelled",
        )

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


class FulfillmentShelfMirrorTests(unittest.TestCase):
    """И3: выдача заказа со склада-витрины зеркалится на карточки полки."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "mirror.sqlite3")
        self.repo = Repo(self.db)
        self.acc = Accounting(self.db)
        self.stock = Stock(self.db)
        self.service = OrderFulfillment(self.db, self.repo, self.stock, self.acc)
        self.db.upsert("warehouses", {
            "id": "wh-zone", "name": "Витрина", "kind": "shelf",
            "archived": 0, "position": 0})
        self.db.upsert("warehouses", {
            "id": "wh-home", "name": "Домашний", "kind": "home",
            "archived": 0, "position": 1})
        self.db.upsert("nomenclature", {
            "id": "nom-1", "name": "Адресник", "unit": "шт"})
        self.stock.add_move("nom-1", "wh-zone", 10, 0, doc_kind="receipt",
                            note="старт")
        self.db.upsert("shelf_items", {
            "id": "sh-1", "name": "Адресник", "nom_id": "nom-1",
            "qty": 10, "price": 500, "active": 1})

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def order(self, order_id="order-1", **overrides):
        data = {"id": order_id, "number": "1001", "product": "Адресник",
                "customer_name": "Мария", "status": "ready",
                "quality": "passed", "qty": 2, "price": 1000, "paid": 0,
                "reserved": 1, "created_at": now_iso(),
                "updated_at": now_iso()}
        data.update(overrides)
        return self.db.upsert("orders", data)

    def shelf_qty(self):
        return num(self.db.one(
            "SELECT qty FROM shelf_items WHERE id='sh-1'")["qty"])

    def test_zone_issue_mirrors_to_shelf_card(self):
        self.order()
        self.stock.reserve("nom-1", 2, "order-1", warehouse_id="wh-zone")
        self.service.fulfill("order-1", handoff_confirmed=True,
                             payment_action="debt")
        self.assertEqual(self.shelf_qty(), 8)
        move = self.db.one(
            "SELECT * FROM shelf_moves WHERE item_id='sh-1'"
            " ORDER BY rowid DESC")
        self.assertEqual(move["kind"], "writeoff")
        self.assertIn("1001", move["note"])
        # зона списана один раз — движением выдачи, без второй ноги
        self.assertEqual(self.stock.qty("nom-1", "wh-zone"), 8)
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) n FROM stock_moves WHERE doc_id='order-1'")["n"],
            1)
        self.assertEqual(
            self.db.one("SELECT COUNT(*) n FROM reserves"
                        " WHERE order_id='order-1' AND state='active'")["n"], 0)

    def test_shelf_shortfall_rolls_back_issue(self):
        self.order()
        self.stock.reserve("nom-1", 2, "order-1", warehouse_id="wh-zone")
        self.db.execute("UPDATE shelf_items SET qty=1 WHERE id='sh-1'")
        with self.assertRaisesRegex(ValueError, "не хватает"):
            self.service.fulfill("order-1", handoff_confirmed=True,
                                 payment_action="debt")
        self.assertEqual(
            self.db.one("SELECT status FROM orders WHERE id='order-1'")["status"],
            "ready")
        self.assertEqual(self.stock.qty("nom-1", "wh-zone"), 10)
        self.assertEqual(self.shelf_qty(), 1)
        self.assertEqual(
            self.db.one("SELECT COUNT(*) n FROM reserves"
                        " WHERE order_id='order-1' AND state='active'")["n"], 1)

    def test_plain_warehouse_issue_ignores_shelf(self):
        self.stock.add_move("nom-1", "wh-home", 10, 0, doc_kind="receipt",
                            note="старт")
        self.order()
        self.stock.reserve("nom-1", 2, "order-1", warehouse_id="wh-home")
        self.service.fulfill("order-1", handoff_confirmed=True,
                             payment_action="debt")
        self.assertEqual(self.shelf_qty(), 10)  # полка не тронута
        self.assertEqual(self.stock.qty("nom-1", "wh-home"), 8)

    def test_zone_without_card_issues_cleanly(self):
        self.db.execute("UPDATE shelf_items SET active=0 WHERE id='sh-1'")
        self.order()
        self.stock.reserve("nom-1", 2, "order-1", warehouse_id="wh-zone")
        self.service.fulfill("order-1", handoff_confirmed=True,
                             payment_action="debt")
        self.assertEqual(self.stock.qty("nom-1", "wh-zone"), 8)


if __name__ == "__main__":
    unittest.main()

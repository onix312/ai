"""Оприходование готового заказа на учётный склад (регистр 3.0).

Не выдача клиенту: товар из заказа в статусе «Готов» попадает в
`stock_moves` документом прихода, заказ закрывается финальным статусом
«На складе». Оплата и долг не создаются автоматически.
"""
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
from connector.printflow.documents import Documents
from connector.printflow.repo import Repo
from connector.printflow.stock import Stock
from connector.printflow.stocking import OrderStocker


class OrderStockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "stocking.sqlite3")
        self.repo = Repo(self.db)
        self.acc = Accounting(self.db)
        self.stock = Stock(self.db)
        self.docs = Documents(self.db)
        self.service = OrderStocker(self.db, self.repo, self.stock, self.docs, self.acc)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def seed(self, order_id="order-1", **overrides):
        warehouse = self.db.upsert("warehouses", {
            "id": "warehouse-1", "name": "Основной склад", "kind": "retail",
            "retail": 1, "archived": 0, "position": 0,
        })
        item = self.db.upsert("nomenclature", {
            "id": "item-1", "code": "000001", "name": "Готовое изделие",
            "kind": "product", "unit": "шт", "archived": 0,
            "grams": 50, "hours": 1,
        })
        data = {
            "id": order_id, "number": "1001", "product": "Готовое изделие",
            "customer_name": "", "status": "ready", "quality": "passed",
            "qty": 3, "price": 0, "paid": 0, "nom_id": item["id"],
            "warehouse_id": warehouse["id"], "actual_grams": 150,
            "actual_hours": 3, "actual_cost": 450, "auto_cost": 1,
            "created_at": now_iso(), "updated_at": now_iso(),
        }
        data.update(overrides)
        self.db.upsert("orders", data)
        return warehouse, item

    def test_stock_creates_move_and_closes_as_final_without_payment(self):
        warehouse, item = self.seed()
        summary = self.service.summary("order-1")
        self.assertTrue(summary["can_stock"])
        self.assertEqual(summary["quantity"], 3)

        result = self.service.stock_to_warehouse(
            "order-1", warehouse_id=warehouse["id"])
        self.assertFalse(result["already_stocked"])
        self.assertTrue(result["stocked"])

        # Остаток в регистре увеличился на 3 шт.
        self.assertEqual(self.stock.qty(item["id"], warehouse["id"]), 3)
        move = self.db.one(
            "SELECT * FROM stock_moves WHERE nom_id=? AND warehouse_id=? "
            "AND doc_kind='receipt'", (item["id"], warehouse["id"]))
        self.assertIsNotNone(move)
        self.assertEqual(num(move["qty"]), 3)
        self.assertEqual(result["document"]["kind"], "receipt")

        # Заказ закрыт как «На складе», оплата не создана.
        order = self.db.one(
            "SELECT status, closed_at, reserved FROM orders WHERE id='order-1'")
        self.assertEqual(order["status"], "stocked")
        self.assertTrue(order["closed_at"])
        self.assertEqual(num(order["reserved"]), 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM payments")["n"], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM transactions")["n"], 0)

    def test_stock_is_atomic_and_idempotent(self):
        warehouse, item = self.seed()
        self.service.stock_to_warehouse("order-1", warehouse_id=warehouse["id"])
        second = self.service.stock_to_warehouse("order-1", warehouse_id=warehouse["id"])
        self.assertTrue(second["already_stocked"])
        self.assertEqual(self.stock.qty(item["id"], warehouse["id"]), 3)
        self.assertEqual(
            self.db.one("SELECT COUNT(*) n FROM stock_moves WHERE doc_kind='receipt'")["n"],
            1,
        )

    def test_summary_blocks_when_not_ready(self):
        self.seed(status="post")
        summary = self.service.summary("order-1")
        self.assertFalse(summary["can_stock"])
        self.assertEqual({item["code"] for item in summary["blocks"]}, {"status"})
        with self.assertRaisesRegex(ValueError, "статус «Готов»"):
            self.service.stock_to_warehouse("order-1")

    def test_requires_nomenclature_item(self):
        self.seed()
        order = self.db.one("SELECT * FROM orders WHERE id='order-1'")
        order["nom_id"] = ""
        self.db.upsert("orders", order)
        summary = self.service.summary("order-1")
        self.assertFalse(summary["can_stock"])
        self.assertIn("goods", {item["code"] for item in summary["blocks"]})

    def test_reserved_stock_is_released_without_double_count(self):
        warehouse, item = self.seed(reserved=1, qty=2)
        # Готовый товар уже лежит в регистре и зарезервирован под заказ.
        self.stock.add_move(item["id"], warehouse["id"], 5, 500,
                            doc_id="receipt", doc_kind="receipt")
        self.stock.reserve(item["id"], 2, "order-1", warehouse["id"], "заказ")

        result = self.service.stock_to_warehouse("order-1", warehouse_id=warehouse["id"])
        self.assertEqual(result["document"]["kind"], "release")
        # Остаток не изменился, резерв снят.
        self.assertEqual(self.stock.qty(item["id"], warehouse["id"]), 5)
        self.assertEqual(self.stock.reserved(item["id"], warehouse["id"]), 0)
        order = self.db.one("SELECT status FROM orders WHERE id='order-1'")
        self.assertEqual(order["status"], "stocked")

    def test_multi_item_order_posts_each_line(self):
        warehouse, item = self.seed(qty=0)
        item2 = self.db.upsert("nomenclature", {
            "id": "item-2", "code": "000002", "name": "Второе изделие",
            "kind": "product", "unit": "шт", "archived": 0,
            "grams": 30, "hours": 0.5,
        })
        self.db.execute("DELETE FROM order_items WHERE order_id='order-1'")
        self.db.upsert("order_items", {
            "id": "oi-1", "order_id": "order-1", "position": 0,
            "nom_id": item["id"], "name": "Готовое изделие", "qty": 2,
            "grams": 50, "hours": 1,
        })
        self.db.upsert("order_items", {
            "id": "oi-2", "order_id": "order-1", "position": 1,
            "nom_id": item2["id"], "name": "Второе изделие", "qty": 3,
            "grams": 30, "hours": 0.5,
        })
        order = self.db.one("SELECT * FROM orders WHERE id='order-1'")
        order["actual_cost"] = 0
        self.db.upsert("orders", order)

        summary = self.service.summary("order-1")
        self.assertEqual(summary["quantity"], 5)
        self.service.stock_to_warehouse(
            "order-1", warehouse_id=warehouse["id"])
        self.assertEqual(self.stock.qty(item["id"], warehouse["id"]), 2)
        self.assertEqual(self.stock.qty(item2["id"], warehouse["id"]), 3)
        order = self.db.one("SELECT status FROM orders WHERE id='order-1'")
        self.assertEqual(order["status"], "stocked")

    def test_failure_posts_nothing_when_doc_post_fails(self):
        warehouse, item = self.seed()
        with (
            mock.patch.object(self.docs, "post",
                              side_effect=RuntimeError("doc failure")),
            self.assertRaisesRegex(RuntimeError, "doc failure"),
        ):
            self.service.stock_to_warehouse("order-1", warehouse_id=warehouse["id"])
        # Никакого сдвига статуса и никакого прихода.
        order = self.db.one("SELECT status FROM orders WHERE id='order-1'")
        self.assertEqual(order["status"], "ready")
        self.assertEqual(self.stock.qty(item["id"], warehouse["id"]), 0)


if __name__ == "__main__":
    unittest.main()

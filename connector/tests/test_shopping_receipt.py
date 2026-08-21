"""Закупка пластика: подтверждённый приход, касса и защита от дублей."""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.api import Api
from connector.printflow.db import Database
from connector.printflow.shopping import ShoppingList


class ShoppingReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "shopping.sqlite3")
        self.shop = ShoppingList(self.db)
        self.item = self.shop.add({
            "id": "shop-1", "name": "PETG Белый", "material": "PETG",
            "color_name": "Белый", "qty": 2, "unit": "катушка",
            "reason": "мало на складе",
        })

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def receive(self, **overrides):
        data = {
            "received_confirmed": True,
            "payment_confirmed": True,
            "material": "PETG",
            "color_name": "Белый",
            "brand": "PrintFlow Test",
            "spool_count": 2,
            "spool_grams": 1000,
            "total_amount": 3200,
            "account_id": "cash",
            "request_id": "receipt-request-1",
        }
        data.update(overrides)
        return self.shop.receive("shop-1", **data)

    def test_receipt_requires_physical_and_payment_confirmation(self):
        with self.assertRaisesRegex(ValueError, "действительно получены"):
            self.receive(received_confirmed=False)
        with self.assertRaisesRegex(ValueError, "фактическую оплату"):
            self.receive(payment_confirmed=False)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM spools")["n"], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM transactions")["n"], 0)
        self.assertEqual(self.db.one("SELECT done FROM shopping_items WHERE id='shop-1'")["done"], 0)

    def test_receipt_creates_individual_spools_expense_and_closes_item(self):
        result = self.receive(supplier="Поставщик", warehouse_id="materials")
        self.assertFalse(result["already_received"])
        self.assertEqual(result["received_grams"], 2000)
        self.assertEqual(len(result["spools"]), 2)
        self.assertTrue(all(row["remaining_grams"] == 1000 for row in result["spools"]))
        self.assertEqual(sum(row["price"] for row in result["spools"]), 3200)
        self.assertTrue(all(row["supplier"] == "Поставщик" for row in result["spools"]))
        tx = result["transaction"]
        self.assertEqual(tx["kind"], "expense")
        self.assertEqual(tx["category"], "filament")
        self.assertEqual(tx["amount"], 3200)
        item = result["item"]
        self.assertEqual(item["done"], 1)
        self.assertTrue(item["received_at"])
        self.assertEqual(item["receipt_tx_id"], tx["id"])
        with self.assertRaisesRegex(ValueError, "нельзя удалить"):
            self.shop.delete("shop-1")
        self.assertEqual(self.shop.clear_done(), 0)
        self.assertIsNotNone(self.db.one("SELECT id FROM shopping_items WHERE id='shop-1'"))

    def test_retry_with_same_request_is_idempotent(self):
        first = self.receive()
        second = self.receive()
        self.assertFalse(first["already_received"])
        self.assertTrue(second["already_received"])
        self.assertEqual(
            [row["id"] for row in first["spools"]],
            [row["id"] for row in second["spools"]],
        )
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM spools")["n"], 2)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM transactions")["n"], 1)

    def test_second_request_cannot_receive_same_item_twice(self):
        self.receive()
        with self.assertRaisesRegex(ValueError, "уже принята"):
            self.receive(request_id="another-request")
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM spools")["n"], 2)

    def test_failure_rolls_back_spools_expense_and_item(self):
        with mock.patch.object(self.shop.acc, "add_transaction", side_effect=RuntimeError("cash failed")):
            with self.assertRaisesRegex(RuntimeError, "cash failed"):
                self.receive()
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM spools")["n"], 0)
        item = self.db.one("SELECT * FROM shopping_items WHERE id='shop-1'")
        self.assertEqual(item["done"], 0)
        self.assertEqual(item["receipt_request_id"], "")

    def test_old_done_toggle_cannot_bypass_receipt(self):
        with self.assertRaisesRegex(ValueError, "подтверждённого приёма"):
            self.shop.toggle("shop-1", True)
        self.assertEqual(self.db.one("SELECT done FROM shopping_items WHERE id='shop-1'")["done"], 0)

    def test_auto_fill_keeps_low_colors_as_separate_purchase_rows(self):
        self.db.upsert("spools", {
            "id": "black", "material": "PLA", "color_name": "Чёрный",
            "total_grams": 1000, "remaining_grams": 10, "archived": 0,
        })
        self.db.upsert("spools", {
            "id": "red", "material": "PLA", "color_name": "Красный",
            "total_grams": 1000, "remaining_grams": 20, "archived": 0,
        })
        result = self.shop.auto_fill(dry_run=True)
        colors = {row.get("color_name") for row in result["added"] if row["material"] == "PLA"}
        self.assertEqual(colors, {"Чёрный", "Красный"})


class ShoppingReceiptApiRouteTests(unittest.TestCase):
    def test_route_passes_strict_confirmations_and_request_key(self):
        api = Api.__new__(Api)
        api.shopping = mock.Mock()
        api.shopping.receive.return_value = {"ok": True}
        code, payload = api.post(
            "/api/shopping/receive",
            {
                "id": "shop-1", "received_confirmed": True,
                "payment_confirmed": "true", "material": "PLA",
                "spool_count": 2, "spool_grams": 1000, "total_amount": 2000,
                "request_id": "receipt-1",
            }, {},
        )
        self.assertEqual((code, payload), (200, {"ok": True}))
        api.shopping.receive.assert_called_once_with(
            "shop-1", received_confirmed=True, payment_confirmed=False,
            material="PLA", color_name="", color_hex="", brand="",
            spool_count=2.0, spool_grams=1000.0, total_amount=2000.0,
            account_id="", supplier="", warehouse_id="", request_id="receipt-1",
        )


class ShoppingReceiptMigrationTests(unittest.TestCase):
    def test_old_shopping_table_gets_receipt_fields_marker_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "old.sqlite3"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE shopping_items (id TEXT PRIMARY KEY,name TEXT DEFAULT '',"
                "material TEXT DEFAULT '',qty REAL DEFAULT 1,unit TEXT DEFAULT 'кг',"
                "reason TEXT DEFAULT '',source TEXT DEFAULT 'manual',done INTEGER DEFAULT 0,"
                "created_at TEXT,updated_at TEXT)"
            )
            conn.execute(
                "INSERT INTO shopping_items(id,name,material,done) VALUES('old','PLA','PLA',1)"
            )
            conn.commit()
            conn.close()
            db = Database(path)
            try:
                columns = db.columns("shopping_items")
                self.assertIn("receipt_request_id", columns)
                self.assertIn("receipt_spool_ids", columns)
                self.assertEqual(
                    db.one("SELECT received_at FROM shopping_items WHERE id='old'")["received_at"],
                    "legacy",
                )
                indexes = {row["name"] for row in db.query("PRAGMA index_list(shopping_items)")}
                self.assertIn("idx_shopping_receipt_request", indexes)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()

"""PrintFlow v19: инварианты целостности данных стеллажа."""
from __future__ import annotations

import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from connector.printflow.db import Database
from connector.printflow.shelf import Shelf
from connector.printflow.stock import Stock


class ShelfV19IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "v19-shelf.sqlite3")
        self.shelf = Shelf(self.db)
        self.stock = Stock(self.db)
        self.db.upsert("warehouses", {
            "id": "home", "name": "Основной склад", "kind": "home",
            "archived": 0, "position": 1,
        })
        if not self.stock.shelf_warehouse():
            self.db.upsert("warehouses", {
                "id": "shelf", "name": "Полка магазина", "kind": "shelf",
                "archived": 0, "position": 0,
            })
        self.db.upsert("nomenclature", {
            "id": "nom-1", "name": "Органайзер", "kind": "product",
            "unit": "шт", "archived": 0,
        })

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_existing_quantity_cannot_be_rewritten_by_card_save(self):
        item = self.shelf.save_item({
            "name": "Органайзер", "nom_id": "nom-1",
            "qty": 3, "price": 500,
        })
        with self.assertRaisesRegex(ValueError, "Остаток нельзя редактировать"):
            self.shelf.save_item({
                "id": item["id"], "name": "Органайзер",
                "qty": 2, "price": 500,
            })
        self.assertEqual(self.shelf.item(item["id"])["qty"], 3)

    def test_unchanged_legacy_quantity_is_tolerated_on_edit(self):
        item = self.shelf.save_item({
            "name": "Органайзер", "qty": 3, "price": 500,
        })
        saved = self.shelf.save_item({
            "id": item["id"], "name": "Органайзер v2",
            "qty": 3, "price": 550,
        })
        self.assertEqual(saved["qty"], 3)
        self.assertEqual(saved["price"], 550)

    def test_summary_uses_historical_sale_price_not_current_card_price(self):
        item = self.shelf.save_item({
            "name": "Органайзер", "qty": 5, "price": 500,
        })
        self.shelf.sale(item["id"], 1, channel="shelf")
        self.shelf.save_item({
            "id": item["id"], "name": "Органайзер", "price": 900,
        })
        row = self.shelf.item(item["id"])
        self.assertEqual(row["sold_7"], 1)
        self.assertEqual(row["sold_7_money"], 500)
        self.assertEqual(self.shelf.summary()["sold_7_money"], 500)

    def test_archive_requires_zero_balance_and_preserves_move_history(self):
        item = self.shelf.save_item({
            "name": "Органайзер", "qty": 1, "price": 500,
        })
        with self.assertRaisesRegex(ValueError, "ещё 1"):
            self.shelf.delete_item(item["id"])

        self.shelf.sale(item["id"], 1, channel="shelf")
        self.shelf.delete_item(item["id"])

        raw = self.db.one("SELECT * FROM shelf_items WHERE id=?", (item["id"],))
        self.assertIsNotNone(raw)
        self.assertEqual(int(raw["active"]), 0)
        history = self.shelf.moves(item["id"])
        self.assertTrue(history)
        self.assertEqual(history[0]["item_name"], "Органайзер")
        self.assertNotIn(item["id"], {row["id"] for row in self.shelf.items()})

    def test_round_trip_stock_shelf_stock_is_atomic_and_conserves_total(self):
        self.stock.add_move("nom-1", "home", 5, 500, doc_kind="receipt")
        moved_in = self.shelf.transfer_from_stock("nom-1", "home", 2)
        item_id = moved_in["item"]["id"]
        zone = self.stock.shelf_warehouse()

        self.assertEqual(self.stock.qty("nom-1", "home"), 3)
        self.assertEqual(self.stock.qty("nom-1", zone), 2)
        self.assertEqual(self.shelf.item(item_id)["qty"], 2)

        moved_out = self.shelf.transfer_to_stock(item_id, "home", 1)
        self.assertTrue(moved_out["ok"])
        self.assertEqual(self.stock.qty("nom-1", "home"), 4)
        self.assertEqual(self.stock.qty("nom-1", zone), 1)
        self.assertEqual(self.shelf.item(item_id)["qty"], 1)
        self.assertEqual(self.stock.qty("nom-1"), 5)

    def test_transfer_to_stock_respects_shelf_reservations(self):
        self.stock.add_move("nom-1", "home", 2, 200, doc_kind="receipt")
        moved = self.shelf.transfer_from_stock("nom-1", "home", 2)
        zone = self.stock.shelf_warehouse()
        self.stock.reserve(
            "nom-1", 2, order_id="order-1",
            warehouse_id=zone, note="под заказ")
        with self.assertRaisesRegex(ValueError, "зарезервировано"):
            self.shelf.transfer_to_stock(moved["item"]["id"], "home", 1)

    def test_unlinked_shelf_item_cannot_be_moved_into_stock(self):
        item = self.shelf.save_item({"name": "Временный товар", "qty": 1})
        with self.assertRaisesRegex(ValueError, "не связана"):
            self.shelf.transfer_to_stock(item["id"], "home", 1)


if __name__ == "__main__":
    unittest.main()

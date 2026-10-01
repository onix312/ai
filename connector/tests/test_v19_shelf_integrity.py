"""PrintFlow 19: invariants for the retail shelf ledger."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow.shelf import Shelf  # noqa: E402


_held: list[tempfile.TemporaryDirectory] = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "test.sqlite3")


class ShelfV19IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.shelf = Shelf(self.db)

    def tearDown(self):
        self.db.close()

    def test_existing_quantity_cannot_be_edited_through_item_card(self):
        item = self.shelf.save_item({"name": "Dragon", "qty": 4, "price": 500})
        with self.assertRaisesRegex(ValueError, "Остаток нельзя менять"):
            self.shelf.save_item({
                "id": item["id"], "name": item["name"],
                "qty": 7, "price": item["price"],
            })
        self.assertEqual(4.0, self.shelf.item(item["id"])["qty"])

    def test_legacy_opening_quantity_is_recorded_as_movement(self):
        item = self.shelf.save_item({"name": "Fox", "qty": 3, "price": 450})
        self.assertEqual(3.0, item["qty"])
        moves = self.shelf.moves(item["id"])
        self.assertEqual(1, len(moves))
        self.assertEqual("inventory", moves[0]["kind"])
        self.assertEqual(3.0, moves[0]["qty"])
        self.assertIn("Начальный остаток", moves[0]["note"])

    def test_archive_preserves_history_and_name(self):
        item = self.shelf.save_item({"name": "Archived item", "qty": 0, "price": 100})
        self.shelf.delete_item(item["id"])
        stored = self.db.one("SELECT * FROM shelf_items WHERE id=?", (item["id"],))
        self.assertIsNotNone(stored)
        self.assertEqual(0, int(stored["active"]))
        self.assertIsNone(next((row for row in self.shelf.items()
                                if row["id"] == item["id"]), None))

    def test_archive_rejects_nonempty_position(self):
        item = self.shelf.save_item({"name": "Still on shelf", "qty": 1, "price": 100})
        with self.assertRaisesRegex(ValueError, "остатком"):
            self.shelf.delete_item(item["id"])
        self.assertEqual(1, int(self.db.one(
            "SELECT active FROM shelf_items WHERE id=?", (item["id"],))["active"]))

    def test_seven_day_revenue_uses_sale_price_not_current_card_price(self):
        item = self.shelf.save_item({"name": "Changing price", "qty": 2, "price": 100})
        self.shelf.sale(item["id"], 1, price=100, record_income=False)
        self.shelf.save_item({
            "id": item["id"], "name": item["name"], "price": 250,
        })
        summary = self.shelf.summary()
        self.assertEqual(1.0, summary["sold_7"])
        self.assertEqual(100.0, summary["sold_7_money"])


class ShelfV19FrontendContractTests(unittest.TestCase):
    def test_card_does_not_submit_quantity_and_uses_archive_wording(self):
        index = (ROOT / "site/index.html").read_text(encoding="utf-8")
        script = (ROOT / "site/assets/shelf.js").read_text(encoding="utf-8")
        self.assertIn('id="shf_qty"', index)
        self.assertIn('readonly title="Остаток меняется через приход, перенос или инвентаризацию"', index)
        self.assertIn('id="shelf_delete" hidden>Архивировать</button>', index)
        payload_start = script.index("const payload = {", script.index("async function saveShelf"))
        payload_end = script.index("};", payload_start)
        self.assertNotIn("qty:", script[payload_start:payload_end])
        self.assertIn("Архивировать позицию стеллажа", script)


if __name__ == "__main__":
    unittest.main()

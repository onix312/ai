"""Ценники стеллажа: кастомизация, кассовый код 1С и безопасное списание."""
from __future__ import annotations

import pathlib
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from connector.printflow.db import Database
from connector.printflow.shelf import Shelf


_held: list[tempfile.TemporaryDirectory] = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "test.sqlite3")


class ShelfPriceTagTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.shelf = Shelf(self.db)

    def tearDown(self):
        self.db.close()

    def test_tag_fields_are_saved_and_validated(self):
        item = self.shelf.save_item({
            "name": "Органайзер", "qty": 3, "price": 590,
            "barcode": "4601234567890", "sku": "NOZZA-001",
            "tag_template": "promo", "tag_badge": "Хит",
            "tag_color": "#ff5500", "tag_note": "Цвет на выбор",
        })
        self.assertEqual(item["barcode"], "4601234567890")
        self.assertEqual(item["tag_template"], "promo")
        self.assertEqual(item["tag_color"], "#ff5500")
        with self.assertRaisesRegex(ValueError, "тип ценника"):
            self.shelf.save_item({"name": "Плохой", "tag_template": "unknown"})
        with self.assertRaisesRegex(ValueError, "формате"):
            self.shelf.save_item({"name": "Плохой", "tag_color": "red"})
        with self.assertRaises(ValueError):
            self.shelf.save_item({"name": "Кириллица", "barcode": "КОД-1"})

    def test_barcode_inherits_from_canonical_nomenclature(self):
        self.db.upsert("nomenclature", {
            "id": "nom-1", "name": "Адресник", "code": "000042",
            "sku": "TAG-42", "barcode": "4600000000042", "material": "PETG",
            "grams": 12.0,
        })
        item = self.shelf.save_item({"name": "Адресник", "nom_id": "nom-1", "qty": 2})
        self.assertEqual(item["barcode"], "4600000000042")
        self.assertEqual(item["barcode_source"], "nomenclature")
        self.assertEqual(item["sku"], "TAG-42")
        self.assertEqual(item["material"], "PETG")

    def test_duplicate_explicit_barcode_is_rejected(self):
        self.shelf.save_item({"name": "Первый", "barcode": "4601234567890"})
        with self.assertRaisesRegex(ValueError, "уже привязан"):
            self.shelf.save_item({"name": "Второй", "barcode": "4601234567890"})

    def test_one_c_sale_is_idempotent_and_does_not_duplicate_income(self):
        item = self.shelf.save_item({
            "name": "Подставка", "barcode": "4601234567890", "qty": 5, "price": 700,
        })
        first = self.shelf.sale_from_1c("4601234567890", 2, "CHECK-7:1", 690)
        second = self.shelf.sale_from_1c("4601234567890", 2, "CHECK-7:1", 690)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(self.shelf.item(item["id"])["qty"], 3)
        self.assertEqual(self.db.query("SELECT * FROM transactions"), [])
        moves = self.db.query("SELECT * FROM shelf_moves WHERE source='1c'")
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["external_id"], "CHECK-7:1")
        self.assertEqual(moves[0]["price"], 690)

    def test_one_c_sale_requires_external_line_id(self):
        self.shelf.save_item({"name": "Подставка", "barcode": "ABC-1", "qty": 1})
        with self.assertRaisesRegex(ValueError, "external_id"):
            self.shelf.sale_from_1c("ABC-1", 1, "")

    def test_one_c_csv_has_bom_and_cashier_columns(self):
        self.shelf.save_item({
            "name": "Крючок", "barcode": "4601234567890", "sku": "HOOK-1",
            "qty": 4, "price": 320,
        })
        content = self.shelf.one_c_export_csv()
        self.assertTrue(content.startswith("\ufeff"))
        self.assertIn("Артикул;Штрихкод;Наименование;Цена", content)
        self.assertIn("HOOK-1;4601234567890;Крючок;320,00;4,00", content)


class ShelfPriceTagApiTests(unittest.TestCase):
    def setUp(self):
        from connector.printflow.api import Api
        self.db = make_db()
        self.api = Api.__new__(Api)
        self.api.db = self.db
        self.api.shelf = Shelf(self.db)
        self.api.last_host = "192.168.1.20:8080"
        self.api.listen_port = 8080
        self.api.manager = types.SimpleNamespace(printers={}, bot=None)
        self.api.shelf.save_item({
            "name": "Органайзер", "barcode": "4601234567890",
            "sku": "ORG-1", "qty": 2, "price": 590, "tag_template": "compact",
        })

    def tearDown(self):
        self.db.close()

    def test_labels_contain_barcode_and_tag_preferences(self):
        payload = self.api.labels("shelf")
        self.assertEqual(payload["one_c"], {"linked": 1, "total": 1})
        self.assertIn("<svg", payload["shelf"][0]["barcode_svg"])
        self.assertEqual(payload["shelf"][0]["tag_template"], "compact")

    def test_lookup_export_and_sale_routes(self):
        code, found = self.api.get("/api/shelf/1c/lookup", {"barcode": ["4601234567890"]})
        self.assertEqual(code, 200)
        self.assertEqual(found["item"]["name"], "Органайзер")
        code, exported = self.api.get("/api/shelf/1c/export", {})
        self.assertEqual(code, 200)
        self.assertEqual(exported["linked"], 1)
        code, sold = self.api.post("/api/shelf/1c/sale", {
            "barcode": "4601234567890", "qty": 1,
            "external_id": "RECEIPT-1:1", "price": 590,
        }, {})
        self.assertEqual(code, 200)
        self.assertFalse(sold["duplicate"])


class ShelfPriceTagFrontendTests(unittest.TestCase):
    def test_designer_is_linked_from_shelf(self):
        index = (ROOT / "site/index.html").read_text(encoding="utf-8")
        page = (ROOT / "site/price-tags.html").read_text(encoding="utf-8")
        script = (ROOT / "site/assets/shelf.js").read_text(encoding="utf-8")
        self.assertIn('/price-tags.html', index)
        self.assertIn('/api/shelf/1c/export', page)
        self.assertIn('/api/shelf/1c/sale', page)
        self.assertIn('/price-tags.html?item=', script)


if __name__ == "__main__":
    unittest.main()

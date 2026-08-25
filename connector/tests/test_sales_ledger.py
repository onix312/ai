"""Реестр проданных товаров: документы, заказы и стеллаж в одном списке."""
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
from connector.printflow.documents import Documents  # noqa: E402
from connector.printflow.nomenclature import Nomenclature  # noqa: E402
from connector.printflow.shelf import Shelf  # noqa: E402


class SalesLedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.acc = Accounting(self.db)
        self.nom = Nomenclature(self.db)
        self.docs = Documents(self.db)
        self.wh = self.db.one(
            "SELECT id FROM warehouses WHERE archived=0 ORDER BY position LIMIT 1")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _product(self, name="Адресник", price=500, cost=100):
        return self.nom.save({
            "name": name, "kind": "product", "price": price,
            "cost": cost, "unit": "шт",
        })

    def test_document_sales_appear_as_line_items(self):
        item = self._product()
        self.docs.quick_receipt(item["id"], 5, 100, self.wh["id"])
        self.docs.quick_sale(
            [{"nom_id": item["id"], "qty": 2, "price": 500}],
            self.wh["id"], "shop", note="продажа документом")

        rep = self.acc.sales_details("year")
        docs = [r for r in rep["rows"] if r["source"] == "document"]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["name"], "Адресник")
        self.assertEqual(docs[0]["qty"], 2)
        self.assertEqual(docs[0]["amount"], 1000)
        self.assertEqual(docs[0]["profit"], 800)

    def test_shelf_and_documents_do_not_double_sale(self):
        item = self._product("Брелок")
        self.docs.quick_receipt(item["id"], 3, 100, self.wh["id"])
        self.docs.quick_sale(
            [{"nom_id": item["id"], "qty": 1, "price": 500}], self.wh["id"], "shop")

        shelf = Shelf(self.db)
        pos = shelf.save_item({"name": "Котобажик", "price": 300,
                               "cost_per_unit": 80, "qty": 2})
        shelf.sale(pos["id"], 1, channel="shelf", note="из ТГ")

        rep = self.acc.sales_details("year")
        sources = sorted(r["source"] for r in rep["rows"])
        self.assertEqual(sources, ["document", "shelf"])
        self.assertEqual(rep["count"], 2)
        self.assertEqual(rep["total_amount"], 800)
        # Сводка по товарам содержит обе позиции.
        names = {p["name"] for p in rep["products"]}
        self.assertEqual(names, {"Брелок", "Котобажик"})

    def test_csv_contains_each_sold_item(self):
        item = self._product()
        self.docs.quick_receipt(item["id"], 2, 100, self.wh["id"])
        self.docs.quick_sale(
            [{"nom_id": item["id"], "qty": 1, "price": 500}], self.wh["id"], "shop")
        csv_text = self.acc.sales_details_csv("year")
        self.assertIn("Адресник", csv_text)
        self.assertIn("Документ", csv_text)


if __name__ == "__main__":
    unittest.main()

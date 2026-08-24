"""Тесты перемещения товара со склада на стеллаж.

Правило: перемещать можно только товар с остатком ≥ 1 шт, целыми штуками
и не больше остатка. Регистр остатков получает пару движений «перемещение»,
стеллаж — приход штук с себестоимостью по средней складской.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow.documents import Documents  # noqa: E402
from connector.printflow.shelf import Shelf  # noqa: E402
from connector.printflow.stock import Stock  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "test.sqlite3")


class ShelfTransferTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.shelf = Shelf(self.db)
        self.stock = Stock(self.db)
        self.db.upsert("nomenclature", {
            "id": "nom1", "name": "Органайзер", "kind": "product",
            "unit": "шт", "archived": 0})
        # 5 штук на домашнем складе по 100 ₽
        self.stock.add_move("nom1", "home", 5, 500, doc_kind="receipt")

    def tearDown(self):
        self.db.close()

    def test_stock_available_lists_only_one_or_more(self):
        """В списке кандидатов только товары с остатком ≥ 1 шт."""
        # второй товар: остаток 0.4 — не должен попасть
        self.db.upsert("nomenclature", {
            "id": "nom2", "name": "Хвостик", "kind": "product",
            "unit": "шт", "archived": 0})
        self.stock.add_move("nom2", "home", 0.4, 10, doc_kind="receipt")
        # третий товар: остаток 0 — тоже мимо
        self.db.upsert("nomenclature", {
            "id": "nom3", "name": "Пусто", "kind": "product",
            "unit": "шт", "archived": 0})
        self.stock.add_move("nom3", "home", 2, 100, doc_kind="receipt")
        self.stock.add_move("nom3", "home", -2, -100, doc_kind="sale")
        items = self.shelf.stock_available()
        ids = [i["nom_id"] for i in items]
        self.assertIn("nom1", ids)
        self.assertNotIn("nom2", ids)
        self.assertNotIn("nom3", ids)

    def test_stock_available_excludes_shelf_warehouse(self):
        """Полка магазина (kind='shelf') — не источник перемещения."""
        self.stock.add_move("nom1", "shelf", 3, 300, doc_kind="receipt")
        items = self.shelf.stock_available()
        warehouses = {i["warehouse_id"] for i in items if i["nom_id"] == "nom1"}
        self.assertEqual(warehouses, {"home"})

    def test_transfer_moves_stock_and_creates_shelf_item(self):
        result = self.shelf.transfer_from_stock("nom1", "home", 2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["qty"], 2)
        # регистр: со склада ушло, на полку пришло
        self.assertEqual(self.stock.qty("nom1", "home"), 3)
        self.assertEqual(self.stock.qty("nom1", "shelf"), 2)
        # позиция стеллажа создана и штуки пришли
        item = result["item"]
        self.assertEqual(item["name"], "Органайзер")
        self.assertEqual(round(item["qty"], 1), 2.0)
        self.assertEqual(round(item["cost_per_unit"], 2), 100.0)

    def test_transfer_into_existing_item(self):
        existing = self.shelf.save_item({"name": "Органайзер", "qty": 1,
                                         "price": 500})
        result = self.shelf.transfer_from_stock("nom1", "home", 1,
                                                item_id=existing["id"])
        self.assertEqual(result["item"]["id"], existing["id"])
        self.assertEqual(round(result["item"]["qty"], 1), 2.0)

    def test_transfer_rejects_zero_and_fraction(self):
        with self.assertRaises(ValueError):
            self.shelf.transfer_from_stock("nom1", "home", 0)
        with self.assertRaises(ValueError):
            self.shelf.transfer_from_stock("nom1", "home", 0.5)
        with self.assertRaises(ValueError):
            self.shelf.transfer_from_stock("nom1", "home", 1.5)

    def test_transfer_rejects_more_than_available(self):
        with self.assertRaises(ValueError):
            self.shelf.transfer_from_stock("nom1", "home", 6)

    def test_transfer_rejects_empty_stock(self):
        """Остаток меньше 1 шт — перемещать нечего."""
        self.db.upsert("nomenclature", {
            "id": "nom2", "name": "Хвостик", "kind": "product",
            "unit": "шт", "archived": 0})
        self.stock.add_move("nom2", "home", 0.4, 10, doc_kind="receipt")
        with self.assertRaises(ValueError):
            self.shelf.transfer_from_stock("nom2", "home", 1)

    def test_transfer_requires_known_nom(self):
        with self.assertRaises(ValueError):
            self.shelf.transfer_from_stock("нет-такого", "home", 1)

    def test_transfer_requires_warehouse(self):
        with self.assertRaises(ValueError):
            self.shelf.transfer_from_stock("nom1", "", 1)

    def test_transfer_showcase_display_item(self):
        """«Для магазина» — витринная позиция: переносится на стеллаж и создаёт ценник."""
        self.db.upsert("nomenclature", {
            "id": "nom4", "name": "Демо-фигурка", "kind": "showcase",
            "unit": "шт", "archived": 0})
        self.stock.add_move("nom4", "home", 2, 0, doc_kind="receipt")
        result = self.shelf.transfer_from_stock("nom4", "home", 1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["item"]["nom_id"], "nom4")
        self.assertEqual(result["item"]["name"], "Демо-фигурка")

    def test_showcase_receipt_ignores_price_as_cost(self):
        """Приход витрины не подставляет цену в себестоимость."""
        self.db.upsert("nomenclature", {
            "id": "nom6", "name": "Знак на витрину", "kind": "showcase",
            "unit": "шт", "archived": 0})
        self.db.upsert("warehouses", {"id": "home", "name": "Склад",
                                      "kind": "home", "archived": 0})
        docs = Documents(self.db)
        doc = docs.save({"kind": "receipt", "warehouse_id": "home",
                         "items": [{"nom_id": "nom6", "qty": 2,
                                    "price": 900, "cost": 0}]})
        docs.post(doc["id"])
        self.assertEqual(self.stock.qty("nom6", "home"), 2)
        # Себестоимость осталась нулевой — цена витрины не стала «с/с».
        self.assertEqual(self.stock.avg_cost("nom6", "home"), 0.0)

    def test_transfer_allows_fractional_non_piece_units(self):
        """Метраж/вес можно вынести на стеллаж дробно; штучные — только целыми."""
        self.db.upsert("nomenclature", {
            "id": "nom5", "name": "Лента 10 см", "kind": "product",
            "unit": "м", "archived": 0})
        self.stock.add_move("nom5", "home", 2.5, 125, doc_kind="receipt")
        items = self.shelf.stock_available()
        self.assertIn("nom5", [i["nom_id"] for i in items])
        result = self.shelf.transfer_from_stock("nom5", "home", 1.25)
        self.assertEqual(result["qty"], 1.25)
        self.assertAlmostEqual(self.stock.qty("nom5", "home"), 1.25)


if __name__ == "__main__":
    unittest.main()

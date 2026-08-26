"""Каталог как совместимое представление номенклатуры (9.3.2).

Баг: товары, созданные во вкладке «Товары», не имели legacy-строки
catalog и не попадали в «Изделия» и генераторы материалов (ценники,
наклейки) — хотя являются тем же каноническим каталогом.
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
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.nomenclature import Nomenclature  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402


class CatalogViewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.repo = Repo(self.db)
        self.acc = Accounting(self.db)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _product(self, key: str, name: str, price: float) -> dict:
        nom = Nomenclature(self.db)
        self.db.upsert("nomenclature", {
            "id": key, "kind": "product", "name": name, "material": "PETG",
            "grams": 20, "hours": 1.5, "fit_per_plate": 2,
            "created_at": "2026-08-20T10:00:00", "updated_at": "2026-08-20T10:00:00"})
        if price > 0:
            nom.set_price(key, price)
        return nom.item(key)

    def test_nom_product_without_legacy_row_is_visible(self):
        """Товар из «Товаров» появляется в каталоге с ценой и экономикой."""
        self._product("nom-a", "Ваза конус", 590)
        rows = self.repo.catalog()
        row = next((r for r in rows if r["id"] == "nom-a"), None)
        self.assertIsNotNone(row, "товар номенклатуры не попал в каталог")
        self.assertEqual(row["price"], 590)
        self.assertEqual(row["nom_id"], "nom-a")
        self.assertEqual(row["material"], "PETG")
        self.assertIn("economics", row)

    def test_legacy_linked_product_is_not_duplicated(self):
        """Связанная legacy-строка остаётся одной строкой каталога."""
        self.db.upsert("catalog", {
            "id": "cat-1", "name": "Адресник", "nom_id": "nom-b", "price": 350,
            "material": "PLA", "grams": 10, "hours": 1, "archived": 0,
            "created_at": "2026-08-20T10:00:00", "updated_at": "2026-08-20T10:00:00"})
        self._product("nom-b", "Адресник", 350)
        rows = [r for r in self.repo.catalog() if "адресник" in str(r["name"]).lower()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "cat-1")
        self.assertEqual(rows[0]["nom_id"], "nom-b")

    def test_archived_nom_product_is_hidden(self):
        self._product("nom-c", "Старый крючок", 150)
        self.db.execute("UPDATE nomenclature SET archived=1 WHERE id='nom-c'")
        rows = [r for r in self.repo.catalog() if r["id"] == "nom-c"]
        self.assertEqual(len(rows), 0)

    def test_delete_nom_only_row_archives_nomenclature(self):
        self._product("nom-d", "Органайзер", 480)
        self.repo.delete_catalog_item("nom-d")
        nom = self.db.one("SELECT archived FROM nomenclature WHERE id='nom-d'")
        self.assertEqual(num_(nom["archived"]), 1)
        self.assertFalse([r for r in self.repo.catalog() if r["id"] == "nom-d"])

    def test_edit_from_catalog_creates_legacy_mirror(self):
        """Правка товара из «Изделий» создаёт legacy-строку и не плодит дубли."""
        self._product("nom-e", "Брелок", 190)
        saved = self.repo.save_catalog_item({
            "id": "nom-e", "name": "Брелок гравировка", "price": 210,
            "material": "PLA", "grams": 8, "hours": 0.5})
        self.assertEqual(saved["id"], "nom-e")
        self.assertEqual(saved["nom_id"], "nom-e")
        rows = [r for r in self.repo.catalog() if r["id"] == "nom-e"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Брелок гравировка")
        self.assertEqual(rows[0]["price"], 210)

    def test_recalc_covers_nom_only_rows(self):
        """«Пересчитать цены» видит товары из «Товаров» и пишет цену в prices."""
        self._product("nom-f", "Полка настенная", 100)   # цена заведомо занижена
        self.db.set_settings({"target_profit_per_hour": 250})
        preview = self.acc.recalc_catalog(False)
        row = next((i for i in preview["items"] if i["id"] == "nom-f"), None)
        self.assertIsNotNone(row, "пересчёт не увидел товар номенклатуры")
        self.assertGreater(row["new_price"], 0)
        result = self.acc.recalc_catalog(True)
        self.assertTrue(any(i["id"] == "nom-f" for i in result["items"]))
        price = self.db.one(
            "SELECT price FROM prices WHERE nom_id='nom-f'"
            " ORDER BY at DESC LIMIT 1")
        self.assertEqual(num_(price["price"]), row["new_price"])
        # и представление каталога показывает новую цену
        fresh = next(r for r in self.repo.catalog() if r["id"] == "nom-f")
        self.assertEqual(num_(fresh["price"]), row["new_price"])


def num_(value) -> float:
    from connector.printflow.accounting import num
    return num(value)


if __name__ == "__main__":
    unittest.main()

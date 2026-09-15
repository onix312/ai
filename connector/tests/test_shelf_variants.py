"""Вариации товара на стеллаже: один товар — много ценников.

Смысл контракта простой: адресник в двенадцати цветах остаётся **одной**
карточкой номенклатуры, но на полке это разные позиции со своими остатком,
штрихкодом и ценником. Если вариации схлопнутся в одну позицию, склад отдаст
чужой остаток, а касса продаст не тот цвет.
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
from connector.printflow.cashier import Cashier  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.nomenclature import Nomenclature  # noqa: E402
from connector.printflow.shelf import Shelf  # noqa: E402
from connector.printflow.stock import Stock  # noqa: E402


class ShelfVariantCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Database(pathlib.Path(folder.name) / "shelf.sqlite3")
        self.addCleanup(self.db.close)
        self.nom = Nomenclature(self.db)
        self.shelf = Shelf(self.db)
        self.stock = Stock(self.db)
        self.warehouse = "home"
        self.db.upsert("warehouses", {"id": self.warehouse, "name": "Дом",
                                      "kind": "home"})
        self.nom.save({"id": "nom1", "name": "Адресник", "unit": "шт",
                       "kind": "product", "grams": 12, "hours": 0.4,
                       "archived": 0})
        self.created = self.nom.generate_variants(
            "nom1", [{"name": "Цвет", "values": ["Красный", "Синий"]},
                     {"name": "Размер", "values": ["S", "L"]}])
        self.variants = self.db.query(
            "SELECT * FROM nom_variants WHERE nom_id=? ORDER BY name", ("nom1",))

    def variant_by_name(self, name: str) -> dict:
        found = [v for v in self.variants if name in str(v.get("name") or "")]
        self.assertTrue(found, f"нет вариации «{name}»: {[v['name'] for v in self.variants]}")
        return found[0]

    def put_stock(self, variant_id: str, qty: float):
        """Готовые штуки на учётном складе — по вариации, как их и печатают."""
        self.stock.add_move("nom1", self.warehouse, qty, qty * 100.0,
                            doc_kind="produce", variant_id=variant_id)


class VariantGenerationTests(ShelfVariantCase):
    def test_variants_stay_one_product(self):
        """Четыре вариации — по-прежнему один товар в номенклатуре."""
        self.assertEqual(4, len(self.variants))
        self.assertEqual(1, len(self.db.query("SELECT id FROM nomenclature")))

    def test_every_variant_has_own_barcode_and_sku(self):
        codes = {str(v.get("sku") or "") for v in self.variants}
        self.assertNotIn("", codes, "у вариации должен быть свой артикул")
        self.assertEqual(len(self.variants), len(codes))


class ShelfVariantTransferTests(ShelfVariantCase):
    def test_each_variant_gets_its_own_shelf_card(self):
        red_l = self.variant_by_name("Красный / L")
        blue_l = self.variant_by_name("Синий / L")
        self.put_stock(red_l["id"], 3)
        self.put_stock(blue_l["id"], 2)
        self.shelf.transfer_from_stock("nom1", self.warehouse, 2,
                                       variant_id=red_l["id"])
        self.shelf.transfer_from_stock("nom1", self.warehouse, 1,
                                       variant_id=blue_l["id"])
        items = self.shelf.items_for_nom("nom1")
        self.assertEqual(2, len(items), "каждой вариации — своя карточка стеллажа")
        by_variant = {str(i.get("variant_id") or ""): i for i in items}
        self.assertEqual(2.0, by_variant[red_l["id"]]["qty"])
        self.assertEqual(1.0, by_variant[blue_l["id"]]["qty"])

    def test_variant_stock_is_moved_apart(self):
        """Перенос одной вариации не трогает остаток другой."""
        red_l = self.variant_by_name("Красный / L")
        red_s = self.variant_by_name("Красный / S")
        self.put_stock(red_l["id"], 3)
        self.put_stock(red_s["id"], 3)
        self.shelf.transfer_from_stock("nom1", self.warehouse, 2,
                                       variant_id=red_l["id"])
        self.assertEqual(3.0, self.stock.qty("nom1", self.warehouse, red_s["id"]))
        self.assertEqual(1.0, self.stock.qty("nom1", self.warehouse, red_l["id"]))

    def test_transfer_refuses_when_that_variant_is_empty(self):
        red_l = self.variant_by_name("Красный / L")
        blue_l = self.variant_by_name("Синий / L")
        self.put_stock(blue_l["id"], 5)
        with self.assertRaises(ValueError) as ctx:
            self.shelf.transfer_from_stock("nom1", self.warehouse, 1,
                                           variant_id=red_l["id"])
        self.assertIn("складе только", str(ctx.exception))

    def test_shelf_card_shows_variant_label(self):
        """Ценник и касса должны показать, чем этот цвет отличается."""
        red_s = self.variant_by_name("Красный / S")
        self.put_stock(red_s["id"], 1)
        self.shelf.transfer_from_stock("nom1", self.warehouse, 1,
                                       variant_id=red_s["id"])
        item = self.shelf.items()[0]
        self.assertEqual("Красный · S", item["variant_label"])
        self.assertEqual(red_s["id"], item["variant_id"])

    def test_variant_barcode_is_reachable_for_cashier(self):
        """Сканер на кассе должен найти именно эту вариацию."""
        red_s = self.variant_by_name("Красный / S")
        self.db.upsert("nom_variants", {"id": red_s["id"], "barcode": "2000000000017"})
        self.put_stock(red_s["id"], 1)
        self.shelf.transfer_from_stock("nom1", self.warehouse, 1,
                                       variant_id=red_s["id"])
        found = self.shelf.cashier_lookup("2000000000017")
        self.assertIsNotNone(found, "штрихкод вариации не нашёлся на стеллаже")
        self.assertEqual(red_s["id"], found["variant_id"])

    def test_shelf_stock_of_variant_is_separate_from_zone(self):
        """Витрина ведёт остаток по вариации, а не «по товару вообще»."""
        red_l = self.variant_by_name("Красный / L")
        blue_l = self.variant_by_name("Синий / L")
        self.put_stock(red_l["id"], 2)
        self.put_stock(blue_l["id"], 2)
        self.shelf.transfer_from_stock("nom1", self.warehouse, 2,
                                       variant_id=red_l["id"])
        zone = self.stock.shelf_warehouse()
        self.assertEqual(2.0, self.stock.qty("nom1", zone, red_l["id"]))
        self.assertEqual(0.0, self.stock.qty("nom1", zone, blue_l["id"]))

    def test_foreign_variant_is_refused(self):
        other = self.variant_by_name("Красный / L")
        self.db.upsert("nomenclature", {"id": "nom2", "name": "Подставка",
                                        "unit": "шт", "kind": "product"})
        self.put_stock(other["id"], 2)
        with self.assertRaises(ValueError) as ctx:
            self.shelf.transfer_from_stock("nom2", self.warehouse, 1,
                                           variant_id=other["id"])
        self.assertIn("другому товару", str(ctx.exception))


class StockAvailableVariantTests(ShelfVariantCase):
    def test_stock_list_splits_variants(self):
        red_l = self.variant_by_name("Красный / L")
        blue_l = self.variant_by_name("Синий / L")
        self.put_stock(red_l["id"], 3)
        self.put_stock(blue_l["id"], 1)
        rows = self.shelf.stock_available(goods_only=True)
        labels = {(r["nom_id"], r.get("variant_label") or ""): r["qty"] for r in rows}
        self.assertEqual(3.0, labels[("nom1", "Красный · L")])
        self.assertEqual(1.0, labels[("nom1", "Синий · L")])

    def test_stock_list_keeps_product_row_without_variants(self):
        """Товар без вариаций остаётся одной строкой, как и был."""
        self.db.upsert("nomenclature", {"id": "nom3", "name": "Подставка",
                                        "unit": "шт", "kind": "product"})
        self.stock.add_move("nom3", self.warehouse, 4, 400.0, doc_kind="produce")
        rows = [r for r in self.shelf.stock_available() if r["nom_id"] == "nom3"]
        self.assertEqual(1, len(rows))
        self.assertEqual("", rows[0]["variant_label"])


class ShelfVariantSaveTests(ShelfVariantCase):
    def test_save_stores_variant(self):
        red_s = self.variant_by_name("Красный / S")
        item = self.shelf.save_item({"name": "Адресник", "nom_id": "nom1",
                                     "variant_id": red_s["id"], "price": 500})
        self.assertEqual(red_s["id"], item["variant_id"])
        self.assertEqual("Красный · S", item["variant_label"])

    def test_unknown_variant_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self.shelf.save_item({"name": "Адресник", "nom_id": "nom1",
                                  "variant_id": "var-нет"})
        self.assertIn("Вариация не найдена", str(ctx.exception))

    def test_variant_of_another_product_is_refused(self):
        red_s = self.variant_by_name("Красный / S")
        self.db.upsert("nomenclature", {"id": "nom2", "name": "Подставка",
                                        "unit": "шт", "kind": "product"})
        with self.assertRaises(ValueError) as ctx:
            self.shelf.save_item({"name": "Подставка", "nom_id": "nom2",
                                  "variant_id": red_s["id"]})
        self.assertIn("другому товару", str(ctx.exception))

    def test_item_without_variant_keeps_old_behaviour(self):
        """Старые карточки без вариации не ломаются."""
        self.stock.add_move("nom1", self.warehouse, 2, 200.0, doc_kind="produce")
        self.shelf.transfer_from_stock("nom1", self.warehouse, 1)
        items = self.shelf.items()
        self.assertEqual(1, len(items))
        self.assertEqual("", items[0]["variant_id"])
        self.assertEqual("", items[0]["variant_label"])


class CashierVariantTests(ShelfVariantCase):
    """Касса: вариации — разные строки витрины, а не «три одинаковых товара»."""

    def setUp(self):
        super().setUp()
        self.cashier = Cashier(self.db, Accounting(self.db))
        self.red_l = self.variant_by_name("Красный / L")
        self.blue_l = self.variant_by_name("Синий / L")
        self.put_stock(self.red_l["id"], 3)
        self.put_stock(self.blue_l["id"], 2)
        self.shelf.transfer_from_stock("nom1", self.warehouse, 3,
                                       variant_id=self.red_l["id"])
        self.shelf.transfer_from_stock("nom1", self.warehouse, 2,
                                       variant_id=self.blue_l["id"])

    def test_catalog_shows_variant_label(self):
        rows = [r for r in self.cashier.catalog()["items"] if r["name"] == "Адресник"]
        labels = sorted(str(r.get("variant_label") or "") for r in rows)
        self.assertEqual(["Красный · L", "Синий · L"], labels)

    def test_catalog_keeps_variants_apart(self):
        rows = {str(r.get("variant_label") or ""): r
                for r in self.cashier.catalog()["items"] if r["name"] == "Адресник"}
        self.assertEqual(3.0, rows["Красный · L"]["qty"])
        self.assertEqual(2.0, rows["Синий · L"]["qty"])

    def test_hold_of_one_variant_does_not_touch_another(self):
        """Резерв под заказ на красный L не съедает доступность синего M."""
        self.stock.reserve("nom1", 2, order_id="order-1",
                           warehouse_id=self.stock.shelf_warehouse(),
                           variant_id=self.red_l["id"])
        rows = {str(r.get("variant_label") or ""): r
                for r in self.cashier.catalog()["items"] if r["name"] == "Адресник"}
        self.assertEqual(1.0, rows["Красный · L"]["qty"])
        self.assertEqual(2.0, rows["Синий · L"]["qty"])


if __name__ == "__main__":
    unittest.main()

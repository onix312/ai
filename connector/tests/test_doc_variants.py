"""Складские документы по вариациям: приход, продажа, списание, инвентаризация.

Вариации товара отличаются остатком, себестоимостью и ценой, но документы
склада долго двигали товар целиком: строка помнила `variant_id` только в базе,
а проверки и себестоимость считались по товару. Из этого росли три беды —
продажа пяти красных L проходила при остатке «по товару вообще», списание
съедало средний расход, а инвентаризация сравнивала факт с суммой по всем
цветам и писала недостачу там, где всё на месте.

Здесь под контрактом именно это: остаток, себестоимость и проверка нехватки
считаются по вариации, а в тексте ошибок видно, о каком цвете речь.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import num  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.documents import Documents  # noqa: E402
from connector.printflow.nomenclature import Nomenclature  # noqa: E402
from connector.printflow.stock import Stock  # noqa: E402


class DocVariantCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(pathlib.Path(self.tmp.name) / "docs.sqlite3")
        self.addCleanup(self.db.close)
        self.nom = Nomenclature(self.db)
        self.stock = Stock(self.db)
        self.docs = Documents(self.db)
        self.a = "shop"
        self.b = "home"
        self.db.upsert("warehouses", {"id": self.a, "name": "Магазин", "kind": "retail",
                                      "retail": 1, "position": 1})
        self.db.upsert("warehouses", {"id": self.b, "name": "Дом", "kind": "home",
                                      "position": 2})
        self.nom.save({"id": "nom1", "name": "Адресник", "unit": "шт",
                       "kind": "product", "grams": 12, "hours": 0.4, "archived": 0})
        self.nom.generate_variants(
            "nom1", [{"name": "Цвет", "values": ["Красный", "Синий"]},
                     {"name": "Размер", "values": ["S", "L"]}])
        variants = self.db.query(
            "SELECT * FROM nom_variants WHERE nom_id=? ORDER BY name", ("nom1",))
        self.red_l = self._variant(variants, "Красный / L")
        self.blue_l = self._variant(variants, "Синий / L")

    @staticmethod
    def _variant(rows: list[dict], name: str) -> dict:
        found = [r for r in rows if name in str(r.get("name") or "")]
        assert found, f"нет вариации «{name}»: {[r['name'] for r in rows]}"
        return found[0]

    def put(self, variant_id: str, qty: float, cost: float, warehouse: str = "") -> None:
        self.stock.add_move("nom1", warehouse or self.a, qty, qty * cost,
                            doc_kind="produce", variant_id=variant_id)

    def doc(self, kind: str, items: list[dict], **fields) -> dict:
        return self.docs.save({
            "kind": kind, "warehouse_id": self.a, "number": "",
            "items": items, **fields})

    def post(self, kind: str, items: list[dict], **fields) -> dict:
        doc = self.doc(kind, items, **fields)
        return self.docs.post(doc["id"])

    def qty(self, variant_id: str, warehouse: str = "") -> float:
        return self.stock.qty("nom1", warehouse or self.a, variant_id)


class DocVariantStockTests(DocVariantCase):
    def test_receipt_lands_on_its_own_variant(self):
        self.post("receipt", [{"nom_id": "nom1", "variant_id": self.red_l["id"],
                               "qty": 3, "cost": 100}])
        self.assertEqual(3.0, self.qty(self.red_l["id"]))
        self.assertEqual(0.0, self.qty(self.blue_l["id"]),
                         "приход красного не должен появляться у синего")

    def test_receipt_without_variant_stays_product_wide(self):
        """Строка без вариации — прежнее поведение, движение по товару целиком."""
        self.post("receipt", [{"nom_id": "nom1", "qty": 2, "cost": 50}])
        self.assertEqual(2.0, self.stock.qty("nom1", self.a, ""))
        self.assertEqual(0.0, self.qty(self.red_l["id"]))

    def test_sale_is_checked_against_the_variant(self):
        self.put(self.blue_l["id"], 5, 80.0)
        with self.assertRaises(ValueError) as ctx:
            self.post("sale", [{"nom_id": "nom1", "variant_id": self.red_l["id"],
                                "qty": 1, "price": 450}])
        message = str(ctx.exception)
        self.assertIn("Красный · L", message)
        self.assertIn("0", message)

    def test_sale_moves_only_the_sold_variant(self):
        self.put(self.red_l["id"], 3, 100.0)
        self.put(self.blue_l["id"], 3, 100.0)
        self.post("sale", [{"nom_id": "nom1", "variant_id": self.red_l["id"],
                            "qty": 2, "price": 450}])
        self.assertEqual(1.0, self.qty(self.red_l["id"]))
        self.assertEqual(3.0, self.qty(self.blue_l["id"]))

    def test_sale_cost_uses_the_variant_average(self):
        """Себестоимость — своя: красный из дорогого пластика, синий из дешёвого."""
        self.put(self.red_l["id"], 2, 200.0)
        self.put(self.blue_l["id"], 2, 50.0)
        doc = self.post("sale", [{"nom_id": "nom1", "variant_id": self.red_l["id"],
                                  "qty": 1, "price": 450}])
        move = self.db.one(
            "SELECT cost FROM stock_moves WHERE doc_id=? AND doc_kind='sale'",
            (doc["id"],))
        self.assertEqual(-200.0, num(move["cost"]),
                         "списание по средней цене товара, а не вариации")


class DocVariantMoveTests(DocVariantCase):
    def test_move_counts_the_variant_on_the_source(self):
        self.put(self.red_l["id"], 2, 100.0)
        with self.assertRaises(ValueError) as ctx:
            self.post("move", [{"nom_id": "nom1", "variant_id": self.red_l["id"],
                                "qty": 3, "price": 0}], warehouse_to_id=self.b)
        self.assertIn("Красный · L", str(ctx.exception))

    def test_move_carries_the_variant_between_warehouses(self):
        self.put(self.red_l["id"], 4, 100.0)
        self.post("move", [{"nom_id": "nom1", "variant_id": self.red_l["id"],
                            "qty": 3, "price": 0}], warehouse_to_id=self.b)
        self.assertEqual(1.0, self.qty(self.red_l["id"], self.a))
        self.assertEqual(3.0, self.qty(self.red_l["id"], self.b))
        self.assertEqual(0.0, self.qty(self.blue_l["id"], self.b))


class DocVariantWriteoffTests(DocVariantCase):
    def test_writeoff_respects_the_variant_free_stock(self):
        self.put(self.blue_l["id"], 5, 60.0)
        with self.assertRaises(ValueError) as ctx:
            self.post("writeoff", [{"nom_id": "nom1", "variant_id": self.red_l["id"],
                                    "qty": 1, "cost": 0}], reason="брак")
        self.assertIn("Красный · L", str(ctx.exception))

    def test_writeoff_spends_the_variant(self):
        self.put(self.red_l["id"], 3, 120.0)
        self.post("writeoff", [{"nom_id": "nom1", "variant_id": self.red_l["id"],
                                "qty": 2, "cost": 0}], reason="брак")
        self.assertEqual(1.0, self.qty(self.red_l["id"]))


class DocVariantInventoryTests(DocVariantCase):
    def test_inventory_compares_fact_with_the_variant(self):
        """Красный L: учёт 3, факт 3 — расхождения нет, хотя у товара всего 8."""
        self.put(self.red_l["id"], 3, 100.0)
        self.put(self.blue_l["id"], 5, 100.0)
        before = self.stock.qty("nom1", self.a, "")
        doc = self.post("inventory", [{"nom_id": "nom1", "variant_id": self.red_l["id"],
                                       "qty": 0, "qty_fact": 3}])
        self.assertEqual(0.0, num(self.db.one(
            "SELECT amount FROM documents WHERE id=?", (doc["id"],))["amount"]),
            "факт совпал с учётом вариации — писать нечего")
        self.assertEqual(before, self.stock.qty("nom1", self.a, ""))

    def test_inventory_writes_the_variant_difference(self):
        self.put(self.red_l["id"], 3, 100.0)
        self.put(self.blue_l["id"], 5, 100.0)
        self.post("inventory", [{"nom_id": "nom1", "variant_id": self.red_l["id"],
                                 "qty": 0, "qty_fact": 2}])
        self.assertEqual(2.0, self.qty(self.red_l["id"]))
        self.assertEqual(5.0, self.qty(self.blue_l["id"]))


class DocVariantReturnTests(DocVariantCase):
    def test_return_restores_the_variant_and_its_cost(self):
        self.put(self.red_l["id"], 2, 200.0)
        self.put(self.blue_l["id"], 2, 50.0)
        self.post("return", [{"nom_id": "nom1", "variant_id": self.blue_l["id"],
                              "qty": 1, "price": 300}])
        self.assertEqual(3.0, self.qty(self.blue_l["id"]))
        move = self.db.one(
            "SELECT cost FROM stock_moves WHERE nom_id='nom1' AND variant_id=?"
            " AND doc_kind='return' ORDER BY rowid DESC", (self.blue_l["id"],))
        self.assertEqual(50.0, num(move["cost"]),
                         "возврат вернул себестоимость чужой вариации")


class DocVariantOrderTests(DocVariantCase):
    """Накладная заказа — та же вариация, что выбрал клиент.

    Здесь ломалось тише всего: резерв под заказ ставится по вариации, а
    накладная собиралась по товару целиком, поэтому «свой» резерв не
    засчитывался, и отгрузка падала на «на складе 0 шт».
    """

    def setUp(self):
        super().setUp()
        self.put(self.red_l["id"], 2, 100.0)
        self.put(self.blue_l["id"], 2, 100.0)
        self.db.upsert("orders", {
            "id": "o1", "number": 7, "status": "ready", "warehouse_id": self.a,
            "created_at": "2026-09-15T09:00:00+00:00", "at": "2026-09-15T09:00:00+00:00"})
        self.db.upsert("order_items", {
            "id": "oi1", "order_id": "o1", "position": 1, "nom_id": "nom1",
            "variant_id": self.red_l["id"], "name": "Адресник", "qty": 1,
            "price": 450})

    def test_waybill_spends_only_the_ordered_variant(self):
        self.stock.reserve("nom1", 1, "o1", self.a, "готовый товар заказа",
                           self.red_l["id"])
        doc = self.docs.waybill_from_order("o1", post=True)
        self.assertEqual("posted", doc["state"])
        self.assertEqual(1.0, self.qty(self.red_l["id"]))
        self.assertEqual(2.0, self.qty(self.blue_l["id"]),
                         "продажа красного не должна трогать синий")
        line = self.docs.get(doc["id"])["items"][0]
        self.assertEqual(self.red_l["id"], line["variant_id"])

    def test_waybill_uses_the_variant_own_reserve(self):
        """Свой резерв засчитывается: без него свободного остатка не хватило бы."""
        self.stock.reserve("nom1", 1, "o1", self.a, "готовый товар заказа",
                           self.red_l["id"])
        # Одну штуку уже продали с полки: свободного остатка вариации нет,
        # и без своего резерва отгрузка обязана упасть.
        self.stock.add_move("nom1", self.a, -1, -100.0, "", "sale",
                            self.red_l["id"], note="продали на полке")
        doc = self.docs.waybill_from_order("o1", post=True)
        self.assertEqual("posted", doc["state"])
        self.assertEqual(0.0, self.qty(self.red_l["id"]))


class DocVariantViewTests(DocVariantCase):
    """Панель показывает, о какой вариации речь: «Адресник · Красный · L»."""

    def test_document_lines_carry_the_variant_label(self):
        doc = self.doc("receipt", [{"nom_id": "nom1", "variant_id": self.red_l["id"],
                                    "qty": 3, "cost": 100}])
        line = self.docs.get(doc["id"])["items"][0]
        self.assertEqual(self.red_l["id"], line["variant_id"])
        self.assertEqual("Красный · L", line["variant_label"])

    def test_moves_carry_the_variant_label(self):
        doc = self.doc("receipt", [{"nom_id": "nom1", "variant_id": self.red_l["id"],
                                    "qty": 3, "cost": 100}])
        self.docs.post(doc["id"])
        move = self.docs.get(doc["id"])["moves"][0]
        self.assertEqual(self.red_l["id"], move["variant_id"])
        self.assertEqual("Красный · L", move["variant_label"])

    def test_line_without_variant_has_empty_label(self):
        doc = self.doc("receipt", [{"nom_id": "nom1", "qty": 1, "cost": 10}])
        line = self.docs.get(doc["id"])["items"][0]
        self.assertEqual("", line["variant_label"])

    def test_deleted_variant_is_named_as_such(self):
        """Вариацию удалили, а строка черновика осталась — не молчим об этом."""
        doc = self.doc("receipt", [{"nom_id": "nom1", "variant_id": self.red_l["id"],
                                    "qty": 1, "cost": 10}])
        self.db.execute("DELETE FROM nom_variants WHERE id=?", (self.red_l["id"],))
        line = self.docs.get(doc["id"])["items"][0]
        self.assertEqual("вариация удалена", line["variant_label"])


if __name__ == "__main__":
    unittest.main()

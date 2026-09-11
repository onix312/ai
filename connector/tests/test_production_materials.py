"""Производство списывает материалы с их склада, а не только со склада изделия.

Жалоба владельца: расходники лежат на отдельном складе («Материалы»), а
производство их «не видит» — проведение падало с «не хватает… есть 0», хотя
позиция есть в наличии. Причина: состав спецификации списывался только с того
склада, куда приходуется готовое изделие.

Здесь проверяется новое правило (`consumption.py`):

* место расходника можно указать в составе и оно соблюдается строго;
* без указанного места коннектор сам берёт склад с остатком и запоминает его;
* нехватка — это план для человека, а не тихое списание из другого места;
* осознанное списание в минус возможно, но помечено и посчитано по цене;
* витрина в роли склада-источника запрещена (её остатки ведёт полка).
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.consumption import (  # noqa: E402
    MaterialShortage, plan, remember_places)
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.documents import Documents  # noqa: E402
from connector.printflow.nomenclature import Nomenclature  # noqa: E402
from connector.printflow.stock import Stock  # noqa: E402


class MaterialBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "materials.sqlite3")
        self.stock = Stock(self.db)
        self.docs = Documents(self.db)
        self.nom = Nomenclature(self.db)
        self._seed()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------------ данные
    def _wh(self, wid: str, name: str, kind: str, *, retail: int = 0, position: int = 0):
        return self.db.upsert("warehouses", {
            "id": wid, "name": name, "kind": kind, "retail": retail,
            "archived": 0, "position": position, "address": "", "note": "",
        })

    def _nom(self, nom_id: str, name: str, kind: str = "product", unit: str = "шт"):
        return self.db.upsert("nomenclature", {
            "id": nom_id, "code": nom_id, "name": name, "kind": kind,
            "unit": unit, "archived": 0,
        })

    def _seed(self):
        # Витрина (её ведёт полка), склад изделий, склад расходников.
        self._wh("shelf", "Полка магазина", "shelf", retail=1, position=0)
        self._wh("home", "Домашний склад", "home", position=1)
        self._wh("wh_mat", "Материалы", "material", position=2)
        self._nom("nom_tag", "Адресник", "product")
        self._nom("nom_screw", "Крепёж M3", "material")
        self._nom("nom_box", "Коробка подарочная", "material")

    def receipt(self, nom_id: str, warehouse_id: str, qty: float, price: float) -> str:
        doc = self.docs.save({
            "kind": "receipt", "warehouse_id": warehouse_id,
            "items": [{"nom_id": nom_id, "qty": qty, "price": price}]})
        return self.docs.post(doc["id"])["id"]

    def spec(self, items: list[dict], nom_id: str = "nom_tag") -> dict:
        return self.nom.save_spec({"nom_id": nom_id, "items": items})

    def produce(self, qty: int, warehouse_id: str = "home", *,
                allow_shortage: bool = False) -> tuple[dict, dict]:
        doc = self.docs.save({
            "kind": "production", "warehouse_id": warehouse_id,
            "items": [{"nom_id": "nom_tag", "qty": qty, "price": 0}]})
        posted = self.docs.post(doc["id"], allow_shortage=allow_shortage)
        return doc, posted

    def free(self, nom_id: str, warehouse_id: str) -> float:
        return self.stock.free(nom_id, warehouse_id)


class WarehouseChoiceTests(MaterialBase):
    """Куда смотрит производство, когда место расходника не задано."""

    def test_material_warehouse_is_used_without_any_setting(self):
        """Главный сценарий владельца: расходник на своём складе — списываем оттуда."""
        self.receipt("nom_screw", "wh_mat", 100, 2.5)
        self.spec([{"nom_id": "nom_screw", "qty": 4}])

        self.produce(qty=2)

        self.assertAlmostEqual(self.free("nom_screw", "wh_mat"), 92.0, places=3)
        self.assertAlmostEqual(self.free("nom_screw", "home"), 0.0, places=3)
        self.assertAlmostEqual(self.free("nom_tag", "home"), 2.0, places=3)

    def test_auto_choice_is_remembered_in_the_spec(self):
        """Автовыбор запоминается: в следующий раз место видно в составе."""
        self.receipt("nom_screw", "wh_mat", 10, 2.5)
        self.spec([{"nom_id": "nom_screw", "qty": 1}])

        self.produce(qty=1)

        spec = self.nom.spec_of("nom_tag")
        self.assertEqual(spec["items"][0]["warehouse_id"], "wh_mat")
        self.assertEqual(spec["items"][0]["warehouse_name"], "Материалы")

    def test_explicit_place_beats_stock_nearby(self):
        """Если место указано в составе — берём строго его, даже если рядом есть запас."""
        self.receipt("nom_screw", "home", 50, 2.5)
        self.receipt("nom_screw", "wh_mat", 50, 3.0)
        self.spec([{"nom_id": "nom_screw", "qty": 2, "warehouse_id": "wh_mat"}])

        self.produce(qty=1)

        self.assertAlmostEqual(self.free("nom_screw", "wh_mat"), 48.0, places=3)
        self.assertAlmostEqual(self.free("nom_screw", "home"), 50.0, places=3)

    def test_warehouse_with_the_stock_wins_over_kind_preference(self):
        """Расходник лежит на домашнем складе, «Материалы» пусты — берём откуда есть."""
        self.receipt("nom_box", "home", 5, 40)
        self.spec([{"nom_id": "nom_box", "qty": 1}])

        self.produce(qty=1)

        self.assertAlmostEqual(self.free("nom_box", "home"), 4.0, places=3)

    def test_no_material_warehouses_falls_back_to_item_warehouse(self):
        """Складов материалов нет — прежнее поведение (склад изделия) не ломается."""
        self.db.execute("UPDATE warehouses SET archived=1 WHERE kind IN ('material','home')")
        self.receipt("nom_screw", "home", 10, 2.5)
        self.spec([{"nom_id": "nom_screw", "qty": 1}])

        self.produce(qty=1)

        self.assertAlmostEqual(self.free("nom_screw", "home"), 9.0, places=3)


class ShortageTests(MaterialBase):
    """Нехватка — это план для человека, а не тихое списание из другого склада."""

    def setUp(self):
        super().setUp()
        self.receipt("nom_screw", "wh_mat", 3, 2.5)      # хватит на 1 изделие (норма 4)
        self.spec([{"nom_id": "nom_screw", "qty": 4, "warehouse_id": "wh_mat"}])

    def test_shortage_refuses_to_post_and_carries_the_plan(self):
        with self.assertRaises(MaterialShortage) as ctx:
            self.produce(qty=1)
        plan = ctx.exception.plan
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["short"][0]["nom_id"], "nom_screw")
        self.assertIn("Материалы", str(ctx.exception))
        # ни одного движения: отказ не должен оставить половинчатый учёт
        self.assertAlmostEqual(self.free("nom_screw", "wh_mat"), 3.0, places=3)
        self.assertAlmostEqual(self.free("nom_tag", "home"), 0.0, places=3)

    def test_shortage_names_the_place_from_the_spec(self):
        try:
            self.produce(qty=1)
        except MaterialShortage as exc:
            self.assertIn("указан на этот склад в составе", str(exc))
        else:  # pragma: no cover
            self.fail("нехватка не поднялась")

    def test_allow_shortage_writes_off_into_minus_with_a_note(self):
        _, posted = self.produce(qty=1, allow_shortage=True)
        self.assertAlmostEqual(self.free("nom_screw", "wh_mat"), -1.0, places=3)
        self.assertAlmostEqual(self.free("nom_tag", "home"), 1.0, places=3)
        moves = self.db.query(
            "SELECT * FROM stock_moves WHERE doc_id=? AND nom_id='nom_screw'",
            (posted["id"],))
        self.assertEqual(len(moves), 1)
        note = str(moves[0]["note"])
        self.assertIn("в минус", note)
        self.assertIn("было 3", note)
        # себестоимость не обнуляется: 4 шт по 2.5 ₽
        self.assertAlmostEqual(abs(float(moves[0]["cost"])), 10.0, places=2)

    def test_empty_material_warehouse_plan_says_it_will_go_negative(self):
        """Пустой склад материалов выбран как место — это видно в плане до проведения."""
        self.db.execute("DELETE FROM stock_moves WHERE nom_id='nom_screw'")
        self.db.execute("DELETE FROM spec_items")
        self.spec([{"nom_id": "nom_screw", "qty": 1}])
        dry = plan(self.db, self.stock,
                   [{"nom_id": "nom_screw", "qty": 1, "name": "Крепёж M3"}], "home")
        self.assertFalse(dry["ok"])
        self.assertIn("уйдёт в минус", dry["lines"][0]["reason"])


class ForbiddenPlaceTests(MaterialBase):
    """Витрина — не склад расходников: её остатки ведёт полка."""

    def test_shelf_cannot_be_a_component_source(self):
        self.db.upsert("shelf_items", {
            "id": "s1", "name": "Адресник", "qty": 5, "price": 500,
            "catalog_id": None, "nom_id": "nom_tag", "active": 1})
        self.stock.add_move("nom_screw", "shelf", 10, 25, doc_kind="manual")
        self.spec([{"nom_id": "nom_screw", "qty": 1, "warehouse_id": "shelf"}])

        with self.assertRaises(MaterialShortage) as ctx:
            self.produce(qty=1)
        self.assertIn("витрина", str(ctx.exception).lower())

    def test_archived_place_is_reported_clearly(self):
        self.receipt("nom_screw", "wh_mat", 10, 2.5)
        self.db.execute("UPDATE warehouses SET archived=1 WHERE id='wh_mat'")
        self.spec([{"nom_id": "nom_screw", "qty": 1, "warehouse_id": "wh_mat"}])

        with self.assertRaises(MaterialShortage) as ctx:
            self.produce(qty=1)
        self.assertIn("не найден или удалён", str(ctx.exception))


class CostAndAggregateTests(MaterialBase):
    """Деньги: себестоимость изделия включает материалы с чужого склада."""

    def test_component_cost_from_another_warehouse_lands_in_the_item(self):
        self.receipt("nom_screw", "wh_mat", 100, 2.5)     # 2.5 ₽ за штуку
        self.spec([{"nom_id": "nom_screw", "qty": 4}])

        _, posted = self.produce(qty=2)

        self.assertAlmostEqual(float(posted["cost_total"]), 20.0, places=2)

    def test_two_spec_lines_of_the_same_material_are_one_writeoff(self):
        """Один расходник в двух строках состава — одно движение, ноль дублей."""
        self.receipt("nom_screw", "wh_mat", 100, 1)
        self.spec([{"nom_id": "nom_screw", "qty": 1},
                   {"nom_id": "nom_screw", "qty": 2}])

        _, posted = self.produce(qty=1)

        moves = self.db.query(
            "SELECT * FROM stock_moves WHERE doc_id=? AND nom_id='nom_screw'",
            (posted["id"],))
        self.assertEqual(len(moves), 1)
        self.assertAlmostEqual(float(moves[0]["qty"]), -3.0, places=3)

    def test_remember_places_does_not_touch_explicit_lines(self):
        self.receipt("nom_screw", "home", 10, 1)
        self.spec([{"nom_id": "nom_screw", "qty": 1, "warehouse_id": "home"}])
        spec = self.nom.spec_of("nom_tag")
        self.assertEqual(spec["items"][0]["warehouse_id"], "home")
        saved = remember_places(self.db, {"lines": [
            {"warehouse_id": "wh_mat", "warehouse_kind": "material",
             "explicit": True, "lines": [spec["items"][0]["id"]]}]})
        self.assertEqual(saved, 0)
        self.assertEqual(self.nom.spec_of("nom_tag")["items"][0]["warehouse_id"], "home")


class SpecDuplicationTests(MaterialBase):
    """Правка состава не должна плодить копии спецификации.

    Панель сохраняет состав без `id` (только `nom_id` и строки). До 17.0.14 это
    каждый раз создавало новую спецификацию, а производство берёт первую
    активную — то есть после смены места расходника списание шло по старым
    настройкам и выглядело как «поменял, а ничего не изменилось».
    """

    def test_second_save_updates_the_same_spec(self):
        self.receipt("nom_screw", "wh_mat", 10, 2)
        self.spec([{"nom_id": "nom_screw", "qty": 1}])
        first = self.nom.spec_of("nom_tag")
        second = self.spec([{"nom_id": "nom_screw", "qty": 3, "warehouse_id": "home"}])

        self.assertEqual(second["id"], first["id"])
        active = self.db.query(
            "SELECT id FROM specs WHERE nom_id='nom_tag' AND active=1")
        self.assertEqual(len(active), 1)
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) n FROM spec_items WHERE spec_id=?", (first["id"],))["n"], 1)

    def test_place_change_is_what_production_uses(self):
        self.receipt("nom_screw", "home", 5, 2)
        self.receipt("nom_screw", "wh_mat", 5, 2)
        self.spec([{"nom_id": "nom_screw", "qty": 1, "warehouse_id": "wh_mat"}])
        self.spec([{"nom_id": "nom_screw", "qty": 1, "warehouse_id": "home"}])

        self.produce(qty=1)

        self.assertAlmostEqual(self.free("nom_screw", "home"), 4.0, places=3)
        self.assertAlmostEqual(self.free("nom_screw", "wh_mat"), 5.0, places=3)

    def test_legacy_duplicates_are_deactivated(self):
        """Старые базы с копиями: остаётся один активный состав, и он правленый."""
        self.db.upsert("specs", {"id": "spc_old", "nom_id": "nom_tag",
                                 "name": "Старая копия", "active": 1})
        self.db.upsert("spec_items", {"id": "spi_old", "spec_id": "spc_old",
                                      "nom_id": "nom_screw", "qty": 99, "line": 1})

        saved = self.spec([{"nom_id": "nom_screw", "qty": 2}])

        self.assertEqual(saved["id"], "spc_old")     # обновляем самый первый состав
        self.assertEqual(len(self.db.query(
            "SELECT id FROM specs WHERE nom_id='nom_tag' AND active=1")), 1)
        self.assertEqual(saved["items"][0]["qty"], 2.0)


class ApiContractTests(MaterialBase):
    """Маршруты, которыми пользуется панель: план «до» и разбор нехватки."""

    def test_production_plan_route_returns_the_same_plan_as_posting(self):
        from connector.printflow.api import Api
        api = Api.__new__(Api)          # только сервисы, без сети и потоков
        api.db, api.stock, api.docs = self.db, self.stock, self.docs
        api.nom = self.nom
        api.settings = {}
        self.receipt("nom_screw", "wh_mat", 10, 2.5)
        self.spec([{"nom_id": "nom_screw", "qty": 2}])

        code, body = api.post("/api/production/plan",
                              {"items": [{"nom_id": "nom_tag", "qty": 3}],
                               "warehouse_id": "home"}, {})
        self.assertEqual(code, 200)
        line = body["plan"]["lines"][0]
        self.assertEqual(line["warehouse_id"], "wh_mat")
        self.assertAlmostEqual(line["need"], 6.0, places=3)
        self.assertTrue(body["plan"]["ok"])

    def test_spec_keeps_the_place_and_reports_free_stock(self):
        self.receipt("nom_screw", "wh_mat", 7, 2.5)
        saved = self.spec([{"nom_id": "nom_screw", "qty": 1, "warehouse_id": "wh_mat"}])
        item = saved["items"][0]
        self.assertEqual(item["warehouse_id"], "wh_mat")
        self.assertEqual(item["warehouse_name"], "Материалы")
        places = {row["warehouse_id"]: row["free"] for row in item["places"]}
        self.assertAlmostEqual(places.get("wh_mat", 0.0), 7.0, places=3)


if __name__ == "__main__":
    unittest.main()

"""Тесты мультизаказа: состав заказа (разные товары на одной плите).

Цена = сумма позиций (автоматически из базы товаров), себестоимость плиты
делится по доле граммов, вес плиты — фактический с принтера.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting, num  # noqa: E402
from connector.printflow.api import Api  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.repo import Repo, uid  # noqa: E402
from connector.printflow.stock import Stock  # noqa: E402


class MultiOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "t.sqlite3")
        self.repo = Repo(self.db)
        self.acc = Accounting(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _nom(self, name, grams, hours=1.0, price=0.0):
        row = self.db.upsert("nomenclature", {
            "id": uid("nom"), "name": name, "kind": "product",
            "grams": grams, "hours": hours, "created_at": "2026-01-01T00:00:00+00:00"})
        if price:
            self.db.execute(
                "INSERT INTO prices(id,nom_id,price_type_id,price,at) VALUES(?,?,?,?,?)",
                (uid("pr"), row["id"], "retail", price, "2026-01-01T00:00:00+00:00"))
        return row

    def _order(self, items):
        return self.repo.save_order({"items": items, "status": "new"})

    def test_price_is_sum_of_items(self):
        a = self._nom("Адресник", 20, 1.0, 300)
        b = self._nom("Крючок", 5, 0.2, 50)
        order = self._order([
            {"nom_id": a["id"], "qty": 3},
            {"nom_id": b["id"], "qty": 4},
        ])
        # цена = 300×3 + 50×4 = 1100, количество = 7
        self.assertAlmostEqual(order["price"], 1100.0)
        self.assertAlmostEqual(order["qty"], 7)
        # название заказа собрано из позиций
        self.assertIn("Адресник ×3", order["product"])
        self.assertIn("Крючок ×4", order["product"])
        # состав сохранён и виден в детализации
        detail = self.repo.order(order["id"])
        self.assertEqual(len(detail["items"]), 2)
        names = {i["name"] for i in detail["items"]}
        self.assertEqual(names, {"Адресник", "Крючок"})
        # база товаров подставила цену и граммы позиции
        by_name = {i["name"]: i for i in detail["items"]}
        self.assertAlmostEqual(by_name["Адресник"]["price"], 300.0)
        self.assertAlmostEqual(by_name["Адресник"]["grams"], 20.0)

    def test_manual_price_and_grams_override_base(self):
        a = self._nom("Адресник", 20, 1.0, 300)
        order = self._order([
            {"nom_id": a["id"], "name": "Адресник большой",
             "qty": 2, "price": 450, "grams": 25},
        ])
        self.assertAlmostEqual(order["price"], 900.0)
        detail = self.repo.order(order["id"])
        self.assertAlmostEqual(detail["items"][0]["price"], 450.0)
        self.assertAlmostEqual(detail["items"][0]["grams"], 25.0)

    def test_cost_split_by_grams_share(self):
        a = self._nom("Адресник", 20, 1.0, 300)
        b = self._nom("Крючок", 5, 0.2, 50)
        order = self._order([
            {"nom_id": a["id"], "qty": 1},
            {"nom_id": b["id"], "qty": 4},
        ])
        # плита: 213 г из слайсера → фактический вес с принтера
        self.db.execute(
            "UPDATE orders SET actual_grams=213, actual_hours=3, actual_cost=500"
            " WHERE id=?", (order["id"],))
        detail = self.repo.order(order["id"])
        econ = detail["items_economics"]
        self.assertEqual(len(econ), 2)
        by_name = {i["name"]: i for i in econ}
        # веса: 20 г против 5×4=20 г — доли равны
        self.assertAlmostEqual(by_name["Адресник"]["share"], 0.5, places=3)
        self.assertAlmostEqual(by_name["Крючок"]["share"], 0.5, places=3)
        self.assertAlmostEqual(by_name["Адресник"]["cost"], 250.0)
        self.assertAlmostEqual(by_name["Крючок"]["cost"], 250.0)
        # прибыль по позиции: цена − доля себестоимости
        self.assertAlmostEqual(by_name["Адресник"]["profit"], 300 - 250, places=2)
        self.assertAlmostEqual(by_name["Крючок"]["profit"], 4 * 50 - 250, places=2)

    def test_plate_grams_not_multiplied_by_qty(self):
        # У мультизаказа grams — вся плита; себестоимость не должна
        # умножаться на количество единиц (7).
        a = self._nom("Адресник", 20, 1.0, 300)
        b = self._nom("Крючок", 5, 0.2, 50)
        order = self._order([
            {"nom_id": a["id"], "qty": 3},
            {"nom_id": b["id"], "qty": 4},
        ])
        self.db.execute("UPDATE orders SET grams=213, hours=3 WHERE id=?", (order["id"],))
        detail = self.repo.order(order["id"])
        eco = detail["economics"]
        # без правки экономика считала бы cost_breakdown(213×7, 3×7) — в разы больше
        single = self.acc.cost_breakdown(213 * 7, 3 * 7)["total"]
        self.assertGreater(single, num(eco["cost"]))
        self.assertAlmostEqual(num(eco["grams"]), 213.0)

    def test_update_items_replaces_and_recomputes(self):
        a = self._nom("Адресник", 20, 1.0, 300)
        b = self._nom("Крючок", 5, 0.2, 50)
        order = self._order([{"nom_id": a["id"], "qty": 2}])
        self.assertAlmostEqual(order["price"], 600.0)
        updated = self.repo.save_order({"id": order["id"], "items": [{"nom_id": b["id"], "qty": 1}]})
        self.assertAlmostEqual(updated["price"], 50.0)
        self.assertAlmostEqual(updated["qty"], 1)
        detail = self.repo.order(order["id"])
        self.assertEqual(len(detail["items"]), 1)
        self.assertEqual(detail["items"][0]["name"], "Крючок")

    def test_duplicate_copies_items(self):
        a = self._nom("Адресник", 20, 1.0, 300)
        order = self._order([{"nom_id": a["id"], "qty": 2}])
        dup = self.repo.duplicate_order(order["id"])
        detail = self.repo.order(dup["id"])
        self.assertEqual(len(detail["items"]), 1)
        self.assertEqual(detail["items"][0]["name"], "Адресник")
        self.assertAlmostEqual(detail["items"][0]["qty"], 2)

    def test_export_import_roundtrip(self):
        a = self._nom("Адресник", 20, 1.0, 300)
        order = self._order([{"nom_id": a["id"], "qty": 3}])
        payload = self.repo.export_all()
        self.assertIn("order_items", payload)
        self.assertEqual(len(payload["order_items"]), 1)
        # импорт в свежую базу
        tmp2 = tempfile.TemporaryDirectory()
        try:
            db2 = Database(pathlib.Path(tmp2.name) / "t2.sqlite3")
            repo2 = Repo(db2)
            repo2.import_backup(payload)
            detail = repo2.order(order["id"])
            self.assertEqual(len(detail["items"]), 1)
            self.assertEqual(detail["items"][0]["name"], "Адресник")
            db2.close()
        finally:
            tmp2.cleanup()

    def test_delete_order_removes_items(self):
        a = self._nom("Адресник", 20, 1.0, 300)
        order = self._order([{"nom_id": a["id"], "qty": 1}])
        self.repo.delete_order(order["id"])
        rows = self.db.query("SELECT * FROM order_items WHERE order_id=?",
                             (order["id"],))
        self.assertEqual(rows, [])

    def test_empty_items_keeps_manual_price(self):
        order = self.repo.save_order({"product": "Услуга", "price": 1500})
        self.assertAlmostEqual(order["price"], 1500.0)
        detail = self.repo.order(order["id"])
        self.assertEqual(detail["items"], [])

    def _api(self):
        """Api-обёртка как в HTTP-слое: именно через неё форма шлёт items.

        Регрессия №40471b98: api.save_order сериализовал items (состав заказа)
        в JSON-строку вместе с spools/colors/qc_done, а repo._save_order_items
        ждёт настоящий список — из-за этого падало «Состав заказа должен быть
        списком позиций» и НИ ОДИН заказ нельзя было сохранить из панели.
        items не хранится в orders, а раскладывается по таблице order_items.
        """
        api = Api.__new__(Api)
        api.db = self.db
        api.repo = self.repo
        api.stock = Stock(self.db)
        api.acc = self.repo.acc
        api.shelf = mock.Mock()
        api.manager = types.SimpleNamespace(printers={}, bot=None)
        api.bus = types.SimpleNamespace(publish=lambda *a, **k: None)
        api.started_at = time.time()
        api.last_host = "test"
        return api

    def test_api_save_order_accepts_items_list(self):
        a = self._nom("Адресник", 20, 1.0, 300)
        b = self._nom("Крючок", 5, 0.2, 50)
        # Тело как у формы карточки заказа: items — это список (JSON-массив).
        body = {
            "customer_name": "Иван", "phone": "777",
            "items": [
                {"nom_id": a["id"], "name": "Адресник", "qty": 3,
                 "price": 300, "grams": 20, "hours": 1.0},
                {"nom_id": b["id"], "name": "Крючок", "qty": 4,
                 "price": 50, "grams": 5, "hours": 0.2},
            ],
            "status": "new",
        }
        res = self._api().save_order(body)
        order = res["order"]
        self.assertTrue(res["ok"])
        self.assertAlmostEqual(order["price"], 1100.0)
        self.assertAlmostEqual(order["qty"], 7)
        detail = self.repo.order(order["id"])
        self.assertEqual(len(detail["items"]), 2)

    def test_api_save_order_accepts_empty_items(self):
        # Простой заказ без мультисостава: items приходит пустым списком.
        res = self._api().save_order({
            "customer_name": "Пётр", "phone": "778",
            "product": "Печать на заказ", "price": 999,
            "qty": 1, "items": [], "status": "new",
        })
        order = res["order"]
        self.assertTrue(res["ok"])
        self.assertAlmostEqual(order["price"], 999.0)


if __name__ == "__main__":
    unittest.main()

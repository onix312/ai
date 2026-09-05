"""Заказ 15.6: фактические катушки в смете заказа и привязка AMS→склад.

1) Себестоимость заказа считается по ценам выбранных катушек склада
   (граммы × цена/вес по каждой строке), а не по справочной цене материала;
   мультицвет с катушками разной цены — точная сумма; нехватка граммов
   видна в ответе (мягкое предупреждение).
2) Катушку из AMS (фантом без цены/бренда) можно привязать к уже
   заведённой складской: слот/tray/история переезжают, фантом архивируется,
   остаток и цена — складские.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting
from connector.printflow.db import Database
from connector.printflow.repo import Repo


class OrderSpoolCostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "spools.sqlite3")
        self.acc = Accounting(self.db)
        self.repo = Repo(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _spool(self, sid, price, total=1000, remaining=None, **extra):
        self.db.upsert("spools", {
            "id": sid, "material": "PLA", "color_name": "чёрный",
            "price": price, "total_grams": total,
            "remaining_grams": total if remaining is None else remaining,
            "archived": 0, "verified": 1, **extra})

    def test_spools_filament_cost_actual_prices(self):
        # дешёвый PLA 1000 ₽/кг и дорогой TPU 4000 ₽/кг
        self._spool("sp-cheap", 1000.0)
        self._spool("sp-dear", 4000.0)
        info = self.acc.spools_filament_cost([
            {"spool_id": "sp-cheap", "grams": 100},   # 100 ₽
            {"spool_id": "sp-dear", "grams": 50},     # 200 ₽
        ])
        self.assertEqual(info["cost"], 300.0)
        self.assertEqual(info["grams"], 150.0)
        self.assertEqual(info["shortage"], 0.0)
        self.assertEqual(len(info["rows"]), 2)

    def test_shortage_detected(self):
        self._spool("sp-tight", 1000.0, total=1000, remaining=20)
        info = self.acc.spools_filament_cost([
            {"spool_id": "sp-tight", "grams": 50}])
        self.assertEqual(info["shortage"], 30.0)  # нужно 50, есть 20
        self.assertEqual(info["have"], 20.0)

    def test_missing_spool_skipped(self):
        info = self.acc.spools_filament_cost([
            {"spool_id": "nope", "grams": 50},
            {"spool_id": "nope2", "grams": 0}])
        self.assertEqual(info["cost"], 0.0)
        self.assertEqual(info["grams"], 0.0)

    def test_cost_breakdown_uses_filament_cost(self):
        # 100 г по фактической цене = 100 ₽ вместо справочной
        self._spool("sp-1", 1000.0)
        br = self.acc.cost_breakdown(
            100, 1.0, manual_minutes=0, qty=1,
            filament_cost=100.0)
        self.assertEqual(br["filament"], 100.0)
        # без filament_cost справочная цена материала (PLA по умолчанию)
        br2 = self.acc.cost_breakdown(100, 1.0, manual_minutes=0, qty=1)
        self.assertGreater(br2["filament"], 0.0)

    def test_order_economics_uses_spools(self):
        self._spool("sp-1", 1000.0)
        order = {
            "price": 1500, "qty": 1, "grams": 100, "hours": 1,
            "spools": json.dumps([{"spool_id": "sp-1", "grams": 100}]),
        }
        eco = self.acc.order_economics(order)
        # пластик 100 ₽ + прочие затраты, себестоимость выше нуля и ниже цены
        self.assertGreater(eco["cost"], 100.0)
        self.assertLess(eco["cost"], 1500.0)
        self.assertGreater(eco["profit"], 0.0)

    def test_order_economics_spools_as_list(self):
        # справа с фронта приходит список, не строка
        self._spool("sp-1", 1000.0)
        order = {"price": 1500, "qty": 1, "grams": 100, "hours": 1,
                 "spools": [{"spool_id": "sp-1", "grams": 100}]}
        eco = self.acc.order_economics(order)
        self.assertGreater(eco["cost"], 100.0)


class LinkAmsSpoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "link.sqlite3")
        self.repo = Repo(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _phantom(self):
        self.db.upsert("spools", {
            "id": "sp-phantom", "material": "PLA", "color_name": "",
            "price": 0, "total_grams": 1000, "remaining_grams": 800,
            "printer_id": "prt-1", "ams_slot": "2", "tray_uuid": "uuid-abc",
            "location": "ams", "ams_sync": 1, "verified": 0,
            "archived": 0, "brand": ""})

    def _warehouse(self):
        self.db.upsert("spools", {
            "id": "sp-real", "material": "PLA", "color_name": "белый",
            "brand": "ESUN", "price": 1200, "total_grams": 1000,
            "remaining_grams": 950, "printer_id": "", "ams_slot": "",
            "tray_uuid": "", "location": "shop", "ams_sync": 0,
            "verified": 1, "archived": 0})

    def test_link_moves_binding_and_archives_phantom(self):
        self._phantom()
        self._warehouse()
        res = self.repo.link_ams_spool("sp-phantom", "sp-real")
        self.assertTrue(res["ok"])
        target = self.db.one("SELECT * FROM spools WHERE id='sp-real'")
        self.assertEqual(target["printer_id"], "prt-1")
        self.assertEqual(target["ams_slot"], "2")
        self.assertEqual(target["tray_uuid"], "uuid-abc")
        self.assertEqual(target["location"], "ams")
        self.assertEqual(target["ams_sync"], 1)
        # остаток и цена — складские
        self.assertEqual(target["remaining_grams"], 950)
        self.assertEqual(target["price"], 1200)
        phantom = self.db.one("SELECT * FROM spools WHERE id='sp-phantom'")
        self.assertEqual(phantom["archived"], 1)
        self.assertEqual(phantom["ams_slot"], "")

    def test_link_moves_usage_history(self):
        self._phantom()
        self._warehouse()
        self.db.execute(
            "INSERT INTO filament_usage(at,spool_id,job_id,order_id,grams,cost,note,auto)"
            " VALUES(?,?,?,?,?,?,?,?)",
            ("2026-09-01T10:00", "sp-phantom", "job-1", "ord-1", 120, 144, "тест", 1))
        res = self.repo.link_ams_spool("sp-phantom", "sp-real")
        self.assertEqual(res["moved_usage"], 1)
        rows = self.db.query("SELECT spool_id FROM filament_usage")
        self.assertTrue(all(r["spool_id"] == "sp-real" for r in rows))

    def test_link_updates_order_spools_json(self):
        self._phantom()
        self._warehouse()
        self.db.execute(
            "INSERT INTO orders(id,number,product,spools) VALUES(?,?,?,?)",
            ("ord-1", "1001", "заказ",
             json.dumps([{"spool_id": "sp-phantom", "grams": 80}])))
        self.repo.link_ams_spool("sp-phantom", "sp-real")
        raw = self.db.one("SELECT spools FROM orders WHERE id='ord-1'")["spools"]
        self.assertEqual(json.loads(raw)[0]["spool_id"], "sp-real")

    def test_link_same_spool_rejected(self):
        self._phantom()
        with self.assertRaises(ValueError):
            self.repo.link_ams_spool("sp-phantom", "sp-phantom")

    def test_link_missing_target_rejected(self):
        self._phantom()
        with self.assertRaises(ValueError):
            self.repo.link_ams_spool("sp-phantom", "nope")

    def test_link_blocked_when_slot_occupied(self):
        self._phantom()
        self._warehouse()
        # третья активная катушка уже на том же слоте того же принтера
        self.db.upsert("spools", {
            "id": "sp-other", "material": "PETG", "color_name": "",
            "price": 0, "total_grams": 1000, "remaining_grams": 500,
            "printer_id": "prt-1", "ams_slot": "2", "tray_uuid": "",
            "location": "ams", "ams_sync": 0, "verified": 1, "archived": 0})
        with self.assertRaises(ValueError):
            self.repo.link_ams_spool("sp-phantom", "sp-real")


if __name__ == "__main__":
    unittest.main()

"""Себестоимость нового товара и сохранение катушки заказа без граммов."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import num  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.nomenclature import Nomenclature  # noqa: E402
from connector.printflow.repo import Repo, uid  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "t.sqlite3")
        self.nom = Nomenclature(self.db)
        self.repo = Repo(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()


class NomCostTests(Base):
    """Новый товар считает и сохраняет нормативную себестоимость."""

    def test_save_persists_norm_cost(self):
        item = self.nom.save({
            "name": "Адресник",
            "grams": 30,
            "hours": 1.5,
            "fit_per_plate": 4,
            "material": "PLA",
        })
        self.assertGreater(num(item["cost"]), 0)
        row = self.db.one("SELECT cost FROM nomenclature WHERE id=?", (item["id"],))
        self.assertGreater(num(row["cost"]), 0)
        self.assertAlmostEqual(num(item["cost"]), num(row["cost"]), places=2)

    def test_material_affects_cost(self):
        pla = self.nom.save({
            "name": "PLA-изделие", "grams": 100, "hours": 2, "material": "PLA",
        })
        tpu = self.nom.save({
            "name": "TPU-изделие", "grams": 100, "hours": 2, "material": "TPU",
        })
        self.assertGreater(num(pla["cost"]), 0)
        self.assertGreater(num(tpu["cost"]), 0)
        self.assertNotAlmostEqual(num(pla["cost"]), num(tpu["cost"]), places=1)
        self.assertGreater(num(tpu["cost"]), num(pla["cost"]))

    def test_edit_recalculates_until_batch_fact(self):
        item = self.nom.save({
            "name": "Крючок", "grams": 10, "hours": 0.5, "material": "PLA",
        })
        first = num(item["cost"])
        again = self.nom.save({
            "id": item["id"], "name": "Крючок",
            "grams": 80, "hours": 2, "material": "PLA",
        })
        self.assertGreater(num(again["cost"]), first)

    def test_batch_cost_not_overwritten(self):
        item = self.nom.save({
            "name": "С фактом", "grams": 20, "hours": 1, "material": "PLA",
        })
        self.db.upsert("batches", {
            "id": uid("bt"), "nom_id": item["id"], "name": "партия",
            "qty_planned": 4, "qty_done": 4, "qty_scrap": 0,
            "cost": 400, "state": "done",
            "at": "2026-01-01T00:00:00+00:00",
        })
        self.db.execute("UPDATE nomenclature SET cost=? WHERE id=?", (99.0, item["id"]))
        again = self.nom.save({
            "id": item["id"], "name": "С фактом",
            "grams": 20, "hours": 1, "material": "PLA",
        })
        row = self.db.one("SELECT cost FROM nomenclature WHERE id=?", (item["id"],))
        self.assertAlmostEqual(num(row["cost"]), 99.0, places=2)
        self.assertAlmostEqual(num(again["cost"]), 99.0, places=2)


class OrderSpoolSaveTests(Base):
    """Катушка заказа сохраняется даже без предзаполненных граммов."""

    def test_save_order_keeps_spool_without_grams(self):
        spool = self.db.upsert("spools", {
            "id": uid("sp"), "material": "PLA", "color_name": "Чёрный",
            "brand": "Bambu", "total_grams": 1000, "remaining_grams": 800,
            "price": 1200, "created_at": "2026-01-01T00:00:00+00:00"})
        order = self.repo.save_order({
            "product": "Адресник",
            "grams": 120,
            "spools": json.dumps([{"spool_id": spool["id"], "grams": 0}]),
        })
        row = self.db.one("SELECT spools FROM orders WHERE id=?", (order["id"],))
        parsed = json.loads(row["spools"] or "[]")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["spool_id"], spool["id"])
        self.assertEqual(num(parsed[0].get("grams")), 0)


if __name__ == "__main__":
    unittest.main()

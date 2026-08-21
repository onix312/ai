"""Регрессии для safety-gate, единого журнала и optimistic locking."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting, num  # noqa: E402
from connector.printflow.ams_sync import sync_ams_spools  # noqa: E402
from connector.printflow.config import DEFAULT_SETTINGS  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.nomenclature import Nomenclature  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402


class SafetyVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "test.sqlite3")
        self.repo = Repo(self.db)
        self.acc = Accounting(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_dangerous_automation_is_safe_by_default(self):
        self.assertFalse(DEFAULT_SETTINGS["unattended_dangerous_actions"])
        self.assertFalse(DEFAULT_SETTINGS["auto_resume_paused"])
        self.assertFalse(DEFAULT_SETTINGS["auto_queue"])

    def test_payment_journal_supports_prepay_payment_refund_and_retry(self):
        order = self.repo.save_order({"product": "Деталь", "price": 1000, "status": "new"})
        first = self.acc.add_payment(order["id"], 600, "prepay", method="cash", request_id="req-1")
        retry = self.acc.add_payment(order["id"], 600, "prepay", method="cash", request_id="req-1")
        refund = self.acc.add_payment(order["id"], 100, "refund", method="cash", request_id="req-2")
        self.assertFalse(first["already_recorded"])
        self.assertTrue(retry["already_recorded"])
        self.assertEqual(refund["kind"], "refund")
        self.assertEqual(num(self.db.one("SELECT paid FROM orders WHERE id=?", (order["id"],))["paid"]), 500)
        with self.assertRaises(ValueError):
            self.acc.add_payment(order["id"], 501, "refund", method="cash", request_id="req-3")

    def test_payment_rejects_stale_order_snapshot(self):
        order = self.repo.save_order({"product": "Деталь", "price": 1000, "status": "new"})
        version = order["updated_at"]
        self.acc.add_payment(order["id"], 100, method="cash", request_id="fresh", expected_updated_at=version)
        with self.assertRaises(ValueError):
            self.acc.add_payment(order["id"], 100, method="cash", request_id="stale", expected_updated_at=version)

    def test_order_items_are_derived_unless_explicit_override(self):
        nom = self.db.upsert("nomenclature", {
            "id": "nom-derived", "name": "Крепление", "grams": 20, "hours": 0.5,
            "created_at": "now", "updated_at": "now",
        })
        self.db.upsert("prices", {
            "id": "price-derived", "nom_id": nom["id"], "price_type_id": "retail",
            "price": 250, "at": "now",
        })
        derived = self.repo.save_order({
            "product": "старое", "price": 999, "qty": 99,
            "items": [{"nom_id": nom["id"], "qty": 2}],
        })
        self.assertEqual(num(derived["price"]), 500)
        self.assertEqual(num(derived["qty"]), 2)
        self.assertEqual(num(derived["grams"]), 40)
        self.assertAlmostEqual(num(derived["hours"]), 1.0)
        override = self.repo.save_order({
            "product": "ручной итог", "price": 777, "qty": 3, "grams": 1, "hours": 2,
            "items_override": 1, "items": [{"nom_id": nom["id"], "qty": 2}],
        })
        self.assertEqual(num(override["price"]), 777)
        self.assertEqual(num(override["qty"]), 3)

    def test_recalc_price_changes_only_selected_product(self):
        nomenclature = Nomenclature(self.db)
        first = nomenclature.save({"name": "Первый товар", "grams": 20, "hours": 0.5, "material": "PLA"})
        second = nomenclature.save({"name": "Второй товар", "grams": 20, "hours": 0.5, "material": "PLA"})
        result = nomenclature.recalc_price(first["id"])
        self.assertGreaterEqual(result["changed"], 1)
        self.assertTrue(self.db.query("SELECT * FROM prices WHERE nom_id=?", (first["id"],)))
        self.assertFalse(self.db.query("SELECT * FROM prices WHERE nom_id=?", (second["id"],)))
        self.assertTrue(any(h["note"] == "автопересчёт одной позиции" for h in self.db.query(
            "SELECT * FROM prices WHERE nom_id=?", (first["id"],))))

    def test_versions_protect_nomenclature_and_spools(self):
        nom = Nomenclature(self.db).save({"name": "Изделие", "grams": 10})
        with self.assertRaises(ValueError):
            Nomenclature(self.db).save({"id": nom["id"], "name": "Старое", "expected_updated_at": "stale"})
        spool = self.repo.save_spool({"material": "PLA", "total_grams": 1000})
        with self.assertRaises(ValueError):
            self.repo.save_spool({"id": spool["id"], "material": "PETG", "expected_updated_at": "stale"})

    def test_legacy_catalog_has_one_canonical_nomenclature(self):
        old = self.repo.save_catalog_item({"name": "Старая модель", "grams": 12, "hours": 1, "price": 300})
        nom_id = old["nom_id"]
        self.assertTrue(nom_id)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM nomenclature WHERE legacy_catalog_id=?", (old["id"],))["n"], 1)
        self.repo.save_catalog_item({"id": old["id"], "name": "Старая модель 2", "grams": 20, "hours": 2, "price": 350})
        self.assertEqual(self.db.one("SELECT id FROM nomenclature WHERE id=?", (nom_id,))["id"], nom_id)
        self.assertEqual(self.db.one("SELECT name FROM nomenclature WHERE id=?", (nom_id,))["name"], "Старая модель 2")
        Nomenclature(self.db).save({"id": nom_id, "name": "Каноническое имя", "expected_updated_at": self.db.one("SELECT updated_at FROM nomenclature WHERE id=?", (nom_id,))["updated_at"]})
        self.assertEqual(self.db.one("SELECT name FROM catalog WHERE id=?", (old["id"],))["name"], "Каноническое имя")

    def test_ams_import_is_unverified_and_not_auto_consumed(self):
        result = sync_ams_spools(self.db, "printer-1", {"ams": {"trays": [{
            "uuid": "rfid-1", "slot": 0, "type": "PETG", "color": "FF0000", "remain": 80,
        }]}})
        self.assertEqual(result["created"], 1)
        spool = self.db.one("SELECT * FROM spools WHERE tray_uuid='rfid-1'")
        self.assertEqual(num(spool["verified"]), 0)
        self.assertEqual(num(spool["price"]), 0)
        blocked = self.acc.consume_filament(10, spool_id=spool["id"], auto=True)
        self.assertFalse(blocked["ok"])


if __name__ == "__main__":
    unittest.main()

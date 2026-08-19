"""Автосбор данных с принтера и AMS в базу (ams_sync).

Проверяем главный контракт: принтер сам заполняет склад и карточку
принтера, но никогда не затирает то, что пользователь ввёл руками.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.ams_sync import sync_ams_spools, sync_printer_info  # noqa: E402
from connector.printflow.db import Database  # noqa: E402


def snap(trays=None, firmware="01.08.02.00", wifi="-52dBm", humidity="4"):
    return {
        "printer": {"firmware": firmware, "wifi": wifi},
        "ams": {"humidity": humidity, "trays": trays or []},
    }


def tray(slot=0, uuid="A1B2C3", mat="PLA", remain=75, color="#00ff00"):
    return {"slot": slot, "uuid": uuid, "type": mat, "remain": remain,
            "color": color, "label": f"AMS 1 · слот {slot + 1}"}


class AmsSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "test.sqlite3")
        self.db.upsert("printers", {"id": "prn1", "name": "P1S"})

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def spool(self, spool_id="sp1", **extra):
        data = {"id": spool_id, "material": "PLA", "brand": "Bambu",
                "color_name": "Зелёный", "total_grams": 1000,
                "remaining_grams": 500, "price": 1900, "printer_id": "prn1",
                "ams_slot": "0", "tray_uuid": "A1B2C3", "archived": 0}
        data.update(extra)
        return self.db.upsert("spools", data)

    # ------------------------------------------------- карточка принтера
    def test_printer_info_written(self):
        self.assertTrue(sync_printer_info(self.db, "prn1", snap()))
        row = self.db.one("SELECT * FROM printers WHERE id='prn1'")
        self.assertEqual(row["firmware"], "01.08.02.00")
        self.assertEqual(row["wifi"], "-52dBm")
        self.assertEqual(row["ams_humidity"], "4")
        self.assertTrue(row["last_seen"])

    def test_printer_info_respects_setting(self):
        self.db.set_settings({"printer_info_sync": False})
        self.assertFalse(sync_printer_info(self.db, "prn1", snap()))

    # ------------------------------------------------- автосоздание катушек
    def test_unknown_tray_creates_spool(self):
        result = sync_ams_spools(self.db, "prn1", snap([tray()]))
        self.assertEqual(result["created"], 1)
        row = self.db.one("SELECT * FROM spools WHERE tray_uuid='A1B2C3'")
        self.assertEqual(row["material"], "PLA")
        self.assertEqual(row["remaining_grams"], 750.0)  # 75% от 1000 г
        self.assertEqual(row["ams_slot"], "0")
        self.assertEqual(row["printer_id"], "prn1")
        self.assertEqual(row["ams_sync"], 1)

    def test_auto_create_can_be_disabled(self):
        self.db.set_settings({"ams_auto_spools": False})
        result = sync_ams_spools(self.db, "prn1", snap([tray()]))
        self.assertEqual(result["created"], 0)
        self.assertIsNone(self.db.one("SELECT * FROM spools WHERE tray_uuid='A1B2C3'"))

    def test_empty_slot_ignored(self):
        empty = {"slot": 1, "uuid": "0" * 32, "type": "", "remain": None, "color": ""}
        result = sync_ams_spools(self.db, "prn1", snap([empty]))
        self.assertEqual(result, {"created": 0, "updated": 0, "unbound": 0})

    # ------------------------------------------------- обновление известных
    def test_known_spool_updates_remaining_only(self):
        self.spool()
        result = sync_ams_spools(self.db, "prn1", snap([tray(remain=42)]))
        self.assertEqual(result["updated"], 1)
        row = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual(row["remaining_grams"], 420.0)
        # ручные поля не тронуты
        self.assertEqual(row["brand"], "Bambu")
        self.assertEqual(row["color_name"], "Зелёный")
        self.assertEqual(row["price"], 1900)
        self.assertTrue(row["synced_at"])

    def test_manual_spool_never_touched(self):
        self.spool(ams_sync=0, remaining_grams=500)
        result = sync_ams_spools(self.db, "prn1", snap([tray(remain=10)]))
        self.assertEqual(result["updated"], 0)
        row = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual(row["remaining_grams"], 500)  # осталось как ввёл юзер

    def test_remaining_sync_can_be_disabled(self):
        self.db.set_settings({"ams_sync_remaining": False})
        self.spool(remaining_grams=500)
        sync_ams_spools(self.db, "prn1", snap([tray(remain=10)]))
        row = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual(row["remaining_grams"], 500)

    def test_spool_found_by_uuid_rebinds_slot(self):
        self.spool(ams_slot="3")
        sync_ams_spools(self.db, "prn1", snap([tray(slot=1)]))
        row = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual(row["ams_slot"], "1")

    # ------------------------------------------------- смена катушки в слоте
    def test_new_uuid_in_slot_unbinds_old_spool(self):
        self.spool(tray_uuid="OLDUUID")
        result = sync_ams_spools(self.db, "prn1", snap([tray(uuid="NEWUUID")]))
        self.assertEqual(result["unbound"], 1)
        self.assertEqual(result["created"], 1)
        old = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual(old["ams_slot"], "")          # отвязана
        self.assertEqual(old["remaining_grams"], 500)  # остаток не тронут
        new = self.db.one("SELECT * FROM spools WHERE tray_uuid='NEWUUID'")
        self.assertIsNotNone(new)
        self.assertEqual(new["ams_slot"], "0")

    def test_tray_without_rfid_matches_by_slot(self):
        self.spool(tray_uuid="")
        result = sync_ams_spools(self.db, "prn1", snap([tray(uuid="", remain=33)]))
        self.assertEqual(result["updated"], 1)
        row = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual(row["remaining_grams"], 330.0)

    def test_small_drift_not_written(self):
        """Разница меньше грамма не дёргает базу при каждом проходе."""
        self.spool(remaining_grams=750.5)
        sync_ams_spools(self.db, "prn1", snap([tray(remain=75)]))
        row = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual(row["remaining_grams"], 750.5)


if __name__ == "__main__":
    unittest.main()

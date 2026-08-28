"""Сверка остатка AMS с базой (идея 21) и автоподбор слотов по метке (идея 20).

Два контракта:

1. Принтер показывает остаток в процентах, база — в граммах. Пока они
   согласованы, автосинк обновляет граммы сам. Если разошлись сильнее
   порога — база не перетирается: расхождение ждёт явного подтверждения,
   иначе учёт расхода и себестоимость поедут.
2. Автоподбор слота для печати предпочитает катушку, известную базе по
   RFID-метке, а не «просто похожий цвет» — расход должен списаться на
   конкретную катушку.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.ams_sync import (  # noqa: E402
    accept_ams_remaining,
    ams_remain_check,
    sync_ams_spools,
)
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.estimate import auto_ams_map  # noqa: E402


def snap(trays):
    return {"printer": {"firmware": "01.08.02.00", "wifi": "-52dBm"},
            "ams": {"humidity": "4", "trays": trays}}


def tray(slot=0, uuid="A1B2C3", mat="PLA", remain=75, color="#000000"):
    return {"slot": slot, "uuid": uuid, "type": mat, "remain": remain,
            "color": color, "label": f"AMS 1 · слот {slot + 1}"}


class AmsRemainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "test.sqlite3")
        self.db.upsert("printers", {"id": "prn1", "name": "P1S"})

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def spool(self, spool_id="sp1", **extra):
        data = {"id": spool_id, "material": "PLA", "brand": "Bambu",
                "color_name": "Чёрный", "total_grams": 1000,
                "remaining_grams": 500, "price": 1900, "printer_id": "prn1",
                "ams_slot": "0", "tray_uuid": "A1B2C3", "archived": 0,
                "ams_sync": 1, "verified": 1}
        data.update(extra)
        return self.db.upsert("spools", data)

    # -------------------------------------------------- обнаружение расхождения
    def test_mismatch_found_above_tolerance(self):
        self.spool()  # в базе 500 г из 1000 = 50%, принтер показывает 20%
        found = ams_remain_check(self.db, "prn1", snap([tray(remain=20)]), 25)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["pct_ours"], 50.0)
        self.assertEqual(found[0]["pct_ams"], 20.0)
        self.assertEqual(found[0]["grams_ams"], 200.0)
        self.assertEqual(found[0]["delta_pct"], -30.0)

    def test_small_drift_is_not_mismatch(self):
        self.spool(remaining_grams=490)  # 49% против 50% — в пределах порога
        self.assertEqual(ams_remain_check(self.db, "prn1", snap([tray(remain=50)]), 25), [])

    def test_manual_spool_is_not_checked(self):
        self.spool(ams_sync=0)
        self.assertEqual(ams_remain_check(self.db, "prn1", snap([tray(remain=20)]), 25), [])

    # ------------------------------------------ автосинк не перетирает остаток
    def test_sync_keeps_grams_on_big_mismatch(self):
        self.spool()
        result = sync_ams_spools(self.db, "prn1", snap([tray(remain=20)]))
        self.assertEqual(result["mismatch"], 1)
        row = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual(row["remaining_grams"], 500.0, "остаток не должен меняться вслепую")
        self.assertEqual(round(row["ams_remain_pct"]), 20)

    def test_sync_updates_grams_on_small_drift(self):
        self.spool(remaining_grams=490)
        result = sync_ams_spools(self.db, "prn1", snap([tray(remain=50)]))
        self.assertEqual(result["mismatch"], 0)
        self.assertEqual(result["updated"], 1)
        row = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual(row["remaining_grams"], 500.0)

    def test_confirm_disabled_overwrites_anyway(self):
        """Выключенный подтвержденческий режим — прежнее поведение."""
        self.db.set_settings({"ams_remain_confirm": False})
        self.spool()
        sync_ams_spools(self.db, "prn1", snap([tray(remain=20)]))
        row = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual(row["remaining_grams"], 200.0)

    # -------------------------------------------------- явное принятие факта
    def test_accept_writes_ams_fact(self):
        self.spool()
        sync_ams_spools(self.db, "prn1", snap([tray(remain=20)]))
        res = accept_ams_remaining(self.db, "sp1", printer_id="prn1")
        self.assertTrue(res["ok"])
        self.assertEqual(res["was_grams"], 500.0)
        self.assertEqual(res["now_grams"], 200.0)
        row = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual(row["remaining_grams"], 200.0)
        self.assertIsNone(row["ams_remain_pct"], "после принятия расхождение снято")

    def test_accept_requires_ams_data(self):
        self.spool()
        with self.assertRaises(ValueError):
            accept_ams_remaining(self.db, "sp1")

    def test_accept_refuses_manual_spool(self):
        self.spool(ams_sync=0)
        with self.assertRaises(ValueError):
            accept_ams_remaining(self.db, "sp1", pct=20)


class AmsAutoMapTests(unittest.TestCase):
    """Идея 20: слот выбирается по катушке, а не только по оттенку."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "test.sqlite3")
        self.db.upsert("printers", {"id": "prn1", "name": "P1S"})

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def spool(self, spool_id, uuid, remaining=800, verified=1, slot="0"):
        return self.db.upsert("spools", {
            "id": spool_id, "material": "PLA", "color_name": "Чёрный",
            "total_grams": 1000, "remaining_grams": remaining, "price": 1900,
            "printer_id": "prn1", "ams_slot": slot, "tray_uuid": uuid,
            "archived": 0, "ams_sync": 1, "verified": verified})

    def test_known_uuid_wins_over_closer_colour(self):
        # Слот 1 по цвету ближе к запросу, но в базе известна катушка в слоте 0.
        self.spool("sp1", "UUID-ONE", slot="0")
        trays = [tray(slot=0, uuid="UUID-ONE", color="#333333"),
                 tray(slot=1, uuid="UUID-TWO", color="#FF0000")]
        self.assertEqual(auto_ams_map([{"type": "PLA", "color": "#FF0000"}],
                                      trays, self.db, "prn1"), [0])

    def test_unverified_known_spool_loses_to_verified(self):
        self.spool("sp1", "UUID-ONE", verified=0, slot="0")
        self.spool("sp2", "UUID-TWO", verified=1, slot="1")
        trays = [tray(slot=0, uuid="UUID-ONE"), tray(slot=1, uuid="UUID-TWO")]
        self.assertEqual(auto_ams_map([{"type": "PLA", "color": "#000000"}],
                                      trays, self.db, "prn1"), [1])

    def test_slot_without_known_spool_is_penalised(self):
        self.spool("sp1", "UUID-ONE", slot="1")
        trays = [tray(slot=0, uuid="UUID-X"), tray(slot=1, uuid="UUID-ONE")]
        self.assertEqual(auto_ams_map([{"type": "PLA", "color": "#000000"}],
                                      trays, self.db, "prn1"), [1])

    def test_slot_not_reused_for_second_filament(self):
        self.spool("sp1", "UUID-ONE", slot="0")
        self.spool("sp2", "UUID-TWO", slot="1")
        trays = [tray(slot=0, uuid="UUID-ONE"), tray(slot=1, uuid="UUID-TWO")]
        mapping = auto_ams_map([{"type": "PLA", "color": "#000000"},
                                {"type": "PLA", "color": "#000000"}],
                               trays, self.db, "prn1")
        self.assertEqual(sorted(mapping), [0, 1])

    def test_preference_can_be_disabled(self):
        self.spool("sp1", "UUID-ONE", slot="1")
        self.db.set_settings({"ams_map_prefer_known": False})
        trays = [tray(slot=0, uuid="UUID-X", color="#FF0000"),
                 tray(slot=1, uuid="UUID-ONE", color="#333333")]
        # без базы слот выбирается только по цвету — побеждает слот 0
        self.assertEqual(auto_ams_map([{"type": "PLA", "color": "#FF0000"}],
                                      trays, self.db, "prn1"), [0])

    def test_without_db_behaviour_is_unchanged(self):
        trays = [tray(slot=0, uuid="UUID-X", color="#333333"),
                 tray(slot=1, uuid="UUID-Y", color="#FF0000")]
        self.assertEqual(auto_ams_map([{"type": "PLA", "color": "#FF0000"}], trays), [1])

    def test_unknown_material_gives_minus_one(self):
        self.assertEqual(auto_ams_map([{"type": "ABS", "color": "#FFFFFF"}],
                                      [tray(mat="PLA")], self.db, "prn1"), [-1])


class AmsApiTests(unittest.TestCase):
    """Маршруты сверки: подтверждение обязательно, деньги не двигаются сами."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "test.sqlite3")
        from connector.printflow.accounting import Accounting
        from connector.printflow.api import Api
        from connector.printflow.shelf import Shelf
        self.api = Api.__new__(Api)
        self.api.db = self.db
        self.api.shelf = Shelf(self.db)
        self.api.acc = Accounting(self.db)
        self.api.manager = types.SimpleNamespace(printers={}, bot=None)
        self.api.bus = types.SimpleNamespace(publish=lambda *a, **k: None)
        self.api.started_at = 0.0
        self.api.last_host = "test"

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def spool(self, **extra):
        data = {"id": "sp1", "material": "PLA", "color_name": "Чёрный",
                "total_grams": 1000, "remaining_grams": 500, "price": 1600,
                "printer_id": "prn1", "ams_slot": "0", "tray_uuid": "A1B2C3",
                "archived": 0, "ams_sync": 1, "verified": 1}
        data.update(extra)
        return self.db.upsert("spools", data)

    def test_accept_requires_confirmation(self):
        self.spool(ams_remain_pct=20)
        with self.assertRaises(ValueError):
            self.api.post("/api/spool/ams-accept", {"spool_id": "sp1"}, {})

    def test_accept_route_applies_fact(self):
        self.spool(ams_remain_pct=20)
        code, payload = self.api.post(
            "/api/spool/ams-accept",
            {"spool_id": "sp1", "confirmed": True, "printer_id": "prn1"}, {})
        self.assertEqual(code, 200)
        self.assertEqual(payload["now_grams"], 200.0)
        self.assertEqual(self.db.one("SELECT remaining_grams FROM spools WHERE id='sp1'")["remaining_grams"], 200.0)

    def test_restock_route_returns_revaluation(self):
        self.spool()
        code, payload = self.api.post(
            "/api/spool/restock", {"id": "sp1", "grams": 1000, "price": 2000}, {})
        self.assertEqual(code, 200)
        self.assertIn("spool", payload)
        self.assertTrue(payload["revaluation"]["applied"])
        # остаток 500 г по 1.6 ₽/г + приход 1000 г по 2.0 ₽/г → 1.8667 ₽/г
        self.assertAlmostEqual(payload["revaluation"]["per_gram_after"], 1.8667, places=3)


class MismatchWarningTests(unittest.TestCase):
    """Предупреждение собирается отдельной функцией — проверяемо без принтера."""

    def test_warns_when_ams_disagrees(self):
        from connector.printflow.ams_sync import mismatch_warning
        warn = mismatch_warning({"remaining_grams": 500, "ams_remain_pct": 20})
        self.assertIsNotNone(warn)
        self.assertEqual(warn["code"], "ams_remain_mismatch")
        self.assertIn("500 г", warn["detail"])
        self.assertIn("20%", warn["detail"])

    def test_silent_when_consistent(self):
        from connector.printflow.ams_sync import mismatch_warning
        self.assertIsNone(mismatch_warning({"remaining_grams": 500}))
        self.assertIsNone(mismatch_warning(None))

    def test_preflight_uses_helper(self):
        """Preflight подключает ту же проверку, что и склад."""
        from connector.printflow import preflight
        src = pathlib.Path(preflight.__file__).read_text(encoding="utf-8")
        self.assertIn("from .ams_sync import mismatch_warning", src)


if __name__ == "__main__":
    unittest.main()

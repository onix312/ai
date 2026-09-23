"""Предполёт с AMS (18.13): печать не стартует «на пустом слоте» и «не на том».

Проверка перед печатью существовала и раньше, но смотрела на активный слот
принтера. С автопилотом склад стал источником правды, и появились свои ошибки:
в слоте не то, что на складе; катушки нет вовсе, хотя файл печатает оттуда;
сторонний пластик не привязан и расход не спишется. Здесь проверяется, что эти
случаи дают блок или предупреждение, а чистый слот — ничего.

Логика — `ams_doctor.preflight_ams` (её же зовёт `preflight.check_preflight`);
в тестах MQTT не нужен: снимок принтера подставляется словарём.
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

from connector.printflow import ams_doctor, preflight  # noqa: E402
from connector.printflow.db import Database  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "test.sqlite3")


def snap_with(*trays: dict, state: str = "IDLE", humidity: float = 22.0) -> dict:
    return {"id": "p1", "printer": {"state": state, "state_label": "Готов",
                                    "problems": [], "sdcard": True},
            "ams": {"units": 1, "humidity": humidity, "temperature": 20,
                    "trays": list(trays)}}


def tray(slot: int, *, material: str = "PLA", color: str = "#00AE42",
         remain: float = 80.0, uuid: str = "b" * 32, present: bool = True,
         generic: bool = False, active: bool = False) -> dict:
    return {"slot": slot, "unit": 0, "type": material, "color": color,
            "remain": remain, "uuid": uuid, "present": present, "generic": generic,
            "bambulab": not generic, "active": active, "nozzle_min": 190,
            "nozzle_max": 240, "label": f"AMS 1 · слот {slot + 1}"}


class PreflightAmsTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.db.upsert("printers", {"id": "p1", "name": "P1S"})

    def tearDown(self):
        self.db.close()

    def spool(self, spool_id: str = "sp1", **extra) -> dict:
        data = {"id": spool_id, "material": "PLA", "brand": "Bambu Lab",
                "color_name": "Зелёный", "color_hex": "#00AE42",
                "total_grams": 1000, "remaining_grams": 500, "price": 1900,
                "printer_id": "p1", "ams_slot": "0", "tray_uuid": "b" * 32,
                "location": "ams", "ams_sync": 1, "verified": 1, "archived": 0}
        data.update(extra)
        return self.db.upsert("spools", data)

    def check(self, snap: dict, **kw) -> dict:
        return ams_doctor.preflight_ams(self.db, "p1", snap, **kw)

    def codes(self, result: dict) -> set[str]:
        return {item["code"] for group in ("blocks", "warns", "infos")
                for item in result[group]}

    def test_clean_slot_passes_silently(self):
        self.spool()
        result = self.check(snap_with(tray(0, active=True)))
        self.assertEqual([], result["blocks"])
        self.assertEqual([], result["warns"])

    def test_wrong_plastic_in_slot_blocks_the_start(self):
        self.spool(material="PETG")
        result = self.check(snap_with(tray(0, material="PLA", active=True)))
        self.assertIn("ams_type_mismatch", self.codes(result))
        self.assertTrue(result["blocks"], "печать на чужом пластике не заблокирована")

    def test_mismatch_is_only_a_note_when_the_block_is_off(self):
        self.db.set_settings({"preflight_block_material": False})
        self.spool(material="PETG")
        result = self.check(snap_with(tray(0, material="PLA", active=True)))
        self.assertEqual([], result["blocks"])
        self.assertIn("ams_slot_diff", self.codes(result))

    def test_empty_slot_from_mapping_blocks_the_start(self):
        result = self.check(snap_with(tray(0, present=False, uuid="", remain=None)),
                            ams_mapping=[0])
        self.assertIn("ams_slot_occupied_empty", self.codes(result))

    def test_slot_the_printer_does_not_know_blocks_the_start(self):
        self.spool()
        result = self.check(snap_with(tray(0, active=True)), ams_mapping=[3])
        self.assertIn("ams_slot_missing", self.codes(result))

    def test_finished_spool_in_slot_blocks_the_start(self):
        self.spool()
        result = self.check(snap_with(tray(0, remain=0, active=True)))
        self.assertIn("ams_empty", self.codes(result))

    def test_foreign_plastic_is_a_warning_not_a_block(self):
        result = self.check(snap_with(tray(0, uuid="", generic=True, active=True)))
        self.assertIn("ams_no_spool", self.codes(result))
        self.assertEqual([], result["blocks"])

    def test_unverified_spool_is_a_warning(self):
        self.spool(verified=0)
        result = self.check(snap_with(tray(0, active=True)))
        self.assertIn("ams_unverified", self.codes(result))

    def test_material_in_ams_without_a_slot_is_a_note(self):
        self.spool()
        result = self.check(snap_with(tray(0)), estimate={"material": "PLA"})
        self.assertIn("ams_pick_slot", self.codes(result))

    def test_trouble_in_another_slot_is_a_note(self):
        self.spool(material="PETG")
        self.db.upsert("spools", {"id": "sp2", "material": "ABS", "printer_id": "p1",
                                  "ams_slot": "1", "tray_uuid": "c" * 32,
                                  "location": "ams", "remaining_grams": 500,
                                  "total_grams": 1000, "price": 1900, "verified": 1})
        # В слоте 1 принтер видит PLA, а склад считает, что там ABS.
        snap = snap_with(tray(0, material="PETG"), tray(1, material="PLA"))
        result = self.check(snap)
        self.assertNotIn("ams_type_mismatch", self.codes(result),
                         "чужой слот заблокировал печать")
        self.assertIn("ams_doctor", self.codes(result))

    def test_without_telemetry_nothing_is_invented(self):
        self.spool()
        result = self.check({})
        self.assertEqual({"blocks": [], "warns": [], "infos": []}, result)


class PreflightIntegrationTests(unittest.TestCase):
    """Тот же путь, что зовёт кнопка «Печать»: менеджер → check_preflight."""

    def setUp(self):
        self.db = make_db()
        self.db.upsert("printers", {"id": "p1", "name": "P1S"})
        self.snap = snap_with(tray(0, material="PLA", active=True))
        printer = types.SimpleNamespace(
            id="p1", record={"name": "P1S", "nozzle_size": 0.4},
            snapshot=lambda: self.snap, camera=None,
            _slicer_estimate=lambda *a, **k: {"grams": 120, "minutes": 60,
                                              "material": "PETG"})
        self.manager = types.SimpleNamespace(get=lambda pid: printer if pid == "p1" else None)
        self.db.upsert("spools", {"id": "sp1", "material": "PETG", "color_name": "Чёрный",
                                  "total_grams": 1000, "remaining_grams": 500, "price": 1900,
                                  "printer_id": "p1", "ams_slot": "0", "tray_uuid": "b" * 32,
                                  "location": "ams", "ams_sync": 1, "verified": 1})
        self.db.set_settings({"preflight_warn_humidity": False})

    def tearDown(self):
        self.db.close()

    def test_check_preflight_sees_the_ams_mismatch(self):
        result = preflight.check_preflight(self.db, self.manager, "p1", "model.3mf")
        codes = {item["code"] for item in result["blocks"]}
        self.assertIn("ams_type_mismatch", codes)
        self.assertFalse(result["ok"], "предполёт пропустил печать с чужим пластиком")

    def test_ams_infos_do_not_break_the_answer_shape(self):
        self.db.set_settings({"preflight_block_material": False})
        result = preflight.check_preflight(self.db, self.manager, "p1", "model.3mf")
        self.assertIn("blocks", result)
        self.assertIn("warns", result)
        self.assertIn("infos", result)
        self.assertIn("estimate", result)


if __name__ == "__main__":
    unittest.main()

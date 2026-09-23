"""Автопилот AMS: переносы, сторонний пластик, остатки и запись в слот (18.13).

Что было. Синк только «верил датчику»: переезд катушки выглядел как «отвязать и
завести заново», сторонний пластик без RFID не узнавался никогда, а кончившаяся
катушка висела в слоте с нулём грамм — и после каждого такого случая владелец
правил склад руками.

Здесь проверяются решения автопилота (`ams_sync`) и то, что каждое из них
попадает в журнал (`ams_actions`) с возможностью отката. Отправка команд в
принтер не проверяется: MQTT в тестах нет, зато есть `pushes` — план из чистых
функций `ams_push`.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import ams_actions, ams_push, ams_sync  # noqa: E402
from connector.printflow.db import Database  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "test.sqlite3")


def seed_printer(db: Database, printer_id: str = "p1") -> None:
    db.upsert("printers", {"id": printer_id, "name": "P1S"})
    db.upsert("printers", {"id": "p2", "name": "A1 mini"})


def snap_with(*trays: dict, state: str = "IDLE") -> dict:
    return {"id": "p1", "printer": {"firmware": "01.08", "wifi": "-52", "state": state},
            "ams": {"humidity": 22, "trays": list(trays)}}


def tray(slot: int, *, material: str = "PLA", color: str = "#00AE42",
         remain: float = 80.0, uuid: str = "b" * 32, present: bool = True,
         generic: bool = False, bambulab: bool = True) -> dict:
    return {"slot": slot, "type": material, "color": color, "remain": remain,
            "uuid": uuid, "present": present, "generic": generic,
            "bambulab": bambulab, "brand": "Bambu Lab" if bambulab else "",
            "nozzle_min": 190, "nozzle_max": 240,
            "label": f"AMS 1 · слот {slot + 1}"}


class AutopilotBase(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        seed_printer(self.db)

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

    def feed(self, kind: str = "") -> list[dict]:
        sql = "SELECT * FROM ams_actions"
        params: tuple = ()
        if kind:
            sql += " WHERE kind=?"
            params = (kind,)
        return self.db.query(sql + " ORDER BY at", params)

    def history(self, action: str = "") -> list[dict]:
        if action:
            return self.db.query("SELECT * FROM ams_slot_history WHERE action=?", (action,))
        return self.db.query("SELECT * FROM ams_slot_history ORDER BY at")


class SpoolCreationTests(AutopilotBase):
    """Срез 1: катушка заводится «как надо», а не 1000 г за 0 рублей."""

    def test_defaults_from_table_and_rfid_brand(self):
        self.db.set_settings({"ams_material_defaults": {
            "PLA": {"total_grams": 750, "price": 2100}}})
        result = ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, remain=60)))
        self.assertEqual(1, result["created"])
        row = self.db.one("SELECT * FROM spools WHERE printer_id='p1'")
        self.assertEqual("Bambu Lab", row["brand"], "RFID Bambu не дал бренд")
        self.assertEqual(750.0, row["total_grams"])
        self.assertEqual(2100.0, row["price"])
        self.assertEqual(450.0, row["remaining_grams"], "60 % от 750 г")
        self.assertEqual(1, row["verified"], "значения из таблицы — уже проверенные")
        self.assertEqual(1, row["ams_auto"], "катушка не помечена как автоматическая")

    def test_unknown_values_are_marked_for_review(self):
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(
            tray(0, generic=True, bambulab=False, uuid="")))
        row = self.db.one("SELECT * FROM spools WHERE printer_id='p1'")
        self.assertEqual(0, row["verified"], "выдуманные значения выглядят проверенными")
        self.assertIn("автопилот", str(row["note"]).lower())

    def test_creation_is_journalled_and_undo_archives_it(self):
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0)))
        action = self.feed("create")[0]
        spool_id = self.db.one("SELECT id FROM spools WHERE printer_id='p1'")["id"]
        ams_actions.undo_action(self.db, action["id"])
        row = self.db.one("SELECT * FROM spools WHERE id=?", (spool_id,))
        self.assertEqual(1, row["archived"], "откат создания не убрал катушку со склада")
        self.assertEqual("", row["ams_slot"])
        self.assertIsNone(self.db.one("SELECT * FROM ams_slots WHERE printer_id='p1'"),
                          "откат создания не вернул память слота")

    def test_autopilot_off_keeps_old_behaviour_without_feed(self):
        self.db.set_settings({"ams_autopilot": False})
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0)))
        self.assertTrue(self.db.one("SELECT * FROM spools WHERE printer_id='p1'"))
        self.assertEqual([], self.feed(), "выключенный автопилот всё равно пишет журнал")


class MoveAndBindTests(AutopilotBase):
    """Срез 2: одна метка = одна катушка, переезд — это перенос привязки."""

    def test_move_to_another_slot_is_a_transfer_not_a_new_spool(self):
        self.spool(ams_slot="0")
        result = ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(2)))
        self.assertEqual(1, result["moved"])
        self.assertEqual(0, result["created"], "переезд создал вторую катушку")
        row = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual("2", row["ams_slot"])
        action = self.feed("move")[0]
        self.assertEqual("2", action["slot"])
        self.assertEqual("sp1", action["spool_id"])

    def test_duplicate_rfid_never_appears(self):
        self.spool(ams_slot="0")
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0)))
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(1)))
        rows = self.db.query("SELECT * FROM spools WHERE tray_uuid=?", ("b" * 32,))
        self.assertEqual(1, len(rows), "одна метка дала две катушки на складе")

    def test_generic_spool_is_recognised_by_history_and_colour(self):
        """Сторонний пластик без RFID: вынули из слота 0 — узнали в слоте 1."""
        self.spool(tray_uuid="", ams_slot="0", color_hex="#00AE42")
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(
            {"slot": 0, "type": "", "color": "", "remain": None, "uuid": "",
             "present": False, "generic": False, "label": "Слот 0"}))
        self.assertEqual(1, len(self.history("auto_unbind")))
        result = ams_sync.sync_ams_spools(self.db, "p1", snap_with(
            tray(1, uuid="", generic=True, bambulab=False, remain=55)))
        self.assertEqual(1, result["adopted"])
        self.assertEqual(0, result["created"], "катушку завели заново вместо узнавания")
        row = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual("1", row["ams_slot"])
        self.assertEqual(550.0, row["remaining_grams"])

    def test_two_candidates_silence_the_autopilot(self):
        """Подходят две катушки — автопилот молчит и оставляет след в журнале."""
        self.spool("sp1", tray_uuid="", ams_slot="", color_hex="#00AE42")
        self.spool("sp2", tray_uuid="", ams_slot="", color_hex="#00AE42")
        for spool_id in ("sp1", "sp2"):
            ams_sync.slot_event(self.db, "p1", "0", spool_id, "auto_unbind", "сняли")
        result = ams_sync.sync_ams_spools(self.db, "p1", snap_with(
            tray(1, uuid="", generic=True, bambulab=False)))
        self.assertEqual(0, result["adopted"])
        self.assertEqual(0, result["created"])
        skip = self.feed("skip")
        self.assertTrue(skip, "непонятный случай не попал в журнал")
        self.assertEqual(0, int(skip[0]["undoable"]))

    def test_generic_with_wrong_colour_is_not_adopted(self):
        self.spool(tray_uuid="", ams_slot="", color_hex="#00AE42")
        ams_sync.slot_event(self.db, "p1", "0", "sp1", "auto_unbind", "сняли")
        result = ams_sync.sync_ams_spools(self.db, "p1", snap_with(
            tray(1, uuid="", generic=True, bambulab=False, color="#CC0000")))
        self.assertEqual(0, result["adopted"], "узнали катушку по чужому цвету")


class RemainPolicyTests(AutopilotBase):
    """Срез 3: 0 % — пустая и на склад, рост — долив, падение без печати — потеря."""

    def test_sensor_zero_empties_spool_but_keeps_rfid(self):
        self.spool(remaining_grams=500)
        result = ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, remain=0)))
        self.assertEqual(1, result["empty"])
        row = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual(0.0, row["remaining_grams"])
        self.assertEqual("", row["ams_slot"])
        self.assertEqual("shop", row["location"])
        self.assertEqual("empty", row["ams_state"])
        self.assertEqual("b" * 32, row["tray_uuid"],
                         "метка стёрта: перемотку потом не узнают, будет дубль")
        self.assertTrue(self.feed("empty"), "пустая катушка не попала в журнал")
        notify = [item["kind"] for item in result["notify"]]
        self.assertIn("empty", notify)

    def test_empty_spool_is_not_created_twice(self):
        self.spool(remaining_grams=500)
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, remain=0)))
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, remain=0)))
        rows = self.db.query("SELECT * FROM spools WHERE tray_uuid=?", ("b" * 32,))
        self.assertEqual(1, len(rows), "пустой слот завёл вторую катушку")

    def test_rewound_spool_comes_back_to_the_same_card(self):
        """Долив на той же метке: остаток вырос, метка и слот на месте."""
        self.spool(remaining_grams=400)
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, remain=40)))
        result = ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, remain=70)))
        self.assertEqual(1, len(self.feed("fill")), "долив не попал в журнал")
        row = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual(700.0, row["remaining_grams"])
        self.assertEqual("0", row["ams_slot"])
        self.assertEqual({"created": 0, "unbound": 0},
                         {k: result[k] for k in ("created", "unbound")})

    def test_drop_without_printing_is_reported_as_a_loss(self):
        self.spool(remaining_grams=700)
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, remain=70)))
        result = ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, remain=30)))
        loss = self.feed("loss")
        self.assertTrue(loss, "падение остатка не попало в журнал")
        self.assertEqual(0, int(loss[0]["undoable"]))
        self.assertIn("loss", [item["kind"] for item in result["notify"]])

    def test_drop_during_printing_is_not_a_loss(self):
        self.spool(remaining_grams=700)
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, remain=70)))
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(
            tray(0, remain=30), state="RUNNING"))
        self.assertEqual([], self.feed("loss"), "печать приняли за потерю пластика")

    def test_undo_of_empty_returns_the_spool_to_the_slot(self):
        self.spool(remaining_grams=500)
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, remain=0)))
        action = self.feed("empty")[0]
        ams_actions.undo_action(self.db, action["id"])
        row = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual("0", row["ams_slot"])
        self.assertEqual(500.0, row["remaining_grams"])
        self.assertEqual("ams", row["location"])
        self.assertTrue(self.db.one("SELECT * FROM ams_slots WHERE printer_id='p1'"
                                    " AND slot='0' AND spool_id='sp1'"))
        with self.assertRaises(ValueError):
            ams_actions.undo_action(self.db, action["id"])


class SlotPushTests(AutopilotBase):
    """Срез 4: склад — источник правды для слота, но не во время печати."""

    def test_type_mismatch_goes_into_the_push_plan(self):
        self.spool(material="PETG", color_hex="#00AE42")
        result = ams_sync.sync_ams_spools(self.db, "p1", snap_with(
            tray(0, material="PLA", color="#00AE42")))
        self.assertEqual(1, len(result["pushes"]))
        item = result["pushes"][0]
        self.assertEqual("type", item["diff"][0]["field"])
        self.assertEqual("PETG", item["diff"][0]["want"])
        self.assertIn("conflict", [note["kind"] for note in result["notify"]])

    def test_same_plan_is_not_sent_twice(self):
        self.spool(material="PETG")
        first = ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, material="PLA")))
        ams_sync.mark_slot_pushed(self.db, "p1", 0, first["pushes"][0]["signature"])
        second = ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, material="PLA")))
        self.assertEqual([], second["pushes"], "одна и та же запись уходит в MQTT повторно")

    def test_nothing_is_pushed_while_printing(self):
        self.spool(material="PETG")
        result = ams_sync.sync_ams_spools(self.db, "p1", snap_with(
            tray(0, material="PLA"), state="RUNNING"))
        self.assertEqual([], result["pushes"], "настройки слота меняли во время печати")

    def test_push_plan_can_be_switched_off(self):
        self.db.set_settings({"ams_push_settings": False})
        self.spool(material="PETG")
        result = ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, material="PLA")))
        self.assertEqual([], result["pushes"])

    def test_desired_slot_uses_bambu_preset(self):
        want = ams_push.desired_slot({"material": "PLA", "brand": "Bambu Lab",
                                      "color_hex": "#00AE42"})
        self.assertEqual("PLA", want["type"])
        self.assertEqual("#00AE42", want["color"])
        self.assertGreaterEqual(want["temp_min"], 100)
        self.assertLessEqual(want["temp_min"], want["temp_max"])

    def test_multicolour_spool_does_not_push_colour(self):
        want = ams_push.desired_slot({"material": "PLA", "colors_json": '["#FF0000","#00FF00"]',
                                      "color_hex": "#FF0000"})
        self.assertEqual("", want["color"], "цвет градиента уехал бы одним кодом")


if __name__ == "__main__":
    unittest.main()

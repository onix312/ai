"""AMS-доктор и маршруты автопилота (18.13).

Доктор — экран, по которому видно работу автоматики и её сомнения: какая
катушка не проверена, где склад расходится с принтером, что автопилот не смог
узнать сам. Здесь проверяются три вещи: правила приоритета проблем (ошибка,
предупреждение, заметка), план раскладки под очередь и маршруты `/api/ams/*`:
доктор, лента, откат, таблица материалов, правила, приём остатка.
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

from connector.printflow import ams_actions, ams_doctor, ams_sync  # noqa: E402
from connector.printflow.db import Database  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "test.sqlite3")


def seed(db: Database) -> None:
    db.upsert("printers", {"id": "p1", "name": "P1S"})


def snap_with(*trays: dict, state: str = "IDLE", humidity: float = 22.0) -> dict:
    return {"id": "p1", "printer": {"state": state, "state_label": "Готов"},
            "ams": {"humidity": humidity, "trays": list(trays)}}


def tray(slot: int, *, material: str = "PLA", color: str = "#00AE42",
         remain: float = 80.0, uuid: str = "b" * 32, present: bool = True,
         generic: bool = False) -> dict:
    return {"slot": slot, "type": material, "color": color, "remain": remain,
            "uuid": uuid, "present": present, "generic": generic,
            "bambulab": not generic, "brand": "Bambu Lab" if not generic else "",
            "nozzle_min": 190, "nozzle_max": 240, "label": f"AMS 1 · слот {slot + 1}"}


class DoctorIssueTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        seed(self.db)

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

    def codes(self, snap: dict | None) -> set[str]:
        return {item["code"] for item in ams_doctor.issues_for_snapshot(self.db, "p1", snap)}

    def test_clean_slot_has_no_issues(self):
        self.spool()
        self.assertEqual(set(), self.codes(snap_with(tray(0))))

    def test_type_mismatch_is_an_error(self):
        self.spool(material="PETG")
        issues = ams_doctor.issues_for_snapshot(self.db, "p1", snap_with(tray(0)))
        mismatch = [item for item in issues if item["code"] == "slot_type_mismatch"]
        self.assertTrue(mismatch, "слот с чужим пластиком не попал в доктор")
        self.assertEqual("error", mismatch[0]["severity"])

    def test_unverified_low_and_empty_are_warnings(self):
        self.spool(verified=0)
        self.assertTrue({"spool_unverified"} <= self.codes(snap_with(tray(0, remain=8))))
        self.assertIn("slot_empty_sensor",
                      self.codes(snap_with(tray(0, remain=0))))

    def test_generic_tray_without_binding_is_a_note(self):
        self.assertIn("slot_unbound_generic",
                      self.codes(snap_with(tray(3, uuid="", generic=True))))

    def test_empty_slot_with_binding_is_a_warning(self):
        self.spool()
        self.assertIn("slot_empty_bound",
                      self.codes(snap_with(tray(0, present=False, uuid=""))))

    def test_humidity_matters_only_for_hygroscopic_plastic(self):
        self.spool(material="PETG")
        self.assertIn("ams_humid", self.codes(snap_with(tray(0, material="PETG"),
                                                       humidity=70)))
        self.spool("sp2", material="PLA", tray_uuid="c" * 32)
        codes = self.codes(snap_with(tray(0, material="PLA"), humidity=70))
        self.assertNotIn("ams_humid", codes, "PLA не боится влажности")

    def test_memory_stale_and_empty_in_slot_come_from_base(self):
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0)))
        self.db.execute("UPDATE ams_slots SET seen_at=?", ("2020-01-01T10:00:00+00:00",))
        self.assertIn("memory_stale",
                      {item["code"] for item in ams_doctor.issues_from_base(self.db, "p1")})
        self.spool(remaining_grams=0)
        self.assertIn("empty_in_slot",
                      {item["code"] for item in ams_doctor.issues_from_base(self.db, "p1")})

    def test_report_counts_and_settings(self):
        self.spool(verified=0, material="PETG")
        data = ams_doctor.report(self.db, "p1", {"p1": snap_with(tray(0, material="PETG"))})
        self.assertEqual(1, len(data["printers"]))
        self.assertGreaterEqual(data["issues_total"], 1)
        self.assertEqual(data["issues_total"],
                         data["printers"][0]["issues_count"])
        self.assertTrue(data["settings"]["autopilot"])
        self.assertEqual(30.0, data["settings"]["retry_min"])
        self.assertIn("actions", data)

    def test_report_without_snapshot_still_works(self):
        self.spool(verified=0)
        data = ams_doctor.report(self.db, "p1", {})
        self.assertFalse(data["printers"][0]["online"])
        self.assertEqual(0, data["errors"], "без телеметрии доктор выдумал ошибки")


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        seed(self.db)
        self.spool("sp-pla", material="PLA", ams_slot="", location="shop",
                   tray_uuid="")
        self.spool("sp-petg", material="PETG", ams_slot="2", location="ams",
                   tray_uuid="c" * 32)

    def tearDown(self):
        self.db.close()

    def spool(self, spool_id: str, **extra) -> dict:
        data = {"id": spool_id, "material": "PLA", "brand": "Bambu Lab",
                "color_name": "Зелёный", "color_hex": "#00AE42",
                "total_grams": 1000, "remaining_grams": 800, "price": 1900,
                "printer_id": "p1", "ams_slot": "", "tray_uuid": "",
                "location": "shop", "ams_sync": 1, "verified": 1, "archived": 0}
        data.update(extra)
        return self.db.upsert("spools", data)

    def job(self, job_id: str, **extra) -> dict:
        data = {"id": job_id, "printer_id": "p1", "name": f"Задание {job_id}",
                "file": "model.3mf", "state": "queued", "grams": 120,
                "spool_id": "", "ams_mapping": "", "priority": 0,
                "created_at": "2026-01-01T10:00:00+00:00"}
        data.update(extra)
        return self.db.upsert("print_jobs", data)

    def test_job_spool_goes_into_a_free_slot(self):
        self.job("j1", spool_id="sp-pla")
        plan = ams_doctor.plan(self.db, "p1", {"p1": snap_with(tray(2))})
        printer = plan["printers"][0]
        step = printer["plan"][0]
        self.assertEqual("put", step["action"])
        self.assertEqual("sp-pla", step["spool_id"])
        self.assertNotEqual("2", step["slot"], "заняли слот, который уже занят")

    def test_job_whose_spool_is_already_in_place_needs_no_move(self):
        self.job("j1", spool_id="sp-petg")
        plan = ams_doctor.plan(self.db, "p1", {"p1": snap_with(tray(2))})
        printer = plan["printers"][0]
        self.assertEqual("keep", printer["plan"][0]["action"])
        self.assertEqual([], printer["moves"], "план предлагает лишнюю перестановку")

    def test_unknown_spool_is_listed_honestly(self):
        self.job("j1", spool_id="")
        plan = ams_doctor.plan(self.db, "p1", {"p1": snap_with(tray(2))})
        unknown = plan["printers"][0]["unknown"]
        self.assertTrue(unknown, "задание без катушки не попало в план")
        self.assertIn("катушка не выбрана", unknown[0]["reason"])

    def test_spare_spool_is_offered_for_eviction(self):
        self.job("j1", spool_id="sp-pla")
        plan = ams_doctor.plan(self.db, "p1", {"p1": snap_with(tray(2))})
        spare = plan["printers"][0]["spare"]
        self.assertTrue(spare, "катушка, мешающая очереди, не попала в подсказку")

    def test_plan_is_only_advice(self):
        """Совет не двигает катушки: склад и привязки остаются как были."""
        self.job("j1", spool_id="sp-pla")
        ams_doctor.plan(self.db, "p1", {"p1": snap_with(tray(2))})
        row = self.db.one("SELECT * FROM spools WHERE id='sp-pla'")
        self.assertEqual("", row["ams_slot"])


class AmsRouteTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        seed(self.db)
        from connector.printflow.api import Api
        from connector.printflow.shelf import Shelf

        self.printer = types.SimpleNamespace(
            id="p1", record={"name": "P1S"}, snapshot=lambda: self.snap)
        self.fix_calls: list = []

        def fix_all(printer_id=""):
            self.fix_calls.append(printer_id or "all")
            return {"ok": True, "printers": 1, "created": 2, "pushes": 1}

        self.manager = types.SimpleNamespace(
            printers={"p1": self.printer},
            get=lambda pid: self.printer if pid == "p1" else None,
            snapshot=lambda: {"printers": [self.snap]},
            ams_fix_all=fix_all, bot=None)
        api = Api.__new__(Api)
        api.db = self.db
        api.shelf = Shelf(self.db)
        api.manager = self.manager
        api.acc = mock.Mock()
        api.bus = types.SimpleNamespace(publish=lambda *a, **k: None)
        api.started_at = time.time()
        api.last_host = "test"
        self.api = api
        self.snap = snap_with(tray(0))

    def tearDown(self):
        self.db.close()

    def spool(self, spool_id: str = "sp1", **extra) -> dict:
        data = {"id": spool_id, "material": "PETG", "brand": "Bambu Lab",
                "color_name": "Зелёный", "color_hex": "#00AE42",
                "total_grams": 1000, "remaining_grams": 400, "price": 1900,
                "printer_id": "p1", "ams_slot": "0", "tray_uuid": "b" * 32,
                "location": "ams", "ams_sync": 1, "verified": 1, "archived": 0}
        data.update(extra)
        return self.db.upsert("spools", data)

    def test_doctor_route_returns_report(self):
        self.spool()
        code, payload = self.api.get("/api/ams/doctor", {"printer_id": ["p1"]})
        self.assertEqual(200, code)
        self.assertIn("printers", payload)
        self.assertIn("material_defaults", payload)
        self.assertIn("settings", payload)

    def test_actions_route_and_undo(self):
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0)))
        code, payload = self.api.get("/api/ams/actions", {"printer_id": ["p1"]})
        self.assertEqual(200, code)
        action = payload["actions"][0]
        self.assertEqual("create", action["kind"])
        code, undone = self.api.post("/api/ams/action/undo", {"id": action["id"]}, {})
        self.assertEqual(200, code)
        self.assertTrue(undone["ok"])
        row = self.db.one("SELECT * FROM ams_actions WHERE id=?", (action["id"],))
        self.assertTrue(row["undone_at"])

    def test_undo_unknown_action_is_an_error(self):
        code, payload = self.api.post("/api/ams/action/undo", {"id": "нет такого"}, {})
        self.assertEqual(400, code)
        self.assertIn("не найдено", str(payload["error"]).lower())

    def test_fix_all_route_calls_the_manager(self):
        code, payload = self.api.post("/api/ams/doctor/fix-all", {}, {})
        self.assertEqual(200, code)
        self.assertEqual(1, payload["printers"])
        self.assertEqual(["all"], self.fix_calls)

    def test_material_default_route(self):
        code, payload = self.api.post("/api/ams/material-default",
                                      {"material": "PETG", "total_grams": 750,
                                       "price": 2200, "brand": "eSUN"}, {})
        self.assertEqual(200, code)
        self.assertEqual(750.0, payload["material_defaults"]["PETG"]["total_grams"])
        code, payload = self.api.get("/api/ams/rules", {})
        self.assertEqual(200, code)
        self.assertIn("PETG", payload["material_defaults"])

    def test_rule_save_and_forget(self):
        code, payload = self.api.post("/api/ams/rule/save", {
            "kind": "spool_defaults", "key": "PLA|esun",
            "value": {"total_grams": 750}}, {})
        self.assertEqual(200, code)
        self.assertTrue(payload["rule"]["applied"], "правило из панели ждёт подтверждения")
        rule_id = payload["rule"]["id"]
        code, payload = self.api.post("/api/ams/rule/forget", {"id": rule_id}, {})
        self.assertEqual(200, code)
        self.assertEqual(1, payload["removed"])

    def test_rule_save_without_value_is_rejected(self):
        code, payload = self.api.post("/api/ams/rule/save",
                                      {"kind": "spool_defaults", "key": "PLA|"}, {})
        self.assertEqual(400, code)

    def test_accept_route_takes_the_sensor_value(self):
        self.spool(remaining_grams=400)
        self.snap = snap_with(tray(0, remain=55))
        code, payload = self.api.post("/api/ams/accept", {"spool_id": "sp1",
                                                          "printer_id": "p1"}, {})
        self.assertEqual(200, code)
        self.assertEqual(550.0, payload["remaining_grams"])
        row = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual(550.0, row["remaining_grams"])
        self.assertEqual("ok", row["ams_state"])
        feed = self.db.query("SELECT * FROM ams_actions WHERE kind='accept'")
        self.assertTrue(feed, "приём данных датчика не попал в ленту")
        ams_actions.undo_action(self.db, feed[0]["id"])
        back = self.db.one("SELECT * FROM spools WHERE id='sp1'")
        self.assertEqual(400.0, back["remaining_grams"], "откат не вернул прежний остаток")

    def test_accept_route_needs_a_printer(self):
        self.spool(printer_id="", ams_slot="")
        code, payload = self.api.post("/api/ams/accept", {"spool_id": "sp1"}, {})
        self.assertEqual(400, code)

    def test_summary_route_counts_manual_work(self):
        self.spool(verified=0)
        code, payload = self.api.get("/api/ams/summary", {"printer_id": ["p1"]})
        self.assertEqual(200, code)
        self.assertEqual(1, payload["unverified"], "непосчитанная ручная работа")
        self.assertIn("actions_24h", payload)

    def test_plan_route_answers_with_advice(self):
        self.spool()
        code, payload = self.api.get("/api/ams/plan", {"printer_id": ["p1"]})
        self.assertEqual(200, code)
        self.assertIn("printers", payload)
        self.assertIn("queue", payload)


if __name__ == "__main__":
    unittest.main()

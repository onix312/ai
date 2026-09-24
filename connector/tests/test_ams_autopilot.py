"""Сквозные ПК-тесты автопилота AMS без MQTT, принтера и Telegram.

Снимки принтера и команда MQTT подменены: физическое подтверждение отдельно.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import types
import unittest

from connector.printflow import (ams_actions, ams_defaults, ams_doctor, ams_push,
                                 ams_sync, subscriptions)
from connector.printflow.ams_autopilot import tidy
from connector.printflow.api import Api
from connector.printflow.config import now_iso
from connector.printflow.db import Database
from connector.printflow.preflight import check_ams_filament, check_preflight
from connector.printflow.repo import Repo


def tray(slot=0, *, uuid="rfid-new", material="PLA", color="#FF4400",
         remain=80, present=True, brand="", nozzle_min=None, nozzle_max=None):
    return {"slot": slot, "label": f"AMS 1:{slot + 1}", "uuid": uuid,
            "type": material, "color": color, "remain": remain, "present": present,
            "brand": brand, "nozzle_min": nozzle_min, "nozzle_max": nozzle_max}


def snap(*trays, state="IDLE", humidity=30):
    return {"printer": {"state": state, "firmware": "01.08"},
            "connection": {"connected": True},
            "ams": {"trays": list(trays), "humidity": humidity}}


class FakePrinter:
    id = "p1"
    connected = True

    def __init__(self, current=None):
        self.current = current or snap(tray())
        self.calls: list[tuple[str, dict]] = []
        self.error = ""

    def snapshot(self):
        return self.current

    def command(self, name, value):
        self.calls.append((name, value))
        if self.error:
            raise RuntimeError(self.error)
        return {"ok": True}


class AmsAutopilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "test.sqlite3")
        self.addCleanup(self.db.close)
        self.db.upsert("printers", {"id": "p1", "name": "P1S"})
        self.printer = FakePrinter()
        self.sent = []
        self.manager = types.SimpleNamespace(get=lambda pid: self.printer,
            notify_async=lambda text, **kw: self.sent.append((text, kw)))
        self.api = Api.__new__(Api)
        self.api.db = self.db
        self.api.manager = self.manager
        self.api.repo = Repo(self.db)

    def spool(self, **fields):
        data = {"id": "sp1", "material": "PLA", "color_hex": "#FF4400",
                "total_grams": 1000, "remaining_grams": 800, "price": 1600,
                "tray_uuid": "rfid-new", "printer_id": "p1", "ams_slot": "0",
                "location": "ams", "ams_sync": 1, "created_at": now_iso()}
        data.update(fields)
        return self.db.upsert("spools", data)

    def test_owner_rule_material_catalog_and_builtin_price(self):
        rule = ams_defaults.save_rule(self.db, {
            "match_material": "PLA", "brand": "Nozza", "price": 1900,
            "total_grams": 750, "temp_min": 205, "temp_max": 225})
        self.assertEqual(rule["match_material"], "PLA")
        data = ams_sync.sync_ams_spools(self.db, "p1", snap(tray()))
        self.assertEqual(1, data["created"])
        created = self.db.one("SELECT * FROM spools WHERE tray_uuid='rfid-new'")
        self.assertEqual("Nozza", created["brand"])
        self.assertEqual(1900, created["price"])
        self.assertEqual(750, created["total_grams"])
        self.assertEqual(600, created["remaining_grams"])
        self.assertEqual("правило владельца", created["price_source"])
        self.assertEqual((205, 225),
                         (ams_push.payload_for(self.db, created, 0)["temp_min"],
                          ams_push.payload_for(self.db, created, 0)["temp_max"]))
        self.db.execute("DELETE FROM spools")
        self.db.execute("DELETE FROM ams_rules")
        self.db.upsert("materials", {"id": "pla-cat", "key": "PLA", "name": "PLA",
                                     "price_per_kg": 2500, "temp_nozzle_min": 210,
                                     "temp_nozzle_max": 230, "archived": 0})
        catalog = ams_defaults.defaults(self.db, tray(uuid="other"))
        self.assertEqual("таблица материалов", catalog["source"])
        self.assertEqual(2500, catalog["price"])
        self.db.execute("DELETE FROM materials WHERE id='pla-cat'")
        fallback = ams_defaults.defaults(self.db, tray(uuid="other"))
        self.assertEqual("встроенный справочник", fallback["source"])
        self.assertGreater(fallback["price"], 0)

    def test_move_unbind_and_runout_keep_rfid_and_no_auto_archive(self):
        ams_sync.sync_ams_spools(self.db, "p1", snap(tray()))
        first = self.db.one("SELECT * FROM spools")
        moved = ams_sync.sync_ams_spools(self.db, "p1", snap(tray(slot=1)))
        self.assertEqual(0, moved["created"])
        self.assertEqual("1", self.db.one("SELECT * FROM spools")["ams_slot"])
        out = ams_sync.sync_ams_spools(self.db, "p1", snap(tray(slot=1, remain=0),
                                                            state="RUNNING"))
        self.assertIn("ams_runout", [n["event"] for n in out["alerts"]])
        spool = self.db.one("SELECT * FROM spools WHERE id=?", (first["id"],))
        self.assertEqual("", spool["ams_slot"])
        self.assertEqual("rfid-new", spool["tray_uuid"])
        self.assertEqual(0, spool["archived"])
        self.assertEqual(0, spool["remaining_grams"])
        again = ams_sync.sync_ams_spools(self.db, "p1", snap(tray(slot=1, remain=0),
                                                              state="RUNNING"))
        self.assertEqual([], again["alerts"])

    def test_unbind_reports_once_and_undo_keeps_rfid(self):
        ams_sync.sync_ams_spools(self.db, "p1", snap(tray()))
        removed = ams_sync.sync_ams_spools(self.db, "p1", snap(tray(present=False, material="", uuid="", remain=0)))
        self.assertEqual(1, removed["unbound"])
        self.assertIn("ams_unbind", [n["event"] for n in removed["alerts"]])
        self.assertEqual([], ams_sync.sync_ams_spools(
            self.db, "p1", snap(tray(present=False, material="", uuid="", remain=0)))["alerts"])
        row = self.db.one("SELECT * FROM spools")
        self.assertEqual("rfid-new", row["tray_uuid"])
        record = ams_actions.detail(self.db, removed["action_ids"][0])
        self.assertEqual("0", record["before"]["spools"][row["id"]]["ams_slot"])
        self.assertEqual("", record["after"]["spools"][row["id"]]["ams_slot"])
        undone = ams_actions.undo(self.db, record["id"])
        self.assertTrue(undone["ok"])
        self.assertEqual("0", self.db.one("SELECT * FROM spools")["ams_slot"])
        self.assertEqual("rfid-new", self.db.one("SELECT * FROM spools")["tray_uuid"])
        self.assertEqual("undo", ams_actions.list_actions(self.db)[0]["action"])
        with self.assertRaisesRegex(ValueError, "уже откатили"):
            ams_actions.undo(self.db, record["id"])

    def test_undo_refuses_stale_card_or_spent_new_card(self):
        result = ams_sync.sync_ams_spools(self.db, "p1", snap(tray()))
        action = result["action_ids"][0]
        sp = self.db.one("SELECT * FROM spools")
        self.db.execute("UPDATE spools SET brand='ручная правка' WHERE id=?", (sp["id"],))
        with self.assertRaisesRegex(ValueError, "уже изменена"):
            ams_actions.undo(self.db, action)
        self.db.execute("UPDATE spools SET brand='' WHERE id=?", (sp["id"],))
        self.db.upsert("print_jobs", {"id": "job1", "spool_id": sp["id"], "state": "done"})
        with self.assertRaisesRegex(ValueError, "используется в учёте"):
            ams_actions.undo(self.db, action)

    def test_ambiguous_generic_and_duplicate_rfid_do_not_guess_or_spam(self):
        # Две недавно стоявшие generic-катушки с одинаковыми типом/цветом.
        for ident in ("sp1", "sp2"):
            self.spool(id=ident, tray_uuid="", ams_slot="", location="shop")
            ams_sync.slot_event(self.db, "p1", "1", ident, "auto_unbind")
        observation = snap(tray(slot=1, uuid="", color="#FF4400"))
        one = ams_sync.sync_ams_spools(self.db, "p1", observation)
        self.assertEqual(0, one["created"])
        self.assertIn("ams_conflict", [n["event"] for n in one["alerts"]])
        self.assertEqual([], ams_sync.sync_ams_spools(self.db, "p1", observation)["alerts"])
        self.assertEqual("unrecognized", self.db.one("SELECT state FROM ams_slots")["state"])
        self.assertTrue(ams_doctor.doctor(self.db, "p1")["counts"]["error"])
        rfid = snap(tray(slot=0, uuid="tag-duplicate"), tray(slot=1, uuid="tag-duplicate"))
        self.assertEqual(0, ams_sync.sync_ams_spools(self.db, "p1", rfid)["created"])
        self.assertEqual(2, len(self.db.query("SELECT * FROM spools")))

    def test_generic_recent_window_delta_e_and_single_candidate(self):
        self.spool(tray_uuid="", ams_slot="", location="shop")
        ams_sync.slot_event(self.db, "p1", "0", "sp1", "auto_unbind")
        data = ams_sync.sync_ams_spools(self.db, "p1", snap(tray(uuid="", color="#FF4300")))
        self.assertEqual(0, data["created"])
        self.assertEqual("sp1", self.db.one("SELECT * FROM ams_slots")["spool_id"])
        self.db.execute("UPDATE spools SET ams_slot='', location='shop'")
        self.db.execute("UPDATE ams_slot_history SET at=?",
            ((datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat(),))
        self.db.execute("DELETE FROM ams_slots")
        data2 = ams_sync.sync_ams_spools(self.db, "p1", snap(tray(uuid="", color="#FF4400")))
        self.assertEqual(0, data2["created"])
        self.assertEqual("", self.db.one("SELECT * FROM ams_slots")["spool_id"])
        self.assertEqual(0, ams_sync.sync_ams_spools(self.db, "p1", snap(tray(uuid="", color="#0000FF")))["created"])

    def test_refill_loss_and_empty_alerts_only_on_transition(self):
        self.spool()
        ams_sync.sync_ams_spools(self.db, "p1", snap(tray()))
        up = ams_sync.sync_ams_spools(self.db, "p1", snap(tray(remain=99)))
        self.assertEqual("refill", ams_actions.list_actions(self.db)[0]["action"])
        down = ams_sync.sync_ams_spools(self.db, "p1", snap(tray(remain=40)))
        self.assertEqual("loss", ams_actions.list_actions(self.db)[0]["action"])
        self.assertIn("ams_loss", [n["event"] for n in down["alerts"]])
        self.assertEqual([], ams_sync.sync_ams_spools(self.db, "p1", snap(tray(remain=40)))["alerts"])
        self.db.set_settings({"ams_auto_spools": False})
        # Отсутствующая в складе катушка: память видит слот, затем пустоту.
        ams_sync.sync_ams_spools(self.db, "p1", snap(tray(slot=2, uuid="", material="PETG")))
        empty = ams_sync.sync_ams_spools(self.db, "p1", snap(tray(slot=2, uuid="", material="", present=False)))
        self.assertIn("ams_empty", [n["event"] for n in empty["alerts"]])
        self.assertEqual([], ams_sync.sync_ams_spools(
            self.db, "p1", snap(tray(slot=2, uuid="", material="", present=False)))["alerts"])

    def test_push_only_idle_dedup_restart_disable_conflict_and_refusal(self):
        self.spool(brand="Nozza", rec_settings='{"temp_nozzle":[205,225]}')
        self.db.upsert("ams_slots", {"id": "ams1", "printer_id": "p1", "slot": "0",
                                     "state": "live", "spool_id": "sp1", "tray_uuid": "rfid-new"})
        occupied = snap(tray())
        self.assertEqual(0, ams_push.push(self.db, self.printer, snap(tray(), state="RUNNING"))["sent"])
        self.assertEqual([], self.printer.calls)
        data = ams_push.push(self.db, self.printer, occupied)
        self.assertEqual(1, data["sent"])
        self.assertEqual("ams_filament", self.printer.calls[0][0])
        self.assertEqual((205, 225), (self.printer.calls[0][1]["temp_min"],
                                     self.printer.calls[0][1]["temp_max"]))
        self.assertEqual(0, ams_push.push(self.db, FakePrinter(occupied), occupied)["sent"])
        self.db.set_settings({"ams_push_settings": False})
        self.db.execute("DELETE FROM ams_push_state")
        self.assertEqual(0, ams_push.push(self.db, self.printer, occupied)["sent"])
        self.db.set_settings({"ams_push_settings": True})
        self.db.execute("UPDATE ams_slots SET state='unrecognized'")
        self.assertEqual(0, ams_push.push(self.db, self.printer, occupied)["sent"])
        self.db.execute("UPDATE ams_slots SET state='live'")
        self.printer.error = "отказ Bambu"
        failed = ams_push.push(self.db, self.printer, occupied)
        self.assertEqual(1, len(failed["errors"]))
        self.assertEqual("push_error", ams_actions.list_actions(self.db)[0]["action"])
        self.assertEqual(0, ams_doctor.doctor(self.db, "p1")["counts"]["error"])
        self.assertTrue(any(i["code"] == "push_error" for i in ams_doctor.doctor(self.db, "p1")["issues"]))

    def test_api_doctor_plan_rules_journal_and_tidy(self):
        status, data = self.api.post("/api/ams/rules/save", {"match_material": "PLA", "brand": "Владелец"}, {})
        self.assertEqual(200, status)
        rid = data["rule"]["id"]
        self.assertEqual(rid, self.api.get("/api/ams/rules", {})[1]["rules"][0]["id"])
        self.assertEqual([], self.api.get("/api/ams/actions", {})[1]["actions"])
        self.db.set_settings({"telegram_enabled": True, "telegram_token": "fake"})
        status, updated = self.api.post("/api/ams/tidy", {"printer_id": "p1"}, {})
        self.assertEqual(200, status)
        self.assertEqual(1, updated["created"])
        self.assertIn("doctor", updated)
        actions = self.api.get("/api/ams/actions", {"printer_id": ["p1"]})[1]["actions"]
        self.assertTrue(actions)
        before_after = self.api.get("/api/ams/actions/detail", {"id": [actions[-1]["id"]]})[1]
        self.assertIn("before", before_after)
        self.assertEqual([], self.sent)  # создание не входит в пять Telegram-событий
        plan = self.api.post("/api/ams/plan", {"printer_id": "p1",
            "materials": [{"material": "PLA", "grams": 100}]}, {})[1]
        self.assertEqual("Уже в AMS", plan["advice"][0]["suggestion"])
        self.assertEqual("Материалы", subscriptions.EVENTS["ams_unbind"][1])
        self.assertEqual("Печать", subscriptions.EVENTS["ams_runout"][1])
        self.api.post("/api/ams/rules/delete", {"id": rid}, {})
        self.assertEqual([], self.api.get("/api/ams/rules", {})[1]["rules"])

    def test_manual_unbind_does_not_claim_physical_tray_is_empty_or_rebind_it(self):
        self.spool(ams_slot="", location="shop", tray_uuid="")
        self.api.post("/api/spool/bind", {"id": "sp1", "printer_id": "p1",
                                          "ams_slot": "0", "push_ams": False}, {})
        self.api.post("/api/spool/bind", {"id": "sp1", "printer_id": "p1",
                                          "ams_slot": ""}, {})
        memory = ams_sync.slot_memory(self.db, "p1")[0]
        self.assertEqual("live", memory["state"])
        self.assertEqual("", memory["spool_id"])
        self.assertIn("unbound", [i["code"] for i in
                      ams_doctor.doctor(self.db, "p1")["issues"]])
        ams_sync.sync_ams_spools(self.db, "p1", snap(tray(uuid="")))
        self.assertEqual("", self.db.one("SELECT * FROM spools WHERE id='sp1'")["ams_slot"])

    def test_manual_resolution_of_ambiguous_generic_is_stable_until_swap(self):
        self.spool(tray_uuid="", ams_slot="", location="shop")
        self.spool(id="sp2", tray_uuid="", ams_slot="", location="shop")
        ams_sync.slot_event(self.db, "p1", "0", "sp2", "auto_unbind", "Ранее лежала")
        code, answer = self.api.post("/api/spool/bind", {
            "id": "sp1", "ams_slot": "0", "printer_id": "p1", "push_ams": False}, {})
        self.assertEqual(200, code)
        self.assertEqual("sp1", answer["spool"]["id"])
        generic = snap(tray(uuid="", material="PLA"))
        result = ams_sync.sync_ams_spools(self.db, "p1", generic)
        self.assertEqual([], result["alerts"])
        self.assertEqual("sp1", ams_sync.slot_memory(self.db, "p1")[0]["spool_id"])
        self.assertEqual("0", self.db.one("SELECT * FROM spools WHERE id='sp1'")["ams_slot"])
        # Подмена материалом без RFID снимает прежнюю автокатушку, новую не
        # угадывает и даёт возможность отменить решение с двух снимков.
        changed = ams_sync.sync_ams_spools(self.db, "p1", snap(
            tray(uuid="", material="PETG", color="#00AA55")))
        self.assertEqual(1, changed["unbound"])
        self.assertIn("ams_conflict", [a["event"] for a in changed["alerts"]])
        self.assertEqual("", self.db.one("SELECT * FROM spools WHERE id='sp1'")["ams_slot"])
        action = ams_actions.feed(self.db, "p1")[0]
        self.assertEqual("auto_unbind", action["action"])
        ams_actions.undo(self.db, action["id"])
        self.assertEqual("0", self.db.one("SELECT * FROM spools WHERE id='sp1'")["ams_slot"])

    def test_runout_when_slot_disappears_while_printing_no_double_alert(self):
        self.db.set_settings({"telegram_enabled": True, "telegram_token": "fake"})
        self.printer.current = snap(tray())
        tidy(self.db, self.printer, self.manager)
        before = len(self.sent)
        empty = snap(tray(present=False, remain=0, uuid="", material=""), state="RUNNING")
        self.printer.current = empty
        tidy(self.db, self.printer, self.manager)
        self.assertEqual(before + 1, len(self.sent))
        self.assertEqual("ams_runout", self.sent[-1][1]["event"])
        tidy(self.db, self.printer, self.manager)
        self.assertEqual(before + 1, len(self.sent))
        self.assertEqual("rfid-new", self.db.one("SELECT * FROM spools")["tray_uuid"])

    def test_recent_completed_print_is_not_flagged_as_inventory_loss(self):
        self.spool(remaining_grams=900)
        self.printer.current = snap(tray(remain=90))
        ams_sync.sync_ams_spools(self.db, "p1", self.printer.current)
        self.db.upsert("print_jobs", {
            "id": "job1", "printer_id": "p1", "spool_id": "sp1",
            "state": "done", "finished_at": now_iso()})
        later = ams_sync.sync_ams_spools(self.db, "p1", snap(tray(remain=30)))
        self.assertNotIn("ams_loss", [a["event"] for a in later["alerts"]])
        self.assertEqual(300, self.db.one("SELECT * FROM spools")["remaining_grams"])

    def test_stale_telemetry_blocks_all_manual_hardware_writes(self):
        import time
        self.printer.current["connection"]["last_message"] = time.time() - 120
        with self.assertRaisesRegex(ValueError, "устарели"):
            self.api.post("/api/printer/command", {"printer_id": "p1",
                "command": "ams_filament", "value": {"ams_id": 0, "tray_id": 0,
                "type": "PLA", "color": "#FF4400"}, "confirmed": True}, {})
        self.assertEqual([], self.printer.calls)

    def test_force_bind_without_confirmation_does_not_eject_old_card(self):
        self.spool()
        self.spool(id="sp2", tray_uuid="other", ams_slot="1")
        with self.assertRaisesRegex(ValueError, "Подтвердите"):
            self.api.post("/api/spool/bind", {"id": "sp2", "printer_id": "p1",
                "ams_slot": "0", "force": True, "push_ams": True}, {})
        self.assertEqual("0", self.db.one("SELECT * FROM spools WHERE id='sp1'")["ams_slot"])
        self.assertEqual("1", self.db.one("SELECT * FROM spools WHERE id='sp2'")["ams_slot"])
        self.assertEqual([], self.printer.calls)

    def test_legacy_duplicate_rfid_detected_across_printers_without_rewriting(self):
        self.spool(tray_uuid="same", ams_slot="")
        self.spool(id="sp2", tray_uuid="same", printer_id="p2", ams_slot="")
        self.assertIn("duplicate_rfid", [i["code"] for i in
                      ams_doctor.doctor(self.db, "p1")["issues"]])
        result = ams_sync.sync_ams_spools(self.db, "p1", snap(tray(uuid="same")))
        self.assertIn("ams_conflict", [a["event"] for a in result["alerts"]])
        self.assertEqual("", self.db.one("SELECT * FROM spools WHERE id='sp1'")["ams_slot"])
        self.assertEqual("", self.db.one("SELECT * FROM spools WHERE id='sp2'")["ams_slot"])

    def test_single_gateway_deduplicates_manual_profile_and_schedule(self):
        from connector.printflow.manager import PrinterManager
        self.db.upsert("ams_profiles", {
            "id": "p", "name": "Два модуля",
            "slots": '[{"tray":0,"type":"PLA","color":"FF4400","brand":""},'
                     '{"tray":4,"type":"PLA","color":"FF4400","brand":""}]'})
        body = {"id": "p", "printer_id": "p1", "confirmed": True}
        self.assertEqual(2, self.api.post("/api/ams-profile/apply", body, {})[1]["sent"])
        self.assertEqual([0, 1], [v["ams_id"] for n, v in self.printer.calls])
        self.assertEqual(2, self.api.post("/api/ams-profile/apply", body, {})[1]["skipped"])
        same = {"ams_id": 1, "tray_id": 0, "type": "PLA", "color": "#FF4400", "brand": ""}
        direct = self.api.post("/api/printer/command", {
            "printer_id": "p1", "command": "ams_filament", "value": same,
            "confirmed": True}, {})[1]
        self.assertTrue(direct["skipped"])
        self.db.set_settings({"unattended_dangerous_actions": True})
        self.db.upsert("scheduled_commands", {"id": "sch1", "at": now_iso(),
            "printer_id": "p1", "command": "ams_filament", "value": __import__("json").dumps(same),
            "done": 0})
        self.manager.db = self.db
        PrinterManager.run_scheduled(self.manager)
        self.assertEqual(2, len(self.printer.calls))
        self.assertIn("30 минут", self.db.one("SELECT result FROM scheduled_commands WHERE id='sch1'")["result"])
        self.assertEqual(2, len([a for a in ams_actions.feed(self.db) if a["action"] == "push"]))
        self.printer.error = "отказ MQTT"
        failed = dict(same, color="#AA4400")
        with self.assertRaisesRegex(RuntimeError, "отказ MQTT"):
            self.api.post("/api/printer/command", {"printer_id": "p1",
                "command": "ams_filament", "value": failed, "confirmed": True}, {})
        self.assertEqual("push_error", ams_actions.feed(self.db)[0]["action"])

    def test_real_mqtt_slot_numbers_across_two_ams_modules(self):
        from connector.printflow.bambu import parse_ams_trays
        raw = {"tray_now": "4", "ams": [
            {"id": 0, "tray_exist_bits": "1", "tray": [
                {"id": 0, "tray_type": "PLA", "tray_uuid": "rf0",
                 "tray_color": "FF0000FF", "remain": 80}]},
            {"id": 1, "tray_exist_bits": "1", "tray": [
                {"id": 0, "tray_type": "PETG", "tray_uuid": "rf1",
                 "tray_color": "00FF00FF", "remain": 75}]},
        ]}
        trays = parse_ams_trays(raw)
        first = next(t for t in trays if t.get("uuid") == "rf0")
        second = next(t for t in trays if t.get("uuid") == "rf1")
        self.assertFalse(first["active"])
        self.assertTrue(second["active"])
        self.assertEqual(0, first["slot"])
        self.assertEqual(1, second["unit"])
        payload = snap(*trays)
        self.printer.current = payload
        result = tidy(self.db, self.printer, self.manager)
        self.assertEqual(2, result["created"])
        slots = {s["tray_uuid"]: s["ams_slot"] for s in self.db.query("SELECT * FROM spools")}
        self.assertEqual({"rf0": "0", "rf1": "4"}, slots)
        command = next(value for name, value in self.printer.calls
                       if name == "ams_filament" and value["ams_id"] == 1)
        self.assertEqual(0, command["tray_id"])
        blocks, infos = [], []
        check_ams_filament(payload, {"material": "PETG"}, [4], blocks, infos)
        self.assertEqual([], blocks)
        second["remain"] = 0
        check_ams_filament(snap(first, second), {"material": "PETG"}, [4], blocks, infos)
        self.assertEqual(["ams_empty"], [x["code"] for x in blocks])
        external = {"unit": 255, "slot": 254, "uuid": "ext", "type": "PLA",
                    "remain": 80, "color": "#FF0000", "present": True}
        self.assertEqual(0, ams_sync.sync_ams_spools(self.db, "p1", snap(external))["created"])

    def test_automatic_placeholder_color_is_never_pushed_as_real(self):
        unknown = snap(tray(color="#CBD5E1"))
        self.printer.current = unknown
        first = tidy(self.db, self.printer, self.manager)
        self.assertEqual(1, first["created"])
        self.assertEqual(0, first["push"]["sent"])
        self.assertEqual([], self.printer.calls)
        self.assertIn("color_unknown", [i["code"] for i in
                      ams_doctor.doctor(self.db, "p1", unknown)["issues"]])
        updated = snap(tray(color="#AA5500"))
        result = tidy(self.db, self.printer, self.manager, updated)
        self.assertEqual(1, result["push"]["sent"])
        self.assertEqual("#AA5500", self.db.one("SELECT * FROM spools")["color_hex"])

    def test_doctor_detects_hms_humidity_and_live_mismatch(self):
        self.spool(tray_uuid="rfid-stored")
        view = snap(tray(uuid="rfid-live", material="PETG", color="#0000FF"), humidity=75)
        view["printer"]["problems"] = [{"code": "0700-8001", "title": "Заклинил привод",
                                         "severity": "error", "advice": "Проверить мотор"}]
        issues = ams_doctor.doctor(self.db, "p1", view)["issues"]
        codes = {i["code"] for i in issues}
        self.assertTrue({"hardware", "humidity", "rfid_mismatch", "material_mismatch",
                         "color_mismatch"} <= codes)

    def test_five_telegram_events_use_topics_and_runout_is_critical(self):
        self.db.set_settings({"telegram_enabled": True, "telegram_token": "fake"})
        self.printer.current = snap(tray())
        tidy(self.db, self.printer, self.manager)
        self.assertEqual([], self.sent)  # создание — не значимое уведомление
        self.printer.current = snap(tray(remain=0), state="RUNNING")
        tidy(self.db, self.printer, self.manager)
        self.assertEqual("ams_runout", self.sent[0][1]["event"])
        self.assertTrue(self.sent[0][1]["critical"])
        self.printer.current = snap(tray(present=False, material="", uuid="", remain=0))
        tidy(self.db, self.printer, self.manager)
        self.assertEqual(1, len(self.sent))  # после runout пустота не дублирует сообщение

    def test_disabled_or_busy_blocks_manual_and_scheduled_commands_not_binding(self):
        self.spool()
        self.db.set_settings({"ams_push_settings": False})
        with self.assertRaisesRegex(ValueError, "отключена"):
            self.api.post("/api/printer/command", {"printer_id": "p1", "command": "ams_filament",
                                                   "value": {"type": "PLA"}, "confirmed": True}, {})
        self.assertEqual([], self.printer.calls)
        profile = self.db.upsert("ams_profiles", {"id": "profile1", "name": "PLA",
                                                 "slots": '[{"tray":0,"type":"PLA"}]'})
        with self.assertRaisesRegex(ValueError, "отключена"):
            self.api.post("/api/ams-profile/apply", {"id": profile["id"],
                                                     "printer_id": "p1", "confirmed": True}, {})
        result = self.api.post("/api/spool/bind", {"id": "sp1", "printer_id": "p1",
                                                   "ams_slot": "1", "push_ams": True}, {})[1]
        self.assertFalse(result["pushed"])
        self.assertEqual("rfid-new", self.db.one("SELECT * FROM spools")["tray_uuid"])
        self.db.set_settings({"ams_push_settings": True})
        self.printer.current = snap(tray(), state="RUNNING")
        with self.assertRaisesRegex(ValueError, "занят"):
            self.api.post("/api/printer/command", {"printer_id": "p1", "command": "ams_filament",
                                                   "value": {"type": "PLA"}, "confirmed": True}, {})
        self.assertEqual([], self.printer.calls)
        self.assertEqual(0, ams_push.push(self.db, self.printer, self.printer.current)["sent"])

    def test_full_preflight_blocks_empty_second_module_and_busy_without_label(self):
        self.printer.current = snap(tray(slot=0), {**tray(slot=0, remain=0), "unit": 1})
        result = check_preflight(self.db, self.manager, "p1", "not-uploaded.gcode",
                                 ams_mapping=[4])
        self.assertFalse(result["ok"])
        self.assertIn("ams_empty", [b["code"] for b in result["blocks"]])
        self.printer.current = {"printer": {"state": "RUNNING"},
                                "connection": {"connected": True}}
        result = check_preflight(self.db, self.manager, "p1", "not-uploaded.gcode")
        self.assertIn("busy", [b["code"] for b in result["blocks"]])

    def test_preflight_block_empty_wrong_material_and_error_is_note(self):
        blocks, infos = [], []
        check_ams_filament(snap(tray(remain=0)), {"material": "PLA"}, [0], blocks, infos)
        self.assertEqual(["ams_empty"], [b["code"] for b in blocks])
        blocks, infos = [], []
        check_ams_filament(snap(tray(material="PETG")), {"material": "PLA"},
                           [0], blocks, infos)
        self.assertEqual(["ams_material"], [b["code"] for b in blocks])
        blocks, infos = [], []
        check_ams_filament(snap(tray()), {"material": "PLA"}, "bad mapping", blocks, infos)
        self.assertFalse(blocks)
        self.assertEqual(["ams_check_error"], [i["code"] for i in infos])


if __name__ == "__main__":
    unittest.main()

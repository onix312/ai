"""PrintFlow 9.0: схема 12, цех, приход, AMS, QR, preflight влажности."""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import APP_VERSION  # noqa: E402
from connector.printflow.db import Database, SCHEMA_VERSION  # noqa: E402
from connector.printflow.qrgen import svg  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402
from connector.printflow.workshop_v9 import WorkshopV9, heartbeat_channels  # noqa: E402
from connector.printflow.materials import is_hygroscopic  # noqa: E402
from connector.tests.test_phase11 import make_api, make_db  # noqa: E402


class VersionTests(unittest.TestCase):
    def test_app_and_schema(self):
        self.assertEqual(APP_VERSION, "15.6.0")
        self.assertEqual(SCHEMA_VERSION, 16)


class Schema12Tests(unittest.TestCase):
    def test_fresh_db_has_v9_tables_and_columns(self):
        db = make_db()
        self.addCleanup(db.close)
        tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in ("workshop_docs", "ams_slot_history", "filament_scrap",
                     "suppliers", "plate_presets", "shift_checks"):
            self.assertIn(name, tables)
        spool_cols = {r["name"] for r in db.query("PRAGMA table_info(spools)")}
        self.assertTrue({"location", "price_per_kg", "received_doc_id"} <= spool_cols)
        job_cols = {r["name"] for r in db.query("PRAGMA table_info(print_jobs)")}
        self.assertTrue({"start_request_id", "mixed_label", "no_auto"} <= job_cols)
        indexes = {r["name"] for r in db.query("PRAGMA index_list(workshop_docs)")}
        self.assertIn("idx_workshop_docs_request", indexes)

    def test_migrate_schema_11_to_12(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "old.sqlite3"
            conn = sqlite3.connect(path)
            conn.executescript(
                "CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "PRAGMA user_version=11;"
            )
            conn.close()
            db = Database(path)
            self.addCleanup(db.close)
            self.assertEqual(db.conn.execute("PRAGMA user_version").fetchone()[0], 16)
            cols = {r["name"] for r in db.query("PRAGMA table_info(spools)")}
            self.assertIn("location", cols)
            job_cols = {r["name"] for r in db.query("PRAGMA table_info(print_jobs)")}
            self.assertIn("no_auto", job_cols)

    def test_migrate_schema_12_to_13(self):
        """Схема 13: у номенклатуры появляется печатная группа мелких товаров."""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "old12.sqlite3"
            conn = sqlite3.connect(path)
            conn.executescript(
                "CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "PRAGMA user_version=12;"
            )
            conn.close()
            db = Database(path)
            self.addCleanup(db.close)
            self.assertEqual(db.conn.execute("PRAGMA user_version").fetchone()[0], 16)
            nom_cols = {r["name"] for r in db.query("PRAGMA table_info(nomenclature)")}
            self.assertIn("print_group", nom_cols)


class WorkshopTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.repo = Repo(self.db)
        self.w = WorkshopV9(self.db, self.repo)

    def tearDown(self):
        self.db.close()

    def test_location_is_label_not_warehouse(self):
        spool = self.repo.save_spool({"material": "PLA", "color_name": "Чёрный",
                                      "remaining_grams": 800, "total_grams": 1000})
        out = self.w.set_spool_location(spool["id"], "home", "шкаф")
        self.assertEqual(out["spool"]["location"], "home")
        with self.assertRaises(ValueError):
            self.w.set_spool_location(spool["id"], "warehouse")

    def test_unique_ams_slot(self):
        a = self.repo.save_spool({"material": "PETG", "color_name": "A",
                                  "remaining_grams": 500, "total_grams": 1000})
        b = self.repo.save_spool({"material": "PETG", "color_name": "B",
                                  "remaining_grams": 500, "total_grams": 1000})
        self.w.bind_unique_slot(a["id"], 0, printer_id="p1")
        with self.assertRaises(ValueError):
            self.w.bind_unique_slot(b["id"], 0, printer_id="p1")
        evicted = self.w.bind_unique_slot(b["id"], 0, printer_id="p1", force=True)
        self.assertEqual(evicted["evicted"], a["id"])
        self.assertEqual(self.repo.spool(a["id"])["ams_slot"], "")

    def test_scrap_needs_confirm_and_is_idempotent(self):
        spool = self.repo.save_spool({"material": "PLA", "remaining_grams": 100,
                                      "total_grams": 1000})
        with self.assertRaises(ValueError):
            self.w.record_scrap(spool["id"], 10, confirmed=False)
        first = self.w.record_scrap(spool["id"], 10, confirmed=True, request_id="scr-1")
        second = self.w.record_scrap(spool["id"], 10, confirmed=True, request_id="scr-1")
        self.assertFalse(first["already"])
        self.assertTrue(second["already"])
        self.assertAlmostEqual(self.repo.spool(spool["id"])["remaining_grams"], 90)

    def test_filament_receipt_document(self):
        out = self.w.filament_receipt(
            material="PETG", color_name="Синий", spool_count=2, spool_grams=1000,
            total_amount=4000, confirmed=True, request_id="fr-1")
        self.assertEqual(out["document"]["kind"], "filament_receipt")
        self.assertEqual(len(out["spools"]), 2)
        again = self.w.filament_receipt(
            material="PETG", confirmed=True, request_id="fr-1")
        self.assertTrue(again["already"])
        self.assertEqual(len(self.repo.spools()), 2)

    def test_mixed_label_and_no_auto(self):
        self.db.upsert("print_jobs", {"id": "j1", "name": "плита", "state": "queued",
                                      "file": "a.3mf"})
        label = self.w.mixed_plate_label([{"name": "Адресник", "qty": 4},
                                          {"name": "Бирка", "qty": 2}], 2)
        self.assertIn("Адресник ×4", label)
        self.w.attach_mixed_label("j1", label=label)
        job = self.db.one("SELECT * FROM print_jobs WHERE id='j1'")
        self.assertEqual(job["mixed_label"], label)
        self.w.set_no_auto("j1", True)
        self.assertEqual(self.db.one("SELECT no_auto FROM print_jobs WHERE id='j1'")["no_auto"], 1)

    def test_qr_and_svg_label(self):
        spool = self.repo.save_spool({"material": "PLA", "color_name": "Белый",
                                      "remaining_grams": 400, "total_grams": 1000})
        info = self.w.qr_wizard(spool["id"])
        self.assertTrue(info["payload"].startswith("pf:spool:"))
        mark = svg(info["payload"], scale=3)
        self.assertIn("<svg", mark)
        html = self.w.spool_label_html(spool["id"])
        self.assertIn("<svg", html)

    def test_supplier_price_and_preset(self):
        spool = self.repo.save_spool({"material": "PETG", "remaining_grams": 900,
                                      "total_grams": 1000})
        sup = self.w.save_supplier({"name": "Пластик.ру", "price_per_kg": 2100})
        self.w.apply_supplier_price(sup["id"], "PETG")
        self.assertEqual(self.repo.spool(spool["id"])["price_per_kg"], 2100)
        preset = self.w.save_plate_preset({"name": "Ночь", "use_ams": True,
                                           "timelapse": True, "plate": 2})
        self.db.upsert("print_jobs", {"id": "j2", "name": "x", "state": "queued", "file": "a.3mf"})
        applied = self.w.apply_plate_preset("j2", preset["id"])
        self.assertEqual(applied["job"]["plate"], 2)

    def test_start_request_id_unique(self):
        self.db.upsert("print_jobs", {
            "id": "j-start-a", "state": "queued", "file": "a.3mf",
            "start_request_id": "sr-1",
        })
        with self.assertRaises(Exception):
            self.db.upsert("print_jobs", {
                "id": "j-start-b", "state": "queued", "file": "b.3mf",
                "start_request_id": "sr-1",
            })
        claimed = self.w.claim_start_request("sr-1")
        self.assertEqual(claimed["id"], "j-start-a")

    def test_inventory_low_and_enough(self):
        self.repo.save_spool({"material": "PLA", "remaining_grams": 50,
                              "total_grams": 1000, "color_name": "A"})
        summary = self.w.inventory_summary()
        self.assertGreaterEqual(summary["low_count"], 1)
        empty = self.w.enough_for_next()
        self.assertTrue(empty["enough"])


class HygroscopicTests(unittest.TestCase):
    def test_pla_not_critical_pc_is(self):
        self.assertFalse(is_hygroscopic("PLA"))
        self.assertTrue(is_hygroscopic("PC"))
        self.assertTrue(is_hygroscopic("PAHT_CF"))
        self.assertTrue(is_hygroscopic("PETG"))
        self.assertTrue(is_hygroscopic("TPU"))


class PreflightHumidityTests(unittest.TestCase):
    def test_humidity_warns_only_hygroscopic(self):
        from connector.printflow.preflight import check_preflight
        db = make_db()
        self.addCleanup(db.close)
        db.set_settings({"preflight_enabled": True, "preflight_warn_humidity": True,
                         "dry_humidity_threshold": 40})

        class P:
            id = "p1"
            record = {"nozzle_size": 0.4}

            def snapshot(self):
                return {
                    "printer": {"state": "IDLE", "state_label": "свободен",
                                "problems": [], "sdcard": True, "nozzle_size": 0.4},
                    "ams": {"humidity": 70, "trays": [{"type": "PLA", "slot": 0, "active": True}]},
                    "maintenance": {},
                }

        manager = types.SimpleNamespace(get=lambda pid: P())
        out = check_preflight(db, manager, "p1", "missing.3mf")
        self.assertFalse(any(w["code"] == "humidity" for w in out["warns"]))

        class P2(P):
            def snapshot(self):
                snap = super().snapshot()
                snap["ams"]["trays"] = [{"type": "PETG", "slot": 0, "active": True}]
                return snap

        manager2 = types.SimpleNamespace(get=lambda pid: P2())
        out2 = check_preflight(db, manager2, "p1", "missing.3mf")
        self.assertTrue(any(w["code"] == "humidity" for w in out2["warns"]))


class PreflightBedClearTests(unittest.TestCase):
    """Я40: стол чист до старта. Без сети, без живой камеры."""

    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.db.set_settings({
            "preflight_enabled": True,
            "preflight_block_bed": True,
            "preflight_warn_humidity": False,
            "bed_watch_threshold": 6.0,
        })
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.photo = pathlib.Path(self.tmp.name)

    def _printer(self, frame=None):
        class P:
            id = "p1"
            record = {"nozzle_size": 0.4}
            camera = types.SimpleNamespace(frame=frame)

            def snapshot(self):
                return {
                    "printer": {"state": "IDLE", "state_label": "свободен",
                                "problems": [], "sdcard": True, "nozzle_size": 0.4},
                    "ams": {"humidity": 20, "trays": []},
                    "maintenance": {},
                }
        return P()

    def test_no_reference_does_not_block(self):
        from connector.printflow.preflight import check_preflight
        with mock.patch("connector.printflow.config.PHOTO_DIR", self.photo):
            out = check_preflight(self.db, types.SimpleNamespace(get=lambda pid: self._printer()),
                                  "p1", "missing.3mf")
        self.assertFalse(any(b["code"] == "bed_dirty" for b in out["blocks"]))
        self.assertTrue(any(i["code"] == "bed_no_ref" for i in out["infos"]))

    def test_dirty_bed_blocks(self):
        from connector.printflow.preflight import check_preflight
        (self.photo / "bed_reference.jpg").write_bytes(b"ref-bytes")
        printer = self._printer(frame=b"live-frame")
        with mock.patch("connector.printflow.config.PHOTO_DIR", self.photo), \
             mock.patch("connector.printflow.spaghetti.frame_diff_ratio", return_value=42.0):
            out = check_preflight(
                self.db, types.SimpleNamespace(get=lambda pid: printer),
                "p1", "missing.3mf")
        self.assertTrue(any(b["code"] == "bed_dirty" for b in out["blocks"]))
        self.assertFalse(out["ok"])

    def test_clear_bed_allows_start(self):
        from connector.printflow.preflight import check_preflight
        (self.photo / "bed_reference.jpg").write_bytes(b"ref-bytes")
        printer = self._printer(frame=b"live-frame")
        with mock.patch("connector.printflow.config.PHOTO_DIR", self.photo), \
             mock.patch("connector.printflow.spaghetti.frame_diff_ratio", return_value=1.2):
            out = check_preflight(
                self.db, types.SimpleNamespace(get=lambda pid: printer),
                "p1", "missing.3mf")
        self.assertFalse(any(b["code"] == "bed_dirty" for b in out["blocks"]))
        self.assertTrue(out["ok"])

    def test_setting_off_skips(self):
        from connector.printflow.preflight import check_preflight
        self.db.set_settings({"preflight_block_bed": False})
        (self.photo / "bed_reference.jpg").write_bytes(b"not-a-jpeg")
        with mock.patch("connector.printflow.config.PHOTO_DIR", self.photo):
            out = check_preflight(
                self.db, types.SimpleNamespace(get=lambda pid: self._printer(b"x")),
                "p1", "missing.3mf")
        self.assertFalse(any(b["code"] == "bed_dirty" for b in out["blocks"]))


class ApiDispatchTests(unittest.TestCase):
    def test_workshop_about_and_files_crumbs(self):
        api = make_api(make_db())
        from connector.printflow.workshop_v9 import WorkshopV9
        api.workshop = WorkshopV9(api.db, Repo(api.db))
        code, payload = api.get("/api/workshop/about", {})
        self.assertEqual(code, 200)
        self.assertEqual(payload["version"], "15.6.0")

        class Files:
            def list_files(self, path="/"):
                return [{"name": "cache", "path": "/cache", "dir": True, "kind": "dir",
                         "printable": False, "size": 0}]

        printer = types.SimpleNamespace(record={"host": "1.2.3.4", "access_code": "x"},
                                        files=Files())
        api.printer_or_fail = lambda pid: printer
        code, payload = api.get("/api/printer/files", {"printer_id": ["p1"], "path": ["/"]})
        self.assertEqual(code, 200)
        self.assertEqual(payload["crumbs"][0]["path"], "/")
        self.assertTrue(payload["files"][0]["dir"])

    def test_print_guard_on_enqueue(self):
        api = make_api(make_db())
        api.manager.enqueue = mock.Mock()
        with self.assertRaises(ValueError):
            api.post("/api/jobs/enqueue", {"file": "/timelapse/x.3mf"}, {})
        api.manager.enqueue.assert_not_called()

    def test_heartbeat_has_channels(self):
        api = make_api(make_db())
        api.manager.printers = {}
        api.manager.bot = None
        hb = api._heartbeat()
        self.assertIn("mqtt", hb)
        self.assertIn("ftps", hb)
        self.assertIn("disk", hb)


class HeartbeatDiskTests(unittest.TestCase):
    def test_channels_include_disk(self):
        manager = types.SimpleNamespace(printers={})
        ch = heartbeat_channels(manager, None)
        self.assertIn("disk", ch)
        self.assertIn("ok", ch["disk"])


class ShoppingReceiptDocTests(unittest.TestCase):
    def test_receive_writes_workshop_doc_without_new_kwargs(self):
        from connector.printflow.shopping import ShoppingList
        db = make_db()
        self.addCleanup(db.close)
        db.upsert("accounts", {"id": "cash", "name": "Касса", "archived": 0})
        shop = ShoppingList(db)
        item = shop.add({"name": "PETG", "material": "PETG", "qty": 1})
        out = shop.receive(
            item["id"], received_confirmed=True, payment_confirmed=True,
            material="PETG", color_name="Чёрный", spool_count=1, spool_grams=1000,
            total_amount=1600, account_id="cash", request_id="recv-1",
        )
        self.assertFalse(out["already_received"])
        docs = db.query("SELECT * FROM workshop_docs")
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["kind"], "filament_receipt")
        spool = db.one("SELECT * FROM spools")
        self.assertEqual(spool["received_doc_id"], docs[0]["id"])
        self.assertEqual(spool["location"], "shop")


if __name__ == "__main__":
    unittest.main()

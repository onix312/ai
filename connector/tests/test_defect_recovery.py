"""Брак: подтверждённая причина, фактические потери и один безопасный повтор."""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting
from connector.printflow.api import Api
from connector.printflow.defect_recovery import DefectRecovery
from connector.printflow.manager import PrinterManager
from connector.printflow.db import Database


class DefectRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "defects.sqlite3")
        self.manager = PrinterManager.__new__(PrinterManager)
        self.manager.db = self.db
        self.service = DefectRecovery(self.db, self.manager)
        self.db.upsert("orders", {
            "id": "order-1", "number": "1001", "product": "Корпус",
            "status": "queue", "material": "PETG", "price": 1200,
        })
        self.db.upsert("spools", {
            "id": "spool-1", "material": "PETG", "color_name": "Белый",
            "total_grams": 1000, "remaining_grams": 700, "price": 1500,
            "archived": 0,
        })
        self.db.upsert("print_jobs", {
            "id": "job-1", "printer_id": "printer-1", "order_id": "order-1",
            "name": "Корпус.3mf", "file": "Корпус.3mf", "state": "failed",
            "source": "printer", "spool_id": "spool-1", "grams": 100,
            "duration_min": 60, "energy_kwh": 0.2, "cost": 180,
            "finished_at": "2026-08-21T10:00:00+03:00",
            "accounted_at": "2026-08-21T10:00:01+03:00",
            "created_at": "2026-08-21T09:00:00+03:00",
        })
        self.db.execute(
            "INSERT INTO filament_usage(at,spool_id,job_id,order_id,grams,cost,note,auto)"
            " VALUES(?,?,?,?,?,?,?,?)",
            ("2026-08-21T10:00:00+03:00", "spool-1", "job-1", "order-1",
             100, 50, "test", 1),
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def recover(self, **overrides):
        data = {
            "defect_confirmed": True,
            "reason": "detached",
            "phase": "middle",
            "note": "Стол очищен",
            "lost_grams": 100,
            "request_id": "defect-request-1",
        }
        data.update(overrides)
        return self.service.recover("job-1", **data)

    def test_summary_uses_job_facts_without_new_write(self):
        result = self.service.summary("job-1")
        self.assertEqual(result["loss"]["grams"], 100)
        self.assertEqual(result["loss"]["filament"], 50)
        self.assertEqual(result["loss"]["energy"], 1.2)
        self.assertEqual(result["loss"]["wear"], 15)
        self.assertEqual(result["loss"]["total"], 66.2)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM defects")["n"], 0)

    def test_confirmation_is_required_and_other_needs_note(self):
        with self.assertRaisesRegex(ValueError, "Подтвердите"):
            self.recover(defect_confirmed=False)
        with self.assertRaisesRegex(ValueError, "комментарий"):
            self.recover(reason="other", note="")
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM defects")["n"], 0)

    def test_recovery_records_analytical_loss_without_double_consumption(self):
        before_spool = self.db.one("SELECT remaining_grams FROM spools WHERE id='spool-1'")
        before_usage = self.db.one("SELECT COUNT(*) n FROM filament_usage")["n"]
        result = self.recover()
        self.assertFalse(result["already_recorded"])
        self.assertTrue(result["loss_already_accounted"])
        self.assertEqual(result["defect"]["loss"], 66.2)
        self.assertEqual(result["defect"]["loss_source"], "filament-usage")
        self.assertTrue(result["defect"]["confirmed_at"])
        self.assertEqual(
            self.db.one("SELECT remaining_grams FROM spools WHERE id='spool-1'"),
            before_spool,
        )
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM filament_usage")["n"], before_usage)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM transactions")["n"], 0)
        report = Accounting(self.db).defects_cost(30)
        self.assertEqual(report["grams"], 100)
        self.assertEqual(report["cost"], 66.2)

    def test_confirmed_reprint_is_single_clean_clone_and_not_auto_started(self):
        result = self.recover(reprint_confirmed=True)
        repeat = result["repeat_job"]
        self.assertEqual(repeat["state"], "queued")
        self.assertEqual(repeat["source"], "defect-recovery")
        self.assertEqual(repeat["reprint_of_job_id"], "job-1")
        self.assertEqual(repeat["defect_id"], result["defect"]["id"])
        self.assertEqual(repeat.get("accounted_at"), "")
        self.assertEqual(repeat.get("remote_task_id"), "")
        self.assertEqual(repeat.get("grams"), 0)
        self.assertEqual(repeat.get("cost"), 0)
        self.db.set_settings({"auto_queue": True})
        self.assertIsNone(self.manager.next_job("printer-1"))

        retry = self.recover(reprint_confirmed=True)
        self.assertTrue(retry["already_recorded"])
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) n FROM print_jobs WHERE reprint_of_job_id='job-1'"
        )["n"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM defects")["n"], 1)

    def test_reprint_without_reason_is_blocked(self):
        with self.assertRaisesRegex(ValueError, "причину брака"):
            self.manager.reprint_job(
                "job-1", confirmed=True, request_id="unsafe-repeat"
            )
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) n FROM print_jobs WHERE reprint_of_job_id='job-1'"
        )["n"], 0)

    def test_lost_grams_cannot_exceed_print_fact(self):
        with self.assertRaisesRegex(ValueError, "больше факта"):
            self.recover(lost_grams=101)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM defects")["n"], 0)

    def test_recurring_reason_needs_fix_confirmation_before_repeat(self):
        self.db.upsert("print_jobs", {
            "id": "older", "order_id": "order-1", "name": "old.3mf",
            "file": "old.3mf", "state": "failed", "finished_at": "2026-08-20T10:00:00",
        })
        self.db.upsert("defects", {
            "id": "old-defect", "job_id": "older", "order_id": "order-1",
            "reason": "detached", "confirmed_at": "2026-08-20T10:01:00",
            "request_id": "old-request",
        })
        with self.assertRaisesRegex(ValueError, "модель или профиль"):
            self.recover(reprint_confirmed=True)
        result = self.recover(
            reprint_confirmed=True, repeat_risk_confirmed=True,
            request_id="risk-confirmed",
        )
        self.assertIsNotNone(result["repeat_job"])

    def test_reprint_failure_rolls_back_defect(self):
        with mock.patch.object(
            self.manager, "reprint_job", side_effect=RuntimeError("queue failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "queue failed"):
                self.recover(reprint_confirmed=True)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM defects")["n"], 0)


class DefectRecoveryApiRouteTests(unittest.TestCase):
    def test_routes_pass_strict_confirmation_and_request_id(self):
        api = Api.__new__(Api)
        api.defect_recovery = mock.Mock()
        api.defect_recovery.summary.return_value = {"loss": {"total": 10}}
        code, payload = api.get(
            "/api/defect/recovery", {"id": ["job-1"], "grams": ["20"]}
        )
        self.assertEqual((code, payload), (200, {"loss": {"total": 10}}))
        api.defect_recovery.summary.assert_called_once_with("job-1", 20.0, "")

        api.defect_recovery.recover.return_value = {"ok": True}
        code, payload = api.post(
            "/api/defect/recover",
            {
                "job_id": "job-1", "defect_confirmed": "true",
                "reprint_confirmed": True, "reason": "clog",
                "request_id": "request-1",
            }, {},
        )
        self.assertEqual((code, payload), (200, {"ok": True}))
        api.defect_recovery.recover.assert_called_once_with(
            "job-1", defect_confirmed=False, reason="clog", phase="unknown",
            code="", note="", lost_grams=0.0, reprint_confirmed=True,
            repeat_risk_confirmed=False, printer_id="", request_id="request-1",
        )


class DefectRecoveryMigrationTests(unittest.TestCase):
    def test_old_tables_get_recovery_fields_and_unique_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "old.sqlite3"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE print_jobs (id TEXT PRIMARY KEY,printer_id TEXT,order_id TEXT,"
                "name TEXT DEFAULT '',file TEXT DEFAULT '',state TEXT DEFAULT 'queued',"
                "source TEXT DEFAULT '',finished_at TEXT,created_at TEXT)"
            )
            conn.execute(
                "CREATE TABLE defects (id TEXT PRIMARY KEY,at TEXT,printer_id TEXT,job_id TEXT,"
                "order_id TEXT,code TEXT DEFAULT '',phase TEXT DEFAULT '',reason TEXT DEFAULT '',"
                "grams REAL DEFAULT 0,loss REAL DEFAULT 0,photo TEXT DEFAULT '')"
            )
            conn.commit()
            conn.close()
            db = Database(path)
            try:
                self.assertIn("reprint_request_id", db.columns("print_jobs"))
                self.assertIn("confirmed_at", db.columns("defects"))
                job_indexes = {row["name"] for row in db.query("PRAGMA index_list(print_jobs)")}
                defect_indexes = {row["name"] for row in db.query("PRAGMA index_list(defects)")}
                self.assertIn("idx_reprint_source", job_indexes)
                self.assertIn("idx_defect_confirmed_job", defect_indexes)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()

"""Тесты модального окна и подтверждения входящих проектов из Bambu Studio."""
from __future__ import annotations

import pathlib
import sys
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.studio_gateway import StudioGateway
from connector.tests.test_phase11 import make_api, make_db, _held


class FakePrinter:
    def __init__(self, pid="p1", model="P1S"):
        self.record = {"id": pid, "name": "Цех P1S", "model": model}
        self.cmds: list[str] = []
        self.connected = True

    def snapshot(self):
        return {
            "printer": {"state": "IDLE", "task": ""},
            "ams": {"trays": [{"slot": 0, "type": "PLA", "color": "#FFFFFF", "present": True, "active": True}]},
        }

    def command(self, name, value=None):
        self.cmds.append(name)
        return {"ok": True}


class FakeMgr:
    def __init__(self, db):
        self.db = db
        self.enqueued: list[dict] = []
        self.started: list[tuple] = []
        self.printers: dict = {}
        self.lock = threading.RLock()
        self.preflight_result = {"ok": True, "blocks": [], "warns": []}
        self.studio = None

    def enqueue(self, data):
        job = {"id": f"job-{len(self.enqueued)+1}", "state": "queued", **data}
        self.enqueued.append(job)
        return job

    def get(self, printer_id=""):
        if printer_id:
            return self.printers.get(printer_id)
        return next(iter(self.printers.values()), None)

    def preflight(self, *a, **k):
        return self.preflight_result

    def start_job(self, job_id, printer_id=""):
        self.started.append((job_id, printer_id))
        return {"id": job_id, "state": "starting"}


class StudioGatewayConfirmTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.tmp = pathlib.Path(_held[-1].name)
        self.lib_dir = self.tmp / "library"
        self.up_dir = self.tmp / "uploads"
        self.lib_dir.mkdir(exist_ok=True)
        self.up_dir.mkdir(exist_ok=True)
        for target in (
            "connector.printflow.library.LIBRARY_DIR",
            "connector.printflow.library.UPLOAD_DIR",
            "connector.printflow.studio_gateway.UPLOAD_DIR",
            "connector.printflow.config.UPLOAD_DIR",
        ):
            patch = mock.patch(target, self.up_dir if "UPLOAD" in target else self.lib_dir)
            patch.start()
            self.addCleanup(patch.stop)

        self.db.set_settings({
            "studio_gateway_access_code": "abcd1234",
            "studio_gateway_name": "NOZZA-PrintFlow",
            "studio_gateway_mode": "confirm",
        })
        self.mgr = FakeMgr(self.db)
        self.printer = FakePrinter(pid="p1")
        self.mgr.printers["p1"] = self.printer
        self.gw = StudioGateway(self.db, self.mgr, bind=False)
        self.mgr.studio = self.gw

    def test_confirm_mode_does_not_auto_enqueue(self):
        # В режиме confirm вызов ingest_bytes сохраняет проект в pending_confirm,
        # но НЕ ставит задание в очередь автоматически.
        out = self.gw.ingest_bytes("vase.gcode.3mf", b"model-bytes")
        self.assertEqual(len(self.mgr.enqueued), 0)
        self.assertIn("pending_id", out)
        pending_id = out["pending_id"]

        pending_list = self.gw.pending_list()
        self.assertEqual(len(pending_list), 1)
        self.assertEqual(pending_list[0]["id"], pending_id)
        self.assertEqual(pending_list[0]["filename"], "vase.gcode.3mf")

    def test_api_pending_and_confirm_queue(self):
        out = self.gw.ingest_bytes("bracket.gcode.3mf", b"bracket-bytes")
        pending_id = out["pending_id"]

        api = make_api(self.db)
        api.manager = self.mgr

        # Проверка GET /api/studio/pending
        code, res = api.get("/api/studio/pending", {})
        self.assertEqual(code, 200)
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["items"]), 1)
        self.assertEqual(res["items"][0]["id"], pending_id)

        # Подтверждение оператором: отправка в очередь
        code, confirm_res = api.post("/api/studio/confirm", {
            "pending_id": pending_id,
            "action": "queue",
            "printer_id": "p1",
            "plate": 2,
        }, {})
        self.assertEqual(code, 200)
        self.assertEqual(confirm_res["action"], "queue")
        self.assertFalse(confirm_res["started"])
        self.assertEqual(len(self.mgr.enqueued), 1)
        self.assertEqual(self.mgr.enqueued[0]["plate"], 2)

        # После подтверждения проект удаляется из pending
        self.assertEqual(len(self.gw.pending_list()), 0)

    def test_api_confirm_start_now(self):
        out = self.gw.ingest_bytes("gear.gcode.3mf", b"gear-bytes")
        pending_id = out["pending_id"]

        api = make_api(self.db)
        api.manager = self.mgr

        code, confirm_res = api.post("/api/studio/confirm", {
            "pending_id": pending_id,
            "action": "start",
            "printer_id": "p1",
            "plate": 1,
        }, {})
        self.assertEqual(code, 200)
        self.assertTrue(confirm_res["started"])
        self.assertEqual(len(self.mgr.started), 1)
        self.assertEqual(self.mgr.started[0][1], "p1")

    def test_api_confirm_reject(self):
        out = self.gw.ingest_bytes("reject_me.gcode.3mf", b"reject-bytes")
        pending_id = out["pending_id"]

        api = make_api(self.db)
        api.manager = self.mgr

        code, confirm_res = api.post("/api/studio/confirm", {
            "pending_id": pending_id,
            "action": "reject",
        }, {})
        self.assertEqual(code, 200)
        self.assertEqual(confirm_res["action"], "rejected")
        self.assertEqual(len(self.mgr.enqueued), 0)
        self.assertEqual(len(self.mgr.started), 0)
        self.assertEqual(len(self.gw.pending_list()), 0)


if __name__ == "__main__":
    unittest.main()

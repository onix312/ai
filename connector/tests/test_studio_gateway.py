"""PrintFlow 11.0.0: шлюз Bambu Studio, библиотека файлов, CLI-слайсер.

Тесты без сети: bind=False, кодек MQTT, ingest, FTP-команды, API.
"""
from __future__ import annotations

import json
import pathlib
import sys
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.config import DEFAULT_SETTINGS, SECRET_SETTINGS  # noqa: E402
from connector.printflow.studio_gateway import (  # noqa: E402
    DEV_MODELS,
    SSDP_NT,
    StudioGateway,
)
from connector.printflow.studio_mqtt import (  # noqa: E402
    CONNACK,
    PINGREQ,
    PINGRESP,
    PUBLISH,
    decode_publish,
    encode_connect,
    encode_publish,
    parse_fixed_header,
    wrap_packet,
)
from connector.tests.test_phase11 import make_api, make_db, _held  # noqa: E402


class FakePrinter:
    def __init__(self, pid="p1", model="P1S"):
        self.record = {"id": pid, "name": "Цех", "model": model}
        self.cmds: list[str] = []

    def snapshot(self):
        return {"ams": {"trays": [{"slot": 0, "type": "PLA", "color": "#FFFFFF"}]}}

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


class StudioGatewayTests(unittest.TestCase):
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
        })
        self.mgr = FakeMgr(self.db)
        self.gw = StudioGateway(self.db, self.mgr, bind=False)
        self.mgr.studio = self.gw

    def test_defaults(self):
        self.assertFalse(DEFAULT_SETTINGS["studio_gateway_enabled"])
        self.assertEqual(DEFAULT_SETTINGS["studio_gateway_name"], "NOZZA-PrintFlow")
        self.assertEqual(DEFAULT_SETTINGS["studio_gateway_mode"], "queue")
        self.assertFalse(DEFAULT_SETTINGS["studio_gateway_autostart"])
        self.assertEqual(DEFAULT_SETTINGS["studio_gateway_serial"], "")
        self.assertEqual(DEFAULT_SETTINGS["studio_gateway_access_code"], "")
        self.assertEqual(DEFAULT_SETTINGS["studio_gateway_printer_id"], "")
        self.assertEqual(DEFAULT_SETTINGS["slicer_bin"], "")
        self.assertIn("studio_gateway_access_code", SECRET_SETTINGS)
        self.assertEqual(DEV_MODELS["P1S"], "C12")
        self.assertEqual(DEV_MODELS["X1C"], "BL-P001")

    def test_library_schema_on_fresh_db(self):
        tables = {r["name"] for r in self.db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("library_files", tables)

    def test_identity_serial_and_no_secret(self):
        ident = self.gw.identity()
        self.assertTrue(ident["serial"].startswith("01P00A"))
        self.assertEqual(len(ident["serial"]), 15)
        self.assertEqual(ident["name"], "NOZZA-PrintFlow")
        self.assertEqual(ident["model"], "P1S")
        self.assertEqual(ident["dev_model"], "C12")
        self.assertNotIn("access_code", ident)
        self.assertEqual(ident["mqtt_port"], 8883)
        self.assertEqual(ident["ftp_port"], 990)

    def test_dev_model_follows_bound_printer(self):
        self.mgr.printers["p1"] = FakePrinter(model="X1C")
        self.db.set_settings({"studio_gateway_printer_id": "p1"})
        self.assertEqual(self.gw.identity()["dev_model"], "BL-P001")
        self.assertEqual(self.gw.identity()["model"], "X1C")

    def test_ssdp_notify(self):
        ident = self.gw.identity()
        text = self.gw.ssdp_notify()
        self.assertIn(SSDP_NT, text)
        self.assertIn(ident["serial"], text)
        self.assertIn("DevModel.bambu.com: C12", text)
        self.assertIn("DevName.bambu.com: NOZZA-PrintFlow", text)
        self.assertTrue(text.startswith("NOTIFY * HTTP/1.1"))
        ok = self.gw.ssdp_search_response()
        self.assertTrue(ok.startswith("HTTP/1.1 200 OK"))
        self.assertIn(SSDP_NT, ok)

    def test_access_code_masked_and_absent_from_status(self):
        settings = self.db.settings()
        self.assertEqual(settings["studio_gateway_access_code"], "••••••••")
        self.assertTrue(settings["has_studio_gateway_access_code"])
        self.assertEqual(self.db.setting("studio_gateway_access_code"), "abcd1234")
        status = self.gw.status()
        self.assertNotIn("access_code", status)
        self.assertTrue(status["has_access_code"])
        self.assertFalse(status["running"])
        self.assertFalse(status["autostart_allowed"])

    def test_ingest_queues_without_autostart(self):
        out = self.gw.ingest_bytes("job.gcode.3mf", b"gcode-bytes")
        self.assertEqual(out["library"]["name"], "job.gcode.3mf")
        self.assertEqual(out["library"]["source"], "studio-gateway")
        self.assertFalse(out["autostart"])
        job = self.mgr.enqueued[-1]
        self.assertEqual(job["source"], "studio-gateway")
        self.assertEqual(job["no_auto"], 1)
        self.assertFalse(self.mgr.started)
        self.assertTrue((self.lib_dir / out["library"]["sha256"][:2]).exists()
                        or (self.up_dir / "job.gcode.3mf").exists())

    def test_autostart_requires_three_gates(self):
        self.mgr.printers["p1"] = FakePrinter()
        self.db.set_settings({
            "studio_gateway_autostart": True,
            "studio_gateway_mode": "autostart",
            "unattended_dangerous_actions": False,
            "studio_gateway_printer_id": "p1",
        })
        self.gw.ingest_bytes("gated.gcode.3mf", b"xx")
        self.assertEqual(self.mgr.enqueued[-1]["no_auto"], 1)
        self.assertFalse(self.mgr.started)

        self.db.set_settings({"unattended_dangerous_actions": True})
        self.gw.ingest_bytes("go.gcode.3mf", b"yy")
        self.assertEqual(self.mgr.enqueued[-1]["no_auto"], 0)
        self.assertTrue(self.mgr.started)

        self.mgr.started.clear()
        self.mgr.preflight_result = {"ok": False, "blocks": [{"title": "нет PLA"}]}
        self.gw.ingest_bytes("blocked.gcode.3mf", b"zz")
        self.assertFalse(self.mgr.started)

    def test_ftp_login_and_apply(self):
        self.assertIn("331", self.gw.ftp_command("USER bblp"))
        self.assertIn("530", self.gw.ftp_command("PASS nope"))
        self.assertIn("230", self.gw.ftp_command("PASS abcd1234"))
        out = self.gw.ftp_apply("from-studio.gcode.3mf", b"stor-body")
        self.assertEqual(out["job"]["file"], "from-studio.gcode.3mf")
        self.assertEqual(self.mgr.enqueued[-1]["source"], "studio-gateway")

    def test_mqtt_connect_and_project_file(self):
        replies = self.gw.mqtt_handle_packet(
            encode_connect(username="bblp", password="abcd1234"))
        ptype, _flags, payload = parse_fixed_header(replies[0])
        self.assertEqual(ptype, CONNACK)
        self.assertEqual(payload[1], 0)

        bad = self.gw.mqtt_handle_packet(
            encode_connect(username="bblp", password="wrong"))
        self.assertEqual(parse_fixed_header(bad[0])[2][1], 4)

        ping = self.gw.mqtt_handle_packet(wrap_packet(PINGREQ))
        self.assertEqual(parse_fixed_header(ping[0])[0], PINGRESP)

        self.gw._incoming["plate.gcode.3mf"] = b"project-bytes"
        reports = self.gw.handle_mqtt_request({
            "print": {
                "command": "project_file",
                "url": "ftp:///plate.gcode.3mf",
                "sequence_id": "9",
                "param": "Metadata/plate_2.gcode",
            }
        })
        pf = next(r for r in reports if r.get("print", {}).get("command") == "project_file")
        self.assertEqual(pf["print"]["result"], "success")
        idle = next(r for r in reports if r.get("print", {}).get("gcode_state") == "IDLE")
        self.assertEqual(idle["print"]["command"], "push_status")
        self.assertEqual(self.mgr.enqueued[-1]["plate"], 2)

        missing = self.gw.handle_mqtt_request({
            "print": {"command": "project_file", "url": "ftp:///nope.gcode.3mf"}
        })
        self.assertEqual(missing[0]["print"]["result"], "fail")

    def test_mqtt_packet_publish_roundtrip(self):
        self.gw._incoming["z.gcode.3mf"] = b"data"
        serial = self.gw.identity()["serial"]
        pkt = encode_publish(
            f"device/{serial}/request",
            json.dumps({"print": {
                "command": "project_file", "url": "ftp:///z.gcode.3mf",
            }}),
        )
        replies = self.gw.mqtt_handle_packet(pkt)
        self.assertTrue(replies)
        ptype, flags, payload = parse_fixed_header(replies[0])
        self.assertEqual(ptype, PUBLISH)
        body = json.loads(decode_publish(flags, payload)["payload"])
        self.assertEqual(body["print"]["result"], "success")

    def test_pushall_version_pause(self):
        push = self.gw.handle_mqtt_request({"pushing": {"command": "pushall"}})
        self.assertEqual(push[0]["print"]["command"], "push_status")
        ver = self.gw.handle_mqtt_request({"info": {"command": "get_version"}})
        self.assertEqual(ver[0]["info"]["module"][0]["sn"], self.gw.identity()["serial"])
        printer = FakePrinter()
        self.mgr.printers["p1"] = printer
        self.db.set_settings({"studio_gateway_printer_id": "p1"})
        self.gw.handle_mqtt_request({"print": {"command": "pause"}})
        self.gw.handle_mqtt_request({"print": {"command": "resume"}})
        self.gw.handle_mqtt_request({"print": {"command": "stop"}})
        self.assertEqual(printer.cmds, ["pause", "resume", "stop"])

    def test_slicer_honest_refusal(self):
        from connector.printflow.slicer import SlicerError, slice_file, status
        st = status("/no/such/orca-slicer")
        self.assertFalse(st["available"])
        self.assertIn("не найден", st["error"].lower())
        stl = self.tmp / "part.stl"
        stl.write_bytes(b"solid x\nendsolid x\n")
        with self.assertRaises(SlicerError):
            slice_file(stl, explicit_bin="/no/such/orca-slicer")

    def test_api_studio_library_slicer(self):
        from connector.printflow.library import FileLibrary
        api = make_api(self.db)
        api.manager = self.mgr
        code, payload = api.get("/api/studio/status", {})
        self.assertEqual(code, 200)
        self.assertNotIn("access_code", payload)
        self.assertEqual(payload["name"], "NOZZA-PrintFlow")
        self.assertIn("serial", payload)

        rec = FileLibrary(self.db).put("keep.gcode.3mf", b"abc123", source="test")
        code, payload = api.get("/api/library", {})
        self.assertEqual(code, 200)
        self.assertTrue(any(f["id"] == rec["id"] for f in payload["files"]))
        code, payload = api.post("/api/library/delete", {"id": rec["id"]}, {})
        self.assertEqual(code, 200)

        code, payload = api.get("/api/slicer/status", {})
        self.assertEqual(code, 200)
        self.assertIn("available", payload)
        code, payload = api.post("/api/slicer/run", {"file": "missing.stl"}, {})
        self.assertEqual(code, 400)
        self.assertIn("error", payload)

        code, payload = api.post("/api/settings", {"studio_gateway_mode": "queue"}, {})
        self.assertEqual(code, 200)
        self.assertEqual(payload["settings"]["studio_gateway_access_code"], "••••••••")


if __name__ == "__main__":
    unittest.main()

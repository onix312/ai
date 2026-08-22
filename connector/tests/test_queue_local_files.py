"""Очередь принимает локальные модели и загружает их только при старте."""
from __future__ import annotations

import io
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.api import Handler, _upload_filename, save_upload  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.manager import PrinterManager  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402


class FilesStub:
    def __init__(self):
        self.uploads: list[tuple[str, str]] = []

    def upload(self, local, remote_name):
        self.uploads.append((str(local), remote_name))
        return {"ok": True, "name": remote_name}


class PrinterStub:
    def __init__(self):
        self.id = "printer-local"
        self.mode = "lan"
        self.record = {"id": self.id, "name": "P1S", "mode": "lan", "host": "192.168.1.20"}
        self.connected = True
        self.files = FilesStub()
        self.starts: list[tuple[tuple, dict]] = []

    def snapshot(self):
        return {"printer": {"state": "IDLE"}, "ams": {"trays": []}}

    def start_print(self, *args, **kwargs):
        self.starts.append((args, kwargs))
        return {"ok": True}

    def shutdown(self):
        pass


class LocalQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.db = Database(self.root / "printflow.sqlite3")
        self.manager = PrinterManager(self.db, Repo(self.db))

    def tearDown(self):
        self.manager.shutdown()
        self.db.close()
        self.tmp.cleanup()

    def test_same_filename_does_not_overwrite_another_queued_model(self):
        with patch("connector.printflow.api.UPLOAD_DIR", self.root / "uploads"):
            first, _, created_first = save_upload("C:\\models\\part.3mf", b"first")
            second, _, created_second = save_upload("C:\\models\\part.3mf", b"second")
        self.assertTrue(created_first)
        self.assertTrue(created_second)
        self.assertEqual(first, "part.3mf")
        self.assertNotEqual(second, first)
        self.assertEqual((self.root / "uploads" / first).read_bytes(), b"first")
        self.assertEqual((self.root / "uploads" / second).read_bytes(), b"second")

    def test_local_queue_file_is_uploaded_when_job_starts(self):
        printer = PrinterStub()
        self.manager.printers[printer.id] = printer
        uploads = self.root / "uploads"
        uploads.mkdir()
        (uploads / "local.gcode").write_text("; TIME: 60\n", encoding="utf-8")
        with patch("connector.printflow.manager.UPLOAD_DIR", uploads):
            job = self.manager.enqueue({
                "file": "local.gcode", "name": "Локальная деталь",
                "printer_id": printer.id, "source": "local-upload",
            })
            started = self.manager.start_job(job["id"], printer.id)
        self.assertEqual(started["state"], "starting")
        self.assertEqual(len(printer.files.uploads), 1)
        self.assertEqual(pathlib.Path(printer.files.uploads[0][0]).name, "local.gcode")
        self.assertEqual(printer.files.uploads[0][1], "local.gcode")
        self.assertEqual(len(printer.starts), 1)

    def test_starting_job_cannot_be_started_twice(self):
        printer = PrinterStub()
        self.manager.printers[printer.id] = printer
        job = self.manager.enqueue({"file": "on-sd.3mf", "printer_id": printer.id})
        self.manager.start_job(job["id"], printer.id)
        with self.assertRaises(ValueError):
            self.manager.start_job(job["id"], printer.id)
        self.assertEqual(len(printer.starts), 1)

    def test_multipart_upload_creates_queue_job_without_printer(self):
        boundary = "----printflow-test"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="name"\r\n\r\n'
            "Деталь\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="priority"\r\n\r\n'
            "7\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="model.gcode"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
            "; TIME: 120\n"
            f"\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        handler = Handler.__new__(Handler)
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        handler.rfile = io.BytesIO(body)
        handler.api = SimpleNamespace(manager=self.manager)
        sent = {}
        handler.send_json = lambda code, payload: sent.update(code=code, payload=payload)
        with patch("connector.printflow.api.UPLOAD_DIR", self.root / "uploads"):
            handler.handle_job_upload()
        self.assertEqual(sent["code"], 200)
        self.assertEqual(sent["payload"]["job"]["file"], "model.gcode")
        self.assertEqual(sent["payload"]["job"]["priority"], 7)
        self.assertEqual(sent["payload"]["job"]["printer_id"], None)
        self.assertTrue((self.root / "uploads" / "model.gcode").exists())

    def test_filename_normalizes_windows_path_and_rejects_null(self):
        self.assertEqual(_upload_filename(r"C:\\temp\\model.3mf"), "model.3mf")
        with self.assertRaises(ValueError):
            _upload_filename("bad\x00.gcode")


if __name__ == "__main__":
    unittest.main()

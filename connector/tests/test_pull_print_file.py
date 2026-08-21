"""Скачивание файла печати с принтера в uploads и оценка граммов."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow.manager import PrinterManager  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402
from connector.tests.test_auto_resume_and_order import MockPrinter  # noqa: E402


class FilesStub:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.downloads = []

    def list_files(self, path="/"):
        return [{"name": n, "path": "/" + n, "dir": False} for n in self.files]

    def read_head(self, path, max_bytes=131072):
        name = str(path).lstrip("/")
        data = self.files.get(name) or self.files.get(pathlib.Path(name).name) or b""
        self.downloads.append(("head", path))
        return data[:max_bytes]

    def download(self, path, max_bytes=10**9):
        name = str(path).lstrip("/")
        data = self.files.get(name) or self.files.get(pathlib.Path(name).name) or b""
        self.downloads.append(("get", path))
        return data[:max_bytes]


class PullPrintFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "t.sqlite3")
        self.repo = Repo(self.db)
        self.manager = PrinterManager(self.db, self.repo)
        self.pr = MockPrinter("pr_pull", "P1S", "RUNNING", "Bowl_213g.gcode")
        self.pr.record["host"] = "192.168.1.10"
        self.pr.record["access_code"] = "12345678"
        head = b"; Filament used [g]: 213.0\n; TIME: 9600\n; filament_type: PLA\nG1 X0\n"
        self.pr.files = FilesStub({"Bowl_213g.gcode": head})
        self.manager.printers["pr_pull"] = self.pr
        self.save = pathlib.Path(self.tmp.name) / "uploads"
        self.save.mkdir()

    def tearDown(self):
        self.manager.shutdown()
        self.db.close()
        self.tmp.cleanup()

    def test_pull_saves_and_reads_grams(self):
        res = self.manager.pull_print_file("pr_pull", "Bowl_213g.gcode", save_dir=self.save)
        self.assertTrue(res["ok"])
        self.assertAlmostEqual(res["grams"], 213.0)
        self.assertAlmostEqual(res["hours"], 160 / 60, places=2)
        self.assertEqual(res["source"], "printer")
        self.assertTrue((self.save / "Bowl_213g.gcode").exists())

    def test_second_pull_uses_local_copy(self):
        first = self.manager.pull_print_file("pr_pull", "Bowl_213g.gcode", save_dir=self.save)
        self.assertTrue((self.save / "Bowl_213g.gcode").exists())
        self.assertGreater(first["bytes"], 50)
        self.pr.files.downloads.clear()
        res = self.manager.pull_print_file("pr_pull", "Bowl_213g.gcode", save_dir=self.save)
        self.assertAlmostEqual(res["grams"], 213.0)
        self.assertIn(res["source"], ("uploads", "printer"))

    def test_api_estimate_pull(self):
        from unittest.mock import patch
        from connector.printflow.api import Api
        api = Api.__new__(Api)
        api.db = self.db
        api.repo = self.repo
        api.manager = self.manager
        with patch("connector.printflow.config.UPLOAD_DIR", self.save):
            code, body = api.post("/api/estimate/pull", {
                "printer_id": "pr_pull", "file": "Bowl_213g.gcode",
            }, {})
        self.assertEqual(code, 200)
        self.assertAlmostEqual(body["grams"], 213.0)


if __name__ == "__main__":
    unittest.main()

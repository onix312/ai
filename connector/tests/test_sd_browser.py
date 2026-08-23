"""SD-обозреватель 9.0: Unix/DOS/MLSD, папки, запрет печати логов."""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import sd_browser  # noqa: E402


UNIX = """\
total 8
drwxr-xr-x 2 bblp bblp 4096 Jan  1 12:00 cache
drwxr-xr-x 2 bblp bblp 4096 Jan  1 12:00 timelapse
-rw-r--r-- 1 bblp bblp 1234 Jan  1 12:00 model.gcode.3mf
-rw-r--r-- 1 bblp bblp  88 Jan  1 12:00 note.txt
"""

DOS = """\
01-02-26  03:04PM       <DIR>          model
01-02-26  03:04PM              2048 plate.3mf
01-02-26  03:04PM             40960 clip.mp4
"""

MLSD = """\
type=dir;size=0; cache
type=file;size=333; job.gcode
type=cdir;size=0; .
type=pdir;size=0; ..
"""


class PathTests(unittest.TestCase):
    def test_sanitize_and_crumbs(self):
        self.assertEqual(sd_browser.sanitize_remote_path("cache/foo"), "/cache/foo")
        with self.assertRaises(ValueError):
            sd_browser.sanitize_remote_path("../etc")
        crumbs = sd_browser.breadcrumbs("/cache/a")
        self.assertEqual(crumbs[0]["path"], "/")
        self.assertEqual(crumbs[-1]["path"], "/cache/a")

    def test_cannot_print_timelapse_or_logs(self):
        self.assertFalse(sd_browser.can_print("/timelapse/clip.3mf"))
        self.assertFalse(sd_browser.can_print("/log/error.gcode"))
        self.assertFalse(sd_browser.can_print("/ipcam/x.gcode"))
        self.assertTrue(sd_browser.can_print("/model.gcode.3mf"))
        self.assertFalse(sd_browser.can_print("/note.txt"))


class ParseTests(unittest.TestCase):
    def test_unix_keeps_dirs_and_media(self):
        items = sd_browser.parse_listing(UNIX, "/")
        names = {i["name"]: i for i in items}
        self.assertTrue(names["cache"]["dir"])
        self.assertTrue(names["model.gcode.3mf"]["printable"])
        self.assertEqual(names["note.txt"]["kind"], "media")
        self.assertFalse(names["timelapse"]["printable"])

    def test_dos_and_mlsd(self):
        dos = {i["name"]: i for i in sd_browser.parse_listing(DOS, "/")}
        self.assertTrue(dos["model"]["dir"])
        self.assertTrue(dos["plate.3mf"]["printable"])
        self.assertEqual(dos["clip.mp4"]["kind"], "media")
        mlsd = {i["name"]: i for i in sd_browser.parse_listing(MLSD, "/")}
        self.assertTrue(mlsd["cache"]["dir"])
        self.assertTrue(mlsd["job.gcode"]["printable"])
        self.assertNotIn(".", mlsd)


class FakeFtp:
    def __init__(self, listing):
        self.listing = listing

    def retrlines(self, cmd, callback):
        if cmd.startswith("MLSD"):
            raise RuntimeError("no mlsd")
        if cmd.startswith("LIST"):
            for line in self.listing.splitlines():
                callback(line)
            return
        raise RuntimeError(cmd)


class ListViaFtpTests(unittest.TestCase):
    def test_falls_back_to_list(self):
        items = sd_browser.list_via_ftp(FakeFtp(UNIX), "/")
        self.assertTrue(any(i["name"] == "cache" and i["dir"] for i in items))
        self.assertTrue(any(i["printable"] for i in items))


if __name__ == "__main__":
    unittest.main()

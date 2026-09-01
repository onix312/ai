"""Идея 119: проекция плиты на живой кадр камеры.

Проверяются гомография по четырём углам, валидация калибровки,
API-маршруты /api/camera/calibrate[/reset] и /api/camera/projection,
а также раздача эталона стола /api/camera/bed-ref.jpg.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import types
import unittest
import zipfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.bed_projection import (  # noqa: E402
    apply_h,
    corners_valid,
    homography,
    plate_to_frame,
    project_objects,
)
from connector.printflow.config import now_iso  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.tests.test_phase11 import make_api, make_db  # noqa: E402

GOOD_CORNERS = [[0.10, 0.90], [0.90, 0.90], [0.95, 0.10], [0.05, 0.10]]


def make_3mf(path: pathlib.Path) -> None:
    """Минимальный 3MF, который понимает plate_map_3mf: один объект 30×40 мм."""
    xml = (
        '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        '<resources>'
        '<object id="1" name="Адресник" minx="10" miny="20" maxx="40" maxy="60"/>'
        '</resources>'
        '<build>'
        '<node objectid="1" position="100 200 0" scale="1 1 1"/>'
        '</build>'
        '</model>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", xml)


class HomographyTests(unittest.TestCase):
    def test_identity_square(self):
        h = homography([(0, 0), (1, 0), (1, 1), (0, 1)],
                       [(0, 0), (1, 0), (1, 1), (0, 1)])
        for x, y in ((0.0, 0.0), (0.37, 0.81), (1.0, 1.0)):
            u, v = apply_h(h, x, y)
            self.assertAlmostEqual(u, x, places=9)
            self.assertAlmostEqual(v, y, places=9)

    def test_perspective_roundtrip(self):
        src = [(0, 0), (1, 0), (1, 1), (0, 1)]
        dst = [(0.10, 0.20), (0.90, 0.15), (0.95, 0.90), (0.05, 0.85)]
        forward = homography(src, dst)
        back = homography(dst, src)
        for x, y in ((0.3, 0.4), (0.75, 0.6), (0.5, 0.5)):
            u, v = apply_h(forward, x, y)
            bx, by = apply_h(back, u, v)
            self.assertAlmostEqual(bx, x, places=6)
            self.assertAlmostEqual(by, y, places=6)

    def test_degenerate_returns_none(self):
        self.assertIsNone(homography([(0, 0), (0, 0), (1, 1), (0, 1)],
                                     [(0, 0), (1, 0), (1, 1), (0, 1)]))
        self.assertIsNone(homography([(0, 0)], [(0, 0)]))

    def test_plate_corners_order(self):
        """Передний ряд плиты (y = plate_h) должен попадать в нижнюю часть кадра."""
        w, h_plate = 256.0, 256.0
        corners = [(0.1, 0.9), (0.9, 0.9), (0.95, 0.1), (0.05, 0.1)]
        u_front, v_front = plate_to_frame(corners, w, h_plate, 0.0, h_plate)
        u_back, v_back = plate_to_frame(corners, w, h_plate, 0.0, 0.0)
        self.assertGreater(v_front, v_back)
        self.assertAlmostEqual(u_front, 0.1, places=3)
        self.assertAlmostEqual(u_back, 0.05, places=3)

    def test_project_objects_in_frame(self):
        corners = [(0.1, 0.9), (0.9, 0.9), (0.95, 0.1), (0.05, 0.1)]
        objs = [{"id": 1, "name": "Деталь", "x": 100, "y": 100, "w": 30, "h": 40}]
        out = project_objects(corners, 256, 256, objs)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], 1)
        self.assertEqual(len(out[0]["pts"]), 4)
        for px, py in out[0]["pts"]:
            self.assertTrue(0.0 <= px <= 1.0 and 0.0 <= py <= 1.0)

    def test_corners_valid(self):
        self.assertTrue(corners_valid(GOOD_CORNERS))
        self.assertFalse(corners_valid([]))
        self.assertFalse(corners_valid([[0, 0], [0, 0], [0, 0], [0, 0]]))
        self.assertFalse(corners_valid([[0, 0], [2, 0], [2, 2], [0, 2]]))  # вне кадра
        self.assertFalse(corners_valid([["a", 0], [1, 0], [1, 1], [0, 1]]))


class CalibrationApiTests(unittest.TestCase):
    def setUp(self):
        self.api = make_api(make_db())
        self.api.manager.get = lambda pid: None    # принтеры не добавлены
        from connector.printflow.api import register_routes

        register_routes()

    def test_calibrate_requires_printer(self):
        with self.assertRaises(ValueError):
            self.api.post("/api/camera/calibrate", {"corners": GOOD_CORNERS}, {})

    def test_calibrate_validates_corners(self):
        with self.assertRaises(ValueError):
            self.api.post("/api/camera/calibrate",
                          {"printer_id": "p1", "corners": [[0, 0], [0, 0]]}, {})
        with self.assertRaises(ValueError):
            self.api.post("/api/camera/calibrate",
                          {"printer_id": "p1", "corners": "мусор"}, {})

    def test_calibrate_save_and_reset(self):
        code, payload = self.api.post("/api/camera/calibrate",
                                      {"printer_id": "p1", "corners": GOOD_CORNERS}, {})
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        raw = self.api.db.setting("cam_cal_p1", "")
        self.assertIn("corners", raw)

        code, payload = self.api.post("/api/camera/calibrate/reset",
                                      {"printer_id": "p1"}, {})
        self.assertEqual((code, payload["ok"]), (200, True))
        self.assertEqual(self.api.db.setting("cam_cal_p1", ""), "")

    def test_reset_requires_printer(self):
        with self.assertRaises(ValueError):
            self.api.post("/api/camera/calibrate/reset", {}, {})

    def _running_job(self, filename: str = "job.3mf") -> None:
        self.api.db.upsert("print_jobs", {
            "id": "j119", "printer_id": "p1", "name": "Заказ 119",
            "state": "running", "file": filename, "started_at": now_iso(),
        })

    def test_projection_without_printer_is_404(self):
        code, _ = self.api.get("/api/camera/projection",
                               {"printer_id": ["nope"]})
        self.assertEqual(code, 404)

    def test_projection_no_job(self):
        self.api.manager.get = lambda pid: types.SimpleNamespace(id=pid)
        code, payload = self.api.get("/api/camera/projection", {"printer_id": ["p1"]})
        self.assertEqual((code, payload["calibrated"], payload["has_map"]),
                         (200, False, False))

    def test_projection_uncalibrated_with_job(self):
        self.api.manager.get = lambda pid: types.SimpleNamespace(id=pid)
        self._running_job()
        code, payload = self.api.get("/api/camera/projection", {"printer_id": ["p1"]})
        self.assertEqual((code, payload["has_map"]), (200, False))
        self.assertFalse(payload["calibrated"])

    def test_projection_full_pipeline(self):
        self.api.manager.get = lambda pid: types.SimpleNamespace(id=pid)
        self.api.post("/api/camera/calibrate",
                      {"printer_id": "p1", "corners": GOOD_CORNERS}, {})
        self._running_job()
        with tempfile.TemporaryDirectory() as tmp:
            make_3mf(pathlib.Path(tmp) / "job.3mf")
            with mock.patch("connector.printflow.config.UPLOAD_DIR",
                            pathlib.Path(tmp)):
                code, payload = self.api.get("/api/camera/projection",
                                             {"printer_id": ["p1"]})
        self.assertEqual((code, payload["calibrated"], payload["has_map"]),
                         (200, True, True))
        self.assertEqual(len(payload["objects"]), 1)
        self.assertEqual(payload["objects"][0]["id"], 1)
        for px, py in payload["objects"][0]["pts"]:
            self.assertTrue(0.0 <= px <= 1.0 and 0.0 <= py <= 1.0)

    def test_projection_non_3mf_job(self):
        self.api.manager.get = lambda pid: types.SimpleNamespace(id=pid)
        self.api.post("/api/camera/calibrate",
                      {"printer_id": "p1", "corners": GOOD_CORNERS}, {})
        self._running_job("job.gcode.3mf".replace(".3mf", ""))  # без .3mf
        code, payload = self.api.get("/api/camera/projection", {"printer_id": ["p1"]})
        self.assertEqual((code, payload["has_map"]), (200, False))
        self.assertIn("3MF", payload["reason"])


class BedRefServeTests(unittest.TestCase):
    def _handler(self):
        from connector.printflow.api import Handler

        handler = Handler.__new__(Handler)
        return handler

    def test_missing_reference_is_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("connector.printflow.config.PHOTO_DIR",
                            pathlib.Path(tmp)):
                handler = self._handler()
                handler.send_json = mock.Mock()
                handler.serve_bed_reference()
                handler.send_json.assert_called_once()
                self.assertEqual(handler.send_json.call_args[0][0], 404)

    def test_reference_bytes_are_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref = pathlib.Path(tmp) / "bed_reference.jpg"
            ref.write_bytes(b"\xff\xd8jpeg-bytes")
            with mock.patch("connector.printflow.config.PHOTO_DIR",
                            pathlib.Path(tmp)):
                handler = self._handler()
                handler.send_response = mock.Mock()
                handler.send_header = mock.Mock()
                handler._send_bytes = mock.Mock()
                handler.serve_bed_reference()
                handler._send_bytes.assert_called_once_with(b"\xff\xd8jpeg-bytes",
                                                            "image/jpeg")


class DbSmoke(unittest.TestCase):
    def test_database_opens(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(pathlib.Path(tmp) / "t.sqlite3")
            self.addCleanup(db.close)
            self.assertTrue(db.query("SELECT count(*) AS n FROM settings"))


if __name__ == "__main__":
    unittest.main()

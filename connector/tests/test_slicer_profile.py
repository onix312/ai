"""Профиль, материалы и аудит слайсера: отказы честные, маршруты не врут."""
from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.slicer import SlicerError  # noqa: E402
from connector.printflow.slicer_engine import SliceSettings, slice_model  # noqa: E402
from connector.printflow.slicer_profile import (  # noqa: E402
    P1S, PROFILE_ID, audit_gcode, audit_model, material_preset, profile_payload,
    settings_from, settings_payload, validate_settings)


def cube(size: float = 20.0, height: float | None = None):
    s = size
    h = height if height is not None else size
    verts = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
             (0, 0, h), (s, 0, h), (s, s, h), (0, s, h)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
             (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return faces, verts


def write_stl(path: Path, faces, verts) -> None:
    out = bytearray(b"printflow".ljust(80, b"\0"))
    out += struct.pack("<I", len(faces))
    for a, b, c in faces:
        out += struct.pack("<3f", 0.0, 0.0, 0.0)
        for index in (a, b, c):
            out += struct.pack("<3f", *verts[index])
        out += struct.pack("<H", 0)
    path.write_bytes(bytes(out))


class MaterialTests(unittest.TestCase):
    def test_presets_come_from_shared_catalog(self):
        pla = material_preset("PLA")
        self.assertEqual(pla["density"], 1.24)
        self.assertTrue(pla["nozzle_min"] <= pla["nozzle_temp"] <= pla["nozzle_max"])
        petg = material_preset("PETG")
        self.assertGreater(petg["nozzle_temp"], pla["nozzle_temp"])

    def test_slow_material_cuts_speed(self):
        settings = settings_from({"slicer_material": "TPU", "slicer_speed_mm_s": 100,
                                  "slicer_nozzle_temp": 0, "slicer_bed_temp": 0,
                                  "slicer_fan_percent": 0})
        self.assertLess(settings.speed_mm_s, 60.0)

    def test_unknown_material_falls_back_to_pla(self):
        settings = settings_from({"slicer_material": "UNOBTAINIUM",
                                  "slicer_nozzle_temp": 0, "slicer_bed_temp": 0,
                                  "slicer_fan_percent": 0})
        self.assertGreaterEqual(settings.nozzle_temp, 190)
        self.assertGreater(settings.bed_temp, 0)


class SettingsValidationTests(unittest.TestCase):
    def _settings(self, **overrides) -> SliceSettings:
        base = settings_from({"slicer_material": "PLA", "slicer_nozzle_temp": 220,
                              "slicer_bed_temp": 60, "slicer_layer_height": 0.2,
                              "slicer_first_layer_height": 0.2, "slicer_walls": 3,
                              "slicer_infill_percent": 15,
                              "slicer_infill_pattern": "grid",
                              "slicer_speed_mm_s": 100,
                              "slicer_travel_speed_mm_s": 200,
                              "slicer_fan_percent": 100})
        if not overrides:
            return base
        return SliceSettings(**{**base.__dict__, **overrides})

    def test_defaults_are_valid(self):
        self.assertEqual(validate_settings(self._settings()), [])

    def test_layer_height_above_nozzle_is_rejected(self):
        # На сопле 0,25 мм слой 0,2 мм уже толще 0,75 диаметра: не продавится.
        with self.assertRaisesRegex(SlicerError, "не продавится"):
            validate_settings(self._settings(nozzle_mm=0.25, layer_height=0.2))

    def test_layer_height_outside_profile_is_rejected(self):
        with self.assertRaisesRegex(SlicerError, "вне предела"):
            validate_settings(self._settings(layer_height=0.8))

    def test_first_layer_thinner_than_layer_is_rejected(self):
        with self.assertRaisesRegex(SlicerError, "тоньше"):
            validate_settings(self._settings(layer_height=0.2,
                                             first_layer_height=0.1))

    def test_unknown_pattern_is_rejected(self):
        with self.assertRaisesRegex(SlicerError, "Неизвестный узор"):
            validate_settings(self._settings(infill_pattern="spiral"))

    def test_gyroid_is_downgraded_with_warning(self):
        warnings = validate_settings(self._settings(infill_pattern="gyroid"))
        self.assertTrue(any("не поддерживается" in item for item in warnings))

    def test_temperature_outside_printer_is_rejected(self):
        with self.assertRaisesRegex(SlicerError, "вне предела принтера"):
            validate_settings(self._settings(nozzle_temp=400))

    def test_temperature_outside_material_warns(self):
        warnings = validate_settings(self._settings(material="PLA", nozzle_temp=280))
        self.assertTrue(any("вне диапазона" in item for item in warnings))

    def test_zero_walls_is_rejected(self):
        with self.assertRaisesRegex(SlicerError, "хотя бы один периметр"):
            validate_settings(self._settings(walls=0))

    def test_infill_out_of_range_is_rejected(self):
        with self.assertRaisesRegex(SlicerError, "0–100"):
            validate_settings(self._settings(infill_percent=140))

    def test_absurd_flow_is_rejected(self):
        with self.assertRaisesRegex(SlicerError, "коэффициентом"):
            validate_settings(self._settings(flow=9.0))

    def test_payload_describes_every_knob(self):
        payload = settings_payload(self._settings(), P1S)
        for key in ("layer_height", "walls", "infill_percent", "infill_pattern",
                    "nozzle_temp", "bed_temp", "seam", "retract_mm", "flow"):
            self.assertIn(key, payload)


class ModelAuditTests(unittest.TestCase):
    def test_cube_audit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cube.stl"
            write_stl(path, *cube())
            audit = audit_model(path, settings_from({}), P1S)
            self.assertTrue(audit["ok"])
            self.assertEqual(audit["layers"], 100)
            self.assertTrue(audit["fits_bed"])
            self.assertEqual(audit["warnings"], [])

    def test_oversized_model_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "big.stl"
            write_stl(path, *cube(size=300.0, height=20.0))
            with self.assertRaisesRegex(SlicerError, "не влезает"):
                audit_model(path, settings_from({}), P1S)

    def test_too_tall_model_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tall.stl"
            write_stl(path, *cube(size=20.0, height=300.0))
            with self.assertRaisesRegex(SlicerError, "больше предела"):
                audit_model(path, settings_from({}), P1S)

    def test_flat_model_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "flat.stl"
            write_stl(path, *cube(size=20.0, height=0.0))
            with self.assertRaises(SlicerError):
                audit_model(path, settings_from({}), P1S)

    def test_thin_model_warns(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "thin.stl"
            write_stl(path, *cube(size=0.4, height=10.0))
            audit = audit_model(path, settings_from({}), P1S)
            self.assertTrue(any("тоньше" in item for item in audit["warnings"]))


class GcodeAuditTests(unittest.TestCase):
    def _slice(self) -> str:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cube.stl"
            write_stl(path, *cube())
            text, _report = slice_model(path, settings_from({}), P1S)
        return text

    def test_own_output_passes_audit(self):
        audit = audit_gcode(self._slice(), P1S)
        self.assertTrue(audit["ok"])
        self.assertTrue(audit["engine_block"])
        self.assertEqual(audit["layers"], 100)
        self.assertGreater(audit["estimated_weight_g"], 0)

    def test_empty_gcode_is_rejected(self):
        with self.assertRaisesRegex(SlicerError, "пустой"):
            audit_gcode("", P1S)

    def test_gcode_without_temperatures_is_rejected(self):
        with self.assertRaisesRegex(SlicerError, "сопла"):
            audit_gcode("G1 X1 Y1 E1\n" * 20, P1S)

    def test_gcode_without_extrusion_is_rejected(self):
        with self.assertRaisesRegex(SlicerError, "выдавливания"):
            audit_gcode("M104 S220\nM140 S60\n" + "G1 X1 Y1\n" * 20, P1S)

    def test_gcode_below_bed_is_rejected(self):
        text = "M104 S220\nM140 S60\n" + "G1 Z-1 E1\n" * 20
        with self.assertRaisesRegex(SlicerError, "ниже стола"):
            audit_gcode(text, P1S)

    def test_foreign_gcode_warns_about_missing_markers(self):
        text = "M104 S220\nM140 S60\n" + "G1 X1 Y1 E1\n" * 20
        audit = audit_gcode(text, P1S)
        self.assertTrue(any("не движком PrintFlow" in item
                            for item in audit["warnings"]))


class SlicerRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import connector.printflow.routes_slicer  # noqa: F401

    def _dispatch(self, method: str, path: str, query: dict | None = None,
                  body: dict | None = None):
        from connector.printflow.router import router
        return router.dispatch(None, method, path, body=body, query=query or {})

    def test_engine_reports_stage_limits(self):
        code, payload = self._dispatch("GET", "/api/slicer/engine")
        self.assertEqual(code, 200)
        self.assertEqual(payload["id"], "printflow")
        self.assertEqual(payload["input_formats"], [".stl"])
        self.assertTrue(payload["unsupported"])

    def test_profile_is_explicit_about_limits(self):
        code, payload = self._dispatch("GET", "/api/slicer/profile")
        self.assertEqual(code, 200)
        self.assertEqual(payload["id"], PROFILE_ID)
        self.assertEqual(payload["printer"], "Bambu Lab P1S")
        self.assertTrue(payload["requires_verified_first_print"])

    def test_profile_says_engine_is_inactive_until_chosen(self):
        code, payload = self._dispatch("GET", "/api/slicer/profile")
        self.assertEqual(code, 200)
        self.assertFalse(payload["engine_active"])
        self.assertFalse(payload["can_slice"])
        self.assertIn("движок", payload["blocked_reason"])

    def test_settings_route_exposes_gates(self):
        code, payload = self._dispatch("GET", "/api/slicer/settings")
        self.assertEqual(code, 200)
        self.assertTrue(payload["manual_only"])
        self.assertIn("layer_height", payload["settings"])
        self.assertFalse(payload["gates"]["engine"])

    def test_profile_payload_is_complete(self):
        payload = profile_payload(P1S, settings_from({}))
        self.assertEqual(payload["bed_mm"], [256.0, 256.0])
        self.assertIn("gyroid", payload["unsupported_patterns"])
        self.assertIn("PLA", payload["materials"])


if __name__ == "__main__":
    unittest.main()

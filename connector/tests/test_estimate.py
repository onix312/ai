"""Тесты разбора 3MF/G-code: время, граммы, материал и цвет."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.estimate import _hex_to_name, estimate_file  # noqa: E402


class EstimateTests(unittest.TestCase):
    def _gcode(self, header: str) -> pathlib.Path:
        tmp = pathlib.Path(tempfile.mkdtemp())
        path = tmp / "model.gcode"
        path.write_text(header + "\nG1 X0 Y0 Z0\n", encoding="utf-8")
        return path

    def test_time_and_grams(self):
        path = self._gcode(";TIME:7200\n;Filament used [g]: 38.5\n")
        est = estimate_file(path)
        self.assertEqual(est["minutes"], 120.0)
        self.assertEqual(est["grams"], 38.5)

    def test_material_and_color_colon_form(self):
        path = self._gcode("; filament_type:PLA\n; filament color:#FF0000\n;TIME:60\n")
        est = estimate_file(path)
        self.assertEqual(est["material"], "PLA")
        self.assertEqual(est["color"], "Красный")

    def test_material_equals_form_and_colour_spelling(self):
        path = self._gcode("; filament_type = PETG\n; filament_colour = #00AE42\n")
        est = estimate_file(path)
        self.assertEqual(est["material"], "PETG")
        self.assertEqual(est["color"], "Зелёный")

    def test_hex_to_name_neutrals(self):
        self.assertEqual(_hex_to_name("000000"), "Чёрный")
        self.assertEqual(_hex_to_name("FFFFFF"), "Белый")
        self.assertEqual(_hex_to_name("808080"), "Серый")

    def test_meters_fallback(self):
        path = self._gcode(";Filament used: 10.0\n")
        est = estimate_file(path)
        self.assertAlmostEqual(est["grams"], 12.4, places=1)


if __name__ == "__main__":
    unittest.main()

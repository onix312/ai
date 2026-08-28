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
        """Метры → граммы: метр прутка 1.75 мм — это ≈2.98 г PLA, не 1.24 г.

        Константа 1.24 г/м была плотностью PLA (г/см³), а не массой метра:
        смета занижалась в 2.4 раза.
        """
        path = self._gcode(";Filament used: 10.0\n")
        est = estimate_file(path)
        self.assertAlmostEqual(est["grams"], 29.8, places=1)

    # --------------------------------------------- идея 26: плотность материала
    def test_meters_use_material_density(self):
        """Один и тот же метраж у разных пластиков даёт разную массу."""
        from connector.printflow.estimate import meters_to_grams
        self.assertAlmostEqual(meters_to_grams(100, "PLA"), 298.3, places=1)
        petg = meters_to_grams(100, "PETG")
        tpu = meters_to_grams(100, "TPU")
        self.assertGreater(petg, meters_to_grams(100, "PLA"))
        self.assertLess(tpu, meters_to_grams(100, "PLA"))

    def test_meters_from_gcode_use_filament_type(self):
        path = self._gcode("; filament_type = PETG\n;Filament used: 10.0\n")
        est = estimate_file(path)
        self.assertEqual(est["material"], "PETG")
        # PETG плотнее PLA: 10 м — 30.5 г против 29.8 г у PLA
        self.assertAlmostEqual(est["grams"], 30.5, places=1)

    def test_mm_path_unchanged_and_material_aware(self):
        from connector.printflow.estimate import mm_to_grams
        self.assertAlmostEqual(mm_to_grams(1000, "PLA"), 3.0, places=1)
        self.assertGreater(mm_to_grams(1000, "PETG"), mm_to_grams(1000, "PLA"))

    def test_filament_diameter_respected(self):
        """2.85-мм пруток тяжелее метра 1.75-мм при той же длине."""
        from connector.printflow.estimate import meters_to_grams
        self.assertGreater(meters_to_grams(100, "PLA", 2.85),
                           meters_to_grams(100, "PLA", 1.75))


if __name__ == "__main__":
    unittest.main()

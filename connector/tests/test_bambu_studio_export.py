import io
import json
import zipfile
import unittest
from connector.printflow.materials import (
    generate_bambu_studio_filament_preset,
    bambu_filament_preset,
)


class TestBambuStudioFilamentExport(unittest.TestCase):
    def test_generate_preset_fields(self):
        spool = {
            "id": "sp_test_1",
            "material": "PETG",
            "brand": "BestFilament",
            "color_name": "Синий",
            "color_hex": "#1E40AF",
            "total_grams": 1000,
            "remaining_grams": 950,
            "price": 2200,
            "price_per_kg": 2200,
        }
        preset = generate_bambu_studio_filament_preset(spool)

        self.assertEqual(preset["type"], "filament")
        self.assertEqual(preset["from"], "User")
        self.assertEqual(preset["inherits"], "Generic PETG")
        self.assertEqual(preset["name"], "BestFilament PETG Синий")
        self.assertEqual(preset["filament_type"], ["PETG"])
        self.assertEqual(preset["filament_vendor"], ["BestFilament"])
        self.assertEqual(preset["default_filament_colour"], ["#1E40AF"])
        self.assertEqual(preset["filament_cost"], ["2200.0"])
        self.assertTrue(len(preset["nozzle_temperature"]) > 0)
        self.assertTrue(len(preset["textured_plate_temp"]) > 0)
        self.assertIn("1.9", preset["version"])

    def test_generate_preset_bambu_brand(self):
        spool = {
            "id": "sp_test_2",
            "material": "PLA",
            "brand": "Bambu Lab",
            "color_name": "Белый",
            "color_hex": "#FFFFFF",
            "price_per_kg": 2600,
        }
        preset = generate_bambu_studio_filament_preset(spool)
        self.assertEqual(preset["filament_type"], ["PLA"])
        self.assertEqual(preset["filament_vendor"], ["Bambu Lab"])
        self.assertEqual(preset["name"], "Bambu Lab PLA Белый")

    def test_zip_export_structure(self):
        spool = {
            "id": "sp_test_3",
            "material": "TPU",
            "brand": "FDplast",
            "color_name": "Чёрный",
            "color_hex": "#000000",
            "total_grams": 1000,
            "price": 1900,
        }
        preset = generate_bambu_studio_filament_preset(spool)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("FDplast TPU Чёрный.json", json.dumps(preset).encode("utf-8"))

        buf.seek(0)
        with zipfile.ZipFile(buf, "r") as zf:
            names = zf.namelist()
            self.assertIn("FDplast TPU Чёрный.json", names)
            data = json.loads(zf.read("FDplast TPU Чёрный.json").decode("utf-8"))
            self.assertEqual(data["filament_type"], ["TPU"])
            self.assertEqual(data["default_filament_colour"], ["#000000"])


if __name__ == "__main__":
    unittest.main()

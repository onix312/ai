import io
import json
import unittest
import zipfile
from connector.printflow.materials import (
    generate_bambu_studio_filament_preset,
    bambu_filament_preset,
)
from connector.printflow.repo import Repo
from connector.tests.test_phase11 import make_db


class RecIsolationTests(unittest.TestCase):
    """Рекомендации производителя — только на своей катушке.

    Жалоба владельца: значения со стикера одной бобины «уезжали» на все
    катушки материала и в экспорт профилей Bambu Studio. Контракт: три
    уровня — справочник (значение по умолчанию) → катушка (факт со стикера)
    → профиль Bambu (экспорт); правка одной катушки не меняет чужие пресеты.
    """

    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.repo = Repo(self.db)

    def test_two_spools_keep_different_recommendations(self):
        """Две катушки одного материала — разные рекомендации в экспорте."""
        a = self.repo.save_spool({
            "material": "PETG", "brand": "FDplast", "color_name": "Синий",
            "rec_settings": json.dumps({"temp_nozzle": [235, 250], "temp_bed": [80, 90]}),
        })
        b = self.repo.save_spool({
            "material": "PETG", "brand": "FDplast", "color_name": "Красный",
            "rec_settings": json.dumps({"temp_nozzle": [245, 260], "temp_bed": [70, 75]}),
        })
        pa = generate_bambu_studio_filament_preset(self.repo.spool(a["id"]))
        pb = generate_bambu_studio_filament_preset(self.repo.spool(b["id"]))
        self.assertNotEqual(pa["nozzle_temperature_range_low"],
                            pb["nozzle_temperature_range_low"])
        self.assertEqual(pa["nozzle_temperature_range_low"], ["235"])
        self.assertEqual(pb["nozzle_temperature_range_low"], ["245"])
        self.assertEqual(pa["textured_plate_temp"], ["80"])
        self.assertEqual(pb["textured_plate_temp"], ["70"])

    def test_saving_one_spool_does_not_touch_others(self):
        """Сохранение катушки меняет только её rec, соседние не затрагиваются."""
        a = self.repo.save_spool({
            "material": "PLA", "color_name": "Белый",
            "rec_settings": json.dumps({"temp_nozzle": [205, 225]}),
        })
        b = self.repo.save_spool({
            "material": "PLA", "color_name": "Чёрный",
            "rec_settings": json.dumps({"temp_nozzle": [215, 235]}),
        })
        self.repo.save_spool({
            "id": a["id"],
            "rec_settings": json.dumps({"temp_nozzle": [200, 210]}),
        })
        self.assertEqual(json.loads(self.repo.spool(a["id"])["rec_settings"])["temp_nozzle"],
                         [200, 210])
        self.assertEqual(json.loads(self.repo.spool(b["id"])["rec_settings"])["temp_nozzle"],
                         [215, 235])

    def test_empty_spool_rec_falls_back_to_material_catalog(self):
        """Нет стикера — экспорт берёт температуры из справочника материалов."""
        from connector.printflow.materials import get_material, seed_builtin_materials
        # Каталог живёт в базе: сеем встроенные и правим PETG под себя —
        # так это делает карточка «Материалы для печати».
        seed_builtin_materials(self.db)
        row = self.db.one("SELECT * FROM materials WHERE key='PETG'")
        self.repo.save_material({
            "id": row["id"], "name": row["name"], "key": "PETG",
            "temp_nozzle_min": 240, "temp_nozzle_max": 250,
            "temp_bed_min": 75, "temp_bed_max": 85,
        })
        self.repo.save_spool({"material": "PETG", "color_name": "Без стикера"})
        spool = self.db.one("SELECT * FROM spools WHERE material='PETG'")
        preset = generate_bambu_studio_filament_preset(spool, db=self.db)
        self.assertEqual(preset["nozzle_temperature_range_low"], ["240"])
        self.assertEqual(preset["nozzle_temperature_range_high"], ["250"])
        self.assertEqual(preset["textured_plate_temp"], ["75"])
        # а без db — прежний встроенный каталог (контракт обратной совместимости)
        builtin = generate_bambu_studio_filament_preset(spool)
        catalog = get_material("PETG")
        self.assertEqual(builtin["nozzle_temperature_range_low"],
                         [str(catalog["temp_nozzle"][0])])

    def test_spool_sticker_beats_material_catalog(self):
        """Стикер катушки сильнее справочника (первый уровень — катушка)."""
        from connector.printflow.materials import seed_builtin_materials
        seed_builtin_materials(self.db)
        row = self.db.one("SELECT * FROM materials WHERE key='PETG'")
        self.repo.save_material({
            "id": row["id"], "name": row["name"], "key": "PETG",
            "temp_nozzle_min": 240, "temp_nozzle_max": 250,
        })
        spool = self.repo.save_spool({
            "material": "PETG", "color_name": "Стикер",
            "rec_settings": json.dumps({"temp_nozzle": [222, 235]}),
        })
        preset = generate_bambu_studio_filament_preset(self.repo.spool(spool["id"]),
                                                       db=self.db)
        self.assertEqual(preset["nozzle_temperature_range_low"], ["222"])
        self.assertEqual(preset["nozzle_temperature_range_high"], ["235"])

    def test_empty_values_stay_unset_no_zero_temps(self):
        """Регресс 18.6.3: пустое поле = «не задано», нули не появляются."""
        self.repo.save_spool({
            "material": "PLA", "color_name": "Регресс",
            "rec_settings": json.dumps({"dry_temp": 50}),
        })
        row = self.db.one("SELECT * FROM spools WHERE color_name='Регресс'")
        preset = generate_bambu_studio_filament_preset(row, db=self.db)
        nozzle_lo = int(preset["nozzle_temperature_range_low"][0])
        nozzle_hi = int(preset["nozzle_temperature_range_high"][0])
        bed = int(preset["textured_plate_temp"][0])
        self.assertGreater(nozzle_lo, 0)
        self.assertGreaterEqual(nozzle_hi, nozzle_lo)
        self.assertGreater(bed, 0)

    def test_presets_route_isolates_spools(self):
        """Маршрут /api/spools/presets отдаёт пресеты по каждой катушке, не по материалу."""
        self.repo.save_spool({
            "material": "PETG", "color_name": "Синий",
            "rec_settings": json.dumps({"temp_nozzle": [235, 250]}),
        })
        self.repo.save_spool({
            "material": "PETG", "color_name": "Красный",
            "rec_settings": json.dumps({"temp_nozzle": [245, 260]}),
        })
        import types
        from connector.printflow.api import Api
        api = Api.__new__(Api)
        api.db = self.db
        api.repo = self.repo
        api.bus = types.SimpleNamespace(publish=lambda *a, **k: None)
        from connector.printflow.router import register_all
        register_all()
        code, body = api.get("/api/spools/presets", {})
        self.assertEqual(code, 200)
        petg = [p for p in body["presets"] if p["name"].startswith("PETG") or "PETG" in p["filament_type"]]
        nozzle_values = {p["nozzle_temperature_range_low"][0] for p in petg}
        self.assertEqual(nozzle_values, {"235", "245"},
                         "экспорт свёл разные катушки в один профиль")


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

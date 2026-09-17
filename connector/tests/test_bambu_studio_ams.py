"""Тесты интеграции катушек склада с AMS и Bambu Studio.

Проверяем:
1. Автоматический расчёт канонических параметров Bambu (tray_type, tray_info_idx,
   tray_sub_brands, nozzle_temp_min/max) для сторонних катушек без RFID
   (BestFilament, FDplast, eSun, Syntech и др.) и катушек марки Bambu.
2. Команду принтера ams_filament: публикация корректного MQTT-пакета
   ams_filament_setting с кодом пресета (GFL99, GFG99 и др.) и брендом.
3. Маршрут /api/spool/bind: привязка катушки склада к слоту AMS автоматически
   передаёт профиль катушки в принтер, обновляя отображение в Bambu Studio.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.materials import (  # noqa: E402
    BAMBU_FILAMENT_PRESETS,
    bambu_filament_preset,
    normalize_bambu_material_key,
)
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402
from connector.printflow.api import Api  # noqa: E402


class BambuFilamentPresetTests(unittest.TestCase):
    """Каноническая нормализация материалов и кодов пресетов Bambu Studio."""

    def test_generic_non_bambu_spools(self):
        # Сторонние катушки без RFID получают Generic-коды Bambu Studio
        petg = bambu_filament_preset("PETG", "BestFilament", (220, 250))
        self.assertEqual(petg["tray_type"], "PETG")
        self.assertEqual(petg["tray_info_idx"], "GFG99")
        self.assertEqual(petg["tray_sub_brands"], "BestFilament")
        self.assertEqual(petg["nozzle_temp_min"], 220)
        self.assertEqual(petg["nozzle_temp_max"], 250)
        self.assertEqual(petg["preset_name"], "Generic PETG")
        self.assertFalse(petg["is_bambu"])

        pla = bambu_filament_preset("PLA", "FDplast")
        self.assertEqual(pla["tray_type"], "PLA")
        self.assertEqual(pla["tray_info_idx"], "GFL99")
        self.assertEqual(pla["tray_sub_brands"], "FDplast")
        self.assertEqual(pla["preset_name"], "Generic PLA")

        tpu = bambu_filament_preset("TPU 95A", "eSun")
        self.assertEqual(tpu["tray_type"], "TPU")
        self.assertEqual(tpu["tray_info_idx"], "GFU99")

        abs_m = bambu_filament_preset("ABS", "Syntech")
        self.assertEqual(abs_m["tray_type"], "ABS")
        self.assertEqual(abs_m["tray_info_idx"], "GFB99")

        asa_m = bambu_filament_preset("ASA", "")
        self.assertEqual(asa_m["tray_type"], "ASA")
        self.assertEqual(asa_m["tray_info_idx"], "GFB98")

        pa_cf = bambu_filament_preset("PA-CF", "Nova")
        self.assertEqual(pa_cf["tray_type"], "PA-CF")
        self.assertEqual(pa_cf["tray_info_idx"], "GFN98")

    def test_bambu_brand_presets(self):
        # Фирменные катушки Bambu получают оригинальные пресеты Bambu
        pla = bambu_filament_preset("PLA", "Bambu Lab")
        self.assertEqual(pla["tray_type"], "PLA")
        self.assertEqual(pla["tray_info_idx"], "GFA00")
        self.assertEqual(pla["preset_name"], "Bambu PLA Basic")
        self.assertTrue(pla["is_bambu"])

        matte = bambu_filament_preset("PLA Matte", "Bambu")
        self.assertEqual(matte["tray_type"], "PLA")
        self.assertEqual(matte["tray_info_idx"], "GFA01")
        self.assertEqual(matte["preset_name"], "Bambu PLA Matte")

        petg = bambu_filament_preset("PETG", "Bambu")
        self.assertEqual(petg["tray_type"], "PETG")
        self.assertEqual(petg["tray_info_idx"], "GFG00")
        self.assertEqual(petg["preset_name"], "Bambu PETG Basic")

    def test_russian_and_composite_aliases(self):
        self.assertEqual(normalize_bambu_material_key("ПЕТГ"), "PETG")
        self.assertEqual(normalize_bambu_material_key("ПЭТГ"), "PETG")
        self.assertEqual(normalize_bambu_material_key("PET-G"), "PETG")
        self.assertEqual(normalize_bambu_material_key("ПЛА"), "PLA")
        self.assertEqual(normalize_bambu_material_key("ПЛА Шелк"), "PLA_SILK")
        self.assertEqual(normalize_bambu_material_key("PLA+"), "PLA")
        self.assertEqual(normalize_bambu_material_key("АБС"), "ABS")
        self.assertEqual(normalize_bambu_material_key("ТПУ"), "TPU")
        self.assertEqual(normalize_bambu_material_key("PETG-CF"), "PETG_CF")
        self.assertEqual(normalize_bambu_material_key("PLA-CF"), "PLA_CF")
        self.assertEqual(normalize_bambu_material_key("PAHT-CF"), "PA_CF")


class BambuPrinterCommandTests(unittest.TestCase):
    """Команда ams_filament формирует корректный MQTT-пакет для принтера."""

    def test_ams_filament_command_generates_bambu_setting(self):
        from connector.printflow.bambu import BambuPrinter

        printer = BambuPrinter({
            "id": "p1", "name": "P1S", "serial": "01P00A123456789",
            "host": "127.0.0.1", "access_code": "12345678", "enabled": 1,
        })
        published = []
        printer.publish = lambda payload: published.append(payload)

        # Передаём сторонний пластик со склада: BestFilament PETG
        printer.command("ams_filament", {
            "ams_id": 0,
            "tray_id": 1,
            "type": "PETG",
            "brand": "BestFilament",
            "color": "#336699",
            "temp_min": 225,
            "temp_max": 250,
        })

        self.assertEqual(len(published), 1)
        cmd = published[0].get("print") or {}
        self.assertEqual(cmd.get("command"), "ams_filament_setting")
        self.assertEqual(cmd.get("ams_id"), 0)
        self.assertEqual(cmd.get("tray_id"), 1)
        self.assertEqual(cmd.get("tray_type"), "PETG")
        self.assertEqual(cmd.get("tray_info_idx"), "GFG99")
        self.assertEqual(cmd.get("tray_sub_brands"), "BestFilament")
        self.assertEqual(cmd.get("tray_color"), "336699FF")
        self.assertEqual(cmd.get("nozzle_temp_min"), 225)
        self.assertEqual(cmd.get("nozzle_temp_max"), 250)


class SpoolBindAmsIntegrationTests(unittest.TestCase):
    """Привязка катушки склада к слоту AMS в /api/spool/bind."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db = Database(self.tmp.name)
        self.repo = Repo(self.db)

    def tearDown(self):
        try:
            pathlib.Path(self.tmp.name).unlink(missing_ok=True)
        except Exception:
            pass

    def test_bind_warehouse_spool_pushes_bambu_preset(self):
        spool = self.repo.save_spool({
            "name": "Petg Blue",
            "material": "PETG",
            "brand": "BestFilament",
            "color_name": "Синий",
            "color_hex": "#0055ff",
            "remaining_grams": 800,
            "rec_settings": "сопло 225-245°C, стол 70°C",
        })

        class FakeManager:
            def __init__(self):
                self.commands = []
            def get(self, pid):
                return self
            def command(self, name, val):
                self.commands.append((name, val))

        fake_mgr = FakeManager()
        api = Api.__new__(Api)
        api.db = self.db
        api.repo = self.repo
        api.manager = fake_mgr

        code, res = api.post("/api/spool/bind", {
            "id": spool["id"],
            "printer_id": "p1",
            "ams_slot": "0",
            "push_ams": True,
            "confirmed": True,
        }, {})

        self.assertEqual(code, 200)
        self.assertTrue(res["ok"])
        self.assertTrue(res["pushed"])
        self.assertIn("bambu_preset", res)
        preset = res["bambu_preset"]
        self.assertEqual(preset["tray_type"], "PETG")
        self.assertEqual(preset["tray_info_idx"], "GFG99")
        self.assertEqual(preset["tray_sub_brands"], "BestFilament")
        self.assertEqual(preset["nozzle_temp_min"], 225)
        self.assertEqual(preset["nozzle_temp_max"], 245)

        # Проверяем, что команда на принтер реально ушла с правильными полями
        self.assertEqual(len(fake_mgr.commands), 1)
        name, val = fake_mgr.commands[0]
        self.assertEqual(name, "ams_filament")
        self.assertEqual(val["ams_id"], 0)
        self.assertEqual(val["tray_id"], 0)
        self.assertEqual(val["type"], "PETG")
        self.assertEqual(val["brand"], "BestFilament")
        self.assertEqual(val["temp_min"], 225)
        self.assertEqual(val["temp_max"], 245)

    def test_bind_spool_without_rec_settings_uses_material_defaults(self):
        spool = self.repo.save_spool({
            "name": "Pla White",
            "material": "PLA",
            "brand": "eSun",
            "color_name": "Белый",
            "color_hex": "#ffffff",
            "remaining_grams": 1000,
        })

        class FakeManager:
            def __init__(self):
                self.commands = []
            def get(self, pid):
                return self
            def command(self, name, val):
                self.commands.append((name, val))

        fake_mgr = FakeManager()
        api = Api.__new__(Api)
        api.db = self.db
        api.repo = self.repo
        api.manager = fake_mgr

        code, res = api.post("/api/spool/bind", {
            "id": spool["id"],
            "printer_id": "p1",
            "ams_slot": "1",
            "push_ams": True,
            "confirmed": True,
        }, {})

        self.assertEqual(code, 200)
        self.assertTrue(res["ok"])
        self.assertTrue(res["pushed"])
        preset = res["bambu_preset"]
        self.assertEqual(preset["tray_type"], "PLA")
        self.assertEqual(preset["tray_info_idx"], "GFL99")
        self.assertEqual(preset["nozzle_temp_min"], 190)
        self.assertEqual(preset["nozzle_temp_max"], 240)

        self.assertEqual(len(fake_mgr.commands), 1)
        name, val = fake_mgr.commands[0]
        self.assertEqual(val["tray_id"], 1)
        self.assertEqual(val["type"], "PLA")
        self.assertEqual(val["brand"], "eSun")


class StudioGatewayAmsTests(unittest.TestCase):
    """Шлюз Bambu Studio передаёт раскладку AMS со слотами и пресетами."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db = Database(self.tmp.name)

    def tearDown(self):
        try:
            pathlib.Path(self.tmp.name).unlink(missing_ok=True)
        except Exception:
            pass

    def test_gateway_ams_status_populates_trays(self):
        from connector.printflow.studio_gateway import StudioGateway

        class FakePrinter:
            def snapshot(self):
                return {
                    "ams": {
                        "trays": [
                            {"slot": 0, "type": "PETG", "color": "#0055ff", "remain": 85,
                             "present": True, "bambulab": False, "uuid": "", "nozzle_min": 220, "nozzle_max": 250},
                            {"slot": 1, "type": "PLA", "color": "#ff0000", "remain": 50,
                             "present": True, "bambulab": True, "uuid": "BBL123", "nozzle_min": 200, "nozzle_max": 230},
                        ]
                    }
                }

        class FakeManager:
            def get(self, pid=""):
                return FakePrinter()

        gw = StudioGateway(self.db, FakeManager(), bind=False)
        ams = gw._ams_status()
        self.assertEqual(ams["ams_exist_bits"], "1")
        self.assertEqual(len(ams["ams"]), 1)
        trays = ams["ams"][0]["tray"]
        self.assertEqual(len(trays), 2)
        # Первый слот — сторонний PETG -> Generic PETG (GFG99)
        self.assertEqual(trays[0]["tray_type"], "PETG")
        self.assertEqual(trays[0]["tray_info_idx"], "GFG99")
        self.assertEqual(trays[0]["tray_color"], "0055FFFF")
        # Второй слот — Bambu PLA
        self.assertEqual(trays[1]["tray_type"], "PLA")


if __name__ == "__main__":
    unittest.main()

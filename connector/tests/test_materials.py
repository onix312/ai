"""Тесты справочника материалов: встроенные типы пластика и свои материалы.

Свои материалы (таблица materials) — собственные характеристики печати
(температуры, обдув, скорость) и их стоимость (цена за кг). Свои материалы
приоритетнее встроенных и видны в калькуляторе и расчётах себестоимости.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402
from connector.printflow.accounting import Accounting  # noqa: E402


class BuiltinCatalogTests(unittest.TestCase):
    """Встроенный справочник покрывает все реальные типы пластика."""

    def test_builtin_materials_cover_main_families(self):
        from connector.printflow.materials import MATERIALS, material_list
        keys = set(MATERIALS)
        for expected in (
                "PLA", "PLA+", "PLA_SILK", "PLA_MATTE", "PLA_WOOD", "PLA_MARBLE",
                "PLA_GLOW", "PLA_METAL", "PLA_CF",
                "PETG", "PETG_CF", "PETG_ESD", "PET", "PET_CF",
                "ABS", "ABS_GF", "ASA", "PC", "PC_ABS",
                "PA", "PA_GF", "PAHT_CF", "PPA_CF",
                "PPS", "PPS_CF", "PBT", "POM", "PP", "PVB", "PMMA",
                "TPU", "TPE", "HIPS", "PVA", "BVOH", "PEI", "PEEK", "PEKK"):
            self.assertIn(expected, keys, f"нет встроенного материала {expected}")
        # в плоском списке у всех есть ключевые поля для калькулятора
        for item in material_list():
            self.assertTrue(item.get("key"))
            self.assertTrue(item.get("name"))
            self.assertGreater(item["price_per_kg"], 0)
            self.assertGreater(item["speed_factor"], 0)

    def test_get_material_aliases(self):
        from connector.printflow.materials import get_material
        self.assertEqual(get_material("NYLON")["name"], "PA (Nylon)")
        self.assertEqual(get_material("PEEK")["name"], "PEEK")
        self.assertEqual(get_material("PLA WOOD")["name"], "PLA Wood")
        self.assertEqual(get_material("УЛЬТЕМ-НИЧЕГО")["name"], "PLA")  # неизвестный → PLA

    def test_supermaterials_have_temperature_warnings(self):
        from connector.printflow.materials import get_material
        for key in ("PEEK", "PEKK", "PEI"):
            self.assertIn("P1S", get_material(key)["weaknesses"])


class CustomMaterialsTests(unittest.TestCase):
    """Свои пластики: создание, шаблоны, приоритет в расчётах, удаление."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "test.sqlite3")
        self.repo = Repo(self.db)
        self.acc = Accounting(self.db)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_save_custom_material_with_template(self):
        # Только имя и шаблон PETG — параметры и цена берутся из шаблона.
        mat = self.repo.save_material({
            "name": "Мой PETG прозрачный",
            "key": "my_petg_clear",
            "base": "PETG",
        })
        self.assertEqual(mat["key"], "MY_PETG_CLEAR")
        self.assertEqual(mat["temp_nozzle"], (230, 260))   # из PETG
        self.assertEqual(mat["temp_bed"], (70, 85))
        self.assertEqual(mat["price_per_kg"], 1900)
        self.assertAlmostEqual(mat["speed_factor"], 0.8)
        self.assertTrue(mat["custom"])

    def test_save_custom_material_own_params(self):
        # Свои характеристики печати и своя стоимость — приоритетнее шаблона.
        mat = self.repo.save_material({
            "name": "PETG медленный для оснастки",
            "key": "MY_PETG_SLOW",
            "base": "PETG",
            "temp_nozzle_min": 250, "temp_nozzle_max": 270,
            "temp_bed_min": 80, "temp_bed_max": 90,
            "speed_factor": 0.5, "price_per_kg": 1500,
            "fan": 30, "chamber": "closed_hot",
            "abrasive": 0, "uv_resistant": 1,
        })
        self.assertEqual(mat["temp_nozzle"], (250, 270))
        self.assertEqual(mat["temp_bed"], (80, 90))
        self.assertEqual(mat["price_per_kg"], 1500)
        self.assertAlmostEqual(mat["speed_factor"], 0.5)
        self.assertEqual(mat["fan"], 30)
        self.assertEqual(mat["chamber"], "closed_hot")
        self.assertTrue(mat["uv_resistant"])

    def test_custom_key_cannot_shadow_builtin(self):
        with self.assertRaises(ValueError):
            self.repo.save_material({"name": "Подделка", "key": "PETG"})

    def test_custom_key_duplicate_rejected(self):
        self.repo.save_material({"name": "Один", "key": "MY_X", "base": "PLA"})
        with self.assertRaises(ValueError):
            self.repo.save_material({"name": "Два", "key": "MY_X", "base": "PLA"})

    def test_custom_material_used_in_cost_breakdown(self):
        # Своя цена за кг влияет на себестоимость пластика.
        self.repo.save_material({
            "name": "Дешёвый PLA", "key": "MY_CHEAP_PLA", "base": "PLA",
            "price_per_kg": 1000, "speed_factor": 1.0,
        })
        br = self.acc.cost_breakdown(grams=100, hours=1, material="MY_CHEAP_PLA",
                                     spool_price=None, spool_weight=None)
        # 100 г × 1000 ₽/кг = 100 ₽ (без поддержек и продувки)
        self.assertAlmostEqual(br["filament"], 100.0, places=1)
        # а встроенный PLA — дороже
        br2 = self.acc.cost_breakdown(grams=100, hours=1, material="PLA",
                                      spool_price=None, spool_weight=None)
        self.assertGreater(br2["filament"], br["filament"])

    def test_custom_material_speed_affects_time(self):
        self.repo.save_material({
            "name": "Медленный TPU", "key": "MY_SLOW", "base": "TPU",
            "speed_factor": 0.25, "price_per_kg": 3000,
        })
        br = self.acc.cost_breakdown(grams=100, hours=1, material="MY_SLOW",
                                     spool_price=None, spool_weight=None)
        # 1 час печати на скорости 0.25× → в 4 раза дольше
        self.assertGreater(br["total_hours"], 3.5)

    def test_material_options_include_customs(self):
        self.repo.save_material({"name": "Свой ABS", "key": "MY_ABS", "base": "ABS"})
        opts = self.acc.material_options()
        keys = [m["key"] for m in opts["materials"]]
        self.assertIn("MY_ABS", keys)
        self.assertIn("PEEK", keys)
        full = {m["key"]: m for m in opts["materials_full"]}
        self.assertTrue(full["MY_ABS"]["custom"])
        self.assertFalse(full["PLA"]["custom"])
        self.assertTrue(full["PLA"]["builtin"])

    def test_get_material_prefers_custom_over_builtin(self):
        from connector.printflow.materials import get_material
        self.repo.save_material({
            "name": "Мой материал", "key": "MY_PLA_X", "base": "PLA",
            "price_per_kg": 777,
        })
        got = get_material("MY_PLA_X", db=self.db)
        self.assertEqual(got["price_per_kg"], 777)
        self.assertEqual(got["name"], "Мой материал")

    def test_delete_material_archives(self):
        mat = self.repo.save_material({"name": "На выброс", "key": "MY_JUNK"})
        self.repo.delete_material(mat["id"])
        keys = [m["key"] for m in self.repo.materials()]
        self.assertNotIn("MY_JUNK", keys)
        with self.assertRaises(ValueError):
            self.repo.delete_material("нет-такого")

    def test_recreate_after_delete_restores(self):
        # Убранный материал можно создать заново с тем же ключом — запись
        # «оживает», а не падает на UNIQUE-конфликте.
        mat = self.repo.save_material({"name": "Вернись", "key": "MY_BACK", "base": "PLA"})
        self.repo.delete_material(mat["id"])
        again = self.repo.save_material({"name": "Вернись v2", "key": "MY_BACK", "base": "PLA"})
        self.assertEqual(again["id"], mat["id"])
        self.assertEqual(again["name"], "Вернись v2")
        self.assertIn("MY_BACK", [m["key"] for m in self.repo.materials()])

    def test_builtin_catalog_is_seeded_into_db(self):
        # Весь каталог лежит в базе (таблица materials), а не только в коде.
        self.repo.materials()  # сидинг по первому обращению
        row = self.db.one(
            "SELECT COUNT(*) n FROM materials WHERE builtin=1 AND archived=0")
        from connector.printflow.materials import MATERIALS
        self.assertEqual(int(row["n"]), len(MATERIALS))
        # Повторный вызов не дублирует
        self.repo.materials()
        row2 = self.db.one(
            "SELECT COUNT(*) n FROM materials WHERE builtin=1")
        self.assertEqual(int(row2["n"]), len(MATERIALS))

    def test_builtin_material_can_be_tuned(self):
        # Встроенный тип можно настроить под себя (цена, температуры) —
        # запись в базе переопределяет каталог, ключ остаётся каталогным.
        self.repo.materials()
        pla = self.db.one("SELECT * FROM materials WHERE key='PLA' AND builtin=1")
        from connector.printflow.materials import get_material
        mat = self.repo.save_material({
            "id": pla["id"], "name": "PLA", "key": "ВЗЛОМ",
            "base": "PLA", "price_per_kg": 999,
            "temp_nozzle_min": 205, "temp_nozzle_max": 225,
        })
        self.assertEqual(mat["key"], "PLA")          # ключ не сменяем
        self.assertTrue(mat["builtin"])
        self.assertEqual(mat["price_per_kg"], 999)
        self.assertEqual(mat["temp_nozzle"], (205, 225))
        got = get_material("PLA", db=self.db)        # расчёты берут из базы
        self.assertEqual(got["price_per_kg"], 999)
        br = self.acc.cost_breakdown(grams=100, hours=1, material="PLA",
                                     spool_price=None, spool_weight=None)
        self.assertAlmostEqual(br["filament"], 99.9, places=1)

    def test_builtin_reset_returns_catalog_values(self):
        self.repo.materials()
        pla = self.db.one("SELECT * FROM materials WHERE key='PLA' AND builtin=1")
        self.repo.save_material({"id": pla["id"], "name": "PLA",
                                 "price_per_kg": 999, "base": "PLA"})
        self.repo.reset_material(pla["id"])
        from connector.printflow.materials import MATERIALS, get_material
        got = get_material("PLA", db=self.db)
        self.assertEqual(got["price_per_kg"], MATERIALS["PLA"]["price_per_kg"])
        # сброс своего материала запрещён
        custom = self.repo.save_material({"name": "Свой", "key": "MY_R", "base": "PLA"})
        with self.assertRaises(ValueError):
            self.repo.reset_material(custom["id"])

    def test_delete_builtin_falls_back_to_catalog(self):
        self.repo.materials()
        pla = self.db.one("SELECT * FROM materials WHERE key='PLA' AND builtin=1")
        self.repo.delete_material(pla["id"])
        # тип не пропадает из справочника — берётся из каталога
        keys = [m["key"] for m in self.acc.material_options()["materials"]]
        self.assertIn("PLA", keys)

    def test_api_materials_save_and_delete(self):
        from connector.printflow.api import Api
        api = Api.__new__(Api)
        api.db = self.db
        api.repo = self.repo
        api.acc = self.acc
        code, body = api.post("/api/materials/save",
                              {"name": "Через API", "key": "MY_API", "base": "PETG"}, {})
        self.assertEqual(code, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body["material"]["key"], "MY_API")
        code, body = api.get("/api/materials", {})
        self.assertEqual(code, 200)
        keys = [m["key"] for m in body["materials_full"]]
        self.assertIn("MY_API", keys)
        custom = [m for m in body["materials_full"] if m["key"] == "MY_API"][0]
        api.post("/api/materials/delete", {"id": custom["id"]}, {})
        self.assertNotIn("MY_API", [m["key"] for m in api.repo.materials()])


if __name__ == "__main__":
    unittest.main()

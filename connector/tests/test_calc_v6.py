"""Тесты калькулятора: модель «плита vs штука», материалы, сценарии, окупаемость."""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.materials import (  # noqa: E402
    MATERIALS, QUALITY_PROFILES, get_material, get_profile,
    material_list, profile_list, recommend_material,
)


class MaterialsTests(unittest.TestCase):
    def test_all_materials_have_required_fields(self):
        for key, m in MATERIALS.items():
            self.assertIn("name", m, key)
            self.assertIn("density", m, key)
            self.assertIn("price_per_kg", m, key)
            self.assertIn("speed_factor", m, key)
            self.assertGreater(m["density"], 0, f"{key}: density > 0")
            self.assertGreater(m["price_per_kg"], 0, f"{key}: price > 0")
            self.assertGreater(m["speed_factor"], 0, f"{key}: speed > 0")

    def test_get_material_alias(self):
        self.assertEqual(get_material("TPU95A")["name"], "TPU 95A")
        self.assertEqual(get_material("PA-CF")["name"], "PAHT-CF")
        self.assertEqual(get_material("")["name"], "PLA")
        self.assertEqual(get_material("UNKNOWN")["name"], "PLA")

    def test_profiles_have_factors(self):
        for key, p in QUALITY_PROFILES.items():
            self.assertIn("time_factor", p, key)
            self.assertIn("filament_factor", p, key)
            self.assertGreater(p["time_factor"], 0, key)

    def test_draft_faster_than_detail(self):
        draft = get_profile("draft")
        detail = get_profile("detail")
        self.assertLess(draft["time_factor"], detail["time_factor"])

    def test_material_list_not_empty(self):
        self.assertGreater(len(material_list()), 10)
        self.assertGreater(len(profile_list()), 2)

    def test_recommend_material(self):
        rec = recommend_material("уличная табличка")
        self.assertIn("ASA", rec)
        rec2 = recommend_material("гибкий чехол")
        self.assertIn("TPU", rec2)
        rec3 = recommend_material("что-то непонятное")
        self.assertIn("PLA", rec3)


class CalcPlateModelTests(unittest.TestCase):
    """Ключевой тест: модель «плита vs штука» считает правильно."""

    def setUp(self):
        self.db = Database(":memory:")
        self.acc = Accounting(self.db)

    def test_plate_model_basic(self):
        """4 адресника на плите: 120 г, 3 ч. Партия 8 шт = 2 плиты."""
        br = self.acc.cost_breakdown(
            grams=0, hours=0, qty=8,
            plate_grams=120, plate_hours=3, fit_per_plate=4,
            material="PLA", quality="standard")
        self.assertEqual(br["plates"], 2)
        self.assertAlmostEqual(br["total_grams"], 240, delta=5)  # 120 × 2
        self.assertAlmostEqual(br["total_hours"], 6, delta=0.2)  # 3 × 2
        self.assertAlmostEqual(br["unit_grams"], 30, delta=1)  # 240 / 8
        self.assertAlmostEqual(br["unit_hours"], 0.75, delta=0.05)  # 6 / 8

    def test_plate_model_with_warmup(self):
        """Прогрев добавляет время к каждой плите."""
        br = self.acc.cost_breakdown(
            grams=0, hours=0, qty=4,
            plate_grams=100, plate_hours=2, fit_per_plate=4,
            warmup_minutes=10, material="PLA")
        self.assertEqual(br["plates"], 1)
        # 2 часа + 10 мин (0.167 ч) = 2.167 ч
        self.assertAlmostEqual(br["total_hours"], 2.167, delta=0.1)

    def test_odd_qty_rounds_up_plates(self):
        """5 штук при fit=4 → 2 плиты (последняя неполная)."""
        br = self.acc.cost_breakdown(
            grams=0, hours=0, qty=5,
            plate_grams=120, plate_hours=3, fit_per_plate=4,
            material="PLA")
        self.assertEqual(br["plates"], 2)
        self.assertAlmostEqual(br["total_grams"], 240, delta=5)

    def test_supports_add_grams(self):
        """Поддержки 20% добавляют 20% к весу."""
        br_no_sup = self.acc.cost_breakdown(
            grams=0, hours=0, qty=4,
            plate_grams=100, plate_hours=2, fit_per_plate=4,
            supports_pct=0, material="PLA")
        br_sup = self.acc.cost_breakdown(
            grams=0, hours=0, qty=4,
            plate_grams=100, plate_hours=2, fit_per_plate=4,
            supports_pct=20, material="PLA")
        self.assertGreater(br_sup["total_grams"], br_no_sup["total_grams"])
        self.assertGreater(br_sup["support_grams"], 0)

    def test_tpu_slower_than_pla(self):
        """TPU печатается в 4 раза медленнее PLA."""
        br_pla = self.acc.cost_breakdown(
            grams=0, hours=0, qty=1,
            plate_grams=50, plate_hours=2, fit_per_plate=1,
            material="PLA", quality="standard")
        br_tpu = self.acc.cost_breakdown(
            grams=0, hours=0, qty=1,
            plate_grams=50, plate_hours=2, fit_per_plate=1,
            material="TPU", quality="standard")
        self.assertGreater(br_tpu["total_hours"], br_pla["total_hours"] * 3)

    def test_detail_slower_than_draft(self):
        """Детальный профиль в ~3 раза дольше чернового."""
        br_draft = self.acc.cost_breakdown(
            grams=0, hours=0, qty=1,
            plate_grams=50, plate_hours=2, fit_per_plate=1,
            quality="draft")
        br_detail = self.acc.cost_breakdown(
            grams=0, hours=0, qty=1,
            plate_grams=50, plate_hours=2, fit_per_plate=1,
            quality="detail")
        self.assertGreater(br_detail["total_hours"], br_draft["total_hours"] * 2)

    def test_color_swaps_add_purge(self):
        """3 смены цвета × 12 г × число плит = продувка."""
        br = self.acc.cost_breakdown(
            grams=0, hours=0, qty=4,
            plate_grams=100, plate_hours=2, fit_per_plate=4,
            color_swaps=3, material="PLA")
        # 1 плита × 3 смены × 12 г = 36 г
        self.assertAlmostEqual(br["purge_grams"], 36, delta=1)

    def test_backward_compat_grams_per_unit(self):
        """Старый режим (без plate_grams): grams × qty."""
        br = self.acc.cost_breakdown(grams=30, hours=2, qty=5, material="PLA")
        # 30 г × 5 шт = 150 г (без поддержек и профиля)
        self.assertAlmostEqual(br["total_grams"], 150, delta=5)

    def test_post_processing_labor(self):
        """Постобработка добавляется к ручной работе."""
        br = self.acc.cost_breakdown(
            grams=0, hours=0, qty=10,
            plate_grams=100, plate_hours=2, fit_per_plate=5,
            remove_minutes=3, sand_minutes=5, paint_minutes=0,
            material="PLA")
        # (3+5) мин × 10 шт = 80 мин × 400 ₽/ч = 533 ₽
        self.assertGreater(br["labor"], 500)


class CalcScenariosTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.acc = Accounting(self.db)

    def test_scenarios_returns_list(self):
        base = {"plate_grams": 100, "plate_hours": 2,
                "fit_per_plate": 4, "qty": 8}
        variants = [
            {"material": "PLA", "label": "PLA"},
            {"material": "PETG", "label": "PETG"},
        ]
        results = self.acc.calc_scenarios(base, variants)
        self.assertEqual(len(results), 2)
        self.assertIn("breakdown", results[0])
        self.assertIn("profit_per_hour", results[0])
        # PETG дороже PLA
        self.assertGreater(results[1]["breakdown"]["filament"],
                           results[0]["breakdown"]["filament"])

    def test_scenarios_different_batches(self):
        """С warmup большая партия выгоднее (прогрев делится на больше штук)."""
        base = {"plate_grams": 100, "plate_hours": 2,
                "fit_per_plate": 4, "warmup_minutes": 10}
        variants = [
            {"qty": 4, "label": "4 шт"},
            {"qty": 20, "label": "20 шт"},
        ]
        results = self.acc.calc_scenarios(base, variants)
        # Больше партия → выше прибыль за час (прогрев делится)
        self.assertGreater(results[1]["profit_per_hour"],
                           results[0]["profit_per_hour"])


class PaybackTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.acc = Accounting(self.db)

    def test_payback_basic(self):
        result = self.acc.payback_calc(
            model_cost=500, profit_per_unit=50, sales_per_week=2)
        self.assertEqual(result["units_needed"], 11)  # 500 / 50 = 10 + 1
        self.assertAlmostEqual(result["weeks_to_payback"], 5.5, delta=0.1)

    def test_payback_with_design(self):
        result = self.acc.payback_calc(
            design_hours=3, profit_per_unit=100, sales_per_week=5)
        self.assertEqual(result["design_cost"], 2400)  # 3 × 800
        self.assertEqual(result["units_needed"], 25)  # 2400 / 100 + 1

    def test_payback_zero(self):
        result = self.acc.payback_calc(profit_per_unit=50)
        self.assertEqual(result["units_needed"], 0)


class MinBatchTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.acc = Accounting(self.db)

    def test_min_batch_finds_qty(self):
        """При target > max достигается только с большим markup."""
        result = self.acc.min_profitable_batch(
            plate_grams=100, plate_hours=2, fit_per_plate=4,
            material="PLA", target_per_hour=120, markup=200)
        # markup 200% и target 120 — должно быть достижимо
        self.assertGreater(result["min_qty"], 0)
        self.assertTrue(len(result["table"]) > 0)


if __name__ == "__main__":
    unittest.main()

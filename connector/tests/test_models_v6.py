"""Тесты реестра моделей: раскладка, подготовка, повторная печать."""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.model_registry import (  # noqa: E402
    ModelRegistry, DEFAULT_PREP_TIMES, PREP_STAGE_NAMES,
)
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.accounting import Accounting  # noqa: E402


class PlateLayoutTests(unittest.TestCase):
    """Калькулятор раскладки: сколько штук влезет на плиту 256×256."""

    def setUp(self):
        self.db = Database(":memory:")
        self.mr = ModelRegistry(self.db)

    def test_small_item_fits_many(self):
        """Адресник 30×20 мм — много штук на плите."""
        layout = self.mr.plate_layout(30, 20)
        self.assertGreater(layout["fit_per_plate"], 20)
        self.assertIn("svg", layout)

    def test_large_item_fits_one(self):
        """Крупная деталь 200×200 мм — 1 штука."""
        layout = self.mr.plate_layout(200, 200)
        self.assertEqual(layout["fit_per_plate"], 1)

    def test_medium_item_fits_several(self):
        """Органайзер 60×40 мм — несколько штук."""
        layout = self.mr.plate_layout(60, 40)
        # 240/(60+3) × 240/(40+3) ≈ 3 × 5 = 15
        self.assertGreater(layout["fit_per_plate"], 8)
        self.assertLess(layout["fit_per_plate"], 20)

    def test_rotation_improves_fit(self):
        """Прямоугольная модель: поворот может улучшить раскладку."""
        # 100×30 мм — прямая: 2×7=14, повёрнутая: 7×2=14 → одинаково
        # но для 80×30: прямая 3×7=21, повёрнутая 7×2=14 → прямая лучше
        layout = self.mr.plate_layout(80, 30)
        self.assertGreater(layout["fit_per_plate"], 10)

    def test_svg_generated(self):
        """SVG-превью раскладки не пустое."""
        layout = self.mr.plate_layout(40, 30)
        self.assertIn("<svg", layout["svg"])
        self.assertIn("rect", layout["svg"])

    def test_utilization_pct(self):
        """Процент использования плиты в разумных пределах."""
        layout = self.mr.plate_layout(50, 50)
        self.assertGreater(layout["utilization_pct"], 0)
        self.assertLessEqual(layout["utilization_pct"], 100)


class PrepTimeTests(unittest.TestCase):
    """Оценка времени подготовки модели."""

    def setUp(self):
        self.db = Database(":memory:")
        self.mr = ModelRegistry(self.db)

    def test_default_prep_simple(self):
        prep = self.mr.estimate_prep_time("simple")
        self.assertAlmostEqual(prep["_total"], 16.0, delta=1)
        self.assertIn("find", prep)
        self.assertIn("slice", prep)

    def test_complex_slower(self):
        simple = self.mr.estimate_prep_time("simple")
        complex_ = self.mr.estimate_prep_time("complex")
        self.assertGreater(complex_["_total"], simple["_total"] * 2)

    def test_custom_stages_override(self):
        prep = self.mr.estimate_prep_time("simple", {"find": 30, "orient": 15})
        self.assertEqual(prep["find"], 30)
        self.assertEqual(prep["orient"], 15)
        # Остальные этапы — дефолтные
        self.assertEqual(prep["duplicate"], DEFAULT_PREP_TIMES["simple"]["duplicate"])

    def test_stage_names_complete(self):
        for key in DEFAULT_PREP_TIMES["simple"]:
            self.assertIn(key, PREP_STAGE_NAMES)


class ModelCRUDTests(unittest.TestCase):
    """CRUD моделей и сессии подготовки."""

    def setUp(self):
        self.db = Database(":memory:")
        self.mr = ModelRegistry(self.db)

    def test_save_and_get(self):
        model = self.mr.save({
            "name": "Адресник круглый",
            "dim_x": 30, "dim_y": 30, "dim_z": 2,
            "file": "tag_v1.stl",
            "complexity": "simple",
        })
        self.assertIn("id", model)
        self.assertEqual(model["name"], "Адресник круглый")
        self.assertGreater(model["fit_per_plate"], 30)  # 30мм → много
        self.assertGreater(model["prep_minutes"], 0)

        fetched = self.mr.get(model["id"])
        self.assertEqual(fetched["name"], "Адресник круглый")
        self.assertIn("layout", fetched)
        self.assertIn("prep", fetched)

    def test_version_created_on_save(self):
        model = self.mr.save({"name": "Тест", "file": "test.stl"})
        fetched = self.mr.get(model["id"])
        self.assertGreater(len(fetched["versions"]), 0)

    def test_list_search(self):
        self.mr.save({"name": "Адресник"})
        self.mr.save({"name": "Табличка"})
        all_models = self.mr.list()
        self.assertEqual(len(all_models), 2)
        found = self.mr.list(search="адресник")
        self.assertEqual(len(found), 1)

    def test_archive(self):
        model = self.mr.save({"name": "Старая модель"})
        self.mr.delete(model["id"])
        all_models = self.mr.list()
        self.assertEqual(len(all_models), 0)

    def test_prep_session(self):
        model = self.mr.save({"name": "Тест подготовки"})
        session = self.mr.start_prep_session(model["id"])
        self.assertEqual(session["result"], "in_progress")
        finished = self.mr.finish_prep_session(
            session["id"],
            stages={"find": 5, "orient": 3, "duplicate": 2,
                    "profile": 2, "supports": 8, "slice": 4})
        self.assertEqual(finished["result"], "done")
        self.assertEqual(finished["duration_minutes"], 24.0)


class ModelPrepInCalcTests(unittest.TestCase):
    """Подготовка модели учитывается в калькуляторе себестоимости."""

    def setUp(self):
        self.db = Database(":memory:")
        self.acc = Accounting(self.db)

    def test_model_prep_adds_cost(self):
        """30 минут подготовки модели → добавляются к ручной работе."""
        br_no_prep = self.acc.cost_breakdown(
            grams=0, hours=0, qty=4,
            plate_grams=100, plate_hours=2, fit_per_plate=4,
            model_prep_minutes=0)
        br_with_prep = self.acc.cost_breakdown(
            grams=0, hours=0, qty=4,
            plate_grams=100, plate_hours=2, fit_per_plate=4,
            model_prep_minutes=30)
        # Подготовка модели увеличивает labor и total
        self.assertGreater(br_with_prep["total"], br_no_prep["total"])
        self.assertEqual(br_with_prep["model_prep"], 30)

    def test_model_prep_spreads_over_batch(self):
        """Подготовка делится на всю партию: 30 мин на 4 шт = 7.5 мин/шт."""
        br = self.acc.cost_breakdown(
            grams=0, hours=0, qty=4,
            plate_grams=100, plate_hours=2, fit_per_plate=4,
            model_prep_minutes=30)
        # model_prep_cost = 30 мин × 400 ₽/ч = 200 ₽
        self.assertAlmostEqual(br["model_prep_cost"], 200, delta=5)
        # per_unit: 200 / 4 = 50 ₽ на штуку за подготовку
        self.assertGreater(br["per_unit"], 0)

    def test_model_prep_one_time_cost(self):
        """Подготовка — разовая: при 10 шт она НЕ умножается на 10."""
        br4 = self.acc.cost_breakdown(
            grams=0, hours=0, qty=4,
            plate_grams=100, plate_hours=2, fit_per_plate=4,
            model_prep_minutes=30)
        br10 = self.acc.cost_breakdown(
            grams=0, hours=0, qty=10,
            plate_grams=100, plate_hours=2, fit_per_plate=4,
            model_prep_minutes=30)
        # model_prep_cost одинаковый: 30 мин × 400/60 = 200 ₽
        self.assertAlmostEqual(br4["model_prep_cost"],
                               br10["model_prep_cost"], delta=5)


class RepeatAndCloneTests(unittest.TestCase):
    """Повторная печать и клонирование партии."""

    def setUp(self):
        self.db = Database(":memory:")
        self.mr = ModelRegistry(self.db)

    def test_repeat_from_model(self):
        model = self.mr.save({
            "name": "Адресник", "file": "tag.3mf",
            "dim_x": 30, "dim_y": 30,
        })
        result = self.mr.repeat_from_model(model["id"], qty=20)
        self.assertEqual(result["qty"], 20)
        self.assertGreater(result["plates"], 0)
        self.assertEqual(result["file"], "tag.3mf")


if __name__ == "__main__":
    unittest.main()

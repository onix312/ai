"""Тесты детектора «спагетти»: метрика кромок и накопление подозрений.

Проверяется чистая логика решения — без камеры и Pillow.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.spaghetti import edge_score, spaghetti_decision  # noqa: E402


class SpaghettiTests(unittest.TestCase):
    def test_edge_score_flat_frame_is_zero(self):
        gray = [[128] * 8 for _ in range(8)]
        self.assertEqual(edge_score(gray), 0.0)

    def test_edge_score_messy_frame_is_higher(self):
        flat = [[128] * 8 for _ in range(8)]
        checker = [[(x + y) % 2 * 255 for x in range(8)] for y in range(8)]
        self.assertGreater(edge_score(checker), edge_score(flat))

    def test_decision_needs_several_strikes(self):
        strikes, fire = spaghetti_decision(5.0, 0, 3.0)
        self.assertFalse(fire)
        strikes, fire = spaghetti_decision(5.0, strikes, 3.0)
        self.assertFalse(fire)
        strikes, fire = spaghetti_decision(5.0, strikes, 3.0)
        self.assertTrue(fire)
        self.assertEqual(strikes, 0)  # после тревоги счётчик сброшен

    def test_decision_forgives_single_blip(self):
        strikes, fire = spaghetti_decision(5.0, 0, 3.0)
        strikes, fire = spaghetti_decision(0.5, strikes, 3.0)
        self.assertFalse(fire)
        self.assertEqual(strikes, 0)

    def test_ratio_below_sensitivity_never_fires(self):
        strikes, fire = spaghetti_decision(2.0, 10, 3.0)
        self.assertFalse(fire)


if __name__ == "__main__":
    unittest.main()

"""Ограничение частоты публичных запросов (идея 34) и ключ клиента.

Без этого любой посетитель трекинга или витрины мог положить коннектор
сотней запросов в секунду; внутренний трафик панели не ограничивается
жёстко, но считается в той же статистике.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.rate_limit import (  # noqa: E402
    DEFAULT_RULES, RateLimiter, client_key)


class RateLimiterTests(unittest.TestCase):
    def test_allows_up_to_limit_then_rejects(self):
        limiter = RateLimiter(rules={"probe": (3, 60)})
        for _ in range(3):
            ok, info = limiter.check("probe", "1.2.3.4")
            self.assertTrue(ok, info)
        ok, info = limiter.check("probe", "1.2.3.4")
        self.assertFalse(ok)
        self.assertEqual(info["limit"], 3)
        self.assertGreaterEqual(info["retry_after"], 1)
        self.assertIn("Слишком много запросов", info["error"])

    def test_keys_are_isolated(self):
        """Лимит на клиента: один шумный посетитель не блокирует остальных."""
        limiter = RateLimiter(rules={"probe": (1, 60)})
        self.assertTrue(limiter.check("probe", "a")[0])
        self.assertFalse(limiter.check("probe", "a")[0])
        self.assertTrue(limiter.check("probe", "b")[0])

    def test_window_releases_old_hits(self):
        limiter = RateLimiter(rules={"probe": (1, 0)})   # окно ноль — всё проходит
        self.assertTrue(limiter.check("probe", "a")[0])
        self.assertTrue(limiter.check("probe", "a")[0])

    def test_unknown_bucket_falls_back_to_default(self):
        limiter = RateLimiter()
        self.assertEqual(limiter.rule("несуществующее"), DEFAULT_RULES["default"])

    def test_reset_clears_counters(self):
        limiter = RateLimiter(rules={"probe": (1, 60)})
        limiter.check("probe", "a")
        self.assertFalse(limiter.check("probe", "a")[0])
        limiter.reset("probe", "a")
        self.assertTrue(limiter.check("probe", "a")[0])

    def test_stats_count_checks_and_rejections(self):
        limiter = RateLimiter(rules={"probe": (1, 60)})
        limiter.check("probe", "a")
        limiter.check("probe", "a")
        stats = limiter.stats()
        self.assertEqual(stats["checked"], 2)
        self.assertEqual(stats["rejected"], 1)
        self.assertEqual(stats["tracked"], 1)

    def test_public_buckets_are_tighter_than_internal(self):
        """Публичные корзины жёстче внутренних: витрина не должна быть дырой."""
        limiter = RateLimiter()
        for bucket in ("public_order", "public_track", "public_catalog", "login"):
            limit, _window = limiter.rule(bucket)
            self.assertLessEqual(limit, DEFAULT_RULES["default"][0], bucket)


class ClientKeyTests(unittest.TestCase):
    def test_forwarded_for_wins(self):
        headers = {"X-Forwarded-For": "9.9.9.9, 10.0.0.1", "X-Real-IP": "8.8.8.8"}
        self.assertEqual(client_key(headers), "9.9.9.9")

    def test_real_ip_is_fallback(self):
        self.assertEqual(client_key({"X-Real-IP": "8.8.8.8"}), "8.8.8.8")

    def test_no_headers_gives_anonymous_key(self):
        self.assertEqual(client_key(None), "unknown")
        self.assertEqual(client_key({}), "unknown")

    def test_object_headers_supported(self):
        """Заголовки сервера — объект с .get, а не словарь."""
        headers = SimpleNamespace(get=lambda name, default=None: "7.7.7.7"
                                  if name == "X-Real-IP" else default)
        self.assertEqual(client_key(headers), "7.7.7.7")


if __name__ == "__main__":
    unittest.main()

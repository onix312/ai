"""Идемпотентность записывающих запросов (идея 5).

Повторная отправка формы (двойной клик, retry браузера, перепосылка
Telegram-вебхука) не должна создавать второй заказ или вторую оплату:
один ключ — один выполненный ответ.
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
from connector.printflow.idempotency import (  # noqa: E402
    IdempotencyStore, extract_key, guarded)


class IdempotencyStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.store = IdempotencyStore(self.db)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_put_then_get_returns_same_response(self):
        self.store.put("key-1", "POST /api/orders", {"ok": True, "id": "o1"})
        found, payload = self.store.get("key-1", "POST /api/orders")
        self.assertTrue(found)
        self.assertEqual(payload, {"ok": True, "id": "o1"})

    def test_unknown_key_is_a_miss(self):
        self.assertEqual(self.store.get("нет-такого", "POST /api/orders"),
                         (False, None))

    def test_empty_key_never_caches(self):
        """Без ключа идемпотентности запрос выполняется как обычный."""
        self.store.put("", "POST /api/orders", {"ok": True})
        self.assertEqual(self.store.get("", "POST /api/orders"), (False, None))

    def test_scope_separates_routes(self):
        """Один ключ на разных маршрутах — разные ответы."""
        self.store.put("same", "POST /api/orders", {"route": "orders"})
        self.store.put("same", "POST /api/payments", {"route": "payments"})
        self.assertEqual(self.store.get("same", "POST /api/orders")[1]["route"], "orders")
        self.assertEqual(self.store.get("same", "POST /api/payments")[1]["route"],
                         "payments")

    def test_repeated_put_overwrites(self):
        self.store.put("key-2", "s", {"v": 1})
        self.store.put("key-2", "s", {"v": 2})
        self.assertEqual(self.store.get("key-2", "s")[1], {"v": 2})

    def test_stats_track_hits_and_misses(self):
        self.store.put("key-3", "s", {"ok": True})
        self.store.get("key-3", "s")
        self.store.get("key-4", "s")
        stats = self.store.stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)


class GuardedActionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.store = IdempotencyStore(self.db)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_action_runs_once_for_the_same_key(self):
        """Второй вызов с тем же ключом отдаёт кэш и помечает его replayed."""
        calls = []

        def action():
            calls.append(1)
            return {"created": len(calls)}

        first, replayed = guarded(self.store, "POST /api/orders", "k", action)
        second, replayed2 = guarded(self.store, "POST /api/orders", "k", action)
        self.assertEqual(len(calls), 1)
        self.assertFalse(replayed)
        self.assertTrue(replayed2)
        self.assertEqual(second["created"], first["created"])
        self.assertTrue(second["replayed"])

    def test_without_key_action_always_runs(self):
        calls = []
        guarded(self.store, "s", "", lambda: calls.append(1))
        guarded(self.store, "s", "", lambda: calls.append(1))
        self.assertEqual(len(calls), 2)

    def test_exception_is_not_cached(self):
        """Упавшее действие не запоминается: повтор имеет право succeed."""
        def boom():
            raise ValueError("сеть")

        with self.assertRaises(ValueError):
            guarded(self.store, "s", "k", boom)
        result, replayed = guarded(self.store, "s", "k", lambda: {"ok": True})
        self.assertFalse(replayed)
        self.assertEqual(result["ok"], True)


class ExtractKeyTests(unittest.TestCase):
    def test_header_wins_over_body(self):
        self.assertEqual(
            extract_key({"request_id": "из-тела"}, {"Idempotency-Key": "из-заголовка"}),
            "из-заголовка")

    def test_alternative_headers(self):
        self.assertEqual(extract_key(None, {"X-Idempotency-Key": "a"}), "a")
        self.assertEqual(extract_key(None, {"X-Request-Id": "b"}), "b")

    def test_body_fallback(self):
        self.assertEqual(extract_key({"request_id": "c"}, None), "c")
        self.assertEqual(extract_key({"idempotency_key": "d"}, {}), "d")

    def test_no_key_anywhere(self):
        self.assertEqual(extract_key(None, None), "")
        self.assertEqual(extract_key({}, {}), "")

    def test_key_is_trimmed_and_capped(self):
        self.assertEqual(extract_key(None, {"Idempotency-Key": "  x  "}), "x")
        self.assertEqual(len(extract_key(None, {"Idempotency-Key": "y" * 500})), 128)


if __name__ == "__main__":
    unittest.main()

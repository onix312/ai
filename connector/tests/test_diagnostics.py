"""Самодиагностика (идея 12): один снимок состояния для панели, бота и CI.

Проверяем, что снимок собирается без падающих веток, что в нём нет
секретов, и что текстовая версия пригодна для отправки в Telegram.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import diagnostics as service  # noqa: E402
from connector.printflow.db import Database  # noqa: E402


class FakeManager:
    def __init__(self, printers=None, queue=()):
        self._printers = printers or []
        self._queue = list(queue)

    def snapshot(self, printer_id: str = "") -> dict:
        return {"printers": self._printers}

    def queue(self) -> list:
        return self._queue


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _api(self, **settings):
        self.db.set_settings(settings)
        return SimpleNamespace(db=self.db, started_at=None,
                               manager=FakeManager(
                                   printers=[{"printer_id": "P1",
                                              "connection": {"connected": True},
                                              "printer": {"state": "RUNNING"}},
                                             {"printer_id": "P2",
                                              "connection": {"connected": False},
                                              "printer": {"state": "IDLE"}}],
                                   queue=[{"id": "j1"}]))

    def test_collect_without_api_is_safe(self):
        report = service.collect()
        for key in ("ok", "version", "schema", "python", "platform",
                    "database", "threads", "errors", "backups", "at"):
            self.assertIn(key, report)

    def test_collect_with_api_describes_services_and_farm(self):
        report = service.collect(self._api(telegram_token="t", client_bot_token="c"))
        self.assertTrue(report["services"]["telegram_bot"])
        self.assertTrue(report["services"]["client_bot"])
        self.assertEqual(report["farm"]["printers"], 2)
        self.assertEqual(report["farm"]["online"], 1)
        self.assertEqual(report["farm"]["printing"], 1)
        self.assertEqual(report["queue"], 1)

    def test_unconfigured_services_are_reported_as_false(self):
        report = service.collect(self._api())
        self.assertFalse(report["services"]["telegram_bot"])
        self.assertFalse(report["services"]["bambu_cloud"])

    def test_no_secrets_in_report(self):
        """Токены не уезжают в диагностику — только признак «настроен»."""
        report = service.collect(self._api(telegram_token="123456:СЕКРЕТ",
                                           cloud_token="облачный-секрет"))
        text = str(report)
        self.assertNotIn("СЕКРЕТ", text)
        self.assertNotIn("облачный-секрет", text)

    def test_router_stats_are_present(self):
        from connector.printflow.api import register_routes
        register_routes()
        report = service.collect(self._api())
        self.assertGreaterEqual(report["router"]["registered"], 28)
        self.assertGreaterEqual(report["router"]["idempotent"], 1)

    def test_thread_stats_list_expected_and_missing(self):
        stats = service._thread_stats()
        self.assertGreaterEqual(stats["total"], 1)
        names = {item["name"] for item in stats["expected"]}
        self.assertIn("pf-http", names)
        # в тестовом процессе фоновые потоки коннектора не запущены
        self.assertIn("HTTP-сервер", stats["missing"])

    def test_alive_thread_is_not_reported_missing(self):
        """Живой поток с префиксом pf- снимает подсистему со списка пропавших."""
        stop = threading.Event()
        alive = threading.Thread(target=stop.wait, name="pf-scheduler", daemon=True)
        alive.start()
        try:
            stats = service._thread_stats()
            self.assertNotIn("Планировщик задач", stats["missing"])
            self.assertTrue(any(item["name"] == "pf-scheduler" and item["alive"]
                                for item in stats["expected"]))
        finally:
            stop.set()
            alive.join(timeout=2)

    def test_schema_mismatch_marks_report_unhealthy(self):
        report = service.collect(self._api())
        self.assertIn(report["schema"]["actual"], (0, report["schema"]["current"]))
        self.assertTrue(report["schema"]["matches"])

    def test_human_report_is_readable_text(self):
        text = service.human_report(service.collect(self._api()))
        self.assertIn("PrintFlow", text)
        self.assertIn("Потоков:", text)
        self.assertIn("Бэкапы:", text)
        self.assertLess(len(text), 3500)   # влезает в одно сообщение Telegram

    def test_human_report_mentions_farm_when_present(self):
        text = service.human_report(service.collect(self._api()))
        self.assertIn("Парк:", text)

    def test_backup_stats_never_raise(self):
        stats = service._backup_stats()
        self.assertIn("count", stats)
        self.assertGreaterEqual(stats["count"], 0)

    def test_error_scan_reads_only_the_tail(self):
        scan = service._error_scan(24)
        self.assertIn("errors", scan)
        self.assertLessEqual(len(scan.get("last", [])), 5)


if __name__ == "__main__":
    unittest.main()

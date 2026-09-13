"""Watch Folder: файл, который не попал в очередь, должен быть виден (17.0.18).

`manager.enqueue` бросает ValueError на файлах, которые печатать нельзя (логи,
таймлапс, ipcam). Раньше `_handle_file` глотал это в `except Exception: pass`,
а событие с `action=queue` уже ушло в ленту — оператор видел «файл принят»,
хотя печать не началась. Здесь проверяем, что причина возвращается и
регистрируется.
"""
from __future__ import annotations

import pathlib
import tempfile
import unittest

from connector.printflow.db import Database
from connector.printflow.watch_folder import WatchFolder

SOURCE = pathlib.Path(__file__).resolve().parents[2] / "connector" / "printflow" / "watch_folder.py"


class FakeManager:
    """Замена менеджера печати: запоминает payload и умеет отказать."""

    def __init__(self, error: str = "", result=None):
        self.error = error
        self.result = result if result is not None else {"id": "job_1"}
        self.calls: list[dict] = []

    def enqueue(self, payload: dict):
        self.calls.append(payload)
        if self.error:
            raise ValueError(self.error)
        return self.result


class WatchFolderEnqueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(pathlib.Path(self.tmp.name) / "watch.sqlite3")
        self.addCleanup(self.db.close)

    def watch(self, manager) -> WatchFolder:
        return WatchFolder(self.db, manager=manager, bus=None)

    def test_success_returns_true_and_sends_payload(self):
        manager = FakeManager()
        ok, reason = self.watch(manager)._enqueue("деталь.3mf", {}, "ord_1")
        self.assertTrue(ok)
        self.assertEqual("", reason)
        self.assertEqual(1, len(manager.calls))
        payload = manager.calls[0]
        self.assertEqual("деталь.3mf", payload["file"])
        self.assertEqual("деталь", payload["name"])
        self.assertEqual("ord_1", payload["order_id"])

    def test_manager_refusal_is_reported_not_swallowed(self):
        manager = FakeManager(error="Нельзя печатать логи, таймлапс и ipcam")
        ok, reason = self.watch(manager)._enqueue("ipcam.mp4.3mf", {}, "")
        self.assertFalse(ok)
        self.assertIn("Нельзя печатать", reason)

    def test_missing_manager_is_reported(self):
        ok, reason = self.watch(None)._enqueue("деталь.3mf", {}, "")
        self.assertFalse(ok)
        self.assertIn("менеджер", reason)

    def test_rejected_result_dict_is_reported(self):
        manager = FakeManager(result={"error": "принтер занят"})
        ok, reason = self.watch(manager)._enqueue("деталь.3mf", {}, "")
        self.assertFalse(ok)
        self.assertEqual("принтер занят", reason)


class WatchFolderCallerTests(unittest.TestCase):
    """Строковый контракт на вызывающий код: причину должна увидеть лента."""

    @classmethod
    def setUpClass(cls):
        cls.src = SOURCE.read_text(encoding="utf-8")

    def test_queue_result_is_used(self):
        self.assertIn('ok, reason = self._enqueue(path.name, info, order_id)', self.src)
        self.assertIn('info["queue_error"] = reason', self.src)
        self.assertIn('"Файл не попал в очередь"', self.src)

    def test_silent_swallow_is_gone(self):
        block = self.src.split('if action == "queue":', 1)[1].split("# оригинал", 1)[0]
        self.assertNotIn("except Exception:\n                pass", block,
                         "ошибка постановки в очередь снова глотается")


if __name__ == "__main__":
    unittest.main()

"""Тонкий бот — только уведомления + кнопка «Открыть цех» web_app.

Старые тесты shelf/queue/деньги ушли в Mini App, здесь проверяем что
бот отвечает меню с web_app и не падает на старых командах.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.staffbot import StaffBot  # noqa: E402


class FakeManager:
    def __init__(self, db, snapshot=None):
        self.db = db
        self.acc = Accounting(db)
        self.repo = None
        self.client_bot = None
        self._snapshot = snapshot or {"printers": []}
        self.notified = []

    def snapshot(self, printer_id: str = "") -> dict:
        return self._snapshot

    def queue(self):
        return []

    def notify_async(self, text, photo=None, buttons=None, critical=False, event=""):
        self.notified.append((text, buttons))


class TelegramThinTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111", "telegram_token": "tok",
                              "public_url": "https://example.com"})
        self.manager = FakeManager(self.db)
        self.bot = StaffBot(self.manager)

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _capture(self):
        calls = []
        self.bot._call = lambda method, params, timeout=35: calls.append((method, params)) or {"ok": True}
        return calls

    def test_menu_contains_web_app(self):
        calls = self._capture()
        self.bot._dispatch("111", "меню")
        self.assertTrue(calls)
        rm = json.loads(calls[-1][1]["reply_markup"])
        btn = rm["inline_keyboard"][0][0]
        self.assertIn("web_app", btn)
        self.assertIn("Открыть цех", btn["text"])

    def test_old_commands_still_show_menu(self):
        for cmd in ("продажа", "полка", "касса", "принтеры", "деньги", "очередь", "заказы"):
            calls = self._capture()
            self.bot._dispatch("111", cmd)
            self.assertTrue(calls, f"no reply for {cmd}")
            rm = json.loads(calls[-1][1]["reply_markup"])
            self.assertIn("web_app", rm["inline_keyboard"][0][0])

    def test_help_contains_ceh(self):
        calls = self._capture()
        self.bot._dispatch("111", "help")
        self.assertTrue(calls)
        text = calls[-1][1]["text"]
        self.assertIn("цех", text.lower())

    def test_code_shows_chat_id(self):
        calls = self._capture()
        self.bot._dispatch("111", "код")
        self.assertTrue(calls)
        text = calls[-1][1]["text"]
        self.assertIn("111", text)


if __name__ == "__main__":
    unittest.main()

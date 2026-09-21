"""Клиентский бот вне объёма staffbot — тонкий бот только меню."""
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
    def __init__(self, db):
        self.db = db
        self.acc = Accounting(db)
        self.repo = None
        self.client_bot = None
        self._snapshot = {"printers": []}

    def snapshot(self, printer_id: str = "") -> dict:
        return self._snapshot

    def queue(self):
        return []

    def notify_async(self, *a, **kw):
        pass


class ClientBotThinTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111", "telegram_token": "tok", "public_url": "https://example.com"})
        self.bot = StaffBot(FakeManager(self.db))

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def test_kotvet_now_in_miniapp(self):
        calls = []
        self.bot._call = lambda m, p, timeout=35: calls.append((m, p)) or {"ok": True}
        self.bot._dispatch("111", "кответ 555 привет")
        self.assertTrue(calls)
        rm = json.loads(calls[-1][1]["reply_markup"])
        self.assertIn("web_app", rm["inline_keyboard"][0][0])


if __name__ == "__main__":
    unittest.main()

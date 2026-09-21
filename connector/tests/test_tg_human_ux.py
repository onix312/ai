"""UX тонкого бота — подсказки и кнопка цеха.

Все непонятные сообщения ведут в Mini App.
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
from connector.printflow.staffbot.router import suggest_command  # noqa: E402


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


class SuggestCommandTests(unittest.TestCase):
    def test_suggest_always_menu(self):
        self.assertEqual(suggest_command("полк"), "меню")
        self.assertEqual(suggest_command("стеллаж"), "меню")
        self.assertEqual(suggest_command(""), "")

    def test_gibberish_no_suggestion(self):
        self.assertEqual(suggest_command("qwerty123"), "меню")  # тонкий бот всегда предлагает меню


class UnknownReplyTests(unittest.TestCase):
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

    def test_unknown_sends_menu_with_web_app(self):
        calls = []
        self.bot._call = lambda method, params, timeout=35: calls.append((method, params)) or {"ok": True}
        self.bot._dispatch("111", "абракадабра")
        self.assertTrue(calls)
        rm = json.loads(calls[-1][1]["reply_markup"])
        self.assertIn("web_app", rm["inline_keyboard"][0][0])

    def test_goto_callback_opens_menu(self):
        calls = []
        self.bot._call = lambda method, params, timeout=35: calls.append((method, params)) or {"ok": True}
        self.bot._handle_callback({
            "id": "cb1",
            "message": {"message_id": 1, "chat": {"id": "111"}},
            "data": "cmd:goto:деньги",
        }, "111")
        self.assertTrue(calls)


if __name__ == "__main__":
    unittest.main()

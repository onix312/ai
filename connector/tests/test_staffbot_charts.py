"""Уведомления бота — дайджест 09:00, график 20:00, низкий остаток, недельный.

Тонкий бот: только уведомления + кнопка web_app «Открыть цех».
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow.staffbot import StaffBot  # noqa: E402
from connector.printflow.staffbot.router import ROUTER  # noqa: E402


class FakeManager:
    def __init__(self, db):
        self.db = db
        self.notified = []
        self._queue = []

    def snapshot(self, printer_id: str = "") -> dict:
        return {"printers": []}

    def queue(self):
        return self._queue

    def notify_async(self, text, photo=None, buttons=None, critical=False, event=""):
        self.notified.append((text, photo, buttons, event))

    @property
    def acc(self):
        class Acc:
            def summary(self, days):
                return {"income": 1000, "profit": 200}
        return Acc()


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111", "public_url": "https://example.com",
                              "telegram_token": "tok"})
        self.manager = FakeManager(self.db)
        self.bot = StaffBot(self.manager)

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def test_digest_text_contains_ceh(self):
        text = self.bot.text_digest()
        self.assertIn("цех", text.lower())

    def test_weekly_text(self):
        text = self.bot.text_weekly()
        self.assertIn("Недельный", text)

    def test_maybe_digest_fires_at_time(self):
        now = datetime.now().strftime("%H:%M")
        self.bot._maybe_digest({"digest_time": now, "telegram_chat_id": "111"})
        self.assertEqual(len(self.manager.notified), 1)
        # второй раз в тот же день — молчит
        self.bot._maybe_digest({"digest_time": now, "telegram_chat_id": "111",
                                "digest_last": datetime.now().strftime("%Y-%m-%d")})
        self.assertEqual(len(self.manager.notified), 1)

    def test_maybe_shelf_low(self):
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS shelf_items (id TEXT PRIMARY KEY, name TEXT, qty REAL, min_qty REAL, active INT)"
        )
        self.db.execute("INSERT INTO shelf_items(id,name,qty,min_qty,active) VALUES('1','Тест',1,5,1)")
        self.bot._maybe_shelf_low({"telegram_chat_id": "111"})
        self.assertEqual(len(self.manager.notified), 1)
        self.assertIn("заканчивается", self.manager.notified[0][0])

    def test_maybe_weekly(self):
        now = datetime.now()
        self.bot._maybe_weekly({"weekly_report_day": now.isoweekday(),
                                "weekly_report_time": now.strftime("%H:%M"),
                                "telegram_chat_id": "111"})
        self.assertEqual(len(self.manager.notified), 1)

    def test_maybe_evening_chart(self):
        now = datetime.now().strftime("%H:%M")
        self.bot._maybe_evening_chart({"evening_chart_time": now, "telegram_chat_id": "111"})
        # должен отправить (хотя бы текст)
        self.assertEqual(len(self.manager.notified), 1)

    def test_notify_with_button_includes_open_ceh(self):
        self.bot._notify_with_button("тест", event="digest")
        self.assertEqual(len(self.manager.notified), 1)
        text, photo, buttons, event = self.manager.notified[0]
        # кнопка callback «Открыть цех» → menu
        self.assertTrue(buttons)
        # в тексте или кнопках должен быть цех
        flat = str(buttons)
        self.assertIn("Открыть цех", flat)


class ChartRoutingTests(unittest.TestCase):
    def test_menu_routes(self):
        # «график» — витрина Mini App, поэтому меню; «итоги» с 18.12.3
        # отвечает текстом денег (см. test_staffbot_report).
        for word in ("график", "меню", "цех"):
            route = ROUTER.match_text(word)
            self.assertIsNotNone(route, word)
            self.assertEqual(route.method, "cmd_menu", word)
        self.assertEqual(ROUTER.match_text("итоги").method, "cmd_money")


if __name__ == "__main__":
    unittest.main()

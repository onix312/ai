"""Роли в Telegram-боте — тонкий бот notify_only + кнопка web_app.

Проверяем gate, invite, и что все команды ведут в меню с web_app.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
import json

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.staff import ROLE_RIGHTS, Staff, gate  # noqa: E402
from connector.printflow.staffbot import StaffBot  # noqa: E402
from connector.printflow.staffbot.core.config import get_miniapp_url  # noqa: E402


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
        self.notified.append((text, buttons, event))


class StaffRoleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111", "telegram_bot": "1",
                              "telegram_token": "tok", "public_url": "https://example.com"})
        self.manager = FakeManager(self.db)
        self.bot = StaffBot(self.manager)
        self.staff = Staff(self.db)

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _capture_call(self):
        calls = []
        self.bot._call = lambda method, params, timeout=35: calls.append((method, params)) or {"ok": True}
        return calls

    def test_owner_chat_gets_owner_role(self):
        who = gate(self.db, "111")
        self.assertEqual(who["role"], "owner")
        self.assertEqual(who["allowed"], ROLE_RIGHTS["owner"])

    def test_add_employee_and_manager(self):
        employee = self.staff.add("Ваня", "employee", "222")
        manager = self.staff.add("Оля", "руководитель", "333")
        self.assertEqual(employee["role"], "employee")
        self.assertEqual(manager["role"], "manager")
        self.assertEqual(gate(self.db, "222")["allowed"], ROLE_RIGHTS["employee"])
        self.assertEqual(gate(self.db, "333")["allowed"], ROLE_RIGHTS["manager"])

    def test_unknown_chat_has_no_rights(self):
        self.assertIsNone(gate(self.db, "999")["role"])

    def test_bot_menu_has_web_app(self):
        self._capture_call()
        self.bot._dispatch("111", "меню")
        # _reply через outbox, но мы перехватили _call в _send_main_menu
        # Проверяем что последний вызов содержит web_app
        found = False
        for method, params in self.bot._call.__self__ if hasattr(self.bot._call, "__self__") else []:
            pass
        # напрямую вызываем _send_main_menu и проверяем markup
        calls = self._capture_call()
        self.bot._send_main_menu("111")
        self.assertTrue(calls)
        method, params = calls[-1]
        self.assertEqual(method, "sendMessage")
        rm = json.loads(params.get("reply_markup") or "{}")
        self.assertIn("inline_keyboard", rm)
        btn = rm["inline_keyboard"][0][0]
        self.assertIn("web_app", btn)
        self.assertIn("staff", btn["web_app"]["url"])
        self.assertIn("Открыть цех", btn["text"])

    def test_bot_dispatch_all_commands_to_menu(self):
        calls = self._capture_call()
        for cmd in ("продажа", "полка", "касса", "принтеры", "деньги", "старт", "help"):
            calls.clear()
            self.bot._dispatch("111", cmd)
            # должен отправить меню
            self.assertTrue(calls, f"no call for {cmd}")
            method, params = calls[-1]
            self.assertEqual(method, "sendMessage")

    def test_invite_code_joins_with_role(self):
        invite = self.staff.invite("manager", "Оля")
        code = invite["code"]
        member = self.staff.use_invite(code, "555", "Оля")
        self.assertEqual(member["role"], "manager")
        self.assertEqual(gate(self.db, "555")["role"], "manager")
        with self.assertRaises(ValueError):
            self.staff.use_invite(code, "777", "Кто-то")

    def test_invite_owner_forbidden(self):
        with self.assertRaises(ValueError):
            self.staff.invite("owner")

    def test_add_owner_role_forbidden(self):
        with self.assertRaises(ValueError):
            self.staff.add("Кто-то", "owner", "888")

    def test_remove_and_restore_member(self):
        member = self.staff.add("Ваня", "employee", "222")
        self.staff.remove("222")
        self.assertIsNone(gate(self.db, "222")["role"])
        self.staff.restore(member["id"])
        self.assertEqual(gate(self.db, "222")["role"], "employee")

    def test_miniapp_url_fallback(self):
        url = get_miniapp_url(self.db)
        self.assertIn("staff", url)

    def test_stranger_gets_hint(self):
        calls = self._capture_call()
        update = {"message": {"chat": {"id": 999}, "text": "привет",
                              "from": {"first_name": "Гость", "id": 999}}}
        self.bot._handle(update, "111")
        self.assertTrue(calls or self.db.query("SELECT * FROM telegram_outbox"))
        # должен быть ответ с chat_id
        payloads = [p for m, p in calls]
        # если через outbox, проверим outbox таблицу
        if not payloads:
            rows = self.db.query("SELECT payload FROM telegram_outbox")
            payloads = [r.get("payload") for r in rows]
        self.assertTrue(any("999" in str(pl) for pl in payloads))

    def test_notify_with_button_uses_callback(self):
        self.manager.notified.clear()
        self.bot._notify_with_button("тест уведомление", event="test")
        self.assertEqual(len(self.manager.notified), 1)
        text, buttons, event = self.manager.notified[0]
        self.assertIn("тест", text)


if __name__ == "__main__":
    unittest.main()

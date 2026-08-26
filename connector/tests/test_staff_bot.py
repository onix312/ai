"""Роли в Telegram-боте: владелец, руководитель, сотрудник, приглашения.

Без сети: проверяется разграничение команд и работа с таблицей staff.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.staff import ROLE_RIGHTS, Staff, gate, group_for_word  # noqa: E402
from connector.printflow.telegram_bot import TelegramBot  # noqa: E402


class FakeManager:
    def __init__(self, db, snapshot=None):
        self.db = db
        self.acc = Accounting(db)
        self.repo = None
        self.client_bot = None
        self._snapshot = snapshot or {"printers": []}

    def snapshot(self, printer_id: str = "") -> dict:
        return self._snapshot


class StaffRoleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111"})
        self.manager = FakeManager(self.db)
        self.bot = TelegramBot(self.manager)
        self.staff = Staff(self.db)

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

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

    def test_inline_callback_edits_card_without_new_message(self):
        """Inline-карточки сотрудника не плодят сообщения в чате."""
        calls = []
        self.bot._call = lambda method, params, timeout=35: calls.append((method, params)) or {"ok": True}
        self.bot._edit_or_reply("111", {"message_id": 7}, "обновлено")
        self.assertEqual([method for method, _ in calls], ["editMessageText"])

    def test_bot_denies_finance_for_employee(self):
        self.staff.add("Ваня", "employee", "222")
        replies: list[str] = []
        self.bot._reply = lambda chat, text: replies.append(text)
        self.bot._dispatch("222", "деньги")
        self.assertTrue(any("недоступен" in r for r in replies), replies)

    def test_bot_allows_shelf_for_employee(self):
        self.staff.add("Ваня", "employee", "222")
        replies: list[str] = []
        self.bot._reply = lambda chat, text: replies.append(text)
        self.bot.sell_keyboard = lambda chat: replies.append("клавиатура продажи")
        self.bot._dispatch("222", "продажа")
        self.assertIn("клавиатура продажи", replies)

    def test_bot_denies_printer_control_for_employee(self):
        self.staff.add("Ваня", "employee", "222")
        replies: list[str] = []
        self.bot._reply = lambda chat, text: replies.append(text)
        self.bot._dispatch("222", "пауза")
        self.assertTrue(any("недоступен" in r for r in replies), replies)

    def test_manager_can_read_finance(self):
        self.staff.add("Оля", "manager", "333")
        replies: list[str] = []
        self.bot._reply = lambda chat, text: replies.append(text)
        self.bot.text_money = lambda: "касса пуста"
        self.bot._dispatch("333", "деньги")
        self.assertIn("касса пуста", replies)

    def test_only_owner_adds_staff_from_chat(self):
        self.staff.add("Оля", "manager", "333")
        self.bot._reply = lambda chat, text: None
        answer = self.bot._staff_command("333", "add", "сотрудник Петя 444",
                                         role="сотрудник")
        self.assertIn("только владелец", answer)
        answer = self.bot._staff_command("111", "add", "сотрудник Петя 444",
                                         role="сотрудник")
        self.assertIn("Петя", answer)
        self.assertEqual(gate(self.db, "444")["role"], "employee")

    def test_invite_code_joins_with_role(self):
        invite = self.staff.invite("manager", "Оля")
        code = invite["code"]
        member = self.staff.use_invite(code, "555", "Оля")
        self.assertEqual(member["role"], "manager")
        self.assertEqual(gate(self.db, "555")["role"], "manager")
        # код одноразовый
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

    def test_group_mapping(self):
        self.assertEqual(group_for_word("деньги"), "finance")
        self.assertEqual(group_for_word("статус"), "view")
        self.assertEqual(group_for_word("статус", text_has_digits=True), "orders")
        self.assertEqual(group_for_word("", command="pause"), "printers")
        self.assertEqual(group_for_word("", command="sell:nom1"), "shelf")
        self.assertEqual(group_for_word("", command="shelf-sell:it1"), "shelf")
        self.assertEqual(group_for_word("стеллаж"), "view")

    def test_stranger_gets_chat_id_hint_and_invite_join(self):
        replies: list[str] = []
        self.bot._reply = lambda chat, text: replies.append(text)
        update = {"message": {"chat": {"id": 999}, "text": "код",
                              "from": {"first_name": "Гость", "id": 999}}}
        self.bot._handle(update, "111")
        self.assertTrue(any("999" in r for r in replies), replies)

        invite = self.staff.invite("employee", "Гость")
        update_join = {"message": {"chat": {"id": 999},
                                   "text": f"старт {invite['code']}",
                                   "from": {"first_name": "Гость", "id": 999}}}
        self.bot._handle(update_join, "111")
        self.assertEqual(gate(self.db, "999")["role"], "employee")


if __name__ == "__main__":
    unittest.main()

"""Клиентский бот + СБП (Касса 16.0): платёж заказа через ядро СБП.

Проверяем: «Я оплатил» создаёт заявку и СБП-платёж (pending) без движения денег;
подтверждение в панели идёт через ядро СБП (счёт «СБП») и закрывает долг;
отклонение гасит и СБП-платёж; карточка заказа показывает состояние оплаты.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.manager import PrinterManager  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402
from connector.printflow.sbp import Sbp  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "cbot_sbp.sqlite3")


class ClientBotSbpTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.manager = PrinterManager(self.db, Repo(self.db))
        self.bot = self.manager.client_bot
        self.acc = Accounting(self.db)
        self.db.set_settings({"sbp_enabled": True})
        # заказ, привязанный к чату
        self.db.upsert("orders", {
            "id": "o1", "number": "1001", "product": "Адресник",
            "customer_name": "Иван", "price": 1000, "paid": 0, "status": "done",
            "channel": "telegram", "created_at": "2026-09-06T10:00:00+03:00",
            "updated_at": "2026-09-06T10:00:00+03:00"})
        self.db.upsert("client_chats", {
            "chat_id": "555", "name": "Иван", "username": "ivan",
            "created_at": "2026-09-06T10:00:00", "last_seen": "2026-09-06T10:00:00"},
            key="chat_id")
        self.db.execute(
            "INSERT OR IGNORE INTO client_orders(chat_id,order_id,number,created_at)"
            " VALUES('555','o1','1001','2026-09-06T10:00:00')")
        self.row = self.db.one("SELECT * FROM client_chats WHERE chat_id='555'")

    def tearDown(self):
        self.manager.shutdown()
        self.db.close()

    def _intent(self) -> dict:
        return self.db.one("SELECT * FROM client_payment_intents WHERE order_id='o1'")

    def test_paid_notice_creates_intent_and_sbp_payment_without_money(self):
        self.bot._paid_notice("555", self.row, "o1")
        intent = self._intent()
        self.assertIsNotNone(intent)
        self.assertEqual(intent["status"], "pending")
        self.assertTrue(intent["sbp_id"])
        sbp = self.db.one("SELECT * FROM sbp_payments WHERE id=?", (intent["sbp_id"],))
        self.assertIsNotNone(sbp)
        self.assertIn(sbp["status"], ("new", "pending"))
        self.assertEqual(sbp["amount"], 1000)
        # деньги и долг не тронуты
        self.assertEqual(self.db.one("SELECT paid FROM orders WHERE id='o1'")["paid"], 0)
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))

    def test_paid_notice_is_idempotent(self):
        self.bot._paid_notice("555", self.row, "o1")
        second = self.bot._paid_notice("555", self.row, "o1")
        self.assertIn("уже передано", second[0])
        self.assertEqual(
            self.db.one("SELECT COUNT(*) n FROM client_payment_intents WHERE order_id='o1'")["n"],
            1)
        self.assertEqual(
            self.db.one("SELECT COUNT(*) n FROM sbp_payments WHERE order_id='o1'")["n"], 1)

    def test_confirm_through_panel_closes_debt_on_sbp_account(self):
        from connector.printflow.api import Api
        self.bot._paid_notice("555", self.row, "o1")
        intent = self._intent()
        api = Api.__new__(Api)
        api.db = self.db
        api.acc = self.acc
        api.repo = Repo(self.db)
        api.sbp = Sbp(self.db, self.acc)
        api.manager = SimpleNamespace(client_bot=self.bot)
        api.bus = SimpleNamespace(publish=lambda *a, **k: None)
        api.started_at = time.time()
        api.last_host = "test"
        code, payload = api.post(
            "/api/client-bot/payment",
            {"intent_id": intent["id"], "action": "confirm", "actor": "owner"}, {})
        self.assertEqual(code, 200)
        self.assertEqual(payload["intent"]["status"], "confirmed")
        self.assertEqual(self.db.one("SELECT paid FROM orders WHERE id='o1'")["paid"], 1000)
        tx = self.db.one("SELECT * FROM transactions WHERE kind='income'")
        self.assertEqual(tx["account_id"], "sbp")
        self.assertEqual(tx["amount"], 1000)
        # повторное подтверждение идемпотентно
        code, payload = api.post(
            "/api/client-bot/payment",
            {"intent_id": intent["id"], "action": "confirm", "actor": "owner"}, {})
        self.assertEqual(code, 200)
        self.assertTrue(payload.get("already_recorded"))
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM transactions")["n"], 1)

    def test_reject_gates_the_sbp_payment(self):
        from connector.printflow.api import Api
        self.bot._paid_notice("555", self.row, "o1")
        intent = self._intent()
        api = Api.__new__(Api)
        api.db = self.db
        api.acc = self.acc
        api.repo = Repo(self.db)
        api.sbp = Sbp(self.db, self.acc)
        api.manager = SimpleNamespace(client_bot=self.bot)
        api.bus = SimpleNamespace(publish=lambda *a, **k: None)
        api.started_at = time.time()
        api.last_host = "test"
        code, payload = api.post(
            "/api/client-bot/payment",
            {"intent_id": intent["id"], "action": "reject", "reason": "не пришло",
             "actor": "owner"}, {})
        self.assertEqual(code, 200)
        self.assertEqual(payload["intent"]["status"], "rejected")
        sbp = self.db.one("SELECT * FROM sbp_payments WHERE id=?", (intent["sbp_id"],))
        self.assertEqual(sbp["status"], "rejected")
        self.assertEqual(self.db.one("SELECT paid FROM orders WHERE id='o1'")["paid"], 0)
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))

    def test_payment_line_shows_pending_then_paid(self):
        line_before = self.bot._payment_line("555", self.db.one("SELECT * FROM orders WHERE id='o1'"))
        self.assertEqual(line_before, "")
        self.bot._paid_notice("555", self.row, "o1")
        order = self.db.one("SELECT * FROM orders WHERE id='o1'")
        self.assertEqual(self.bot._payment_line("555", order), "\n💳 Оплата: ожидает подтверждения")
        self.db.execute("UPDATE orders SET paid=1000, updated_at=? WHERE id='o1'",
                        ("2026-09-06T11:00:00+03:00",))
        order = self.db.one("SELECT * FROM orders WHERE id='o1'")
        self.assertEqual(self.bot._payment_line("555", order), "\n✅ Оплачено")


if __name__ == "__main__":
    unittest.main()

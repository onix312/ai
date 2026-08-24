"""Долги: предпросмотр напоминания, подтверждение отправки и оплата без дублей."""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting, num
from connector.printflow.api import Api
from connector.printflow.config import now_iso
from connector.printflow.db import Database
from connector.printflow.receivables import Receivables
from connector.printflow.repo import Repo


class ReceivablesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "receivables.sqlite3")
        self.repo = Repo(self.db)
        self.acc = Accounting(self.db)
        self.service = Receivables(self.db, self.repo, self.acc)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def order(self, **overrides):
        data = {
            "id": "order-1", "number": "1001", "product": "Адресник",
            "customer_name": "Мария", "phone": "+7", "messenger": "@maria",
            "status": "done", "price": 1000, "paid": 300,
            "created_at": "2026-07-01T10:00:00+03:00",
            "closed_at": now_iso(), "updated_at": now_iso(),
        }
        data.update(overrides)
        return self.db.upsert("orders", data)

    def test_preview_does_not_claim_message_was_sent(self):
        self.order()
        first = self.service.summary("order-1")
        second = self.service.summary("order-1")
        self.assertEqual(first["debt"], 700)
        self.assertIn("700 ₽", first["message"])
        self.assertFalse(first["external_sent_by_printflow"])
        self.assertEqual(second["last_reminded_at"], "")
        self.assertEqual(self.db.one("SELECT reminded_at FROM orders WHERE id='order-1'")["reminded_at"], "")

    def test_reminder_requires_confirmation_and_respects_cooldown(self):
        self.order()
        with self.assertRaisesRegex(ValueError, "действительно отправлено"):
            self.service.mark_reminded("order-1")
        first = self.service.mark_reminded("order-1", sent_confirmed=True)
        self.assertTrue(first["last_reminded_at"])
        self.assertGreater(first["cooldown_left_days"], 0)
        with self.assertRaisesRegex(ValueError, "недавно"):
            self.service.mark_reminded("order-1", sent_confirmed=True)
        forced = self.service.mark_reminded("order-1", sent_confirmed=True, force=True)
        self.assertTrue(forced["marked_sent"])

    def test_partial_payment_retry_is_idempotent(self):
        self.order()
        first = self.service.settle(
            "order-1", payment_confirmed=True, amount=200,
            payment_method="transfer", request_id="browser-request-1",
        )
        second = self.service.settle(
            "order-1", payment_confirmed=True, amount=200,
            payment_method="transfer", request_id="browser-request-1",
        )
        self.assertFalse(first["already_recorded"])
        self.assertTrue(second["already_recorded"])
        self.assertEqual(first["debt"], 500)
        self.assertEqual(second["debt"], 500)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM payments")["n"], 1)
        self.assertEqual(num(self.db.one("SELECT paid FROM orders WHERE id='order-1'")["paid"]), 500)

    def test_overpayment_is_rejected_without_partial_writes(self):
        self.order()
        with self.assertRaisesRegex(ValueError, "больше долга"):
            self.service.settle(
                "order-1", payment_confirmed=True, amount=701,
                payment_method="cash", request_id="too-much",
            )
        with self.assertRaisesRegex(ValueError, "больше остатка"):
            self.acc.add_payment("order-1", 701, "payment", method="cash")
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM payments")["n"], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM transactions")["n"], 0)

    def test_fully_paid_order_is_a_safe_noop(self):
        self.order(paid=1000)
        result = self.service.settle(
            "order-1", payment_confirmed=True, amount=100,
            payment_method="cash", request_id="already-paid",
        )
        self.assertTrue(result["settled"])
        self.assertTrue(result["already_recorded"])
        self.assertEqual(result["received"], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM payments")["n"], 0)
        with self.assertRaisesRegex(ValueError, "полностью"):
            self.service.mark_reminded("order-1", sent_confirmed=True)

    def test_debt_age_starts_at_handoff_not_order_creation(self):
        self.order(created_at="2025-01-01T00:00:00+00:00", closed_at=now_iso())
        row = next(item for item in self.acc.debts()["rows"] if item["id"] == "order-1")
        self.assertEqual(row["days"], 0)
        self.assertFalse(row["overdue"])

    def test_report_cash_flow_nets_platform_fees(self):
        """В денежный поток доход входит за вычетом комиссии эквайринга.

        accounts_state считает amount-fee, а отчёт раньше показывал gross,
        из-за чего «в кассе» и «денежный поток» расходились на размер комиссии.
        """
        self.acc.add_transaction("income", "sale", 1000, "Продажа онлайн",
                                 fee=50)
        self.acc.add_transaction("expense", "other", 200, "Материал")
        rep = self.acc.report("month")
        self.assertEqual(rep["cash_in"], 950)
        self.assertEqual(rep["cash_out"], 200)
        self.assertEqual(rep["cash_flow"], 750)

    def test_payment_after_legacy_prepaid_clears_debt(self):
        """Старое поле prepaid не должно «замораживать» остаток долга.

        Если в старой базе долг хранился в prepaid, а сейчас клиент доплачивает,
        новый платёж увеличивает paid, и экономика не должна смотреть только
        на prepaid: после доплаты долг исчезает полностью.
        """
        self.order(paid=0, prepaid=500)
        self.assertEqual(self.acc.order_economics(
            self.db.one("SELECT * FROM orders WHERE id='order-1'"))["debt"], 500)
        recorded = self.acc.add_payment(
            "order-1", 500, "payment", request_id="legacy-prepay-1")
        self.assertFalse(recorded["already_recorded"])
        row = self.db.one("SELECT paid,prepaid FROM orders WHERE id='order-1'")
        self.assertEqual(row["paid"], 1000)
        self.assertEqual(row["prepaid"], 0)
        eco = self.acc.order_economics(
            self.db.one("SELECT * FROM orders WHERE id='order-1'"))
        self.assertEqual(eco["debt"], 0)
        self.assertEqual(self.acc.debts()["rows"], [])


class ReceivablesApiRouteTests(unittest.TestCase):
    def setUp(self):
        self.api = Api.__new__(Api)
        self.api.receivables = mock.Mock()

    def test_summary_route_passes_order_id(self):
        self.api.receivables.summary.return_value = {"debt": 700}
        code, payload = self.api.get("/api/debt/summary", {"id": ["order-1"]})
        self.assertEqual((code, payload), (200, {"debt": 700}))
        self.api.receivables.summary.assert_called_once_with("order-1")

    def test_remind_route_only_builds_preview(self):
        self.api.receivables.summary.return_value = {"message": "Текст", "debt": 700}
        code, payload = self.api.post("/api/debt/remind", {"id": "order-1"}, {})
        self.assertEqual(code, 200)
        self.assertEqual(payload["text"], "Текст")
        self.api.receivables.summary.assert_called_once_with("order-1")
        self.api.receivables.mark_reminded.assert_not_called()

    def test_reminder_confirmation_route_requires_literal_true(self):
        self.api.receivables.mark_reminded.return_value = {"marked_sent": True}
        code, payload = self.api.post(
            "/api/debt/remind/confirm",
            {"order_id": "order-1", "sent_confirmed": "true", "force": True}, {},
        )
        self.assertEqual((code, payload), (200, {"marked_sent": True}))
        self.api.receivables.mark_reminded.assert_called_once_with(
            "order-1", sent_confirmed=False, force=True,
        )

    def test_settle_route_passes_confirmation_and_idempotency_key(self):
        self.api.receivables.settle.return_value = {"settled": True}
        code, payload = self.api.post(
            "/api/debt/settle",
            {
                "id": "order-1", "payment_confirmed": True, "amount": "700",
                "account_id": "cash", "payment_method": "cash",
                "request_id": "request-1",
            }, {},
        )
        self.assertEqual((code, payload), (200, {"settled": True}))
        self.api.receivables.settle.assert_called_once_with(
            "order-1", payment_confirmed=True, amount=700.0,
            account_id="cash", payment_method="cash", request_id="request-1",
        )


class PaymentRequestMigrationTests(unittest.TestCase):
    def test_old_payment_table_gets_request_id_and_unique_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "old.sqlite3"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE payments (id TEXT PRIMARY KEY,at TEXT,order_id TEXT,"
                "customer_id TEXT,amount REAL DEFAULT 0,kind TEXT DEFAULT 'payment',"
                "account_id TEXT,method TEXT DEFAULT '',fee REAL DEFAULT 0,"
                "note TEXT DEFAULT '',tx_id TEXT)"
            )
            conn.commit()
            conn.close()
            db = Database(path)
            try:
                self.assertIn("request_id", db.columns("payments"))
                indexes = {row["name"] for row in db.query("PRAGMA index_list(payments)")}
                self.assertIn("idx_pay_request", indexes)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()

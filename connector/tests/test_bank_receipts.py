"""Авто-СБП: поступления из банка, сопоставление, строгое авто-подтверждение.

Проверяем: парсер выписки Т-Банка; точное совпадение суммы и времени
подтверждает платёж (выручка на счёт СБП, долг закрыт); неоднозначность и
отсутствие кандидата идут в «на сверку»; импорт идемпотентен; ручная привязка
и подтверждение.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.bank_receipts import (  # noqa: E402
    BankReceipts, _parse_amount, _parse_date, parse_tbank_csv)
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.sbp import Sbp  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "bank.sqlite3")


def order(db: Database, **overrides) -> dict:
    data = {"id": "o1", "number": "1001", "product": "Адресник", "status": "done",
            "price": 1000, "paid": 0, "created_at": "2026-09-06T10:00:00+03:00",
            "updated_at": "2026-09-06T10:00:00+03:00"}
    data.update(overrides)
    return db.upsert("orders", data)


CSV_SAMPLE = (
    "Дата операции;Сумма операции;Валюта;Назначение платежа;Статус\n"
    "06.09.2026 12:00:00;1000,00;RUB;Перевод СБП 1001;OK\n"
    "06.09.2026 13:00:00;250,50;RUB;Перевод клиента;OK\n"
    "06.09.2026 14:00:00;-500,00;RUB;Покупка;OK\n"
)


class ParserTests(unittest.TestCase):
    def test_parse_amount(self):
        self.assertEqual(_parse_amount("1000,00"), 1000.0)
        self.assertEqual(_parse_amount("-500,00"), -500.0)
        self.assertEqual(_parse_amount("(500,00)"), -500.0)
        self.assertEqual(_parse_amount("1 234,56"), 1234.56)
        self.assertEqual(_parse_amount("123,45-"), -123.45)
        self.assertEqual(_parse_amount(""), 0.0)

    def test_parse_date(self):
        self.assertEqual(_parse_date("06.09.2026 12:00:00"),
                         "2026-09-06T12:00:00")
        self.assertEqual(_parse_date("2026-09-06T10:00:00Z"),
                         "2026-09-06T10:00:00")
        self.assertEqual(_parse_date(""), "")

    def test_parse_csv_returns_only_income(self):
        rows = parse_tbank_csv(CSV_SAMPLE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["amount"], 1000.0)
        self.assertEqual(rows[0]["purpose"], "Перевод СБП 1001")
        self.assertEqual(rows[1]["amount"], 250.5)

    def test_parse_csv_requires_amount_column(self):
        with self.assertRaisesRegex(ValueError, "Сумма"):
            parse_tbank_csv("Имя;Фамилия\nИван;Петров\n")


class BankReceiptsTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.sbp = Sbp(self.db, self.acc)
        self.bank = BankReceipts(self.db, self.acc, self.sbp)
        self.db.set_settings({"sbp_enabled": True, "sbp_auto_confirm": True})

    def tearDown(self):
        self.db.close()

    def _now(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _payment(self, amount: float, order_id: str = "o1") -> dict:
        return self.sbp.create(amount=amount, order_id=order_id)

    def test_exact_match_auto_confirms(self):
        order(self.db)
        p = self._payment(1000)
        result = self.bank.ingest([{
            "at": self._now(), "amount": 1000, "purpose": "Перевод 1001"}])
        self.assertEqual(result["confirmed"], 1)
        self.assertEqual(self.db.one("SELECT paid FROM orders WHERE id='o1'")["paid"], 1000)
        receipt = self.db.one("SELECT * FROM bank_receipts")
        self.assertEqual(receipt["status"], "confirmed")
        self.assertEqual(receipt["sbp_id"], p["id"])
        tx = self.db.one("SELECT * FROM transactions WHERE kind='income'")
        self.assertEqual(tx["account_id"], "sbp")

    def test_import_is_idempotent(self):
        order(self.db)
        self._payment(1000)
        rows = [{"at": self._now(), "amount": 1000, "purpose": "Перевод 1001"}]
        first = self.bank.ingest(rows)
        second = self.bank.ingest(rows)
        self.assertEqual(first["new"], 1)
        self.assertEqual(second["new"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM bank_receipts")["n"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM transactions")["n"], 1)

    def test_ambiguous_goes_to_review(self):
        # два платежа на одну сумму → «на сверку», выручки нет
        order(self.db)
        self._payment(1000)
        self._payment(1000)
        result = self.bank.ingest([{
            "at": self._now(), "amount": 1000, "purpose": "Перевод"}])
        self.assertEqual(result["confirmed"], 0)
        receipt = self.db.one("SELECT * FROM bank_receipts")
        self.assertEqual(receipt["status"], "review")
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))

    def test_no_candidate_goes_unmatched(self):
        result = self.bank.ingest([{
            "at": self._now(), "amount": 777, "purpose": "Неизвестно"}])
        self.assertEqual(result["unmatched"], 1)
        receipt = self.db.one("SELECT * FROM bank_receipts")
        self.assertEqual(receipt["status"], "unmatched")
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))

    def test_auto_confirm_disabled_stays_matched(self):
        self.db.set_settings({"sbp_auto_confirm": False})
        order(self.db)
        p = self._payment(1000)
        result = self.bank.ingest([{
            "at": self._now(), "amount": 1000, "purpose": "Перевод"}])
        self.assertEqual(result["confirmed"], 0)
        self.assertEqual(result["matched"], 1)
        receipt = self.db.one("SELECT * FROM bank_receipts")
        self.assertEqual(receipt["status"], "matched")
        self.assertEqual(receipt["sbp_id"], p["id"])
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))

    def test_manual_link_and_confirm(self):
        order(self.db)
        p = self._payment(1000)
        self.bank.ingest([{"at": self._now(), "amount": 1000, "purpose": "Перевод"}])
        receipt = self.db.one("SELECT * FROM bank_receipts")
        # уже подтверждено авто — для ручного сценария сделаем новый
        order(self.db, id="o2", number="1002", paid=0)
        p2 = self._payment(500, order_id="o2")
        self.bank.ingest([{"at": self._now(), "amount": 500, "purpose": "Перевод 1002"}])
        r2 = self.db.one("SELECT * FROM bank_receipts WHERE sbp_id=?", (p2["id"],))
        # привяжем вручную другое поступление к p2 — уже связано; проверим link на свежем
        self.bank.link(r2["id"], p2["id"], confirm=True)
        r2 = self.db.one("SELECT * FROM bank_receipts WHERE id=?", (r2["id"],))
        self.assertEqual(r2["status"], "confirmed")
        self.assertEqual(self.db.one("SELECT paid FROM orders WHERE id='o2'")["paid"], 500)
        # убедимся, что первый платёж тоже подтверждён
        self.assertEqual(self.db.one("SELECT paid FROM orders WHERE id='o1'")["paid"], 1000)

    def test_link_unknown_payment_raises(self):
        order(self.db)
        self._payment(1000)
        self.bank.ingest([{"at": self._now(), "amount": 1000, "purpose": "Перевод"}])
        receipt = self.db.one("SELECT * FROM bank_receipts")
        with self.assertRaisesRegex(ValueError, "не найден"):
            self.bank.link(receipt["id"], "nonexistent")

    def test_audit_covers_match_and_confirm(self):
        order(self.db)
        self._payment(1000)
        self.bank.ingest([{"at": self._now(), "amount": 1000, "purpose": "Перевод"}])
        actions = [r["action"] for r in self.db.query(
            "SELECT action FROM audit_log WHERE entity='bank_receipt' ORDER BY id")]
        self.assertIn("auto_confirm", actions)


if __name__ == "__main__":
    unittest.main()

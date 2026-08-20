"""Telegram-команды: отчёты по запросу, «спроси принтер», закрытие месяца.

Проверяется только текст ответов — без сети: бот получает базу и
подставной менеджер со снимком парка.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.manager import PrinterManager  # noqa: E402
from connector.printflow.telegram_bot import TelegramBot  # noqa: E402


class FakeManager:
    """Менеджер-заглушка: база, учёт и снимок парка из переданных данных."""

    def __init__(self, db, snapshot=None):
        self.db = db
        self.acc = Accounting(db)
        self._snapshot = snapshot or {"printers": []}

    def snapshot(self, printer_id: str = "") -> dict:
        return self._snapshot


class TelegramTextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.acc = Accounting(self.db)
        self.manager = FakeManager(self.db)
        self.bot = TelegramBot(self.manager)

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _order(self, number: str, price: float, paid: float,
               product: str = "адресник", status: str = "done"):
        self.db.upsert("orders", {
            "id": f"o{number}", "number": number, "customer_name": "Мария",
            "phone": "+7", "product": product, "price": price, "prepaid": paid,
            "status": status, "created_at": "2026-08-10T10:00:00",
            "updated_at": "2026-08-10T10:00:00"})

    def test_debts_lists_unpaid_orders(self):
        self._order("1001", 1000, 0)
        self._order("1002", 500, 500)
        text = self.bot.text_debts()
        self.assertIn("1001", text)
        self.assertNotIn("1002", text)
        self.assertIn("1 000 ₽", text)

    def test_defects_counts_failed_jobs(self):
        self.db.upsert("print_jobs", {
            "id": "j1", "name": "адресник", "state": "failed", "result": "error",
            "grams": 100, "duration_min": 60, "finished_at": "2026-08-10T10:00:00",
            "queued_at": "2026-08-10T09:00:00", "printer_id": ""})
        text = self.bot.text_defects(30)
        self.assertIn("Брак за 30 дней: 1", text)
        self.assertIn("100 г", text)

    def test_rating_uses_orders(self):
        self._order("1003", 900, 900, product="номерок")
        text = self.bot.text_rating()
        self.assertIn("номерок", text)

    def test_ask_without_printer(self):
        self.assertEqual(self.bot.text_ask("сколько осталось"),
                         "Сейчас ничего не печатается.")

    def test_ask_during_print(self):
        self.manager._snapshot = {"printers": [{
            "name": "P1S", "connection": {"connected": True},
            "printer": {"state": "RUNNING", "progress": 60, "layer": 30,
                        "total_layers": 50, "remaining_min": 40,
                        "eta": "2026-08-20T14:30:00", "task": "адресник"},
            "job": {"order": {"number": "1004"}}}]}
        self.assertIn("60%", self.bot.text_ask("сколько осталось"))
        self.assertIn("14:30", self.bot.text_ask("когда закончит"))
        self.assertIn("адресник", self.bot.text_ask("что печатает"))

    def test_month_close_command_text(self):
        text = self.bot._month_close("закрыть месяц")
        self.assertIn("Закрыть месяц", text)
        self.assertIn("Постоянные расходы", text)
        # шаг fixed выполняется и не задваивается
        done = self.bot._month_close("закрыть месяц fixed")
        self.assertIn("✅", done)
        again = self.bot._month_close("закрыть месяц fixed")
        self.assertIn("уже выполнен", again)


class TelegramQuietHoursTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.fake = SimpleNamespace(db=self.db)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_disabled_when_bounds_empty(self):
        self.db.set_settings({"telegram_quiet_from": "", "telegram_quiet_to": ""})
        self.assertFalse(PrinterManager.tg_quiet_now(self.fake))

    def test_interval_through_midnight(self):
        self.db.set_settings({"telegram_quiet_from": "23:00",
                              "telegram_quiet_to": "07:00"})
        import time as _time
        from unittest import mock as _mock
        for stamp, expected in (("23:30", True), ("03:00", True),
                                ("12:00", False), ("06:59", True)):
            with _mock.patch.object(_time, "strftime",
                                    return_value=stamp):
                self.assertEqual(PrinterManager.tg_quiet_now(self.fake), expected,
                                 f"в {stamp} тихие часы должны быть {expected}")

    def test_day_interval(self):
        self.db.set_settings({"telegram_quiet_from": "12:00",
                              "telegram_quiet_to": "13:00"})
        import time as _time
        from unittest import mock as _mock
        with _mock.patch.object(_time, "strftime", return_value="12:30"):
            self.assertTrue(PrinterManager.tg_quiet_now(self.fake))
        with _mock.patch.object(_time, "strftime", return_value="14:00"):
            self.assertFalse(PrinterManager.tg_quiet_now(self.fake))


if __name__ == "__main__":
    unittest.main()

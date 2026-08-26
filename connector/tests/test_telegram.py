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
from connector.printflow.repo import Repo  # noqa: E402
from connector.printflow.telegram_bot import TelegramBot  # noqa: E402


class FakeManager:
    """Менеджер-заглушка: база, учёт и снимок парка из переданных данных."""

    def __init__(self, db, snapshot=None):
        self.db = db
        self.acc = Accounting(db)
        self.repo = Repo(db)
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

    def test_fulfill_requires_explicit_payment_choice(self):
        self._order("1003", 1000, 300, status="ready")
        self.db.execute("UPDATE orders SET paid=300,prepaid=0 WHERE number='1003'")
        pending = self.bot._fulfill("выдать 1003")
        self.assertIn("оплачен", pending)
        self.assertIn("в долг", pending)
        self.assertEqual(self.db.one("SELECT status FROM orders WHERE number='1003'")["status"],
                         "ready")
        done = self.bot._fulfill("выдать 1003 оплачен перевод")
        self.assertIn("получено", done)
        self.assertEqual(self.db.one("SELECT status FROM orders WHERE number='1003'")["status"],
                         "done")
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM payments WHERE order_id='o1003'")["n"], 1)

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

    def test_shelf_summary_marks_shortage_and_print_plan(self):
        self.db.upsert("shelf_items", {
            "id": "s1", "name": "Адресник", "qty": 1, "price": 500,
            "cost_per_unit": 120, "min_qty": 3, "active": 1,
        })
        text = self.bot.text_shelf()
        self.assertIn("Стеллаж: 1 поз.", text)
        self.assertIn("Адресник — 1", text)
        self.assertIn("мало", text)
        # Без факта продаж бот не выдумывает план печати: показывает дефицит,
        # а количество для пополнения строится только из реального спроса.
        self.assertNotIn("печать +", text)
        self.assertIn("Внимание к стеллажу", self.bot.text_shelf(only_needs=True))

    def test_shelf_sell_and_sales_report_from_telegram(self):
        self.db.upsert("shelf_items", {
            "id": "s1", "name": "Адресник", "qty": 3, "price": 500,
            "cost_per_unit": 120, "active": 1,
        })
        reply = self.bot.do_shelf_sell("s1", 1)
        self.assertIn("Продано", reply)
        self.assertIn("Осталось 2", reply)
        sales = self.bot.text_shelf_sales(7)
        self.assertIn("Продажи стеллажа за 7", sales)
        self.assertIn("Адресник", sales)
        moves = self.bot.text_shelf_moves(5)
        self.assertIn("Последние движения", moves)
        # Продажа без цены тоже проходит как списание.
        self.db.upsert("shelf_items", {
            "id": "s2", "name": "Визитка", "qty": 2, "price": 0,
            "cost_per_unit": 0, "active": 1,
        })
        self.assertIn("Продано", self.bot.do_shelf_sell("s2", 1))

    def test_sell_rows_return_all_available_items(self):
        """Меню продаж показывает все позиции с остатком, а не первые 8."""
        for i in range(1, 13):
            self.db.upsert("shelf_items", {
                "id": f"s{i}", "name": f"Позиция {i}", "qty": 2, "price": 100,
                "cost_per_unit": 10, "active": 1})
        self.db.upsert("shelf_items", {
            "id": "empty", "name": "Пустая", "qty": 0, "price": 100,
            "cost_per_unit": 10, "active": 1})
        rows = self.bot._sell_rows()
        # все 12 с остатком, пустая — не кандидат
        self.assertEqual(len(rows), 12)
        # страницы нарезаются корректно: 12 / 8 = 2 страницы
        page_rows, page, total = self.bot._paginate(rows, 0)
        self.assertEqual(len(page_rows), 8)
        self.assertEqual(page, 0)
        self.assertEqual(total, 2)
        page_rows2, page2, _ = self.bot._paginate(rows, 1)
        self.assertEqual(page2, 1)
        self.assertEqual(len(page_rows2), 4)
        # за пределами — отдаём последнюю страницу, не падаем
        _, page3, _ = self.bot._paginate(rows, 99)
        self.assertEqual(page3, 1)

    def test_shop_cash_text_and_collect(self):
        """Касса магазина: сводка и запись выемки."""
        from connector.printflow.shelf import Shelf
        shelf = Shelf(self.db)
        self.db.upsert("shelf_items", {
            "id": "s1", "name": "Адресник", "qty": 5, "price": 500,
            "cost_per_unit": 120, "active": 1})
        # две продажи по 500 ₽ = 1000 ₽ в кассе магазина
        shelf.sale("s1", 1, 0, channel="shelf", note="продажа из Telegram")
        shelf.sale("s1", 1, 0, channel="shelf", note="продажа из Telegram")
        text = self.bot.text_shop_cash()
        self.assertIn("Касса стеллажа", text)
        self.assertIn("1 000", text)  # продано со стеллажа
        # запись выемки
        reply = self.bot.do_collect_from_shop("забрали 400 наличными")
        self.assertIn("Забрали из магазина 400", reply)
        self.assertIn("600", reply)  # осталось 1000-400
        # больше накопленного забрать нельзя
        over = self.bot.do_collect_from_shop("забрали 999999")
        self.assertIn("Не получилось", over)

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

    def test_queue_reorder(self):
        self._order("1001", 500, 500, product="длинное")
        self._order("1002", 500, 500, product="срочное")
        self._order("1003", 500, 500, product="запасное")
        self.db.upsert("print_jobs", {
            "id": "j1", "order_id": "o1001", "name": "длинное", "state": "queued",
            "file": "a.3mf", "priority": 3, "created_at": "2026-08-10T10:00:00"})
        self.db.upsert("print_jobs", {
            "id": "j2", "order_id": "o1002", "name": "срочное", "state": "queued",
            "file": "b.3mf", "priority": 2, "created_at": "2026-08-10T10:01:00"})
        self.db.upsert("print_jobs", {
            "id": "j3", "order_id": "o1003", "name": "запасное", "state": "queued",
            "file": "c.3mf", "priority": 1, "created_at": "2026-08-10T10:02:00"})

        # заказ 1003 последний → «выше 1003» ставит его выше 1002
        result = self.bot._reorder_queue("выше 1003", "выше")
        self.assertIn("передвинуто выше", result)
        jobs = self.db.query("SELECT j.name, j.priority FROM print_jobs j"
                             " WHERE j.state='queued' ORDER BY j.priority DESC")
        self.assertEqual(jobs[0]["name"], "длинное")
        self.assertEqual(jobs[1]["name"], "запасное")

        edge = self.bot._reorder_queue("выше 1001", "выше")
        self.assertIn("уже первое", edge)
        missing = self.bot._reorder_queue("выше 9999", "выше")
        self.assertIn("не стоит в очереди", missing)

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

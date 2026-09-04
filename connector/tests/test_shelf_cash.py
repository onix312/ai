"""Касса магазина (стеллаж): учёт денег, которые лежат в магазине.

Правило: продажа со стеллажа даёт доход PrintFlow (channel='shelf'), но
физически деньги остаются в кассе магазина. Выемка «забрали из магазина»
не создаёт проводки, а только уменьшает остаток магазина. Больше накопленного
забрать нельзя; онлайн-продажи (channel='online') в кассу магазина не идут.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow.shelf import Shelf  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "test.sqlite3")


class ShelfCashTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.shelf = Shelf(self.db)
        self.db.upsert("shelf_items", {
            "id": "s1", "name": "Адресник", "qty": 10, "price": 500,
            "cost_per_unit": 120, "active": 1})

    def tearDown(self):
        self.db.close()

    def test_shelf_sale_is_income_with_channel_shelf(self):
        """Продажа со стеллажа записывает доход с channel='shelf'."""
        self.shelf.sale("s1", 2, 0, channel="shelf", note="продажа")
        tx = self.db.one("SELECT * FROM transactions WHERE kind='income'")
        self.assertIsNotNone(tx)
        self.assertEqual(tx["channel"], "shelf")
        self.assertEqual(tx["amount"], 1000)

    def test_shop_cash_tracks_income_and_collection(self):
        """shop_cash: доход, выемки и остаток в магазине."""
        self.shelf.sale("s1", 2, 0, channel="shelf", note="продажа")
        state = self.shelf.shop_cash()
        self.assertEqual(state["shelf_income"], 1000)
        self.assertEqual(state["collected_total"], 0)
        self.assertEqual(state["in_shop"], 1000)
        # выемка уменьшает остаток, не трогая доход
        self.shelf.add_collection(400, "наличными")
        state = self.shelf.shop_cash()
        self.assertEqual(state["shelf_income"], 1000)
        self.assertEqual(state["collected_total"], 400)
        self.assertEqual(state["in_shop"], 600)

    def test_collection_more_than_income_is_rejected(self):
        """Нельзя забрать больше, чем накоплено от стеллажа."""
        self.shelf.sale("s1", 1, 0, channel="shelf", note="продажа")
        with self.assertRaises(ValueError):
            self.shelf.add_collection(999999)

    def test_collection_zero_or_negative_rejected(self):
        with self.assertRaises(ValueError):
            self.shelf.add_collection(0)
        with self.assertRaises(ValueError):
            self.shelf.add_collection(-5)

    def test_delete_collection_returns_money_to_shop(self):
        """Отмена выемки возвращает деньги в остаток магазина."""
        self.shelf.sale("s1", 2, 0, channel="shelf", note="продажа")
        row = self.shelf.add_collection(300)
        self.assertEqual(self.shelf.shop_cash()["in_shop"], 700)
        self.shelf.delete_collection(row["id"])
        self.assertEqual(self.shelf.shop_cash()["in_shop"], 1000)
        self.assertEqual(self.shelf.shop_cash()["collected_total"], 0)

    def test_online_sale_not_in_shop_cash(self):
        """Онлайн-продажа (channel='online') не лежит в кассе магазина."""
        self.shelf.sale("s1", 1, 0, channel="online", note="Авито")
        state = self.shelf.shop_cash()
        self.assertEqual(state["shelf_income"], 0)
        self.assertEqual(state["in_shop"], 0)
        self.assertEqual(state["online_income"], 500)

    def test_online_and_shop_money_split_today(self):
        """«Сегодня» различает полку и онлайн — для кассы и счёта."""
        self.shelf.sale("s1", 2, 0, channel="shelf", note="полка")
        self.shelf.sale("s1", 1, 0, channel="online", note="Авито")
        today = self.shelf.today_sales()
        self.assertEqual(today["qty"], 3)
        self.assertEqual(today["money"], 1500)
        self.assertEqual(today["shop_money"], 1000)
        self.assertEqual(today["online_money"], 500)
        self.assertEqual(today["online_qty"], 1)

    def test_today_sales_counts_only_active_moves(self):
        """«Продано сегодня»: считаются продажи, отменённые — нет."""
        self.shelf.sale("s1", 2, 0, channel="shelf", note="утром")
        self.shelf.sale("s1", 1, 0, channel="online", note="Авито")
        before = self.shelf.today_sales()
        self.assertEqual(before["qty"], 3)
        self.assertEqual(before["money"], 1500)
        # отменяем продажу: деньги и штуки уходят из «сегодня»
        move = self.db.one(
            "SELECT id FROM shelf_moves WHERE kind='sale' ORDER BY at DESC LIMIT 1")
        self.shelf.undo_sale(move["id"])
        after = self.shelf.today_sales()
        self.assertEqual(after["qty"], 1)
        self.assertEqual(after["money"], 500)

    def test_summary_exposes_today_sales(self):
        """Сводка стеллажа отдаёт «сегодня» для панели и бота."""
        self.shelf.sale("s1", 2, 0, channel="shelf", note="продажа")
        summary = self.shelf.summary()
        self.assertEqual(summary["sold_today"], 2)
        self.assertEqual(summary["sold_today_money"], 1000)


if __name__ == "__main__":
    unittest.main()

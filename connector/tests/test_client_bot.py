"""Клиентский бот: каталог, заказ по номеру, статусы, привязка телефона.

Проверяется логика без сети: токен не задан, Telegram не вызывается.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.client_bot import ClientBot  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.manager import PrinterManager  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402


class ClientBotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.manager = PrinterManager(self.db, Repo(self.db))
        self.bot = self.manager.client_bot
        self.assertIsNotNone(self.bot)

    def tearDown(self):
        self.manager.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _product(self, key: str, name: str, price: float, grams: float = 10):
        self.db.upsert("nomenclature", {
            "id": key, "kind": "product", "name": name,
            "grams": grams, "hours": 0.5, "qty": 3, "material": "PLA",
            "created_at": "2026-08-20T10:00:00"})
        if price > 0:
            from connector.printflow.nomenclature import Nomenclature
            Nomenclature(self.db).set_price(key, price)
        return self.db.one("SELECT * FROM nomenclature WHERE id=?", (key,))

    def _chat_row(self, chat: str = "555"):
        return self.db.upsert("client_chats", {
            "chat_id": chat, "name": "Иван", "username": "ivan",
            "created_at": "2026-08-24T10:00:00",
            "last_seen": "2026-08-24T12:00:00"}, key="chat_id")

    def test_catalog_lists_priced_products(self):
        self._product("a", "Адресник", 350)
        self._product("b", "Брелок без цены", 0)
        text = self.bot.text_catalog()
        self.assertIn("Адресник", text)
        self.assertIn("350", text)
        self.assertNotIn("Брелок без цены", text)

    def test_order_by_position_creates_order_and_link(self):
        self._product("a", "Адресник", 350)
        row = self._chat_row()
        answer = self.bot._order_item("555", row, "1")
        self.assertIn("✓", answer)
        order = self.db.one("SELECT * FROM orders ORDER BY created_at DESC")
        self.assertEqual(order["product"], "Адресник")
        self.assertEqual(order["channel"], "telegram")
        self.assertEqual(order["status"], "new")
        link = self.db.one("SELECT * FROM client_orders WHERE chat_id='555'")
        self.assertEqual(link["order_id"], order["id"])

    def test_custom_order_creates_lead(self):
        row = self._chat_row()
        answer = self.bot._custom_order("555", row, "индивидуальный держатель для кабеля 120 мм")
        self.assertIn("✓", answer)
        order = self.db.one("SELECT * FROM orders ORDER BY created_at DESC")
        self.assertIn("держатель", order["product"])

    def test_status_requires_own_order(self):
        row = self._chat_row()
        other = self.db.upsert("orders", {
            "id": "o1001", "number": "1001", "customer_name": "Мария",
            "product": "адресник", "price": 300, "status": "print",
            "created_at": "2026-08-24T10:00:00", "updated_at": "2026-08-24T10:00:00"})
        denied = self.bot.text_order_status("555", row, "1001")
        self.assertIn("не ваш", denied)
        self.db.execute(
            "INSERT OR IGNORE INTO client_orders(chat_id,order_id,number,created_at)"
            " VALUES('555','o1001','1001','2026-08-24T11:00:00')")
        allowed = self.bot.text_order_status("555", row, "1001")
        self.assertIn("1001", allowed)
        self.assertIn(other["product"], allowed)

    def test_phone_binding_links_shelf_orders(self):
        row = self._chat_row()
        self.db.upsert("orders", {
            "id": "o1002", "number": "1002", "customer_name": "Иван",
            "phone": "+7 978 111-22-33", "product": "брелок", "price": 250,
            "status": "new", "created_at": "2026-08-24T10:00:00",
            "updated_at": "2026-08-24T10:00:00"})
        answer = self.bot._save_phone("555", row, "телефон +7 978 111-22-33")
        self.assertIn("привязан", answer)
        mine = self.bot.text_my_orders("555", row)
        self.assertIn("1002", mine)

    def test_push_notifies_once_per_status(self):
        row = self._chat_row()
        order = self.db.upsert("orders", {
            "id": "o1003", "number": "1003", "customer_name": "Иван",
            "product": "крючок", "price": 200, "status": "new",
            "created_at": "2026-08-24T10:00:00",
            "updated_at": "2026-08-24T10:00:00"})
        self.db.execute(
            "INSERT OR IGNORE INTO client_orders(chat_id,order_id,number,created_at)"
            " VALUES('555','o1003','1003','2026-08-24T11:00:00')")
        sent: list[tuple[str, str]] = []
        self.bot._reply = lambda chat, text, buttons=None: sent.append((chat, text))
        self.bot._maybe_push_statuses()   # первая проходка — молчит о создании
        self.db.execute("UPDATE orders SET status='print',"
                        " updated_at='2026-08-24T12:00:00' WHERE id='o1003'")
        self.bot._maybe_push_statuses()   # смена статуса — одно уведомление
        self.bot._maybe_push_statuses()   # повтор той же смены — тишина
        pushes = [s for s in sent if "1003" in s[1]]
        self.assertEqual(len(pushes), 1)
        self.assertIn("крючок", pushes[0][1])

    def test_log_and_stats(self):
        row = self._chat_row()
        self.bot._log("555", "Иван", "каталог", "вот каталог")
        stats = self.bot.stats()
        self.assertGreaterEqual(stats["chats"], 1)
        self.assertGreaterEqual(stats["messages"], 1)


if __name__ == "__main__":
    unittest.main()

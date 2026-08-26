"""Гонки на публичных заявках, рассылках и платежах.

Тесты используют одну SQLite-базу и несколько потоков: именно такой retry
возникает, когда браузер/Telegram не получил ответ вовремя и повторил запрос.
"""
from __future__ import annotations

import pathlib
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from connector.printflow.accounting import Accounting
from connector.printflow.api import Api
from connector.printflow.db import Database
from connector.printflow.nomenclature import Nomenclature
from connector.printflow.repo import Repo
from connector.printflow.stock import Stock


class IdempotencyRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "race.sqlite3")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _public_api(self) -> Api:
        api = Api.__new__(Api)
        api.db = self.db
        api.repo = Repo(self.db)
        api.nom = Nomenclature(self.db)
        api.stock = Stock(self.db)
        api.bus = types.SimpleNamespace(publish=lambda *a, **k: None)
        return api

    def _seed_product(self):
        self.db.upsert("nomenclature", {
            "id": "nom-race", "kind": "product", "name": "Тестовая позиция",
            "grams": 10, "hours": 0.5, "archived": 0,
            "created_at": "2026-08-26T00:00:00",
        })
        Nomenclature(self.db).set_price("nom-race", 300)

    def test_public_order_same_request_creates_one_order(self):
        self._seed_product()
        api = self._public_api()
        body = {
            "request_id": "browser-retry-42", "name": "Анна", "phone": "+79990000000",
            "nom_id": "nom-race", "qty": 1, "source": "utm_campaign",
        }
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: api.public_order(dict(body)), range(8)))
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM orders")["n"], 1)
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) n FROM idempotency_keys WHERE request_id=?",
            (body["request_id"],))["n"], 1)
        self.assertEqual({result["order_id"] for result in results},
                         {results[0]["order_id"]})
        self.assertGreaterEqual(sum(bool(result.get("already_recorded")) for result in results), 1)

    def test_public_order_reservation_is_variant_aware(self):
        self._seed_product()
        self.db.upsert("nom_variants", {
            "id": "variant-red", "nom_id": "nom-race", "name": "Красный",
            "color_name": "Красный", "archived": 0,
        })
        Nomenclature(self.db).set_price("nom-race", 450, variant_id="variant-red")
        self.db.upsert("warehouses", {"id": "warehouse-race", "name": "Склад", "archived": 0})
        Stock(self.db).add_move("nom-race", "warehouse-race", 1, 50,
                                variant_id="variant-red", doc_kind="receipt")
        result = self._public_api().public_order({
            "request_id": "variant-request", "name": "Анна", "phone": "p",
            "items": [{"nom_id": "nom-race", "variant_id": "variant-red", "qty": 1}],
        })
        reserve = self.db.one("SELECT * FROM reserves WHERE order_id=?", (result["order_id"],))
        self.assertEqual(reserve["variant_id"], "variant-red")
        self.assertEqual(reserve["qty"], 1)

    def test_payment_same_request_is_one_cash_entry(self):
        order_id = "order-payment-race"
        self.db.upsert("orders", {
            "id": order_id, "number": "7001", "product": "Оплата",
            "customer_name": "Анна", "price": 500, "status": "new",
            "created_at": "2026-08-26T00:00:00", "updated_at": "2026-08-26T00:00:00",
        })
        accounting = Accounting(self.db)

        def pay(_):
            return accounting.add_payment(order_id, 500, "payment", request_id="pay-retry-42")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(pay, range(8)))
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) n FROM payments WHERE request_id='pay-retry-42'")["n"], 1)
        self.assertEqual(self.db.one(
            "SELECT paid FROM orders WHERE id=?", (order_id,))["paid"], 500)
        self.assertGreaterEqual(sum(bool(result.get("already_recorded")) for result in results), 1)

    def test_broadcast_same_request_enqueues_once(self):
        self.db.set_settings({"client_bot_marketing_enabled": True})
        self.db.upsert("client_chats", {
            "chat_id": "555", "name": "Анна", "marketing_opt_in": 1,
            "created_at": "2026-08-26T00:00:00",
        }, key="chat_id")
        calls = []
        client = types.SimpleNamespace(
            _in_quiet_hours=lambda row: False,
            _menu=lambda: {"inline_keyboard": []},
            _reply_keyed=lambda *args, **kwargs: calls.append((args, kwargs)),
        )
        api = Api.__new__(Api)
        api.db = self.db
        api.manager = types.SimpleNamespace(client_bot=client)
        api._audit = mock.Mock()
        body = {"request_id": "broadcast-retry-42", "text": "Новинка NOZZA",
                "confirmed": True}

        def send(_):
            return api.post("/api/client-bot/broadcast", dict(body), {})

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(send, range(8)))
        self.assertEqual({status for status, _ in results}, {200})
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) n FROM client_broadcasts WHERE request_id=?",
            (body["request_id"],))["n"], 1)
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(sum(bool(payload.get("already_recorded"))
                                  for _, payload in results), 1)


if __name__ == "__main__":
    unittest.main()

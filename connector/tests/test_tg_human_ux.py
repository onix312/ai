"""Человеческий интерфейс Telegram-бота (15.2.2): подсказки, касса стеллажа.

Без сети: проверяются тексты, кнопки и разграничение прав — как в
test_staff_bot / test_telegram, но для новых UX-сценариев.
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
from connector.printflow.shelf import Shelf  # noqa: E402
from connector.printflow.staff import Staff  # noqa: E402
from connector.printflow.telegram_bot import (  # noqa: E402
    TelegramBot, suggest_command)


class FakeManager:
    def __init__(self, db, snapshot=None):
        self.db = db
        self.acc = Accounting(db)
        self.repo = None
        self.client_bot = None
        self._snapshot = snapshot or {"printers": []}

    def snapshot(self, printer_id: str = "") -> dict:
        return self._snapshot

    def queue(self):
        return []


def _shelf_item(db, name: str = "Адресник", qty: float = 5,
                price: float = 500.0, cost: float = 150.0) -> dict:
    row = db.upsert("shelf_items", {
        "id": f"it-{name.lower()}-{qty}", "name": name, "qty": qty,
        "price": price, "cost_per_unit": cost, "min_qty": 2,
        "active": 1, "created_at": "2026-09-01T10:00:00",
        "updated_at": "2026-09-01T10:00:00"})
    return row


class SuggestCommandTests(unittest.TestCase):
    """Непонятое сообщение → ближайшая команда, а не сухой отказ."""

    def test_typo_resolves_to_shelf(self):
        self.assertEqual(suggest_command("полк"), "стеллаж")
        self.assertEqual(suggest_command("стeллашь"), "стеллаж")
        self.assertEqual(suggest_command("приход"), "приход")

    def test_typo_resolves_to_common_verbs(self):
        self.assertEqual(suggest_command("продолжить"), "продолжить")
        self.assertEqual(suggest_command("снимок"), "кадр")
        self.assertEqual(suggest_command("деньги"), "деньги")

    def test_phrase_matches(self):
        self.assertEqual(suggest_command("движния стелажа"), "движения стеллажа")
        self.assertEqual(suggest_command("продажи полки"), "продажи стеллажа")
        self.assertEqual(suggest_command("движения полки"), "движения стеллажа")

    def test_gibberish_has_no_suggestion(self):
        self.assertEqual(suggest_command("qwerty123"), "")
        self.assertEqual(suggest_command("абракадабра"), "")
        self.assertEqual(suggest_command("а"), "")


class UnknownReplyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111"})
        self.manager = FakeManager(self.db)
        self.bot = TelegramBot(self.manager)

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def test_unknown_suggests_and_sends_buttons(self):
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot._dispatch("111", "полк")  # опечатка «полка» → «стеллаж»
        self.assertEqual(calls[-1][0], "sendMessage")
        markup = calls[-1][1]["reply_markup"]
        self.assertIn("стеллаж", markup)
        self.assertIn("cmd:goto:", markup)

    def test_gibberish_gets_help_buttons_not_guess(self):
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot._dispatch("111", "ываяыва")
        markup = calls[-1][1]["reply_markup"]
        self.assertIn("cmd:help", markup)
        self.assertNotIn("cmd:goto:", markup)

    def test_goto_denies_finance_for_employee(self):
        """Кнопка-подсказка не должна обходить права роли."""
        Staff(self.db).add("Ваня", "employee", "222")
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        dispatched = []
        self.bot._dispatch = lambda chat, text: dispatched.append(text)
        self.bot._handle_callback({
            "id": "cb1",
            "message": {"message_id": 1, "chat": {"id": "222"}},
            "data": "cmd:goto:деньги",
        }, "111")
        self.assertEqual(dispatched, [])
        answers = [p for m, p in calls if m == "answerCallbackQuery"]
        self.assertTrue(any("Недоступно" in p.get("text", "") for p in answers), calls)

    def test_goto_allows_shelf_for_employee(self):
        Staff(self.db).add("Ваня", "employee", "222")
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        dispatched = []
        self.bot._dispatch = lambda chat, text: dispatched.append(text)
        self.bot._handle_callback({
            "id": "cb2",
            "message": {"message_id": 2, "chat": {"id": "222"}},
            "data": "cmd:goto:стеллаж",
        }, "111")
        self.assertEqual(dispatched, ["стеллаж"])


class ShelfCashBotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111"})
        self.manager = FakeManager(self.db)
        self.bot = TelegramBot(self.manager)
        self.shelf = Shelf(self.db)

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _sell(self, qty: float = 1, price: float = 500.0):
        item = _shelf_item(self.db)
        self.shelf.sale(item["id"], qty, price, channel="shelf")

    def test_shelf_text_shows_today_and_cash(self):
        item = _shelf_item(self.db)
        self.shelf.sale(item["id"], 2, 500, channel="shelf")
        text = self.bot.text_shelf()
        self.assertIn("Сегодня", text)
        self.assertIn("1 000", text)
        self.assertIn("в кассе магазина", text)

    def test_shelf_text_shows_online_money_separately(self):
        item = _shelf_item(self.db)
        self.shelf.sale(item["id"], 1, 700, channel="shelf")
        self.shelf.sale(item["id"], 1, 800, channel="online")
        text = self.bot.text_shelf()
        self.assertIn("Онлайн (Авито/ТГ)", text)
        self.assertIn("800", text)
        # онлайн не прибавляется к кассе магазина
        self.assertIn("на счёте", text)

    def test_cash_menu_shows_online_row(self):
        item = _shelf_item(self.db)
        self.shelf.sale(item["id"], 1, 800, channel="online")
        text = self.bot.text_shelf_cash()
        self.assertIn("Онлайн (Авито/ТГ)", text)
        self.assertIn("800", text)
        self.assertIn("на счёте", text)

    def test_cash_screen_has_online_income(self):
        item = _shelf_item(self.db)
        self.shelf.sale(item["id"], 1, 900, channel="online")
        cash = self.shelf.shop_cash()
        self.assertEqual(cash["shelf_income"], 0)  # в кассу магазина не входит
        self.assertEqual(cash["online_income"], 900)
        self.assertEqual(cash["in_shop"], 0)

    def test_shelf_text_without_items_is_honest(self):
        text = self.bot.text_shelf()
        self.assertIn("Стеллаж пуст", text)

    def test_main_menu_has_shelf_and_cash_buttons(self):
        """Полка и касса — на первом уровне меню, не только текстом."""
        import json as _json
        menu = _json.dumps(self.bot._inline_menu(), ensure_ascii=False)
        self.assertIn('"callback_data": "cmd:shelf"', menu)
        self.assertIn('"callback_data": "cmd:shelf-cash"', menu)
        self.assertIn("Стеллаж", menu)
        self.assertIn("Касса", menu)

    def test_cash_collect_all_and_constraint(self):
        item = _shelf_item(self.db)
        self.shelf.sale(item["id"], 4, 500, channel="shelf")  # 2 000 ₽ в магазине
        result = self.bot.do_shelf_collect("all")
        self.assertIn("Забрали 2 000", result)
        # второй раз уже нечего забирать
        self.assertIn("нет денег", self.bot.do_shelf_collect("all"))
        # забрать больше, чем лежит — отказ без списания
        state = self.shelf.shop_cash()
        self.assertEqual(num(state["in_shop"]), 0)

    def test_cash_collect_too_much_refused(self):
        item = _shelf_item(self.db)
        self.shelf.sale(item["id"], 1, 500, channel="shelf")
        result = self.bot.do_shelf_collect("9999")
        self.assertIn("нельзя", result)
        self.assertEqual(self.shelf.shop_cash()["in_shop"], 500)

    def test_cash_keyboard_sends_collect_buttons(self):
        item = _shelf_item(self.db)
        self.shelf.sale(item["id"], 4, 500, channel="shelf")  # 2 000 ₽ в магазине
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot.shelf_cash_keyboard("111")
        msg = [p for m, p in calls if m == "sendMessage"][0]
        markup = msg["reply_markup"]
        self.assertIn("shelf-cash-w:1000", markup)
        self.assertNotIn("shelf-cash-w:5000", markup)  # больше, чем лежит
        self.assertIn("shelf-cash-w:all", markup)

    def test_dispatch_cash_opens_button_menu(self):
        item = _shelf_item(self.db)
        self.shelf.sale(item["id"], 2, 500, channel="shelf")
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot._dispatch("111", "касса")
        self.assertIn("sendMessage", [m for m, _ in calls])
        self.assertIn("shelf-cash-w:", calls[-1][1]["reply_markup"])

    def test_collect_all_by_text(self):
        item = _shelf_item(self.db)
        self.shelf.sale(item["id"], 2, 500, channel="shelf")
        result = self.bot.do_collect_from_shop("забрали все")
        self.assertIn("Забрали из магазина 1 000", result)
        self.assertEqual(self.shelf.shop_cash()["in_shop"], 0)


class MainMenuButtonTests(unittest.TestCase):
    """Каркас бота-кнопок (15.4.0): нижняя панель, «Ещё», навигация.

    Команды остаются скрытым способом, кнопки — основной вход: ни одна
    функция не требует знать команду.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111"})
        self.manager = FakeManager(self.db)
        self.bot = TelegramBot(self.manager)

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def test_main_reply_keyboard_has_six_buttons(self):
        markup = self.bot._main_reply_keyboard()
        flat = [b for row in markup["keyboard"] for b in row]
        self.assertEqual(
            flat, ["🛒 Продать", "📦 Полка", "💰 Касса",
                   "📷 Кадр", "📊 Итоги", "⚙️ Ещё"])
        self.assertTrue(markup.get("is_persistent"))

    def test_help_has_no_command_list(self):
        from connector.printflow.telegram_bot import HELP
        self.assertIn("кнопками", HELP)
        self.assertIn("⚙️ Ещё", HELP)
        self.assertNotIn("продажа —", HELP)
        self.assertNotIn("• стеллаж", HELP)

    def test_help_sends_reply_keyboard(self):
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot._dispatch("111", "помощь")
        msg = [p for m, p in calls if m == "sendMessage"][-1]
        self.assertIn('"keyboard"', msg["reply_markup"])
        self.assertIn("кнопками", msg["text"])

    def test_reply_alias_more_opens_more_menu(self):
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot._dispatch("111", "⚙️ Ещё")
        msg = [p for m, p in calls if m == "sendMessage"][-1]
        markup = msg["reply_markup"]
        for cmd in ("cmd:queue", "cmd:printers", "cmd:filament", "cmd:cat",
                    "cmd:plan", "cmd:money", "cmd:doctor", "cmd:team",
                    "cmd:menu", "cmd:help"):
            self.assertIn(cmd, markup)

    def test_reply_alias_sell_opens_shelf_menu(self):
        _shelf_item(self.db)
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot._dispatch("111", "🛒 Продать")
        msg = [p for m, p in calls if m == "sendMessage"][-1]
        # 15.4: продажа начинается с «Все товары» (топ недели добавится при продажах)
        self.assertIn("cmd:sell-menu", msg["reply_markup"])
        self.assertIn("cmd:menu", msg["reply_markup"])

    def test_more_includes_orders_and_clients(self):
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot._handle_callback({
            "id": "cb-more2",
            "message": {"message_id": 9, "chat": {"id": "111"}},
            "data": "cmd:more"}, "111")
        markup = calls[-1][1]["reply_markup"]
        self.assertIn("cmd:orders", markup)
        self.assertIn("cmd:clients", markup)

    def test_queue_keyboard_has_controls(self):
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot.queue_keyboard("111")
        markup = calls[-1][1]["reply_markup"]
        for cmd in ("cmd:next", "cmd:removed", "cmd:pause", "cmd:resume",
                    "cmd:frame", "cmd:stop", "cmd:menu"):
            self.assertIn(cmd, markup)

    def test_orders_callback_opens_list(self):
        self.db.upsert("orders", {
            "id": "o-1001", "number": "1001", "product": "Адресник",
            "customer_name": "Мария", "price": 900, "qty": 1, "status": "new",
            "created_at": "2026-09-04T10:00:00",
            "updated_at": "2026-09-04T10:00:00"})
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot._handle_callback({
            "id": "cb-orders",
            "message": {"message_id": 10, "chat": {"id": "111"}},
            "data": "cmd:orders"}, "111")
        self.assertIn("cmd:order:1001", calls[-1][1]["reply_markup"])

    def test_menu_callback_sends_main_menu(self):
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot._handle_callback({
            "id": "cb-menu",
            "message": {"message_id": 7, "chat": {"id": "111"}},
            "data": "cmd:menu",
        }, "111")
        send = [p for m, p in calls if m == "sendMessage"]
        self.assertTrue(send)
        self.assertIn('"keyboard"', send[-1]["reply_markup"])
        answered = [p for m, p in calls if m == "answerCallbackQuery"]
        self.assertTrue(answered)

    def test_more_callback_edits_same_message(self):
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot._handle_callback({
            "id": "cb-more",
            "message": {"message_id": 8, "chat": {"id": "111"}},
            "data": "cmd:more",
        }, "111")
        edit = [p for m, p in calls if m == "editMessageText"]
        self.assertTrue(edit)
        self.assertIn("cmd:team", edit[-1]["reply_markup"])

    def test_team_callback_returns_staff_list(self):
        text = self.bot._run_command("team", "111")
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip())

    def test_every_screen_has_home_button(self):
        _shelf_item(self.db)
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot.shelf_keyboard("111")
        self.bot.sell_keyboard("111")
        self.bot.shelf_cash_keyboard("111")
        self.bot.shelf_produce_keyboard("111")
        for m, p in calls:
            if m == "sendMessage" and "reply_markup" in p:
                self.assertIn("cmd:menu", p["reply_markup"])

    def test_unknown_offers_menu_button(self):
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot._dispatch("111", "ываяыва")
        markup = calls[-1][1]["reply_markup"]
        self.assertIn("cmd:menu", markup)
        self.assertIn("cmd:help", markup)


class SellFlowButtonTests(unittest.TestCase):
    """Итерация 2: продажа кнопками — топ, количество, цена, канал, ✅."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111"})
        self.manager = FakeManager(self.db)
        self.bot = TelegramBot(self.manager)
        self._shelf_item()

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _shelf_item(self):
        from connector.tests.test_tg_human_ux import _shelf_item
        return _shelf_item(self.db, name="Адресник", qty=5, price=500)

    def test_home_shows_top_after_sales(self):
        item = self._shelf_item()
        Shelf(self.db).sale(item["id"], 3, 500, channel="shelf")
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot.sell_home_keyboard("111")
        markup = calls[-1][1]["reply_markup"]
        self.assertIn("🔥", markup)
        self.assertIn("cmd:sell-item:", markup)

    def test_flow_qty_step(self):
        item = self._shelf_item()
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot.sell_flow_qty("111", item["id"])
        markup = calls[-1][1]["reply_markup"]
        for q in ("cmd:sell-qty:1", "cmd:sell-qty:2", "cmd:sell-qty:5",
                  "cmd:sell-qty:10", "cmd:sell-cancel"):
            self.assertIn(q, markup)

    def test_flow_channel_and_confirm(self):
        item = self._shelf_item()
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot._handle_callback({
            "id": "c1",
            "message": {"message_id": 1, "chat": {"id": "111"}},
            "data": f"cmd:sell-item:{item['id']}"}, "111")
        self.bot._handle_callback({
            "id": "c2",
            "message": {"message_id": 1, "chat": {"id": "111"}},
            "data": "cmd:sell-qty:2"}, "111")
        self.bot._handle_callback({
            "id": "c3",
            "message": {"message_id": 1, "chat": {"id": "111"}},
            "data": "cmd:sell-price:default"}, "111")
        self.assertIn("cmd:sell-channel:", calls[-1][1]["reply_markup"])
        self.bot._handle_callback({
            "id": "c4",
            "message": {"message_id": 1, "chat": {"id": "111"}},
            "data": "cmd:sell-channel:online"}, "111")
        self.assertIn("cmd:sell-confirm", calls[-1][1]["reply_markup"])
        self.assertIn("онлайн", calls[-1][1]["text"])

    def test_confirm_sells_with_channel(self):
        item = self._shelf_item()
        self.bot._sell_flow["111"] = {"item": item["id"], "qty": 2,
                                      "price": 500, "channel": "online",
                                      "await": "confirm"}
        result = self.bot.sell_flow_do("111")
        self.assertIn("на счёт", result)
        cash = Shelf(self.db).shop_cash()
        self.assertEqual(cash["online_income"], 1000)
        self.assertEqual(cash["in_shop"], 0)

    def test_confirm_sells_to_shop_cash(self):
        item = self._shelf_item()
        self.bot._sell_flow["111"] = {"item": item["id"], "qty": 2,
                                      "price": 500, "channel": "shelf",
                                      "await": "confirm"}
        result = self.bot.sell_flow_do("111")
        self.assertIn("в кассу магазина", result)
        self.assertEqual(Shelf(self.db).shop_cash()["in_shop"], 1000)

    def test_custom_price_text(self):
        item = self._shelf_item()
        self.bot._sell_flow["111"] = {"item": item["id"], "qty": 1,
                                      "price": 500, "channel": "",
                                      "await": "price"}
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot._dispatch("111", "450")
        self.assertIn("cmd:sell-channel:", calls[-1][1]["reply_markup"])
        self.assertIn("450", calls[-1][1]["text"])

    def test_cannot_sell_more_than_stock(self):
        item = self._shelf_item()
        result = self.bot.do_shelf_sell(item["id"], 99)
        self.assertIn("только", result)
        self.assertEqual(Shelf(self.db).shop_cash()["in_shop"], 0)

    def test_cancel_keeps_stock(self):
        item = self._shelf_item()
        self.bot._sell_flow["111"] = {"item": item["id"], "qty": 5,
                                      "price": 500, "channel": "shelf"}
        result = self.bot.sell_flow_cancel("111")
        self.assertIn("отменена", result)
        self.assertEqual(Shelf(self.db).shop_cash()["in_shop"], 0)


class CashReconcileButtonTests(unittest.TestCase):
    """Итерация 3: касса — сверка факта и отмена выемки кнопками."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111"})
        self.manager = FakeManager(self.db)
        self.bot = TelegramBot(self.manager)

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def test_cash_keyboard_has_reconcile_and_undo(self):
        item = _shelf_item(self.db)
        Shelf(self.db).sale(item["id"], 4, 500, channel="shelf")  # 2000 в кассе
        Shelf(self.db).add_collection(1000)  # была выемка — есть что отменять
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot.shelf_cash_keyboard("111")
        markup = calls[-1][1]["reply_markup"]
        self.assertIn("shelf-cash-reconcile", markup)
        self.assertIn("cmd:today", markup)
        self.assertIn("shelf-cash-undo:", markup)

    def test_reconcile_start_asks_fact(self):
        item = _shelf_item(self.db)
        Shelf(self.db).sale(item["id"], 4, 500, channel="shelf")
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot._handle_callback({
            "id": "r1",
            "message": {"message_id": 2, "chat": {"id": "111"}},
            "data": "cmd:shelf-cash-reconcile"}, "111")
        self.assertIn("2 000", calls[-1][1]["text"])

    def test_reconcile_fact_ok(self):
        item = _shelf_item(self.db)
        Shelf(self.db).sale(item["id"], 4, 500, channel="shelf")
        result = self.bot._cash_fact("111", "2000")
        self.assertIn("как по учёту", result)

    def test_reconcile_fact_difference(self):
        item = _shelf_item(self.db)
        Shelf(self.db).sale(item["id"], 4, 500, channel="shelf")
        result = self.bot._cash_fact("111", "1800")
        self.assertIn("недостача", result)

    def test_undo_collection_returns_money(self):
        item = _shelf_item(self.db)
        Shelf(self.db).sale(item["id"], 4, 500, channel="shelf")
        coll = Shelf(self.db).add_collection(1000)
        self.assertEqual(Shelf(self.db).shop_cash()["in_shop"], 1000)
        Shelf(self.db).delete_collection(coll["id"])
        self.assertEqual(Shelf(self.db).shop_cash()["in_shop"], 2000)


class OrdersClientsButtonTests(unittest.TestCase):
    """Итерация 5: заказы и клиенты кнопками."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111"})
        self.manager = FakeManager(self.db)
        self.bot = TelegramBot(self.manager)

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _order(self, number="1001"):
        self.db.upsert("orders", {
            "id": f"o-{number}", "number": number, "product": "Адресник",
            "customer_name": "Мария", "phone": "79150000000", "price": 900,
            "prepaid": 0, "qty": 1, "status": "new",
            "created_at": "2026-09-04T10:00:00", "updated_at": "2026-09-04T10:00:00"})
        return number

    def test_orders_keyboard_lists_active(self):
        self._order()
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot.orders_keyboard("111")
        markup = calls[-1][1]["reply_markup"]
        self.assertIn("cmd:order:1001", markup)

    def test_order_card_has_actions(self):
        self._order()
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot.order_card("111", "1001")
        markup = calls[-1][1]["reply_markup"]
        for cmd in ("order-pay:1001", "order-status:1001", "order-ready:1001",
                    "order-fulfill:1001", "watch:1001", "cmd:menu"):
            self.assertIn(cmd, markup)

    def test_clients_keyboard(self):
        self.db.upsert("client_chats", {
            "chat_id": "555", "name": "Мария", "phone": "79150000000",
            "last_seen": "2026-09-04T10:00:00"}, key="chat_id")
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot.clients_keyboard("111")
        markup = calls[-1][1]["reply_markup"]
        self.assertIn("cmd:client:555", markup)

    def test_client_card_has_reply(self):
        self.db.upsert("client_chats", {
            "chat_id": "555", "name": "Мария", "phone": "79150000000",
            "last_seen": "2026-09-04T10:00:00"}, key="chat_id")
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot.client_card("111", "555")
        markup = calls[-1][1]["reply_markup"]
        self.assertIn("cmd:client-reply:555", markup)

    def test_client_reply_goes_to_client_bot(self):
        # без клиентского бота отвечаем честной ошибкой, но не падаем
        result = self.bot._client_answer("кответ 555 Привет")
        self.assertTrue(result)


class ShelfLowNotifyTests(unittest.TestCase):
    """Итерация 6: напоминание о низком остатке полки (раз в день)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111"})
        self.manager = FakeManager(self.db)
        self.bot = TelegramBot(self.manager)

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def test_low_stock_notifies_once_per_day(self):
        _shelf_item(self.db, qty=1, price=500)
        sent = []
        self.manager.notify_async = lambda text, **kw: sent.append((text, kw))
        self.bot._maybe_shelf_low(self.db.settings())
        self.assertEqual(len(sent), 1)
        self.assertIn("заканчивается", sent[0][0])
        self.assertEqual(sent[0][1].get("event"), "shelf:low")
        # второй раз в тот же день — молчим
        self.bot._maybe_shelf_low(self.db.settings())
        self.assertEqual(len(sent), 1)

    def test_no_low_stock_no_notify(self):
        _shelf_item(self.db, qty=10, price=500)
        sent = []
        self.manager.notify_async = lambda text, **kw: sent.append(text)
        self.bot._maybe_shelf_low(self.db.settings())
        self.assertEqual(sent, [])


def num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":
    unittest.main()

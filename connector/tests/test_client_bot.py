"""Клиентский бот: каталог, заказ по номеру, статусы, привязка телефона.

Проверяется логика без сети: токен не задан, Telegram не вызывается.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

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
        answer = self.bot._save_phone("555", row, "телефон +7 978 111-22-33", verified=True)
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

    # ------------------------------------------------- кнопки и ответы (9.3.1)
    def _wire(self):
        """Заглушка транспорта: ловим sendMessage вместо сети."""
        sent: list[tuple[str, dict]] = []
        def fake_call(method, params, timeout=35):
            sent.append((method, params))
            return {"ok": True}
        self.bot._call = fake_call
        return sent

    def _sends(self, sent):
        return [p for m, p in sent if m in ("sendMessage", "editMessageText")]

    @staticmethod
    def _msg(text=None, **extra):
        message = {"chat": {"id": 555, "type": "private"}, "message_id": 1,
                   "from": {"id": 777, "first_name": "Иван", "username": "ivan"}}
        if text is not None:
            message["text"] = text
        message.update(extra)
        return {"update_id": 1, "message": message}

    @staticmethod
    def _cb(data):
        return {"update_id": 2, "callback_query": {
            "id": "cq1", "data": data,
            "message": {"chat": {"id": 555, "type": "private"}, "message_id": 9},
            "from": {"id": 777, "first_name": "Иван", "username": "ivan"}}}

    def test_inline_callback_edits_card_without_new_message(self):
        """Inline-навигация обновляет карточку и не засоряет чат."""
        sent = self._wire()
        self.bot._edit_or_reply("555", {"message_id": 9}, "обновлено", {"inline_keyboard": []})
        self.assertEqual([method for method, _ in sent], ["editMessageText"])

    def test_callback_buttons_reply(self):
        """Баг 9.3: кнопка отвечала только записью в журнал — покупатель
        не видел реакции. Теперь каждая кнопка шлёт ответ."""
        sent = self._wire()
        for data in ("catalog", "mine", "help"):
            sent.clear()
            self.bot._handle(self._cb(data))
            self.assertTrue(self._sends(sent),
                            f"кнопка {data} не отправила ответ")

    def test_text_commands_are_sent(self):
        """Баг 9.3: «мои заказы»/«статус» возвращали текст без отправки."""
        row = self._chat_row()
        sent = self._wire()
        self.bot._handle(self._msg("мои заказы"))
        self.assertTrue(self._sends(sent))
        sent.clear()
        self.bot._handle(self._msg("телефон 8 978 000-00-00"))
        self.assertTrue(any("привязан" in p["text"] for p in self._sends(sent)))

    def test_buy_button_creates_order_and_replies(self):
        self._product("a", "Адресник", 350)
        sent = self._wire()
        self.bot._handle(self._cb("buy:a"))
        order = self.db.one("SELECT * FROM orders ORDER BY created_at DESC")
        self.assertEqual(order["product"], "Адресник")
        self.assertTrue(any("принят" in p["text"] for p in self._sends(sent)))

    def test_status_button_shows_own_order_card(self):
        row = self._chat_row()
        self.db.upsert("orders", {
            "id": "o1001", "number": "1001", "customer_name": "Иван",
            "product": "адресник", "price": 300, "status": "print",
            "created_at": "2026-08-24T10:00:00", "updated_at": "2026-08-24T10:00:00"})
        self.db.execute(
            "INSERT OR IGNORE INTO client_orders(chat_id,order_id,number,created_at)"
            " VALUES('555','o1001','1001','2026-08-24T11:00:00')")
        sent = self._wire()
        self.bot._handle(self._cb("status:1001"))
        self.assertTrue(any("адресник" in p["text"] for p in self._sends(sent)))
        # чужой номер по кнопке не раскрываем
        sent.clear()
        self.bot._handle(self._cb("status:9999"))
        self.assertTrue(any("не найден" in p["text"] for p in self._sends(sent)))

    def test_my_orders_keyboard_has_per_order_buttons(self):
        row = self._chat_row()
        self.db.upsert("orders", {
            "id": "o1001", "number": "1001", "customer_name": "Иван",
            "product": "адресник", "price": 300, "status": "print",
            "created_at": "2026-08-24T10:00:00", "updated_at": "2026-08-24T10:00:00"})
        self.db.execute(
            "INSERT OR IGNORE INTO client_orders(chat_id,order_id,number,created_at)"
            " VALUES('555','o1001','1001','2026-08-24T11:00:00')")
        keys = self.bot._orders_keyboard("555", row)
        datas = [b["callback_data"]
                 for row_ in keys["inline_keyboard"] for b in row_]
        self.assertIn("status:1001", datas)

    def test_photo_gets_guidance_and_is_logged(self):
        sent = self._wire()
        self.bot._handle(self._msg(photo=[{"file_id": "x"}]))
        self.assertTrue(any("получил" in p["text"].lower()
                            for p in self._sends(sent)))
        logged = self.db.one(
            "SELECT * FROM client_bot_log WHERE text LIKE '%фото%'"
            " ORDER BY id DESC")
        self.assertIsNotNone(logged)

    def test_photo_caption_works_as_custom_order(self):
        sent = self._wire()
        self.bot._handle(self._msg(text=None, caption="индивидуальный держатель",
                                   photo=[{"file_id": "x"}]))
        order = self.db.one("SELECT * FROM orders ORDER BY created_at DESC")
        self.assertIn("держатель", order["product"])
        self.assertTrue(any("принята" in p["text"] for p in self._sends(sent)))

    def test_contact_binds_phone(self):
        self._chat_row()
        sent = self._wire()
        self.bot._handle(self._msg(
            contact={"phone_number": "+7 978 111-22-33",
                     "user_id": 777, "first_name": "Иван"}))
        row = self.db.one("SELECT * FROM client_chats WHERE chat_id='555'")
        self.assertEqual(row["phone"], "+79781112233")
        self.assertTrue(any("привязан" in p["text"] for p in self._sends(sent)))

    def test_phone_hint_offers_contact_button(self):
        row = self._chat_row()
        text, buttons = self.bot._dispatch("555", row, "телефон")
        self.assertIn("Отправить номер", text)
        self.assertEqual(buttons["keyboard"][0][0]["request_contact"], True)

    def test_push_includes_menu_buttons(self):
        row = self._chat_row()
        self.db.upsert("orders", {
            "id": "o1003", "number": "1003", "customer_name": "Иван",
            "product": "крючок", "price": 200, "status": "new",
            "created_at": "2026-08-24T10:00:00",
            "updated_at": "2026-08-24T10:00:00"})
        self.db.execute(
            "INSERT OR IGNORE INTO client_orders(chat_id,order_id,number,created_at)"
            " VALUES('555','o1003','1003','2026-08-24T11:00:00')")
        sent = self._wire()
        self.bot._maybe_push_statuses()   # создание — молчим
        self.db.execute("UPDATE orders SET status='print',"
                        " updated_at='2026-08-24T12:00:00' WHERE id='o1003'")
        self.bot._maybe_push_statuses()   # смена статуса — пуш с кнопками
        pushes = [p for p in self._sends(sent) if "1003" in p["text"]]
        self.assertEqual(len(pushes), 1)
        markup = json.loads(pushes[0]["reply_markup"])
        datas = [b["callback_data"] for r in markup["inline_keyboard"] for b in r]
        self.assertIn("mine", datas)

    def test_catalog_off_hides_buy_buttons(self):
        self._product("a", "Адресник", 350)
        self.db.set_settings({"client_bot_catalog": False})
        keys = self.bot._catalog_keyboard()
        datas = [b["callback_data"]
                 for row in keys["inline_keyboard"] for b in row]
        self.assertNotIn("buy:a", datas)

    # ----------------------------------------------------- 9.3.2: сервис бота
    def _notify(self):
        """Заглушка уведомлений мастеру: ловим текст и фото."""
        out: list[tuple[str, bytes | None, bool]] = []
        self.manager.notify_async = (
            lambda text, photo=None, buttons=None, critical=False:
            out.append((text, photo, critical)))
        return out

    def _patch_photo_dir(self):
        """Фото — во временную папку, тест не трогает реальный каталог данных."""
        import connector.printflow.config as pf_config
        old = pf_config.PHOTO_DIR
        tmp = pathlib.Path(self._tmp.name) / "photos"
        pf_config.PHOTO_DIR = tmp
        self.addCleanup(setattr, pf_config, "PHOTO_DIR", old)
        return tmp

    def _own_order(self, number="1001", status="new", price=350.0, age="2026-08-24T10:00:00"):
        order = self.db.upsert("orders", {
            "id": f"o{number}", "number": number, "customer_name": "Иван",
            "product": "адресник", "price": price, "status": status,
            "created_at": age, "updated_at": age})
        self.db.execute(
            "INSERT OR IGNORE INTO client_orders(chat_id,order_id,number,created_at)"
            f" VALUES('555','o{number}','{number}','{age}')")
        return order

    def test_fallback_alerts_master_and_throttles(self):
        """Вопрос мимо команд будит мастера, но не чаще раза в 10 минут."""
        row = self._chat_row()
        notified = self._notify()
        text, _ = self.bot._dispatch("555", row, "здравствуйте когда будет готово")
        self.assertIn("Не понял", text)
        self.assertTrue(notified and "ждёт ответа" in notified[0][0])
        self.assertIn("кответ 555", notified[0][0])
        notified.clear()
        self.bot._dispatch("555", row, "ещё вопросик")
        self.assertEqual(len(notified), 0)   # троттлинг: второй раз молчим

    def test_kotvet_answers_client_from_internal_bot(self):
        """«кответ <чат> <текст>» — ответ уходит покупателю и в журнал."""
        row = self._chat_row()
        sent = self._wire()
        answer = self.manager.bot._client_answer("кответ 555 Добрый день! Готово к пятнице.")
        self.assertIn("Отправлено ✓", answer)
        self.assertTrue(any("Готово к пятнице" in p["text"] for p in self._sends(sent)))
        logged = self.db.one(
            "SELECT * FROM client_bot_log WHERE kind='answer' ORDER BY id DESC")
        self.assertIn("Готово", logged["answer"])
        bad = self.manager.bot._client_answer("кответ 999 текст")
        self.assertIn("не найден", bad)

    def test_photo_without_caption_creates_lead_with_reference(self):
        """Фото без подписи: лид-заявка + файл в заказе + фото мастеру."""
        self._patch_photo_dir()
        row = self._chat_row()
        notified = self._notify()
        sent = self._wire()
        self.bot._download_file = lambda file_id: b"\xff\xd8fake-jpeg"
        self.bot._handle(self._msg(photo=[{"file_id": "f1"}]))
        order = self.db.one("SELECT * FROM orders ORDER BY created_at DESC")
        self.assertIn("Фотозаявка", order["product"])
        photo = self.db.one("SELECT * FROM order_photos WHERE order_id=?",
                            (order["id"],))
        self.assertIsNotNone(photo)
        self.assertEqual(photo["note"], "фото из клиентского бота")
        self.assertTrue(any("получил" in p["text"].lower() for p in self._sends(sent)))
        self.assertTrue(notified and notified[0][1] == b"\xff\xd8fake-jpeg")

    def test_photo_with_caption_attaches_to_custom_order(self):
        self._patch_photo_dir()
        row = self._chat_row()
        self._notify()
        self.bot._download_file = lambda file_id: b"\xff\xd8fake"
        self.bot._handle(self._msg(text=None, caption="индивидуальный держатель",
                                   photo=[{"file_id": "f1"}]))
        order = self.db.one("SELECT * FROM orders ORDER BY created_at DESC")
        self.assertIn("держатель", order["product"])
        self.assertIsNotNone(self.db.one(
            "SELECT * FROM order_photos WHERE order_id=?", (order["id"],)))

    def test_photo_by_number_attaches_to_own_order(self):
        self._patch_photo_dir()
        row = self._chat_row()
        self._own_order("1001")
        sent = self._wire()
        self.bot._download_file = lambda file_id: b"\xff\xd8fake"
        self.bot._handle(self._msg(text=None, caption="фото 1001",
                                   photo=[{"file_id": "f1"}]))
        photo = self.db.one("SELECT * FROM order_photos WHERE order_id='o1001'")
        self.assertIsNotNone(photo)
        self.assertTrue(any("Прикрепил" in p["text"] for p in self._sends(sent)))
        # чужой номер не раскрываем и ничего не крепим
        self.db.execute("DELETE FROM order_photos")
        self.bot._handle(self._msg(text=None, caption="фото 2002",
                                   photo=[{"file_id": "f2"}]))
        self.assertFalse(self.db.one("SELECT * FROM order_photos"))

    def test_faq_button_and_command(self):
        sent = self._wire()
        self.bot._handle(self._cb("faq"))
        self.assertTrue(any("PLA и PETG" in p["text"] for p in self._sends(sent)))
        sent.clear()
        text, buttons = self.bot._dispatch("555", self._chat_row(), "вопрос")
        self.assertIn("PLA и PETG", text)

    def test_catalog_pagination(self):
        """Витрина страницами: нумерация глобальная, «заказ 10» — десятый товар."""
        for i in range(1, 13):
            self._product(f"p{i}", f"Товар {i}", 100 + i)
        rows = self.bot._catalog_rows()
        self.assertEqual(len(rows), 12)
        first = self.bot.text_catalog()
        self.assertIn("1 из 2", first)
        self.assertIn(rows[7]["name"], first)          # последняя позиция страницы 1
        self.assertNotIn(f"{rows[8]['name']} —", first)
        second = self.bot.text_catalog(2)
        self.assertIn("9–12", second)
        self.assertIn(rows[11]["name"], second)
        keys = self.bot._catalog_keyboard(2)
        datas = [b.get("callback_data") or ""
                 for r in keys["inline_keyboard"] for b in r]
        self.assertIn("catalog:1", datas)
        # глобальная нумерация: «заказ 10» = десятая позиция каталога
        answer = self.bot._order_item("555", self._chat_row(), "10")
        self.assertIn("✓", answer)
        order = self.db.one("SELECT * FROM orders ORDER BY created_at DESC")
        self.assertEqual(order["product"], rows[9]["name"])

    def test_item_card_with_photo(self):
        photo_dir = self._patch_photo_dir()
        photo_dir.mkdir(parents=True, exist_ok=True)
        self._product("x", "Ваза", 590)
        (photo_dir / "nom_x.jpg").write_bytes(b"\xff\xd8photo")
        self.db.execute("UPDATE nomenclature SET photo='nom_x.jpg' WHERE id='x'")
        cards: list[tuple[str, bytes, dict]] = []
        self.bot._send_photo = lambda chat, caption, raw, buttons=None: \
            cards.append((caption, raw, buttons))
        self.bot._send_item_card("555", "item:x:1")
        caption, raw, buttons = cards[0]
        self.assertEqual(raw, b"\xff\xd8photo")
        self.assertIn("Ваза", caption)
        self.assertIn("590", caption)
        datas = [b["callback_data"] for r in buttons["inline_keyboard"] for b in r]
        self.assertIn("buy:x", datas)
        # без фото карточка уходит текстом с той же кнопкой
        self._product("y", "Крючок", 150)
        sent = self._wire()
        self.bot._send_item_card("555", "item:y:1")
        self.assertTrue(any("Крючок" in p["text"] for p in self._sends(sent)))

    def test_share_button_uses_bot_username(self):
        self._product("a", "Адресник", 350)
        self.bot._bot_username = "nozza_test_bot"
        keys = self.bot._catalog_keyboard(1)
        urls = [b.get("url") for r in keys["inline_keyboard"] for b in r if b.get("url")]
        self.assertTrue(urls and "t.me/share/url" in urls[0]
                        and "nozza_test_bot" in urls[0])

    def test_review_asked_once_and_rating_flow(self):
        row = self._chat_row()
        self._own_order("1001", status="done", age="2026-08-20T10:00:00")
        self.db.execute("UPDATE orders SET client_delivered_at='2026-08-20T10:00:00' WHERE id='o1001'")
        sent = self._wire()
        self.bot._maybe_ask_reviews()
        self.assertTrue(any("всё ли хорошо" in p["text"].lower()
                            for p in self._sends(sent)))
        sent.clear()
        self.bot._maybe_ask_reviews()      # второй раз не спрашиваем
        self.assertFalse(self._sends(sent))
        notified = self._notify()
        sent.clear()
        self.bot._handle(self._cb("review:o1001:good"))
        review = self.db.one("SELECT * FROM client_reviews WHERE order_id='o1001'")
        self.assertEqual(review["rating"], "good")
        sent.clear()
        self.bot._handle(self._cb("review:o1001:bad"))
        review = self.db.one("SELECT * FROM client_reviews WHERE order_id='o1001'")
        self.assertEqual(review["rating"], "bad")
        self.assertTrue(any("недоволен" in n[0] for n in notified))
        # следующий текст — детали проблемы: комментарий + мастеру
        notified.clear()
        self.bot._handle(self._msg("крепление треснуло на второй день"))
        review = self.db.one("SELECT * FROM client_reviews WHERE order_id='o1001'")
        self.assertIn("треснуло", review["comment"])
        self.assertTrue(any("Детали проблемы" in n[0] for n in notified))
        self.assertTrue(any("поправим" in p["text"] for p in self._sends(sent)))

    def test_pickup_reminder_once(self):
        row = self._chat_row()
        self._own_order("1002", status="ready", age="2026-08-20T10:00:00")
        sent = self._wire()
        self.bot._maybe_remind_pickup()
        self.assertTrue(any("ждёт вас" in p["text"] for p in self._sends(sent)))
        link = self.db.one("SELECT * FROM client_orders WHERE order_id='o1002'")
        self.assertTrue(link["reminded_at"])
        sent.clear()
        self.bot._maybe_remind_pickup()    # повтор не шлём
        self.assertFalse(self._sends(sent))

    def test_pay_card_and_paid_notice(self):
        row = self._chat_row()
        self._own_order("1001", price=350.0)
        self.db.set_settings({"client_bot_pay_info": "СБП +7 900 000-00-00, NOZZA"})
        sent = self._wire()
        self.bot._handle(self._cb(f"pay:o1001"))
        card = [p for p in self._sends(sent) if "350" in p["text"]]
        self.assertTrue(card)
        self.assertIn("СБП", card[0]["text"])
        self.assertIn("NOZZA 1001", card[0]["text"])
        notified = self._notify()
        sent.clear()
        self.bot._handle(self._cb(f"paid:o1001"))
        self.assertTrue(any("Передал мастеру" in p["text"] for p in self._sends(sent)))
        self.assertTrue(any("сообщил об оплате" in n[0] for n in notified))
        self.assertIsNotNone(self.db.one(
            "SELECT * FROM events WHERE title='Покупатель сообщил об оплате'"))

    def test_track_url_button_in_order_card(self):
        row = self._chat_row()
        self._own_order("1001")
        self.db.set_settings({"client_bot_track_url": "http://192.168.1.5:8080"})
        sent = self._wire()
        self.bot._handle(self._msg("статус 1001"))
        urls = [b.get("url") for p in self._sends(sent)
                for r in (json.loads(p["reply_markup"])["inline_keyboard"] if
                          p.get("reply_markup") else []) for b in r if b.get("url")]
        self.assertTrue(urls and urls[0].endswith("/track?number=1001"))

    def test_non_private_update_is_ignored_and_update_is_idempotent(self):
        """Группы не получают ответы, а Telegram retry не создаёт второй заказ."""
        sent = self._wire()
        group = self._msg("помощь")
        group["message"]["chat"] = {"id": -555, "type": "group"}
        self.assertTrue(self.bot._handle(group, dedupe=True))
        self.assertIsNone(self.db.one("SELECT * FROM client_chats WHERE chat_id='-555'"))
        self.assertFalse(self._sends(sent))

        self._product("a", "Адресник", 350)
        update = self._cb("buy:a")
        self.assertTrue(self.bot._handle(update, dedupe=True))
        self.assertTrue(self.bot._handle(update, dedupe=True))
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM orders")["n"], 1)
        self.assertEqual(self.db.one("SELECT state FROM client_bot_updates WHERE update_id='2'")["state"], "done")

    def test_variant_cart_and_price_are_preserved(self):
        self._product("a", "Адресник", 350)
        self.db.upsert("nom_variants", {
            "id": "v-red", "nom_id": "a", "name": "Красный",
            "color_name": "Красный", "archived": 0})
        from connector.printflow.nomenclature import Nomenclature
        Nomenclature(self.db).set_price("a", 490, variant_id="v-red")
        self.assertEqual(self.bot._catalog_rows()[0]["price"], 350)
        sent = self._wire()
        self.bot._handle(self._cb("cartv:a:v-red"))
        cart = self.db.one("SELECT * FROM client_bot_cart WHERE chat_id='555'")
        self.assertEqual(cart["variant_id"], "v-red")
        self.bot._handle(self._cb("checkout"))
        order = self.db.one("SELECT * FROM orders ORDER BY created_at DESC")
        self.assertEqual(order["client_variant_id"], "v-red")
        self.assertEqual(order["price"], 490)
        item = self.db.one("SELECT * FROM order_items WHERE order_id=?", (order["id"],))
        self.assertEqual(item["variant_id"], "v-red")
        self.assertIsNone(self.db.one("SELECT * FROM client_bot_cart WHERE chat_id='555'"))
        self.assertTrue(self._sends(sent))

    def test_quote_action_is_owner_only_and_idempotent(self):
        row = self._chat_row()
        self._own_order("1001", price=620)
        self.db.execute("UPDATE orders SET client_quote_status='requested' WHERE id='o1001'")
        other = self.db.upsert("client_chats", {
            "chat_id": "556", "name": "Мария", "created_at": "2026-08-24T10:00:00"}, key="chat_id")
        sent = self._wire()
        answer, _ = self.bot._run_callback("556", other, "quote_yes:o1001")
        self.assertIn("не ждёт", answer)
        self.assertEqual(self.db.one("SELECT client_quote_status FROM orders WHERE id='o1001'")["client_quote_status"], "requested")
        answer, _ = self.bot._run_callback("555", row, "quote_yes:o1001")
        self.assertIn("согласованные", answer)
        self.assertEqual(self.db.one("SELECT client_quote_status FROM orders WHERE id='o1001'")["client_quote_status"], "accepted")
        again, _ = self.bot._run_callback("555", row, "quote_yes:o1001")
        self.assertIn("не ждёт", again)

    def test_review_requires_actual_handoff(self):
        row = self._chat_row()
        self._own_order("1001", status="done", age="2026-08-20T10:00:00")
        sent = self._wire()
        self.bot._maybe_ask_reviews()
        self.assertFalse(self._sends(sent))
        self.db.execute("UPDATE orders SET client_delivered_at='2026-08-20T10:00:00' WHERE id='o1001'")
        self.bot._maybe_ask_reviews()
        self.assertTrue(self._sends(sent))
        sent.clear()
        self.bot._handle(self._cb("review:o1001:good"))
        self.assertEqual(self.db.one("SELECT rating FROM client_reviews WHERE order_id='o1001'")["rating"], "good")

    def test_response_templates_are_editable_in_local_db(self):
        defaults = self.bot.templates()
        self.assertTrue(defaults)
        saved = self.bot.save_template(name="Мой ответ", text="Цена — {цена}, срок — {срок}.")
        self.assertEqual(self.bot.templates()[-1]["id"], saved["id"])
        updated = self.bot.save_template(saved["id"], "Обновлённый ответ", "Готово ✓")
        self.assertEqual(updated["text"], "Готово ✓")
        self.bot.delete_template(saved["id"])
        self.assertNotIn(saved["id"], {item["id"] for item in self.bot.templates()})

    # ----------------------------------- оператор (связь) и статус предоплаты
    def test_operator_button_in_menu_and_callback(self):
        keys = self.bot._menu()
        datas = [b["callback_data"]
                 for r in keys["inline_keyboard"] for b in r]
        self.assertIn("operator", datas)
        notified = self._notify()
        text, _ = self.bot._dispatch("555", self._chat_row(), "оператор")
        self.assertIn("Передал мастерской", text)
        self.assertTrue(notified and "оператор" in notified[0][0].lower())
        notified.clear()
        sent = self._wire()
        self.bot._handle(self._cb("operator"))
        self.assertTrue(any("Передал мастерской" in p["text"]
                            for p in self._sends(sent)))
        self.assertTrue(notified and "оператор" in notified[0][0].lower())

    def test_reply_to_operator_skips_fallback(self):
        row = self._chat_row()
        notified = self._notify()
        message = self._msg("спасибо, всё получилось!")
        message["message"]["reply_to_message"] = {
            "message_id": 5, "chat": {"id": 555, "type": "private"},
            "from": {"id": 999, "is_bot": True, "first_name": "NOZZA"},
        }
        sent = self._wire()
        self.bot._handle(message)
        replies = self._sends(sent)
        self.assertTrue(replies)
        self.assertFalse(any("Не понял" in p["text"] for p in replies))
        self.assertTrue(any("Передал ваш ответ оператору" in p["text"]
                            for p in replies))
        self.assertTrue(notified
                        and "Ответ покупателя оператору" in notified[0][0])
        # текст без reply по-прежнему попадает в «не понял»
        text, _ = self.bot._dispatch("555", row, "совсем другой вопрос")
        self.assertIn("Не понял", text)

    def test_dispatch_reply_flag_skips_fallback(self):
        row = self._chat_row()
        notified = self._notify()
        text, _ = self.bot._dispatch("555", row, "да, подойдёт", reply=True)
        self.assertNotIn("Не понял", text)
        self.assertIn("Передал ваш ответ оператору", text)
        self.assertTrue(notified
                        and "Ответ покупателя оператору" in notified[0][0])

    def test_prepay_order_card_offers_payment_methods(self):
        self._own_order("1001", status="prepay", price=1500.0)
        order = self.db.one("SELECT * FROM orders WHERE number='1001'")
        keys = self.bot._order_card_keyboard(order)
        buttons = [b for r in keys["inline_keyboard"] for b in r]
        self.assertIn("pay:o1001", [b["callback_data"] for b in buttons])
        self.assertTrue(any("Способы оплаты" in b["text"] for b in buttons))
        self.assertFalse(any(b["text"] == "💳 Оплатить" for b in buttons))

    def test_pay_card_prepay_wording(self):
        self._own_order("1001", status="prepay", price=1500.0)
        self.db.set_settings({"client_bot_pay_info": "СБП +7 900 000-00-00"})
        sent = self._wire()
        self.bot._handle(self._cb("pay:o1001"))
        card = [p for p in self._sends(sent) if "Предоплата по заказу" in p["text"]]
        self.assertTrue(card)
        self.assertIn("1 500", card[0]["text"])


if __name__ == "__main__":
    unittest.main()


class ClientBot12Tests(unittest.TestCase):
    """12.0 — свой заказ под ключ, новинки, выдача, отзывы, SLA, шаблоны."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.manager = PrinterManager(self.db, Repo(self.db))
        self.bot = self.manager.client_bot
        self.row = self.db.upsert("client_chats", {
            "chat_id": "555", "name": "Иван", "username": "ivan",
            "created_at": "2026-08-24T10:00:00",
            "last_seen": "2026-08-24T12:00:00"}, key="chat_id")

    def tearDown(self):
        self.manager.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _product(self, key, name, price, created="2026-08-01T10:00:00"):
        self.db.upsert("nomenclature", {
            "id": key, "kind": "product", "name": name, "grams": 10,
            "hours": 0.5, "qty": 3, "material": "PLA",
            "created_at": created})
        from connector.printflow.nomenclature import Nomenclature
        Nomenclature(self.db).set_price(key, price)
        return self.db.one("SELECT * FROM nomenclature WHERE id=?", (key,))

    def _notify(self):
        out: list[tuple[str, bytes | None, bool]] = []
        self.manager.notify_async = (
            lambda text, photo=None, buttons=None, critical=False:
            out.append((text, photo, critical)))
        return out

    # ---------------------------------------------------- К1/К4: меню и вход
    def test_menu_has_new_buttons_and_custom_start(self):
        keys = self.bot._menu()["inline_keyboard"]
        flat = {b["callback_data"] for row in keys for b in row}
        for callback in ("custom", "new", "reviews", "pickup"):
            self.assertIn(callback, flat)
        text, buttons = self.bot._run_callback("555", self.row, "custom")
        self.assertIn("шаг 1 из 4", text.lower())
        self.assertIn("STL", text)
        flat = {b["callback_data"] for row in buttons["inline_keyboard"] for b in row}
        self.assertIn("draft_cancel", flat)
        self.assertIn("faq:materials", flat)

    def test_start_menu_is_a_fork(self):
        answer, buttons = self.bot._dispatch("555", self.row, "start")
        flat = {b["callback_data"] for row in buttons["inline_keyboard"] for b in row}
        self.assertIn("catalog", flat)
        self.assertIn("custom", flat)

    # --------------------------------------------- К2/К3: мастер с проверкой
    def test_wizard_full_flow_with_confirmation(self):
        notified = self._notify()
        answer, _ = self.bot._dispatch("555", self.row, "свой заказ")
        self.assertIn("шаг 1 из 4", answer.lower())
        answer, _ = self.bot._dispatch("555", self.row, "Держатель для кабеля")
        self.assertIn("Шаг 2 из 4", answer)
        self.assertIn("фото", answer.lower())          # К3: подсказка про файлы
        answer, _ = self.bot._dispatch("555", self.row, "120 x 40 x 20 мм")
        self.assertIn("Шаг 3 из 4", answer)
        answer, buttons = self.bot._dispatch("555", self.row, "держать провода под столом")
        self.assertIn("проверьте", answer.lower())      # К2: шаг проверки
        self.assertIn("Держатель для кабеля", answer)
        self.assertIn("120 x 40 x 20", answer)
        flat = {b["callback_data"] for row in buttons["inline_keyboard"] for b in row}
        self.assertIn("draft_send", flat)
        # случайный текст на проверке не отправляет заявку
        before = self.db.one("SELECT COUNT(*) n FROM orders")["n"]
        again, _ = self.bot._dispatch("555", self.row, "отправляй скорее")
        self.assertIn("проверьте", again.lower())
        self.assertEqual(before, self.db.one("SELECT COUNT(*) n FROM orders")["n"])
        # отправка кнопкой
        text, _ = self.bot._run_callback("555", self.row, "draft_send")
        self.assertIn("✓", text)
        order = self.db.one("SELECT * FROM orders ORDER BY created_at DESC")
        self.assertIn("Держатель", order["product"])
        self.assertIn("Размеры: 120", order["notes"])
        self.assertIn("Назначение: держать", order["notes"])
        self.assertIsNone(self.db.one(
            "SELECT * FROM client_bot_drafts WHERE chat_id='555'"))
        self.assertTrue(any("Индивидуальная заявка" in n[0] for n in notified))
        # повторная отправка — заявки больше нет
        text, _ = self.bot._run_callback("555", self.row, "draft_send")
        self.assertIn("уже отправлена", text)

    def test_wizard_edit_and_cancel(self):
        self.bot._dispatch("555", self.row, "свой заказ")
        self.bot._dispatch("555", self.row, "первое описание")
        self.bot._dispatch("555", self.row, "размеры")
        self.bot._dispatch("555", self.row, "назначение")
        text, buttons = self.bot._run_callback("555", self.row,
                                               "draft_edit:description")
        self.assertIn("Шаг 1 из 4", text)
        answer, _ = self.bot._dispatch("555", self.row, "исправленное описание")
        self.assertIn("Шаг 2 из 4", answer)
        text, _ = self.bot._run_callback("555", self.row, "draft_cancel")
        self.assertIn("отменена", text)
        self.assertIsNone(self.db.one(
            "SELECT * FROM client_bot_drafts WHERE chat_id='555'"))
        # после отмены обычный текст снова «не понял», а не шаг мастера
        answer, _ = self.bot._dispatch("555", self.row, "привет")
        self.assertIn("Не понял", answer)

    def test_wizard_skip_step(self):
        self.bot._dispatch("555", self.row, "свой заказ")
        self.bot._dispatch("555", self.row, "подставка")
        text, _ = self.bot._run_callback("555", self.row, "draft_skip")
        self.assertIn("Шаг 3 из 4", text)
        self.bot._dispatch("555", self.row, "для телефона")
        text, _ = self.bot._run_callback("555", self.row, "draft_send")
        self.assertIn("✓", text)
        order = self.db.one("SELECT * FROM orders ORDER BY created_at DESC")
        self.assertNotIn("Размеры", order["notes"])
        self.assertIn("Назначение: для телефона", order["notes"])

    def test_wizard_keeps_draft_on_faq_materials(self):
        """К16: статья про материалы не сбрасывает шаг мастера."""
        self.bot._dispatch("555", self.row, "свой заказ")
        self.bot._dispatch("555", self.row, "кронштейн")
        text, buttons = self.bot._run_callback("555", self.row, "faq:materials")
        self.assertIn("PETG", text)
        flat = {b["callback_data"] for row in buttons["inline_keyboard"] for b in row}
        self.assertIn("draft_skip", flat)
        draft = self.db.one("SELECT * FROM client_bot_drafts WHERE chat_id='555'")
        self.assertEqual(draft["step"], "dimensions")
        self.assertIn("кронштейн", draft["data"])

    def test_custom_direct_text_still_instant(self):
        answer, _ = self.bot._dispatch("555", self.row,
                                       "индивидуальный держатель 120 мм")
        self.assertIn("✓", answer)
        self.assertIsNone(self.db.one(
            "SELECT * FROM client_bot_drafts WHERE chat_id='555'"))

    # ---------------------------------------------------------- К10: новинки
    def test_new_arrivals(self):
        self._product("old", "Старая вещь", 100, created="2026-07-01T10:00:00")
        self._product("new1", "Свежая вещь", 200, created="2026-08-28T10:00:00")
        self._product("new2", "Совсем новая", 300, created="2026-08-29T10:00:00")
        text = self.bot.text_new()
        pos_new1 = text.index("Свежая вещь")
        pos_new2 = text.index("Совсем новая")
        self.assertLess(pos_new2, pos_new1)          # новые выше
        self.assertLess(text.index("Свежая вещь"), text.index("Старая вещь"))
        answer, _ = self.bot._dispatch("555", self.row, "новинки")
        self.assertIn("Свежая вещь", answer)

    # ---------------------------------------------------- К12/К13: получение
    def test_pickup_info(self):
        self.db.set_settings({"client_bot_pickup_info":
                              "Адрес: Ленина 1\nЧасы: 10:00–20:00"})
        text = self.bot.text_pickup()
        self.assertIn("Ленина 1", text)
        self.assertIn("3 дн", text)                   # pickup_days по умолчанию
        answer, _ = self.bot._dispatch("555", self.row, "как получить")
        self.assertIn("Ленина 1", answer)
        # адрес попадает и в напоминание о выдаче (К13)
        self.db.execute(
            "INSERT INTO orders(id,number,product,status,updated_at,created_at)"
            " VALUES('o1','1001','крючок','ready','2026-08-20T10:00:00',"
            "'2026-08-01T10:00:00')")
        self.db.execute(
            "INSERT INTO client_orders(chat_id,order_id,number,created_at)"
            " VALUES('555','o1','1001','2026-08-01T10:00:00')")
        sent = self._wire_sends()
        self.bot._maybe_remind_pickup()
        self.assertTrue(any("Ленина 1" in t for t in sent))

    def _wire_sends(self):
        sent: list[str] = []
        self.bot._reply_keyed = (
            lambda chat, text, buttons=None, dedupe_key="": sent.append(text))
        return sent

    # ------------------------------------------------------- К7: фото готово
    def test_ready_status_sends_photo(self):
        import connector.printflow.config as pf_config
        photos = pathlib.Path(self._tmp.name) / "photos"
        photos.mkdir(exist_ok=True)
        old = pf_config.PHOTO_DIR
        pf_config.PHOTO_DIR = photos
        self.addCleanup(setattr, pf_config, "PHOTO_DIR", old)
        (photos / "ready.jpg").write_bytes(b"jpegdata")
        self.db.execute(
            "INSERT INTO orders(id,number,product,status,updated_at,created_at)"
            " VALUES('o1','1001','ваза','ready','2026-08-24T11:00:00',"
            "'2026-08-24T10:00:00')")
        self.db.execute(
            "INSERT INTO client_orders(chat_id,order_id,number,"
            "last_notified_status,created_at)"
            " VALUES('555','o1','1001','printing','2026-08-24T10:00:00')")
        self.db.execute(
            "INSERT INTO order_photos(id,order_id,at,file,note,kind)"
            " VALUES('p1','o1','2026-08-24T11:30:00','ready.jpg','','camera')")
        sent = self._wire_sends()
        photos_sent: list[str] = []
        self.bot._send_photo = (
            lambda chat, caption, raw, buttons=None, dedupe_key="":
            photos_sent.append(caption))
        self.bot._maybe_push_statuses()
        self.assertTrue(any("Готов" in t for t in sent))
        self.assertEqual(photos_sent, ["📸 Заказ №1001 «ваза» готов — "
                                       "так он выглядит перед выдачей."])

    def test_ready_photo_respects_setting(self):
        self.db.set_settings({"client_bot_ready_photo": False})
        photos_sent: list[str] = []
        self.bot._send_photo = (
            lambda chat, caption, raw, buttons=None, dedupe_key="":
            photos_sent.append(caption))
        self.bot._send_ready_photo("555", {"id": "o1", "number": "1001",
                                           "product": "ваза"})
        self.assertEqual(photos_sent, [])

    # ------------------------------------------------------ К14: проблема
    def test_problem_button_waits_description(self):
        notified = self._notify()
        self.db.execute(
            "INSERT INTO orders(id,number,product,status,created_at,updated_at)"
            " VALUES('o1','1001','крючок','printing','2026-08-24T10:00:00',"
            "'2026-08-24T10:00:00')")
        self.db.execute(
            "INSERT INTO client_orders(chat_id,order_id,number,created_at)"
            " VALUES('555','o1','1001','2026-08-24T10:00:00')")
        order = self.db.one("SELECT * FROM orders WHERE id='o1'")
        keys = self.bot._order_card_keyboard(order)["inline_keyboard"]
        flat = [b["callback_data"] for row in keys for b in row]
        self.assertIn("problem:o1", flat)
        text, _ = self.bot._run_callback("555", self.row, "problem:o1")
        self.assertIn("Опишите", text)
        self.assertTrue(any("проблему" in n[0] for n in notified))
        # следующее сообщение уходит мастеру как детализация
        answer, _ = self.bot._dispatch("555", self.row, "расслоение на углу")
        self.assertIn("Спасибо за подробности", answer)

    # ------------------------------------------------------ К15: отзывы
    def test_reviews_text(self):
        self.assertEqual("Отзывов пока нет" in self.bot.text_reviews(), True)
        self.db.execute(
            "INSERT INTO orders(id,number,product,created_at)"
            " VALUES('o1','1001','крючок','2026-08-20T10:00:00')")
        self.db.execute(
            "INSERT INTO client_reviews(order_id,chat_id,rating,comment,state,"
            " asked_at,created_at) VALUES('o1','555','good','Быстро и аккуратно',"
            "'rated','2026-08-22T10:00:00','2026-08-22T10:05:00')")
        text = self.bot.text_reviews()
        self.assertIn("👍 1 · 👎 0", text)
        self.assertIn("Быстро и аккуратно", text)

    # ------------------------------------------------------- К17: SLA
    def test_operator_sla_minutes(self):
        base = "2026-08-25T"
        for i, (minutes, kind) in enumerate(
                [(0, "in"), (30, "answer"), (0, "in"), (30, "answer"),
                 (0, "in"), (30, "answer")]):
            hour, minute = divmod(10 * 60 + minutes, 60)
            at = f"{base}{hour:02d}:{minute:02d}:00"
            self.db.execute(
                "INSERT INTO client_bot_log(at,chat_id,text,kind,direction)"
                " VALUES(?,'555',?,? ,?)", (at, f"m{i}", kind,
                                            "in" if kind == "in" else "out"))
        self.assertEqual(self.bot.operator_sla_minutes(), 30.0)
        # и в ответе оператора есть честное ожидание
        notified = self._notify()
        answer, _ = self.bot._dispatch("555", self.row, "оператор")
        self.assertIn("~30 мин", answer)
        self.assertTrue(notified)

    # ------------------------------------------------------- К9: лояльность
    def _finished_order(self, i):
        order = self.db.upsert("orders", {
            "id": f"done{i}", "number": f"20{i:02d}", "customer_name": "Иван",
            "product": f"вещь {i}", "price": 500, "qty": 1, "status": "done",
            "created_at": "2026-08-20T10:00:00",
            "updated_at": "2026-08-20T10:00:00"})
        self.bot._link_order("555", self.row, order, source="telegram")
        return order

    def test_loyalty_counter_and_discount(self):
        for i in range(4):
            self._finished_order(i)
        text = self.bot.text_my_orders("555", self.row)
        self.assertIn("следующий со скидкой 10%", text)
        # корзина: 5-й заказ со скидкой
        self._product("a", "Держатель", 1000)
        self.bot._add_to_cart("555", "a")
        answer = self.bot._checkout_cart_impl("555", self.row)
        self.assertIn("скидка 10%", answer)
        order = self.db.one("SELECT * FROM orders ORDER BY created_at DESC")
        self.assertEqual(order["price"], 1000)
        self.assertEqual(order["discount"], 100)

    def test_no_discount_before_fifth_order(self):
        for i in range(3):
            self._finished_order(i)
        self._product("a", "Держатель", 1000)
        self.bot._add_to_cart("555", "a")
        self.bot._checkout_cart_impl("555", self.row)
        order = self.db.one("SELECT * FROM orders ORDER BY created_at DESC")
        self.assertEqual(order["discount"], 0)

    def test_loyalty_hint_in_custom_order(self):
        for i in range(4):
            self._finished_order(i)
        answer = self.bot._create_custom_order("555", self.row, "стойка")
        self.assertIn("скидка 10%", answer)
        order = self.db.one("SELECT * FROM orders ORDER BY created_at DESC")
        self.assertIn("Лояльность", order["notes"])

    # -------------------------------------------------- К18: шаблоны ответов
    def test_default_templates_library(self):
        defaults = self.bot.default_templates()
        self.assertGreaterEqual(len(defaults), 10)
        names = {item["name"] for item in defaults}
        self.assertIn("Цена посчитана", names)
        self.assertIn("Готов к выдаче", names)
        # готовый шаблон сохраняется как обычный — панель добавляет кнопкой
        first = defaults[0]
        saved = self.bot.save_template(name=first["name"], text=first["text"])
        self.assertIn(saved["id"], {item["id"] for item in self.bot.templates()})

    def test_stats_has_sla(self):
        self.assertIn("sla_minutes", self.bot.stats())


class ClientBot121Tests(unittest.TestCase):
    """12.1 — управление заказом из чата, блокировки, кнопки мастеру."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.manager = PrinterManager(self.db, Repo(self.db))
        self.bot = self.manager.client_bot
        self.row = self.db.upsert("client_chats", {
            "chat_id": "555", "name": "Иван", "username": "ivan",
            "created_at": "2026-08-24T10:00:00",
            "last_seen": "2026-08-24T12:00:00"}, key="chat_id")

    def tearDown(self):
        self.manager.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _notify(self):
        out: list[tuple[str, bytes | None, list, bool]] = []
        self.manager.notify_async = (
            lambda text, photo=None, buttons=None, critical=False:
            out.append((text, photo, buttons or [], critical)))
        return out

    def _own_order(self, number="1001", status="new"):
        self.db.execute(
            "INSERT INTO orders(id,number,product,status,price,created_at,"
            "updated_at) VALUES(?,?, 'крючок', ?, 500, '2026-08-24T10:00:00',"
            "'2026-08-24T10:00:00')", (f"o{number}", number, status))
        self.db.execute(
            "INSERT INTO client_orders(chat_id,order_id,number,created_at)"
            " VALUES('555',?,?,'2026-08-24T10:00:00')", (f"o{number}", number))
        return self.db.one("SELECT * FROM orders WHERE id=?", (f"o{number}",))

    def _wire(self):
        sent: list[str] = []
        self.bot._reply = lambda chat, text, buttons=None: sent.append(text)
        self.bot._reply_keyed = (
            lambda chat, text, buttons=None, dedupe_key="": sent.append(text))
        return sent

    # --------------------------------------------------------- КБ7: дополнить
    def test_supplement_flow(self):
        notified = self._notify()
        order = self._own_order("1001", status="estimate")
        keys = self.bot._order_card_keyboard(order)["inline_keyboard"]
        flat = [b["callback_data"] for row in keys for b in row]
        self.assertIn("supplement:o1001", flat)
        text, _ = self.bot._run_callback("555", self.row, "supplement:o1001")
        self.assertIn("что добавить", text)
        answer, _ = self.bot._dispatch("555", self.row, "материал PETG, чёрный")
        self.assertIn("Передал мастеру", answer)
        order = self.db.one("SELECT * FROM orders WHERE id='o1001'")
        self.assertIn("Дополнение покупателя: материал PETG", order["notes"])
        self.assertTrue(any("Дополнение" in n[0] for n in notified))
        # ожидание снято: следующее сообщение — обычный разбор
        answer, _ = self.bot._dispatch("555", self.row, "спасибо большое")
        self.assertIn("Не понял", answer)

    def test_supplement_final_order_refused(self):
        self._own_order("1001", status="done")
        text, _ = self.bot._run_callback("555", self.row, "supplement:o1001")
        self.assertIn("не дополнить", text)

    # ---------------------------------------------------------- КБ8: отмена
    def test_cancel_request_flow(self):
        notified = self._notify()
        self._own_order("1001", status="new")
        keys = self.bot._order_card_keyboard(
            self.db.one("SELECT * FROM orders WHERE id='o1001'"))["inline_keyboard"]
        flat = [b["callback_data"] for row in keys for b in row]
        self.assertIn("cancelreq:o1001", flat)
        text, _ = self.bot._run_callback("555", self.row, "cancelreq:o1001")
        self.assertIn("Передал мастеру", text)
        order = self.db.one("SELECT * FROM orders WHERE id='o1001'")
        self.assertTrue(order["cancel_requested_at"])
        self.assertIn("запросил отмену", order["notes"])
        self.assertTrue(any("просит отменить" in n[0] for n in notified))
        self.assertTrue(any(n[3] for n in notified))  # critical=True
        # повтор — идемпотентно
        text, _ = self.bot._run_callback("555", self.row, "cancelreq:o1001")
        self.assertIn("уже передан", text)

    def test_cancel_refused_when_printing(self):
        self._own_order("1001", status="printing")
        text, _ = self.bot._run_callback("555", self.row, "cancelreq:o1001")
        self.assertIn("уже в работе", text)
        order = self.db.one("SELECT * FROM orders WHERE id='o1001'")
        self.assertFalse(order["cancel_requested_at"])

    # ------------------------------------------------------ КБ11: перенос
    def test_pickup_later_flow(self):
        notified = self._notify()
        self._own_order("1001", status="ready")
        keys = self.bot._order_card_keyboard(
            self.db.one("SELECT * FROM orders WHERE id='o1001'"))["inline_keyboard"]
        flat = [b["callback_data"] for row in keys for b in row]
        self.assertIn("pickuplater:o1001", flat)
        text, _ = self.bot._run_callback("555", self.row, "pickuplater:o1001")
        self.assertIn("когда удобно", text)
        answer, _ = self.bot._dispatch("555", self.row, "смогу в пятницу после 18")
        self.assertIn("Передал мастеру", answer)
        order = self.db.one("SELECT * FROM orders WHERE id='o1001'")
        self.assertIn("Перенос выдачи: смогу в пятницу", order["notes"])
        self.assertTrue(any("переносит выдачу" in n[0] for n in notified))

    def test_pickup_later_only_when_ready(self):
        self._own_order("1001", status="printing")
        text, _ = self.bot._run_callback("555", self.row, "pickuplater:o1001")
        self.assertIn("статусе «Готов»", text)

    # ------------------------------------------------------- КБ6: блокировка
    def test_banned_chat_is_silently_ignored(self):
        self._own_order("1001", status="new")
        self.db.execute("UPDATE client_chats SET banned=1 WHERE chat_id='555'")
        sent = self._wire()
        update = {"update_id": 9, "message": {
            "chat": {"id": 555, "type": "private"}, "message_id": 1,
            "from": {"id": 777, "first_name": "Иван"},
            "text": "каталог"}}
        handled = self.bot._handle(update, dedupe=False)
        self.assertTrue(handled)
        self.assertEqual(sent, [])
        # статусы заблокированному тоже не уходят
        self.db.execute(
            "UPDATE client_orders SET last_notified_status='printing'"
            " WHERE order_id='o1001'")
        self.db.execute("UPDATE orders SET status='ready' WHERE id='o1001'")
        self.bot._maybe_push_statuses()
        self.assertEqual(sent, [])

    # -------------------------------------------------------- КБ2: кнопки
    def test_master_reply_buttons_from_own_templates(self):
        # в настройках уже есть стартовые шаблоны — кнопки берутся из них
        buttons = self.bot.master_reply_buttons("555")
        self.assertEqual(len(buttons), 2)
        for label, data in buttons:
            self.assertTrue(data.startswith("cbot_tpl:555:"))
        self.assertTrue(any(d.endswith(":tpl_quote") for _l, d in buttons))

    def test_master_reply_buttons_fall_back_to_library(self):
        # свои шаблоны удалены — кнопки из встроенной библиотеки (КБ2/К18)
        self.db.set_settings({"client_bot_templates": []})
        buttons = self.bot.master_reply_buttons("555")
        self.assertEqual(len(buttons), 3)
        self.assertTrue(any(d.endswith(":tpl_price_ready") for _l, d in buttons))

    def test_master_reply_buttons_prefer_own_templates(self):
        self.bot.save_template(name="Свой ответ", text="Готово")
        labels = [label for label, _d in self.bot.master_reply_buttons("555")]
        self.assertIn("Свой ответ", labels)

    def test_alert_master_carries_buttons(self):
        captured = self._notify()
        self.bot._alert_master("555", self.row, "сколько стоит доставка?")
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0][2])  # buttons не пустые


class ClientBot122PanelTests(unittest.TestCase):
    """12.2 (ЗА3–ЗА6): контур Telegram у карточки заказа и файлы заявки.

    Эндпоинты проверяются напрямую на Api без HTTP-сервера: order-thread —
    только чтение, cancel-ack — снятие отметки и перенос в статус отмены,
    to-uploads — перекладывание файла покупателя в папку загрузок.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(self._tmp.name)
        self.photo_dir = tmp / "photos"
        self.upload_dir = tmp / "uploads"
        self.photo_dir.mkdir()
        self.db = Database(tmp / "t.sqlite3")
        self.manager = PrinterManager(self.db, Repo(self.db))
        self.repo = self.manager.repo
        self.bot = self.manager.client_bot

        from connector.printflow import api as api_mod
        from connector.printflow import config as cfg_mod
        self._patches = [
            patch.object(cfg_mod, "UPLOAD_DIR", self.upload_dir),
            patch.object(cfg_mod, "PHOTO_DIR", self.photo_dir),
        ]
        for p in self._patches:
            p.start()

        self.api = api_mod.Api.__new__(api_mod.Api)
        self.api.db = self.db
        self.api.repo = self.repo
        self.api.manager = self.manager
        self.order = self.repo.save_order({
            "product": "Адресник", "customer_name": "Иван", "qty": 1,
            "channel": "telegram", "client_source": "custom",
        })
        self.oid = self.order["id"]

    def tearDown(self):
        for _ in self._patches:
            patch.stopall()
        self.manager.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _link_chat(self, chat: str = "555"):
        self.db.upsert("client_chats", {
            "chat_id": chat, "name": "Иван", "username": "ivan",
            "source": "custom", "created_at": "2026-08-30T10:00:00",
        }, key="chat_id")
        self.db.execute(
            "INSERT INTO client_orders(chat_id,order_id,number,source,created_at)"
            " VALUES(?,?,?,?,?)",
            (chat, self.oid, self.order.get("number"), "telegram", "2026-08-30T10:00:00"))

    def test_order_thread_without_chat_link(self):
        code, payload = self.api.get("/api/client-bot/order-thread", {"order_id": [self.oid]})
        self.assertEqual(code, 200)
        self.assertEqual(payload["chat_id"], "")
        self.assertEqual(payload["messages"], [])
        self.assertIsNone(payload["payment_intent"])

    def test_order_thread_unknown_order(self):
        code, payload = self.api.get("/api/client-bot/order-thread", {"order_id": ["нет"]})
        self.assertEqual(code, 404)

    def test_order_thread_carries_chat_messages_actions(self):
        self._link_chat()
        self.db.execute(
            "INSERT INTO client_bot_log(at,chat_id,name,text,kind,direction)"
            " VALUES(?,?,?,?,?,?)",
            ("2026-08-30T10:01:00", "555", "Иван", "а когда будет готово?", "message", "in"))
        self.bot.save_template(name="Срок", text="Готово завтра")
        self.db.upsert("client_payment_intents", {
            "id": "pi_1", "order_id": self.oid, "chat_id": "555",
            "request_id": "req-1", "amount": 700.0, "purpose": "предоплата",
            "status": "pending", "created_at": "2026-08-30T10:02:00",
        })
        code, payload = self.api.get("/api/client-bot/order-thread", {"order_id": [self.oid]})
        self.assertEqual(code, 200)
        self.assertEqual(payload["chat_id"], "555")
        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["direction"], "in")
        self.assertEqual(payload["payment_intent"]["id"], "pi_1")
        self.assertIn("Срок", [t["name"] for t in payload["templates"]])
        self.assertEqual(payload["order"]["client_source"], "custom")

    def test_cancel_ack_keep_clears_flag_and_keeps_order(self):
        self.db.execute("UPDATE orders SET cancel_requested_at=?, notes='заметка'"
                        " WHERE id=?", ("2026-08-30T10:05:00", self.oid))
        code, payload = self.api.post("/api/client-bot/cancel-ack",
                                      {"order_id": self.oid, "action": "keep"}, {})
        self.assertEqual(code, 200)
        fresh = self.db.one("SELECT * FROM orders WHERE id=?", (self.oid,))
        self.assertFalse(fresh["cancel_requested_at"])
        self.assertIn("мастер оставил заказ в работе", fresh["notes"])
        self.assertIn("заметка", fresh["notes"])
        self.assertEqual(fresh["status"], self.order["status"])

    def test_cancel_ack_canceled_moves_to_final_cancel_status(self):
        self.db.execute(
            "INSERT INTO statuses(id,name,color,position,is_final)"
            " VALUES('canceled','Отменён','#64748b',90,1)")
        self.db.execute("UPDATE orders SET cancel_requested_at=? WHERE id=?",
                        ("2026-08-30T10:05:00", self.oid))
        code, payload = self.api.post("/api/client-bot/cancel-ack",
                                      {"order_id": self.oid, "action": "canceled"}, {})
        self.assertEqual(code, 200)
        fresh = self.db.one("SELECT * FROM orders WHERE id=?", (self.oid,))
        self.assertEqual(fresh["status"], "canceled")
        self.assertFalse(fresh["cancel_requested_at"])

    def test_cancel_ack_requires_known_action(self):
        with self.assertRaises(ValueError):
            self.api.post("/api/client-bot/cancel-ack",
                          {"order_id": self.oid, "action": "maybe"}, {})

    def test_photos_endpoint_exposes_size_and_original_name(self):
        self.bot._attach_file(self.oid, b"solid-data", "leftBracket.3mf", "555", lead=True)
        code, payload = self.api.get("/api/order/photos", {"order_id": [self.oid]})
        self.assertEqual(code, 200)
        photos = payload["photos"]
        self.assertEqual(len(photos), 1)
        self.assertEqual(photos[0]["original_name"], "leftBracket.3mf")
        self.assertEqual(photos[0]["size"], len(b"solid-data"))
        self.assertEqual(photos[0]["kind"], "client_file")

    def test_to_uploads_copies_client_file_and_returns_name(self):
        self.bot._attach_file(self.oid, b"solid-data", "pet-holder.stl", "555", lead=True)
        row = self.db.one("SELECT * FROM order_photos WHERE order_id=?", (self.oid,))
        code, payload = self.api.post("/api/order/photo/to-uploads", {"id": row["id"]}, {})
        self.assertEqual(code, 200)
        self.assertEqual(payload["file"], "pet-holder.stl")
        self.assertTrue((self.upload_dir / "pet-holder.stl").is_file())
        self.assertEqual((self.upload_dir / "pet-holder.stl").read_bytes(), b"solid-data")

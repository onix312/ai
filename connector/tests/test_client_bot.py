"""Клиентский бот: каталог, заказ по номеру, статусы, привязка телефона.

Проверяется логика без сети: токен не задан, Telegram не вызывается.
"""
from __future__ import annotations

import json
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
        return [p for m, p in sent if m == "sendMessage"]

    @staticmethod
    def _msg(text=None, **extra):
        message = {"chat": {"id": 555}, "message_id": 1,
                   "from": {"first_name": "Иван", "username": "ivan"}}
        if text is not None:
            message["text"] = text
        message.update(extra)
        return {"update_id": 1, "message": message}

    @staticmethod
    def _cb(data):
        return {"update_id": 2, "callback_query": {
            "id": "cq1", "data": data,
            "message": {"chat": {"id": 555}, "message_id": 9},
            "from": {"first_name": "Иван", "username": "ivan"}}}

    def test_callback_buttons_reply(self):
        """Баг 9.3: кнопка отвечала только записью в журнал — покупатель
        не видел реакции. Теперь каждая кнопка шлёт сообщение."""
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
                     "first_name": "Иван"}))
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


if __name__ == "__main__":
    unittest.main()

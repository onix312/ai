"""Бот отвечает текстом и картинками, а не только кнопкой Mini App (18.12.3).

Просьба владельца: «сделай пока вывод не только miniapp но и текстом с
картинками». С 19.0 web_app-кнопок нет вовсе: все отчёты живут в чате
его нет, цех в чате был недоступен. Здесь закреплено новое поведение:

* у слов появились свои ответы: «статус», «принтеры», «заказы [номер]»,
  «полка», «очередь», «кадр», «деньги» — текстом, с кадром камеры или фото
  изделия, когда есть что показать;
* кнопки меню зовут те же команды, поэтому текстовый режим работает и без
  настройки Mini App;
* цифры берутся из настоящих таблиц: деньги — из `transactions` (в базе нет
  `money_log`), полка — через `nom_groups` (таблицы `categories` не существует);
  иначе отчёт показывал бы нули и пустую полку;
* повторная команда отвечает снова: ключ дедупликации outbox больше не строится
  из текста сообщения.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.config import now_iso  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.staff import Staff  # noqa: E402
from connector.printflow.staffbot import StaffBot  # noqa: E402
from connector.printflow.staffbot import report  # noqa: E402
from connector.printflow.staffbot import ui  # noqa: E402
from connector.printflow.staffbot.router import CALLBACKS, ROUTER, normalize  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"printflow-frame" * 8


def flat_buttons(markup: dict) -> list[dict]:
    return [b for row in ((markup or {}).get("inline_keyboard") or []) for b in row]


class TextTests(unittest.TestCase):
    """Формулировки: цифры по-русски, свежие остатки вперёд, лимиты Telegram."""

    def test_money_format(self):
        self.assertEqual("1 250 ₽", report.money(1250))
        self.assertEqual("0 ₽", report.money(0))
        self.assertEqual("99,50 ₽", report.money(99.5))

    def test_plural(self):
        self.assertEqual("1 задание", report.plural(1, "задание", "задания", "заданий"))
        self.assertEqual("2 задания", report.plural(2, "задание", "задания", "заданий"))
        self.assertEqual("5 заданий", report.plural(5, "задание", "задания", "заданий"))
        self.assertEqual("11 заданий", report.plural(11, "задание", "задания", "заданий"))
        self.assertEqual("21 задание", report.plural(21, "задание", "задания", "заданий"))

    def test_status_text_counts_and_printers(self):
        printers = report.printers_from_snapshot({"printers": [
            {"id": "p1", "name": "P1S", "connection": {"connected": True},
             "printer": {"state": "RUNNING", "progress": 42, "remaining_min": 35,
                         "task": "Деталь.gcode"},
             "job": {"order": {"number": "1043"}}},
            {"id": "p2", "name": "X1C", "connection": {"connected": False},
             "printer": {"state": "OFFLINE"}},
        ]})
        text = report.status_text(
            {"total": 2, "online": 1, "printing": 1, "queue": 2, "orders_ready": 3,
             "inbox": 1, "low_stock": 1, "today_money": 1250},
            printers)
        self.assertIn("В сети 1 из 2", text)
        self.assertIn("печатает 1 станок", text)
        self.assertIn("Очередь печати: 2 задания", text)
        self.assertIn("Готово к выдаче: 3 заказа", text)
        self.assertIn("1 250 ₽", text)
        self.assertIn("P1S — печатает · 42% · Деталь.gcode · заказ №1043 · ~35 мин", text)
        self.assertIn("X1C — не в сети", text)

    def test_printer_line_shows_temperatures(self):
        printers = report.printers_from_snapshot({"printers": [
            {"id": "p1", "name": "P1S", "connection": {"connected": True},
             "printer": {"state": "IDLE"},
             "temperature": {"nozzle": 24.4, "bed": 60.0}},
        ]})
        self.assertIn("🌡 24/60 °C", report.printer_line(printers[0]))

    def test_status_without_printers_is_honest(self):
        text = report.status_text({"total": 0}, [])
        self.assertIn("Принтеров нет", text)

    def test_orders_text_and_card(self):
        order = {"id": "o1", "number": "1043", "product": "Деталь", "status": "ready",
                 "customer_name": "Иван", "price": 1200}
        text = report.orders_text([order])
        self.assertIn("1 заказ", text)
        self.assertIn("№1043 · Деталь · готов к выдаче · Иван · 1 200 ₽", text)
        card = report.order_card(order)
        self.assertIn("Заказ №1043", card)
        self.assertIn("Статус: готов к выдаче", card)
        self.assertEqual("📦 Заказы: пока ничего нет.", report.orders_text([]))

    def test_shelf_low_items_come_first(self):
        text = report.shelf_text([
            {"name": "Гайка", "qty": 20, "min_qty": 2, "price": 50},
            {"name": "Крепление", "qty": 1, "min_qty": 2, "category_name": "Крепёж"},
        ])
        lines = text.splitlines()
        self.assertIn("мало — 1", lines[0])
        self.assertIn("Крепление", lines[1])
        self.assertIn("мало", lines[1])
        self.assertIn("Гайка", lines[2])

    def test_queue_text(self):
        self.assertEqual("🧾 Очередь печати пуста — принтеры свободны.", report.queue_text([]))
        text = report.queue_text([
            {"name": "Деталь.gcode", "state": "running", "est_minutes": 35,
             "order": {"number": "1043"}},
            {"name": "Вторая.gcode", "state": "queued"},
        ])
        self.assertIn("2 задания", text)
        self.assertIn("1. Деталь.gcode — печатается · заказ №1043 · ~35 мин", text)
        self.assertIn("2. Вторая.gcode — в очереди", text)

    def test_money_text(self):
        text = report.money_text({"income": 1250, "expense": -300}, {"income": 9800})
        self.assertIn("приход 1 250 ₽", text)
        self.assertIn("расход 300 ₽", text)
        self.assertIn("итог +950 ₽", text)
        self.assertIn("За 7 дней: приход 9 800 ₽", text)

    def test_camera_absent_is_explained(self):
        text = report.camera_absent_text([{"name": "P1S"}])
        self.assertIn("Кадр недоступен", text)
        self.assertIn("P1S", text)
        self.assertIn("панели", report.camera_absent_text([{"name": "P1S"}]))

    def test_limit_keeps_telegram_limits(self):
        self.assertEqual(3800, len(report.limit("я" * 9000, 3800)))
        self.assertTrue(report.limit("я" * 9000, 3800).endswith("…"))


class RouterTests(unittest.TestCase):
    """Слова и кнопки ведут в новые текстовые ответы."""

    CASES = {
        "статус": "cmd_status",
        "сводка": "cmd_status",
        "как дела": "cmd_status",
        "принтеры": "cmd_printers",
        "датчики": "cmd_printers",
        "доктор": "cmd_status",
        "очередь": "cmd_queue",
        "заказы": "cmd_orders",
        "заказ 1043": "cmd_orders",
        "полка": "cmd_shelf",
        "стеллаж": "cmd_shelf",
        "деньги": "cmd_money",
        "итоги": "cmd_money",
        "кадр": "cmd_frame",
        "камера": "cmd_frame",
        # витринные слова по-прежнему открывают меню с Mini App
        "продажа": "cmd_menu",
        "меню": "cmd_menu",
    }

    def test_words_route_to_text_reports(self):
        for word, method in self.CASES.items():
            with self.subTest(word=word):
                route = ROUTER.match_text(normalize(word))
                self.assertIsNotNone(route, word)
                self.assertEqual(method, route.method, word)

    def test_reply_keyboard_emojis_become_commands(self):
        self.assertEqual("полка", normalize("📦 Полка"))
        self.assertEqual("деньги", normalize("💰 Касса"))
        self.assertEqual("кадр", normalize("📷 Кадр"))
        self.assertEqual("меню", normalize("🛒 Продать"))

    def test_callbacks_have_methods(self):
        methods = {route.prefix: route.method for route in CALLBACKS}
        for prefix in ("status", "printers", "queue", "orders", "shelf", "frame", "money"):
            with self.subTest(prefix=prefix):
                self.assertIn(prefix, methods)
                self.assertTrue(hasattr(StaffBot, methods[prefix]),
                                f"нет метода {methods[prefix]}")

    def test_keyboard_shows_text_reports_without_web_app(self):
        without = ui.main_menu_keyboard("owner")
        texts = [b["text"] for b in flat_buttons(without)]
        self.assertIn("📊 Статус", texts)
        self.assertIn("🛒 Полка", texts)
        self.assertIn("💰 Деньги", texts)
        self.assertIn("🤖 Ассистент", texts)
        self.assertFalse([b for b in flat_buttons(without) if "web_app" in b])
        with_url = ui.main_menu_keyboard("employee")
        # адрес-аргумент игнорируется: web_app-кнопок нет ни для одной роли
        self.assertFalse([b for b in flat_buttons(with_url) if "web_app" in b])
        self.assertNotIn("Деньги", [b["text"] for b in flat_buttons(with_url)])
        self.assertNotIn("Ассистент", [b["text"] for b in flat_buttons(with_url)])


class FakeCamera:
    def __init__(self, frame=None):
        self.frame = frame


class FakePrinter:
    def __init__(self, pid="p1", name="P1S", state="RUNNING", frame=None,
                 progress=42.0, remaining=35.0, connected=True):
        self.id = pid
        self.record = {"name": name}
        self.state = state
        self.connection = {"connected": connected}
        self.camera = FakeCamera(frame)
        self.progress = progress
        self.remaining = remaining


class FakeManager:
    """Мини-менеджер: снимок, очередь и живые принтеры с кадром камеры."""

    def __init__(self, db, printers=None, queue=None):
        self.db = db
        self.printers = {p.id: p for p in (printers or [])}
        self._queue = list(queue or [])
        self.notified = []

    def snapshot(self, printer_id: str = "") -> dict:
        rows = []
        for printer in self.printers.values():
            rows.append({
                "id": printer.id,
                "name": printer.record.get("name"),
                "connection": {"connected": bool(printer.connection["connected"])},
                "printer": {"state": printer.state, "state_label": "", "progress": printer.progress,
                            "remaining_min": printer.remaining, "task": "Деталь.gcode"},
                "job": {"order": {"number": "1043"}},
            })
        return {"printers": rows}

    def queue(self):
        return list(self._queue)

    def notify_async(self, text, photo=None, buttons=None, critical=False, event=""):
        self.notified.append((text, photo, buttons, event))


class BotReportTests(unittest.TestCase):
    """Команды бота: текст, фото, роли и цифры из настоящих таблиц."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.photos = self.tmp / "photos"
        self.photos.mkdir()
        self._photo_patch = mock.patch("connector.printflow.config.PHOTO_DIR", self.photos)
        self._photo_patch.start()
        self.db = Database(self.tmp / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111", "telegram_bot": "1",
                              "telegram_token": "tok"})
        self.printers = [
            FakePrinter(pid="p1", name="P1S", state="RUNNING", frame=JPEG),
            FakePrinter(pid="p2", name="X1C", state="OFFLINE", connected=False),
        ]
        self.manager = FakeManager(self.db, self.printers,
                                   queue=[{"name": "Вторая.gcode", "state": "queued"}])
        self.bot = StaffBot(self.manager)
        self.calls: list[tuple[str, dict, dict | None]] = []
        self.bot._call = lambda method, params, timeout=35, files=None: (
            self.calls.append((method, params, files)) or {"ok": True})

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._photo_patch.stop()
        self._tmp.cleanup()

    # ------------------------------------------------------------- данные
    def add_order(self, number="1043", status="ready", product="Деталь", price=1200):
        self.db.upsert("orders", {
            "id": f"order-{number}", "number": str(number), "product": product,
            "status": status, "customer_name": "Иван", "price": price, "qty": 1,
            "created_at": now_iso(), "updated_at": now_iso()})
        return f"order-{number}"

    def add_photo(self, order_id, name="order_frame.jpg"):
        (self.photos / name).write_bytes(JPEG)
        self.db.upsert("order_photos", {"id": "ph-" + name, "order_id": order_id,
                                        "at": now_iso(), "file": name, "note": "", "kind": "camera"})

    def add_shelf_item(self, name="Крепление", qty=1, min_qty=2, photo="nom.jpg"):
        self.db.upsert("nom_groups", {"id": "g1", "name": "Крепёж", "color": "#123456"})
        self.db.upsert("nomenclature", {
            "id": "n1", "name": name, "sku": "SKU-1", "photo": photo, "group_id": "g1",
            "created_at": now_iso(), "updated_at": now_iso()})
        self.db.upsert("shelf_items", {"id": "s1", "nom_id": "n1", "qty": qty,
                                       "min_qty": min_qty, "price": 100, "active": 1})

    def add_money(self, amount=1250, kind="income"):
        self.db.upsert("transactions", {"id": f"t-{amount}-{kind}", "at": now_iso(),
                                        "kind": kind, "amount": amount, "title": "Продажа",
                                        "category": "order"})

    def methods(self) -> list[str]:
        return [method for method, _params, _files in self.calls]

    def last_text(self) -> str:
        for method, params, _files in reversed(self.calls):
            if method == "sendMessage":
                return str(params.get("text") or "")
        return ""

    # ------------------------------------------------------------- отчёты
    def test_status_sends_photo_then_text(self):
        self.add_order(status="ready")
        self.bot._dispatch("111", "статус")
        self.assertEqual(["sendPhoto", "sendMessage"], self.methods())
        self.assertEqual(JPEG, self.calls[0][2]["photo"][1])
        self.assertIn("P1S", self.calls[0][1]["caption"])
        text = self.last_text()
        self.assertIn("Цех сейчас", text)
        self.assertIn("В сети 1 из 2", text)
        self.assertIn("Готово к выдаче: 1 заказ", text)

    def test_status_without_camera_is_still_text(self):
        self.manager.printers = {"p2": self.printers[1]}
        self.bot._dispatch("111", "статус")
        self.assertEqual(["sendMessage"], self.methods())

    def test_orders_number_shows_card_with_photo(self):
        order_id = self.add_order(number="1043")
        self.add_photo(order_id)
        self.bot._dispatch("111", "заказ 1043")
        self.assertEqual(["sendPhoto", "sendMessage"], self.methods())
        self.assertEqual(JPEG, self.calls[0][2]["photo"][1])
        self.assertIn("№1043", self.calls[0][1]["caption"])
        self.assertIn("Заказ №1043", self.last_text())

    def test_orders_number_not_found(self):
        self.bot._dispatch("111", "заказ 99999")
        self.assertEqual(["sendMessage"], self.methods())
        self.assertIn("не найден", self.last_text())
        self.assertNotIn("99999 ₽", self.last_text())

    def test_orders_list_prefers_ready_order_photo(self):
        self.add_order(number="1042", status="printing", product="Старая")
        ready = self.add_order(number="1043", status="ready")
        self.add_photo(ready)
        self.bot._dispatch("111", "заказы")
        self.assertEqual(["sendPhoto", "sendMessage"], self.methods())
        self.assertIn("№1043", self.calls[0][1]["caption"])
        self.assertIn("№1042", self.last_text())

    def test_shelf_shows_low_first_and_photo_from_nomenclature(self):
        (self.photos / "nom.jpg").write_bytes(JPEG)
        self.add_shelf_item()
        self.bot._dispatch("111", "полка")
        self.assertEqual(["sendPhoto", "sendMessage"], self.methods())
        self.assertIn("Крепление", self.calls[0][1]["caption"])
        text = self.last_text()
        self.assertIn("Полка: 1 позиция, из них мало — 1", text)
        self.assertIn("⚠ Крепление — 1 шт (мало) · Крепёж · 100 ₽", text)

    def test_queue_text_mentions_printing(self):
        self.bot._dispatch("111", "очередь")
        text = self.last_text()
        self.assertIn("Очередь печати: 1 задание", text)
        self.assertIn("Вторая.gcode — в очереди", text)

    def test_frame_without_camera_answers_text(self):
        self.manager.printers = {"p2": self.printers[1]}
        self.bot._dispatch("111", "кадр")
        self.assertEqual(["sendMessage"], self.methods())
        self.assertIn("Кадр недоступен", self.last_text())

    def test_frame_sends_photo(self):
        self.bot._dispatch("111", "кадр")
        self.assertEqual(["sendPhoto"], self.methods())
        self.assertIn("P1S", self.calls[0][1]["caption"])

    # ------------------------------------------------------------- деньги
    def test_money_counts_real_transactions(self):
        self.add_money(1250)
        self.add_money(300, kind="expense")
        self.bot._dispatch("111", "деньги")
        text = self.last_text()
        self.assertIn("приход 1 250 ₽", text)
        self.assertIn("расход 300 ₽", text)

    def test_money_is_closed_for_employee(self):
        Staff(self.db).add("Ваня", "employee", "222")
        self.bot._dispatch("222", "деньги")
        self.assertEqual(["sendMessage"], self.methods())
        self.assertIn("только руководителю и владельцу", self.last_text())

    def test_money_button_is_hidden_for_employee(self):
        self.assertNotIn("Деньги", [b["text"] for b in flat_buttons(ui.main_menu_keyboard("employee"))])

    # ---------------------------------------------------------- регрессии
    def test_repeated_command_is_sent_again(self):
        """Ключ дедупликации из текста глотал второй «деньги» с теми же цифрами."""
        self.bot._dispatch("111", "деньги")
        self.bot._dispatch("111", "деньги")
        self.assertEqual(["sendMessage", "sendMessage"], self.methods())

    def test_menu_line_shows_numbers_and_text_buttons(self):
        self.add_order(status="ready")
        self.bot._dispatch("111", "меню")
        markup = json.loads(self.calls[-1][1]["reply_markup"])
        texts = [b["text"] for b in flat_buttons(markup)]
        self.assertIn("📊 Статус", texts)
        self.assertFalse([b for b in flat_buttons(markup) if "web_app" in b])
        self.assertIn("готово 1", self.last_text())
        self.assertNotIn("Mini App", self.last_text())

    def test_reports_never_exceed_telegram_limits(self):
        for index in range(40):
            self.add_order(number=f"10{index:02d}", status="new",
                           product="Очень длинное название изделия " * 4)
        self.bot._dispatch("111", "заказы")
        for method, params, _files in self.calls:
            self.assertLessEqual(len(str(params.get("text") or "")), 3800)
            self.assertLessEqual(len(str(params.get("caption") or "")), 1024)
            self.assertTrue(method in ("sendPhoto", "sendMessage"))


if __name__ == "__main__":
    unittest.main()

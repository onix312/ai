"""Отчёт днём картинкой: PNG-график, кнопка в «итогах», вечерний автопуш.

Проверяется генератор (структура PNG, пиксели стековых столбиков, подписи),
маршруты («график» и кнопка «cmd:chart»), интеграция с ботом через фальшивый
менеджер и расписание вечерней рассылки. PNG читается обратно zlib-ом —
внешние библиотеки не нужны и в тестах.
"""
from __future__ import annotations

import pathlib
import struct
import sys
import tempfile
import unittest
import zlib
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow.staffbot import TelegramBot  # noqa: E402
from connector.printflow.staffbot.charts import daily_report  # noqa: E402
from connector.printflow.staffbot.router import ROUTER  # noqa: E402

# День отчёта — всегда «сегодня»: генератор показывает текущую дату,
# и жёстко вписанная дата делала тест мёртвым наутро после её смены.
DAY = datetime.now().date().isoformat()


def _seed(db: Database) -> None:
    """День: 5 доходов (2 через кассу) и один расход."""
    rows = [
        ("t1", "10", 1500.0, "Продажа (shelf) · shelf"),
        ("t2", "10", 500.0, "Касса: наличные · shelf"),
        ("t3", "14", 3000.0, "Касса: наличные · shelf"),
        ("t4", "14", 2500.0, "Продажа (shelf) · shelf"),
        ("t5", "09", 800.0, "Продажа (online) · online"),
    ]
    for sale_id, hour, amount, note in rows:
        db.execute(
            "INSERT INTO transactions(id,at,kind,category,amount,note)"
            " VALUES(?,?,?,?,?,?)",
            (sale_id, f"{DAY}T{hour}:15:00", "income", "sale", amount, note))
    db.execute(
        "INSERT INTO transactions(id,at,kind,category,amount,note)"
        " VALUES('x1',?, 'expense','other',900.0,'')", (f"{DAY}T12:00:00",))
    db.execute(
        "INSERT INTO cashier_sales(id,payment_id,method,amount,cashier,"
        "created_at,confirmed_at) VALUES('c1','','cash',500.0,'Аня',?,'')",
        (f"{DAY}T10:05:00",))
    db.execute(
        "INSERT INTO cashier_sales(id,payment_id,method,amount,cashier,"
        "created_at,confirmed_at) VALUES('c2','','sbp',3000.0,'Аня',?,?)",
        (f"{DAY}T14:30:00", f"{DAY}T15:00:00"))


def _decode_png(png: bytes) -> tuple[int, int, list[bytes]]:
    """PNG → (ширина, высота, строки RGB). Фильтры всегда 0 — мы их пишем."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "нет PNG-сигнатуры"
    width = height = 0
    idat = b""
    pos = 8
    while pos < len(png):
        length = struct.unpack(">I", png[pos:pos + 4])[0]
        tag = png[pos + 4:pos + 8]
        payload = png[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            width, height = struct.unpack(">II", payload[:8])
            assert payload[8] == 8, "глубина цвета ожидана 8 бит"
            assert payload[9] == 2, "ожидан truecolor (RGB)"
            assert payload[12] == 0, "без интерлейса"
        elif tag == b"IDAT":
            idat += payload
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = 1 + width * 3
    lines = [raw[i * stride + 1:(i + 1) * stride] for i in range(height)]
    return width, height, lines


class FakePhotoManager:
    """Менеджер, который только фотографирует отправку фото и notify."""

    def __init__(self, db):
        self.db = db
        self.photos = []
        self.notes = []

    def snapshot(self, printer_id: str = "") -> dict:
        return {"printers": []}

    def _send_photo(self, token, chat, caption, photo, reply_markup=None):
        self.photos.append((chat, caption, photo))
        return {"ok": True}

    def notify_async(self, text, photo=None, buttons=None,
                     critical=False, event=""):
        self.notes.append((text, photo))


class DailyReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_png_structure_720x540(self):
        _seed(self.db)
        png, _ = daily_report(self.db, DAY)
        width, height, lines = _decode_png(png)
        self.assertEqual((width, height), (720, 540))
        self.assertEqual(len(lines), 540)
        self.assertEqual(len(lines[0]), 720 * 3)

    def test_caption_counts_and_money(self):
        _seed(self.db)
        _, caption = daily_report(self.db, DAY)
        self.assertIn("выручка 8 300 ₽", caption)
        self.assertIn("чеков 5", caption)
        self.assertIn("средний чек 1 660 ₽", caption)
        self.assertIn("2 продажи на 3 500 ₽", caption)
        self.assertIn("нал 1 · СБП 1", caption)
        self.assertIn("Расход дня 900 ₽ · чистыми 7 400 ₽", caption)

    def test_pixels_header_bars_and_kassa_stack(self):
        _seed(self.db)
        png, _ = daily_report(self.db, DAY)
        _, _, lines = _decode_png(png)

        def px(x, y):
            return tuple(lines[y][x * 3:x * 3 + 3])

        self.assertEqual(px(10, 10), (11, 16, 32), "шапка должна быть NAVY")
        self.assertEqual(px(710, 110), (248, 250, 252), "фон — светлый")
        slot = (700 - 56) / 15
        counts = {}
        for hour in (9, 10, 14):
            x = int(56 + slot * (hour - 8) + (slot - int(slot * 0.56)) / 2) + 5
            indigo = violet = 0
            for y in range(132, 468):
                if px(x, y) == (79, 70, 229):
                    indigo += 1
                elif px(x, y) == (124, 58, 237):
                    violet += 1
            counts[hour] = (indigo, violet)
        self.assertEqual(counts[9][1], 0, "9 часов — без кассы")
        self.assertGreater(counts[9][0], 0, "9 часов — есть индиго")
        self.assertGreater(counts[10][1], 0, "10 часов — фиолетовая секция кассы")
        self.assertGreater(counts[10][0], 0)
        self.assertGreater(counts[14][1], 0, "14 часов — фиолетовая секция кассы")
        self.assertGreater(counts[14][0], 0)
        self.assertGreater(counts[14][1], counts[10][1],
                           "касса в 14 часов больше, чем в 10")

    def test_empty_day_still_renders(self):
        png, caption = daily_report(self.db, DAY)
        width, height, _ = _decode_png(png)
        self.assertEqual((width, height), (720, 540))
        self.assertIn("выручка 0 ₽", caption)
        self.assertIn("0 продаж", caption)

    def test_deterministic_output(self):
        _seed(self.db)
        first = daily_report(self.db, DAY)
        second = daily_report(self.db, DAY)
        self.assertEqual(first, second)

    def test_unconfirmed_sbp_not_counted(self):
        db = self.db
        db.execute(
            "INSERT INTO transactions(id,at,kind,category,amount,note)"
            " VALUES('t1',?,'income','sale',1000.0,'')", (f"{DAY}T11:00:00",))
        db.execute(
            "INSERT INTO cashier_sales(id,payment_id,method,amount,cashier,"
            "created_at,confirmed_at) VALUES('c1','','sbp',1000.0,'Аня',?,'')",
            (f"{DAY}T11:05:00",))
        _, caption = daily_report(db, DAY)
        # транзакция есть, но неподтверждённая СБП-продажа кассы не считается
        self.assertIn("выручка 1 000 ₽", caption)
        self.assertIn("0 продаж", caption)

    def test_hours_outside_shift_ignored(self):
        db = self.db
        db.execute(
            "INSERT INTO transactions(id,at,kind,category,amount,note)"
            " VALUES('night',?,'income','sale',777.0,'')",
            (f"{DAY}T03:00:00",))
        _, caption = daily_report(db, DAY)
        self.assertIn("выручка 0 ₽", caption)  # ночь вне оси 8–22


class ChartRoutingTests(unittest.TestCase):
    def test_text_routes_to_cmd_chart(self):
        for word in ("график", "картинкой", "график за сегодня"):
            route = ROUTER.match_text(word)
            self.assertIsNotNone(route, word)
            self.assertEqual(route.method, "cmd_chart", word)

    def test_chart_callback_is_handler_kind(self):
        route, params = ROUTER.match_callback("chart")
        self.assertEqual((route.method, params), ("cb_chart", ""))
        self.assertNotEqual(route.kind, "text",
                            "фото отправляет сам обработчик, не строкой")


class ChartBotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111"})
        self.manager = FakePhotoManager(self.db)
        self.bot = TelegramBot(self.manager)

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _capture(self):
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        return calls

    def test_graph_command_sends_photo(self):
        _seed(self.db)
        self._capture()
        self.bot._dispatch("111", "график")
        self.assertEqual(len(self.manager.photos), 1)
        chat, caption, png = self.manager.photos[0]
        self.assertEqual(chat, "111")
        self.assertIn("выручка 8 300 ₽", caption)
        width, height, _ = _decode_png(png)
        self.assertEqual((width, height), (720, 540))

    def test_today_reply_carries_chart_button(self):
        self._capture()
        self.bot._dispatch("111", "итоги")
        # кнопку проверяем через очередь outbox: _reply кладёт sendMessage
        rows = self.db.query(
            "SELECT payload FROM telegram_outbox WHERE method='sendMessage'"
            " ORDER BY id DESC LIMIT 5")
        joined = " ".join(str(r.get("payload")) for r in rows)
        self.assertIn("cmd:chart", joined)
        self.assertIn("Картинкой", joined)

    def test_chart_button_callback_sends_photo(self):
        _seed(self.db)
        self._capture()
        self.bot.cb_chart("111", "42", "")
        self.assertEqual(len(self.manager.photos), 1)
        self.assertIn("8 300", self.manager.photos[0][1])


class EveningChartTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.manager = FakePhotoManager(self.db)
        self.bot = TelegramBot(self.manager)

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _settings(self, at: str) -> dict:
        # настройки читаются из базы (как в цикле бота), отметка «послано»
        # переживает рестарт — поэтому словарь собираем заново каждый раз
        base = dict(self.db.settings())
        base["telegram_chat_id"] = "111"
        base["evening_chart_time"] = at
        return base

    def test_fires_once_per_day(self):
        _seed(self.db)
        now = datetime.now().strftime("%H:%M")
        self.bot._maybe_evening_chart(self._settings(now))
        self.assertEqual(len(self.manager.notes), 1)
        caption, photo = self.manager.notes[0]
        self.assertIn("выручка 8 300 ₽", caption)
        self.assertTrue(photo and photo[:8] == b"\x89PNG\r\n\x1a\n")
        # отметка «уже послано» — второй вызов в ту же минуту молчит
        self.bot._maybe_evening_chart(self._settings(now))
        self.assertEqual(len(self.manager.notes), 1)
        self.assertEqual(
            self.db.setting("evening_chart_last"),
            datetime.now().strftime("%Y-%m-%d"))

    def test_skips_when_time_differs(self):
        self.bot._maybe_evening_chart(self._settings("03:33"))
        self.assertEqual(self.manager.notes, [])
        # отметка остаётся пустой (дефолт — пустая строка)
        self.assertFalse(self.db.setting("evening_chart_last"))


if __name__ == "__main__":
    unittest.main()

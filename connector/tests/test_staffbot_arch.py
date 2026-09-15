"""Архитектура бота сотрудников 18.1: маршруты, диалоги-сцены, рестарт.

Проверяется каркас переписанного бота (пакет staffbot):
  * таблицы маршрутов консистентны — у каждой записи есть метод-обработчик,
    слова не перекрываются, фразы выигрывают у слов;
  * права считаются по записи маршрута, включая особый случай «статус N»;
  * диалоги живут в SQLite: переживают пересоздание бота (перезапуск
    коннектора), просроченное ожидание честно начинается заново;
  * итог продажи предлагает «Продать ещё» и «Забрали деньги» (ТЗ 15.4 §4.1).
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
from connector.printflow.staffbot import StaffBot, TelegramBot  # noqa: E402
from connector.printflow.staffbot.router import (  # noqa: E402
    CALLBACKS, ROUTER, TEXT_COMMANDS, normalize)
from connector.printflow.staffbot.scenes import BotScenes, SELL  # noqa: E402


class FakeManager:
    def __init__(self, db, snapshot=None):
        self.db = db
        self.acc = Accounting(db)
        self.repo = None
        self.client_bot = None
        self._snapshot = snapshot or {"printers": []}

    def snapshot(self, printer_id: str = "") -> dict:
        return self._snapshot


def _shelf_item(db, name: str = "Адресник", qty: float = 5,
                price: float = 500.0) -> dict:
    return db.upsert("shelf_items", {
        "id": f"it-{name.lower()}", "name": name, "qty": qty,
        "price": price, "cost_per_unit": 150.0, "min_qty": 2,
        "active": 1, "created_at": "2026-09-01T10:00:00",
        "updated_at": "2026-09-01T10:00:00"})


class RouterTableTests(unittest.TestCase):
    """Реестры команд — данные, а значит проверяются как данные."""

    def test_every_text_command_has_method(self):
        for command in TEXT_COMMANDS:
            self.assertTrue(hasattr(StaffBot, command.method),
                            f"нет метода {command.method} для {command.words}")

    def test_every_callback_has_method(self):
        for route in CALLBACKS:
            self.assertTrue(hasattr(StaffBot, route.method),
                            f"нет метода {route.method} для {route.prefix}")

    def test_words_do_not_shadow_each_other(self):
        """Одно слово — один маршрут: позже добавленная запись не должна
        молча перебивать раннюю (в dict это произошло бы незаметно)."""
        seen: dict[str, str] = {}
        for command in TEXT_COMMANDS:
            if command.phrase:
                continue
            for word in command.words:
                self.assertNotIn(word, seen,
                                 f"слово «{word}» в двух командах: "
                                 f"{seen.get(word)} и {command.method}")
                seen[word] = command.method

    def test_callback_prefixes_do_not_shadow_each_other(self):
        seen: dict[str, str] = {}
        for route in CALLBACKS:
            self.assertNotIn(route.prefix, seen,
                             f"префикс «{route.prefix}» в двух маршрутах")
            seen[route.prefix] = route.method

    def test_phrase_wins_over_word(self):
        # «продажи стеллажа» — сводка, а не меню быстрой продажи
        route = ROUTER.match_text("продажи стеллажа")
        self.assertEqual(route.method, "cmd_shelf_sales7")
        route = ROUTER.match_text("продажи стеллажа за месяц")
        self.assertEqual(route.method, "cmd_shelf_sales7")
        # а «продажа» без уточнения — меню продажи
        self.assertEqual(ROUTER.match_text("продажа").method, "cmd_sell")

    def test_close_month_phrase(self):
        self.assertEqual(ROUTER.match_text("закрыть месяц").method,
                         "cmd_month_close")
        self.assertEqual(ROUTER.match_text("закрыть месяц fixed").method,
                         "cmd_month_close")

    def test_status_group_depends_on_digits(self):
        self.assertEqual(ROUTER.group_for(
            ROUTER.match_text("статус"), "статус"), "view")
        self.assertEqual(ROUTER.group_for(
            ROUTER.match_text("статус 1001 печать"), "статус 1001 печать"),
            "orders")
        # «принтер» — управление парком независимо от цифр
        self.assertEqual(ROUTER.group_for(
            ROUTER.match_text("принтер 2"), "принтер 2"), "printers")

    def test_unknown_text_and_callback(self):
        self.assertIsNone(ROUTER.match_text("ываваыва"))
        self.assertIsNone(ROUTER.match_text(""))
        self.assertIsNone(ROUTER.match_callback("нет-такой-кнопки"))

    def test_callback_longest_prefix_wins(self):
        route, params = ROUTER.match_callback("shelf-cash-w:500")
        self.assertEqual((route.method, params), ("cb_shelf_collect", "500"))
        route, params = ROUTER.match_callback("shelf-cash")
        self.assertEqual(route.method, "cb_shelf_cash")
        route, params = ROUTER.match_callback("cat-grp:g1:n1")
        self.assertEqual((route.method, params), ("cb_cat_group", "g1:n1"))
        route, _ = ROUTER.match_callback("cat-grps")
        self.assertEqual(route.method, "cb_cat_groups")
        route, params = ROUTER.match_callback("order-fulfill:1001:paid")
        self.assertEqual((route.method, params), ("cb_order_fulfill", "1001:paid"))

    def test_unknown_callback_group_is_view(self):
        self.assertEqual(ROUTER.group_for_callback("нет-такой-кнопки"), "view")

    def test_late_answer_only_for_goto(self):
        late = [r.prefix for r in CALLBACKS if r.late_answer]
        self.assertEqual(late, ["goto"])

    def test_normalize_reply_aliases_and_stop_live(self):
        self.assertEqual(normalize("🛒 Продать"), "продажа")
        self.assertEqual(normalize("📦 Полка"), "стеллаж")
        self.assertEqual(normalize("/start"), "start")
        self.assertEqual(normalize("стоп живой"), "стоп-живой")
        self.assertEqual(normalize("СТОП ЖИВОЙ"), "стоп-живой")
        # «стоп да» — подтверждение остановки печати, не дашборд
        self.assertEqual(normalize("стоп да"), "стоп да")


class BotScenesTests(unittest.TestCase):
    """Диалоги в SQLite: TTL, sweep, переживание пересоздания объекта."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_set_active_pop_roundtrip(self):
        scenes = BotScenes(self.db)
        self.assertIsNone(scenes.active("111"))
        scenes.set("111", SELL, {"item": "x", "qty": 2})
        scene = scenes.active("111")
        self.assertEqual(scene["scene"], SELL)
        self.assertEqual(scene["data"]["item"], "x")
        popped = scenes.pop("111")
        self.assertEqual(popped["scene"], SELL)
        self.assertIsNone(scenes.active("111"))

    def test_expired_scene_ignored_and_removed(self):
        scenes = BotScenes(self.db)
        scenes.set("111", SELL, {"item": "x"})
        self.db.execute("UPDATE bot_scenes SET expires_at='2000-01-01T00:00:00'")
        self.assertIsNone(scenes.active("111"))
        self.assertIsNone(self.db.one("SELECT * FROM bot_scenes WHERE chat_id='111'"))

    def test_sweep_removes_expired_only(self):
        scenes = BotScenes(self.db)
        scenes.set("111", SELL, {})
        scenes.set("222", SELL, {})
        self.db.execute("UPDATE bot_scenes SET expires_at='2000-01-01T00:00:00'"
                        " WHERE chat_id='222'")
        removed = scenes.sweep()
        self.assertEqual(removed, 1)
        self.assertIsNotNone(scenes.active("111"))
        self.assertIsNone(scenes.active("222"))

    def test_scene_survives_new_instance(self):
        """Перезапуск коннектора = новый объект поверх той же базы."""
        BotScenes(self.db).set("111", SELL, {"item": "x", "await": "price"})
        fresh = BotScenes(self.db)
        scene = fresh.active("111")
        self.assertEqual(scene["scene"], SELL)
        self.assertEqual(scene["data"]["await"], "price")


class SceneDispatchTests(unittest.TestCase):
    """Диалоги через диспетчер: рестарт, просрочка, сверка, ответ клиенту."""

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

    def _capture(self, bot):
        calls = []
        bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        return calls

    def test_sell_flow_survives_restart(self):
        item = _shelf_item(self.db)
        calls = self._capture(self.bot)
        self.bot.sell_flow_qty("111", item["id"])
        self.bot.sell_flow_price("111", 1)  # дошли до шага «своя цена»
        # «Перезапуск»: новый объект бота поверх той же базы и менеджера.
        self.bot.shutdown()
        self.bot = TelegramBot(self.manager)
        calls = self._capture(self.bot)
        self.bot._dispatch("111", "450")
        markup = calls[-1][1]["reply_markup"]
        self.assertIn("cmd:sell-channel:", markup)
        self.assertIn("450", calls[-1][1]["text"])
        scene = self.bot.scenes.active("111")
        self.assertEqual(scene["data"]["await"], "channel")

    def test_expired_sell_scene_starts_over(self):
        item = _shelf_item(self.db)
        self.bot.scenes.set("111", SELL, {"item": item["id"], "qty": 1,
                                          "price": 500, "channel": "",
                                          "await": "price"})
        self.db.execute("UPDATE bot_scenes SET expires_at='2000-01-01T00:00:00'")
        calls = self._capture(self.bot)
        self.bot._dispatch("111", "450")
        # число больше не считается своей ценой — честное «не понял»
        for method, params in calls:
            self.assertNotIn("cmd:sell-channel", str(params.get("reply_markup", "")))

    def test_sell_flow_do_without_scene(self):
        self.assertEqual(self.bot.sell_flow_do("111"),
                         "Начните продажу заново: «Продать».")

    def test_cash_reconcile_scene_takes_number(self):
        self.bot.scenes.set("111", "cash_reconcile", {})
        calls = self._capture(self.bot)
        self.bot._dispatch("111", "2000")
        text = calls[-1][1]["text"]
        self.assertIn("Сверка", text)
        self.assertIsNone(self.bot.scenes.active("111"))

    def test_client_reply_scene_takes_any_text(self):
        self.bot.scenes.set("111", "client_reply", {"target": "555"})
        calls = self._capture(self.bot)
        self.bot._dispatch("111", "Здравствуйте!")
        # клиентский бот не запущен — честный отказ, но ввод ушёл в диалог
        self.assertTrue(any("Клиентский бот не запущен" in str(p.get("text", ""))
                            for m, p in calls))
        self.assertIsNone(self.bot.scenes.active("111"))

    def test_sale_result_offers_next_steps(self):
        """ТЗ 15.4 §4.1: после продажи — «Продать ещё» и «Забрали деньги»."""
        item = _shelf_item(self.db)
        self.bot.scenes.set("111", SELL, {"item": item["id"], "qty": 1,
                                          "price": 500, "channel": "shelf",
                                          "await": "confirm"})
        calls = self._capture(self.bot)
        self.bot.cb_sell_confirm("111", "", "")
        text = calls[-1][1]["text"]
        self.assertIn("Продано 1 шт", text)
        markup = calls[-1][1]["reply_markup"]
        self.assertIn("Продать ещё", markup)
        self.assertIn("Забрали деньги", markup)
        self.assertIn("cmd:menu", markup)
        # продажа проведена и сцена закрыта
        from connector.printflow.shelf import Shelf
        self.assertEqual(Shelf(self.db).shop_cash()["in_shop"], 500)
        self.assertIsNone(self.bot.scenes.active("111"))


class StopLiveFixTests(unittest.TestCase):
    """«стоп живой» со пробелом больше не просит подтвердить останов печати."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111"})
        self.bot = TelegramBot(FakeManager(self.db))

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def test_stop_live_phrase_stops_dashboard(self):
        calls = []
        self.bot._call = lambda method, params, timeout=35: \
            calls.append((method, params)) or {"ok": True}
        self.bot._live["111"] = {"message_id": 5, "text": "x"}
        self.bot._dispatch("111", "стоп живой")
        self.assertIn("Дашборд выключен", calls[-1][1]["text"])
        self.assertNotIn("111", self.bot._live)


if __name__ == "__main__":
    unittest.main()

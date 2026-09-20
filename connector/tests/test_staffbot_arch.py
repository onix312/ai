"""Архитектура бота сотрудников — тонкий бот notify_only + Mini App.

Проверяется:
- clean_layers: core/config, core/db, core/api_client, router, handlers/menu, handlers/notify, scenes, ui, miniapp
- таблицы маршрутов консистентны — у каждой записи есть метод-обработчик, слова не перекрываются, фразы выигрывают
- права считаются по записи маршрута
- диалоги живут в SQLite: переживают пересоздание бота, просроченное ожидание честно начинается заново
- ui: клавиатура с web_app «Открыть цех»
- miniapp auth HMAC
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
import hashlib
import hmac
import urllib.parse
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.staffbot import StaffBot  # noqa: E402
from connector.printflow.staffbot.router import CALLBACKS, ROUTER, TEXT_COMMANDS, normalize, suggest_command  # noqa: E402
from connector.printflow.staffbot.scenes import BotScenes, SELL  # noqa: E402
from connector.printflow.staffbot.ui import main_menu_keyboard, web_app_keyboard  # noqa: E402
from connector.printflow.staffbot.miniapp import validate_init_data  # noqa: E402
from connector.printflow.staffbot.core.config import get_miniapp_url  # noqa: E402
from connector.printflow.staffbot.core.db import ensure_scenes_table  # noqa: E402
from connector.printflow.staffbot.core.api_client import TelegramApiClient  # noqa: E402


class FakeManager:
    def __init__(self, db, snapshot=None):
        self.db = db
        self.acc = Accounting(db)
        self.repo = None
        self.client_bot = None
        self._snapshot = snapshot or {"printers": []}
        self.notified = []

    def snapshot(self, printer_id: str = "") -> dict:
        return self._snapshot

    def queue(self):
        return []

    def notify_async(self, text, photo=None, buttons=None, critical=False, event=""):
        self.notified.append((text, buttons))


class CleanLayersTests(unittest.TestCase):
    def test_core_files_exist(self):
        base = ROOT / "connector" / "printflow" / "staffbot"
        for rel in ("core/config.py", "core/db.py", "core/api_client.py",
                    "router.py", "scenes.py", "ui.py", "miniapp.py",
                    "handlers/menu.py", "handlers/notify.py", "bot.py"):
            self.assertTrue((base / rel).exists(), f"нет файла {rel}")

    def test_api_client_has_call(self):
        client = TelegramApiClient(lambda: "", "staff")
        self.assertTrue(hasattr(client, "call"))

    def test_ensure_scenes_table(self):
        tmp = tempfile.TemporaryDirectory()
        db = Database(pathlib.Path(tmp.name) / "t.sqlite3")
        ensure_scenes_table(db)
        row = db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='bot_scenes'")
        self.assertIsNotNone(row)
        db.close()
        tmp.cleanup()

    def test_routes_staff_miniapp_exists(self):
        self.assertTrue((ROOT / "connector" / "printflow" / "routes_staff_miniapp.py").exists())
        self.assertTrue((ROOT / "site" / "assets" / "js" / "staff-miniapp.js").exists())


class RouterTableTests(unittest.TestCase):
    def test_every_text_command_has_method(self):
        for command in TEXT_COMMANDS:
            self.assertTrue(hasattr(StaffBot, command.method),
                            f"нет метода {command.method} для {command.words}")

    def test_every_callback_has_method(self):
        for route in CALLBACKS:
            self.assertTrue(hasattr(StaffBot, route.method),
                            f"нет метода {route.method} для {route.prefix}")

    def test_words_do_not_shadow_each_other(self):
        seen: dict[str, str] = {}
        for command in TEXT_COMMANDS:
            if command.phrase:
                continue
            for word in command.words:
                self.assertNotIn(word, seen,
                                 f"слово «{word}» в двух командах: {seen.get(word)} и {command.method}")
                seen[word] = command.method

    def test_callback_prefixes_do_not_shadow_each_other(self):
        seen: dict[str, str] = {}
        for route in CALLBACKS:
            self.assertNotIn(route.prefix, seen,
                             f"префикс «{route.prefix}» в двух маршрутах")
            seen[route.prefix] = route.method

    def test_phrase_wins_over_word(self):
        # фраза «закрыть месяц» должна матчиться
        route = ROUTER.match_text("закрыть месяц")
        self.assertIsNotNone(route)
        self.assertEqual(route.method, "cmd_menu")

    def test_unknown_text_and_callback(self):
        self.assertIsNone(ROUTER.match_text("ываваыва"))
        self.assertIsNone(ROUTER.match_text(""))
        self.assertIsNone(ROUTER.match_callback("нет-такой-кнопки"))

    def test_callback_longest_prefix_wins(self):
        route, params = ROUTER.match_callback("menu")
        self.assertEqual(route.method, "cb_menu")
        route, params = ROUTER.match_callback("help")
        self.assertEqual(route.method, "cb_help")

    def test_unknown_callback_group_is_view(self):
        self.assertEqual(ROUTER.group_for_callback("нет-такой-кнопки"), "view")

    def test_late_answer_only_for_goto(self):
        late = [r.prefix for r in CALLBACKS if r.late_answer]
        self.assertEqual(late, ["goto"])

    def test_normalize_reply_aliases(self):
        self.assertEqual(normalize("🛒 Продать"), "меню")
        self.assertEqual(normalize("📦 Полка"), "меню")
        self.assertEqual(normalize("/start"), "start")

    def test_suggest_command(self):
        self.assertEqual(suggest_command("полка"), "меню")
        self.assertEqual(suggest_command(""), "")


class UiWebAppTests(unittest.TestCase):
    def test_web_app_keyboard_has_url(self):
        kb = web_app_keyboard("https://example.com/staff")
        self.assertIn("inline_keyboard", kb)
        btn = kb["inline_keyboard"][0][0]
        self.assertEqual(btn["text"], "🏭 Открыть цех")
        self.assertEqual(btn["web_app"]["url"], "https://example.com/staff")

    def test_main_menu_keyboard_has_help_and_code(self):
        kb = main_menu_keyboard("https://example.com/staff")
        flat = [b for row in kb["inline_keyboard"] for b in row]
        texts = [b["text"] for b in flat]
        self.assertIn("🏭 Открыть цех", texts)
        # есть помощь и код
        self.assertTrue(any("Помощь" in t for t in texts))
        self.assertTrue(any("код" in t.lower() or "Мой код" in t for t in texts))


class BotScenesTests(unittest.TestCase):
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
        self.db.execute("UPDATE bot_scenes SET expires_at='2000-01-01T00:00:00' WHERE chat_id='222'")
        removed = scenes.sweep()
        self.assertEqual(removed, 1)
        self.assertIsNotNone(scenes.active("111"))
        self.assertIsNone(scenes.active("222"))

    def test_scene_survives_new_instance(self):
        BotScenes(self.db).set("111", SELL, {"item": "x", "await": "price"})
        fresh = BotScenes(self.db)
        scene = fresh.active("111")
        self.assertEqual(scene["scene"], SELL)
        self.assertEqual(scene["data"]["await"], "price")


class MiniAppAuthTests(unittest.TestCase):
    def _make_init_data(self, token: str, user: dict, auth_date: int | None = None) -> str:
        auth_date = auth_date or int(time.time())
        user_json = json.dumps(user, separators=(",", ":"))
        data = {
            "user": user_json,
            "auth_date": str(auth_date),
            "query_id": "test",
        }
        check_parts = [f"{k}={v}" for k, v in sorted(data.items())]
        check_string = "\n".join(check_parts)
        secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
        data["hash"] = calc_hash
        return urllib.parse.urlencode(data)

    def test_validate_init_data_ok(self):
        token = "123:ABC"
        user = {"id": 123, "first_name": "Test"}
        init_data = self._make_init_data(token, user)
        res = validate_init_data(init_data, token)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 123)

    def test_validate_init_data_bad_hash(self):
        token = "123:ABC"
        user = {"id": 123}
        init_data = self._make_init_data(token, user) + "x"
        res = validate_init_data(init_data, token)
        self.assertIsNone(res)

    def test_validate_init_data_expired(self):
        token = "123:ABC"
        user = {"id": 123}
        old_date = int(time.time()) - 90000
        init_data = self._make_init_data(token, user, auth_date=old_date)
        res = validate_init_data(init_data, token, max_age=86400)
        self.assertIsNone(res)


class MenuDispatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111", "telegram_token": "tok",
                              "public_url": "https://example.com"})
        self.manager = FakeManager(self.db)
        self.bot = StaffBot(self.manager)

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def test_dispatch_sends_menu_with_web_app(self):
        calls = []
        self.bot._call = lambda method, params, timeout=35: calls.append((method, params)) or {"ok": True}
        self.bot._dispatch("111", "меню")
        self.assertTrue(calls)
        method, params = calls[-1]
        self.assertEqual(method, "sendMessage")
        rm = json.loads(params["reply_markup"])
        btn = rm["inline_keyboard"][0][0]
        self.assertIn("web_app", btn)

    def test_unknown_suggests_menu(self):
        calls = []
        self.bot._call = lambda method, params, timeout=35: calls.append((method, params)) or {"ok": True}
        self.bot._dispatch("111", "абракадабра")
        self.assertTrue(calls)
        method, params = calls[-1]
        self.assertEqual(method, "sendMessage")


if __name__ == "__main__":
    unittest.main()

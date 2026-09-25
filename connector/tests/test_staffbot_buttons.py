"""Бот без Mini App: только кнопки, всё отвечает в чате, ассистент встроен.

Что держат эти тесты (19.0):

* web_app-кнопок нет ни в одном ответе бота — ни в меню, ни в отчётах,
  ни в уведомлениях: внешний HTTPS-адрес боту больше не нужен;
* настройка `staff_miniapp_url` удалена из DEFAULT_SETTINGS — set_settings
  её больше не сохраняет, страница /staff и маршруты /api/staff/* убраны;
* кнопки действий из уведомлений («Следующее», «Снял») отвечают в чате;
* ассистент: кнопка «🤖 Ассистент» открывает режим вопроса, свободная фраза
  уходит мозгу помощника, подсказки приходят кнопками, сотруднику режим
  недоступен, действие с деньгами/печатью из чата не выполняется.
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

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.config import DEFAULT_SETTINGS  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.staff import Staff  # noqa: E402
from connector.printflow.staffbot import StaffBot  # noqa: E402
from connector.printflow.staffbot.scenes import ASK  # noqa: E402
from connector.printflow.staffbot.ui import ask_keyboard, main_menu_keyboard  # noqa: E402


class FakeManager:
    def __init__(self, db, snapshot=None):
        self.db = db
        self.acc = Accounting(db)
        self.repo = None
        self.client_bot = None
        self._snapshot = snapshot or {"printers": []}
        self.notified = []
        self.removed = []

    def snapshot(self, printer_id: str = "") -> dict:
        return self._snapshot

    def queue(self):
        return []

    def notify_async(self, text, photo=None, buttons=None, critical=False, event=""):
        self.notified.append((text, buttons, event))

    def part_removed(self, printer_id=""):
        self.removed.append(printer_id)
        return {"ok": True}


def flat_buttons(markup: dict) -> list[dict]:
    return [b for row in (markup or {}).get("inline_keyboard") or [] for b in row]


def parse_markup(raw) -> dict:
    """reply_markup бывает строкой (прямой вызов) и словарём (через outbox)."""
    if isinstance(raw, str):
        return json.loads(raw)
    return raw or {}


class BotWithButtonsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111", "telegram_token": "tok",
                              "telegram_bot": "1", "telegram_enabled": "1"})
        self.manager = FakeManager(self.db)
        self.bot = StaffBot(self.manager)

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _capture_call(self):
        calls = []
        self.bot._call = lambda m, p, timeout=35, files=None: calls.append((m, p)) or {"ok": True}
        return calls

    def test_setting_staff_miniapp_url_removed(self):
        self.assertNotIn("staff_miniapp_url", DEFAULT_SETTINGS)
        self.db.set_settings({"staff_miniapp_url": "https://example.com/staff"})
        self.assertIsNone(self.db.setting("staff_miniapp_url", None))

    def test_no_web_app_in_any_reply(self):
        calls = self._capture_call()
        for cmd in ("меню", "статус", "полка", "деньги", "кадр", "очередь", "помощь", "код"):
            calls.clear()
            self.bot._dispatch("111", cmd)
            for _, params in calls:
                markup = params.get("reply_markup")
                if not markup:
                    continue
                for btn in flat_buttons(json.loads(markup)):
                    self.assertNotIn("web_app", btn, f"{cmd}: web_app-кнопка в ответе")

    def test_menu_has_assistant_button_for_owner(self):
        calls = self._capture_call()
        self.bot._dispatch("111", "меню")
        markups = [p["reply_markup"] for _, p in calls if p.get("reply_markup")]
        self.assertTrue(markups)
        markup = parse_markup(markups[-1])
        texts = [b["text"] for b in flat_buttons(markup)]
        self.assertIn("🤖 Ассистент", texts)
        self.assertTrue(all("callback_data" in b for b in flat_buttons(markup)))

    def test_notify_carries_menu_button_not_web_app(self):
        self.manager.notified.clear()
        self.bot._notify_with_button("печать завершена", event="test")
        text, buttons, event = self.manager.notified[0]
        self.assertEqual(event, "test")
        self.assertTrue(buttons)
        flat = [b for row in buttons for b in (row if isinstance(row, list) else [row])]
        for item in flat:
            if isinstance(item, dict):
                self.assertNotIn("web_app", item)

    def test_next_and_removed_answer_in_chat(self):
        calls = self._capture_call()
        self.bot._dispatch("111", "следующее")
        self.assertTrue(calls)
        self.assertIn("Очередь пуста", self._last_out(calls))
        calls.clear()
        self.bot._handle_callback({"message": {"chat": {"id": "111"}, "message_id": "5"},
                                   "data": "cmd:removed", "id": "42"}, "111")
        self.assertEqual(self.manager.removed, [""])
        self.assertTrue(calls)

    def _last_out(self, calls) -> str:
        for method, params in reversed(calls):
            if method == "sendMessage":
                return str(params.get("text") or "")
        return ""

    def test_unknown_text_suggests_buttons(self):
        calls = self._capture_call()
        self.bot._dispatch("111", "абракадабра")
        self.assertTrue(calls)
        markups = [p["reply_markup"] for _, p in calls if p.get("reply_markup")]
        self.assertTrue(markups)
        self.assertTrue(flat_buttons(parse_markup(markups[-1])))


class AssistantInBotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111", "telegram_token": "tok",
                              "telegram_bot": "1", "telegram_enabled": "1"})
        self.manager = FakeManager(self.db)
        self.bot = StaffBot(self.manager)
        self.staff = Staff(self.db)

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _capture_call(self):
        calls = []
        self.bot._call = lambda m, p, timeout=35, files=None: calls.append((m, p)) or {"ok": True}
        return calls

    def _last_out(self, calls) -> str:
        for method, params in reversed(calls):
            if method == "sendMessage":
                return str(params.get("text") or "")
        return ""

    def _all_out(self, calls) -> str:
        return "\n".join(str(p.get("text") or "") for m, p in calls if m == "sendMessage")

    def _brain_answer(self, reply="Альфа печатает крышку, осталось 12 минут.",
                      suggestions=("Сколько ему осталось?", "Кто нам должен?"), action=None):
        return {"ok": True, "reply": reply, "kind": "answer", "suggestions": list(suggestions),
                "action": action or {}, "steps": []}

    def test_employee_has_no_assistant_button(self):
        self.staff.add("Ваня", "employee", "222")
        calls = self._capture_call()
        self.bot._dispatch("222", "ассистент")
        # слово «ассистент» в группе finance — сотруднику роутер отказывает
        self.assertIn("недоступен для роли", self._last_out(calls))
        # и кнопки в меню у сотрудника нет
        calls.clear()
        self.bot._dispatch("222", "меню")
        markup = parse_markup(next(p for m, p in reversed(calls) if p.get("reply_markup")))
        self.assertNotIn("Ассистент", [b["text"] for b in flat_buttons(markup)])

    def test_owner_opens_ask_mode_and_free_text_goes_to_brain(self):
        calls = self._capture_call()
        self.bot._dispatch("111", "ассистент")
        self.assertIn("Ассистент цеха на связи", self._last_out(calls))
        scene = self.bot.scenes.active("111")
        self.assertEqual(scene["scene"], ASK)
        calls.clear()
        with mock.patch("connector.printflow.assistant_brain.chat",
                        return_value=self._brain_answer()) as brain_chat:
            self.bot._dispatch("111", "что печатает альфа?")
            brain_chat.assert_called_once()
            args, kwargs = brain_chat.call_args
            self.assertEqual(args[1], "что печатает альфа?")
            self.assertEqual(kwargs.get("delegate"), False)
            self.assertEqual(kwargs.get("source"), "telegram")
        self.assertIn("Альфа печатает крышку", self._all_out(calls))
        # сцена осталась: можно задавать вопросы дальше
        self.assertEqual((self.bot.scenes.active("111") or {}).get("scene"), ASK)

    def test_suggestions_come_back_as_buttons(self):
        calls = self._capture_call()
        self.bot._dispatch("111", "ассистент")
        calls.clear()
        with mock.patch("connector.printflow.assistant_brain.chat",
                        return_value=self._brain_answer()):
            self.bot._dispatch("111", "что печатает альфа?")
            # данные подсказки лежат в сцене, callback короткий
            scene = self.bot.scenes.active("111")
            self.assertEqual(scene["data"]["suggestions"][0], "Сколько ему осталось?")
            # кнопка-подсказка снова спрашивает мозг — под тем же моком
            self.bot._handle_callback({"message": {"chat": {"id": "111"}, "message_id": "5"},
                                       "data": "cmd:sug:0", "id": "43"}, "111")
        markup_row = next(p for m, p in calls if "Куда дальше" in str(p.get("text") or ""))
        texts = [b["text"] for b in flat_buttons(parse_markup(markup_row["reply_markup"]))]
        self.assertTrue(any("Сколько ему осталось" in t for t in texts))
        self.assertIn("Альфа печатает крышку", self._all_out(calls))

    def test_action_from_chat_is_not_executed(self):
        calls = self._capture_call()
        self.bot._dispatch("111", "ассистент")
        calls.clear()
        action = {"id": "printer_command", "title": "Команда станку", "confirm": True}
        with mock.patch("connector.printflow.assistant_brain.chat",
                        return_value=self._brain_answer(reply="Поставить Альфу на паузу?",
                                                        suggestions=(), action=action)):
            self.bot._dispatch("111", "поставь альфу на паузу")
        out = self._all_out(calls)
        self.assertIn("не выполняю", out)
        self.assertIn("панел", out)

    def test_menu_word_exits_ask_mode(self):
        calls = self._capture_call()
        self.bot._dispatch("111", "ассистент")
        calls.clear()
        self.bot._dispatch("111", "меню")
        self.assertIsNone(self.bot.scenes.active("111"))
        self.assertTrue(calls)

    def test_brain_failure_answers_honestly(self):
        calls = self._capture_call()
        self.bot._dispatch("111", "ассистент")
        calls.clear()
        with mock.patch("connector.printflow.assistant_brain.chat",
                        side_effect=RuntimeError("нет модели")):
            self.bot._dispatch("111", "сколько заказов?")
        self.assertIn("Не получилось", self._all_out(calls))
        # сцена погасла — бот не застрял в сломанном режиме
        self.assertIsNone(self.bot.scenes.active("111"))

    def test_ask_keyboard_has_no_web_app(self):
        for btn in flat_buttons(ask_keyboard()):
            self.assertNotIn("web_app", btn)
        menu = main_menu_keyboard("owner")
        for btn in flat_buttons(menu):
            self.assertNotIn("web_app", btn)


if __name__ == "__main__":
    unittest.main()

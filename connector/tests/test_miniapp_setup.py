"""Mini App цеха: адрес, кнопка и честный отказ вместо «Example Domain» (18.12.2).

Что держим этими тестами:

* адрес Mini App — **настоящая настройка**: `staff_miniapp_url` сохраняется
  через `db.set_settings` (до 18.12.2 ключ читался, но в `DEFAULT_SETTINGS`
  его не было, и настройка молча не записывалась);
* пустой или не-HTTPS адрес не превращается в `https://example.com/staff` —
  кнопка web_app просто не ставится (иначе Telegram отклоняет ВСЁ сообщение,
  `BUTTON_URL_INVALID`, и уведомления пропадают целиком);
* бот на команду «меню» объясняет, что заполнить, а не отправляет мёртвую
  кнопку.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.staffbot import StaffBot  # noqa: E402
from connector.printflow.staffbot.core.config import (  # noqa: E402
    REASON_NOT_HTTPS,
    REASON_NOT_SET,
    get_miniapp_url,
    miniapp_hint,
    miniapp_reason,
    miniapp_state,
    normalize_miniapp_url,
)
from connector.printflow.staffbot.ui import (  # noqa: E402
    markup_or_none,
    miniapp_button,
    web_app_keyboard,
)


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
        self.notified.append((text, buttons, event))


def flat_buttons(markup: dict) -> list[dict]:
    return [b for row in (markup.get("inline_keyboard") or []) for b in row]


class SettingTests(unittest.TestCase):
    """Адрес Mini App — обычная настройка панели, а не «мёртвый» ключ."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_address_is_saved(self):
        self.db.set_settings({"staff_miniapp_url": "https://ceh.example.ru/staff"})
        self.assertEqual("https://ceh.example.ru/staff",
                         self.db.setting("staff_miniapp_url"))
        self.assertEqual("https://ceh.example.ru/staff", get_miniapp_url(self.db))

    def test_address_is_in_default_settings(self):
        """Ключ обязан быть в DEFAULT_SETTINGS — иначе set_settings его глотает."""
        from connector.printflow.config import DEFAULT_SETTINGS
        self.assertIn("staff_miniapp_url", DEFAULT_SETTINGS)
        self.assertEqual("", DEFAULT_SETTINGS["staff_miniapp_url"])

    def test_address_is_in_settings_schema(self):
        """Поле видно в панели и имеет пояснение."""
        from connector.printflow import settings_schema
        spec = settings_schema.get_schema()["staff_miniapp_url"]
        self.assertEqual("telegram", spec["group"])
        self.assertIn("Mini App", spec["label"])
        self.assertIn("https", (spec["hint"] or "").lower())


class NormalizeTests(unittest.TestCase):
    def test_scheme_is_added(self):
        self.assertEqual("https://ceh.example.ru/staff",
                         normalize_miniapp_url("ceh.example.ru/staff"))

    def test_trailing_slash_is_stripped(self):
        self.assertEqual("https://ceh.example.ru/staff",
                         normalize_miniapp_url("https://ceh.example.ru/staff/"))

    def test_http_is_kept_but_visible(self):
        """http не выкидываем — но он не готов: Telegram такую кнопку не примет."""
        self.assertEqual("http://192.168.1.50:8765/staff",
                         normalize_miniapp_url("http://192.168.1.50:8765/staff/"))
        self.assertEqual(REASON_NOT_HTTPS,
                         miniapp_reason("http://192.168.1.50:8765/staff"))

    def test_empty_and_garbage(self):
        for value in ("", "   ", None, "ftp://ceh.example.ru"):
            with self.subTest(value=value):
                self.assertEqual("", normalize_miniapp_url(value))


class MiniappUrlTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_nothing_set_means_no_url(self):
        """Никакого example.com: пустой адрес — пустая строка."""
        self.assertEqual("", get_miniapp_url(self.db))
        state = miniapp_state(self.db)
        self.assertFalse(state["ready"])
        self.assertEqual(REASON_NOT_SET, state["reason"])
        self.assertIn("Адрес Mini App цеха", state["problem"])
        self.assertIn("MINIAPP-ЦЕХА", miniapp_hint(self.db))

    def test_public_url_gets_staff_suffix(self):
        self.db.set_settings({"public_url": "https://ceh.example.ru"})
        self.assertEqual("https://ceh.example.ru/staff", get_miniapp_url(self.db))

    def test_public_url_with_staff_is_not_doubled(self):
        self.db.set_settings({"public_url": "https://ceh.example.ru/staff"})
        self.assertEqual("https://ceh.example.ru/staff", get_miniapp_url(self.db))

    def test_direct_address_wins(self):
        self.db.set_settings({"public_url": "https://panel.example.ru",
                              "staff_miniapp_url": "https://ceh.example.ru/staff"})
        self.assertEqual("https://ceh.example.ru/staff", get_miniapp_url(self.db))

    def test_legacy_miniapp_url_key_still_works(self):
        """Старое имя ключа читается: базы, куда его вписали руками, не ломаются."""
        self.db.execute("INSERT INTO settings(key,value) VALUES(?,?)",
                        ("miniapp_url", json.dumps("https://legacy.example.ru/staff")))
        self.assertEqual("https://legacy.example.ru/staff", get_miniapp_url(self.db))

    def test_lan_address_is_not_ready(self):
        self.db.set_settings({"public_url": "http://192.168.1.50:8765"})
        state = miniapp_state(self.db)
        self.assertFalse(state["ready"])
        self.assertEqual("http://192.168.1.50:8765/staff", state["url"])
        self.assertIn("https", state["problem"])

    def test_https_address_is_ready(self):
        self.db.set_settings({"staff_miniapp_url": "https://ceh.example.ru/staff"})
        state = miniapp_state(self.db)
        self.assertTrue(state["ready"])
        self.assertEqual("", state["problem"])
        # Подсказки нет — нечего объяснять
        self.assertNotIn("Mini App цеха не настроен", miniapp_hint(self.db))


class KeyboardTests(unittest.TestCase):
    def test_https_button_is_web_app(self):
        button = miniapp_button("https://ceh.example.ru/staff")
        self.assertEqual({"text": "🏭 Открыть цех",
                          "web_app": {"url": "https://ceh.example.ru/staff"}}, button)

    def test_http_button_is_not_web_app(self):
        """Иначе Telegram отвечает BUTTON_URL_INVALID — и сообщение не уходит."""
        self.assertIsNone(miniapp_button("http://192.168.1.50:8765/staff"))
        self.assertIsNone(miniapp_button(""))
        self.assertEqual([], web_app_keyboard("http://192.168.1.50:8765/staff")["inline_keyboard"])
        self.assertEqual([], web_app_keyboard("")["inline_keyboard"])

    def test_extra_rows_survive_without_address(self):
        kb = web_app_keyboard("", extra_rows=[[{"text": "❔ Помощь", "callback_data": "cmd:help"}]])
        texts = [b["text"] for b in flat_buttons(kb)]
        self.assertEqual(["❔ Помощь"], texts)

    def test_empty_keyboard_becomes_none(self):
        """Пустой inline_keyboard Telegram отклоняет — сообщение не уходит."""
        self.assertIsNone(markup_or_none({"inline_keyboard": []}))
        self.assertIsNone(markup_or_none(None))
        kb = web_app_keyboard("", extra_rows=[[{"text": "❔ Помощь", "callback_data": "cmd:help"}]])
        self.assertIs(kb, markup_or_none(kb))

    def test_no_example_domain_in_bot(self):
        """Строковый контракт: example.com не возвращается в код бота.

        Docstrings не считаем — в них как раз объяснено, что подстановка
        ``https://example.com/staff`` была ошибкой 18.12.1. Проверяются рабочие
        строковые литералы: именно они попадают в кнопку.
        """
        import ast
        base = ROOT / "connector" / "printflow"
        for rel in ("staffbot/core/config.py", "staffbot/ui.py",
                    "staffbot/handlers/menu.py", "staffbot/handlers/notify.py"):
            text = (base / rel).read_text(encoding="utf-8")
            tree = ast.parse(text)
            # Docstring — тоже строковый литерал: запоминаем их по id узла.
            docs = set()
            for node in ast.walk(tree):
                body = getattr(node, "body", None)
                if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                        and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    docs.add(id(body[0].value))
            bad = [n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and id(n) not in docs and "example.com" in n.value]
            self.assertEqual([], bad, f"{rel}: снова мёртвая кнопка на example.com")


class MenuReplyTests(unittest.TestCase):
    """Команда «меню»: либо кнопка цеха, либо инструкция — не «Example Domain»."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111", "telegram_token": "tok"})
        self.manager = FakeManager(self.db)
        self.bot = StaffBot(self.manager)
        self.calls: list[tuple[str, dict]] = []
        self.bot._call = lambda method, params, timeout=35: (
            self.calls.append((method, params)) or {"ok": True})

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _last_markup(self) -> dict:
        payload = self.calls[-1][1]
        return json.loads(payload.get("reply_markup") or "{}")

    def test_menu_without_address_explains_instead_of_button(self):
        self.bot._dispatch("111", "меню")
        payload = self.calls[-1][1]
        self.assertNotIn("example.com", json.dumps(payload, ensure_ascii=False))
        buttons = flat_buttons(self._last_markup())
        self.assertFalse([b for b in buttons if "web_app" in b], "web_app без адреса")
        self.assertTrue([b for b in buttons if "Помощь" in b["text"]], "нет кнопки помощи")
        self.assertIn("Адрес Mini App цеха", payload["text"])
        self.assertIn("MINIAPP-ЦЕХА", payload["text"])

    def test_menu_with_lan_address_explains_https(self):
        self.db.set_settings({"public_url": "http://192.168.1.50:8765"})
        self.bot._dispatch("111", "меню")
        payload = self.calls[-1][1]
        self.assertIn("https", payload["text"])
        self.assertFalse([b for b in flat_buttons(self._last_markup()) if "web_app" in b])

    def test_unknown_command_explains_missing_address(self):
        """«Откройте цех кнопкой» без кнопки не обещает: причина рядом."""
        self.bot._dispatch("111", "ываваыва")
        text = self.calls[-1][1]["text"]
        self.assertIn("Адрес Mini App цеха", text)

    def test_unknown_command_without_note_when_ready(self):
        self.db.set_settings({"staff_miniapp_url": "https://ceh.example.ru/staff"})
        self.bot._dispatch("111", "ываваыва")
        text = self.calls[-1][1]["text"]
        self.assertNotIn("Адрес Mini App", text)

    def test_menu_with_https_address_has_web_app(self):
        self.db.set_settings({"staff_miniapp_url": "https://ceh.example.ru/staff"})
        self.bot._dispatch("111", "меню")
        buttons = flat_buttons(self._last_markup())
        web = [b for b in buttons if "web_app" in b]
        self.assertEqual(1, len(web))
        self.assertEqual("https://ceh.example.ru/staff", web[0]["web_app"]["url"])


class SendCodeTests(unittest.TestCase):
    """«Мой код» без адреса: сообщение уходит, пустой клавиатуры в нём нет."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111", "telegram_token": "tok"})
        self.bot = StaffBot(FakeManager(self.db))
        self.calls: list[tuple[str, dict]] = []
        self.bot._call = lambda method, params, timeout=35: (
            self.calls.append((method, params)) or {"ok": True})
        self.bot._reply = lambda chat, text, buttons=None: self.calls.append(
            ("reply", {"chat_id": chat, "text": text, "buttons": buttons}))

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def test_code_command_without_address(self):
        self.bot._dispatch("111", "код")
        name, payload = self.calls[-1]
        self.assertEqual("reply", name)
        self.assertIsNone(payload["buttons"], "пустая клавиатура ломает сообщение")
        self.assertIn("chat_id", payload["text"])

    def test_code_command_with_address(self):
        self.db.set_settings({"staff_miniapp_url": "https://ceh.example.ru/staff"})
        self.bot._dispatch("111", "код")
        self.assertIsNotNone(self.calls[-1][1]["buttons"])


_APP_JS = (ROOT / "site" / "assets" / "app.js").read_text(encoding="utf-8")


def _js_function(name: str) -> str:
    """Исходник функции панели по имени — для прогона в node (правило 4)."""
    match = re.search(r"function " + name + r"\(s\) \{.*?\n\}", _APP_JS, re.S)
    assert match, f"в site/assets/app.js нет функции {name}"
    return match.group(0)


_JS_HARNESS = """
const esc = (s) => String(s == null ? '' : s);
""" + _js_function("miniappTarget") + "\n" + _js_function("miniappStatusRow") + """
const cases = [
  {},
  { public_url: 'https://ceh.example.ru' },
  { public_url: 'https://ceh.example.ru/staff' },
  { staff_miniapp_url: 'ceh.example.ru/staff' },
  { staff_miniapp_url: 'https://ceh.example.ru/staff/' },
  { staff_miniapp_url: 'https://ceh.example.ru/staff', public_url: 'https://panel.example.ru' },
  { public_url: 'http://192.168.1.50:8765' },
  { staff_miniapp_url: 'http://ceh.example.ru/staff' },
];
console.log(JSON.stringify({
  targets: cases.map((s) => { const t = miniappTarget(s); return { url: t.url, problem: !!t.problem }; }),
  statusOff: /data-miniapp-status="off"/.test(miniappStatusRow({ public_url: 'http://192.168.1.50:8765' })),
  statusOn: /data-miniapp-status="on"/.test(miniappStatusRow({ staff_miniapp_url: 'https://ceh.example.ru/staff' })),
  statusOnNoAddress: /data-miniapp-status="off"/.test(miniappStatusRow({})),
}));
"""


@unittest.skipUnless(shutil.which("node"), "нужен node для прогона функции панели")
class PanelAddressTests(unittest.TestCase):
    """Вердикт панели считает адрес так же, как сервер (иначе он врёт)."""

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(["node", "-e", _JS_HARNESS], capture_output=True,
                              text=True, timeout=60, cwd=ROOT)
        assert proc.returncode == 0, f"node упал: {proc.stderr[:800]}"
        cls.out = json.loads(proc.stdout)

    def test_addresses(self):
        urls = [t["url"] for t in self.out["targets"]]
        self.assertEqual("", urls[0])
        self.assertEqual("https://ceh.example.ru/staff", urls[1])
        self.assertEqual("https://ceh.example.ru/staff", urls[2], "хвостовой /staff удвоился")
        self.assertEqual("https://ceh.example.ru/staff", urls[3], "схема не подставлена")
        self.assertEqual("https://ceh.example.ru/staff", urls[4])
        self.assertEqual("https://ceh.example.ru/staff", urls[5], "прямой адрес не выиграл")
        self.assertEqual("http://192.168.1.50:8765/staff", urls[6])
        self.assertEqual("http://ceh.example.ru/staff", urls[7])

    def test_only_https_is_ready(self):
        problems = [t["problem"] for t in self.out["targets"]]
        self.assertEqual([True, False, False, False, False, False, True, True], problems)

    def test_verdict_markers(self):
        self.assertTrue(self.out["statusOff"])
        self.assertTrue(self.out["statusOn"])
        self.assertTrue(self.out["statusOnNoAddress"])

    def test_field_is_inside_telegram_card(self):
        block = _APP_JS.split("safe('telegram'", 1)[1].split("safe('theme'", 1)[0]
        self.assertIn('data-setting="staff_miniapp_url"', block,
                      "поле адреса Mini App ушло из карточки Telegram-уведомлений")
        self.assertIn("miniappStatusRow(s)", block)
        self.assertIn("Адрес Mini App цеха", block)


class ManagerNotifyTests(unittest.TestCase):
    """Уведомления принтера: кнопка цеха появляется только для https-адреса."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _buttons(self, settings: dict) -> list:
        from connector.printflow.manager import PrinterManager
        self.db.set_settings(settings)
        sent: list = []
        manager = PrinterManager.__new__(PrinterManager)   # без MQTT и потоков
        manager.db = self.db
        manager.lock = threading.RLock()
        manager.printers = {}
        manager.notify_async = lambda text, photo=None, buttons=None, critical=False, event="": sent.append(buttons)
        manager._notify("pause", "Пауза", "Станок ждёт", "нет-такого-принтера")
        return sent

    def test_web_app_button_only_for_https(self):
        buttons = self._buttons({"notify_pause": True, "telegram_enabled": True,
                                 "telegram_token": "tok", "telegram_chat_id": "111",
                                 "public_url": "http://192.168.1.50:8765"})
        self.assertEqual([None], buttons, "кнопка web_app для http-адреса уронила бы доставку")

    def test_notify_keeps_web_app_for_https(self):
        buttons = self._buttons({"notify_pause": True, "telegram_enabled": True,
                                 "telegram_token": "tok", "telegram_chat_id": "111",
                                 "staff_miniapp_url": "https://ceh.example.ru/staff"})
        flat = [b for row in (buttons[0] or []) for b in row]
        web = [b for b in flat if "web_app" in b]
        self.assertEqual(1, len(web))
        self.assertEqual("https://ceh.example.ru/staff", web[0]["web_app"]["url"])


if __name__ == "__main__":
    unittest.main()

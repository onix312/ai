"""Browser Provider 1.0: loopback guard, structured read and Brain contracts."""
from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest.mock import patch

from agent import brain, browser, executor
from agent.panel_client import Client
from agent.store import Store


class BrowserGuardTests(unittest.TestCase):
    def test_remote_cdp_is_rejected(self):
        with self.assertRaises(browser.BrowserError):
            browser._loopback_url("http://192.168.1.20:9222", ("http", "https"))
        with self.assertRaises(browser.BrowserError):
            browser._loopback_url("ws://example.com/devtools/page/1", ("ws", "wss"))

    def test_loopback_cdp_is_allowed(self):
        self.assertEqual(
            "http://127.0.0.1:9222",
            browser._loopback_url("http://127.0.0.1:9222", ("http", "https")),
        )
        self.assertEqual(
            "ws://localhost:9222/devtools/page/1",
            browser._loopback_url("ws://localhost:9222/devtools/page/1", ("ws", "wss")),
        )

    def test_inspection_script_never_returns_form_values(self):
        self.assertNotRegex(browser._INSPECT_JS, r"\bvalue\s*:")
        self.assertIn("sensitive", browser._INSPECT_JS)
        self.assertIn("type === 'password'", browser._INSPECT_JS)

    def test_probe_requires_websocket_dependency(self):
        with patch.object(browser, "_websocket_available", return_value=False):
            ok, reason = browser.probe()
        self.assertFalse(ok)
        self.assertIn("websocket-client", reason)


class BrowserReadTests(unittest.TestCase):
    def setUp(self):
        self.target = {
            "id": "tab-1",
            "title": "Example",
            "url": "https://example.test/",
            "websocket": "ws://127.0.0.1:9222/devtools/page/tab-1",
            "active": True,
        }

    def test_tabs_are_bounded_and_hide_websocket_url(self):
        payload = [
            {"type": "page", "id": "1", "title": "One", "url": "https://one.test",
             "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/1"},
            {"type": "page", "id": "2", "title": "Two", "url": "https://two.test",
             "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/2"},
            {"type": "service_worker", "id": "sw", "title": "worker", "url": "x"},
        ]
        with patch.object(browser, "_http_json", return_value=payload):
            result = browser.tabs(limit=1)
        self.assertTrue(result["ok"])
        self.assertEqual(1, result["count"])
        self.assertEqual("One", result["tabs"][0]["title"])
        self.assertNotIn("websocket", result["tabs"][0])

    def test_page_returns_structured_dom_without_mutation(self):
        dom = {
            "title": "Store",
            "url": "https://shop.test/item",
            "text": "PETG 1 кг. Цена 19 евро.",
            "selection": "PETG 1 кг",
            "links": [{"text": "Доставка", "href": "https://shop.test/delivery"}],
            "buttons": [{"text": "В корзину", "type": "button", "disabled": False}],
            "forms": [{"index": 0, "method": "post", "fields": [
                {"type": "email", "name": "email", "filled": True, "sensitive": False},
                {"type": "password", "name": "password", "filled": None, "sensitive": True},
            ]}],
        }
        with patch.object(browser, "_target", return_value=self.target), \
             patch.object(browser, "_evaluate", return_value=dom) as evaluate:
            result = browser.page(max_chars=1000)
        self.assertTrue(result["ok"])
        self.assertEqual("Store", result["title"])
        self.assertEqual("PETG 1 кг", result["selection"])
        self.assertEqual("password", result["forms"][0]["fields"][1]["type"])
        evaluate.assert_called_once()

    def test_page_text_is_bounded(self):
        with patch.object(browser, "_target", return_value=self.target), \
             patch.object(browser, "_evaluate", return_value={
                 "title": "Long", "url": "https://example.test", "text": "x" * 5000,
                 "selection": "", "links": [], "buttons": [], "forms": [],
             }):
            result = browser.page(max_chars=600)
        self.assertEqual(600, len(result["text"]))

    def test_find_returns_local_context(self):
        with patch.object(browser, "page", return_value={
            "ok": True, "target_id": "1", "title": "Guide", "url": "https://guide.test",
            "text": "Начало. PETG печатается при 245 градусах. Конец.",
        }):
            result = browser.find("PETG", limit=4)
        self.assertTrue(result["ok"])
        self.assertEqual(1, result["count"])
        self.assertIn("245", result["matches"][0])

    def test_selection_keeps_source(self):
        with patch.object(browser, "page", return_value={
            "ok": True, "target_id": "1", "title": "Guide", "url": "https://guide.test",
            "selection": "важный фрагмент",
        }):
            result = browser.selection()
        self.assertEqual("важный фрагмент", result["selection"])
        self.assertEqual("https://guide.test", result["url"])


class BrowserExecutorTests(unittest.TestCase):
    def test_registered_browser_skill_reaches_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(pathlib.Path(tmp) / "assistant.sqlite3")
            runner = executor.Runner(store=store, panel=Client("http://127.0.0.1:1"))
            runner._caps = {"browser": True, "browser_reason": ""}
            try:
                with patch.object(browser, "tabs", return_value={
                    "ok": True, "tabs": [{"id": "1", "title": "Tab", "url": "https://example.test"}],
                    "count": 1, "reason": "",
                }) as call:
                    result = runner.run("browser.tabs", {"limit": 5})
                self.assertTrue(result["ok"])
                self.assertEqual(1, result["count"])
                call.assert_called_once_with(5)
            finally:
                store.close()


class BrowserBrainTests(unittest.TestCase):
    def test_common_browser_phrases_are_deterministic(self):
        cases = {
            "какие вкладки открыты": "browser.tabs",
            "что на этой странице": "browser.page",
            "прочитай выделенное": "browser.selection",
            "найди на странице доставка": "browser.find",
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                plan = brain.understand(phrase)
                self.assertIsNotNone(plan)
                self.assertEqual(expected, plan["skill"])

    def test_browser_page_summary_is_human_readable(self):
        text = brain.summarize("browser.page", {
            "ok": True, "title": "Инструкция", "text": "Настройка принтера",
            "links": [{"text": "A"}], "forms": [],
        })
        self.assertIn("Инструкция", text)
        self.assertIn("Настройка принтера", text)


if __name__ == "__main__":
    unittest.main()

"""Provider Architecture 1.0 contracts."""
from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent import browser, executor, skills
from agent.panel_client import Client
from agent.providers import registry
from agent.store import Store


class ProviderRegistryTests(unittest.TestCase):
    def test_registry_has_no_drift_or_duplicate_ownership(self):
        self.assertEqual([], registry.validate())

    def test_migrated_skills_have_exact_owner(self):
        self.assertEqual("browser", registry.for_skill("browser.page").spec.name)
        self.assertEqual("desktop", registry.for_skill("desktop.observe").spec.name)
        self.assertEqual("printflow", registry.for_skill("panel.ask").spec.name)
        self.assertEqual("printflow", registry.for_skill("day.summary").spec.name)
        self.assertEqual("personal", registry.for_skill("reminder.list").spec.name)
        self.assertEqual("personal", registry.for_skill("habit.add").spec.name)
        self.assertIsNone(registry.for_skill("system.volume"))

    def test_provider_metadata_does_not_leak_to_unowned_or_learned_skills(self):
        system = skills.payload({"name": "system.volume", **skills.SKILLS["system.volume"]}, {})
        browser_skill = skills.payload({"name": "browser.page", **skills.SKILLS["browser.page"]}, {})
        self.assertEqual("", system["provider"])
        self.assertEqual("browser", browser_skill["provider"])

        learned, reason = skills.learn({
            "name": "my.safe",
            "title": "Мой навык",
            "steps": [{"skill": "system.volume", "params": {"level": 20}}],
        })
        self.assertIsNotNone(learned, reason)
        self.assertFalse(learned.get("provider"))

    def test_catalog_reports_capability_reasons_and_contracts(self):
        rows = registry.catalog({
            "browser": False,
            "browser_reason": "DevTools выключен",
            "windows": True,
            "windows_reason": "",
            "panel": True,
            "panel_reason": "",
            "sqlite": True,
            "sqlite_reason": "",
        })
        by_name = {row["name"]: row for row in rows}
        self.assertFalse(by_name["browser"]["available"])
        self.assertIn("DevTools", by_name["browser"]["reason"])
        self.assertTrue(by_name["desktop"]["available"])
        self.assertTrue(by_name["printflow"]["available"])
        self.assertTrue(by_name["personal"]["available"])
        browser_skills = {row["name"]: row for row in by_name["browser"]["skills"]}
        self.assertEqual("read", browser_skills["browser.page"]["risk"])
        self.assertFalse(browser_skills["browser.page"]["confirm"])
        self.assertTrue(browser_skills["browser.page"]["reversible"])
        self.assertFalse(browser_skills["browser.page"]["danger"])
        panel_skills = {row["name"]: row for row in by_name["printflow"]["skills"]}
        self.assertTrue(panel_skills["panel.do"]["danger"])
        self.assertTrue(panel_skills["panel.do"]["confirm"])


class PanelClientV19Tests(unittest.TestCase):
    def test_actions_groups_server_contracts_by_domain(self):
        client = Client()
        rows = [
            {"id": "park", "domain": "production", "confirm": False},
            {"id": "shelf_transfer_out", "domain": "retail", "confirm": True},
            {"id": "shelf", "domain": "retail", "confirm": False},
        ]
        with patch.object(client, "status", return_value={
            "alive": True, "actions": rows, "reason": "",
        }):
            payload = client.actions()
        self.assertTrue(payload["ok"])
        self.assertEqual(2, payload["contract_version"])
        self.assertEqual(["park"], payload["domains"]["production"])
        self.assertEqual(["shelf_transfer_out", "shelf"], payload["domains"]["retail"])
        self.assertEqual(1, payload["confirmed"])
        self.assertEqual(2, payload["reads"])


    def test_run_action_unwraps_order_draft_and_requests_verification(self):
        client = Client()
        action = {
            "id": "order_save", "method": "POST", "path": "/api/order/save",
            "confirm": True,
        }
        calls = []

        def fake_request(url, payload=None, timeout=0, **kwargs):
            calls.append((url, payload))
            if url.endswith("/api/order/save"):
                return True, {"ok": True, "order": {"id": "o1"}}, ""
            if url.endswith("/api/assistant/verify"):
                return True, {"ok": True, "verified": True, "state": "verified",
                              "evidence": {"order_id": "o1"}}, ""
            return False, None, "unexpected"

        with patch("agent.panel_client._request", side_effect=fake_request):
            result = client.run_action(
                action, {"draft": {"id": "o1", "product": "Ваза"}}, confirmed=True)

        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual("o1", calls[0][1]["id"])
        self.assertEqual("Ваза", calls[0][1]["product"])
        self.assertNotIn("draft", calls[0][1])
        self.assertEqual("order_save", calls[1][1]["action"])

    def test_run_action_unwraps_settings_patch(self):
        client = Client()
        action = {
            "id": "settings_save", "method": "POST", "path": "/api/settings",
            "confirm": True,
        }
        calls = []

        def fake_request(url, payload=None, timeout=0, **kwargs):
            calls.append((url, payload))
            if url.endswith("/api/settings"):
                return True, {"ok": True, "settings": {"tax_rate": 6}}, ""
            return True, {"ok": True, "verified": True, "state": "verified"}, ""

        with patch("agent.panel_client._request", side_effect=fake_request):
            client.run_action(action, {"patch": {"tax_rate": 6}}, confirmed=True)

        self.assertEqual(6, calls[0][1]["tax_rate"])
        self.assertNotIn("patch", calls[0][1])

    def test_order_fulfill_maps_confirmation_to_handoff(self):
        client = Client()
        action = {
            "id": "order_fulfill", "method": "POST", "path": "/api/order/fulfill",
            "confirm": True,
        }
        calls = []

        def fake_request(url, payload=None, timeout=0, **kwargs):
            calls.append((url, payload))
            if url.endswith("/api/order/fulfill"):
                return True, {"ok": True, "order": {"id": "o1"}}, ""
            return True, {"ok": True, "verified": True, "state": "verified"}, ""

        with patch("agent.panel_client._request", side_effect=fake_request):
            client.run_action(
                action, {"id": "o1", "payment_action": "debt"}, confirmed=True)

        self.assertTrue(calls[0][1]["handoff_confirmed"])
        self.assertNotIn("confirmed", calls[0][1])

class ProviderImplementationTests(unittest.TestCase):
    def test_printflow_provider_preserves_panel_action_confirmation_metadata(self):
        provider = registry.for_skill("panel.do")

        class Panel:
            def find_action(self, name):
                self.name = name
                return {"id": "queue.pause", "title": "Пауза очереди", "confirm": True}, ""

            def run_action(self, action, values, confirmed=False):
                return {"ok": True, "values": values, "confirmed": confirmed}

        runner = type("RunnerStub", (), {"panel": Panel()})()
        result = provider.run("panel.do", {
            "action": "queue.pause",
            "params": {"printer": "P1S"},
            "explain": "поставить очередь на паузу",
        }, runner)
        self.assertTrue(result["ok"])
        self.assertTrue(result["panel_confirm"])
        self.assertTrue(result["confirmed"])
        self.assertEqual("Пауза очереди", result["title_action"])
        self.assertEqual("поставить очередь на паузу", result["target"])

    def test_personal_provider_delegates_only_to_personal_handlers(self):
        provider = registry.for_skill("reminder.list")
        runner = object()
        handler = Mock(return_value={
            "ok": True, "reminders": [], "say": "Напоминаний нет.",
        })
        with patch("agent.providers.personal.personal_skills.handlers",
                   return_value={"reminder.list": handler}):
            result = provider.run("reminder.list", {"limit": 5}, runner)
        self.assertTrue(result["ok"])
        handler.assert_called_once_with({"limit": 5})


class ProviderDispatchTests(unittest.TestCase):
    def test_executor_routes_browser_skill_through_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(pathlib.Path(tmp) / "assistant.sqlite3")
            runner = executor.Runner(store=store, panel=Client("http://127.0.0.1:1"))
            runner._caps = {"browser": True, "browser_reason": ""}
            provider = registry.for_skill("browser.tabs")
            try:
                with patch.object(provider, "run", return_value={
                    "ok": True, "tabs": [], "count": 0, "reason": "",
                }) as run:
                    result = runner.run("browser.tabs", {"limit": 7})
                self.assertTrue(result["ok"])
                run.assert_called_once_with("browser.tabs", {"limit": 7}, runner)
            finally:
                store.close()

    def test_executor_routes_printflow_skill_through_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(pathlib.Path(tmp) / "assistant.sqlite3")
            runner = executor.Runner(store=store, panel=Client("http://127.0.0.1:1"))
            runner._caps = {"panel": True, "panel_reason": ""}
            provider = registry.for_skill("panel.ask")
            try:
                with patch.object(provider, "run", return_value={
                    "ok": True, "reply": "PrintFlow отвечает", "reason": "",
                }) as run:
                    result = runner.run("panel.ask", {"question": "что печатается"})
                self.assertTrue(result["ok"])
                run.assert_called_once_with(
                    "panel.ask", {"question": "что печатается"}, runner)
            finally:
                store.close()

    def test_executor_routes_personal_skill_through_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(pathlib.Path(tmp) / "assistant.sqlite3")
            runner = executor.Runner(store=store, panel=Client("http://127.0.0.1:1"))
            runner._caps = {"sqlite": True, "sqlite_reason": ""}
            provider = registry.for_skill("reminder.list")
            try:
                with patch.object(provider, "run", return_value={
                    "ok": True, "reminders": [], "say": "Напоминаний нет.",
                }) as run:
                    result = runner.run("reminder.list", {"limit": 10})
                self.assertTrue(result["ok"])
                run.assert_called_once_with(
                    "reminder.list", {"limit": 10}, runner)
            finally:
                store.close()

    def test_executor_routes_desktop_observe_through_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(pathlib.Path(tmp) / "assistant.sqlite3")
            runner = executor.Runner(store=store, panel=Client("http://127.0.0.1:1"))
            runner._caps = {"windows": True, "windows_reason": ""}
            provider = registry.for_skill("desktop.observe")
            try:
                with patch.object(provider, "run", return_value={
                    "ok": True, "window": {"title": "Test"}, "controls": [],
                }) as run:
                    result = runner.run("desktop.observe", {"limit": 12, "ocr": False})
                self.assertTrue(result["ok"])
                run.assert_called_once_with(
                    "desktop.observe", {"limit": 12, "ocr": False}, runner)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()

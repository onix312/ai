"""Security 2.0 phase 1 contracts."""
from __future__ import annotations

import pathlib
import tempfile
import unittest

from agent.security import DEFAULT_SENSITIVE_WINDOWS, SecurityPolicy
from agent.store import Store


class SecurityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "assistant.sqlite3")
        self.security = SecurityPolicy(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    @staticmethod
    def _skill(name: str, risk: str = "read", provider: str = "") -> dict:
        return {"name": name, "title": name, "risk": risk, "provider": provider}

    def test_defaults_are_not_guest_or_panicked(self):
        payload = self.security.payload()
        self.assertFalse(payload["guest_mode"])
        self.assertFalse(payload["panic"])
        self.assertEqual(list(DEFAULT_SENSITIVE_WINDOWS), payload["sensitive_windows"])

    def test_update_is_validated_before_persisting(self):
        failed = self.security.update(
            guest_mode=True,
            sensitive_windows=["x" * 81],
        )
        self.assertFalse(failed["ok"])
        self.assertFalse(self.security.guest_mode())

        saved = self.security.update(
            guest_mode=True,
            sensitive_windows=[" Bitwarden ", "private CRM", "bitwarden"],
        )
        self.assertTrue(saved["guest_mode"])
        self.assertEqual(["bitwarden", "private crm"], saved["sensitive_windows"])

    def test_guest_blocks_private_providers_and_owner_data(self):
        self.security.update(guest_mode=True)
        for skill in (
            self._skill("reminder.list", provider="personal"),
            self._skill("panel.ask", provider="printflow"),
            self._skill("browser.page", provider="browser"),
            self._skill("files.search"),
            self._skill("clipboard.read"),
            self._skill("window.text"),
        ):
            ok, reason = self.security.check_skill(skill)
            self.assertFalse(ok, skill["name"])
            self.assertIn("Guest Mode", reason)

        ok, _reason = self.security.check_skill(self._skill("system.health"))
        self.assertTrue(ok)

    def test_panic_blocks_actions_but_keeps_read_only_health(self):
        self.security.panic("hotkey")
        ok, reason = self.security.check_skill(self._skill("system.volume", "write"))
        self.assertFalse(ok)
        self.assertIn("Panic", reason)

        ok, _reason = self.security.check_skill(self._skill("system.health", "read"))
        self.assertTrue(ok)

        ui_ok, ui_reason = self.security.check_ui_action("click", {}, "Notepad")
        self.assertFalse(ui_ok)
        self.assertIn("Panic", ui_reason)

        self.security.resume()
        self.assertFalse(self.security.panic_latched())

    def test_sensitive_window_blocks_read_and_ui_automation(self):
        self.security.update(sensitive_windows=["Password Vault"])
        skill = self._skill("desktop.observe", "read", "desktop")
        ok, reason = self.security.check_skill(skill, {}, "My Password Vault - Work")
        self.assertFalse(ok)
        self.assertIn("Чувствительное окно", reason)

        ok, reason = self.security.check_ui_action(
            "type", {"text": "hello"}, "My Password Vault - Work")
        self.assertFalse(ok)
        self.assertIn("Чувствительное окно", reason)

        self.assertTrue(self.security.is_sensitive("PASSWORD VAULT"))
        self.assertFalse(self.security.is_sensitive("Calculator"))


if __name__ == "__main__":
    unittest.main()

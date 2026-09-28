"""Post-action verification: only evidence may say "verified"."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from agent import verifier


class VerifierTests(unittest.TestCase):
    def test_unknown_skill_is_assumed_not_verified(self):
        result = verifier.verify("app.open", {"target": "telegram"}, {"ok": True})
        self.assertEqual("assumed", result["status"])

    def test_volume_exact_level_is_read_back(self):
        with patch.object(verifier.pc, "volume_get",
                          return_value=({"level": 30, "muted": False}, "")):
            result = verifier.verify("system.volume", {"level": 30},
                                     {"ok": True, "level": 30, "muted": False})
        self.assertEqual("verified", result["status"])
        self.assertEqual(30, result["evidence"]["level"])

    def test_volume_mismatch_fails(self):
        with patch.object(verifier.pc, "volume_get",
                          return_value=({"level": 71, "muted": False}, "")):
            result = verifier.verify("system.volume", {"level": 30},
                                     {"ok": True, "level": 30, "muted": False})
        self.assertEqual("failed", result["status"])
        self.assertIn("30", result["reason"])

    def test_unreadable_volume_is_only_assumed(self):
        with patch.object(verifier.pc, "volume_get",
                          return_value=({}, "Core Audio недоступен")):
            result = verifier.verify("system.volume", {"level": 30}, {"ok": True})
        self.assertEqual("assumed", result["status"])
        self.assertIn("Core Audio", result["reason"])

    def test_focus_checks_active_window(self):
        with patch.object(verifier.winapi, "active_window",
                          return_value=("Telegram", "")):
            result = verifier.verify("window.focus", {"title": "telegram"},
                                     {"ok": True, "title": "Telegram"})
        self.assertEqual("verified", result["status"])

    def test_focus_mismatch_fails(self):
        with patch.object(verifier.winapi, "active_window",
                          return_value=("Блокнот", "")):
            result = verifier.verify("window.focus", {"title": "telegram"},
                                     {"ok": True, "title": "Telegram"})
        self.assertEqual("failed", result["status"])
        self.assertIn("Блокнот", result["reason"])

    def test_clipboard_is_read_back(self):
        from agent import clipboard as clipboard_mod
        with patch.object(clipboard_mod, "get_text", return_value=("привет", "")):
            result = verifier.verify("clipboard.write", {"text": "привет"},
                                     {"ok": True, "text": "привет"})
        self.assertEqual("verified", result["status"])

    def test_clipboard_mismatch_fails(self):
        from agent import clipboard as clipboard_mod
        with patch.object(clipboard_mod, "get_text", return_value=("другое", "")):
            result = verifier.verify("clipboard.write", {"text": "привет"},
                                     {"ok": True})
        self.assertEqual("failed", result["status"])

    def test_failed_skill_is_never_verified(self):
        result = verifier.verify("system.volume", {"level": 10},
                                 {"ok": False, "reason": "нет прав"})
        self.assertEqual("failed", result["status"])
        self.assertIn("нет прав", result["reason"])


if __name__ == "__main__":
    unittest.main()

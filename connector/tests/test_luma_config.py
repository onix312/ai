"""Compatibility contracts for Luma configuration aliases."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from agent import config


class EnvironmentAliasTests(unittest.TestCase):
    def test_luma_name_wins_over_nozza_alias(self):
        with patch.dict(os.environ, {
            "LUMA_ASSISTANT_NAME": "Люма",
            "NOZZA_ASSISTANT_NAME": "Старое имя",
        }, clear=True):
            self.assertEqual("Люма", config._env(
                "LUMA_ASSISTANT_NAME", "NOZZA_ASSISTANT_NAME", default="fallback"
            ))

    def test_legacy_alias_still_works(self):
        with patch.dict(os.environ, {
            "NOZZA_BROWSER_CDP_URL": "http://127.0.0.1:9333",
        }, clear=True):
            self.assertEqual(
                "http://127.0.0.1:9333",
                config._env(
                    "LUMA_BROWSER_CDP_URL",
                    "NOZZA_BROWSER_CDP_URL",
                    "PRINTFLOW_BROWSER_CDP_URL",
                    default="http://127.0.0.1:9222",
                ),
            )

    def test_default_is_used_when_no_alias_is_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                "fallback",
                config._env("LUMA_MISSING", "NOZZA_MISSING", default="fallback"),
            )


if __name__ == "__main__":
    unittest.main()

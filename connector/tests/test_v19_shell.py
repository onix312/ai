"""PrintFlow 19: contracts for the new shell and embedded Nozza rail."""
from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SITE = ROOT / "site"


class PrintFlowV19ShellTests(unittest.TestCase):
    def setUp(self):
        self.index = (SITE / "index.html").read_text(encoding="utf-8")
        self.tokens = (SITE / "assets" / "tokens.css").read_text(encoding="utf-8")
        self.v19 = (SITE / "assets" / "v19.css").read_text(encoding="utf-8")
        self.nozza = (SITE / "assets" / "nozza-rail.js").read_text(encoding="utf-8")

    def test_v19_shell_assets_are_loaded(self):
        self.assertIn('assets/tokens.css?v=19.0.0', self.index)
        self.assertIn('assets/v19.css?v=19.0.0', self.index)
        self.assertIn('assets/nozza-rail.js?v=19.0.0', self.index)
        self.assertNotIn(r"\n<link", self.index)
        self.assertNotIn(r"\n<script", self.index)

    def test_primary_information_architecture_is_human_centred(self):
        for label in ("Сегодня", "Продажи", "Производство", "Ресурсы", "Бизнес"):
            self.assertIn(f'<div class="nav-group-title">{label}</div>', self.index)
        self.assertIn('data-view="shelf"', self.index)
        self.assertIn('>Стеллаж<span', self.index)
        self.assertIn('>Товары и склад<span', self.index)
        self.assertIn('>Pricing Studio</a>', self.index)
        self.assertIn('>Входящие</a>', self.index)

    def test_nozza_is_embedded_but_full_ai_center_remains_available(self):
        for element_id in (
            "nozza_rail", "nozza_toggle", "nozza_nav_open", "nozza_messages",
            "nozza_context", "nozza_input", "nozza_send",
        ):
            self.assertIn(f'id="{element_id}"', self.index)
        self.assertIn('href="/assistant.html"', self.index)
        self.assertIn('Полный AI-центр', self.index)

    def test_p1_nozza_rail_is_read_chat_first(self):
        self.assertIn("/api/assistant/chat", self.nozza)
        self.assertIn("/api/assistant/context", self.nozza)
        self.assertIn("/api/assistant/agent", self.nozza)
        self.assertIn("delegate: true", self.nozza)
        # P1 must not create an execution bypass. P2 will use the canonical
        # Luma/PrintFlow provider + domain action registry.
        for forbidden in (
            "/api/printer/command",
            "/api/jobs/start",
            "/api/shelf/sale",
            "/api/order/fulfill",
            "panel.do",
        ):
            self.assertNotIn(forbidden, self.nozza)

    def test_warm_purple_tokens_are_the_v19_base(self):
        self.assertIn("--accent: #6d28d9;", self.tokens)
        self.assertIn("--accent-2: #8b5cf6;", self.tokens)
        self.assertIn("--bg: #f6f1ea;", self.tokens)
        self.assertIn("--panel: #fffdf9;", self.tokens)
        self.assertIn("--text: #29211d;", self.tokens)
        self.assertIn("--bg: #171310;", self.tokens)
        self.assertIn("--panel: #211b18;", self.tokens)
        self.assertIn("--v19-rail-w: 374px;", self.v19)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.v19)


if __name__ == "__main__":
    unittest.main()

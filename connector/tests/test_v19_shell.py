"""PrintFlow 19 shell and integrated AI rail contracts."""
from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SITE = ROOT / "site"


class PrintFlowV19ShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = (SITE / "index.html").read_text(encoding="utf-8")
        cls.css = (SITE / "assets" / "v19-shell.css").read_text(encoding="utf-8")
        cls.js = (SITE / "assets" / "v19-shell.js").read_text(encoding="utf-8")
        cls.assistant = (SITE / "assistant.html").read_text(encoding="utf-8")
        cls.sw = (SITE / "sw.js").read_text(encoding="utf-8")

    def test_primary_shell_uses_printflow_identity(self):
        head = self.index[:9000]
        self.assertIn("<title>PrintFlow — управление 3D-производством</title>", head)
        self.assertIn('<body class="pf-v19">', head)
        self.assertIn('<b>PrintFlow</b><small>локальное 3D-производство</small>', head)
        self.assertIn('class="pf-brand-mark"', head)
        self.assertNotIn('assets/brand/nozza-mark.svg', head)

    def test_integrated_ai_rail_is_wired(self):
        for element_id in (
            "pf_ai_nav", "pf_ai_open", "pf_ai_rail", "pf_ai_state",
            "pf_ai_close", "pf_ai_frame", "pf_ai_scrim",
        ):
            self.assertIn(f'id="{element_id}"', self.index)
        self.assertIn('data-src="/assistant.html?embed=1"', self.index)
        self.assertIn("assets/v19-shell.js?v=19.1.0", self.index)

    def test_ai_rail_is_local_and_contextual(self):
        self.assertIn("/api/assistant/status", self.js)
        self.assertIn("PF.on('view'", self.js)
        self.assertIn("pf-ai-open", self.js)
        self.assertIn("Alt+A", self.index)
        for banned in ("http://", "https://"):
            self.assertNotIn(banned, self.js)

    def test_embed_reuses_existing_assistant_in_nozza_visual_shell(self):
        self.assertIn("pf-ai-embed", self.assistant)
        self.assertIn('.as-pane[data-pane="chat"]', self.assistant)
        self.assertIn("--accent: #8E43F0", self.assistant)
        self.assertIn("html.pf-ai-embed .as-top", self.assistant)
        self.assertIn("html.pf-ai-embed .as-side", self.assistant)


    def test_approved_warm_violet_visual_tokens(self):
        self.assertIn("--pf-accent: #8E43F0", self.css)
        self.assertIn("--pf-accent-2: #6E2BC8", self.css)
        self.assertIn("--pf-peach: #E9925E", self.css)
        self.assertIn("--pf-cocoa: #31242E", self.css)
        self.assertIn('data-accent="violet"', self.index)
        self.assertIn("<span>Nozza</span>", self.index)
        self.assertIn("<b>Nozza</b><small id=\"pf_ai_state\">Luma core", self.index)
        self.assertIn("assets/v19-shell.css?v=19.1.0", self.index)

    def test_shell_assets_are_in_offline_cache(self):
        self.assertIn("/assets/v19-shell.css", self.sw)
        self.assertIn("/assets/v19-shell.js", self.sw)
        self.assertIn("printflow-shell-v107", self.sw)

    def test_motion_and_small_screen_are_supported(self):
        self.assertIn("prefers-reduced-motion: reduce", self.css)
        self.assertIn("@media (max-width: 700px)", self.css)
        self.assertIn("--pf-ai-w: 398px", self.css)


if __name__ == "__main__":
    unittest.main()

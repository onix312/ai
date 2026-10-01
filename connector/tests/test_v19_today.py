"""PrintFlow 19 operator-first Today dashboard contracts."""
from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SITE = ROOT / "site"


class TodayV19Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = (SITE / "index.html").read_text(encoding="utf-8")
        cls.app = (SITE / "assets" / "app.js").read_text(encoding="utf-8")
        cls.css = (SITE / "assets" / "v19-shell.css").read_text(encoding="utf-8")

    def test_today_starts_with_live_production_and_ai_briefing(self):
        view = self.index.split('id="view-dashboard"', 1)[1].split('</section>', 1)[0]
        self.assertIn('<h1>Сегодня</h1>', view)
        self.assertIn('class="v19-today-grid"', view)
        self.assertIn('id="dash_hero_pult"', view)
        self.assertIn('id="dash_ai_brief"', view)
        self.assertLess(view.index('id="dash_hero_pult"'), view.index('id="dash_kpis"'))
        self.assertLess(view.index('id="dash_ai_brief"'), view.index('id="dash_kpis"'))

    def test_briefing_is_deterministic_day_endpoint_not_model_prompt(self):
        self.assertIn("function refreshAiBriefing()", self.app)
        self.assertIn("get('/api/assistant/day', { kind: 'briefing', days: 1 })", self.app)
        block = self.app.split("function refreshAiBriefing()", 1)[1].split(
            "/* ============================================", 1)[0]
        self.assertNotIn("/api/assistant/chat", block)
        self.assertNotIn("/api/assistant/ask", block)

    def test_briefing_can_open_integrated_printflow_ai(self):
        self.assertIn("id=\"dash_ai_open\"", self.index)
        self.assertIn("id=\"dash_ai_refresh\"", self.index)
        self.assertIn("const button = $('pf_ai_open');", self.app)
        self.assertIn("if (button) button.click();", self.app)

    def test_briefing_refreshes_with_dashboard_and_periodically(self):
        self.assertIn("refreshAiBriefing()", self.app)
        self.assertIn("setInterval(refreshAiBriefing, 120000)", self.app)
        refresh = self.app.split("on('dash_refresh'", 1)[1].split("});", 1)[0]
        self.assertIn("refreshAiBriefing()", refresh)

    def test_ai_briefing_is_a_user_controllable_widget_with_migration(self):
        self.assertIn("['ai_brief', 'AI-брифинг смены']", self.app)
        self.assertIn("WIDGET_V19_MIGRATION_KEY", self.app)
        self.assertIn("filtered.includes('ai_brief')", self.app)

    def test_today_layout_collapses_cleanly_on_medium_screens(self):
        self.assertIn(".v19-today-grid", self.css)
        self.assertIn("grid-template-columns: minmax(0, 1.72fr) minmax(320px, .78fr)", self.css)
        self.assertIn("@media (max-width: 1160px)", self.css)
        self.assertIn(".v19-ai-line.warn", self.css)


if __name__ == "__main__":
    unittest.main()

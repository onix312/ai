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


    def test_printers_and_queue_visual_contract(self):
        self.assertIn("PrintFlow 19 printers + queue reference polish", self.css)
        self.assertIn("#view-printers .pc-prog .track i", self.css)
        self.assertIn(".v19-pr-operator::before", self.css)
        self.assertIn("#view-queue .queue-item.live", self.css)
        self.assertIn(".v19-queue-pulse button.on::before", self.css)
        self.assertIn("assets/v19-shell.css?v=19.2.0", self.index)
        self.assertIn("Приоритет, срок, материал и совместимость", self.index)


    def test_sales_and_finance_visual_contract(self):
        self.assertIn("PrintFlow 19 sales + finance reference polish", self.css)
        self.assertIn("#view-orders .v19-orders-pulse button::before", self.css)
        self.assertIn("#view-customers .crm-brief::before", self.css)
        self.assertIn("#view-finance .v19-fin-kpis .kpi::before", self.css)
        self.assertIn("#view-finance .v19-fin-attention-card::before", self.css)
        self.assertIn("assets/v19-shell.css?v=19.3.0", self.index)
        self.assertIn("без лишней CRM-сложности", self.index)
        self.assertIn("без бухгалтерского шума", self.index)


    def test_stock_and_shelf_visual_contract(self):
        self.assertIn("PrintFlow 19 stock + shelf reference polish", self.css)
        self.assertIn("#view-products .prod-card::before", self.css)
        self.assertIn("#view-shelf .shelf-card::before", self.css)
        self.assertIn("#view-shelf .v19-shelf-pulse button.on::before", self.css)
        self.assertIn("#view-inventory .inventory-extra", self.css)
        self.assertIn("assets/v19-shell.css?v=19.4.0", self.index)
        self.assertIn("следующий перенос на полку", self.index)
        self.assertIn("быстрым контролем дефицита и AMS", self.index)


    def test_production_accounting_visual_contract(self):
        self.assertIn("PrintFlow 19 production accounting polish", self.css)
        self.assertIn("#view-batches .batch-item.printing::before", self.css)
        self.assertIn("#view-documents > .toolbar", self.css)
        self.assertIn("#view-warehouses .wh-card::after", self.css)
        self.assertIn("assets/v19-shell.css?v=19.5.0", self.index)
        self.assertIn("прогресс выпуска и приёмка", self.index)
        self.assertIn("какие движения требуют проверки", self.index)


    def test_growth_and_calculator_visual_contract(self):
        self.assertIn("PrintFlow 19 growth + calculator polish", self.css)
        self.assertIn("#view-niches .niche-brief::before", self.css)
        self.assertIn("#view-calc .calc-grid > .card::before", self.css)
        self.assertIn("#view-calc .field input:focus", self.css)
        self.assertIn("assets/v19-shell.css?v=19.6.0", self.index)
        self.assertIn("фактической прибыли, конверсии", self.index)
        self.assertIn("прибыль на час", self.index)

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

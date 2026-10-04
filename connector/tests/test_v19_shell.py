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

    def test_primary_shell_uses_nozza_identity(self):
        head = self.index[:9000]
        self.assertIn("<title>NOZZA — управление 3D-производством</title>", head)
        self.assertIn('<body class="pf-v19">', head)
        self.assertIn('<b>NOZZA</b><small>производство и мастерская</small>', head)
        self.assertIn('class="brand-logo nozza-brand-logo"', head)
        self.assertIn('assets/brand/nozza-logo.svg', head)

    def test_print_catalog_failure_shows_unknown_instead_of_spinner_and_zero(self):
        script = (SITE / "assets" / "print.js").read_text(encoding="utf-8")
        loader = (SITE / "assets" / "core.js").read_text(encoding="utf-8")
        visual = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("function renderCatalogUnavailable()", script)
        self.assertIn("if (!loaded) renderCatalogUnavailable();", script)
        self.assertIn("if (tabCount) tabCount.textContent = '—';", script)
        self.assertIn("if (allCount) allCount.textContent = '—';", script)
        self.assertIn("if (search) search.disabled = true;", script)
        self.assertIn("#view-print .pr-catalog-offline", visual)
        self.assertIn('id="pr_filter_all_count">—</span>', self.index)
        self.assertIn("assets/core.js", self.index)
        self.assertIn("print: ['print.js']", loader)
        self.assertIn("(ASSET_VERSION ? '?v=' + ASSET_VERSION : '')", loader)

    def test_shift_center_offline_cards_preserve_reference_frame(self):
        visual = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("body.pf-v19 #view-ops10 { margin-right:-10px; }", visual)
        self.assertIn("body.pf-v19 #view-ops10 > .view-head { margin-bottom:5px; }", visual)
        self.assertIn("body.pf-v19 #view-ops10 #ops10_pipeline .kpi { height:95px;min-height:95px;box-sizing:border-box; }", visual)
        self.assertIn("body.pf-v19 #view-ops10 #ops10_printers > .empty { min-height:140px;box-sizing:border-box; }", visual)
        self.assertIn("assets/nozza-design-v2.css", self.index)

    def test_shift_center_production_offline_status_keeps_one_row_footprint(self):
        visual = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("body.pf-v19 #view-ops10 #ops10_production_list > .empty.compact", visual)
        self.assertIn("min-height:28px;box-sizing:border-box;", visual)
        self.assertIn("border-top:1px solid var(--line);", visual)
        self.assertIn("assets/nozza-design-v2.css", self.index)

    def test_niches_offline_state_preserves_reference_card_height(self):
        visual = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        ops = (SITE / "assets" / "ops.js").read_text(encoding="utf-8")
        self.assertIn("body.pf-v19 #view-niches > .view-head { margin-bottom:5px; }", visual)
        self.assertIn("body.pf-v19 #view-niches #niche_grid.niche-grid-offline > .empty { min-height:438px;box-sizing:border-box; }", visual)
        self.assertIn("host.classList.add('niche-grid-offline');", ops)
        self.assertIn("host.classList.remove('niche-grid-offline');", ops)
        self.assertIn("assets/ops.js", self.index)
        self.assertIn("assets/nozza-design-v2.css", self.index)

    def test_integrated_ai_rail_is_wired(self):
        for element_id in (
            "pf_ai_nav", "pf_ai_open", "pf_ai_rail", "pf_ai_state",
            "pf_ai_close", "pf_ai_frame", "pf_ai_scrim",
        ):
            self.assertIn(f'id="{element_id}"', self.index)
        self.assertIn('data-src="/assistant.html?embed=1&v=2"', self.index)
        self.assertIn("assets/v19-shell.js", self.index)

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

    def test_embedded_ai_rail_keeps_illustration_out_of_active_chat_surface(self):
        visual = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("html.pf-ai-embed body { background:#171116!important;background-image:none!important; }", self.assistant)
        rail = visual[visual.index("body.pf-v19 .pf-ai-rail::before {"):visual.index("body.pf-v19 .pf-ai-rail-head { position:relative", visual.index("body.pf-v19 .pf-ai-rail::before {"))]
        self.assertIn("background:#1D161F", rail)
        self.assertNotIn("nozza-workshop-v2.png", rail)
        self.assertIn("html[data-theme] body.pf-v19 { --pf-ai-w:398px; }", visual)
        self.assertIn("assets/nozza-design-v2.css", self.index)
        self.assertIn("assets/nozza-design-v2.css", self.assistant)

    def test_ai_rail_matches_specified_desktop_width_at_reference_viewport(self):
        visual = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("@media screen and (min-width:1280px) and (max-width:1380px) {\n  html[data-theme] body.pf-v19 { --pf-ai-w:398px; }", visual)


    def test_approved_warm_violet_visual_tokens(self):
        self.assertIn("--pf-accent: #8E43F0", self.css)
        self.assertIn("--pf-accent-2: #6E2BC8", self.css)
        self.assertIn("--pf-peach: #E9925E", self.css)
        self.assertIn("--pf-cocoa: #31242E", self.css)
        self.assertIn('data-accent="violet"', self.index)
        self.assertIn("<span>Nozza</span>", self.index)
        self.assertIn("<b>Nozza</b><small id=\"pf_ai_state\">Luma core", self.index)
        self.assertIn("assets/v19-shell.css", self.index)


    def test_printers_and_queue_visual_contract(self):
        self.assertIn("PrintFlow 19 printers + queue reference polish", self.css)
        self.assertIn("#view-printers .pc-prog .track i", self.css)
        self.assertIn(".v19-pr-operator::before", self.css)
        self.assertIn("#view-queue .queue-item.live", self.css)
        self.assertIn(".v19-queue-pulse button.on::before", self.css)
        self.assertIn("assets/v19-shell.css", self.index)
        self.assertIn("Приоритет, срок, материал и совместимость", self.index)


    def test_sales_and_finance_visual_contract(self):
        self.assertIn("PrintFlow 19 sales + finance reference polish", self.css)
        self.assertIn("#view-orders .v19-orders-pulse button::before", self.css)
        self.assertIn("#view-customers .crm-brief::before", self.css)
        self.assertIn("#view-finance .v19-fin-kpis .kpi::before", self.css)
        self.assertIn("#view-finance .v19-fin-attention-card::before", self.css)
        self.assertIn("assets/v19-shell.css", self.index)
        self.assertIn("без лишней CRM-сложности", self.index)
        self.assertIn("без бухгалтерского шума", self.index)


    def test_stock_and_shelf_visual_contract(self):
        self.assertIn("PrintFlow 19 stock + shelf reference polish", self.css)
        self.assertIn("#view-products .prod-card::before", self.css)
        self.assertIn("#view-shelf .shelf-card::before", self.css)
        self.assertIn("#view-shelf .v19-shelf-pulse button.on::before", self.css)
        self.assertIn("#view-inventory .inventory-extra", self.css)
        self.assertIn("assets/v19-shell.css", self.index)
        self.assertIn("следующий перенос на полку", self.index)
        self.assertIn("быстрым контролем дефицита и AMS", self.index)


    def test_production_accounting_visual_contract(self):
        self.assertIn("PrintFlow 19 production accounting polish", self.css)
        self.assertIn("#view-batches .batch-item.printing::before", self.css)
        self.assertIn("#view-documents > .toolbar", self.css)
        self.assertIn("#view-warehouses .wh-card::after", self.css)
        self.assertIn("assets/v19-shell.css", self.index)
        self.assertIn("прогресс выпуска и приёмка", self.index)
        self.assertIn("какие движения требуют проверки", self.index)


    def test_growth_and_calculator_visual_contract(self):
        self.assertIn("PrintFlow 19 growth + calculator polish", self.css)
        design = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("body.pf-v19 #view-calc .calc-result-price .big-num {\n  display:grid;justify-items:center;gap:4px;\n}", design)
        self.assertIn("#view-niches .niche-brief::before", self.css)
        self.assertIn("#view-calc .calc-inputs > .card::before", self.css)
        self.assertIn("#view-calc .calc-results > .card::before", self.css)
        self.assertIn("#view-calc .field input:focus", self.css)
        self.assertIn("assets/v19-shell.css", self.index)
        self.assertIn("фактической прибыли, конверсии", self.index)
        self.assertIn("прибыль на час", self.index)


    def test_tools_and_settings_visual_contract(self):
        self.assertIn("PrintFlow 19 tools + settings final polish", self.css)
        self.assertIn("#view-print .pr-workspace-tab[aria-selected=\"true\"]", self.css)
        self.assertIn("#view-conveyor .cv-dropzone:hover", self.css)
        self.assertIn("#view-clientbot .clientbot-settings-card", self.css)
        self.assertIn("#view-library .library-home", self.css)
        self.assertIn("#view-settings .settings-quicknav button.on::before", self.css)
        self.assertIn("assets/v19-shell.css", self.index)
        self.assertIn("<h2>Nozza</h2>", self.index)
        self.assertIn("Спросить Nozza", self.index)

    def test_clientbot_surfaces_inbox_before_collapsible_settings(self):
        section = self.index[self.index.index('<section class="view" id="view-clientbot">'):]
        self.assertLess(section.index('class="clientbot-grid clientbot-grid-top"'), section.index('class="clientbot-layout"'))
        self.assertIn('<details class="card clientbot-card clientbot-settings-card">', section)
        self.assertNotIn('<details class="card clientbot-card clientbot-settings-card" open>', section)
        self.assertIn('<input type="password" autocomplete="new-password" id="cb_token"', section)
        for action_id in ("cb_save", "cb_test", "cb_broadcast", "cb_outbox_retry"):
            self.assertIn(f'id="{action_id}"', section)
        design = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn(".clientbot-settings-card[open] .clientbot-settings-disclosure", design)
        self.assertIn("position:relative;display:flex;align-items:center;justify-content:space-between;gap:16px;padding-right:38px;cursor:pointer;list-style:none;", design)
        self.assertIn("position:absolute;right:14px;top:16px;flex:none;color:var(--muted);font-size:20px;", design)
        bot = (SITE / "assets" / "clientbot.js").read_text(encoding="utf-8")
        self.assertIn("<b>Inbox недоступен</b>", bot)
        self.assertIn("Текущие заявки неизвестны.", bot)
        self.assertIn("$('cb_commands').innerHTML = COMMANDS.map", bot)
        self.assertIn("if (!PF.offline) fail(error);", bot)
        self.assertIn("body.pf-v19 #view-clientbot .clientbot-grid-top .clientbot-table {\n  overflow-x:auto;overflow-y:hidden;-webkit-overflow-scrolling:touch;overscroll-behavior-inline:contain;\n}", design)

    def test_settings_is_pinned_above_connection_and_remains_searchable(self):
        self.assertIn('class="nav-link side-settings-link" id="side_settings_link" href="#settings"', self.index)
        self.assertLess(self.index.index('id="side_settings_link"'), self.index.index('id="conn_chip"'))
        details_start = self.index.index('<details class="nav-more"')
        details_end = self.index.index('</details>', details_start)
        self.assertNotIn('data-view="settings"', self.index[details_start:details_end])
        core = (SITE / "assets" / "core.js").read_text(encoding="utf-8")
        self.assertIn("const pinnedSettings = $('side_settings_link')", core)
        self.assertIn("pinnedSettings.hidden = !ok", core)
        self.assertIn("if (ok) shown++", core)
        self.assertIn("#side .nav-link').find((a) => !a.hidden)", core)

    def test_dashboard_follows_reference_composition_order(self):
        dashboard = self.index[self.index.index('<section class="view" id="view-dashboard">'):]
        self.assertLess(dashboard.index('class="v19-today-grid"'), dashboard.index('id="dash_kpis"'))
        self.assertLess(dashboard.index('id="dash_kpis"'), dashboard.index('data-widget="operator_focus"'))
        self.assertLess(dashboard.index('data-widget="operator_focus"'), dashboard.index('data-widget="overview_workspaces"'))
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns:minmax(0,2fr) minmax(280px,1fr)", css)
        self.assertIn("min-height:0!important", css)
        self.assertIn(".hero-cam-wrap { max-height:360px; }", css)

    def test_dashboard_keeps_period_and_primary_actions_with_page_heading(self):
        app = (SITE / "assets" / "app.js").read_text(encoding="utf-8")
        v2_css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("if (isDashboard) {", app)
        self.assertIn("if (head && actions.parentElement !== head) head.append(actions);", app)
        self.assertIn("#view-dashboard > .view-head #dash_period {", v2_css)
        self.assertIn("grid-column:1/-1;display:flex;width:100%;", v2_css)
        self.assertIn("#view-dashboard > .view-head .head-actions > .btn {", v2_css)
        self.assertIn("#view-dashboard .card-head > div { min-width:0;flex:1 1 auto; }", v2_css)
        self.assertIn("#view-dashboard .card-head > .btn,", v2_css)

    def test_shared_shell_dimensions_match_design_spec(self):
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("#topbar { height:64px;min-height:64px; }", css)
        self.assertIn("#side .nozza-brand-logo { width:34px;height:34px; }", css)
        self.assertIn("#side .nav-find { height:40px;min-height:40px;", css)
        self.assertIn("#side .nav-link { min-height:40px;", css)
        self.assertIn("@media screen and (max-width:1279px)", css)
        self.assertIn("body.pf-v19 #side.show { transform:translateX(0); }", css)
        self.assertIn("body.pf-v19 #main { margin-left:0;padding-right:0; }", css)
        self.assertIn("@media screen and (max-width:1380px)", css)
        self.assertIn("body.pf-v19.pf-ai-open #main { padding-right:0!important; }", css)
        self.assertIn("body.pf-v19 #topbar .top-title { display:flex;flex:0 1 104px;min-width:0; }", css)
        self.assertIn("body.pf-v19 #topbar .top-title span { display:block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap; }", css)

    def test_dashboard_typography_and_density_match_design_spec(self):
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("#view-dashboard > .view-head h1 { font-size:28px;line-height:36px; }", css)
        self.assertIn("#view-dashboard .card-head h2 { font-size:18px;line-height:26px; }", css)
        self.assertIn("min-height:104px;height:auto;padding:16px 20px", css)
        self.assertIn("#view-dashboard > .n2-dashboard-workspace table.data { font-size:14px;line-height:20px; }", css)
        self.assertIn("#view-dashboard .btn { min-height:40px;padding-inline:14px;font-size:14px;line-height:20px; }", css)

    def test_calculator_desktop_header_gap_matches_reference(self):
        visual = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("body.pf-v19 #view-calc > .view-head { margin-bottom:7px; }", visual)
        self.assertIn("assets/nozza-design-v2.css", self.index)

    def test_dashboard_offline_orders_do_not_force_wide_empty_table(self):
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        selector = "body.pf-v19 .n2-dashboard-orders table.data:not(:has(#dash_order_rows > tr[data-order]))"
        self.assertIn(selector + " {", css)
        self.assertIn("width:100%;min-width:0;table-layout:fixed;", css)
        self.assertIn(selector + " thead { display:none; }", css)
        self.assertIn("table.data { min-width:740px;font-size:11px; }", css)

    def test_batches_desktop_reference_spacing_and_kpi_height(self):
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("body.pf-v19 #view-batches > .view-head { margin-bottom:7px; }", css)
        self.assertIn("body.pf-v19 #view-batches #batch_kpis .kpi { padding:21px 20px 17px; }", css)
        self.assertIn("assets/nozza-design-v2.css", self.index)

    def test_dashboard_offline_live_shop_preserves_reference_frame(self):
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns:minmax(0,2.2fr) minmax(280px,1fr);", css)
        self.assertIn("min-height:568px!important;", css)
        self.assertIn("body.pf-v19 #view-dashboard > .view-head { padding-bottom:0;margin-bottom:5px; }", css)
        self.assertIn("body.pf-v19 #view-dashboard { margin-right:-10px; }", css)
        self.assertIn("body.pf-v19 #view-dashboard { margin-left:3px;margin-right:6px; }", css)
        self.assertIn("body.pf-v19 #view-dashboard > .view-head { margin-bottom:-3px; }", css)
        self.assertIn("#dash_hero_pult:has(#dash_active > .empty.compact)", css)
        self.assertIn("#dash_active:has(> .empty.compact) {\n    flex:1;display:grid;align-content:center;", css)

    def test_settings_header_actions_match_reference_alignment(self):
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("body.pf-v19 #view-settings { margin-right:-10px; }", css)
        self.assertIn("body.pf-v19 #view-settings > .view-head > .head-actions { margin-right:20px; }", css)
        self.assertIn("body.pf-v19 #view-settings #settings_connection_notice { display:none!important; }", css)
        self.assertIn("body.pf-v19 #view-settings .settings-quicknav { margin-top:-4px; }", css)
        self.assertIn("#view-settings > .view-head > div:first-child { flex:1 1 0;min-width:0; }", css)
        self.assertIn("display:grid;flex:0 0 424px;grid-template-columns:minmax(0,1fr) auto;grid-template-rows:40px 40px;", css)
        self.assertIn("#set_search { grid-column:1;grid-row:1;width:100%;min-width:0;box-sizing:border-box; }", css)
        self.assertIn("#settings_reset { grid-column:2;grid-row:1; }", css)
        self.assertIn("#settings_save { grid-column:1;grid-row:2;justify-self:start; }", css)

    def test_settings_offline_disables_legacy_form_controls(self):
        app = (SITE / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn(".pane input:not([type=\"search\"]), .pane select, .pane textarea", app)
        self.assertIn("controls.forEach((control) => { control.disabled = unknown; });", app)

    def test_products_workspace_matches_reference_frame_at_1352px(self):
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("@media screen and (min-width:1280px) and (max-width:1380px)", css)
        self.assertIn("body.pf-v19 #views { padding:28px 36px 40px 24px; }", css)
        self.assertIn("body.pf-v19 #view-products > .view-head { align-items:flex-end;margin-bottom:5px; }", css)
        self.assertIn("body.pf-v19 #view-products #prod_kpis .kpi { padding-block:18.5px; }", css)

    def test_queue_workspace_frame_matches_1362px_reference(self):
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("@media screen and (min-width:1280px) and (max-width:1380px)", css)
        self.assertIn("body.pf-v19 #view-queue { margin:0 -9px 0 3px; }", css)
        self.assertIn("body.pf-v19 #view-queue > .view-head { margin-bottom:6px; }", css)
        self.assertIn("assets/nozza-design-v2.css", self.index)

    def test_finance_mobile_tabs_fit_narrow_viewport(self):
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("body.pf-v19 #view-finance #fin_tabs { width:100%;box-sizing:border-box;gap:1px; }", css)
        self.assertIn("body.pf-v19 #view-finance #fin_tabs button { padding-inline:7px;font-size:12px; }", css)
        self.assertIn("assets/nozza-design-v2.css", self.index)

    def test_orders_page_keeps_documented_scale_and_kanban_geometry(self):
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("body.pf-v19 #view-orders .v19-orders-pulse > button b { font-size:20px;line-height:26px; }", css)
        self.assertIn("body.pf-v19 #view-orders #orders_kanban > .kan-col { flex:0 0 clamp(280px,22vw,320px);min-width:280px; }", css)
        self.assertIn("body.pf-v19 #view-orders .ocard h4 { margin:9px 0 7px;font-size:16px;line-height:22px; }", css)
        self.assertIn("body.pf-v19 #view-orders > .view-head { margin-bottom:5px; }", css)
        self.assertIn("body.pf-v19 #view-orders #orders_pulse { margin-top:6px;margin-bottom:13px; }", css)
        self.assertIn("body.pf-v19 #view-orders .v19-orders-pulse > button { min-height:62px;padding-block:8px; }", css)
        self.assertIn("body.pf-v19 #view-orders .v19-orders-toolbar { padding-block:8px; }", css)
        self.assertIn("@media screen and (max-width:600px)", css)
        self.assertIn("assets/nozza-design-v2.css", self.index)

    def test_printer_park_respects_minimum_card_width_and_offline_readability(self):
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        printer = (SITE / "assets" / "printer.js").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr))", css)
        self.assertIn("body.pf-v19 #view-printers .pr-offline-card span { font-size:13px;line-height:19px; }", css)
        self.assertIn("body.pf-v19 #view-printers .pr-offline-card {", css)
        self.assertIn("display:grid;grid-template-columns:minmax(0,1fr);align-content:start;gap:8px;", css)
        self.assertIn("empty.hidden = unavailable || list.length > 0 || configured > 0;", printer)
        self.assertIn("body.pf-v19 #view-printers .pr-offline-workspace .ptab { min-height:40px;", css)
        self.assertIn("assets/printer.js", self.index)
        self.assertIn("body.pf-v19 #view-printers .pr-offline-workspace .telemetry .fan-row .bar,", css)
        self.assertIn("body.pf-v19 #view-printers .pr-offline-workspace .telemetry .tspark { display:none!important; }", css)
        self.assertIn("body.pf-v19 #view-printers .pr-offline-workspace .orbit { display:none!important; }", css)
        self.assertIn("body.pf-v19 #view-printers { margin-right:-10px; }", css)
        self.assertIn("body.pf-v19 #view-printers > .view-head { margin-bottom:5px; }", css)
        self.assertIn("assets/nozza-design-v2.css", self.index)

    def test_stock_kpis_and_product_cards_follow_reference_scale(self):
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("body.pf-v19 #view-products #prod_kpis { grid-template-columns:repeat(5,minmax(0,1fr));gap:10px; }", css)
        self.assertIn("body.pf-v19 #view-inventory #stock_kpis { grid-template-columns:repeat(4,minmax(0,1fr));gap:10px; }", css)
        stock_js = (SITE / "assets" / "money.js").read_text(encoding="utf-8")
        self.assertIn("kpi('Остаток пластика', nfmt(totalG) + ' г', `${nfmt(spools.length)} катушек`)", stock_js)
        self.assertIn("kpi('Стоимость запаса', money(value), 'по цене закупки')", stock_js)
        self.assertIn("kpi('Заканчиваются', String(low.length)", stock_js)
        self.assertIn("kpi('Материалов', String(materials), 'разных типов')", stock_js)
        self.assertIn("`${nfmt(spools.length)} катушек`", stock_js)
        self.assertIn("'Остаток пластика', 'Стоимость запаса', 'Заканчиваются', 'Материалов'", stock_js)
        self.assertIn("body.pf-v19 #view-products #prod_kpis .kpi .value,\n  body.pf-v19 #view-inventory #stock_kpis .kpi .value { font-size:28px;line-height:34px; }", css)
        self.assertIn("body.pf-v19 #view-products .prod-grid:not(.shelf3d) { grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); }", css)
        self.assertIn("body.pf-v19 #view-products .prod-grid:not(.shelf3d) .prod-card .pphoto { width:96px;height:96px; }", css)
        self.assertIn("assets/nozza-design-v2.css", self.index)

    def test_shelf_actions_and_missing_code_state_are_explicit(self):
        shelf = (SITE / "assets" / "shelf.js").read_text(encoding="utf-8")
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("$('shelf_kpis').classList.add('shelf-unavailable');", shelf)
        self.assertIn("$('shelf_kpis').classList.remove('shelf-unavailable');", shelf)
        self.assertIn("#view-shelf #shelf_kpis.shelf-unavailable {", css)
        self.assertIn("#view-shelf #shelf_kpis:has(> .shelf-forecast) {", css)
        self.assertIn("grid-column:3 / span 3;grid-row:2;", css)
        self.assertIn('class="shelf-code-missing">1С: код не задан</span>', shelf)
        self.assertIn('data-shelf-sell="${esc(i.id)}" title="Продать 1 штуку и уменьшить остаток">Продать −1', shelf)
        self.assertIn('data-shelf-prod="${esc(i.id)}" title="Оформить приход одной штуки">Приход +1', shelf)
        self.assertIn(".shelf-code-missing {", css)
        self.assertIn("assets/shelf.js", self.index)
        self.assertIn("assets/nozza-design-v2.css", self.index)

    def test_warehouse_empty_state_spans_grid_and_period_controls_are_readable(self):
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("body.pf-v19 #view-warehouses #wh_grid > .empty { grid-column:1/-1; }", css)
        self.assertIn("body.pf-v19 #view-warehouses .warehouses-offline-state { grid-column:1/-1;min-height:220px; }", css)
        self.assertIn("body.pf-v19 #view-warehouses #wh_kpis { grid-template-columns:repeat(4,minmax(0,1fr));gap:10px; }", css)
        self.assertIn("body.pf-v19 #view-batches #batch_kpis .kpi,\n  body.pf-v19 #view-warehouses #wh_kpis .kpi { min-height:104px;height:auto;padding:16px 20px; }", css)
        self.assertIn("body.pf-v19 #view-warehouses #turn_period button,\n  body.pf-v19 #view-batches #batch_filter button { min-height:40px;", css)
        self.assertIn("body.pf-v19 #view-warehouses { margin-right:-10px; }", css)
        self.assertIn("body.pf-v19 #view-warehouses > .view-head { margin-bottom:5px; }", css)
        self.assertIn("body.pf-v19 #view-warehouses #wh_kpis .kpi { min-height:118px; }", css)
        self.assertIn("body.pf-v19 #view-warehouses .warehouses-offline-state { min-height:376px; }", css)
        self.assertIn("assets/nozza-design-v2.css", self.index)

    def test_documents_table_keeps_local_scroll_and_click_only_opens(self):
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        js = (SITE / "assets" / "products.js").read_text(encoding="utf-8")
        self.assertIn("body.pf-v19 #view-documents > .table-wrap { overflow-x:auto;overflow-y:hidden;", css)
        self.assertIn("table.data:has(#doc_tbody tr[data-doc]) { min-width:1040px; }", css)
        click_start = js.index("$('doc_tbody').addEventListener('click'")
        click_end = js.index("$('df_add_row').addEventListener", click_start)
        click_handler = js[click_start:click_end]
        self.assertIn("openDoc(row.dataset.doc || row.dataset.docOpen)", click_handler)
        self.assertNotIn("post(", click_handler)
        self.assertIn('aria-label="Открыть документ №${esc(d.number || \'\')}"', js)
        self.assertIn("assets/nozza-design-v2.css", self.index)

    def test_finance_neutral_amounts_do_not_use_positive_green(self):
        money = (SITE / "assets" / "money.js").read_text(encoding="utf-8")
        finance = (SITE / "assets" / "finance.js").read_text(encoding="utf-8")
        self.assertIn("kpi('Доход', money(s.income), `за ${s.period_days} дн.`)", money)
        self.assertIn("kpi('Доход за месяц', money(cur.income), dir('income'))", finance)
        self.assertIn("num(acc.total) < 0 ? 'bad' : ''", finance)
        self.assertIn("kpi('Продано со стеллажа', money(c.shelf_income), 'доход за всю историю')", finance)
        self.assertIn("assets/money.js", self.index)
        self.assertIn("assets/finance.js", self.index)

    def test_calculator_card_polish_targets_wrapped_sections_and_stacks_on_mobile(self):
        self.assertIn("body.pf-v19 #view-calc .calc-inputs > .card::before", self.css)
        self.assertIn("body.pf-v19 #view-calc .calc-results > .card::before", self.css)
        self.assertIn("body.pf-v19 #view-calc .calc-results > .card .card-head h2", self.css)
        mobile = self.css[self.css.index("@media (max-width: 700px)"):]
        self.assertIn("#view-calc .calc-results { grid-template-columns:minmax(0,1fr); }", mobile)
        self.assertIn("#view-calc .calc-results > .calc-result-price { grid-column:auto; }", mobile)
        self.assertIn("assets/v19-shell.css", self.index)

    def test_niche_verdict_distinguishes_no_data_from_views_without_leads(self):
        ops = (SITE / "assets" / "ops.js").read_text(encoding="utf-8")
        verdict = ops[ops.index("function nicheVerdict(n) {"):ops.index("function renderNicheSummary(niches) {")]
        no_data = verdict.index("if (!orders && !leads && !views)")
        views_no_leads = verdict.index("if (!orders && !leads && views)")
        self.assertLess(no_data, views_no_leads)
        self.assertIn("Показы есть (${nfmt(views)}), но обращений пока нет.", verdict)
        self.assertIn("if (!orders) return ['warn'", verdict)
        self.assertIn("assets/ops.js", self.index)

    def test_customer_aftercare_offline_state_is_explicit_and_accessible(self):
        ops = (SITE / "assets" / "ops.js").read_text(encoding="utf-8")
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        start = ops.index("async function loadAftercare() {")
        end = ops.index("function renderAftercareModal(item) {", start)
        handler = ops[start:end]
        self.assertIn("Состояние обратной связи неизвестно без подключения к PrintFlow.", handler)
        self.assertIn('role="status"', handler)
        self.assertIn('role="alert"', handler)
        self.assertIn('kpi.setAttribute(\'aria-label\', \'Счётчики обратной связи неизвестны без подключения\')', handler)
        self.assertIn('Готово <b>—</b>', handler)
        self.assertIn('Ждём ответ <b>—</b>', handler)
        self.assertIn('Получено <b>—</b>', handler)
        self.assertIn("if (!PF.offline) fail(e)", handler)
        self.assertIn("body.pf-v19 #view-customers .aftercare-card.unavailable #aftercare_kpi { min-height:26px; }", css)
        self.assertIn("body.pf-v19 #view-customers > .view-head { align-items:flex-end;margin-bottom:5px; }", css)
        self.assertIn("assets/ops.js", self.index)
        self.assertIn("assets/nozza-design-v2.css", self.index)

    def test_finance_report_offline_state_is_explicit_without_global_toast(self):
        finance = (SITE / "assets" / "finance.js").read_text(encoding="utf-8")
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        start = finance.index("async function refreshReport() {")
        end = finance.index("\n/* ================================================================== P&L */", start)
        handler = finance[start:end]
        self.assertIn("if (!PF.offline) { fail(e); return; }", handler)
        self.assertIn("Отчёт недоступен", handler)
        self.assertIn("Текущие значения неизвестны.", handler)
        self.assertIn("['rep_sales_rows', 10]", handler)
        self.assertIn("body.pf-v19 #finpane-cash.on {", css)
        self.assertNotIn("body.pf-v19 #finpane-cash {\n    display:grid;", css)
        self.assertIn("body.pf-v19 #finpane-cash .v19-fin-attention-card:has(#fin_attention > .empty[role=\"status\"])", css)
        self.assertIn("#fin_attention:has(> .empty[role=\"status\"]) { flex:1;align-content:center; }", css)
        self.assertIn("assets/finance.js", self.index)

    def test_spa_uses_shared_v2_typography_tokens(self):
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("body.pf-v19 .view-head h1 { font-size:28px;line-height:36px; }", css)
        self.assertIn("body.pf-v19 .card-head h2 { font-size:18px;line-height:26px; }", css)
        self.assertIn("body.pf-v19 .view table.data { font-size:14px;line-height:20px; }", css)
        self.assertIn("body.pf-v19 .view .kpi .value { font-size:28px;line-height:34px; }", css)
        self.assertIn("body.pf-v19 .view .btn { min-height:40px;", css)
        self.assertIn("@media screen and (max-width:760px)", css)
        self.assertIn("body.pf-v19 .view .btn.xs,", css)
        self.assertIn("body.pf-v19 #side .nav-link { min-height:44px!important; }", css)

    def test_compact_header_moves_secondary_controls_into_existing_menu(self):
        self.assertIn('id="header_preferences" role="group" aria-label="Настройки шапки"', self.index)
        app = (SITE / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn("window.matchMedia('(max-width:460px)').matches", app)
        self.assertIn("['live_pill', 'sound_btn', 'density_btn', 'theme_btn']", app)
        self.assertIn("preferences.append(item)", app)
        self.assertIn("const anchor = item.id === 'live_pill' ? $('incidents_btn') : $('pf_ai_open')", app)
        self.assertIn("topbar.insertBefore(item, anchor)", app)
        self.assertIn("window.addEventListener('resize'", app)
        self.assertIn('aria-label="Открыть помощника Nozza"', self.index)
        css = (SITE / "assets" / "nozza-design-v2.css").read_text(encoding="utf-8")
        self.assertIn("@media screen and (max-width:460px)", css)
        self.assertIn("body.pf-v19 #topbar #quick_order { width:44px;", css)


    def test_nozza_identity_has_no_legacy_printflow_ai_labels(self):
        self.assertIn('aria-label="Nozza · AI-ассистент PrintFlow"', self.index)
        self.assertIn('title="Nozza"', self.index)
        self.assertIn('aria-label="Закрыть Nozza"', self.index)
        self.assertNotIn("PrintFlow AI", self.index)

    def test_shell_assets_are_in_offline_cache(self):
        self.assertIn("/assets/v19-shell.css", self.sw)
        self.assertIn("/assets/v19-shell.js", self.sw)
        self.assertRegex(self.sw, r"const CACHE = 'printflow-shell-v\d+';")
        self.assertIn("'/assets/nozza-design-v2.css'", self.sw)

    def test_motion_and_small_screen_are_supported(self):
        self.assertIn("prefers-reduced-motion: reduce", self.css)
        self.assertIn("@media (max-width: 700px)", self.css)
        self.assertIn("--pf-ai-w: 398px", self.css)


if __name__ == "__main__":
    unittest.main()

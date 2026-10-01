"""PrintFlow 19 decision-first Finance screen contracts."""
from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SITE = ROOT / "site"


class FinanceV19Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = (SITE / "index.html").read_text(encoding="utf-8")
        cls.js = (SITE / "assets" / "finance.js").read_text(encoding="utf-8")
        cls.css = (SITE / "assets" / "v19-shell.css").read_text(encoding="utf-8")

    def _view(self) -> str:
        return self.index.split('id="view-finance"', 1)[1].split(
            '<!-- ==================================================', 1)[0]

    def test_finance_language_is_decision_first(self):
        view = self._view()
        self.assertIn('Сколько есть сейчас, сколько должны и что требует решения.', view)
        self.assertIn('data-pane="cash" class="on">Деньги сейчас</button>', view)
        self.assertIn('data-pane="profit">Прибыль и P&amp;L</button>', view)
        self.assertIn('data-pane="reports">Налоги и отчёты</button>', view)

    def test_primary_header_keeps_transaction_and_hides_technical_actions_in_more(self):
        view = self._view()
        head = view.split('id="fin_tabs"', 1)[0]
        self.assertIn('id="fin_add"', head)
        self.assertIn('<summary class="btn">Ещё ▾</summary>', head)
        self.assertIn('id="fin_month_close"', head)
        self.assertIn('id="fin_export_tx"', head)
        self.assertIn('href="/bank.html"', head)
        self.assertIn('href="/sbp.html"', head)

    def test_cash_view_exposes_business_control_before_accounting_sources(self):
        view = self._view()
        self.assertIn('id="cash_kpis"', view)
        self.assertIn('id="fin_attention"', view)
        self.assertIn('id="fin_result"', view)
        self.assertIn('class="card v19-fin-debts"', view)
        self.assertIn('class="card v19-fin-details"', view)
        self.assertLess(view.index('id="fin_attention"'), view.index('id="cash_accounts"'))
        self.assertLess(view.index('id="debt_tbody"'), view.index('id="cash_accounts"'))

    def test_financial_attention_uses_authoritative_server_aggregates(self):
        self.assertIn("function renderFinancialAttention(data)", self.js)
        self.assertIn("const debts = data.debts || {}", self.js)
        self.assertIn("const tax = data.tax || {}", self.js)
        self.assertIn("num(debts.overdue)", self.js)
        self.assertIn("num(tax.total_due)", self.js)
        self.assertIn("num(a.balance) < -0.005", self.js)
        self.assertIn("shelfCash.in_shop", self.js)

    def test_attention_actions_navigate_existing_finance_surfaces(self):
        self.assertIn("data-fin-focus", self.js)
        self.assertIn("focus === 'reports'", self.js)
        self.assertIn("focus === 'details'", self.js)
        self.assertIn("focus === 'debts'", self.js)
        self.assertNotIn("/api/assistant", self.js)

    def test_money_channels_are_visible_in_one_finance_surface(self):
        view = self._view()
        self.assertIn('class="card v19-fin-sources"', view)
        self.assertIn('id="fin_sources"', view)
        self.assertIn('id="fin_sources_refresh"', view)
        self.assertIn("get('/api/bank/state')", self.js)
        self.assertIn("get('/api/sbp/state')", self.js)
        self.assertIn("get('/api/cashier/sessions')", self.js)
        self.assertIn("function renderMoneySources()", self.js)
        self.assertIn("'/bank.html'", self.js)
        self.assertIn("'/sbp.html'", self.js)
        self.assertIn("'/cashier.html'", self.js)

    def test_money_channel_pending_items_join_financial_attention(self):
        self.assertIn("moneySources.bank.pending_review", self.js)
        self.assertIn("moneySources.sbp.pending", self.js)
        self.assertIn("'sources'", self.js)
        self.assertIn(".v19-fin-sources", self.js)

    def test_finance_layout_is_responsive(self):
        self.assertIn(".v19-fin-kpis", self.css)
        self.assertIn(".v19-fin-attention", self.css)
        self.assertIn(".v19-fin-details", self.css)
        self.assertIn(".v19-fin-source-grid", self.css)
        self.assertIn("@media (max-width: 980px)", self.css)
        self.assertIn("@media (max-width: 600px)", self.css)


if __name__ == "__main__":
    unittest.main()

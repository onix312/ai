"""PrintFlow 19 lifecycle-first Orders screen contracts."""
from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SITE = ROOT / "site"


class OrdersV19Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = (SITE / "index.html").read_text(encoding="utf-8")
        cls.js = (SITE / "assets" / "ops.js").read_text(encoding="utf-8")
        cls.css = (SITE / "assets" / "v19-shell.css").read_text(encoding="utf-8")

    def _view(self) -> str:
        return self.index.split('id="view-orders"', 1)[1].split(
            '<!-- ================================================== КЛИЕНТЫ', 1)[0]

    def test_primary_header_is_create_first(self):
        view = self._view()
        head = view.split('id="orders_pulse"', 1)[0]
        self.assertIn('id="orders_new"', head)
        self.assertIn('<summary class="btn">Ещё ▾</summary>', head)
        self.assertIn('id="orders_wishes"', head)
        self.assertIn('id="orders_statuses"', head)
        self.assertIn('id="orders_export"', head)

    def test_lifecycle_pulse_has_actionable_views(self):
        view = self._view()
        for key in ("all", "hot", "ready", "debt", "stale"):
            self.assertIn(f'data-order-pulse="{key}"', view)
            self.assertIn(f'id="orders_pulse_{key}"', view)
        self.assertLess(view.index('id="orders_pulse"'), view.index('id="orders_search"'))

    def test_ready_is_a_real_system_preset(self):
        self.assertIn("{ id: 'ready', name: 'Готовы к выдаче'", self.js)
        self.assertIn("filters.extra === 'ready'", self.js)
        self.assertIn("function orderReady(o)", self.js)
        self.assertIn("name.includes('готов')", self.js)

    def test_pulse_reuses_existing_preset_pipeline(self):
        self.assertIn("function renderOrderPulse()", self.js)
        self.assertIn("presetMatchesFilters(button.dataset.orderPulse || 'all')", self.js)
        self.assertIn("applyPreset(button.dataset.orderPulse || 'all')", self.js)
        self.assertIn("if (PF.orderBox === 'archived') await setOrderBox('')", self.js)

    def test_advanced_controls_are_grouped(self):
        view = self._view()
        details = view.split('id="orders_more_filters"', 1)[1].split('</details>', 1)[0]
        self.assertIn('id="orders_filter_status"', details)
        self.assertIn('id="orders_box"', details)
        self.assertIn('id="orders_filter_niche"', details)
        self.assertIn('id="orders_sort"', details)
        self.assertIn('id="orders_chan"', details)
        self.assertIn('id="orders_density"', details)

    def test_orders_layout_is_responsive(self):
        self.assertIn(".v19-orders-pulse", self.css)
        self.assertIn(".v19-orders-toolbar", self.css)
        self.assertIn("@media (max-width: 1050px)", self.css)
        self.assertIn("@media (max-width: 760px)", self.css)


if __name__ == "__main__":
    unittest.main()

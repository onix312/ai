"""PrintFlow 19 stock-family and retail Shelf contracts."""
from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SITE = ROOT / "site"


class StockV19Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = (SITE / "index.html").read_text(encoding="utf-8")
        cls.core = (SITE / "assets" / "core.js").read_text(encoding="utf-8")
        cls.shelf = (SITE / "assets" / "shelf.js").read_text(encoding="utf-8")
        cls.css = (SITE / "assets" / "v19-shell.css").read_text(encoding="utf-8")

    def test_stock_tabs_put_daily_surfaces_first(self):
        block = self.core.split("const STOCK_TABS = [", 1)[1].split("];", 1)[0]
        order = [block.index(f"id: '{name}'") for name in
                 ("products", "shelf", "inventory", "batches", "warehouses", "documents")]
        self.assertEqual(order, sorted(order))
        self.assertIn("{ id: 'documents', label: 'Операции'", block)

    def test_shelf_has_independent_retail_language_and_simple_header(self):
        view = self.index.split('id="view-shelf"', 1)[1].split(
            'id="view-inventory"', 1)[0]
        self.assertIn("Розница · отдельный регистр", view)
        self.assertNotIn("полка NOZZA", view)
        self.assertIn('id="shelf_transfer_btn"', view)
        self.assertIn('id="shelf_add"', view)
        self.assertIn('<summary class="btn">Ещё ▾</summary>', view)

    def test_shelf_pulse_reuses_existing_status_filter(self):
        view = self.index.split('id="view-shelf"', 1)[1].split(
            'id="view-inventory"', 1)[0]
        for key in ("", "low", "empty", "needs", "dead"):
            self.assertIn(f'data-shelf-pulse="{key}"', view)
        self.assertIn("function renderShelfPulse()", self.shelf)
        self.assertIn("select.value = button.dataset.shelfPulse || ''", self.shelf)
        self.assertIn("applyShelfFilter()", self.shelf)

    def test_retail_secondary_tools_are_collapsed_below_cards(self):
        view = self.index.split('id="view-shelf"', 1)[1].split(
            'id="view-inventory"', 1)[0]
        self.assertLess(view.index('id="shelf_grid"'), view.index('id="shelf_groups_widget"'))
        self.assertLess(view.index('id="shelf_grid"'), view.index('id="shelf_cash_widget"'))
        self.assertIn("v19-shelf-secondary", view)
        self.assertIn("v19-shelf-moves", view)

    def test_products_keep_daily_actions_primary(self):
        view = self.index.split('id="view-products"', 1)[1].split(
            'id="view-batches"', 1)[0]
        head = view.split('class="toolbar v19-products-toolbar"', 1)[0]
        self.assertIn('id="prod_plan_btn"', head)
        self.assertIn('id="prod_add"', head)
        self.assertIn('<summary class="btn">Ещё ▾</summary>', head)
        toolbar = view.split('class="toolbar v19-products-toolbar"', 1)[1]
        self.assertIn('id="prod_warehouse"', toolbar)
        self.assertIn('id="prod_shelf3d"', toolbar)
        self.assertIn('id="prod_season"', toolbar)
        self.assertIn('id="prod_shop_eye"', toolbar)

    def test_stock_family_layout_is_responsive(self):
        self.assertIn("body.pf-v19 .stock-tabs", self.css)
        self.assertIn(".v19-shelf-pulse", self.css)
        self.assertIn(".v19-shelf-secondary", self.css)
        self.assertIn(".v19-products-toolbar", self.css)
        self.assertIn("@media (max-width: 620px)", self.css)


if __name__ == "__main__":
    unittest.main()

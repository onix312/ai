from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.analytics import Analytics  # noqa: E402
from connector.printflow.db import Database  # noqa: E402


class ProductIdeasTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.analytics = Analytics(self.db)
        self.db.upsert("nomenclature", {
            "id": "product-1", "name": "Органайзер", "kind": "product",
            "grams": 40, "hours": 2, "archived": 0,
        })
        now = datetime.now()
        for index, age in enumerate((10, 12, 45)):
            self.db.upsert("stock_moves", {
                "id": f"sale-{index}", "at": (now - timedelta(days=age)).isoformat(),
                "doc_kind": "sale", "nom_id": "product-1", "qty": -1,
            })

    def test_compares_selected_window_with_previous_equal_window(self):
        data = self.analytics.product_opportunities(30)
        product = data["products"][0]

        self.assertEqual(30, data["source"]["window_days"])
        self.assertEqual(2, product["sold_period"])
        self.assertEqual(1, product["sold_previous"])
        self.assertEqual(100, product["change_pct"])
        self.assertEqual("rising", product["trend"])


if __name__ == "__main__":
    unittest.main()

"""PrintFlow 19 screen-context contract for Luma -> Nozza."""
from __future__ import annotations

import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.router import Ctx  # noqa: E402
from connector.printflow.routes_assistant import (  # noqa: E402
    assistant_ui_context_get,
    assistant_ui_context_set,
)


class UiContextRouteTests(unittest.TestCase):
    def setUp(self):
        self.api = types.SimpleNamespace()

    def test_context_is_ephemeral_and_sanitized(self):
        result = assistant_ui_context_set(self.api, Ctx(body={
            "view": "shelf",
            "sub": "retail",
            "entity_type": "shelf_item",
            "entity_id": "shf_123",
            "filters": {
                "status": "low",
                "linked": True,
                "unsafe": {"nested": "drop"},
            },
            "dirty": False,
        }))
        self.assertTrue(result["ok"])
        ctx = result["context"]
        self.assertEqual("shelf", ctx["view"])
        self.assertEqual("shf_123", ctx["entity_id"])
        self.assertEqual({"status": "low", "linked": True}, ctx["filters"])
        self.assertTrue(ctx["updated_at"])
        self.assertEqual(ctx, assistant_ui_context_get(
            self.api, Ctx())["context"])

    def test_empty_process_has_empty_context(self):
        self.assertEqual({}, assistant_ui_context_get(
            types.SimpleNamespace(), Ctx())["context"])


class UiContextFrontendContractTests(unittest.TestCase):
    def test_core_publishes_view_and_modules_publish_selected_entity(self):
        core = (ROOT / "site/assets/core.js").read_text(encoding="utf-8")
        orders = (ROOT / "site/assets/ops.js").read_text(encoding="utf-8")
        printers = (ROOT / "site/assets/printer.js").read_text(encoding="utf-8")
        shelf = (ROOT / "site/assets/shelf.js").read_text(encoding="utf-8")

        self.assertIn("PF.setAssistantContext", core)
        self.assertIn("/api/assistant/ui-context", core)
        self.assertIn("entity_type: id ? 'order' : 'order_draft'", orders)
        self.assertIn("entity_type: 'printer'", printers)
        self.assertIn("entity_type: id ? 'shelf_item' : 'shelf_item_draft'", shelf)


if __name__ == "__main__":
    unittest.main()

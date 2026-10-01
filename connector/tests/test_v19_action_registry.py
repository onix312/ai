"""PrintFlow 19 domain action registry contracts."""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from connector.printflow import action_registry
from connector.printflow.api import register_routes, router


def server_routes() -> set[tuple[str, str]]:
    register_routes()
    known = {(row["method"], row["path"]) for row in router.reference()}
    for name in ("api.py", "http_handler.py"):
        method = "GET"
        source = (ROOT / "connector" / "printflow" / name).read_text(encoding="utf-8")
        for line in source.splitlines():
            head = re.match(r"    def (get|post)\(", line)
            if head:
                method = head.group(1).upper()
            if re.search(r"if path (==|in )", line):
                for path in re.findall(r'"(/api/[^"]+)"', line):
                    known.add((method, path))
    return known


class PrintFlowV19ActionRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.routes = server_routes()

    def test_registry_is_internally_valid(self):
        self.assertEqual([], action_registry.validate())
        self.assertGreaterEqual(len(action_registry.ACTIONS), 50)

    def test_every_action_points_to_a_real_server_route(self):
        missing = []
        for action_id, spec in action_registry.ACTIONS.items():
            pair = (spec["method"], spec["path"])
            if pair not in self.routes:
                missing.append((action_id, *pair))
        self.assertEqual([], missing)

    def test_dangerous_actions_are_server_confirmed(self):
        dangerous = {"physical", "financial", "irreversible"}
        for action_id, spec in action_registry.ACTIONS.items():
            if spec["risk"] in dangerous:
                self.assertTrue(spec["confirm"], action_id)
                self.assertEqual("POST", spec["method"], action_id)

    def test_registry_covers_core_v19_domains(self):
        domains = {spec["domain"] for spec in action_registry.ACTIONS.values()}
        expected = {
            "today", "system", "printers", "queue", "orders", "customers",
            "shelf", "products", "stock", "materials", "finance",
            "analytics", "inbox", "farmloop",
        }
        self.assertTrue(expected.issubset(domains), sorted(expected - domains))

    def test_shelf_is_separate_but_has_both_transfer_directions(self):
        incoming = action_registry.get("shelf.transfer_in")
        outgoing = action_registry.get("shelf.transfer_out")
        self.assertEqual("/api/shelf/transfer", incoming["path"])
        self.assertEqual("/api/shelf/transfer-out", outgoing["path"])
        self.assertEqual("shelf_stock_transfer", incoming["verify"])
        self.assertEqual("shelf_stock_transfer", outgoing["verify"])

    def test_model_never_supplies_raw_url(self):
        for action_id, spec in action_registry.ACTIONS.items():
            self.assertNotIn("path", spec["params"], action_id)
            self.assertNotIn("url", spec["params"], action_id)


if __name__ == "__main__":
    unittest.main()

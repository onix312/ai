"""PrintFlow 19 read -> act -> verify contracts."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.assistant_verify import verify_action  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402
from connector.printflow.shelf import Shelf  # noqa: E402
from connector.printflow.stock import Stock  # noqa: E402


_held: list[tempfile.TemporaryDirectory] = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "verify.sqlite3")


class FakePrinter:
    def __init__(self, state: str):
        self.state = state

    def snapshot(self):
        return {"printer": {"state": self.state}}


class FakeManager:
    def __init__(self, state: str):
        self.printer = FakePrinter(state)

    def get(self, printer_id: str):
        return self.printer if printer_id == "p1" else None


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.repo = Repo(self.db)
        self.api = types.SimpleNamespace(
            db=self.db, repo=self.repo, manager=FakeManager("PAUSED"))
        self.api.shelf = Shelf(self.db)

    def tearDown(self):
        self.db.close()

    def test_printer_command_can_be_verified_or_pending(self):
        verified = verify_action(
            self.api, "printer_command",
            {"printer_id": "p1", "command": "pause"}, {"ok": True})
        self.assertTrue(verified["verified"])
        self.assertEqual("verified", verified["state"])

        self.api.manager.printer.state = "RUNNING"
        pending = verify_action(
            self.api, "printer_command",
            {"printer_id": "p1", "command": "pause"}, {"ok": True})
        self.assertFalse(pending["verified"])
        self.assertEqual("pending", pending["state"])

    def test_order_status_is_checked_against_database(self):
        order = self.repo.save_order({
            "product": "Ваза", "customer_name": "Мария", "status": "new",
        })
        self.db.execute("UPDATE orders SET status='queue' WHERE id=?", (order["id"],))
        good = verify_action(
            self.api, "order_status",
            {"id": order["id"], "status": "queue"}, {"ok": True})
        self.assertTrue(good["verified"])

        bad = verify_action(
            self.api, "order_status",
            {"id": order["id"], "status": "printing"}, {"ok": True})
        self.assertEqual("failed", bad["state"])
        self.assertFalse(bad["verified"])

    def test_shelf_transfer_readback_checks_retail_register_sync(self):
        self.db.upsert("nomenclature", {
            "id": "nom1", "name": "Органайзер", "kind": "product",
            "unit": "шт", "archived": 0,
        })
        stock = Stock(self.db)
        stock.add_move("nom1", "home", 5, 500, doc_kind="receipt")
        moved = self.api.shelf.transfer_from_stock("nom1", "home", 3)
        result = self.api.shelf.transfer_to_stock(
            moved["item"]["id"], "home", 1, "вернуть")
        checked = verify_action(
            self.api, "shelf_transfer_out",
            {"item_id": moved["item"]["id"], "warehouse_id": "home", "qty": 1},
            result)
        self.assertTrue(checked["verified"])
        self.assertTrue(checked["evidence"]["register_synced"])
        self.assertEqual(2.0, checked["evidence"]["shelf_qty"])

    def test_unknown_action_is_not_verified(self):
        result = verify_action(self.api, "made_up_action", {}, {})
        self.assertEqual("failed", result["state"])
        self.assertFalse(result["verified"])


if __name__ == "__main__":
    unittest.main()

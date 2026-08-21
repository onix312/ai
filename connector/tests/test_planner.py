"""Тесты мастера-плана производства (PrintFlow 4.0).

Проверяют только логику сборки плана — без принтера и сети: какие заказы
попадают в план, как сортируются, как считается загрузка и пластик.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow.planner import Planner  # noqa: E402


def _order(db: Database, oid: str, **overrides) -> None:
    row = {
        "id": oid, "number": oid[-4:], "product": "Изделие " + oid,
        "status": "queue", "priority": "normal", "qty": 1,
        "hours": 2, "grams": 40, "material": "PLA", "file": "x.3mf",
        "due": "2026-08-20", "created_at": "2026-08-10T10:00:00+03:00",
    }
    row.update(overrides)
    db.upsert("orders", row)


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "test.sqlite3")
        self.planner = Planner(self.db)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_open_order_lands_in_plan(self):
        _order(self.db, "ord-0001", hours=3, due="2026-08-20")
        plan = self.planner.day_plan()
        self.assertEqual(plan["orders_to_print"], 1)
        self.assertEqual(plan["sequence"][0]["id"], "ord-0001")
        self.assertEqual(plan["sequence"][0]["kind"], "order")

    def test_preproduction_and_finished_orders_are_skipped(self):
        # заявка до производства — не печатаем
        _order(self.db, "ord-new", status="new", hours=3)
        # заказ, который уже напечатан (есть завершённое задание) — не повторяем
        _order(self.db, "ord-done", status="post", hours=3)
        self.db.upsert("print_jobs", {"id": "job-1", "order_id": "ord-done",
                                      "state": "done", "name": "done"})
        plan = self.planner.day_plan()
        self.assertEqual(plan["orders_to_print"], 0)

    def test_orders_sorted_by_due_then_priority(self):
        _order(self.db, "ord-late", due="2026-08-22", priority="normal", hours=1)
        _order(self.db, "ord-early", due="2026-08-18", priority="low", hours=1)
        plan = self.planner.day_plan()
        order_ids = [t["id"] for t in plan["sequence"] if t["kind"] == "order"]
        self.assertEqual(order_ids, ["ord-early", "ord-late"])

    def test_in_progress_hours_from_queue(self):
        _order(self.db, "ord-q", hours=4)
        self.db.upsert("print_jobs", {"id": "job-2", "order_id": "ord-q",
                                      "state": "queued", "est_minutes": 90})
        plan = self.planner.day_plan()
        self.assertEqual(plan["in_progress_hours"], 1.5)

    def test_overload_verdict(self):
        self.db.set_settings({"weekly_capacity_hours": 1})
        _order(self.db, "ord-big", hours=5)
        plan = self.planner.day_plan()
        self.assertEqual(plan["verdict"], "bad")
        self.assertGreater(plan["overload_hours"], 0)

    def test_filament_shortage_flags_issue(self):
        _order(self.db, "ord-fil", hours=1, grams=500, material="PLA")
        plan = self.planner.day_plan()
        task = next(t for t in plan["sequence"] if t["id"] == "ord-fil")
        self.assertFalse(task["ready"])
        self.assertTrue(any(i["kind"] == "filament" for i in task["issues"]))

    def test_missing_file_flags_issue(self):
        _order(self.db, "ord-nofile", hours=1, file="")
        plan = self.planner.day_plan()
        task = next(t for t in plan["sequence"] if t["id"] == "ord-nofile")
        self.assertTrue(any(i["kind"] == "file" for i in task["issues"]))

    def test_empty_plan_is_ok(self):
        plan = self.planner.day_plan()
        self.assertEqual(plan["verdict"], "ok")
        self.assertEqual(plan["sequence"], [])
        self.assertIsNone(plan["suggested_next"])

    def test_multi_order_plate_grams_not_multiplied_by_qty(self):
        # Мультизаказ: grams/hours — вся плита, qty — сумма единиц по позициям.
        _order(self.db, "ord-multi", qty=7, hours=2.0, grams=213.0)
        self.db.upsert("order_items", {
            "id": "it-1", "order_id": "ord-multi", "position": 1,
            "nom_id": "", "name": "Смок-адресник", "qty": 3,
            "price": 350.0, "grams": 40.0, "hours": 0.0,
        })
        plan = self.planner.day_plan()
        task = next(t for t in plan["sequence"] if t["id"] == "ord-multi")
        # Вся плита, а не 213×7=1491 г и не 2×7=14 ч.
        self.assertEqual(task["grams"], 213.0)
        self.assertEqual(task["hours"], 2.0)

    def test_multi_order_in_progress_hours_not_multiplied(self):
        _order(self.db, "ord-multi-q", qty=5, hours=3.0)
        self.db.upsert("order_items", {
            "id": "it-2", "order_id": "ord-multi-q", "position": 1,
            "nom_id": "", "name": "Крючок", "qty": 5, "price": 120.0,
            "grams": 12.0, "hours": 0.0,
        })
        # Задание в очереди без оценки слайсера: часы заказа — вся плита.
        self.db.upsert("print_jobs", {"id": "job-multi", "order_id": "ord-multi-q",
                                      "state": "queued", "est_minutes": 0})
        plan = self.planner.day_plan()
        self.assertEqual(plan["in_progress_hours"], 3.0)


if __name__ == "__main__":
    unittest.main()

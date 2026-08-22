"""Регрессия: нельзя привязать печать к закрытому (финальному) заказу.

Пользовательский баг: при перепривязке можно было попасть на закрытый
заказ и «оживить» его (вернуть в статус печати). Привязка должна
отклоняться, а не возвращать заказ в работу.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.config import now_iso  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.manager import PrinterManager  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402
from connector.tests.test_auto_resume_and_order import MockPrinter  # noqa: E402


class OrderLinkGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "link.sqlite3")
        self.repo = Repo(self.db)
        self.manager = PrinterManager(self.db, self.repo)
        self.pr = MockPrinter("pr_link", "P1S", "RUNNING", "Bowl.3mf")
        self.manager.printers["pr_link"] = self.pr
        self.db.upsert("orders", {
            "id": "o-closed", "number": "9001", "product": "Закрытый заказ",
            "status": "done", "quality": "passed", "price": 100, "paid": 100,
            "created_at": now_iso(), "updated_at": now_iso(), "closed_at": now_iso(),
        })
        self.db.upsert("orders", {
            "id": "o-open", "number": "9002", "product": "Активный заказ",
            "status": "queue", "price": 200,
            "created_at": now_iso(), "updated_at": now_iso(),
        })

    def tearDown(self):
        self.manager.shutdown()
        self.db.close()
        self.tmp.cleanup()

    def test_link_job_to_closed_order_is_rejected(self):
        job = self.manager.enqueue({
            "name": "Bowl.3mf", "file": "Bowl.3mf", "printer_id": "pr_link",
        })
        with self.assertRaisesRegex(ValueError, "закрыт"):
            self.manager.link_job_to_order(job["id"], "o-closed")
        # Заказ остался закрытым, задание не привязалось.
        row = self.db.one("SELECT status FROM orders WHERE id='o-closed'")
        self.assertEqual(row["status"], "done")
        job_row = self.db.one("SELECT order_id FROM print_jobs WHERE id=?", (job["id"],))
        self.assertIn(job_row["order_id"], (None, ""))

    def test_link_active_to_closed_order_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "закрыт"):
            self.manager.link_active_to_order("pr_link", "o-closed")
        row = self.db.one("SELECT status FROM orders WHERE id='o-closed'")
        self.assertEqual(row["status"], "done")

    def test_link_job_to_open_order_succeeds(self):
        job = self.manager.enqueue({
            "name": "Bowl.3mf", "file": "Bowl.3mf", "printer_id": "pr_link",
        })
        # Реальная перепривязка происходит для печати в процессе: задание
        # уже запущено и физически печатается.
        self.db.upsert("print_jobs", {**job, "state": "running"})
        res = self.manager.link_job_to_order(job["id"], "o-open")
        self.assertTrue(res["ok"])
        row = self.db.one("SELECT status FROM orders WHERE id='o-open'")
        # Активное задание на принтере переводит заказ в печать.
        self.assertEqual(row["status"], "printing")
        job_row = self.db.one("SELECT order_id FROM print_jobs WHERE id=?", (job["id"],))
        self.assertEqual(job_row["order_id"], "o-open")

    def test_queue_hides_jobs_of_final_orders(self):
        # «Печать, которая уже закончилась»: задание числится running, но
        # привязано к закрытому заказу. Такое задание не должно висеть в очереди.
        job = self.manager.enqueue({
            "name": "Bowl.3mf", "file": "Bowl.3mf", "printer_id": "pr_link",
        })
        self.db.upsert("print_jobs", {**job, "order_id": "o-closed", "state": "running"})
        # Незакрытый заказ — виден.
        open_id = next((o["id"] for o in self.manager.queue() if o["order_id"] == "o-open"), None)
        self.assertIsNone(open_id)  # o-open пока не привязан
        self.assertTrue(all(j["order_id"] != "o-closed" for j in self.manager.queue()))


if __name__ == "__main__":
    unittest.main()

"""Финиш печати и приёмка заказа: идемпотентный факт и безопасный текст."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting, num
from connector.printflow.completion import OrderCompletion
from connector.printflow.config import now_iso
from connector.printflow.db import Database
from connector.printflow.manager import PrinterManager
from connector.printflow.repo import Repo


class OrderCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "completion.sqlite3")
        self.repo = Repo(self.db)
        self.acc = Accounting(self.db)
        self.completion = OrderCompletion(self.db, self.repo)
        self.db.upsert("printers", {
            "id": "printer-1", "name": "P1S", "model": "P1S", "enabled": 0,
        })
        self.db.upsert("spools", {
            "id": "spool-1", "material": "PETG", "color_name": "Чёрный",
            "total_grams": 1000, "remaining_grams": 500, "price": 1500,
            "archived": 0, "created_at": now_iso(),
        })

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def order(self, order_id="order-1", **overrides):
        data = {
            "id": order_id, "number": "1001", "product": "Адресник",
            "customer_name": "Мария", "status": "post", "qty": 2,
            "grams": 50, "hours": 0.5, "cost": 140, "price": 1000,
            "paid": 300, "material": "PETG", "auto_cost": 1,
            "spools": json.dumps([{"spool_id": "spool-1", "grams": 100}]),
            "created_at": now_iso(), "updated_at": now_iso(),
        }
        data.update(overrides)
        return self.db.upsert("orders", data)

    def job(self, job_id="job-1", **overrides):
        data = {
            "id": job_id, "printer_id": "printer-1", "order_id": "order-1",
            "name": "tag.3mf", "file": "tag.3mf", "state": "running",
            "spool_id": "spool-1", "started_at": now_iso(), "created_at": now_iso(),
        }
        data.update(overrides)
        return self.db.upsert("print_jobs", data)

    def manager_without_threads(self):
        manager = PrinterManager.__new__(PrinterManager)
        manager.db = self.db
        manager.repo = self.repo
        manager.acc = self.acc
        manager.batches = None
        manager.guard = mock.Mock()
        manager.get = mock.Mock(return_value=None)
        manager._maybe_start_next = mock.Mock()
        return manager

    def test_finalize_is_atomic_and_idempotent_before_any_second_write(self):
        self.order(status="printing")
        job = self.job()
        manager = self.manager_without_threads()

        first = manager._finalize_job(job, "done", "complete", 60, 100, {"progress": 100})
        second = manager._finalize_job(job, "done", "complete", 60, 100, {"progress": 100})

        self.assertTrue(first["accounted_at"])
        self.assertEqual(second["accounted_at"], first["accounted_at"])
        self.assertEqual(num(self.db.one("SELECT remaining_grams FROM spools WHERE id='spool-1'")["remaining_grams"]), 400)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM filament_usage WHERE job_id='job-1'")["n"], 1)
        order = self.db.one("SELECT * FROM orders WHERE id='order-1'")
        self.assertEqual(num(order["actual_grams"]), 100)
        self.assertEqual(num(order["actual_hours"]), 1)
        self.assertEqual(order["status"], "post")
        stats = self.db.one("SELECT * FROM printer_stats WHERE printer_id='printer-1'")
        self.assertEqual(stats["jobs_done"], 1)
        manager.guard.add_runtime.assert_called_once()

    def test_failed_accounting_rolls_job_state_back_for_safe_retry(self):
        self.order(status="printing")
        job = self.job()
        manager = self.manager_without_threads()
        manager.acc = mock.Mock()
        manager.acc.register_job_costs.side_effect = RuntimeError("ledger unavailable")

        with self.assertRaisesRegex(RuntimeError, "ledger unavailable"):
            manager._finalize_job(job, "done", "complete", 60, 100, {"progress": 100})

        stored = self.db.one("SELECT * FROM print_jobs WHERE id='job-1'")
        self.assertEqual(stored["state"], "running")
        self.assertFalse(stored["accounted_at"])
        self.assertEqual(num(self.db.one("SELECT remaining_grams FROM spools WHERE id='spool-1'")["remaining_grams"]), 500)

    def test_repeated_printer_finish_with_same_task_does_not_create_job(self):
        self.order(status="printing")
        self.job(remote_task_id="bambu-42")
        manager = self.manager_without_threads()
        payload = {"remote_task_id": "bambu-42", "duration_min": 20, "weight": 100}

        manager._on_print_end("printer-1", "complete", "tag.3mf", payload)
        manager._on_print_end("printer-1", "complete", "tag.3mf", payload)

        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM print_jobs")["n"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM filament_usage")["n"], 1)

    def test_summary_blocks_active_or_missing_successful_jobs(self):
        self.order()
        self.job(state="running")
        summary = self.completion.summary("order-1")
        self.assertFalse(summary["can_accept"])
        self.assertEqual({item["code"] for item in summary["blocks"]},
                         {"active_jobs", "successful_job"})
        with self.assertRaisesRegex(ValueError, "Нельзя принять"):
            self.completion.accept("order-1", quality_confirmed=True)

    def test_leftover_queue_does_not_block_accept(self):
        self.order()
        self.job(state="done", duration_min=30, grams=100, cost=150,
                 finished_at=now_iso(), accounted_at=now_iso())
        self.job(job_id="job-queued", state="queued")
        summary = self.completion.summary("order-1")
        self.assertTrue(summary["can_accept"])
        self.completion.accept("order-1", quality_confirmed=True)
        leftover = self.db.one("SELECT state FROM print_jobs WHERE id='job-queued'")
        self.assertEqual(leftover["state"], "cancelled")

    def test_accept_requires_visual_confirmation_and_is_idempotent(self):
        self.order(qc_done=json.dumps({"0": True, "1": True, "2": True, "3": True}))
        self.job(state="done", duration_min=60, grams=105, cost=175,
                 finished_at=now_iso(), accounted_at=now_iso())
        with self.assertRaisesRegex(ValueError, "визуальную"):
            self.completion.accept("order-1")

        first = self.completion.accept("order-1", quality_confirmed=True)
        second = self.completion.accept("order-1", quality_confirmed=True)

        self.assertTrue(first["accepted"])
        self.assertFalse(first["already_accepted"])
        self.assertTrue(second["already_accepted"])
        self.assertFalse(first["external_sent"])
        self.assertIn("Мария", first["message"])
        self.assertIn("700", first["message"])
        order = self.db.one("SELECT status,quality FROM orders WHERE id='order-1'")
        self.assertEqual(order, {"status": "ready", "quality": "passed"})
        events = self.db.one(
            "SELECT COUNT(*) n FROM events WHERE title='Результат принят — заказ готов'"
        )
        self.assertEqual(events["n"], 1)

    def test_ready_reply_template_is_previewed_without_sending(self):
        self.order()
        self.job(state="done", duration_min=30, grams=100, cost=150,
                 finished_at=now_iso(), accounted_at=now_iso())
        self.db.set_settings({
            "reply_templates": [{
                "id": "ready-client", "title": "Заказ готов",
                "text": "{name}, заказ №{number} готов. Остаток {remaining} ₽",
            }],
        })
        summary = self.completion.summary("order-1")
        self.assertEqual(summary["message_source"], "template")
        self.assertEqual(summary["message"], "Мария, заказ №1001 готов. Остаток 700 ₽")
        self.assertFalse(summary["external_sent"])


if __name__ == "__main__":
    unittest.main()

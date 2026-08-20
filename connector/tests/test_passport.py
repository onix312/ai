"""Паспорт печати (B.1.2): план против факта, сторож, фото, расшифровка."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow.passport import job_passport  # noqa: E402


class PassportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.upsert("orders", {
            "id": "o1", "number": "1001", "product": "адресник",
            "customer_name": "Мария", "phone": "+7", "price": 900,
            "status": "done", "created_at": "2026-08-10T10:00:00",
            "updated_at": "2026-08-10T10:00:00"})
        self.db.upsert("print_jobs", {
            "id": "j1", "printer_id": "p1", "order_id": "o1",
            "name": "адресник", "file": "нет-файла.3mf", "state": "done",
            "result": "done", "duration_min": 66, "grams": 22,
            "started_at": "2026-08-10T11:00:00",
            "finished_at": "2026-08-10T12:06:00",
            "queued_at": "2026-08-10T10:30:00"})
        self.db.add_event("guard", "Сторож: перерасход", "пластика больше плана",
                          "p1", {"actions": ["сохранён кадр"]})
        # Событие должно попасть в окно печати задания
        self.db.execute(
            "UPDATE events SET at='2026-08-10T11:30:00' WHERE kind='guard'")
        self.db.upsert("order_photos", {
            "id": "ph1", "order_id": "o1", "at": "2026-08-10T12:07:00",
            "file": "order_o1.jpg", "note": "", "kind": "upload"})

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_missing_job_raises(self):
        with self.assertRaises(ValueError):
            job_passport(self.db, "нет-такого")

    def test_gathers_order_guard_photos(self):
        passport = job_passport(self.db, "j1")
        self.assertEqual(passport["job"]["name"], "адресник")
        self.assertEqual(passport["order"]["number"], "1001")
        self.assertEqual(len(passport["guard"]), 1)
        self.assertEqual(len(passport["photos"]), 1)
        # Событие сторожа попало в окно печати
        self.assertEqual(passport["guard"][0]["data"]["actions"],
                         ["сохранён кадр"])

    def test_plan_vs_fact_without_slicer_estimate(self):
        passport = job_passport(self.db, "j1")
        pvf = passport["plan_vs_fact"]
        # Файла нет — плана нет, но факт посчитан
        self.assertEqual(pvf["grams"]["fact"], 22)
        self.assertEqual(pvf["minutes"]["fact"], 66)
        self.assertEqual(pvf["minutes"]["verdict"], "сметы не было")

    def test_error_decoded_for_known_code(self):
        self.db.execute("UPDATE print_jobs SET error='0300-4006' WHERE id='j1'")
        passport = job_passport(self.db, "j1")
        decoded = passport["error_decoded"]
        self.assertTrue(decoded.get("title"), "известный код должен расшифроваться")


if __name__ == "__main__":
    unittest.main()

"""Катушка задания печати: выбор до старта, а не «какая стоит в AMS» (17.0.26).

Раньше катушку можно было указать только в модалке запуска файла. Задание,
поставленное в очередь из заказа, оставалось без неё — и к завершению пластик
списывался с той катушки, что оказалась в активном слоте AMS. Переставили
катушки местами — расход ушёл на чужой материал, а «печать без катушки»
всплывала уже после финиша, в сводке проблем.

Здесь три вещи под контрактом: выбор хранится на самом задании, завершённое
задание выбор не меняет, и списание действительно идёт с выбранной катушки,
даже когда в AMS стоит другая.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting, num  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.manager import PrinterManager  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402


class FakePrinter:
    """Принтер на связи: в активном слоте AMS стоит «чужая» катушка."""

    def __init__(self, printer_id: str = "prn1"):
        self.id = printer_id
        self.connected = True

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "printer": {"state": "FINISH"},
            "ams": {"trays": [{"slot": "0", "label": "Слот 1", "type": "PLA",
                               "uuid": "uuid-pla-black", "active": True,
                               "present": True}]},
        }


class JobSpoolCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(pathlib.Path(self.tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.db.close)
        self.repo = Repo(self.db)
        self.acc = Accounting(self.db)
        self.manager = PrinterManager(self.db, self.repo)

    # ------------------------------------------------------------- фикстуры
    def spool(self, sid: str, *, material: str = "PLA", color: str = "чёрный",
              grams: float = 1000.0, price: float = 1600.0, **extra) -> dict:
        return self.db.upsert("spools", {
            "id": sid, "material": material, "color_name": color,
            "price": price, "total_grams": 1000.0, "remaining_grams": grams,
            "archived": 0, "verified": 1, **extra})

    def job(self, jid: str = "job1", **fields) -> dict:
        base = {"id": jid, "name": "Адресник", "file": "dog.3mf",
                "printer_id": "prn1", "state": "queued",
                "est_grams": 120.0, "grams": 0.0}
        base.update(fields)
        return self.db.upsert("print_jobs", base)


class JobSpoolChoiceTests(JobSpoolCase):
    def test_choice_is_stored_on_the_job(self):
        self.spool("sp-a", material="PETG", color="зелёный", grams=640)
        self.job()
        res = self.manager.set_job_spool("job1", "sp-a")
        self.assertEqual("sp-a", res["job"]["spool_id"])
        self.assertEqual("sp-a", self.db.one(
            "SELECT spool_id FROM print_jobs WHERE id=?", ("job1",))["spool_id"])
        self.assertEqual("PETG", res["spool"]["material"])

    def test_empty_choice_returns_to_ams(self):
        self.spool("sp-a")
        self.job(spool_id="sp-a")
        res = self.manager.set_job_spool("job1", "")
        self.assertFalse(res["job"].get("spool_id"),
                         "пустой выбор — это «определить по AMS», а не старая катушка")
        self.assertEqual({}, res["spool"])

    def test_running_job_can_be_corrected(self):
        """Катушку меняют и во время печати: оператор поставил другую."""
        self.spool("sp-b")
        self.job(state="running", started_at="2026-09-15T09:00:00+00:00")
        res = self.manager.set_job_spool("job1", "sp-b")
        self.assertEqual("sp-b", res["job"]["spool_id"])

    def test_finished_job_refuses(self):
        self.spool("sp-a")
        self.job(state="done", accounted_at="2026-09-15T10:00:00+00:00")
        with self.assertRaises(ValueError) as ctx:
            self.manager.set_job_spool("job1", "sp-a")
        self.assertIn("завершено", str(ctx.exception))

    def test_unknown_spool_refuses(self):
        self.job()
        with self.assertRaises(ValueError) as ctx:
            self.manager.set_job_spool("job1", "sp-none")
        self.assertIn("Катушка не найдена", str(ctx.exception))

    def test_archived_spool_refuses_with_its_name(self):
        self.spool("sp-old", material="PLA", color="серый", archived=1)
        self.job()
        with self.assertRaises(ValueError) as ctx:
            self.manager.set_job_spool("job1", "sp-old")
        message = str(ctx.exception)
        self.assertIn("в архиве", message)
        self.assertIn("PLA", message)
        self.assertIn("серый", message)

    def test_missing_job_refuses(self):
        with self.assertRaises(ValueError):
            self.manager.set_job_spool("", "sp-a")
        with self.assertRaises(ValueError):
            self.manager.set_job_spool("job-none", "sp-a")


class JobSpoolRouteTests(JobSpoolCase):
    """Маршрут задания: панель шлёт `spool_id`, ответ приходит с числами."""

    def setUp(self):
        super().setUp()
        from connector.printflow.api import Api
        from connector.printflow import router as router_module
        router_module.register_all()
        self.api = Api.__new__(Api)
        self.api.db = self.db
        self.api.router = router_module.router
        self.api.manager = self.manager

    def test_route_sets_the_spool(self):
        self.spool("sp-route", material="ABS", color="белый", grams=400.0)
        self.job()
        code, payload = self.api.post("/api/jobs/spool",
                                      {"id": "job1", "spool_id": "sp-route"}, {})
        self.assertEqual(200, code)
        self.assertEqual("sp-route", payload["job"]["spool_id"])
        self.assertEqual("ABS", payload["spool"]["material"])

    def test_route_clears_the_spool(self):
        self.spool("sp-route")
        self.job(spool_id="sp-route")
        code, payload = self.api.post("/api/jobs/spool",
                                      {"id": "job1", "spool_id": ""}, {})
        self.assertEqual(200, code)
        self.assertFalse(payload["job"].get("spool_id"))

    def test_route_reports_the_deficit_in_words(self):
        self.spool("sp-route", grams=50.0)
        self.job(est_grams=200.0)
        code, payload = self.api.post("/api/jobs/spool",
                                      {"id": "job1", "spool_id": "sp-route"}, {})
        self.assertEqual(200, code)
        self.assertIn("150", payload["warning"])


class JobSpoolWarningTests(JobSpoolCase):
    def test_short_spool_warns_and_still_saves(self):
        self.spool("sp-tight", grams=120.0)
        self.job(est_grams=300.0)
        res = self.manager.set_job_spool("job1", "sp-tight")
        self.assertEqual("sp-tight", res["job"]["spool_id"])
        self.assertIn("120", res["warning"])
        self.assertIn("300", res["warning"])
        self.assertIn("180", res["warning"])

    def test_enough_spool_has_no_warning(self):
        self.spool("sp-full", grams=900.0)
        self.job(est_grams=120.0)
        self.assertEqual("", self.manager.set_job_spool("job1", "sp-full")["warning"])

    def test_unknown_estimate_has_no_warning(self):
        """Сметы нет — не пугаем выдуманным дефицитом."""
        self.spool("sp-a", grams=10.0)
        self.job(est_grams=0.0)
        self.assertEqual("", self.manager.set_job_spool("job1", "sp-a")["warning"])

    def test_five_gram_slack_is_not_a_deficit(self):
        """Остаток считают с точностью до грамма: 115 г на 120 — не нехватка."""
        self.spool("sp-near", grams=115.0)
        self.job(est_grams=120.0)
        self.assertEqual("", self.manager.set_job_spool("job1", "sp-near")["warning"])


class JobSpoolConsumptionTests(JobSpoolCase):
    """Списание идёт с выбранной катушки, а не с той, что стоит в AMS."""

    def setUp(self):
        super().setUp()
        # «Своя» катушка задания и чужая, стоящая в активном слоте AMS.
        self.spool("sp-chosen", material="PETG", color="зелёный", grams=500.0,
                   price=2000.0)
        self.spool("sp-ams", material="PLA", color="чёрный", grams=900.0,
                   price=1600.0, printer_id="prn1", ams_slot="0",
                   tray_uuid="uuid-pla-black")

    def test_consumption_follows_the_chosen_spool(self):
        job = self.job(spool_id="sp-chosen", state="done", grams=150.0)
        self.acc.register_job_costs(job)
        usage = self.db.one(
            "SELECT * FROM filament_usage WHERE job_id=? ORDER BY id DESC", ("job1",))
        self.assertEqual("sp-chosen", usage["spool_id"])
        self.assertEqual(500.0 - 150.0, num(self.db.one(
            "SELECT remaining_grams FROM spools WHERE id=?", ("sp-chosen",))["remaining_grams"]))
        self.assertEqual(900.0, num(self.db.one(
            "SELECT remaining_grams FROM spools WHERE id=?", ("sp-ams",))["remaining_grams"]),
            "катушка из AMS не должна расходоваться, если выбрана другая")

    def test_without_choice_the_ams_spool_is_used_as_before(self):
        """Пустой выбор — прежнее поведение: катушка активного слота AMS.

        Подбор делает менеджер при финализации (учёт сам слот не читает), а
        не учёт — поэтому проверяем тем же путём, что и настоящий финиш.
        """
        self.manager.printers = {"prn1": FakePrinter()}
        job = self.job(state="running", grams=0.0)
        done = self.manager._finalize_job(job, "done", "complete", 60.0, 150.0)
        self.assertEqual("sp-ams", done["spool_id"])
        usage = self.db.one(
            "SELECT * FROM filament_usage WHERE job_id=? ORDER BY id DESC", ("job1",))
        self.assertEqual("sp-ams", usage["spool_id"])
        self.assertEqual(500.0, num(self.db.one(
            "SELECT remaining_grams FROM spools WHERE id=?", ("sp-chosen",))["remaining_grams"]))

    def test_finalize_keeps_the_chosen_spool_over_ams(self):
        """Катушка выбрана на задании — AMS её не подменяет."""
        self.manager.printers = {"prn1": FakePrinter()}
        job = self.job(state="running", spool_id="sp-chosen")
        done = self.manager._finalize_job(job, "done", "complete", 60.0, 150.0)
        self.assertEqual("sp-chosen", done["spool_id"])
        usage = self.db.one(
            "SELECT * FROM filament_usage WHERE job_id=? ORDER BY id DESC", ("job1",))
        self.assertEqual("sp-chosen", usage["spool_id"])
        self.assertEqual(900.0, num(self.db.one(
            "SELECT remaining_grams FROM spools WHERE id=?", ("sp-ams",))["remaining_grams"]))

    def test_choice_survives_the_ams_slot_change(self):
        """Катушки переставили — задание всё равно спишется со своей."""
        self.job(spool_id="sp-chosen", state="done", grams=100.0)
        self.db.execute("UPDATE spools SET printer_id='', ams_slot='' WHERE id=?",
                        ("sp-chosen",))
        self.db.execute("UPDATE spools SET printer_id='prn1', ams_slot='0',"
                        " tray_uuid='uuid-other' WHERE id=?", ("sp-ams",))
        self.acc.register_job_costs(self.db.one(
            "SELECT * FROM print_jobs WHERE id=?", ("job1",)))
        usage = self.db.one(
            "SELECT * FROM filament_usage WHERE job_id=? ORDER BY id DESC", ("job1",))
        self.assertEqual("sp-chosen", usage["spool_id"])


if __name__ == "__main__":
    unittest.main()

"""Тесты 8.2: зависшие задания/заказы, катушки заказа и смешанные плиты."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting, num  # noqa: E402
from connector.printflow.api import Api  # noqa: E402
from connector.printflow.batches import Batches  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.repo import Repo, uid  # noqa: E402
from connector.printflow.stock import Stock  # noqa: E402


class PrinterStub:
    """Минимальный принтер: только то, что нужен менеджеру."""

    def __init__(self, pid="p1", state="IDLE"):
        self.id = pid
        self.record = {"id": pid, "name": "P1", "enabled": 1, "mode": "lan"}
        self.connected = True
        self.state = state

    def snapshot(self):
        return {"printer": {"state": self.state, "progress": 0, "total_layers": 0},
                "ams": {"trays": []}}

    def command(self, name, value=None):
        return {"ok": True}

    def shutdown(self):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "t.sqlite3")
        self.repo = Repo(self.db)
        self.acc = Accounting(self.db)
        self.manager = None

    def tearDown(self):
        if self.manager:
            self.manager.shutdown()
        self.db.close()
        self.tmp.cleanup()

    def manager_with(self, printer):
        from connector.printflow.manager import PrinterManager
        self.manager = PrinterManager(self.db, self.repo)
        self.manager.printers[printer.id] = printer
        return self.manager

    def spool(self, remaining=800, grams_used=0):
        return self.db.upsert("spools", {
            "id": uid("sp"), "material": "PLA", "color_name": "Чёрный",
            "brand": "Bambu", "total_grams": 1000, "remaining_grams": remaining,
            "price": 1200, "created_at": "2026-01-01T00:00:00+00:00"})


class OrderSpoolsTests(Base):
    """Списание по катушкам, указанным в заказе (мультицвет)."""

    def test_consume_order_spools_exact(self):
        s1 = self.spool(remaining=800)
        s2 = self.spool(remaining=500)
        order = self.repo.save_order({
            "product": "Мультицвет", "grams": 100,
            "spools": json.dumps([{"spool_id": s1["id"], "grams": 70},
                                  {"spool_id": s2["id"], "grams": 30}])})
        job = self.db.upsert("print_jobs", {
            "id": uid("job"), "order_id": order["id"], "state": "done",
            "grams": 100, "name": "multi.3mf", "created_at": "2026-01-02T00:00:00+00:00"})
        out = self.acc.consume_order_spools(job)
        self.assertEqual(len(out), 2)
        self.assertTrue(all(x["ok"] for x in out))
        self.assertEqual(num(self.repo.spool(s1["id"])["remaining_grams"]), 730)
        self.assertEqual(num(self.repo.spool(s2["id"])["remaining_grams"]), 470)

    def test_register_job_costs_prefers_order_spools(self):
        s1 = self.spool(remaining=900)
        order = self.repo.save_order({
            "product": "Точный расход", "grams": 50,
            "spools": json.dumps([{"spool_id": s1["id"], "grams": 50}])})
        job = self.db.upsert("print_jobs", {
            "id": uid("job"), "order_id": order["id"], "state": "done",
            "grams": 50, "duration_min": 60, "name": "x.3mf",
            "created_at": "2026-01-02T00:00:00+00:00"})
        result = self.acc.register_job_costs(job)
        self.assertEqual(result["filament"].get("by"), "order_spools")
        self.assertEqual(num(self.repo.spool(s1["id"])["remaining_grams"]), 850)

    def test_order_spools_scale_to_printer_actual_weight(self):
        s1 = self.spool(remaining=800)
        s2 = self.spool(remaining=500)
        order = self.repo.save_order({
            "product": "Факт с принтера", "grams": 100,
            "spools": json.dumps([{"spool_id": s1["id"], "grams": 70},
                                  {"spool_id": s2["id"], "grams": 30}])})
        job = {"id": uid("job"), "order_id": order["id"], "grams": 110,
               "name": "actual.3mf"}
        self.acc.consume_order_spools(job)
        self.assertEqual(num(self.repo.spool(s1["id"])["remaining_grams"]), 723)
        self.assertEqual(num(self.repo.spool(s2["id"])["remaining_grams"]), 467)

    def test_colors_field_saved_through_order_fields(self):
        """orders.colors — многоцветная раскладка хранится в заказе."""
        order = self.repo.save_order({
            "product": "Цвета", "colors": json.dumps([{"color": "Белый", "grams": 40}])})
        row = self.db.one("SELECT colors FROM orders WHERE id=?", (order["id"],))
        parsed = json.loads(row["colors"])
        self.assertEqual(parsed[0]["color"], "Белый")


class ReadyProductOrderTests(Base):
    """Готовый товар резервируется в заказе и списывается при выдаче."""

    def setUp(self):
        super().setUp()
        self.stock = Stock(self.db)
        self.api = Api.__new__(Api)
        self.api.db = self.db
        self.api.repo = self.repo
        self.api.stock = self.stock
        self.api.acc = self.acc
        self.db.set_settings({"auto_income_on_done": False})
        self.wh = self.db.upsert("warehouses", {
            "id": uid("wh"), "name": "Полка", "kind": "retail", "retail": 1,
            "archived": 0, "position": 0})
        self.nom = self.db.upsert("nomenclature", {
            "id": uid("nom"), "code": "000100", "name": "Готовый адресник",
            "kind": "product", "unit": "шт", "archived": 0})
        self.stock.add_move(self.nom["id"], self.wh["id"], 5, 500,
                            doc_id="receipt", doc_kind="receipt")

    def test_reserve_and_consume_ready_product(self):
        result = self.api.save_order({
            "product": self.nom["name"], "nom_id": self.nom["id"],
            "warehouse_id": self.wh["id"], "qty": 2, "reserved": 1,
            "price": 600, "status": "ready"})
        order = result["order"]
        self.assertEqual(self.stock.reserved(self.nom["id"], self.wh["id"]), 2)
        self.assertEqual(self.stock.qty(self.nom["id"], self.wh["id"]), 5)
        self.api.fulfill_order(
            order["id"], handoff_confirmed=True, payment_action="debt"
        )
        self.assertEqual(self.stock.qty(self.nom["id"], self.wh["id"]), 3)
        self.assertEqual(self.stock.reserved(self.nom["id"], self.wh["id"]), 0)
        move = self.db.one("SELECT * FROM stock_moves WHERE doc_id=? AND doc_kind='sale'",
                           (order["id"],))
        self.assertEqual(num(move["qty"]), -2)

    def test_failed_reserve_rolls_back_order_and_previous_reserve(self):
        with self.assertRaises(ValueError):
            self.api.save_order({"product": self.nom["name"], "nom_id": self.nom["id"],
                                 "warehouse_id": self.wh["id"], "qty": 8, "reserved": 1})
        self.assertIsNone(self.db.one("SELECT id FROM orders WHERE product=?", (self.nom["name"],)))


class ReconcileTests(Base):
    """Зависшие задания: принтер свободен, а заказ «в печати»."""

    def setUp(self):
        super().setUp()

    def _stale_job(self, printer_id, order_id, progress=45):
        return self.db.upsert("print_jobs", {
            "id": uid("job"), "printer_id": printer_id, "order_id": order_id,
            "state": "running", "name": "stale.3mf", "progress": progress,
            "started_at": "2026-01-01T00:00:00+00:00",
            "created_at": "2026-01-01T00:00:00+00:00", "grams": 30})

    def test_reconcile_cancels_stale_and_releases_order(self):
        order = self.repo.save_order({"product": "Завис", "status": "printing"})
        self._stale_job("p1", order["id"])
        printer = PrinterStub("p1", state="IDLE")
        mgr = self.manager_with(printer)
        mgr._reconcile_printer(printer, printer.snapshot())
        job = self.db.one("SELECT * FROM print_jobs WHERE order_id=?", (order["id"],))
        self.assertEqual(job["state"], "cancelled")
        self.assertEqual(job["result"], "lost")
        status = self.db.one("SELECT status FROM orders WHERE id=?", (order["id"],))
        self.assertEqual(status["status"], "queue")

    def test_reconcile_done_when_finish_state(self):
        order = self.repo.save_order({"product": "Финиш", "status": "printing"})
        self._stale_job("p1", order["id"], progress=100)
        printer = PrinterStub("p1", state="FINISH")
        mgr = self.manager_with(printer)
        mgr._reconcile_printer(printer, printer.snapshot())
        job = self.db.one("SELECT * FROM print_jobs WHERE order_id=?", (order["id"],))
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["result"], "auto-done")
        status = self.db.one("SELECT status FROM orders WHERE id=?", (order["id"],))
        self.assertEqual(status["status"], "post")

    def test_reconcile_ignores_fresh_jobs(self):
        from connector.printflow.config import now_iso
        order = self.repo.save_order({"product": "Свежая", "status": "printing"})
        self.db.upsert("print_jobs", {
            "id": uid("job"), "printer_id": "p1", "order_id": order["id"],
            "state": "starting", "name": "new.3mf", "progress": 0,
            "started_at": now_iso(), "created_at": now_iso()})
        printer = PrinterStub("p1", state="IDLE")
        mgr = self.manager_with(printer)
        mgr._reconcile_printer(printer, printer.snapshot())
        job = self.db.one("SELECT * FROM print_jobs WHERE order_id=?", (order["id"],))
        self.assertEqual(job["state"], "starting")

    def test_cancel_job_releases_order(self):
        order = self.repo.save_order({"product": "Отмена", "status": "printing"})
        job = self.db.upsert("print_jobs", {
            "id": uid("job"), "printer_id": "p1", "order_id": order["id"],
            "state": "running", "name": "x.3mf", "started_at": "2026-01-01T00:00:00+00:00",
            "created_at": "2026-01-01T00:00:00+00:00"})
        printer = PrinterStub("p1", state="RUNNING")
        mgr = self.manager_with(printer)
        mgr.cancel_job(job["id"])
        status = self.db.one("SELECT status FROM orders WHERE id=?", (order["id"],))
        self.assertEqual(status["status"], "queue")

    def test_failed_print_releases_order(self):
        order = self.repo.save_order({"product": "Срыв", "status": "printing"})
        self.db.upsert("print_jobs", {
            "id": uid("job"), "printer_id": "p1", "order_id": order["id"],
            "state": "running", "name": "y.3mf", "started_at": "2026-01-01T00:00:00+00:00",
            "created_at": "2026-01-01T00:00:00"})
        printer = PrinterStub("p1", state="RUNNING")
        mgr = self.manager_with(printer)
        mgr._on_print_end("p1", "error", "y.3mf", {"duration_min": 5, "weight": 10})
        status = self.db.one("SELECT status FROM orders WHERE id=?", (order["id"],))
        self.assertEqual(status["status"], "queue")

    def test_new_print_replaces_stale_running_job(self):
        order = self.repo.save_order({"product": "Старое", "status": "printing",
                                      "file": "old.3mf"})
        self.db.upsert("print_jobs", {
            "id": uid("job"), "printer_id": "p1", "order_id": order["id"],
            "state": "running", "name": "old.3mf", "progress": 40,
            "started_at": "2026-01-01T00:00:00+00:00",
            "created_at": "2026-01-01T00:00:00+00:00"})
        printer = PrinterStub("p1", state="RUNNING")
        mgr = self.manager_with(printer)
        mgr._on_print_start("p1", "brand_new.3mf", {})
        rows = self.db.query("SELECT * FROM print_jobs WHERE printer_id='p1'")
        self.assertEqual(len(rows), 2)
        old = next(r for r in rows if r["name"] == "old.3mf")
        new = next(r for r in rows if r["name"] == "brand_new.3mf")
        self.assertEqual(old["state"], "cancelled")
        self.assertEqual(old["result"], "replaced")
        self.assertEqual(new["state"], "running")


class MixedBatchTests(Base):
    """Смешанная плита: разные товары на одном столе."""

    def _nom(self, name, grams, hours):
        return self.db.upsert("nomenclature", {
            "id": uid("nom"), "code": name, "name": name, "kind": "product",
            "material": "PLA", "grams": grams, "hours": hours, "fit_per_plate": 1,
            "file": "mix.3mf", "archived": 0, "created_at": "2026-01-01T00:00:00+00:00"})

    def _warehouse(self):
        return self.db.upsert("warehouses", {
            "id": uid("wh"), "name": "Полка", "kind": "retail", "retail": 1,
            "archived": 0, "position": 0})

    def test_plan_multi(self):
        a = self._nom("Адресник", 20, 1)
        b = self._nom("Крючок", 5, 0.2)
        batches = Batches(self.db, None)
        plan = batches.plan_multi([{"nom_id": a["id"], "qty": 3},
                                   {"nom_id": b["id"], "qty": 4}], plates=2, file="mix.3mf")
        self.assertTrue(plan["mixed"])
        self.assertEqual(plan["units_per_plate"], 7)
        self.assertEqual(plan["qty_real"], 14)
        self.assertAlmostEqual(plan["grams"], (20 * 3 + 5 * 4) * 2, places=1)

    def test_mixed_batch_receive_by_job(self):
        a = self._nom("Адресник", 20, 1)
        b = self._nom("Крючок", 5, 0.2)
        wh = self._warehouse()
        self.db.execute(
            "INSERT INTO prices(id,nom_id,price_type_id,price) VALUES(?,?,?,?)",
            (uid("pr"), a["id"], "", 300))
        batches = Batches(self.db, None)
        batch = batches.create({
            "items": [{"nom_id": a["id"], "qty": 3}, {"nom_id": b["id"], "qty": 4}],
            "plates": 2, "file": "mix.3mf", "warehouse_id": wh["id"]})
        self.assertEqual(batch["mode"], "mixed")
        self.assertEqual(num(batch["qty_planned"]), 14)
        self.assertEqual(len(batch["items_list"]), 2)
        # первая плита напечаталась
        job = {"id": uid("job"), "batch_id": batch["id"], "state": "done",
               "batch_qty": 7, "name": "mix плита 1/2", "cost": 0}
        batches.on_job_finished(job)
        row = self.db.one("SELECT * FROM batches WHERE id=?", (batch["id"],))
        self.assertEqual(num(row["qty_done"]), 7)
        doc = self.db.one(
            "SELECT * FROM documents WHERE batch_id=? AND kind='production'", (batch["id"],))
        self.assertIsNotNone(doc)
        lines = self.db.query("SELECT * FROM doc_items WHERE doc_id=?", (doc["id"],))
        got = {l["nom_id"]: num(l["qty"]) for l in lines}
        self.assertEqual(got.get(a["id"]), 3)
        self.assertEqual(got.get(b["id"]), 4)
        # вторая плита — партия готова
        job2 = dict(job, id=uid("job"), name="mix плита 2/2")
        batches.on_job_finished(job2)
        row = self.db.one("SELECT * FROM batches WHERE id=?", (batch["id"],))
        self.assertEqual(row["state"], "done")
        self.assertEqual(num(row["qty_done"]), 14)

    def test_mixed_receive_manual_with_items(self):
        a = self._nom("Адресник", 20, 1)
        b = self._nom("Крючок", 5, 0.2)
        wh = self._warehouse()
        batches = Batches(self.db, None)
        batch = batches.create({
            "items": [{"nom_id": a["id"], "qty": 2}, {"nom_id": b["id"], "qty": 2}],
            "plates": 1, "file": "mix.3mf", "warehouse_id": wh["id"]})
        batches.receive(batch["id"], 4, items=[
            {"nom_id": a["id"], "qty": 2}, {"nom_id": b["id"], "qty": 2}])
        row = self.db.one("SELECT * FROM batches WHERE id=?", (batch["id"],))
        self.assertEqual(row["state"], "done")
        lines = self.db.query(
            "SELECT i.* FROM doc_items i JOIN documents d ON d.id=i.doc_id"
            " WHERE d.batch_id=?", (batch["id"],))
        self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()


class OrphanOrderSweepTests(Base):
    """Заказ «в печати» без заданий — панель говорит «готов», заказ висит."""

    def test_orphan_released_when_printers_idle(self):
        order = self.repo.save_order({"product": "Висяк", "status": "printing"})
        # время последнего изменения — больше 30 минут назад
        self.db.execute(
            "UPDATE orders SET updated_at=? WHERE id=?",
            ("2026-01-01T00:00:00+00:00", order["id"]))
        printer = PrinterStub("p1", state="IDLE")
        mgr = self.manager_with(printer)
        mgr._reconcile_orphan_orders()
        status = self.db.one("SELECT status FROM orders WHERE id=?", (order["id"],))
        self.assertEqual(status["status"], "queue")

    def test_orphan_kept_while_printer_busy(self):
        order = self.repo.save_order({"product": "Ручной статус", "status": "printing"})
        self.db.execute(
            "UPDATE orders SET updated_at=? WHERE id=?",
            ("2026-01-01T00:00:00+00:00", order["id"]))
        printer = PrinterStub("p1", state="RUNNING")
        mgr = self.manager_with(printer)
        mgr._reconcile_orphan_orders()
        status = self.db.one("SELECT status FROM orders WHERE id=?", (order["id"],))
        self.assertEqual(status["status"], "printing")

    def test_fresh_orphan_not_touched(self):
        order = self.repo.save_order({"product": "Только поставили", "status": "printing"})
        printer = PrinterStub("p1", state="IDLE")
        mgr = self.manager_with(printer)
        mgr._reconcile_orphan_orders()
        status = self.db.one("SELECT status FROM orders WHERE id=?", (order["id"],))
        self.assertEqual(status["status"], "printing")

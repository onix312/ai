"""Пересборка «Заказов» 18.3: лёгкий список доски, переходы как данные.

Что покрыто:

* ``GET /api/orders?view=board`` — урезанная строка доски: канбан и таблица
  получают только нужные поля, а экономика совпадает с полным видом;
* ``sort=due|debt|stale`` — порядок списка считает сервер, а не браузер;
* фильтр ``channel`` (включая алиасы ``telegram``/``no-tg``) и поиск по
  файлу и заметкам — паритет со старым клиентским фильтром;
* переходы статусов как данные (``statuses.next_ids``): свой статус
  становится настоящим этапом, штатные 8 при пустом поле ведут себя
  побитово как раньше;
* «перепечатать» (``ready → printing``), конфликт версий статуса,
  сухая проверка пакета и удаление без сирот.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.api import Api  # noqa: E402
from connector.printflow.config import now_iso  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.repo import ORDER_TRANSITIONS, Repo  # noqa: E402


def make_repo(test: unittest.TestCase) -> Repo:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    db = Database(pathlib.Path(tmp.name) / "rebuild.sqlite3")
    test.addCleanup(db.close)
    return Repo(db)


def seed_order(repo: Repo, order_id: str = "o1", **over: object) -> dict:
    data = {"id": order_id, "number": "1001", "product": "Адресник",
            "customer_name": "Анна", "phone": "+7 900 111-22-33",
            "status": "queue", "price": 1500, "grams": 40, "hours": 3,
            "created_at": "2026-09-01T10:00:00",
            "updated_at": "2026-09-10T10:00:00"}
    data.update(over)
    return repo.db.upsert("orders", data)


class BoardViewTests(unittest.TestCase):
    """Лёгкая строка доски: меньше полей — та же экономика."""

    def setUp(self):
        self.repo = make_repo(self)
        seed_order(self.repo, "o1", price=1500, discount=100, fee=50,
                   delivery=200, manual_minutes=30, design_minutes=15,
                   spools=json.dumps([{"spool_id": "s1", "grams": 40}]),
                   notes="позвонить вечером", messenger="@anna",
                   file="adresnik.3mf", quality="pending")

    def test_board_view_drops_card_only_fields(self):
        row = self.repo.orders(view="board")[0]
        for heavy in ("colors", "qc_done", "nom_id",
                      "warehouse_id", "gift", "quality_note", "closed_at",
                      "client_request_id", "archived_at", "auto_cost",
                      "account_id", "customer_id", "reserved", "rush"):
            self.assertNotIn(heavy, row, f"доске не нужно поле {heavy}")
        for need in ("id", "number", "product", "customer_name", "phone",
                     "messenger", "status", "priority", "niche_id", "channel",
                     "client_source", "due", "paid", "prepaid", "price",
                     "discount", "fee", "payer", "delivery", "material",
                     "color", "grams", "hours", "actual_grams",
                     "actual_hours", "actual_cost", "cost", "manual_minutes",
                     "design_minutes", "qty", "spools", "file", "notes", "quality",
                     "cancel_requested_at", "archived", "created_at",
                     "updated_at", "items_count", "economics", "next"):
            self.assertIn(need, row, f"доске нужно поле {need}")

    def test_board_economics_matches_full_view(self):
        slim = self.repo.orders(view="board")[0]["economics"]
        full = self.repo.orders()[0]["economics"]
        self.assertEqual(slim, full,
                         "урезанная строка обязана считать деньги так же")

    def test_board_is_smaller_than_full(self):
        slim = json.dumps(self.repo.orders(view="board"), ensure_ascii=False)
        full = json.dumps(self.repo.orders(), ensure_ascii=False)
        self.assertLess(len(slim.encode("utf-8")), len(full.encode("utf-8")))

    def test_unknown_view_falls_back_to_full(self):
        row = self.repo.orders(view="nope")[0]
        self.assertIn("notes", row)


class OrderSortTests(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self)
        seed_order(self.repo, "late", number="1010", due="2026-09-05",
                   created_at="2026-09-01T10:00:00")
        seed_order(self.repo, "soon", number="1011", due="2026-09-12",
                   created_at="2026-09-03T10:00:00")
        seed_order(self.repo, "nodue", number="1012", due="",
                   created_at="2026-09-02T10:00:00")

    def test_sort_due_puts_empty_last(self):
        ids = [r["id"] for r in self.repo.orders(sort="due")]
        self.assertEqual(ids, ["late", "soon", "nodue"])

    def test_default_sort_is_still_newest_first(self):
        ids = [r["id"] for r in self.repo.orders()]
        self.assertEqual(ids, ["soon", "nodue", "late"])

    def test_unknown_sort_falls_back_to_default(self):
        ids = [r["id"] for r in self.repo.orders(sort="nope")]
        self.assertEqual(ids, ["soon", "nodue", "late"])

    def test_sort_debt_is_biggest_first(self):
        seed_order(self.repo, "rich", number="1020", price=5000, paid=5000)
        seed_order(self.repo, "poor", number="1021", price=5000, paid=100)
        ids = [r["id"] for r in self.repo.orders(sort="debt")]
        self.assertEqual(ids[0], "poor")
        self.assertIn("rich", ids[1:])

    def test_sort_stale_is_oldest_update_first(self):
        seed_order(self.repo, "fresh", number="1030",
                   updated_at="2026-09-14T10:00:00")
        ids = [r["id"] for r in self.repo.orders(sort="stale")]
        self.assertEqual(ids[-1], "fresh")


class OrderChannelFilterTests(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self)
        seed_order(self.repo, "tg", number="1010", channel="telegram")
        seed_order(self.repo, "shop", number="1011", channel="",
                   client_source="catalog")
        seed_order(self.repo, "avito", number="1012", channel="avito")

    def test_exact_channel(self):
        ids = [r["id"] for r in self.repo.orders(channel="avito")]
        self.assertEqual(ids, ["avito"])

    def test_telegram_alias_covers_bot_sources(self):
        ids = sorted(r["id"] for r in self.repo.orders(channel="telegram"))
        self.assertEqual(ids, ["shop", "tg"])

    def test_no_tg_alias_excludes_bot_sources(self):
        ids = [r["id"] for r in self.repo.orders(channel="no-tg")]
        self.assertEqual(ids, ["avito"])


class OrderSearchTests(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self)
        seed_order(self.repo, "o1", number="1010", product="Адресник",
                   file="adresnik-v2.3mf", notes="позвонить вечером")

    def test_search_finds_file_name(self):
        self.assertEqual(len(self.repo.orders(search="adresnik-v2")), 1)

    def test_search_finds_notes(self):
        self.assertEqual(len(self.repo.orders(search="позвонить")), 1)

    def test_search_still_finds_number(self):
        self.assertEqual(len(self.repo.orders(search="1010")), 1)


class TransitionsAsDataTests(unittest.TestCase):
    """Свои статусы — этапы с переходами, а не декорация."""

    def setUp(self):
        self.repo = make_repo(self)
        seed_order(self.repo, "o1", status="ready")
        self.repo.save_status({"id": "st_delivery", "name": "Доставка",
                               "color": "#0ea5e9", "position": 9,
                               "is_final": 0})

    def test_custom_stage_without_links_is_a_dead_end(self):
        with self.assertRaises(ValueError):
            self.repo.set_order_status("o1", "st_delivery")

    def test_custom_stage_receives_and_releases_when_linked(self):
        self.repo.save_status({"id": "ready", "next_ids": ["post", "st_delivery"]})
        self.repo.save_status({"id": "st_delivery", "next_ids": ["post", "ready"]})
        self.assertEqual(self.repo.set_order_status("o1", "st_delivery")["status"],
                         "st_delivery")
        self.assertEqual(self.repo.set_order_status("o1", "post")["status"], "post")
        # В финальный ведёт выдача, а не стрелка, — даже из своего этапа:
        # запрещает уже конфигурация, а не только сам переход.
        with self.assertRaises(ValueError):
            self.repo.save_status({"id": "st_delivery", "next_ids": ["done"]})
        with self.assertRaises(ValueError):
            self.repo.set_order_status("o1", "done")
        self.assertEqual(self.repo.order("o1")["status"], "post")

    def test_stock_statuses_keep_hardcoded_map_when_unset(self):
        seed_order(self.repo, "o2", number="1002", status="new")
        self.assertEqual(self.repo.set_order_status("o2", "queue")["status"], "queue")
        with self.assertRaises(ValueError):
            self.repo.set_order_status("o2", "done")

    def test_hardcoded_map_is_still_live_for_unset(self):
        """Пин прошлого раунда: ORDER_TRANSITIONS читается вживую."""
        seed_order(self.repo, "o3", number="1003", status="done")
        with mock.patch.dict(ORDER_TRANSITIONS, {"done": {"queue"}}, clear=False):
            self.repo.set_order_status("o3", "queue")
        self.assertEqual(self.repo.order("o3")["status"], "queue")

    def test_next_ids_must_point_at_real_statuses(self):
        with self.assertRaises(ValueError):
            self.repo.save_status({"id": "ready", "next_ids": ["nope"]})

    def test_next_ids_must_not_point_at_final(self):
        with self.assertRaises(ValueError):
            self.repo.save_status({"id": "ready", "next_ids": ["post", "done"]})

    def test_statuses_carry_effective_next(self):
        by_id = {s["id"]: s for s in self.repo.statuses()}
        self.assertIn("printing", by_id["queue"]["next"])
        self.assertEqual(by_id["st_delivery"]["next"], [])
        self.repo.save_status({"id": "st_delivery", "next_ids": ["ready"]})
        by_id = {s["id"]: s for s in self.repo.statuses()}
        self.assertEqual(by_id["st_delivery"]["next"], ["ready"])

    def test_explicit_empty_list_means_dead_end(self):
        self.repo.save_status({"id": "queue", "next_ids": []})
        seed_order(self.repo, "o4", number="1004", status="queue")
        with self.assertRaises(ValueError):
            self.repo.set_order_status("o4", "printing")
        self.assertEqual(
            {s["id"]: s for s in self.repo.statuses()}["queue"]["next"], [])


class ReprintTests(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self)
        seed_order(self.repo, "o1", status="ready")

    def test_ready_can_go_back_to_printing(self):
        self.assertEqual(self.repo.set_order_status("o1", "printing")["status"],
                         "printing")

    def test_ready_still_offers_post(self):
        self.assertIn("post", self.repo.next_statuses("ready"))

    def test_done_is_still_a_dead_end(self):
        seed_order(self.repo, "o2", number="1002", status="done")
        with self.assertRaises(ValueError):
            self.repo.set_order_status("o2", "queue")


class StatusConflictTests(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self)
        seed_order(self.repo, "o1", status="queue",
                   updated_at="2026-09-10T10:00:00.000001")

    def test_stale_version_is_refused(self):
        with self.assertRaises(ValueError):
            self.repo.set_order_status("o1", "printing",
                                       expected_updated_at="2026-01-01T00:00:00")
        self.assertEqual(self.repo.order("o1")["status"], "queue")

    def test_fresh_version_passes(self):
        row = self.repo.set_order_status(
            "o1", "printing", expected_updated_at="2026-09-10T10:00:00.000001")
        self.assertEqual(row["status"], "printing")

    def test_route_forwards_the_version(self):
        api = Api.__new__(Api)
        api.db = self.repo.db
        api.repo = self.repo
        api.manager = types.SimpleNamespace(client_bot=None)
        api._audit = lambda *a, **k: None  # noqa: E731
        # ValueError здесь — это HTTP 400: в 400 его превращает http_handler.
        with self.assertRaisesRegex(ValueError, "уже изменён"):
            api.post("/api/order/status",
                     {"id": "o1", "status": "printing",
                      "expected_updated_at": "2026-01-01T00:00:00"}, {})
        self.assertEqual(self.repo.order("o1")["status"], "queue")
        code, payload = api.post(
            "/api/order/status",
            {"id": "o1", "status": "printing",
             "expected_updated_at": "2026-09-10T10:00:00.000001"}, {})
        self.assertEqual(code, 200)
        self.assertEqual(payload["order"]["status"], "printing")


class BulkDryRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(pathlib.Path(self.tmp.name) / "dry.sqlite3")
        self.addCleanup(self.db.close)
        self.repo = Repo(self.db)
        self.api = Api.__new__(Api)
        self.api.db = self.db
        self.api.repo = self.repo
        self.api.bus = types.SimpleNamespace(publish=lambda *a, **k: None)
        seed_order(self.repo, "a", number="1001", status="queue")
        seed_order(self.repo, "b", number="1002", status="done")

    def test_check_only_reports_without_changing(self):
        code, payload = self.api.post(
            "/api/orders/bulk-status",
            {"ids": ["a", "b"], "status": "printing", "check_only": True}, {})
        self.assertEqual(code, 200)
        self.assertTrue(payload["check_only"])
        self.assertEqual(payload["updated"], 0)
        self.assertEqual([r["number"] for r in payload["allowed"]], ["1001"])
        self.assertEqual(len(payload["skipped"]), 1)
        self.assertEqual(self.repo.order("a")["status"], "queue")
        self.assertEqual(self.repo.order("b")["status"], "done")


class DeleteOrphansTests(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(self)
        seed_order(self.repo, "o1")
        db = self.repo.db
        db.execute("INSERT INTO order_history(at,order_id,field,old_value,new_value)"
                   " VALUES(?,?,?,?,?)",
                   (now_iso(), "o1", "status", "new", "queue"))
        db.upsert("order_photos", {"id": "ph1", "order_id": "o1",
                                   "file": "img1.jpg"})
        db.upsert("defects", {"id": "d1", "order_id": "o1", "reason": "слой"})
        db.execute("INSERT INTO documents(id, order_id, kind, state) VALUES(?,?,?,?)",
                   ("doc1", "o1", "sale", "draft"))
        db.upsert("customer_feedback", {"id": "f1", "order_id": "o1"})
        db.upsert("payments", {"id": "p1", "order_id": "o1", "amount": 100})

    def test_journal_rows_go_with_the_order(self):
        self.repo.delete_order("o1")
        db = self.repo.db
        self.assertEqual(db.query("SELECT * FROM order_history WHERE order_id=?", ("o1",)), [])
        self.assertEqual(db.query("SELECT * FROM order_photos WHERE order_id=?", ("o1",)), [])
        self.assertIsNone(db.one("SELECT id FROM orders WHERE id=?", ("o1",)))

    def test_facts_survive_unlinked(self):
        self.repo.delete_order("o1")
        db = self.repo.db
        for table, key in (("defects", "d1"), ("documents", "doc1"),
                           ("customer_feedback", "f1"), ("payments", "p1")):
            with self.subTest(table=table):
                row = db.one(f"SELECT * FROM {table} WHERE id=?", (key,))
                self.assertIsNotNone(row, f"{table} должна пережить заказ")
                self.assertIsNone(row.get("order_id"), f"{table} отвязана")


class DueIndexTests(unittest.TestCase):
    def test_due_index_exists(self):
        repo = make_repo(self)
        names = {r["name"] for r in repo.db.query(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_orders_due", names)


if __name__ == "__main__":
    unittest.main()

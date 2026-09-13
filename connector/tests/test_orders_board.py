"""Канбан заказов 17.0.16: страницы списка, «куда можно шагнуть» и пакет статуса.

Три вещи, которые раньше жили вразнобой:

* ``/api/orders`` отдавал все заказы сразу — ответ рос вместе с историей;
* карту допустимых переходов статуса знал только ``set_order_status``, поэтому
  фронт не мог показать «→ следующий этап» иначе как перебором с ошибкой 400;
* ``/api/orders/bulk-status`` менял статусы без общей транзакции: обрыв на
  середине оставлял часть заказов переведённой, и панель об этом не знала.
"""
from __future__ import annotations

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


class OrderListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "board.sqlite3")
        self.repo = Repo(self.db)
        for i in range(5):
            self.db.upsert("orders", {
                "id": f"o{i}", "number": str(1001 + i), "product": f"Деталь {i}",
                "customer_name": "Анна", "status": "queue", "price": 100 + i,
                "created_at": f"2026-09-0{i + 1}T10:00:00", "updated_at": now_iso()})

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_limit_returns_page_not_whole_history(self):
        self.assertEqual(len(self.repo.orders()), 5, "без limit — прежнее поведение")
        page = self.repo.orders(limit=2)
        self.assertEqual([r["id"] for r in page], ["o4", "o3"],
                         "страница — самые свежие, как и весь список")

    def test_offset_continues_page_without_repeats(self):
        first = [r["id"] for r in self.repo.orders(limit=2, offset=0)]
        second = [r["id"] for r in self.repo.orders(limit=2, offset=2)]
        self.assertEqual(first, ["o4", "o3"])
        self.assertEqual(second, ["o2", "o1"])
        self.assertFalse(set(first) & set(second), "страницы не должны пересекаться")

    def test_rows_carry_allowed_next_statuses(self):
        """Канбан рисует «→» по данным сервера, а не по своей карте."""
        row = self.repo.orders(limit=1)[0]
        self.assertEqual(row["next"], ["new", "printing"])
        single = self.repo.order("o0")
        self.assertEqual(single["next"], ["new", "printing"])

    def test_final_status_has_no_next(self):
        self.db.execute("UPDATE orders SET status='done' WHERE id='o0'")
        self.assertEqual(self.repo.order("o0")["next"], [])


class OrderTransitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "board.sqlite3")
        self.repo = Repo(self.db)
        self.db.upsert("orders", {
            "id": "o1", "number": "1001", "product": "Корпус", "status": "done",
            "price": 500, "created_at": now_iso(), "updated_at": now_iso()})

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_forbidden_transition_is_refused(self):
        with self.assertRaises(ValueError):
            self.repo.set_order_status("o1", "queue")
        self.assertEqual(self.repo.order("o1")["status"], "done")

    def test_transition_map_is_the_single_source(self):
        """set_order_status() читает ORDER_TRANSITIONS, а не свою копию."""
        self.assertIn("queue", ORDER_TRANSITIONS["new"])
        with mock.patch.dict(ORDER_TRANSITIONS, {"done": {"queue"}}, clear=False):
            self.repo.set_order_status("o1", "queue")
        self.assertEqual(self.repo.order("o1")["status"], "queue")


class BulkStatusRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "bulk.sqlite3")
        self.repo = Repo(self.db)
        self.api = Api.__new__(Api)
        self.api.db = self.db
        self.api.repo = self.repo
        self.api.bus = types.SimpleNamespace(publish=lambda *a, **k: None)
        self.order("a", "queue")
        self.order("b", "done")
        self.order("c", "queue")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def order(self, order_id: str, status: str) -> None:
        self.db.upsert("orders", {
            "id": order_id, "number": "100" + order_id, "product": "Деталь",
            "status": status, "price": 100, "created_at": now_iso(),
            "updated_at": now_iso()})

    def test_unknown_status_is_rejected_before_touching_orders(self):
        code, payload = self.api.post(
            "/api/orders/bulk-status", {"ids": ["a", "b"], "status": "nope"}, {})
        self.assertEqual(code, 400)
        self.assertEqual(self.repo.order("a")["status"], "queue")
        self.assertEqual(self.repo.order("b")["status"], "done")

    def test_forbidden_rows_are_reported_by_number(self):
        code, payload = self.api.post(
            "/api/orders/bulk-status", {"ids": ["a", "b"], "status": "printing"}, {})
        self.assertEqual(code, 200)
        self.assertEqual(payload["updated"], 1)
        self.assertEqual(self.repo.order("a")["status"], "printing")
        self.assertEqual(self.repo.order("b")["status"], "done", "переход из done запрещён")
        self.assertEqual(len(payload["skipped"]), 1)
        self.assertEqual(payload["skipped"][0]["number"], "100b")
        self.assertIn("запрещён", payload["skipped"][0]["error"])

    def test_hard_failure_rolls_the_whole_batch_back(self):
        """Обрыв на середине пакета не оставляет половину переведённой."""
        real = self.repo.set_order_status
        seen: list[str] = []

        def flaky(order_id, status):
            seen.append(order_id)
            if len(seen) > 1:
                raise RuntimeError("диск отвалился")
            return real(order_id, status)

        self.repo.set_order_status = flaky  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError):
                self.api.post("/api/orders/bulk-status",
                              {"ids": ["a", "c"], "status": "printing"}, {})
        finally:
            self.repo.set_order_status = real  # type: ignore[assignment]
        self.assertEqual(self.repo.order("a")["status"], "queue",
                         "первый заказ должен откатиться вместе с пакетом")


class OrderArchiveTests(unittest.TestCase):
    """Архив вместо удаления (17.0.16).

    Удаление заказа обрывает историю: `delete_order` отвязывает платежи и
    стирает состав. Архив убирает заказ только из списка — строка, состав и
    деньги остаются, поэтому учёт его по-прежнему видит.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(pathlib.Path(self.tmp.name) / "archive.sqlite3")
        self.addCleanup(self.db.close)
        self.repo = Repo(self.db)
        self.db.upsert("customers", {"id": "c1", "name": "Анна"})
        self.order("o1", "1001", price=1000, paid=1000)
        self.db.upsert("order_items", {"id": "i1", "order_id": "o1", "name": "Деталь",
                                       "qty": 2, "price": 500, "position": 0})
        self.api = Api.__new__(Api)
        self.api.db = self.db
        self.api.repo = self.repo
        self.api.bus = types.SimpleNamespace(publish=lambda *a, **k: None)

    def order(self, order_id: str, number: str, **fields) -> None:
        self.db.upsert("orders", {"id": order_id, "number": number, "product": "Деталь",
                                  "customer_id": "c1", "customer_name": "Анна",
                                  "status": "queue", "created_at": now_iso(),
                                  "updated_at": now_iso(), **fields})

    def test_archived_order_leaves_the_board(self):
        self.assertEqual([r["id"] for r in self.repo.orders()], ["o1"])
        self.repo.archive_order("o1")
        self.assertEqual([], self.repo.orders(), "архивный заказ остался на доске")
        self.assertEqual([r["id"] for r in self.repo.orders(only_archived=True)], ["o1"])
        self.assertEqual([r["id"] for r in self.repo.orders(include_archived=True)], ["o1"])

    def test_archiving_keeps_rows_items_and_money(self):
        self.repo.archive_order("o1")
        row = self.repo.order("o1")
        self.assertIsNotNone(row, "строка заказа пропала")
        self.assertEqual(1000, row["price"])
        self.assertEqual(1000, row["paid"])
        self.assertEqual(1, len(row["items"]), "состав заказа стёрт")
        self.assertTrue(str(row["archived_at"]).strip(), "не отмечено, когда сняли с доски")
        # Учёт читает таблицу своим запросом и архив не замечает.
        customer = [c for c in self.repo.customers() if c["id"] == "c1"][0]
        self.assertEqual(1, customer["orders"])
        self.assertEqual(1000, customer["revenue"],
                         "архив изменил деньги клиента — так нельзя")

    def test_restore_puts_the_order_back(self):
        self.repo.archive_order("o1")
        self.repo.archive_order("o1", archived=False)
        self.assertEqual([r["id"] for r in self.repo.orders()], ["o1"])
        self.assertEqual("", self.repo.order("o1")["archived_at"])

    def test_archive_route(self):
        code, payload = self.api.post("/api/order/archive", {"id": "o1"}, {})
        self.assertEqual(200, code)
        self.assertEqual(1, payload["order"]["archived"])
        code, payload = self.api.post("/api/order/archive",
                                      {"id": "o1", "archived": False}, {})
        self.assertEqual(200, code)
        self.assertEqual(0, payload["order"]["archived"])

    def test_archive_route_rejects_unknown_order(self):
        code, payload = self.api.post("/api/order/archive", {"id": "нет"}, {})
        self.assertEqual(400, code)
        self.assertIn("не найден", payload["error"].lower())
        code, _ = self.api.post("/api/order/archive", {}, {})
        self.assertEqual(400, code)

    def test_orders_route_filters_archive(self):
        self.repo.archive_order("o1")
        self.order("o2", "1002", price=200)
        _, live = self.api.get("/api/orders", {})
        self.assertEqual(["o2"], [r["id"] for r in live["orders"]])
        _, archived = self.api.get("/api/orders", {"archived": ["1"]})
        self.assertEqual(["o1"], [r["id"] for r in archived["orders"]])
        _, everything = self.api.get("/api/orders", {"archived": ["all"]})
        self.assertEqual({"o1", "o2"}, {r["id"] for r in everything["orders"]})


if __name__ == "__main__":
    unittest.main()

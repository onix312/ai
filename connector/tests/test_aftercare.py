"""После продажи: запрос отзыва, ответ, разрешение и безопасный повтор."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.aftercare import CustomerAftercare
from connector.printflow.api import Api
from connector.printflow.clients import Clients
from connector.printflow.config import now_iso
from connector.printflow.db import Database, SCHEMA_VERSION
from connector.printflow.repo import Repo


class AftercareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "aftercare.sqlite3")
        self.repo = Repo(self.db)
        self.service = CustomerAftercare(self.db, self.repo)
        self.db.set_settings({"feedback_delay_days": 0})

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def order(self, order_id="order-1", **overrides):
        data = {
            "id": order_id, "number": "1001", "product": "Корпус датчика",
            "customer_name": "Мария", "phone": "+79990000000", "messenger": "@maria",
            "status": "done", "price": 1000, "paid": 1000,
            "created_at": now_iso(), "closed_at": now_iso(), "updated_at": now_iso(),
        }
        data.update(overrides)
        return self.db.upsert("orders", data)

    def send(self, order_id="order-1", request_id="send-1"):
        return self.service.confirm_request(
            order_id, sent_confirmed=True, request_id=request_id)

    def respond(self, order_id="order-1", request_id="response-1", **overrides):
        data = {
            "response_received": True, "rating": 5, "text": "Всё отлично",
            "publish_permission": "granted", "repeat_interest": "yes",
            "request_id": request_id,
        }
        data.update(overrides)
        return self.service.record_response(order_id, **data)

    def test_preview_prepares_text_without_recording_external_send(self):
        self.order()
        first = self.service.summary("order-1")
        second = self.service.summary("order-1")
        self.assertEqual(first["state"], "ready")
        self.assertIn("Корпус датчика", first["message"])
        self.assertFalse(first["external_sent_by_printflow"])
        self.assertFalse(first["publish_action_performed"])
        self.assertEqual(second["message"], first["message"])
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM customer_feedback")["n"], 0)

    def test_only_final_paid_order_is_eligible(self):
        self.order(status="work")
        with self.assertRaisesRegex(ValueError, "после выдачи"):
            self.service.summary("order-1")
        self.db.delete("orders", "order-1")
        self.order(price=1000, paid=900)
        with self.assertRaisesRegex(ValueError, "долг"):
            self.service.summary("order-1")

    def test_sent_confirmation_is_required_and_idempotent(self):
        self.order()
        with self.assertRaisesRegex(ValueError, "действительно отправлен"):
            self.service.confirm_request("order-1", request_id="send-1")
        first = self.send()
        second = self.send()
        third = self.service.confirm_request(
            "order-1", sent_confirmed=True, request_id="another-browser-retry")
        self.assertFalse(first["already_recorded"])
        self.assertTrue(second["already_recorded"])
        self.assertTrue(third["already_recorded"])
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM customer_feedback")["n"], 1)
        self.assertEqual(self.service.summary("order-1")["state"], "waiting")

    def test_response_keeps_rating_text_and_permissions_separate(self):
        self.order()
        self.send()
        result = self.respond(
            rating=2, text="Крепление люфтит", publish_permission="denied",
            repeat_interest="no")
        retry = self.respond(
            rating=2, text="Крепление люфтит", publish_permission="denied",
            repeat_interest="no")
        self.assertTrue(retry["already_recorded"])
        feedback = result["feedback"]
        self.assertEqual(feedback["rating"], 2)
        self.assertEqual(feedback["feedback_text"], "Крепление люфтит")
        self.assertEqual(feedback["publish_permission"], "denied")
        self.assertEqual(feedback["repeat_interest"], "no")
        self.assertTrue(result["needs_attention"])
        self.assertFalse(result["publish_action_performed"])
        self.assertIn("исправить", result["message_after_feedback"])

    def test_response_requires_real_request_and_literal_confirmation(self):
        self.order()
        with self.assertRaisesRegex(ValueError, "Сначала подтвердите"):
            self.respond()
        self.send()
        with self.assertRaisesRegex(ValueError, "действительно получен"):
            self.respond(response_received=False)
        with self.assertRaisesRegex(ValueError, "от 1 до 5"):
            self.respond(rating=6)
        self.assertFalse(self.db.one("SELECT * FROM customer_feedback")["feedback_received_at"])

    def test_repeat_draft_requires_client_interest_and_operator_confirmation(self):
        self.order()
        self.send()
        self.respond()
        with self.assertRaisesRegex(ValueError, "Подтвердите создание"):
            self.service.prepare_repeat("order-1", request_id="repeat-1")
        first = self.service.prepare_repeat(
            "order-1", repeat_confirmed=True, request_id="repeat-1")
        second = self.service.prepare_repeat(
            "order-1", repeat_confirmed=True, request_id="repeat-1")
        self.assertFalse(first["already_prepared"])
        self.assertTrue(second["already_prepared"])
        self.assertEqual(first["order"]["id"], second["order"]["id"])
        self.assertEqual(first["order"]["status"], "new")
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM orders")["n"], 2)

    def test_repeat_failure_rolls_back_new_order_and_link(self):
        self.order()
        self.send()
        self.respond()
        original_execute = self.db.execute

        def fail_link(sql, params=()):
            if sql.startswith("UPDATE customer_feedback SET repeat_order_id"):
                raise RuntimeError("link failed")
            return original_execute(sql, params)

        with mock.patch.object(self.db, "execute", side_effect=fail_link):
            with self.assertRaisesRegex(RuntimeError, "link failed"):
                self.service.prepare_repeat(
                    "order-1", repeat_confirmed=True, request_id="repeat-failure")
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM orders")["n"], 1)
        feedback = self.db.one("SELECT * FROM customer_feedback WHERE order_id='order-1'")
        self.assertFalse(feedback["repeat_order_id"])
        self.assertFalse(feedback["repeat_request_id"])

    def test_low_rating_or_no_interest_never_creates_repeat(self):
        self.order()
        self.send()
        self.respond(rating=1, repeat_interest="no", publish_permission="denied")
        with self.assertRaisesRegex(ValueError, "не подтвердил интерес"):
            self.service.prepare_repeat(
                "order-1", repeat_confirmed=True, request_id="repeat-no")
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM orders")["n"], 1)

    def test_queue_moves_order_through_safe_states_and_skips_debt(self):
        self.order()
        self.order("order-debt", number="1002", paid=0)
        self.assertEqual(self.service.queue()["counts"]["ready"], 1)
        self.send()
        queued = self.service.queue()
        self.assertEqual(queued["counts"]["waiting"], 1)
        self.respond()
        self.assertEqual(self.service.queue()["counts"]["received"], 1)

    def test_rfm_uses_requested_history_window(self):
        customer = self.repo.save_customer({"name": "Мария", "phone": "+7999"})
        self.order(customer_id=customer["id"], created_at=now_iso())
        row = next(item for item in Clients(self.db).rfm(90) if item["id"] == customer["id"])
        self.assertEqual(row["orders"], 1)
        self.assertEqual(row["paid"], 1000)

    def test_schema_version_and_feedback_indexes(self):
        self.assertEqual(SCHEMA_VERSION, 12)
        indexes = {row["name"] for row in self.db.query("PRAGMA index_list(customer_feedback)")}
        self.assertIn("idx_feedback_order", indexes)
        self.assertIn("idx_feedback_request", indexes)
        self.assertIn("idx_feedback_response_request", indexes)
        self.assertIn("idx_feedback_repeat_request", indexes)


class AftercareApiRouteTests(unittest.TestCase):
    def setUp(self):
        self.api = Api.__new__(Api)
        self.api.aftercare = mock.Mock()

    def test_summary_and_queue_routes(self):
        self.api.aftercare.summary.return_value = {"state": "ready"}
        code, payload = self.api.get("/api/aftercare/summary", {"id": ["order-1"]})
        self.assertEqual((code, payload), (200, {"state": "ready"}))
        self.api.aftercare.queue.return_value = {"items": []}
        code, payload = self.api.get("/api/aftercare/queue", {"limit": ["20"]})
        self.assertEqual((code, payload), (200, {"items": []}))
        self.api.aftercare.queue.assert_called_once_with(20)

    def test_request_route_requires_literal_true(self):
        self.api.aftercare.confirm_request.return_value = {"state": "waiting"}
        code, payload = self.api.post(
            "/api/aftercare/request/confirm",
            {"id": "order-1", "sent_confirmed": "true", "force": True,
             "request_id": "send-1"}, {},
        )
        self.assertEqual((code, payload), (200, {"state": "waiting"}))
        self.api.aftercare.confirm_request.assert_called_once_with(
            "order-1", sent_confirmed=False, force=True, request_id="send-1")

    def test_response_route_keeps_consent_separate(self):
        self.api.aftercare.record_response.return_value = {"state": "received"}
        code, _ = self.api.post(
            "/api/aftercare/response",
            {"id": "order-1", "response_received": True, "rating": "4",
             "text": "Хорошо", "publish_permission": "granted",
             "repeat_interest": "not_asked", "request_id": "response-1"}, {},
        )
        self.assertEqual(code, 200)
        self.api.aftercare.record_response.assert_called_once_with(
            "order-1", response_received=True, rating="4", text="Хорошо",
            publish_permission="granted", repeat_interest="not_asked",
            request_id="response-1")


if __name__ == "__main__":
    unittest.main()

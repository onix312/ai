"""Идея 61: резюме диалога покупателя (локально-экстрактивный разбор).

Проверяются правила извлечения (открытые вопросы, суммы, сроки, телефоны),
маршрут /api/client-bot/thread-summary и пустые кейсы.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.thread_summary import summarize  # noqa: E402
from connector.tests.test_phase11 import make_api, make_db  # noqa: E402


def msg(at, direction, text, answer="", kind="message", operator=""):
    return {"at": at, "direction": direction, "kind": kind, "text": text,
            "answer": answer, "operator": operator}


class SummarizeTests(unittest.TestCase):
    def test_empty(self):
        out = summarize([])
        self.assertTrue(out["empty"] if "empty" in out else out["counts"]["total"] == 0)
        self.assertEqual(out["verdict"], "Диалог пуст")
        self.assertEqual(out["summary"], "")

    def test_client_wait_answer_verdict(self):
        rows = [
            msg("2026-09-01T10:00:00", "in", "Когда будет готов адресник?"),
        ]
        out = summarize(rows)
        self.assertEqual(out["last_direction"], "in")
        self.assertIn("ждёт ответа", out["verdict"])
        self.assertEqual(len(out["open_questions"]), 1)
        self.assertIn("Когда", out["open_questions"][0]["text"])

    def test_operator_replied_no_open_question(self):
        rows = [
            msg("2026-09-01T10:00:00", "in", "Когда будет готов заказ?"),
            msg("2026-09-01T10:05:00", "out", "Готов завтра к 18:00", operator="Анна"),
        ]
        out = summarize(rows)
        self.assertEqual(out["open_questions"], [])
        self.assertIn("мяч на стороне покупателя", out["verdict"])
        self.assertTrue(any("завтра" in d for d in out["deadlines"]))

    def test_amounts_and_phone_extracted(self):
        rows = [
            msg("2026-09-01T10:00:00", "in", "Предоплата 1 500 ₽ переведена"),
            msg("2026-09-01T10:01:00", "in", "+7 912 345-67-89 — мой новый номер"),
        ]
        out = summarize(rows)
        self.assertTrue(any("1 500" in a for a in out["amounts"]))
        self.assertTrue(any("912" in p for p in out["phones"]))

    def test_filler_messages_not_highlights(self):
        rows = [
            msg("2026-09-01T10:00:00", "in", "ок"),
            msg("2026-09-01T10:01:00", "in", "Спасибо!"),
            msg("2026-09-01T10:02:00", "in", "А цвет можно серый матовый?"),
        ]
        out = summarize(rows)
        texts = [h["text"] for h in out["highlights"]]
        self.assertEqual(len(texts), 1)
        self.assertIn("серый", texts[0])

    def test_open_question_capped_at_three(self):
        rows = [msg(f"2026-09-01T10:0{i}:00", "in", f"Вопрос номер {i}? И ещё?")
                for i in range(6)]
        rows.append(msg("2026-09-01T11:00:00", "out", "Отвечаю на всё"))
        out = summarize(rows)
        self.assertLessEqual(len(out["open_questions"]), 3)


class ThreadSummaryApiTests(unittest.TestCase):
    def setUp(self):
        self.api = make_api(make_db())
        from connector.printflow.api import register_routes

        register_routes()

    def test_requires_order_id(self):
        with self.assertRaises(ValueError):
            self.api.get("/api/client-bot/thread-summary", {})

    def test_unknown_order_is_empty_verdict(self):
        self.api.db.one = mock_one({})   # заказа и ссылки нет
        code, payload = self.api.get("/api/client-bot/thread-summary",
                                     {"order_id": ["nope"]})
        self.assertEqual(code, 200)
        self.assertTrue(payload["empty"])
        self.assertIn("Чат не привязан", payload["verdict"])

    def test_summary_from_log(self):
        db = self.api.db
        db.execute("INSERT INTO client_orders(order_id, chat_id) VALUES('o1','c9')")
        db.execute("INSERT INTO client_bot_log(chat_id, at, direction, text, answer)"
                   " VALUES('c9','2026-09-01T10:00:00','in','Когда готово?','')")
        db.execute("INSERT INTO client_bot_log(chat_id, at, direction, text, answer)"
                   " VALUES('c9','2026-09-01T10:05:00','out','','Готово завтра')")
        code, payload = self.api.get("/api/client-bot/thread-summary",
                                     {"order_id": ["o1"]})
        self.assertEqual(code, 200)
        self.assertFalse(payload["empty"])
        self.assertEqual(payload["counts"]["total"], 2)
        self.assertEqual(payload["counts"]["in"], 1)
        self.assertEqual(payload["open_questions"], [])   # мастер ответил позже
        self.assertIn("мяч", payload["verdict"])
        self.assertTrue(any("завтра" in d for d in payload["deadlines"]))


def mock_one(mapping):
    def _one(sql, params=()):
        return mapping.get(sql.strip()[:40])
    return _one


if __name__ == "__main__":
    unittest.main()

"""Единый инбокс диалогов (Н55).

Раньше «кому я должен ответить» собиралось обходом трёх мест: входящие
клиент-бота, карточка отзывов и лог рабочего бота. Здесь одна лента с
фильтром по каналу, поиском и признаком «ждёт ответа».
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.conversations import (  # noqa: E402
    CHANNELS, NEEDS_ANSWER, Conversations)
from connector.printflow.db import Database  # noqa: E402


class ConversationFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.service = Conversations(self.db)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def add_client(self, chat_id="555", name="Мария", text="здравствуйте",
                   unread=1, at="2026-08-31T10:00:00", direction="in", answer=""):
        self.db.execute(
            "INSERT INTO client_bot_log(at,chat_id,name,text,answer,direction,unread)"
            " VALUES(?,?,?,?,?,?,?)",
            (at, chat_id, name, text, answer, direction, unread))

    def add_review(self, order_id="o1", chat_id="555", rating="bad",
                   comment="расслоение", state="needs_attention"):
        self.db.execute(
            "INSERT INTO client_reviews(order_id,chat_id,rating,comment,state,created_at)"
            " VALUES(?,?,?,?,?,?)", (order_id, chat_id, rating, comment, state,
                                     "2026-08-30T12:00:00"))

    def add_staff_update(self, update_id="91", state="done", error="",
                         at="2026-08-31T09:00:00"):
        self.db.execute(
            "INSERT INTO telegram_bot_updates(update_id,state,received_at,error)"
            " VALUES(?,?,?,?)", (update_id, state, at, error))


class ChannelTests(ConversationFixture):
    def test_client_thread_waits_while_unanswered(self):
        self.add_client()
        rows = self.service.threads()
        self.assertEqual(1, len(rows))
        self.assertEqual("client", rows[0]["channel"])
        self.assertEqual("waiting", rows[0]["state"])
        self.assertEqual(1, rows[0]["unread"])

    def test_answered_thread_is_not_waiting(self):
        self.add_client(chat_id="777", name="Иван", text="когда готово",
                        answer="завтра", unread=0, direction="out")
        row = self.service.threads()[0]
        self.assertEqual("answered", row["state"])
        self.assertEqual(0, row["unread"])

    def test_review_with_composite_key_is_addressable(self):
        """У client_reviews нет суррогатного id — ключ составной."""
        self.db.upsert("orders", {"id": "o1", "number": "1001", "product": "крючок",
                                  "price": 500, "created_at": "2026-08-30T10:00:00",
                                  "updated_at": "2026-08-30T10:00:00"})
        self.add_review()
        row = self.service.threads()[0]
        self.assertEqual("review:o1:555", row["id"])
        self.assertEqual("needs_attention", row["state"])
        thread = self.service.thread(row["id"])
        self.assertEqual("расслоение", thread["review"]["comment"])
        self.assertEqual("1001", thread["review"]["number"])

    def test_staff_update_shows_outcome_without_text(self):
        """Лог рабочего бота не хранит текст — только факт и результат."""
        self.add_staff_update(update_id="92", state="failed", error="сеть")
        row = self.service.threads()[0]
        self.assertEqual("staff", row["channel"])
        self.assertIn("сеть", row["text"])
        self.assertEqual("waiting", row["state"])

    def test_all_three_channels_are_listed(self):
        self.db.upsert("orders", {"id": "o1", "number": "1001", "product": "крючок",
                                  "price": 500, "created_at": "2026-08-30T10:00:00",
                                  "updated_at": "2026-08-30T10:00:00"})
        self.add_client()
        self.add_review()
        self.add_staff_update()
        channels = {row["channel"] for row in self.service.threads()}
        self.assertEqual(set(CHANNELS), channels)

    def test_summary_counts_threads_per_channel(self):
        self.add_client(chat_id="555")
        self.add_client(chat_id="555", text="ещё вопрос", at="2026-08-31T10:01:00")
        self.add_client(chat_id="777", name="Иван")
        summary = self.service.summary()
        self.assertEqual(2, summary["counts"]["client"], "два чата, а не два сообщения")
        self.assertEqual(2, summary["total"])


class FilterTests(ConversationFixture):
    def setUp(self):
        super().setUp()
        self.db.upsert("orders", {"id": "o1", "number": "1001", "product": "крючок",
                                  "price": 500, "created_at": "2026-08-30T10:00:00",
                                  "updated_at": "2026-08-30T10:00:00"})
        self.add_client()
        self.add_review()
        self.add_staff_update(update_id="92", state="failed", error="сеть")

    def test_channel_filter(self):
        self.assertEqual(1, len(self.service.threads(channel="client")))
        self.assertEqual(1, len(self.service.threads(channel="review")))
        self.assertEqual(1, len(self.service.threads(channel="staff")))

    def test_unknown_channel_returns_nothing(self):
        self.assertEqual([], self.service.threads(channel="нет_такого"))

    def test_needs_answer_filter(self):
        rows = self.service.threads(needs_answer=True)
        self.assertEqual(3, len(rows))
        for row in rows:
            self.assertIn(row["state"], NEEDS_ANSWER)

    def test_unread_filter(self):
        self.assertEqual(3, len(self.service.threads(unread_only=True)))
        self.add_client(chat_id="888", name="Пётр", text="спасибо", unread=0,
                        at="2026-08-31T11:00:00", direction="out", answer="пожалуйста")
        self.assertEqual(3, len(self.service.threads(unread_only=True)),
                         "прочитанный диалог не попадает в непрочитанные")

    def test_search_matches_review_not_staff(self):
        """Поиск обязан работать во всех каналах одинаково.

        Раньше в ветке сотрудников фильтр игнорировался, и «расслоение»
        находило команды боту.
        """
        rows = self.service.threads(q="расслоение")
        self.assertEqual(["review"], [row["channel"] for row in rows])

    def test_search_matches_staff_error(self):
        rows = self.service.threads(q="сеть")
        self.assertEqual(["staff"], [row["channel"] for row in rows])

    def test_search_matches_update_id(self):
        rows = self.service.threads(q="92")
        self.assertEqual(["staff"], [row["channel"] for row in rows])

    def test_search_is_case_insensitive(self):
        self.add_client(chat_id="999", name="Ann", text="Hello THERE",
                        at="2026-08-31T12:00:00")
        self.assertEqual(1, len(self.service.threads(q="hello there")))

    def test_limit_caps_result(self):
        for index in range(5):
            self.add_client(chat_id=str(1000 + index), name=f"К{index}",
                            at=f"2026-08-31T13:0{index}:00")
        self.assertLessEqual(len(self.service.threads(limit=2)), 2 * len(CHANNELS))


class ThreadTests(ConversationFixture):
    def test_thread_returns_history_newest_last(self):
        self.add_client(at="2026-08-31T10:00:00", text="здравствуйте")
        self.add_client(at="2026-08-31T10:01:00", text="сколько стоит?")
        thread = self.service.thread("client:555")
        self.assertEqual("Мария", thread["name"])
        self.assertEqual(["здравствуйте", "сколько стоит?"],
                         [message["text"] for message in thread["messages"]])

    def test_thread_carries_client_profile(self):
        self.db.execute("INSERT INTO client_chats(chat_id,name,phone,phone_verified)"
                        " VALUES('555','Мария','+7',1)")
        self.add_client()
        profile = self.service.thread("client:555")["profile"]
        self.assertEqual("+7", profile["phone"])
        self.assertEqual(1, profile["phone_verified"])

    def test_empty_key_returns_empty_thread(self):
        thread = self.service.thread("")
        self.assertEqual([], thread["messages"])
        self.assertEqual("", thread["channel"])

    def test_unknown_channel_returns_empty_thread(self):
        thread = self.service.thread("нет_такого:123")
        self.assertEqual([], thread["messages"])

    def test_missing_thread_is_not_an_error(self):
        thread = self.service.thread("client:000")
        self.assertEqual([], thread["messages"])


if __name__ == "__main__":
    unittest.main()

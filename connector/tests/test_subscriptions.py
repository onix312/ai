"""Подписки сотрудников на события (Н54).

Раньше любое уведомление уходило всей команде: ночной дефект будил
бухгалтера, а алерты по деньгам — мастера. Теперь у каждого свой набор,
при этом отсутствие подписок означает «получать важное» — иначе первый же
запуск после обновления оставил бы цех без уведомлений.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import subscriptions  # noqa: E402
from connector.printflow.db import Database  # noqa: E402


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.execute("INSERT INTO staff(id,name,role,chat_id,active)"
                        " VALUES('s1','Мастер','employee','111',1)")
        self.db.execute("INSERT INTO staff(id,name,role,chat_id,active)"
                        " VALUES('s2','Бухгалтер','accountant','222',1)")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    # ------------------------------------------------------------ каталог
    def test_catalog_covers_every_event(self):
        events = {item["event"] for item in subscriptions.catalog()}
        self.assertEqual(set(subscriptions.EVENTS), events)

    def test_catalog_items_are_shaped_for_ui(self):
        for item in subscriptions.catalog():
            self.assertTrue(item["label"], f"{item['event']}: нет подписи")
            self.assertTrue(item["group"], f"{item['event']}: нет группы")
            self.assertIn(item["default"], (True, False))

    def test_defaults_are_subset_of_events(self):
        self.assertTrue(subscriptions.DEFAULT_ON <= set(subscriptions.EVENTS))

    def test_unknown_event_is_rejected(self):
        self.assertFalse(subscriptions.is_known("не_событие"))
        self.assertTrue(subscriptions.is_known("filament_low"))

    # -------------------------------------------------------- подписки
    def test_without_rows_everyone_gets_default_on_events(self):
        """Нет ни одной строки — действуют умолчания, а не тишина."""
        for event in subscriptions.DEFAULT_ON:
            names = [p["name"] for p in subscriptions.subscribers(self.db, event)]
            self.assertEqual(["Мастер", "Бухгалтер"], names, event)

    def test_explicit_off_overrides_default(self):
        subscriptions.set_many(self.db, "s2", {"filament_low": False})
        names = [p["name"] for p in subscriptions.subscribers(self.db, "filament_low")]
        self.assertEqual(["Мастер"], names)

    def test_first_subscription_row_switches_staff_to_explicit_mode(self):
        """Появление любой строки отключает режим «получать всё».

        До первой записи сотрудник считается новичком и получает все события.
        Как только он хоть что-то настроил, неуказанное не приходит — иначе
        выбор превратился бы в декорацию.
        """
        everyone = [p["name"] for p in subscriptions.subscribers(self.db, "money_low")]
        self.assertEqual(["Мастер", "Бухгалтер"], everyone)

        subscriptions.set_many(self.db, "s1", {"filament_low": True})
        after_row = [p["name"] for p in subscriptions.subscribers(self.db, "money_low")]
        self.assertEqual(["Бухгалтер"], after_row,
                         "неуказанное событие больше не приходит настроенному")

        subscriptions.set_many(self.db, "s1", {"money_low": True})
        explicit = [p["name"] for p in subscriptions.subscribers(self.db, "money_low")]
        self.assertEqual(["Мастер", "Бухгалтер"], explicit)

    def test_unknown_events_are_dropped_not_saved(self):
        subscriptions.set_many(self.db, "s1", {"не_событие": True, "order_ready": False})
        row = self.db.one("SELECT * FROM staff_subscriptions WHERE staff_id='s1'")
        self.assertEqual("order_ready", row["event"])
        self.assertEqual(1, self.db.one(
            "SELECT COUNT(*) AS n FROM staff_subscriptions")["n"])

    def test_reset_returns_defaults(self):
        subscriptions.set_many(self.db, "s2", {"filament_low": False})
        after = subscriptions.reset(self.db, "s2")
        self.assertTrue(after["filament_low"])
        self.assertEqual(0, self.db.one(
            "SELECT COUNT(*) AS n FROM staff_subscriptions WHERE staff_id='s2'")["n"])

    def test_inactive_staff_is_excluded(self):
        self.db.execute("UPDATE staff SET active=0 WHERE id='s2'")
        names = [p["name"] for p in subscriptions.subscribers(self.db, "filament_low")]
        self.assertEqual(["Мастер"], names)
        both = [p["name"] for p in subscriptions.subscribers(
            self.db, "filament_low", include_inactive=True)]
        self.assertEqual(["Мастер", "Бухгалтер"], both)

    def test_staff_without_chat_id_cannot_receive(self):
        self.db.execute("UPDATE staff SET chat_id='' WHERE id='s2'")
        names = [p["name"] for p in subscriptions.subscribers(self.db, "filament_low")]
        self.assertEqual(["Мастер"], names)

    def test_unknown_event_has_no_subscribers(self):
        self.assertEqual([], subscriptions.subscribers(self.db, "не_событие"))

    # --------------------------------------------------------- маршруты
    def test_route_returns_subscriber_chats(self):
        subscriptions.set_many(self.db, "s2", {"filament_low": False})
        result = subscriptions.route(self.db, "filament_low")
        self.assertEqual(["111"], result["chats"])
        self.assertFalse(result["use_fallback"])

    def test_route_falls_back_when_nobody_subscribed(self):
        subscriptions.set_many(self.db, "s1", {"money_low": False})
        subscriptions.set_many(self.db, "s2", {"money_low": False})
        result = subscriptions.route(self.db, "money_low")
        self.assertEqual([], result["chats"])
        self.assertTrue(result["use_fallback"],
                        "без подписчиков событие обязано уйти в общий чат")

    def test_critical_event_always_uses_fallback(self):
        """Авария идёт всем и в общий чат: подписка не повод промолчать."""
        subscriptions.set_many(self.db, "s1", {"printer_error": False})
        result = subscriptions.route(self.db, "printer_error", critical=True)
        self.assertTrue(result["use_fallback"])

    def test_eventless_route_uses_fallback(self):
        result = subscriptions.route(self.db, "")
        self.assertEqual([], result["chats"])
        self.assertTrue(result["use_fallback"])


if __name__ == "__main__":
    unittest.main()

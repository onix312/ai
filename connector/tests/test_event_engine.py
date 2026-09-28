"""Proactivity / Event Engine 1.0 contracts."""
from __future__ import annotations

import datetime as dt
import pathlib
import tempfile
import unittest

from agent.autonomy import AutonomyPolicy
from agent.event_engine import EventEngine
from agent.personal import Personal
from agent.store import Store


class _Runner:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.personal = Personal(store)


class _Tasks:
    def __init__(self) -> None:
        self.rows = []

    def list(self, _limit: int = 100):
        return list(self.rows)


class _Agent:
    def __init__(self, store: Store) -> None:
        self.runner = _Runner(store)
        self.autonomy = AutonomyPolicy(store)
        self.tasks = _Tasks()


class EventEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "assistant.sqlite3")
        self.agent = _Agent(self.store)
        self.events = EventEngine(self.agent)
        self.noon = dt.datetime(2026, 9, 29, 12, 0, 0)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_defaults_are_notification_only_and_autopilot_off(self):
        payload = self.events.payload()
        self.assertEqual("balanced", payload["settings"]["mode"])
        self.assertEqual(2, payload["settings"]["max_nonurgent_per_hour"])
        self.assertFalse(payload["autopilot"])
        self.assertIn("только уведомления", payload["invariant"])

    def test_proactive_event_is_journaled_but_suppressed_without_autopilot(self):
        result = self.events.emit(
            source="test", event_key="one", title="Событие",
            text="не прерывать", urgency="important", now=self.noon,
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["delivered"])
        self.assertEqual("suppressed", result["event"]["state"])
        self.assertIn("Autopilot", result["reason"])
        self.assertEqual([], self.agent.runner.personal.unseen())

    def test_autopilot_balanced_delivers_normal_notification(self):
        self.agent.autonomy.update("autopilot")
        result = self.events.emit(
            source="goal", event_key="goal:1", title="Цель",
            text="завтра срок", urgency="normal", provider="personal", now=self.noon,
        )
        self.assertTrue(result["delivered"])
        self.assertEqual("delivered", result["event"]["state"])
        unseen = self.agent.runner.personal.unseen()
        self.assertEqual(1, len(unseen))
        self.assertEqual("Цель", unseen[0]["title"])

    def test_quiet_hours_suppress_normal_but_urgent_breaks_through(self):
        self.agent.autonomy.update("autopilot")
        night = dt.datetime(2026, 9, 29, 23, 30, 0)
        normal = self.events.emit(
            source="test", event_key="night-normal", title="Ночь",
            urgency="normal", now=night,
        )
        urgent = self.events.emit(
            source="test", event_key="night-urgent", title="Срочно",
            urgency="urgent", now=night,
        )
        self.assertFalse(normal["delivered"])
        self.assertEqual("quiet hours", normal["reason"])
        self.assertTrue(urgent["delivered"])

    def test_hourly_budget_limits_nonurgent_interruptions(self):
        self.agent.autonomy.update("autopilot")
        self.events.update({"max_nonurgent_per_hour": 1, "cooldown_minutes": 5})
        first = self.events.emit(
            source="a", event_key="a1", kind="alpha", title="A",
            urgency="normal", now=self.noon,
        )
        second = self.events.emit(
            source="b", event_key="b1", kind="beta", title="B",
            urgency="normal", now=self.noon + dt.timedelta(minutes=1),
        )
        self.assertTrue(first["delivered"])
        self.assertFalse(second["delivered"])
        self.assertIn("attention budget", second["reason"])

    def test_cooldown_applies_across_same_event_kind(self):
        self.agent.autonomy.update("autopilot")
        self.events.update({"max_nonurgent_per_hour": 10, "cooldown_minutes": 30})
        first = self.events.emit(
            source="task", event_key="first", kind="task_failed", title="Задача 1",
            urgency="important", now=self.noon,
        )
        second = self.events.emit(
            source="task", event_key="second", kind="task_failed", title="Задача 2",
            urgency="important", now=self.noon + dt.timedelta(minutes=5),
        )
        self.assertTrue(first["delivered"])
        self.assertFalse(second["delivered"])
        self.assertIn("cooldown", second["reason"])

    def test_same_key_is_deduplicated_and_occurrence_count_grows(self):
        self.agent.autonomy.update("autopilot")
        first = self.events.emit(
            source="test", event_key="same", title="Same", now=self.noon,
        )
        second = self.events.emit(
            source="test", event_key="same", title="Same",
            now=self.noon + dt.timedelta(minutes=1),
        )
        self.assertFalse(first.get("deduped", False))
        self.assertTrue(second["deduped"])
        self.assertEqual(2, second["event"]["occurrences"])
        self.assertEqual(first["event"]["id"], second["event"]["id"])

    def test_explicit_notification_is_mirrored_even_without_autopilot(self):
        note = self.agent.runner.personal.notify("reminder", "Напоминание", "Позвонить", 7)
        event = self.events.record_explicit(note, self.noon)
        self.assertEqual("delivered", event["state"])
        self.assertTrue(event["explicit"])
        self.assertEqual(note["id"], event["notification_id"])
        again = self.events.record_explicit(note, self.noon)
        self.assertEqual(event["id"], again["id"])

    def test_goal_deadline_scanner_is_proactive_only(self):
        self.agent.autonomy.update("autopilot")
        self.agent.runner.personal.add_goal(
            "Отправить макет", target=1, unit="макет",
            deadline=self.noon.date() + dt.timedelta(days=1),
        )
        delivered = self.events.tick(self.noon)
        self.assertEqual(1, len(delivered))
        self.assertIn("Завтра срок цели", delivered[0]["text"])

    def test_failed_task_scanner_surfaces_once(self):
        self.agent.autonomy.update("autopilot")
        self.agent.tasks.rows = [{
            "id": 9, "title": "Подготовить ПК", "status": "failed",
            "updated_at": "2026-09-29 11:59:00", "error": "Telegram не открылся",
            "current_step": 1,
        }]
        first = self.events.tick(self.noon)
        second = self.events.tick(self.noon + dt.timedelta(minutes=1))
        self.assertEqual(1, len(first))
        self.assertEqual([], second)
        rows = self.events.list()
        task_rows = [row for row in rows if row["kind"] == "task_failed"]
        self.assertEqual(1, len(task_rows))
        self.assertEqual(1, task_rows[0]["occurrences"])

    def test_settings_validation_is_atomic(self):
        result = self.events.update({
            "mode": "active",
            "quiet_start": "99:99",
        })
        self.assertFalse(result["ok"])
        self.assertEqual("balanced", self.events.settings()["mode"])


if __name__ == "__main__":
    unittest.main()

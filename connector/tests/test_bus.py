"""Тесты шины событий: доставка, отставшие вкладки, отписка, телеметрия.

Проверяем поведение, из-за которого раньше сервер работал вхолостую:
события должны доходить до всех вкладок, медленная вкладка не должна
копить мусор, а фоновый поток — просыпаться, когда никто не смотрит.
"""
from __future__ import annotations

import pathlib
import sys
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.bus import EventBus, LiveBroadcaster  # noqa: E402
from connector.printflow.db import Database  # noqa: E402


class TestEventBus(unittest.TestCase):
    def test_delivers_to_every_subscriber(self):
        bus = EventBus()
        first, second = bus.subscribe(), bus.subscribe()
        self.assertEqual(bus.publish("event", {"id": 1}), 2)
        self.assertEqual(first.get(0.1), ("event", {"id": 1}))
        self.assertEqual(second.get(0.1), ("event", {"id": 1}))

    def test_unsubscribed_tab_gets_nothing(self):
        bus = EventBus()
        subscriber = bus.subscribe()
        bus.unsubscribe(subscriber)
        self.assertEqual(bus.publish("event", {}), 0)
        self.assertIsNone(subscriber.get(0.05))

    def test_subscription_context_always_releases(self):
        bus = EventBus()
        with self.assertRaises(RuntimeError):
            with bus.subscription():
                self.assertEqual(bus.listeners, 1)
                raise RuntimeError("вкладка отвалилась")
        self.assertEqual(bus.listeners, 0)

    def test_slow_tab_gets_resync_instead_of_endless_queue(self):
        bus = EventBus(limit=4)
        subscriber = bus.subscribe()
        for index in range(20):          # вкладка «уснула» и ничего не читает
            bus.publish("telemetry", {"n": index})
        self.assertTrue(subscriber.overflow)
        self.assertLessEqual(subscriber.queue.qsize(), 4)
        kinds = []
        while True:
            message = subscriber.get(0.01)
            if message is None:
                break
            kinds.append(message[0])
        self.assertIn("resync", kinds, "отставшую вкладку надо попросить перечитать данные")

    def test_get_returns_none_on_timeout(self):
        bus = EventBus()
        subscriber = bus.subscribe()
        started = time.time()
        self.assertIsNone(subscriber.get(0.05))
        self.assertLess(time.time() - started, 1.0)

    def test_publish_is_thread_safe(self):
        bus = EventBus(limit=500)
        subscriber = bus.subscribe()

        def worker():
            for _ in range(50):
                bus.publish("event", {})

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(subscriber.queue.qsize(), 200)


class TestDatabasePublishes(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "test.sqlite3")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_event_reaches_subscribers(self):
        bus = EventBus()
        self.db.bus = bus
        subscriber = bus.subscribe()
        self.db.add_event("complete", "Печать завершена", "адресник")
        message = subscriber.get(0.2)
        self.assertIsNotNone(message)
        self.assertEqual(message[0], "event")
        self.assertEqual(message[1]["kind"], "complete")
        self.assertEqual(message[1]["title"], "Печать завершена")

    def test_database_works_without_bus(self):
        row = self.db.add_event("info", "Без шины")
        self.assertEqual(row["title"], "Без шины")

    def test_broken_bus_does_not_break_writes(self):
        class Broken:
            def publish(self, *_args, **_kwargs):
                raise RuntimeError("шина упала")

        self.db.bus = Broken()
        row = self.db.add_event("info", "Событие всё равно записано")
        self.assertTrue(row["id"])
        self.assertEqual(self.db.events(limit=1)[0]["title"], "Событие всё равно записано")


class FakePrinter:
    def __init__(self, pid: str, connected: bool = True):
        self.id = pid
        self.connected = connected
        self.last_message = 100.0
        self.previous_state = "RUNNING"


class FakeManager:
    def __init__(self, printers):
        self.lock = threading.RLock()
        self.printers = {p.id: p for p in printers}
        self.snapshots = 0

    def snapshot(self, printer_id: str = ""):
        self.snapshots += 1
        return {"printers": [{"id": p.id} for p in self.printers.values()]}


class TestLiveBroadcaster(unittest.TestCase):
    def test_fingerprint_changes_only_with_new_data(self):
        printer = FakePrinter("p1")
        caster = LiveBroadcaster(EventBus(), FakeManager([printer]))
        before = caster.fingerprint()
        self.assertEqual(before, caster.fingerprint())
        printer.last_message += 5
        self.assertNotEqual(before, caster.fingerprint())

    def test_interval_depends_on_connection(self):
        online = LiveBroadcaster(EventBus(), FakeManager([FakePrinter("p1", True)]))
        offline = LiveBroadcaster(EventBus(), FakeManager([FakePrinter("p1", False)]))
        self.assertEqual(online.interval(), LiveBroadcaster.LIVE_INTERVAL)
        self.assertEqual(offline.interval(), LiveBroadcaster.IDLE_INTERVAL)

    def test_no_listeners_means_no_work(self):
        manager = FakeManager([FakePrinter("p1")])
        bus = EventBus()
        caster = LiveBroadcaster(bus, manager)
        caster.LIVE_INTERVAL = 0.01
        caster.start()
        time.sleep(0.1)
        caster.shutdown()
        self.assertEqual(manager.snapshots, 0, "без подписчиков снимок собирать незачем")

    def test_broadcasts_snapshot_to_listener(self):
        manager = FakeManager([FakePrinter("p1")])
        bus = EventBus()
        caster = LiveBroadcaster(bus, manager)
        caster.LIVE_INTERVAL = 0.01
        subscriber = bus.subscribe()
        caster.start()
        message = subscriber.get(2.0)
        caster.shutdown()
        self.assertIsNotNone(message, "подписчик должен получить телеметрию")
        self.assertEqual(message[0], "telemetry")
        self.assertEqual(message[1]["printers"], [{"id": "p1"}])

    def test_unchanged_state_is_not_resent(self):
        manager = FakeManager([FakePrinter("p1")])
        bus = EventBus()
        caster = LiveBroadcaster(bus, manager)
        caster.LIVE_INTERVAL = 0.01
        caster.HEARTBEAT = 60
        bus.subscribe()
        caster.start()
        time.sleep(0.3)
        caster.shutdown()
        self.assertEqual(manager.snapshots, 1,
                         "снимок пересобирается только при изменениях")


if __name__ == "__main__":
    unittest.main()

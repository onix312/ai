"""Присутствие касс и событие каталога (17.0.13).

Раунд «взаимодействие кассы с ПК и панелью» отвечает на два вопроса, которые
раньше решались наугад:

* **касса на связи?** — панель показывает список касс: кто вошёл, когда
  телефон последний раз отвечал и «молчит N мин» (``last_seen``);
* **владелец узнаёт об обрыве?** — сторож шлёт ОДНО сообщение за эпизод
  молчания, а не по одному в каждые 30 секунд, и молчит про истёкшую смену.

Здесь же проверяется, что правка каталога публикует ``catalog_changed``:
без него касса не узнаёт о новой цене, пока кассир сам не перечитает каталог.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import db as db_module        # noqa: E402
from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.cashier import Cashier        # noqa: E402
from connector.printflow.db import Database            # noqa: E402
from connector.printflow.presence import CashierWatcher  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "presence.sqlite3")


def stamp(minutes_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


class SchemaTests(unittest.TestCase):
    def test_last_seen_column_exists_for_fresh_and_old_databases(self):
        # Свежая база: колонка приходит из SCHEMA_V3.
        db = make_db()
        columns = {row["name"] for row in db.query("PRAGMA table_info(cashier_tokens)")}
        self.assertIn("last_seen", columns)
        # Старая база: колонку добавляет догоняющая миграция ADDED_COLUMNS.
        added = {name for name, _decl in db_module.ADDED_COLUMNS.get("cashier_tokens", [])}
        self.assertIn("last_seen", added)


class SessionsTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.db.set_settings({"cashier_code": "1234"})
        self.cashier = Cashier(self.db, self.acc)

    def test_login_marks_cash_as_online(self):
        token = self.cashier.login("1234")["token"]
        report = self.cashier.sessions()
        self.assertEqual(report["total"], 1)
        row = report["sessions"][0]
        self.assertTrue(row["online"], "только что вошедшая касса — на связи")
        self.assertIsNotNone(row["silent_seconds"])
        self.assertLessEqual(row["silent_seconds"], 5)
        self.assertEqual(report["online"], 1)
        self.assertNotIn(token, str(report), "токен наружу не отдаём")

    def test_request_refreshes_last_seen(self):
        token = self.cashier.login("1234")["token"]
        # Имитируем старую отметку: касса отвечала пять минут назад.
        self.db.execute("UPDATE cashier_tokens SET last_seen=? WHERE 1", (stamp(5),))
        self.cashier._touched.clear()
        self.cashier.require(token)
        row = self.cashier.sessions()["sessions"][0]
        self.assertTrue(row["online"], "запрос кассы обновляет отметку связи")
        self.assertLess(row["silent_seconds"], 60)

    def test_silent_cashier_is_not_online(self):
        self.cashier.login("1234")
        self.db.execute("UPDATE cashier_tokens SET last_seen=? WHERE 1", (stamp(7),))
        row = self.cashier.sessions()["sessions"][0]
        self.assertFalse(row["online"])
        self.assertGreater(row["silent_seconds"], 60)

    def test_never_seen_session_is_not_online_and_has_no_number(self):
        # Старая запись из базы до 17.0.13: last_seen пуст — «неизвестно»,
        # а не «молчит 50 лет».
        self.cashier.login("1234")
        self.db.execute("UPDATE cashier_tokens SET last_seen='' WHERE 1")
        row = self.cashier.sessions()["sessions"][0]
        self.assertIsNone(row["silent_seconds"])
        self.assertFalse(row["online"])


class _FakeDB:
    def __init__(self, minutes: float):
        self.minutes = minutes

    def setting(self, key, default=None):
        return self.minutes if key == "cashier_offline_alert_min" else default


class _FakeManager:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def notify_async(self, text, photo=None, buttons=None, critical=False, event=""):
        self.sent.append((text, event))


class _FakeCashier:
    def __init__(self, rows):
        self.rows = rows

    def sessions(self):
        return {"sessions": self.rows, "online": sum(1 for r in self.rows if r["online"])}


def row(name="Аня", online=False, silent=600.0, expires_in=3600.0, key="a"):
    return {"id": key, "name": name, "online": online, "silent_seconds": silent,
            "expires_at": (datetime.now(timezone.utc)
                           + timedelta(seconds=expires_in)).isoformat()}


class WatcherTests(unittest.TestCase):
    def test_one_message_per_outage(self):
        manager = _FakeManager()
        cashier = _FakeCashier([row()])
        watcher = CashierWatcher(cashier, manager, _FakeDB(5))
        self.assertEqual(watcher.check(), ["Аня"])
        self.assertEqual(len(manager.sent), 1)
        self.assertEqual(manager.sent[0][1], "cashier_offline")
        self.assertEqual(watcher.check(), [], "второй проход не повторяет сообщение")
        self.assertEqual(len(manager.sent), 1)

    def test_new_outage_after_recovery_is_reported_again(self):
        manager = _FakeManager()
        cashier = _FakeCashier([row()])
        watcher = CashierWatcher(cashier, manager, _FakeDB(5))
        watcher.check()
        cashier.rows = [row(online=True, silent=1.0)]
        watcher.check()                       # вернулась на связь — эпизод закрыт
        cashier.rows = [row()]
        watcher.check()
        self.assertEqual(len(manager.sent), 2)

    def test_short_silence_and_finished_shift_are_not_reported(self):
        manager = _FakeManager()
        watcher = CashierWatcher(_FakeCashier([row(silent=120.0)]), manager, _FakeDB(5))
        self.assertEqual(watcher.check(), [], "две минуты молчания — ещё не обрыв")
        expired = CashierWatcher(_FakeCashier([row(expires_in=-60.0)]), manager, _FakeDB(5))
        self.assertEqual(expired.check(), [], "конец смены — не обрыв")
        self.assertEqual(manager.sent, [])

    def test_zero_threshold_switches_the_watcher_off(self):
        manager = _FakeManager()
        watcher = CashierWatcher(_FakeCashier([row()]), manager, _FakeDB(0))
        self.assertEqual(watcher.threshold_seconds(), 0.0)
        self.assertEqual(watcher.check(), [])
        self.assertEqual(manager.sent, [])


class _Bus:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def publish(self, kind, payload=None):
        self.events.append((kind, payload or {}))
        return 1


class CatalogEventTests(unittest.TestCase):
    """`catalog_changed` — аддитивное событие: денежный формат не трогаем."""

    def test_helper_publishes_catalog_changed(self):
        from connector.printflow.api import Api

        api = Api.__new__(Api)     # без тяжёлого __init__: проверяем сам publish
        api.bus = _Bus()
        Api.catalog_changed(api, "unit")
        self.assertEqual(len(api.bus.events), 1)
        kind, payload = api.bus.events[0]
        self.assertEqual(kind, "catalog_changed")
        self.assertEqual(payload["reason"], "unit")

    def test_catalog_mutations_publish_the_event(self):
        source = (ROOT / "connector" / "printflow" / "api.py").read_text(encoding="utf-8")
        for marker in ('self.catalog_changed("catalog_save")',
                       'self.catalog_changed("catalog_delete")',
                       'self.catalog_changed("price_recalc")',
                       'self.catalog_changed("shelf_save")',
                       'self.catalog_changed("shelf_delete")',
                       'self.catalog_changed("nomenclature_save")'):
            self.assertIn(marker, source, f"нет публикации события: {marker}")

    def test_route_is_registered_and_reads_without_token(self):
        import connector.printflow.routes_cashier  # noqa: F401 — регистрация
        from connector.printflow.router import router
        from connector.printflow.routes_cashier import cashier_sessions

        self.assertIn("/api/cashier/sessions", router.paths())

        db = make_db()

        class _Fake:
            cashier = Cashier(db, Accounting(db))

        payload = cashier_sessions(_Fake(), None)
        self.assertIn("sessions", payload)
        self.assertEqual(payload["total"], 0, "пока никто не входил — список пуст")


if __name__ == "__main__":
    unittest.main()

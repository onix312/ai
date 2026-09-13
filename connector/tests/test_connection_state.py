"""Состояние каналов связи с принтером (17.0.19).

Главное, что здесь закреплено: каналы не склеиваются в один «online». Мёртвый
FTPS при живом MQTT — это «частично», с названием канала и безопасным действием,
а не зелёная лампочка, по которой оператор жмёт «Скачать файл» в пустоту.
"""
from __future__ import annotations

import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from connector.printflow.connection_state import CHANNELS, ConnectionState
from connector.printflow.db import Database

BASE = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def stamp(seconds: int = 0) -> str:
    return (BASE + timedelta(seconds=seconds)).isoformat()


class Clock:
    """Часы, которые можно перевести вперёд: «давно не было вестей» надо уметь
    получить в тесте, а не ждать минуту."""

    def __init__(self, moment: datetime = BASE) -> None:
        self.moment = moment

    def __call__(self) -> str:
        return self.moment.isoformat()

    def advance(self, seconds: int) -> None:
        self.moment += timedelta(seconds=seconds)


class ConnectionStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(pathlib.Path(self.tmp.name) / "links.sqlite3")
        self.addCleanup(self.db.close)
        self.clock = Clock()
        self.links = ConnectionState(self.db, now=self.clock)

    def channel(self, printer: str, name: str) -> dict:
        return self.links.describe(printer, name)

    def test_unknown_channel_was_never_checked(self):
        info = self.channel("p1", "mqtt")
        self.assertEqual("never", info["state"])
        self.assertEqual("ещё не проверяли", info["label"])
        self.assertEqual("", info["action"])

    def test_success_marks_channel_alive(self):
        self.links.mark_ok("p1", "mqtt")
        info = self.channel("p1", "mqtt")
        self.assertEqual("ok", info["state"])
        self.assertEqual(0, info["attempts"])
        self.assertEqual(0, info["silent_for"])

    def test_failure_keeps_reason_and_next_retry(self):
        self.links.mark_fail("p1", "ftps", "timeout", retry_in=45)
        info = self.channel("p1", "ftps")
        self.assertEqual("down", info["state"])
        self.assertEqual("timeout", info["last_error"])
        self.assertEqual(1, info["attempts"])
        self.assertTrue(info["next_retry_at"].startswith("2026-09-13T12:00:45"))
        self.assertIn("Access Code", info["action"])

    def test_attempts_grow_until_success_resets_them(self):
        self.links.mark_fail("p1", "mqtt", "нет ответа")
        self.links.mark_fail("p1", "mqtt", "нет ответа")
        self.assertEqual(2, self.channel("p1", "mqtt")["attempts"])
        self.links.mark_ok("p1", "mqtt")
        info = self.channel("p1", "mqtt")
        self.assertEqual(0, info["attempts"])
        self.assertEqual("", info["last_error"])

    def test_silence_longer_than_threshold_becomes_stale(self):
        self.links.mark_ok("p1", "mqtt")
        self.clock.advance(CHANNELS["mqtt"][1] + 1)
        info = self.channel("p1", "mqtt")
        self.assertEqual("stale", info["state"])
        self.assertEqual("давно не было вестей", info["label"])
        self.assertTrue(info["action"], "у молчащего канала нет совета оператору")

    def test_threshold_is_a_setting_not_a_constant(self):
        self.links.mark_ok("p1", "mqtt")
        self.clock.advance(120)
        self.assertEqual("stale", self.channel("p1", "mqtt")["state"])
        self.db.set_settings({"link_stale_mqtt": 600})
        self.assertEqual("ok", self.channel("p1", "mqtt")["state"],
                         "настройка link_stale_mqtt не применяется")

    def test_empty_reason_is_not_lost(self):
        self.links.mark_fail("p1", "camera", "")
        self.assertEqual("причина не сообщена", self.channel("p1", "camera")["last_error"])


class VerdictTests(unittest.TestCase):
    """Сводка называет канал, а не говорит «всё хорошо»."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(pathlib.Path(self.tmp.name) / "links.sqlite3")
        self.addCleanup(self.db.close)
        self.links = ConnectionState(self.db, now=Clock())

    def test_nothing_checked_yet(self):
        snap = self.links.snapshot("p1")
        self.assertEqual("не проверяли", snap["verdict"])
        self.assertEqual("", snap["action"])

    def test_all_alive(self):
        for channel in CHANNELS:
            self.links.mark_ok("p1", channel)
        snap = self.links.snapshot("p1")
        self.assertEqual("все каналы живы", snap["verdict"])
        self.assertEqual("", snap["action"])

    def test_partial_failure_names_the_channel(self):
        self.links.mark_ok("p1", "mqtt")
        self.links.mark_fail("p1", "ftps", "connection refused")
        snap = self.links.snapshot("p1")
        self.assertEqual("частично", snap["verdict"])
        self.assertIn("MQTT", snap["summary"])
        self.assertIn("FTPS", snap["summary"])
        self.assertIn("не отвечает", snap["summary"])
        self.assertIn("Access Code", snap["action"])

    def test_everything_down(self):
        self.links.mark_fail("p1", "mqtt", "нет ответа")
        self.links.mark_fail("p1", "ftps", "нет ответа")
        snap = self.links.snapshot("p1")
        self.assertEqual("нет связи", snap["verdict"])
        self.assertIn("Молчит всё", snap["summary"])

    def test_only_enabled_printers_are_listed(self):
        self.db.upsert("printers", {"id": "p1", "name": "Живой", "enabled": 1, "position": 0})
        self.db.upsert("printers", {"id": "p2", "name": "Выключен", "enabled": 0, "position": 1})
        self.assertEqual(["p1"], [p["printer_id"] for p in self.links.all_printers()])


class WiringTests(unittest.TestCase):
    """Отметки ставят менеджер (MQTT) и API (FTPS), а наблюдение не роняет запрос."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(pathlib.Path(self.tmp.name) / "wire.sqlite3")
        self.addCleanup(self.db.close)

    def test_manager_marks_mqtt_by_event_kind(self):
        from connector.printflow.manager import PrinterManager
        manager = PrinterManager.__new__(PrinterManager)
        manager.db = self.db
        manager._mark_link("p1", "offline", "Принтер отключился")
        row = ConnectionState(self.db).describe("p1", "mqtt")
        self.assertEqual("down", row["state"])
        self.assertEqual("Принтер отключился", row["last_error"])
        manager._mark_link("p1", "error", "Ошибка печати: засор")
        row = ConnectionState(self.db).describe("p1", "mqtt")
        self.assertEqual("ok", row["state"],
                         "кадр об ошибке дошёл — значит канал жив")

    def test_api_marks_channel_and_survives_broken_db(self):
        from connector.printflow.api import Api
        api = Api.__new__(Api)
        api.db = self.db
        api.mark_link("p1", "ftps", False, "timeout")
        self.assertEqual("down", ConnectionState(self.db).describe("p1", "ftps")["state"])
        api.mark_link("p1", "ftps", True)
        self.assertEqual("ok", ConnectionState(self.db).describe("p1", "ftps")["state"])
        broken = Api.__new__(Api)
        broken.db = None
        broken.mark_link("p1", "ftps", True)  # не должно бросить

    def test_unknown_channel_is_ignored(self):
        links = ConnectionState(self.db)
        links.mark_ok("p1", "телепатия")
        self.assertEqual([], self.db.query("SELECT * FROM printer_links"))


class LinksRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(pathlib.Path(self.tmp.name) / "route.sqlite3")
        self.addCleanup(self.db.close)
        self.db.upsert("printers", {"id": "p1", "name": "P1S", "enabled": 1, "position": 0})
        from connector.printflow.api import Api
        from connector.printflow import router as router_module
        router_module.register_all()
        self.api = Api.__new__(Api)
        self.api.db = self.db
        self.api.router = router_module.router

    def test_route_lists_channels_for_every_printer(self):
        code, payload = self.api.get("/api/printer/links", {})
        self.assertEqual(200, code)
        self.assertEqual(list(CHANNELS), [c["channel"] for c in payload["channels"]])
        self.assertEqual(["p1"], [p["printer_id"] for p in payload["printers"]])
        self.assertEqual("не проверяли", payload["printers"][0]["verdict"])

    def test_route_can_ask_for_one_printer(self):
        self.db.upsert("printers", {"id": "p2", "name": "X1C", "enabled": 1, "position": 1})
        code, payload = self.api.get("/api/printer/links", {"printer_id": ["p2"]})
        self.assertEqual(200, code)
        self.assertEqual(["p2"], [p["printer_id"] for p in payload["printers"]])

    def test_unknown_printer_is_a_client_error(self):
        with self.assertRaises(ValueError):
            self.api.get("/api/printer/links", {"printer_id": ["нет"]})


class DiagnosticsTests(unittest.TestCase):
    """Самодиагностика называет молчащий канал, а не одно число «онлайн»."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(pathlib.Path(self.tmp.name) / "diag.sqlite3")
        self.addCleanup(self.db.close)
        self.db.upsert("printers", {"id": "p1", "name": "P1S", "enabled": 1, "position": 0})
        from connector.printflow.api import Api
        self.api = Api.__new__(Api)
        self.api.db = self.db
        self.api.started_at = None

    def report(self) -> dict:
        from connector.printflow import diagnostics as service
        return service.collect(self.api)

    def test_report_carries_channel_state(self):
        self.links = ConnectionState(self.db)
        self.links.mark_ok("p1", "mqtt")
        self.links.mark_fail("p1", "ftps", "connection refused")
        payload = self.report()
        self.assertIn("links", payload)
        item = payload["links"][0]
        self.assertEqual("частично", item["verdict"])
        self.assertEqual(["ftps"], [b["channel"] for b in item["bad"]])
        self.assertEqual("connection refused", item["bad"][0]["last_error"])

    def test_human_report_names_the_dead_channel(self):
        ConnectionState(self.db).mark_fail("p1", "ftps", "connection refused")
        from connector.printflow import diagnostics as service
        text = service.human_report(self.report())
        self.assertIn("Связь:", text)
        self.assertIn("FTPS", text)
        self.assertIn("Access Code", text)


class SettingsTests(unittest.TestCase):
    """Пороги — настоящие настройки, а не обещание в комментарии.

    `Database.set_settings` молча пропускает ключи, которых нет в
    `DEFAULT_SETTINGS`, поэтому без записи в `config.py` настройка не сохранится.
    """

    def test_every_threshold_is_a_real_setting(self):
        from connector.printflow.config import DEFAULT_SETTINGS
        for channel, (_title, seconds, _breaks) in CHANNELS.items():
            key = f"link_stale_{channel}"
            with self.subTest(channel=channel):
                self.assertIn(key, DEFAULT_SETTINGS,
                              "настройка не сохранится: ключа нет в DEFAULT_SETTINGS")
                self.assertEqual(seconds, DEFAULT_SETTINGS[key],
                                 "значение по умолчанию разошлось с CHANNELS")


class SchemaTests(unittest.TestCase):
    def test_printer_links_table_exists_with_expected_columns(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Database(pathlib.Path(tmp.name) / "schema.sqlite3")
        self.addCleanup(db.close)
        self.assertEqual(
            {"link_id", "printer_id", "channel", "state", "last_ok_at",
             "last_fail_at", "last_error", "attempts", "next_retry_at",
             "updated_at"},
            db.columns("printer_links"))


if __name__ == "__main__":
    unittest.main()

"""Регрессии процессов: лимиты запросов, бэкапы и сторож печати."""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.api import (  # noqa: E402
    MAX_JSON,
    request_length,
    request_origin_allowed,
)
from connector.printflow.bambu import BambuPrinter  # noqa: E402
from connector.printflow.config import backup_keep, rotate_backups  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.ftps import PrinterFiles  # noqa: E402
from connector.printflow.manager import PrinterManager  # noqa: E402
from connector.printflow.watchdog import Watchdog  # noqa: E402


class RequestLimitTests(unittest.TestCase):
    def test_json_limit_is_checked_before_body_read(self):
        self.assertEqual(request_length(str(MAX_JSON), MAX_JSON), (MAX_JSON, False))
        self.assertEqual(request_length(str(MAX_JSON + 1), MAX_JSON), (MAX_JSON + 1, True))

    def test_invalid_content_length_is_rejected(self):
        for value in ("abc", "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                request_length(value, MAX_JSON)

    def test_browser_origin_must_match_request_host(self):
        self.assertTrue(request_origin_allowed(None, "192.168.1.20:8080"))
        self.assertTrue(request_origin_allowed(
            "http://192.168.1.20:8080", "192.168.1.20:8080"))
        self.assertFalse(request_origin_allowed(
            "http://192.168.1.99:8080", "192.168.1.20:8080"))
        self.assertFalse(request_origin_allowed(
            "https://attacker.example", "192.168.1.20:8080"))


class BackupRotationTests(unittest.TestCase):
    def test_retention_is_clamped(self):
        self.assertEqual(backup_keep(0), 1)
        self.assertEqual(backup_keep(500), 200)
        self.assertEqual(backup_keep("bad"), 20)

    def test_all_backup_kinds_share_one_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            names = [
                "pre-migration-1.sqlite3",
                "printflow-auto-2.sqlite3",
                "printflow-3.sqlite3",
                "before-restore-4.sqlite3",
            ]
            for index, name in enumerate(names, 1):
                path = directory / name
                path.write_bytes(b"sqlite")
                os.utime(path, (index, index))

            removed = rotate_backups(directory, keep=2)

            self.assertEqual({p.name for p in removed}, set(names[:2]))
            self.assertEqual(
                {p.name for p in directory.glob("*.sqlite3")}, set(names[2:]))


class AutoBackupTests(unittest.TestCase):
    def test_existing_copy_sets_due_time_and_failures_retry_hourly(self):
        class FakeDB:
            """12.0: автобэкап идёт через отдельное ro-соединение
            (backup_database_file), а не через блокирующий db.backup_to."""
            def __init__(self):
                self.calls = 0
                self.fail = False
                self.path = "db.sqlite3"

            def setting(self, key, default=None):
                return {"auto_backup_days": 1, "backup_keep": 20}.get(key, default)

            def add_event(self, *args, **kwargs):
                pass

        now = 2_000_000_000.0
        manager = PrinterManager.__new__(PrinterManager)
        manager.db = FakeDB()
        manager._last_backup = 0.0
        manager._last_backup_attempt = 0.0

        def fake_backup(source, target):
            manager.db.calls += 1
            if manager.db.fail:
                raise OSError("диск недоступен")
            pathlib.Path(target).write_bytes(b"sqlite")

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("connector.printflow.manager.BACKUP_DIR", pathlib.Path(tmp)), \
                mock.patch("connector.printflow.manager.backup_database_file", fake_backup), \
                mock.patch("connector.printflow.manager.time.time", return_value=now):
            existing = pathlib.Path(tmp) / "printflow-auto-existing.sqlite3"
            existing.write_bytes(b"sqlite")
            os.utime(existing, (now - 60, now - 60))
            manager.auto_backup_if_due()
            self.assertEqual(manager.db.calls, 0)

            os.utime(existing, (now - 2 * 86400, now - 2 * 86400))
            manager.db.fail = True
            manager.auto_backup_if_due()
            self.assertEqual(manager.db.calls, 1)
            self.assertEqual(manager._last_backup, 0.0)

            with mock.patch("connector.printflow.manager.time.time",
                            return_value=now + 3599):
                manager.auto_backup_if_due()
            self.assertEqual(manager.db.calls, 1)
            with mock.patch("connector.printflow.manager.time.time",
                            return_value=now + 3601):
                manager.auto_backup_if_due()
            self.assertEqual(manager.db.calls, 2)


class RuntimeConnectionSettingsTests(unittest.TestCase):
    def test_ftps_settings_reach_printer_client(self):
        printer = BambuPrinter({
            "id": "p1", "host": "192.168.1.10", "access_code": "code",
            "mode": "lan", "ftps_timeout": 19, "ftps_retries": 5,
            "ftps_block_kb": 512,
        })
        files = printer.files
        self.assertEqual(files.timeout, 19)
        self.assertEqual(files.retries, 5)
        self.assertEqual(files.block_size, 512 * 1024)

    def test_ftps_retry_resets_progress_counter(self):
        class FakeFTP:
            def __init__(self, fail: bool):
                self.fail = fail

            def storbinary(self, command, stream, blocksize, callback):
                callback(b"abc")
                if self.fail:
                    raise OSError("временный обрыв")
                callback(b"def")

            def quit(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "model.3mf"
            source.write_bytes(b"abcdef")
            files = PrinterFiles("printer", "code", retries=2)
            progress: list[int] = []
            with mock.patch.object(
                    files, "_connect", side_effect=[FakeFTP(True), FakeFTP(False)]), \
                    mock.patch("time.sleep"):
                result = files.upload(source, "model.3mf", progress.append)

        self.assertEqual(result["size"], 6)
        self.assertEqual(progress, [3, 3, 6])


class TaxReserveTests(unittest.TestCase):
    def test_disabled_reserve_is_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(pathlib.Path(tmp) / "tax.sqlite3")
            try:
                db.set_settings({
                    "tax_mode": "manual", "tax_rate": 10,
                    "tax_reserve_enabled": False, "tax_reserve_extra": 5,
                })
                accounting = Accounting(db)
                accounting.add_transaction("income", "sale", 1000, "Продажа")
                report = accounting.tax_report()
            finally:
                db.close()
        self.assertEqual(report["tax_due"], 100)
        self.assertEqual(report["reserve"], 0)
        self.assertEqual(report["reserve_rate"], 0)


class _FakeDb:
    def __init__(self, settings: dict | None = None):
        self.values = settings or {}
        self.events: list[tuple] = []

    def setting(self, key, default=None):
        return self.values.get(key, default)

    def one(self, *_args, **_kwargs):
        return None

    def add_event(self, *args, **kwargs):
        self.events.append((args, kwargs))


class _FakeManager:
    def __init__(self, db):
        self.db = db
        self.notifications: list[str] = []

    def notify_async(self, text, *_args, **_kwargs):
        self.notifications.append(text)


class _FakeCamera:
    frame = None

    def snapshot(self, **_kwargs):
        return {}


class _FakePrinter:
    id = "p1"
    record = {"guard_enabled": 1, "name": "P1S"}
    camera = _FakeCamera()


class WatchdogDedupTests(unittest.TestCase):
    @staticmethod
    def snap(*, nozzle=210, target=210, low_filament=True):
        trays = ([{"id": "tray1", "active": True, "remain": 5,
                   "label": "A1"}] if low_filament else [])
        return {
            "printer": {"state": "RUNNING", "progress": 20, "layer": 3,
                        "problems": []},
            "temperature": {"nozzle": nozzle, "nozzle_target": target},
            "ams": {"trays": trays},
        }

    def test_non_hms_alert_is_not_repeated_each_tick(self):
        db = _FakeDb({"guard_stall_minutes": 999, "guard_overrun_pct": 0})
        watch = Watchdog(_FakeManager(db))
        printer = _FakePrinter()

        first = watch.check(printer, self.snap())
        second = watch.check(printer, self.snap())

        self.assertEqual([a["code"] for a in first], ["filament_low"])
        self.assertEqual(second, [])

    def test_cold_nozzle_respects_configured_delay_and_deduplicates(self):
        db = _FakeDb({"guard_stall_minutes": 999, "guard_cold_minutes": 10})
        watch = Watchdog(_FakeManager(db))
        printer = _FakePrinter()
        snap = self.snap(nozzle=30, target=210, low_filament=False)

        with mock.patch("connector.printflow.watchdog.time.time", return_value=0):
            self.assertEqual(watch._check_stall(printer, snap), [])  # seed progress
        with mock.patch("connector.printflow.watchdog.time.time", return_value=1):
            self.assertEqual(watch._check_stall(printer, snap), [])  # start cold timer
        with mock.patch("connector.printflow.watchdog.time.time", return_value=9 * 60):
            self.assertEqual(watch._check_stall(printer, snap), [])
        with mock.patch("connector.printflow.watchdog.time.time", return_value=10 * 60 + 1):
            alert = watch._check_stall(printer, snap)
        with mock.patch("connector.printflow.watchdog.time.time", return_value=12 * 60):
            repeated = watch._check_stall(printer, snap)

        self.assertEqual([a["code"] for a in alert], ["cold"])
        self.assertEqual(repeated, [])


if __name__ == "__main__":
    unittest.main()

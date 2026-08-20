"""Откат базы (роадмап 10.12): копия перед миграцией, маркер восстановления.

Проверяется сценарий «сломалось — откатились» целиком, без живого сервера:
копия → изменения в базе → запрос отката → применение на старте → база
вернулась к состоянию копии, а текущее состояние сохранено рядом.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import db as db_module  # noqa: E402
from connector.printflow.db import Database  # noqa: E402


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(self._tmp.name)
        self.data_dir = tmp
        self.backup_dir = tmp / "backups"
        self.backup_dir.mkdir()
        self.db_file = tmp / "printflow.sqlite3"
        # Функции db.py читают пути как атрибуты модуля — патчим их напрямую.
        patcher_backup = mock.patch.object(db_module, "BACKUP_DIR", self.backup_dir)
        patcher_db_file = mock.patch.object(db_module, "DB_FILE", self.db_file)
        patcher_marker = mock.patch.object(db_module, "RESTORE_REQUEST",
                                           tmp / "restore.request")
        patcher_backup.start()
        patcher_db_file.start()
        patcher_marker.start()
        self.addCleanup(patcher_backup.stop)
        self.addCleanup(patcher_db_file.stop)
        self.addCleanup(patcher_marker.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _base(self) -> Database:
        database = Database(self.db_file)
        database.add_event("test", "первая запись", "до отката", "", {})
        database.close()
        return database

    def test_make_backup_creates_file(self):
        self._base()
        result = db_module.make_backup("test")
        self.assertTrue(result["ok"])
        self.assertTrue((self.backup_dir / result["file"]).is_file())

    def test_list_backups_newest_first(self):
        self._base()
        first = db_module.make_backup("a")["file"]
        second = db_module.make_backup("b")["file"]
        names = [item["name"] for item in db_module.list_backups()]
        self.assertIn(second, names)
        self.assertIn(first, names)
        self.assertLess(names.index(second), names.index(first))

    def test_request_restore_rejects_unknown_file(self):
        with self.assertRaises(ValueError):
            db_module.request_restore("нет-такой-копии.sqlite3")
        with self.assertRaises(ValueError):
            db_module.request_restore("../../etc/passwd")
        with self.assertRaises(ValueError):
            db_module.request_restore("картинка.jpg")

    def test_full_restore_cycle(self):
        self._base()
        backup = db_module.make_backup("cycle")["file"]

        # После копии база изменилась: появилась запись, которой не было в копии.
        database = Database(self.db_file)
        database.add_event("test", "вторая запись", "после копии", "", {})
        database.close()

        request = db_module.request_restore(backup)
        self.assertTrue(request["ok"])
        self.assertTrue(request["safety"])  # текущая база сохранена рядом
        self.assertTrue((self.backup_dir / request["safety"]).is_file())
        self.assertTrue(db_module.pending_restore())

        applied = db_module.apply_pending_restore()
        self.assertEqual(applied["restored"], backup)
        self.assertFalse(db_module.RESTORE_REQUEST.exists())

        database = Database(self.db_file)
        texts = [row["detail"] for row in database.events(kind="test")]
        database.close()
        self.assertIn("до отката", texts)
        self.assertNotIn("после копии", texts)

        # Страховочная копия содержит «после копии» — ничего не потерялось.
        safety_db = Database(self.backup_dir / request["safety"])
        safety_texts = [row["detail"] for row in safety_db.events(kind="test")]
        safety_db.close()
        self.assertIn("после копии", safety_texts)

    def test_marker_with_missing_file_is_reported(self):
        db_module.RESTORE_REQUEST.write_text(
            json.dumps({"file": "исчезла.sqlite3"}), encoding="utf-8")
        result = db_module.apply_pending_restore()
        self.assertEqual(result["restored"], "")
        self.assertIn("не найдена", result["error"])
        self.assertFalse(db_module.RESTORE_REQUEST.exists())

    def test_pre_migration_snapshot_created(self):
        # Свежая база, затем вручную занижаем user_version — как при старом файле.
        self._base()
        import sqlite3
        conn = sqlite3.connect(str(self.db_file))
        conn.execute("PRAGMA user_version=0")
        conn.commit()
        conn.close()

        database = Database(self.db_file)  # _migrate увидит version < SCHEMA_VERSION
        database.close()
        snapshots = list(self.backup_dir.glob("pre-migration-*.sqlite3"))
        self.assertTrue(snapshots, "перед миграцией должна появиться копия базы")


if __name__ == "__main__":
    unittest.main()

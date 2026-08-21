"""Проверки автообновления: применение, защиты, режим архива."""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "connector"))


def git(*args: str, cwd: pathlib.Path) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return done.stdout.strip()


class FakePrinter:
    def __init__(self, state: str, name: str) -> None:
        self.state, self.name = state, name

    def snapshot(self) -> dict:
        return {"printer": {"state": self.state, "name": self.name}}


class FakeManager:
    def __init__(self, printers: dict | None = None) -> None:
        self.printers = printers or {}
        self.sent: list[str] = []

    def notify_async(self, text: str, photo=None) -> None:
        self.sent.append(text)


class UpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        import printflow.config as config
        for name, value in (("DATA_DIR", self.tmp), ("DB_FILE", self.tmp / "pf.db"),
                            ("UPLOAD_DIR", self.tmp / "up"), ("PHOTO_DIR", self.tmp / "ph")):
            self._patch(config, name, value)

        import printflow.updater as updater
        self.updater_mod = updater
        self._patch(updater, "BACKUP_DIR", self.tmp / "backups")

        from printflow.db import Database
        self.db = Database(self.tmp / "pf.db")
        self.addCleanup(self.db.close)

    def _patch(self, module, name: str, value) -> None:
        old = getattr(module, name, None)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, old)

    def _make_repo(self) -> pathlib.Path:
        """Апстрим с двумя коммитами и клон, отставший на один."""
        upstream = self.tmp / "upstream"
        upstream.mkdir()
        git("init", "-q", "--initial-branch=main", ".", cwd=upstream)
        git("config", "user.email", "t@t", cwd=upstream)
        git("config", "user.name", "T", cwd=upstream)
        (upstream / "site").mkdir()
        (upstream / "site" / "app.js").write_text("версия 1", encoding="utf-8")
        git("add", "-A", cwd=upstream)
        git("commit", "-q", "-m", "первый", cwd=upstream)

        work = self.tmp / "work"
        git("clone", "-q", str(upstream), str(work), cwd=self.tmp)
        git("config", "user.email", "t@t", cwd=work)
        git("config", "user.name", "T", cwd=work)

        (upstream / "site" / "app.js").write_text("версия 2", encoding="utf-8")
        (upstream / "site" / "new.js").write_text("добавлено", encoding="utf-8")
        git("add", "-A", cwd=upstream)
        git("commit", "-q", "-m", "второй", cwd=upstream)
        git("fetch", "-q", "origin", cwd=work)

        self._patch(self.updater_mod, "ROOT", work)
        return work

    def checker(self, manager=None):
        return self.updater_mod.UpdateChecker("3.0.0", self.db, manager)

    # ---------------------------------------------------------------- режимы
    def test_detects_git_and_archive_modes(self) -> None:
        work = self._make_repo()
        self.assertEqual(self.checker().mode, "git")

        plain = self.tmp / "plain"
        plain.mkdir()
        self._patch(self.updater_mod, "ROOT", plain)
        checker = self.checker()
        self.assertEqual(checker.mode, "archive")
        # без git ветка берётся из настроек
        self.assertEqual(checker.branch(), "main")
        self.assertTrue(work.exists())

    # -------------------------------------------------------------- установка
    def test_apply_fast_forwards_and_records_history(self) -> None:
        work = self._make_repo()
        checker = self.checker(FakeManager())
        before = checker.local_sha()

        result = checker.apply()

        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertTrue(result["restart_required"])
        self.assertEqual(result["files"], 2)
        self.assertEqual((work / "site" / "app.js").read_text(encoding="utf-8"), "версия 2")
        self.assertTrue((work / "site" / "new.js").exists())
        self.assertNotEqual(checker.local_sha(), before)
        self.assertTrue(self.db.setting("last_update_at", ""))
        self.assertEqual(len(checker.history()), 1)
        self.assertTrue(pathlib.Path(result["backup"]).exists())

    def test_apply_refuses_when_working_copy_is_dirty(self) -> None:
        work = self._make_repo()
        (work / "site" / "app.js").write_text("мои правки", encoding="utf-8")
        checker = self.checker()

        self.assertIn("несохранённые изменения", checker.busy_reason())
        with self.assertRaises(ValueError):
            checker.apply()
        self.assertEqual((work / "site" / "app.js").read_text(encoding="utf-8"), "мои правки")

    def test_apply_refuses_while_printing(self) -> None:
        self._make_repo()
        manager = FakeManager({"p1": FakePrinter("RUNNING", "P1S у окна")})
        checker = self.checker(manager)

        self.assertIn("P1S у окна", checker.busy_reason())
        with self.assertRaises(ValueError):
            checker.apply()

    # ----------------------------------------------------------- режим архива
    def test_copy_tree_skips_service_dirs_and_identical_files(self) -> None:
        plain = self.tmp / "plain"
        (plain / "site").mkdir(parents=True)
        (plain / "site" / "app.js").write_text("старое", encoding="utf-8")
        self._patch(self.updater_mod, "ROOT", plain)
        checker = self.checker()

        src = self.tmp / "src"
        (src / "site").mkdir(parents=True)
        (src / ".git").mkdir()
        (src / "node_modules").mkdir()
        (src / "site" / "app.js").write_text("новое", encoding="utf-8")
        (src / ".git" / "config").write_text("секрет", encoding="utf-8")
        (src / "node_modules" / "x.js").write_text("мусор", encoding="utf-8")

        copied = checker._copy_tree(src, plain)

        self.assertEqual(copied, 1)
        self.assertEqual((plain / "site" / "app.js").read_text(encoding="utf-8"), "новое")
        self.assertFalse((plain / ".git").exists())
        self.assertFalse((plain / "node_modules").exists())
        # повторный проход не трогает одинаковые файлы
        self.assertEqual(checker._copy_tree(src, plain), 0)

    # ----------------------------------------------------------------- автомат
    def test_auto_tick_installs_when_enabled(self) -> None:
        work = self._make_repo()
        self.db.set_settings({"auto_update_enabled": True})
        manager = FakeManager()
        checker = self.checker(manager)
        head = git("rev-parse", "origin/main", cwd=work)
        checker._remote_head = lambda branch: {
            "sha": head, "short": head[:7], "title": "второй", "message": "",
            "author": "", "date": "2026-08-17", "url": "", "branch": branch}
        checker._pending = lambda branch, limit=20: []
        restarts: list[float] = []
        checker.restart = lambda delay=2.0: restarts.append(delay)

        checker._auto_tick()

        self.assertEqual((work / "site" / "app.js").read_text(encoding="utf-8"), "версия 2")
        self.assertEqual(len(restarts), 1)
        self.assertTrue(any("обновлён" in text for text in manager.sent))

    def test_auto_tick_only_notifies_once_when_disabled(self) -> None:
        work = self._make_repo()
        manager = FakeManager()
        checker = self.checker(manager)
        head = git("rev-parse", "origin/main", cwd=work)
        checker._remote_head = lambda branch: {
            "sha": head, "short": head[:7], "title": "второй", "message": "",
            "author": "", "date": "2026-08-17", "url": "", "branch": branch}
        checker._pending = lambda branch, limit=20: []

        checker._auto_tick()
        checker._auto_tick()

        self.assertEqual((work / "site" / "app.js").read_text(encoding="utf-8"), "версия 1")
        self.assertEqual(len(manager.sent), 1)
        self.assertEqual(self.db.setting("update_seen_sha", ""), head)

    # ------------------------------------------------------------------ отчёт
    def test_frozen_mode_refuses_to_apply(self) -> None:
        """Собранный exe не обновляет сам себя: режим frozen, apply отклонён."""
        self._make_repo()
        checker = self.checker()
        with mock.patch("sys.frozen", True, create=True):
            self.assertEqual(checker.mode, "frozen")
            with self.assertRaises(ValueError):
                checker.apply()
            with mock.patch.object(checker, "check",
                                   return_value={"sha": "abc1234", "branch": "main"}):
                report = checker.report()
            self.assertEqual(report["mode"], "frozen")
            self.assertTrue(report["update"])
            self.assertFalse(report["can_apply"])
            self.assertIn("exe", report["busy_reason"])

    def test_report_hides_details_when_checks_disabled(self) -> None:
        self._make_repo()
        self.db.set_settings({"update_check_enabled": False})

        report = self.checker().report()

        self.assertTrue(report["disabled"])
        self.assertFalse(report["update"])
        self.assertIsNone(report["latest"])
        self.assertFalse(report["can_apply"])

    def test_backup_failure_stops_update_before_files_change(self) -> None:
        work = self._make_repo()
        checker = self.checker()
        with mock.patch.object(
                checker, "_backup_db", side_effect=ValueError("диск заполнен")):
            with self.assertRaisesRegex(ValueError, "диск заполнен"):
                checker.apply()
        self.assertEqual((work / "site" / "app.js").read_text(encoding="utf-8"),
                         "версия 1")

    def test_backup_creates_readable_database_copy(self) -> None:
        self._make_repo()
        checker = self.checker()

        backup = checker._backup_db()

        self.assertIsNotNone(backup)
        self.assertTrue(backup.exists())
        import sqlite3
        conn = sqlite3.connect(str(backup))
        try:
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertIn("settings", names)


if __name__ == "__main__":
    unittest.main()

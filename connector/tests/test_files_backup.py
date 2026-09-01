"""Файловая копия цеха (идея 21).

Бэкап базы без файлов даёт «учётку без заказов»: модели, фотографии,
библиотека и сертификат шлюза в копию не попадали. Здесь проверяем состав
архива, проверку целостности и то, что ключ шифрования в него не уходит.
"""
from __future__ import annotations

import pathlib
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import files_backup  # noqa: E402


class FilesBackupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.data = self.root / "data"
        self.backups = self.root / "backups"
        (self.data / "uploads").mkdir(parents=True)
        (self.data / "photos").mkdir(parents=True)
        (self.data / "library").mkdir(parents=True)
        (self.data / "backups").mkdir(parents=True)
        (self.data / "uploads" / "деталь.3mf").write_bytes("3mf-содержимое".encode())
        (self.data / "photos" / "заказ.jpg").write_bytes(b"\xff\xd8" + "фото".encode())
        (self.data / "library" / "модель.stl").write_text("solid", encoding="utf-8")
        (self.data / ".printflow.key").write_text("ключ шифрования", encoding="utf-8")
        (self.data / "backups" / "старый.sqlite3").write_bytes("база".encode())
        self._patches = [
            mock.patch.object(files_backup, "DATA_DIR", self.data),
            mock.patch.object(files_backup, "BACKUP_DIR", self.backups),
            mock.patch.object(files_backup, "UPLOAD_DIR", self.data / "uploads"),
            mock.patch.object(files_backup, "PHOTO_DIR", self.data / "photos"),
            mock.patch.object(files_backup, "ensure_dirs", lambda: None),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()

    def test_manifest_counts_data_files(self):
        manifest = files_backup.collect_manifest()
        self.assertEqual(manifest["files"], 3)
        self.assertGreater(manifest["bytes"], 0)
        self.assertEqual({d["name"] for d in manifest["dirs"]},
                         {"uploads", "photos", "library"})

    def test_backup_archive_contains_data_not_secrets(self):
        report = files_backup.make_files_backup(prefix="printflow-test")
        self.assertTrue(report["ok"], report)
        archive = self.backups / report["file"]
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        self.assertIn("uploads/деталь.3mf", names)
        self.assertIn("photos/заказ.jpg", names)
        self.assertIn("backup-manifest.json", names)
        # ключ шифрования и сами бэкапы в архив не попадают
        self.assertFalse(any(".printflow.key" in name for name in names))
        self.assertFalse(any(name.startswith("backups/") for name in names))

    def test_archive_is_verified_after_write(self):
        """Архив читается — иначе это не копия, а мусор на диске."""
        report = files_backup.make_files_backup(prefix="printflow-test")
        self.assertTrue(report["ok"])
        self.assertGreaterEqual(report["entries"], 4)
        with tarfile.open(self.backups / report["file"], "r:gz") as tar:
            self.assertTrue(tar.getnames())

    def test_empty_storage_is_skipped_not_failed(self):
        for name in ("uploads", "photos", "library"):
            for path in (self.data / name).glob("*"):
                path.unlink()
        report = files_backup.make_files_backup(prefix="printflow-test")
        self.assertTrue(report["ok"])
        self.assertTrue(report["skipped"])
        self.assertEqual(list(self.backups.glob("*.tar.gz")) if self.backups.is_dir()
                         else [], [])

    def test_oversized_storage_is_refused(self):
        with mock.patch.object(files_backup, "MAX_TOTAL_BYTES", 10):
            report = files_backup.make_files_backup(prefix="printflow-test")
        self.assertFalse(report["ok"])
        self.assertIn("МБ", report["error"])

    def test_restore_puts_files_back(self):
        report = files_backup.make_files_backup(prefix="printflow-test")
        target = self.root / "restore"
        result = files_backup.restore_files_backup(report["file"], target)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["restored"], 3)
        self.assertTrue((target / "uploads" / "деталь.3mf").is_file())
        self.assertFalse((target / "backup-manifest.json").exists())

    def test_restore_rejects_traversal_paths(self):
        """Архив с «../» не должен писать за пределы каталога данных."""
        archive = self.backups / "злой.files.tar.gz"
        self.backups.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("../вышел-наружу.txt")
            payload = "здесь быть не должно".encode()
            info.size = len(payload)
            import io
            tar.addfile(info, io.BytesIO(payload))
        target = self.root / "restore2"
        result = files_backup.restore_files_backup("злой.files.tar.gz", target)
        self.assertTrue(result["ok"])
        self.assertEqual(result["restored"], 0)
        self.assertFalse((self.root / "вышел-наружу.txt").exists())

    def test_restore_missing_archive_is_reported(self):
        result = files_backup.restore_files_backup("нет-такого.files.tar.gz")
        self.assertFalse(result["ok"])
        self.assertIn("не найден", result["error"])

    def test_list_backups_newest_first(self):
        first = files_backup.make_files_backup(prefix="printflow-a")
        second = files_backup.make_files_backup(prefix="printflow-b")
        names = [row["name"] for row in files_backup.list_files_backups()]
        self.assertIn(first["file"], names)
        self.assertIn(second["file"], names)
        self.assertEqual(names, sorted(names, reverse=True))


if __name__ == "__main__":
    unittest.main()

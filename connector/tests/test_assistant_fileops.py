"""Раскладка файлов по делам (18.14, идея И144): план, граница папок, исполнение.

Навык двигает файлы на диске — единственное место в 18.14, где ассистент меняет
что-то за пределами своей базы. Поэтому контракты жёсткие:

  * план отделён от исполнения: `files.tidy_plan` — риск `read` и ничего не
    трогает, `files.tidy_apply` — риск `write` и спрашивает человека;
  * граница папок проверяется до чтения и до перемещения: путь вне папок из
    настроек агента отклоняется с причиной, а не «прочитали и посмотрим»;
  * назначения — подпапки той же папки, поэтому подменённый план не уедет в
    другое место диска;
  * удаления нет вовсе: при совпадении имён предлагается новое имя;
  * символы `..` и ссылки не обходят границу — путь приводится к абсолютному.
"""
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

from agent import config, fileops  # noqa: E402


class FolderTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name).resolve() / "Downloads"
        self.root.mkdir()
        self.addCleanup(self.tmp.cleanup)
        # Граница папок задаётся явно: тесты не ходят по живому ~/Downloads.
        patcher = mock.patch.object(config, "DOWNLOADS_FOLDER", str(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(config, "DOCUMENT_FOLDERS", ())
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(config, "KNOWLEDGE_FOLDERS", ())
        patcher.start()
        self.addCleanup(patcher.stop)

    def touch(self, name: str, text: str = "содержимое") -> pathlib.Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class GuardTests(FolderTestCase):
    def test_path_inside_folder_is_allowed(self):
        allowed, reason, resolved = fileops.inside_allowed(self.touch("a.stl"))
        self.assertTrue(allowed, reason)
        self.assertEqual("", reason)
        self.assertTrue(resolved.is_absolute())

    def test_path_outside_folder_is_refused(self):
        allowed, reason, _resolved = fileops.inside_allowed("/etc/passwd")
        self.assertFalse(allowed)
        self.assertIn("вне разрешённых папок", reason)

    def test_parent_escape_is_refused(self):
        allowed, reason, _resolved = fileops.inside_allowed(
            str(self.root / ".." / ".." / "secret.txt"))
        self.assertFalse(allowed)
        self.assertTrue(reason)

    def test_no_folders_configured_is_honest(self):
        with mock.patch.object(config, "DOWNLOADS_FOLDER", ""), \
                mock.patch.object(config, "DOCUMENT_FOLDERS", ()), \
                mock.patch.object(config, "KNOWLEDGE_FOLDERS", ()):
            allowed, reason, _resolved = fileops.inside_allowed(self.root / "a.stl")
        self.assertFalse(allowed)
        self.assertIn("PRINTFLOW_ASSISTANT_FOLDERS", reason)


class RuleTests(FolderTestCase):
    def test_name_rule_beats_suffix_rule(self):
        folder, why = fileops.destination_for(pathlib.Path("счёт-за-сентябрь.xlsx"))
        self.assertEqual("Счета", folder)
        self.assertIn("счёт", why)

    def test_model_suffix_goes_to_models(self):
        for name in ("деталь.stl", "корпус.3mf", "крышка.obj"):
            folder, _why = fileops.destination_for(pathlib.Path(name))
            self.assertEqual("Модели", folder)

    def test_gcode_photo_archive_documents(self):
        expected = {"job.gcode": "Нарезка", "фото.jpg": "Фото", "архив.zip": "Архивы",
                    "инструкция.pdf": "Документы", "setup.exe": "Установщики",
                    "договор-мария.docx": "Договоры", "заказ-145.md": "Заказы"}
        for name, folder in expected.items():
            found, _why = fileops.destination_for(pathlib.Path(name))
            self.assertEqual(folder, found, f"не туда просится «{name}»")

    def test_unknown_file_is_left_alone(self):
        folder, why = fileops.destination_for(pathlib.Path("нечто.xyz"))
        self.assertEqual("", folder)
        self.assertIn("не подходят", why)


class PlanTests(FolderTestCase):
    def test_plan_moves_nothing(self):
        self.touch("деталь.stl")
        self.touch("счёт-12.pdf")
        plan = fileops.plan(self.root)
        self.assertTrue(plan["ok"])
        self.assertEqual(2, plan["count"])
        self.assertTrue(all(pathlib.Path(row["path"]).exists() for row in plan["moves"]))
        self.assertIn("план", plan["hint"].casefold())

    def test_plan_explains_every_move(self):
        self.touch("деталь.stl")
        plan = fileops.plan(self.root)
        row = plan["moves"][0]
        self.assertEqual("Модели", row["destination"])
        self.assertTrue(row["reason"])
        self.assertTrue(row["target"].endswith("Модели/деталь.stl")
                        or row["target"].endswith(os.path.join("Модели", "деталь.stl")))

    def test_already_sorted_files_are_skipped(self):
        self.touch("Модели/деталь.stl")
        plan = fileops.plan(self.root)
        self.assertEqual(0, plan["count"])
        self.assertTrue(any("уже лежит" in row["reason"] for row in plan["skipped"]))

    def test_unknown_files_are_skipped_with_reason(self):
        self.touch("нечто.xyz")
        plan = fileops.plan(self.root)
        self.assertEqual(0, plan["count"])
        self.assertTrue(plan["skipped"][0]["reason"])

    def test_huge_file_is_skipped(self):
        path = self.root / "образ.stl"
        with path.open("wb") as handle:
            handle.truncate(fileops.MAX_MOVE_BYTES + 1)
        plan = fileops.plan(self.root)
        self.assertEqual(0, plan["count"])
        self.assertIn("МБ", plan["skipped"][0]["reason"])

    def test_name_collision_gets_new_name_not_overwrite(self):
        self.touch("Модели/деталь.stl")
        self.touch("деталь.stl")
        plan = fileops.plan(self.root)
        self.assertEqual(1, plan["count"])
        self.assertIn("(2)", plan["moves"][0]["target"])

    def test_plan_refuses_foreign_folder(self):
        plan = fileops.plan("/etc")
        self.assertFalse(plan["ok"])
        self.assertEqual([], plan["moves"])
        self.assertTrue(plan["reason"])

    def test_plan_refuses_missing_folder(self):
        plan = fileops.plan(self.root / "нет-такой")
        self.assertFalse(plan["ok"])
        self.assertIn("не найдена", plan["reason"])


class ExecuteTests(FolderTestCase):
    def test_files_are_moved_not_copied(self):
        source = self.touch("деталь.stl")
        plan = fileops.plan(self.root)
        result = fileops.execute(plan["moves"])
        self.assertTrue(result["ok"], result["failed"])
        self.assertEqual(1, result["moved"])
        self.assertFalse(source.exists())
        self.assertTrue((self.root / "Модели" / "деталь.stl").exists())

    def test_destination_folders_are_created(self):
        self.touch("деталь.stl")
        self.touch("счёт.pdf")
        self.touch("фото.jpg")
        result = fileops.execute(fileops.plan(self.root)["moves"])
        self.assertEqual(3, result["moved"])
        for folder in ("Модели", "Счета", "Фото"):
            self.assertTrue((self.root / folder).is_dir())

    def test_nothing_is_deleted(self):
        self.touch("деталь.stl")
        self.touch("Модели/деталь.stl")
        before = [path for path in self.root.rglob("*") if path.is_file()]
        fileops.execute(fileops.plan(self.root)["moves"])
        after = [path for path in self.root.rglob("*") if path.is_file()]
        self.assertEqual(len(before), len(after), "файл исчез вместо перемещения")
        # Перемещение с коллизией не перезаписывает: остаётся два файла.
        self.assertEqual(2, len(after))

    def test_foreign_target_is_refused(self):
        source = self.touch("деталь.stl")
        result = fileops.execute([{"path": str(source), "destination": "Модели",
                                   "target": "/tmp/чужое-место/деталь.stl"}])
        self.assertFalse(result["ok"])
        self.assertTrue(source.exists())
        self.assertIn("не в подпапке", result["failed"][0]["reason"])

    def test_missing_file_is_reported(self):
        result = fileops.execute([{"path": str(self.root / "нет.stl"), "destination": "Модели",
                                   "target": str(self.root / "Модели" / "нет.stl")}])
        self.assertFalse(result["ok"])
        self.assertIn("исчез", result["failed"][0]["reason"])

    def test_empty_plan_does_nothing(self):
        result = fileops.execute([])
        self.assertTrue(result["ok"])
        self.assertEqual(0, result["moved"])

    def test_execute_respects_folder_border_even_with_extra(self):
        """План, собранный для одной папки, не исполняется в другой."""
        source = self.touch("деталь.stl")
        other = pathlib.Path(self.tmp.name).resolve() / "other"
        other.mkdir()
        result = fileops.execute([{"path": str(source), "destination": "Модели",
                                   "target": str(other / "Модели" / "деталь.stl")}])
        self.assertFalse(result["ok"])
        self.assertTrue(source.exists())


class ConfigTests(unittest.TestCase):
    def test_file_folders_are_the_border(self):
        with mock.patch.object(config, "DOWNLOADS_FOLDER", "/tmp/downloads"), \
                mock.patch.object(config, "DOCUMENT_FOLDERS", ("/tmp/docs",)), \
                mock.patch.object(config, "KNOWLEDGE_FOLDERS", ("/tmp/knowledge",)):
            folders = config.file_folders()
        self.assertEqual(("/tmp/downloads", "/tmp/docs", "/tmp/knowledge"), folders)

    def test_defaults_do_not_include_system_folders(self):
        for folder in config.file_folders():
            lowered = folder.casefold()
            for forbidden in ("windows", "system32", "/etc", "/usr", "/bin"):
                self.assertNotIn(forbidden, lowered,
                                 f"папка «{folder}» похожа на системную")


if __name__ == "__main__":
    unittest.main()

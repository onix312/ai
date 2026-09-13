"""Документы не должны врать про версию и про команды запуска (волна 5).

README открывают первым, а версия в его заголовке отставала от кода на пять
мажорных релизов: «PrintFlow 11.1 … Текущая версия: 12.1.0» при `APP_VERSION`
17.0.16. Здесь три дешёвых контракта:

* заголовок версии в README и первая запись CHANGELOG совпадают с
  `APP_VERSION`;
* три шага запуска на месте и ведут на файлы, которые существуют;
* внутренние ссылки `docs/…` из README не битые.
"""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")


class VersionDocsTests(unittest.TestCase):
    def test_readme_version_matches_app_version(self):
        from connector.printflow import APP_VERSION
        match = re.search(r"^## Текущая версия:\s*(\S+)\s*$", README, re.M)
        self.assertIsNotNone(match, "в README нет заголовка «Текущая версия:»")
        self.assertEqual(APP_VERSION, match.group(1),
                         "заголовок версии в README разошёлся с APP_VERSION")

    def test_changelog_starts_with_current_version(self):
        from connector.printflow import APP_VERSION
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        first = re.search(r"^##\s+(\d+\.\d+\.\d+)", changelog, re.M)
        self.assertIsNotNone(first, "в CHANGELOG нет заголовка версии")
        self.assertEqual(APP_VERSION, first.group(1),
                         "последняя запись CHANGELOG не про текущую версию")

    def test_readme_title_has_no_stale_version(self):
        """«PrintFlow 11.1 — …» в заголовке: версию в названии вести нельзя."""
        title = README.splitlines()[0]
        self.assertNotRegex(title, r"\d+\.\d+",
                            f"в заголовке README зашита версия: {title}")


class LaunchDocsTests(unittest.TestCase):
    def test_three_steps_are_present(self):
        block = README.split("## Запуск за три шага", 1)[1].split("\n## ", 1)[0]
        steps = re.findall(r"^\d+\.\s", block, re.M)
        self.assertEqual(3, len(steps), f"шагов в блоке {len(steps)}, нужно 3")

    def test_referenced_entry_points_exist(self):
        for name in ("pf.py", "ЗАПУСТИТЬ.bat", "КАК-ЗАПУСТИТЬ.txt"):
            self.assertTrue((ROOT / name).exists(), f"нет {name}")
        self.assertIn("`python pf.py`", README)
        self.assertIn("`python pf.py doctor`", README)

    def test_doctor_command_is_real(self):
        """`pf.py doctor` обещан в README — проверим, что подкоманда есть."""
        launcher = (ROOT / "pf.py").read_text(encoding="utf-8")
        self.assertRegex(launcher, r"[\"']doctor[\"']",
                         "в pf.py нет подкоманды doctor, а README её обещает")


class InternalLinksTests(unittest.TestCase):
    def test_doc_links_resolve(self):
        links = set(re.findall(r"\]\(([^)#]+?\.(?:md|txt))\)", README))
        self.assertIn("CHANGELOG.md", links)
        broken = [link for link in sorted(links) if not (ROOT / link).exists()]
        self.assertEqual([], broken, "эти ссылки из README никуда не ведут")


if __name__ == "__main__":
    unittest.main()

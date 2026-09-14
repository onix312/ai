"""Документы не должны врать про версию, команды и картинки (волна 5, 18.0.11).

README открывают первым, а версия в его заголовке отставала от кода на пять
мажорных релизов: «PrintFlow 11.1 … Текущая версия: 12.1.0» при `APP_VERSION`
17.0.16. Здесь дешёвые контракты, которые держат первую страницу репозитория
честной:

* заголовок версии в README и первая запись CHANGELOG совпадают с
  `APP_VERSION`;
* три шага запуска на месте и ведут на файлы, которые существуют;
* внутренние ссылки `docs/…` из README не битые;
* картинки README лежат в репозитории (внешних бейджей с чужих CDN нет) и
  рисуются своим SVG;
* содержание README ведёт на существующие разделы, а разделы не теряются.
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


class ReadmeStructureTests(unittest.TestCase):
    """README — лицо репозитория: картинки свои, содержание не врёт.

    README открывают первым, поэтому у него два собственных контракта: все
    изображения лежат в репозитории (внешние бейджи — это чужой CDN на странице
    проекта, а система принципиально работает без внешних сервисов) и список
    «Содержание» ведёт на реально существующие разделы. Полноту списка держим
    от обратного: каждый `##`-раздел обязан быть в содержании.
    """

    def images(self) -> list[str]:
        found = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", README)
        found += re.findall(r'<img[^>]+src="([^"]+)"', README)
        return found

    def test_images_are_local_and_exist(self):
        images = self.images()
        self.assertGreaterEqual(len(images), 3, "в README нет картинок")
        for src in images:
            with self.subTest(src=src):
                self.assertFalse(src.startswith(("http://", "https://", "//")),
                                 f"картинка тянется из интернета: {src}")
                self.assertTrue((ROOT / src).is_file(), f"нет файла картинки: {src}")

    def test_svg_is_self_contained(self):
        """Схему и бейджи рисуем своим SVG: без <script>, <image> и внешних ссылок."""
        import xml.etree.ElementTree as ElementTree

        svgs = sorted((ROOT / name) for name in self.images() if name.endswith(".svg"))
        self.assertTrue(svgs, "нет ни одного SVG — проверять нечего")
        for path in svgs:
            with self.subTest(file=path.name):
                text = path.read_text(encoding="utf-8")
                ElementTree.fromstring(text)          # файл обязан быть корректным XML
                self.assertIn("viewBox=", text, "SVG без viewBox не масштабируется")
                for forbidden in ("<script", "<image", "xlink:href"):
                    self.assertNotIn(forbidden, text,
                                     f"{path.name}: {forbidden} — внешняя зависимость")

    def test_contents_matches_headings(self):
        """Содержание — карта README: ничего не пропущено и ничего не бито."""
        headings = re.findall(r"^## (.+?)\s*$", README, re.M)
        anchors = set(re.findall(r'<a id="([^"]+)">', README))
        contents = re.findall(r"^\s*[-*] \[[^\]]+\]\(#([^)]+)\)", README, re.M)
        self.assertGreaterEqual(len(contents), 8, "содержание подозрительно короткое")
        broken = [item for item in contents
                  if item not in anchors and item not in self.slugs(headings)]
        self.assertEqual([], broken, "эти ссылки содержания никуда не ведут")
        # Заголовок версии меняется вместе с номером, его якорь задан руками.
        skip = {"Содержание"}
        missed = [h for h in headings
                  if h not in skip and not h.lower().startswith("текущая версия")
                  and self.slug(h) not in contents]
        self.assertEqual([], missed, "разделы не попали в содержание")

    @staticmethod
    def slug(title: str) -> str:
        """Якорь GitHub: строчные буквы, пунктуация прочь, пробелы в дефисы."""
        kept = [ch for ch in title.strip().lower() if ch.isalnum() or ch in "-_ "]
        return "".join(kept).replace(" ", "-")

    @classmethod
    def slugs(cls, headings) -> set:
        return {cls.slug(h) for h in headings}


if __name__ == "__main__":
    unittest.main()

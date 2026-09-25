"""Индекс тестов `docs/ТЕСТЫ.md` не должен расходиться с папкой (17.0.21).

Тесты двух видов: одни запускают код, другие читают файлы как текст. Смешивать
их в одну кучу нельзя — по строковому контракту не видно, что логика сломалась,
а поведенческий тест не поймает съехавшую вёрстку. Индекс показывает, где какой
вид, и этот контракт держит его в синхроне с папкой.
"""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TESTS = ROOT / "connector" / "tests"
INDEX = ROOT / "docs" / "ТЕСТЫ.md"


def classify(path: pathlib.Path) -> str:
    """Вид теста по его исходнику: запускает код, читает текст или и то и то."""
    source = path.read_text(encoding="utf-8")
    # Агент компьютера (`agent/`) — такой же исполняемый код, как коннектор:
    # тест, который импортирует `agent`, запускает навыки, базу и сервер агента,
    # а не читает их как текст (уточнено в 18.21).
    runs_code = bool(re.search(r"^(from|import)\s+(connector\.printflow|agent)\b", source, re.M))
    reads_text = bool(re.search(r"read_text\(|open\(.*encoding", source))
    if runs_code and reads_text:
        return "оба"
    if runs_code:
        return "поведение"
    return "строки"


class SuiteMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = INDEX.read_text(encoding="utf-8")
        cls.files = sorted(TESTS.glob("test_*.py"))
        cls.listed = set(re.findall(r"`(test_[a-z0-9_]+)`", cls.text))

    def test_index_exists(self):
        self.assertTrue(INDEX.exists(), "нет docs/ТЕСТЫ.md")

    def test_every_test_file_is_listed(self):
        actual = {p.stem for p in self.files}
        self.assertEqual(actual, self.listed,
                         "индекс тестов разошёлся с папкой connector/tests")

    def test_total_count_is_real(self):
        self.assertIn(f"Файлов в `connector/tests/` — **{len(self.files)}**", self.text,
                      "пересчитайте файлы и обновите индекс")

    def test_kind_counts_are_real(self):
        counts = {"поведение": 0, "оба": 0, "строки": 0}
        for path in self.files:
            counts[classify(path)] += 1
        for kind, number in counts.items():
            with self.subTest(kind=kind):
                self.assertIn(f"| **{number}** |", self.text,
                              f"число тестов вида «{kind}» устарело")

    def test_string_only_tests_are_marked(self):
        """Файлы без запуска кода перечислены отдельно — их видно с первого взгляда."""
        section = self.text.split("## Только строки", 1)[1].split("##", 1)[0]
        for path in self.files:
            if classify(path) == "строки":
                with self.subTest(file=path.stem):
                    self.assertIn(f"`{path.stem}`", section)

    def test_rule_is_written_down(self):
        for phrase in ("обязан его запускать", "panel-check.js", "не заменяет поведенческий"):
            self.assertIn(phrase, self.text, f"в индексе нет правила про «{phrase}»")


if __name__ == "__main__":
    unittest.main()

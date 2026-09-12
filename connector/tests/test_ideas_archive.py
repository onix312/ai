"""Индекс каталогов идей не должен расходиться с папкой `docs/` (17.0.16).

Каталогов идей в репозитории 14, вместе больше пяти тысяч строк. Без индекса
новый каталог начинают писать заново и предлагают то, что уже согласовано или
уже отклонено владельцем. `docs/АРХИВ-ИДЕЙ.md` перечисляет их; здесь —
контракт, что список полный, а числа строк настоящие.
"""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
INDEX = DOCS / "АРХИВ-ИДЕЙ.md"


def catalogs() -> list[pathlib.Path]:
    files = sorted(DOCS.glob("ИДЕИ-*.md"))
    extra = DOCS / "ЗАКАЗЫ-100-ИДЕЙ.md"
    if extra.exists():
        files.append(extra)
    return files


class IdeasArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = INDEX.read_text(encoding="utf-8")
        cls.rows = dict(re.findall(r"\| `(docs/[^`]+\.md)` \| (\d+) \|", cls.text))

    def test_index_exists_and_lists_every_catalog(self):
        self.assertTrue(INDEX.exists(), "нет docs/АРХИВ-ИДЕЙ.md")
        listed = set(self.rows)
        actual = {f"docs/{p.name}" for p in catalogs()}
        self.assertEqual(actual, listed,
                         "индекс каталогов разошёлся с папкой docs/")

    def test_line_counts_are_real(self):
        wrong = {}
        for name, count in self.rows.items():
            real = len((ROOT / name).read_text(encoding="utf-8").splitlines())
            if real != int(count):
                wrong[name] = f"в индексе {count}, в файле {real}"
        self.assertEqual({}, wrong, "пересчитайте строки и обновите индекс")

    def test_owner_rules_are_written_down(self):
        """Правила владельца живут в индексе, а не только в переписке."""
        for phrase in ("текстом в чате", "125–151", "денежный контур"):
            self.assertIn(phrase, self.text, f"в индексе нет правила про «{phrase}»")

    def test_closed_catalogs_are_marked(self):
        for name in ("docs/ИДЕИ-9.0.md", "docs/ИДЕИ-9.3.md"):
            row = next((line for line in self.text.splitlines()
                        if line.startswith(f"| `{name}`")), "")
            self.assertIn("Закрыт", row, f"{name} не помечен как закрытый")


if __name__ == "__main__":
    unittest.main()

"""Проверки структуры основного HTML-интерфейса."""
from html.parser import HTMLParser
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = ROOT / "site" / "index.html"
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class _StructureParser(HTMLParser):
    """Строгая проверка вложенности для используемого нами HTML."""

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, dict[str, str | None]]] = []
        self.errors: list[str] = []
        self.view_parents: dict[str, list[str]] = {}
        self.report_parents: dict[str, list[tuple[str, str]]] = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id") or ""
        classes = set((attributes.get("class") or "").split())
        parent_ids = [item[1].get("id") or "" for item in self.stack]

        if "view" in classes and element_id:
            self.view_parents[element_id] = parent_ids
        if element_id in {"rep_channels", "rep_expenses"}:
            self.report_parents[element_id] = [
                (item[1].get("id") or "", item[1].get("class") or "")
                for item in self.stack
            ]

        if tag not in VOID_TAGS:
            self.stack.append((tag, attributes))

    def handle_endtag(self, tag):
        if not self.stack:
            self.errors.append(f"лишний </{tag}> в строке {self.getpos()[0]}")
            return
        opened_tag, attributes = self.stack[-1]
        if opened_tag != tag:
            opened_id = attributes.get("id") or "без id"
            self.errors.append(
                f"строка {self.getpos()[0]}: </{tag}> закрывает "
                f"<{opened_tag} id={opened_id}>"
            )
            # Восстанавливаем стек, чтобы показать и последующие ошибки.
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    del self.stack[index:]
                    return
            return
        self.stack.pop()


class SiteMarkupTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = _StructureParser()
        cls.parser.feed(INDEX_HTML.read_text(encoding="utf-8"))

    def test_index_has_balanced_markup(self):
        errors = list(self.parser.errors)
        if self.parser.stack:
            errors.append(f"не закрыты теги: {self.parser.stack[-5:]}")
        self.assertEqual(errors, [])

    def test_every_view_stays_inside_main_views_container(self):
        self.assertGreaterEqual(len(self.parser.view_parents), 12)
        for view_id, parent_ids in self.parser.view_parents.items():
            with self.subTest(view=view_id):
                self.assertIn("main", parent_ids)
                self.assertIn("views", parent_ids)

    def test_finance_report_cards_stay_in_report_grid(self):
        for element_id in ("rep_channels", "rep_expenses"):
            with self.subTest(element=element_id):
                parents = self.parser.report_parents[element_id]
                self.assertTrue(any(parent_id == "finpane-reports" for parent_id, _ in parents))
                self.assertTrue(any("grid" in classes.split() for _, classes in parents))

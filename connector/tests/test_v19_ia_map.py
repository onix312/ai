"""PrintFlow 19 final information architecture and functional map contracts."""
from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SITE = ROOT / "site"


class PrintFlowV19IAMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = (SITE / "index.html").read_text(encoding="utf-8")
        cls.core = (SITE / "assets" / "core.js").read_text(encoding="utf-8")
        cls.shell = (SITE / "assets" / "v19-shell.js").read_text(encoding="utf-8")
        cls.css = (SITE / "assets" / "v19-shell.css").read_text(encoding="utf-8")

    def test_financial_subsurfaces_are_not_duplicate_sidebar_items(self):
        nav = self.index.split('<nav id="side">', 1)[1].split('</nav>', 1)[0]
        self.assertNotIn('>СБП · входящие</a>', nav)
        self.assertNotIn('>Мобильная касса</a>', nav)
        self.assertNotIn('>Банк</a>', nav)
        self.assertIn('>Финансы</a>', nav)

    def test_rare_navigation_is_grouped_as_tools_and_system(self):
        nav = self.index.split('<nav id="side">', 1)[1].split('</nav>', 1)[0]
        self.assertIn('Инструменты и система', nav)
        self.assertIn('data-view="system-map"', nav)
        self.assertIn('>Карта системы</a>', nav)

    def test_system_map_is_registered_view(self):
        self.assertIn("'system-map': { title: 'Карта системы'", self.core)
        for alias in ("map: 'system-map'", "graph: 'system-map'", "functions: 'system-map'"):
            self.assertIn(alias, self.core)
        self.assertIn('id="view-system-map"', self.index)

    def test_map_covers_primary_business_domains(self):
        view = self.index.split('id="view-system-map"', 1)[1].split(
            '<section class="view" id="view-settings"', 1
        )[0]
        for label in (
            '>Сегодня</b>', '>Заказы</b>', '>Принтеры</b>', '>Очередь</b>',
            '>Склад</b>', '>Клиенты</b>', '>Финансы</b>',
            '>Печатные формы</b>', '>Клиент-бот</b>', '>LAN-страницы</b>',
            '>Настройки</b>', '>PrintFlow AI</h2>',
        ):
            self.assertIn(label, view)
        self.assertIn('read → plan → act → verify', view)

    def test_map_nodes_navigate_real_views(self):
        view = self.index.split('id="view-system-map"', 1)[1].split(
            '<section class="view" id="view-settings"', 1
        )[0]
        for route in (
            "dashboard", "orders", "printers", "queue", "products",
            "customers", "finance", "calc", "print", "clientbot", "pages", "settings",
        ):
            self.assertIn(f'data-view="{route}"', view)

    def test_map_opens_integrated_ai(self):
        self.assertIn("system_map_ai", self.shell)
        self.assertIn("system_map_ai_bottom", self.shell)
        self.assertIn("setOpen(true)", self.shell)

    def test_map_has_responsive_graph_layout(self):
        self.assertIn(".v19-map-grid", self.css)
        self.assertIn("grid-template-columns:repeat(3,minmax(0,1fr))", self.css)
        self.assertIn("@media (max-width:1040px)", self.css)
        self.assertIn("@media (max-width:700px)", self.css)


if __name__ == "__main__":
    unittest.main()

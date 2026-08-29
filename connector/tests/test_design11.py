"""Тесты релиза 11.1 «Дизайн»: иконки Н1 и «живая панель» Н3.

Браузера в CI нет, поэтому проверяется контракт между файлами: каждое
имя data-icon в index.html должно существовать в реестре icons.js,
скелетоны и «докрут» чисел — иметь свои стили/функции и уважать
prefers-reduced-motion.
"""
from __future__ import annotations

import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

SITE = ROOT / "site"
INDEX = SITE / "index.html"
ICONS_JS = SITE / "assets" / "icons.js"
CORE_JS = SITE / "assets" / "core.js"
APP_JS = SITE / "assets" / "app.js"
THEME_CSS = SITE / "assets" / "theme.css"
SW_JS = SITE / "sw.js"


def icon_registry() -> dict[str, str]:
    """Имена иконок из icons.js: ``имя: '<path…>'`` (одно на строку)."""
    text = ICONS_JS.read_text(encoding="utf-8")
    found = dict(re.findall(r"^  ([a-z][a-z0-9]*): '(.+?)',$", text, re.M))
    assert found, "реестр иконок не найден — формат icons.js изменился?"
    return found


def data_icons() -> list[str]:
    return re.findall(r'data-icon="([a-z][a-z0-9]*)"', INDEX.read_text(encoding="utf-8"))


class IconRegistryTests(unittest.TestCase):
    def test_registry_has_no_duplicates(self):
        """Повторное имя молча перезаписало бы чужую иконку."""
        names = re.findall(r"^  ([a-z][a-z0-9]*): ", ICONS_JS.read_text(encoding="utf-8"), re.M)
        self.assertEqual(len(names), len(set(names)), "дубликаты в реестре иконок")

    def test_every_icon_has_visible_body(self):
        for name, body in icon_registry().items():
            self.assertGreater(len(body.strip()), 10, f"иконка {name} подозрительно пустая")

    def test_icons_js_is_standalone(self):
        """Файл не тянет внешних адресов и не требует сборщика."""
        text = ICONS_JS.read_text(encoding="utf-8")
        for banned in ("http://", "https://", "import ", "fetch("):
            self.assertNotIn(banned, text, f"icons.js не должен содержать {banned!r}")


class IconWiringTests(unittest.TestCase):
    def test_every_data_icon_exists_in_registry(self):
        """data-icon с опечаткой оставил бы текстовый глиф молча — ловим сразу."""
        registry = icon_registry()
        used = set(data_icons())
        self.assertTrue(used, "в index.html нет ни одного data-icon")
        unknown = used - set(registry)
        self.assertEqual(set(), unknown, f"нет таких иконок: {sorted(unknown)}")

    def test_navigation_uses_icons(self):
        """Все пункты меню переведены на единый набор иконок."""
        text = INDEX.read_text(encoding="utf-8")
        nav_links = re.findall(r'class="nav-link[^"]*"[^>]*>.*?</a>', text)
        self.assertGreaterEqual(len(nav_links), 14)
        for link in nav_links:
            self.assertIn("data-icon=", link, f"у ссылки меню нет иконки: {link[:80]}")

    def test_icons_script_is_connected_and_cached(self):
        index = INDEX.read_text(encoding="utf-8")
        self.assertIn("assets/icons.js", index, "icons.js не подключён в index.html")
        sw = SW_JS.read_text(encoding="utf-8")
        self.assertIn("/assets/icons.js", sw, "иконки должны переживать мигание сети (SHELL)")


class LivingPanelTests(unittest.TestCase):
    """Н3: скелетоны, «докрут» чисел, анимация раздела."""

    def test_dashboard_has_skeletons(self):
        index = INDEX.read_text(encoding="utf-8")
        for anchor in ('id="dash_kpis"', 'id="dash_plan"', 'id="dash_events"', 'id="dash_active"'):
            self.assertIn(anchor, index)
        self.assertGreaterEqual(index.count('class="skel"'), 12,
                                "Обзор должен показывать форму раздела до данных")

    def test_skeleton_css_and_reduced_motion(self):
        css = THEME_CSS.read_text(encoding="utf-8")
        self.assertIn(".skel", css)
        self.assertIn("@keyframes pf-skel", css)
        self.assertRegex(css, r"@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.skel \{ animation: none")
        self.assertIn(".view.on", css)
        self.assertIn("@keyframes pf-view-in", css)

    def test_countup_lives_in_core_and_is_used_by_dashboard(self):
        core = CORE_JS.read_text(encoding="utf-8")
        self.assertIn("function countUp", core)
        self.assertIn("countUp,", core, "countUp должен попасть в PF.ui")
        self.assertIn("prefers-reduced-motion", core,
                      "«докрут» обязан уважать системную настройку анимации")
        app = APP_JS.read_text(encoding="utf-8")
        self.assertIn("animateKpis", app, "KPI Обзора не анимируются")
        self.assertIn("countUp", app)

    def test_kpi_tiles_have_stable_keys(self):
        """Ключ нужен, чтобы «докрут» сравнивал одно и то же значение между рендерами."""
        app = APP_JS.read_text(encoding="utf-8")
        self.assertIn('data-kpi="${esc(label)}"', app)


if __name__ == "__main__":
    unittest.main()

"""Тесты 3D-визуализации (релиз 12.5.0)."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site" / "assets"


class GCodeViewerTests(unittest.TestCase):
    """Проверки gcode-viewer.js."""

    def setUp(self):
        self.js = (SITE / "gcode-viewer.js").read_text(encoding="utf-8")

    def test_file_exists(self):
        """Файл gcode-viewer.js существует."""
        self.assertTrue((SITE / "gcode-viewer.js").exists())

    def test_has_parse_function(self):
        """Есть функция parseGcode."""
        self.assertIn("function parseGcode", self.js)

    def test_has_render_function(self):
        """Есть функция renderLayer."""
        self.assertIn("function renderLayer", self.js)

    def test_has_create_function(self):
        """Есть функция createViewer."""
        self.assertIn("function createViewer", self.js)

    def test_exports_gcodeviewer(self):
        """Экспортирует GCodeViewer в window."""
        self.assertIn("window.GCodeViewer", self.js)

    def test_parses_g1_commands(self):
        """Парсит G1 команды."""
        self.assertIn("cmd.startsWith('G1')", self.js)

    def test_parses_z_layers(self):
        """Парсит Z-слои."""
        self.assertIn("p.startsWith('Z')", self.js)

    def test_detects_extrusion(self):
        """Определяет экструзию."""
        self.assertIn("p.startsWith('E')", self.js)

    def test_renders_svg(self):
        """Рендерит SVG."""
        self.assertIn("<svg", self.js)
        self.assertIn("</svg>", self.js)

    def test_has_slider(self):
        """Есть слайдер слоёв."""
        self.assertIn('type="range"', self.js)

    def test_has_play_button(self):
        """Есть кнопка play/pause."""
        self.assertIn("gcode_play", self.js)

    def test_shows_stats(self):
        """Показывает статистику."""
        self.assertIn("gcode-stats", self.js)


class STLViewerTests(unittest.TestCase):
    """Проверки stl-viewer.js."""

    def setUp(self):
        self.js = (SITE / "stl-viewer.js").read_text(encoding="utf-8")

    def test_file_exists(self):
        """Файл stl-viewer.js существует."""
        self.assertTrue((SITE / "stl-viewer.js").exists())

    def test_has_parse_function(self):
        """Есть функция parseSTL."""
        self.assertIn("function parseSTL", self.js)

    def test_has_create_function(self):
        """Есть функция createViewer."""
        self.assertIn("function createViewer", self.js)

    def test_exports_stlviewer(self):
        """Экспортирует STLViewer в window."""
        self.assertIn("window.STLViewer", self.js)

    def test_parses_binary_stl(self):
        """Парсит бинарный STL."""
        self.assertIn("DataView", self.js)
        self.assertIn("getUint32", self.js)
        self.assertIn("getFloat32", self.js)

    def test_checks_threejs(self):
        """Проверяет наличие Three.js."""
        self.assertIn("typeof THREE", self.js)

    def test_shows_placeholder(self):
        """Показывает placeholder если Three.js не загружен."""
        self.assertIn("stl-viewer-placeholder", self.js)

    def test_has_install_instructions(self):
        """Есть инструкции по установке Three.js."""
        self.assertIn("three.min.js", self.js)
        self.assertIn("threejs.org", self.js)

    def test_creates_scene(self):
        """Создаёт Three.js сцену."""
        self.assertIn("THREE.Scene", self.js)
        self.assertIn("THREE.PerspectiveCamera", self.js)
        self.assertIn("THREE.WebGLRenderer", self.js)

    def test_has_mouse_controls(self):
        """Есть управление мышью."""
        self.assertIn("onmousedown", self.js)
        self.assertIn("onmousemove", self.js)

    def test_has_zoom(self):
        """Есть зум колесом."""
        self.assertIn("onwheel", self.js)

    def test_shows_stats(self):
        """Показывает статистику."""
        self.assertIn("stl-stats", self.js)
        self.assertIn("Треугольников", self.js)


class Viewer3DIntegrationTests(unittest.TestCase):
    """Интеграционные проверки 3D viewer."""

    def test_css_has_gcode_viewer(self):
        """CSS содержит стили gcode-viewer."""
        css = (SITE / "theme.css").read_text(encoding="utf-8")
        self.assertIn(".gcode-viewer", css)
        self.assertIn(".gcode-canvas", css)
        self.assertIn(".gcode-controls", css)

    def test_css_has_stl_viewer(self):
        """CSS содержит стили stl-viewer."""
        css = (SITE / "theme.css").read_text(encoding="utf-8")
        self.assertIn(".stl-viewer-placeholder", css)
        self.assertIn(".stl-stats", css)

    def test_docs_exist(self):
        """Документация 3D-ВИЗУАЛИЗАЦИЯ.md существует."""
        docs = ROOT / "docs" / "3D-ВИЗУАЛИЗАЦИЯ.md"
        self.assertTrue(docs.exists())
        content = docs.read_text(encoding="utf-8")
        self.assertIn("G-code viewer", content)
        self.assertIn("STL/3MF viewer", content)
        self.assertIn("three.min.js", content)


if __name__ == "__main__":
    unittest.main()

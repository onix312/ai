"""Headless-стенд панели: необъявленные переменные в JS (идея 94, Б1/Б2).

`node --check` проверяет синтаксис, поэтому файл с вызовом несуществующего
хелпера проходит проверку и падает уже в браузере. Так в панель уехали
`debounce is not defined` (marketing.js) и `fail is not defined`
(ops10.js): разделы выглядели целыми, но не работали.

Здесь запускается `scripts/panel-check.js` — он грузит все скрипты панели
в песочнице с заглушкой DOM и требует, чтобы необъявленных переменных не
было. Если node в окружении нет, проверка честно пропускается.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "panel-check.js"


class PanelSmokeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node не установлен — проверка пропущена")
    def test_no_undeclared_variables_in_panel_scripts(self):
        """Ни один модуль панели не обращается к необъявленной переменной."""
        result = subprocess.run(
            ["node", str(CHECKER)],
            capture_output=True, text=True, timeout=180, cwd=str(ROOT))
        self.assertEqual(
            0, result.returncode,
            f"Стенд панели нашёл ошибки:\n{result.stdout}\n{result.stderr}")
        self.assertIn("Необъявленных переменных нет", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "node не установлен — проверка пропущена")
    def test_checker_covers_every_asset(self):
        """Стенд обязан грузить все JS панели, а не выборочный список."""
        result = subprocess.run(
            ["node", str(CHECKER)],
            capture_output=True, text=True, timeout=180, cwd=str(ROOT))
        assets = sorted(p.name for p in (ROOT / "site" / "assets").glob("*.js"))
        self.assertTrue(assets, "JS-ассетов не найдено")
        self.assertIn(f"Панель: файлов {len(assets)}", result.stdout)

    def test_checker_exists(self):
        """Сам стенд на месте и подключён к check.py."""
        self.assertTrue(CHECKER.is_file(), "scripts/panel-check.js отсутствует")
        check = (ROOT / "scripts" / "check.py").read_text(encoding="utf-8")
        self.assertIn("panel-check.js", check)


if __name__ == "__main__":
    unittest.main()

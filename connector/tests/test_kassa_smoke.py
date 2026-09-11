"""Headless-стенд страницы кассы: логика очереди, а не только вёрстка.

`node --check` (test_interface.py) видит синтаксис, но не видит смысла: страница
может остаться «целой» и при этом положить продажу в очередь с новым номером —
а это вторая продажа на сервере и лишние деньги в ящике. Здесь запускается
`scripts/kassa-check.js`: он поднимает site/cashier.html в песочнице с заглушкой
DOM и проверяет денежные сценарии офлайн-контура.

Замеры на живом коннекторе — отдельный инструмент (scripts/kassa-lan.js): он
создаёт продажи и требует изолированного стенда, поэтому в unittest его нет.
Если node в окружении нет, проверка честно пропускается.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "kassa-check.js"
HARNESS = ROOT / "scripts" / "kassa-harness.js"
DRILL = ROOT / "scripts" / "kassa-lan.js"


class KassaSmokeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node не установлен — проверка пропущена")
    def test_queue_and_link_scenarios_pass(self):
        """Очередь держит номер продажи, а копия в оболочке переживает смену адреса."""
        result = subprocess.run(
            ["node", str(CHECKER)],
            capture_output=True, text=True, timeout=180, cwd=str(ROOT))
        self.assertEqual(
            0, result.returncode,
            f"Стенд кассы нашёл ошибки:\n{result.stdout}\n{result.stderr}")
        self.assertIn("OK: стенд кассы пройден", result.stdout)

    def test_checker_script_exists_and_loads_the_real_page(self):
        """Стенд обязан поднимать именно site/cashier.html, а не свою копию."""
        self.assertTrue(CHECKER.is_file(), "scripts/kassa-check.js отсутствует")
        self.assertTrue(HARNESS.is_file(), "scripts/kassa-harness.js отсутствует")
        html = (ROOT / "site" / "cashier.html").read_text(encoding="utf-8")
        self.assertIn('"use strict"', html)
        harness = HARNESS.read_text(encoding="utf-8")
        self.assertIn("site", harness)
        self.assertIn("cashier.html", harness)
        self.assertIn("<script>", harness)

    def test_lan_drill_exists_and_is_read_only_about_the_code(self):
        """Замер на живом стенде: продажи создаёт, исходники не трогает."""
        self.assertTrue(DRILL.is_file(), "scripts/kassa-lan.js отсутствует")
        drill = DRILL.read_text(encoding="utf-8")
        for marker in ("startRottenProxy", "queueSave" , "cashier_offline_q",
                       "offFlush(true)", "sessions"):
            self.assertIn(marker, drill, f"замер не покрывает: {marker}")
        # ни одного write в репозиторий: стенд не должен править код или доки
        for bad in ("writeFileSync", "appendFileSync", "unlinkSync", "rmSync"):
            self.assertNotIn(bad, drill, f"замер пишет в файлы: {bad}")

    def test_static_contracts_cover_the_new_cashier_markup(self):
        """Новые контракты страницы живут в test_site_markup (единый источник)."""
        markup = (ROOT / "connector" / "tests" / "test_site_markup.py").read_text(encoding="utf-8")
        for marker in ("test_update_banner_shows_size_hash_and_changelog",
                       "test_offline_queue_is_backed_up_into_the_shell",
                       "test_stream_ping_is_observable_for_the_cashier"):
            self.assertIn(marker, markup)
        # и не дублируются здесь: один контракт — одно место
        self.assertNotIn("installText", markup.replace("test_update_banner_shows", ""))


if __name__ == "__main__":
    unittest.main()

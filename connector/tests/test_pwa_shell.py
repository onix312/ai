"""PWA-оболочка: pre-кэш sw.js обязан совпадать с разметкой панели (Б4).

Список SHELL вёлся вручную и разошёлся с index.html: bridge.js, ops10.js,
workshop.js и gcode-viewer.js не кэшировались, поэтому офлайн-панель
открывалась «половиной» и падала на первом незагруженном модуле.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
INDEX = SITE / "index.html"
SW = SITE / "sw.js"


def shell_entries() -> list[str]:
    source = SW.read_text(encoding="utf-8")
    block = source.split("const SHELL = [", 1)[1].split("];", 1)[0]
    return re.findall(r"'([^']+)'", block)


class PwaShellTests(unittest.TestCase):
    def setUp(self):
        self.shell = set(shell_entries())
        self.index = INDEX.read_text(encoding="utf-8")

    def test_every_script_is_precached(self):
        """Каждый подключаемый index.html скрипт есть в pre-кэше."""
        referenced = {f"/assets/{name}" for name in
                      re.findall(r'<script src="assets/([^"?]+)', self.index)}
        missing = sorted(referenced - self.shell)
        self.assertEqual([], missing,
                         f"sw.js не кэширует скрипты панели: {missing}")

    def test_every_stylesheet_is_precached(self):
        """Каждый стиль панели есть в pre-кэше."""
        referenced = {f"/assets/{name}" for name in
                      re.findall(r'<link rel="stylesheet" href="assets/([^"?]+)', self.index)}
        missing = sorted(referenced - self.shell)
        self.assertEqual([], missing, f"sw.js не кэширует стили: {missing}")

    def test_no_ghost_entries(self):
        """В pre-кэше нет файлов, которых не существует на диске."""
        ghosts = []
        for entry in sorted(self.shell):
            if entry in ("/", "/manifest.webmanifest"):
                continue
            target = SITE / entry.lstrip("/")
            if not target.is_file():
                ghosts.append(entry)
        self.assertEqual([], ghosts, f"sw.js кэширует несуществующее: {ghosts}")

    def test_shell_cache_is_versioned(self):
        """Имя кэша меняется вместе со списком — иначе старая копия залипнет."""
        source = SW.read_text(encoding="utf-8")
        self.assertRegex(source, r"const CACHE = 'printflow-shell-v\d+';")

    def test_lazy_modules_are_precached(self):
        """Ленивые модули (идея 47) тоже должны быть в оболочке."""
        core = (SITE / "assets" / "core.js").read_text(encoding="utf-8")
        block = core.split("const LAZY_MODULES = {", 1)[1].split("};", 1)[0]
        lazy = re.findall(r"'([a-z0-9]+\.js)'", block)
        self.assertTrue(lazy, "ленивые модули не объявлены")
        missing = sorted(f"/assets/{name}" for name in lazy
                         if f"/assets/{name}" not in self.shell)
        self.assertEqual([], missing, f"ленивые модули вне pre-кэша: {missing}")


if __name__ == "__main__":
    unittest.main()

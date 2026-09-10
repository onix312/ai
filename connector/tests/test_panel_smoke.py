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

    def test_auto_queue_message_mentions_safety_gate(self):
        """UI не должен обещать автозапуск, если safety-gate ещё выключен."""
        printer = (ROOT / "site" / "assets" / "printer.js").read_text(encoding="utf-8")
        self.assertIn("unattended_dangerous_actions", printer)
        self.assertIn("Safety-gate выключен: задания пока запускаются вручную", printer)

    def test_ams_slot_picker_is_wired(self):
        """Кнопка «⇄ Склад» и мини-слоты AMS обязаны открывать пикер привязки.

        Разметка `data-pslot` рисуется в трёх местах (плитка слота на карточке
        принтера, кнопка «⇄ Склад» во вкладке «AMS и катушки», «Привязать» в
        баннере непривязанных слотов), а обработчик был потерян — кнопки
        выглядели рабочими, но ничего не делали.
        """
        printer = (ROOT / "site" / "assets" / "printer.js").read_text(encoding="utf-8")
        self.assertIn("data-pslot=", printer, "точки входа пикера пропали из разметки")
        self.assertIn("closest('[data-pslot]')", printer,
                      "нет делегированного обработчика клика по слоту AMS")
        self.assertIn("openSlotPicker(pid, slot)", printer,
                      "обработчик не открывает пикер привязки катушки")
        # Обработчик должен жить в bindPicker(), который вызывается при старте.
        picker = printer.split("function bindPicker()", 1)
        self.assertEqual(2, len(picker), "функция bindPicker() исчезла")
        self.assertIn("closest('[data-pslot]')", picker[1][:1200])
        self.assertIn("bindPicker();", printer)

    def test_printer_commands_use_busy_guards(self):
        """Повторный клик по командам принтера не должен слать дубли."""
        printer = (ROOT / "site" / "assets" / "printer.js").read_text(encoding="utf-8")
        for needle in ("btn.classList.contains('busy')",
                       "return await U.withBusy(btn, async () => {",
                       "button: cmd",
                       "button: jog",
                       "button: set",
                       "button: load",
                       "button: $('pr_speed_apply')",
                       "button: $('pr_skip_apply')",
                       "await U.withBusy($('pr_reconnect'), async () => {"):
            with self.subTest(needle=needle):
                self.assertIn(needle, printer)


if __name__ == "__main__":
    unittest.main()

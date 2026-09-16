"""Единый индикатор остатка — вертикальный «бак» (18.6).

Владелец: «кружки процентов заменить на что-то другое; в этом кружке не
отмечаются двойные цвета, градиенты и радуги». Правила:

* один паттерн: одна функция (PFSpoolColor.tank) и один CSS (.pf-tank);
* бак стоит во всех трёх местах: катушка склада, остаток товара, слот AMS;
* слот AMS красится цветом привязанной катушки через общий swatchCss —
  двойные цвета, градиент, радуга, шёлк-2 видны, а не сырой hex принтера;
* low (<15%), «не привязан» и «пусто» не сломаны;
* старых кружков (reel, prod-donut, pc) в коде не осталось.

Функция чистая (без DOM), поэтому прогоняется в node по исходному тексту —
так же проверены ordersSource()/filtered() (правило 4 docs/ТЕСТЫ.md).
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
COLORS_JS = ROOT / "site" / "assets" / "colors.js"

_TANK_CASES = """
const cases = [
  [{ pct: 62, fill: '#e11d48', size: 'md' }],
  [{ pct: 0, fill: '#e11d48', size: 'sm' }],
  [{ pct: 8, fill: '#e11d48', size: 'xs' }],
  [{ pct: 140, fill: '#e11d48' }],
  [{ pct: -5, fill: '#e11d48' }],
  [{ pct: 50, fill: 'linear-gradient(135deg, #ff0000 0%, #00ff00 100%)' }],
  [{ pct: 33, fill: '#e11d48', caption: '320 г', title: '320 / 1000 г' }],
  [{ pct: 8, fill: '#e11d48', low: false }],
  [{ pct: 80, fill: '#e11d48', low: true }],
  [{ pct: 50, fill: '#e11d48";background:url(x)', title: 'a"b' }],
];
console.log(JSON.stringify(cases.map((a) => PFSpoolColor.tank.apply(null, a))));
"""


def _run_tank_cases() -> list[str]:
    """Прогнать tank() в node: заглушка window, дальше — настоящий исходник."""
    script = ("var window = {};\n" + COLORS_JS.read_text(encoding="utf-8")
              + "\nvar PFSpoolColor = window.PFSpoolColor;\n" + _TANK_CASES)
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                          timeout=30, cwd=ROOT)
    assert proc.returncode == 0, f"node упал: {proc.stderr[:500]}"
    return json.loads(proc.stdout)


@unittest.skipUnless(shutil.which("node"), "нужен node")
class TankBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = _run_tank_cases()

    def test_level_and_caption(self):
        html = self.out[0]
        self.assertIn("pf-tank md", html)
        self.assertIn("--lvl:62%", html)
        self.assertIn("--fill:#e11d48", html)
        self.assertIn("<b>62%</b>", html)

    def test_empty_state(self):
        html = self.out[1]
        self.assertIn("pf-tank sm empty", html)
        self.assertIn("--lvl:0%", html)

    def test_low_state_auto(self):
        html = self.out[2]
        self.assertIn("pf-tank xs low", html)

    def test_percent_is_clamped(self):
        self.assertIn("--lvl:100%", self.out[3])
        self.assertIn("--lvl:0%", self.out[4])
        self.assertIn("empty", self.out[4])

    def test_gradient_fill_passes_through(self):
        self.assertIn("linear-gradient(135deg, #ff0000 0%, #00ff00 100%)", self.out[5])

    def test_custom_caption_and_title(self):
        self.assertIn("<b>320 г</b>", self.out[6])
        self.assertIn('title="320 / 1000 г"', self.out[6])

    def test_low_flag_is_overridable(self):
        self.assertNotIn("low", self.out[7].split("<b>")[0])
        self.assertIn("low", self.out[8])

    def test_injection_is_neutralized(self):
        html = self.out[9]
        self.assertNotIn('";background', html)
        self.assertIn('title="a&quot;b"', html)


class TankContractTests(unittest.TestCase):
    """Строковые контракты: бак стоит везде, кружков не осталось."""

    def test_tank_function_exists(self):
        src = COLORS_JS.read_text(encoding="utf-8")
        self.assertIn("function tank(o)", src)
        self.assertIn("tank", src.split("return {", 1)[1].split("}", 1)[0])

    def test_tank_css_exists(self):
        css = (ROOT / "site" / "assets" / "tokens.css").read_text(encoding="utf-8")
        for token in (".pf-tank", ".pf-tank.sm", ".pf-tank.xs",
                      ".pf-tank.low", ".pf-tank.empty", "--lvl", "--fill"):
            self.assertIn(token, css, f"в tokens.css нет {token}")

    def test_spool_card_uses_tank(self):
        src = (ROOT / "site" / "assets" / "money.js").read_text(encoding="utf-8")
        self.assertIn("PFSpoolColor.tank", src)
        self.assertNotIn('class="reel"', src)
        self.assertNotIn("reel-pct", src)

    def test_product_card_uses_tank(self):
        src = (ROOT / "site" / "assets" / "products.js").read_text(encoding="utf-8")
        self.assertIn("PFSpoolColor.tank", src)
        self.assertIn("size: 'sm'", src)
        self.assertNotIn("prod-donut", src)

    def test_slot_uses_bound_spool_colors(self):
        src = (ROOT / "site" / "assets" / "printer.js").read_text(encoding="utf-8")
        self.assertIn("PFSpoolColor.swatchCss(bound", src)
        self.assertIn("PFSpoolColor.tank", src)
        self.assertNotIn('<span class="pc">', src)

    def test_slot_states_survived(self):
        src = (ROOT / "site" / "assets" / "printer.js").read_text(encoding="utf-8")
        for token in ("'unbound'", "'low'", "'пусто'", "'не привязан'"):
            self.assertIn(token, src, f"в printer.js потеряно {token}")

    def test_tube_uses_bound_spool_colors(self):
        src = (ROOT / "site" / "assets" / "printer.js").read_text(encoding="utf-8")
        self.assertIn("tubeCss", src)
        self.assertIn("gradient(", src)

    def test_pult_slot_uses_tank_and_bound_colors(self):
        src = (ROOT / "site" / "control.html").read_text(encoding="utf-8")
        self.assertIn("PFSpoolColor.tank", src)
        self.assertIn("PFSpoolColor.swatchCss(bound", src)

    def test_dead_circle_css_removed(self):
        app = (ROOT / "site" / "assets" / "app.css").read_text(encoding="utf-8")
        self.assertNotIn(".reel", app)
        theme = (ROOT / "site" / "assets" / "theme.css").read_text(encoding="utf-8")
        self.assertNotIn(".prod-donut", theme)
        more = (ROOT / "site" / "assets" / "more.css").read_text(encoding="utf-8")
        self.assertNotIn(".pslot .pc", more)


if __name__ == "__main__":
    unittest.main()

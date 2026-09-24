"""Флаер кофейням: макет, который уходит в копицентр пачкой (18.12.4).

Заход в кофейни идёт «коробка образцов + флаер», и у листовки цена ошибки
бумажная: опечатка в раскладке — это не красная консоль, а сто напечатанных
и выброшенных карточек. Поэтому здесь закреплены только те свойства, которые
ломаются молча и видны лишь на бумаге:

* размеры карточек и раскладка листов считаются так, что ряд помещается в
  печатное поле A4 (210 × 297 минус поля 8 мм);
* A6 печатается на альбомном листе, «компакт» — на портретном, и правило
  @page переключается скриптом, а не остаётся от предыдущего формата;
* печатная ширина листа не наследуется от `generator.css`: фиксированные
  297 мм выталкивали каждую вторую карточку на пустую страницу;
* домен `nozza.ru` в QR не зашит — он не куплен, тираж уйдёт в никуда;
* текст блоков 01–04 подгоняется по высоте, а если не влез — страница
  ругается, а не режет фразу;
* страница подключена к витрине материалов и к библиотеке панели.
"""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PAGE = ROOT / "site" / "materials" / "флаер-кофейням.html"
MATERIALS_INDEX = ROOT / "site" / "materials" / "index.html"
PANEL = ROOT / "site" / "index.html"

# Печатное поле A4 при полях 8 мм со всех сторон.
FIELD_PORTRAIT = (210 - 16, 297 - 16)
FIELD_LANDSCAPE = (297 - 16, 210 - 16)


class FlyerLayoutTests(unittest.TestCase):
    """Геометрия: карточки обязаны помещаться на лист."""

    @classmethod
    def setUpClass(cls):
        cls.text = PAGE.read_text(encoding="utf-8")

    def css_mm(self, selector: str, prop: str) -> float:
        """Значение свойства в мм из блока правил конкретного селектора."""
        block = re.search(rf"^\s*{re.escape(selector)}\s*\{{(.+?)\}}",
                          self.text, re.S | re.M)
        self.assertIsNotNone(block, f"в стилях нет правила {selector}")
        found = re.search(rf"{prop}\s*:\s*([\d.]+)mm", block.group(1))
        self.assertIsNotNone(found, f"у {selector} нет {prop} в мм")
        return float(found.group(1))

    def test_card_sizes_are_the_promised_formats(self):
        """A6 — 105 × 148, «компакт» — 95 × 135: это обещано в панели и note."""
        self.assertEqual(105.0, self.css_mm(".fl", "width"))
        self.assertEqual(148.0, self.css_mm(".fl", "height"))
        self.assertEqual(95.0, self.css_mm(".fl.compact", "width"))
        self.assertEqual(135.0, self.css_mm(".fl.compact", "height"))

    def test_a6_pair_fits_landscape_sheet(self):
        """Лицо + оборот рядом (сгиб пополам) — 210 мм при поле 281 мм."""
        pair = 2 * self.css_mm(".fl", "width")
        self.assertLessEqual(pair, FIELD_LANDSCAPE[0])
        self.assertLessEqual(self.css_mm(".fl", "height"), FIELD_LANDSCAPE[1])

    def test_a6_duplex_row_fits_landscape_sheet(self):
        """Два лица в ряд с зазором .row2 — тоже в пределах альбомного листа."""
        gap = self.css_mm(".row2", "gap")
        self.assertLessEqual(2 * self.css_mm(".fl", "width") + gap, FIELD_LANDSCAPE[0])

    def test_compact_grid_fits_portrait_sheet(self):
        """Четыре «компакта» 2 × 2 — в портретное поле 194 × 281 мм."""
        gaps = re.search(r"^\s*\.grid4\s*\{(.+?)\}", self.text, re.S | re.M)
        self.assertIsNotNone(gaps, "нет правила .grid4")
        row_gap, col_gap = re.search(r"gap\s*:\s*([\d.]+)mm\s+([\d.]+)mm",
                                     gaps.group(1)).groups()
        width = 2 * self.css_mm(".fl.compact", "width") + float(col_gap)
        height = 2 * self.css_mm(".fl.compact", "height") + float(row_gap)
        self.assertLessEqual(width, FIELD_PORTRAIT[0], "ряд «компактов» шире листа")
        self.assertLessEqual(height, FIELD_PORTRAIT[1], "два ряда выше листа")

    def test_compact_pair_fits_portrait_sheet(self):
        """«Компакт» со сгибом: пара 190 мм — влезает в портретные 194 мм."""
        self.assertLessEqual(2 * self.css_mm(".fl.compact", "width"), FIELD_PORTRAIT[0])

    def test_print_css_drops_fixed_sheet_width(self):
        """297 мм из generator.css на печати выталкивают карточки на пустую
        страницу: в @media print ширина листа обязана стать auto."""
        printed = self.text.split("@media print", 1)
        self.assertEqual(2, len(printed), "на странице нет правил @media print")
        self.assertRegex(printed[1], r"\.sheet[^{]*\{[^}]*width:\s*auto")

    def test_page_orientation_follows_format(self):
        """A6 — альбомный лист, «компакт» — портретный."""
        self.assertIn("@page { size: A4 landscape; margin: 8mm; }", self.text)
        self.assertIn("@page { size: A4; margin: 8mm; }", self.text)
        self.assertIn("applyPageSize(landscape)", self.text)
        self.assertIn("const landscape = !compact;", self.text)


class FlyerContentTests(unittest.TestCase):
    """Содержание: обещания макета и защита от брака в тираже."""

    @classmethod
    def setUpClass(cls):
        cls.text = PAGE.read_text(encoding="utf-8")

    def test_four_blocks_of_the_offer_are_present(self):
        """Блоки 01–04 — скелет предложения кофейне; поля обязаны быть все."""
        for i in (1, 2, 3, 4):
            with self.subTest(block=i):
                self.assertIn(f'data-page="s{i}t"', self.text)
                self.assertIn(f'data-page="s{i}d"', self.text)

    def test_no_hardcoded_domain_in_qr(self):
        """Домен не куплен (docs/БРЕНД-NOZZA.md): QR ведёт в Telegram."""
        self.assertNotIn("nozza.ru", self.text)
        self.assertIn("https://t.me/", self.text)

    def test_no_price_promises(self):
        """Цену владелец называет в переписке: в макете её быть не должно."""
        self.assertNotRegex(self.text, r"\d+\s*₽")
        self.assertNotIn("руб.", self.text)

    def test_contacts_are_telegram_only(self):
        """Решение владельца: только логотип, имя бренда, Telegram и QR."""
        self.assertNotIn('data-nz="phone"', self.text)
        self.assertNotIn('data-nz="site"', self.text)
        self.assertIn('data-nz="tg"', self.text)

    def test_text_is_fitted_and_overflow_is_loud(self):
        """Длинный текст сначала ужимается, потом честно ругается."""
        self.assertIn("function fitBacks()", self.text)
        self.assertIn("--fit", self.text)
        self.assertIn("overWarn", self.text)
        self.assertIn("overbadge", self.text)
        # Красная метка — экранная подсказка, на бумагу она не идёт.
        self.assertRegex(self.text, r"@media print \{ \.overbadge \{ display: none")

    def test_named_flyers_drive_the_print_run(self):
        """Список кофеен задаёт тираж: каждому заведению — свой флаер."""
        self.assertIn("function placeList()", self.text)
        self.assertIn("const count = places.length ||", self.text)

    def test_food_safety_disclaimer_is_kept(self):
        """Контакт с напитком не обещаем (docs/B2B-СКРИПТЫ.md, п. 4)."""
        self.assertIn("не касается напитка", self.text)

    def test_page_uses_shared_engine_and_local_qr(self):
        """Общая панель и QR без интернета — как у остальных генераторов."""
        self.assertIn('<script src="nz.js"></script>', self.text)
        self.assertIn('<script src="../assets/qr.js"></script>', self.text)
        self.assertIn("NZ.mount(draw)", self.text)


class FlyerWiringTests(unittest.TestCase):
    """Макет, которого нет в витрине, владелец не найдёт."""

    def test_listed_in_materials_index(self):
        text = MATERIALS_INDEX.read_text(encoding="utf-8")
        self.assertIn('href="флаер-кофейням.html"', text)

    def test_listed_in_panel_library(self):
        text = PANEL.read_text(encoding="utf-8")
        self.assertIn('href="materials/флаер-кофейням.html"', text)


if __name__ == "__main__":
    unittest.main()

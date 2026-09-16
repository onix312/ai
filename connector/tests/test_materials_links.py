"""Материалы печати не должны вести на несуществующие файлы (18.4.2).

В 18.4.1 удалили страницу выбора логотипа и часть бренд-SVG — а кнопки
«Логотипы» в генераторах остались и отправляли покупателя в 404.
`site/materials/` — это то, что уходит в копицентр и на бумагу: битая
ссылка тут стоит не «ошибка в консоли», а тираж брака.

Контракт: каждая статическая ссылка `href`/`src` в материалах указывает на
существующий файл; внешние ссылки и собираемые в JS через `${…}` не
проверяются — их не проверить без выполнения страницы.
"""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
MATERIALS = ROOT / "site" / "materials"
REF_RE = re.compile(r'(?:src|href)="([^"]+)"')
SKIP_PREFIXES = ("http://", "https://", "//", "#", "data:", "mailto:", "tel:", "javascript:")


class MaterialsStaticLinksTests(unittest.TestCase):
    def pages(self) -> list[pathlib.Path]:
        pages = sorted(MATERIALS.glob("*.html"))
        self.assertGreaterEqual(len(pages), 10, "материалов подозрительно мало")
        return pages

    def test_static_refs_resolve(self):
        broken: list[str] = []
        for html in self.pages():
            text = html.read_text(encoding="utf-8")
            for ref in REF_RE.findall(text):
                # JS-сборка вида src="' + NZ.markSrc(t) + '" даёт фрагменты с
                # кавычкой — это не статическая ссылка, проверяем только их части,
                # которые попадут в DOM, отдельно.
                if "${" in ref or "'" in ref or not ref or ref.startswith(SKIP_PREFIXES):
                    continue
                clean = ref.split("#", 1)[0].split("?", 1)[0]
                if not clean:
                    continue
                target = (html.parent / clean).resolve()
                if not target.exists():
                    broken.append(f"{html.name}: {ref}")
        self.assertEqual([], broken, "битые локальные ссылки в материалах")

    def test_no_logo_choice_leftovers(self):
        """Страница выбора логотипа удалена в 18.4.1 — кнопок на неё быть не должно."""
        for html in self.pages():
            with self.subTest(file=html.name):
                text = html.read_text(encoding="utf-8")
                self.assertNotIn("логотипы-на-выбор", text)
                self.assertNotIn("nozza-mark-2", text)
                self.assertNotIn("nozza-mark-3", text)
                self.assertNotIn("nozza-mark-4", text)

    def test_stickers_page_has_no_hangtags(self):
        """Бирки убраны решением владельца (18.4.2): разметка не должна их рисовать."""
        text = (MATERIALS / "наклейки-и-бирки.html").read_text(encoding="utf-8")
        self.assertNotIn("hangtag", text)
        self.assertNotIn("/api/catalog", text)

    def test_qr_links_not_hardcoded_domain(self):
        """Домен не куплен: новые конструкторы не зашивают nozza.ru в QR."""
        for name in ("наклейки-и-бирки.html", "визитки-и-таблички.html"):
            with self.subTest(file=name):
                text = (MATERIALS / name).read_text(encoding="utf-8")
                self.assertNotIn("nozza.ru/p/", text)


if __name__ == "__main__":
    unittest.main()

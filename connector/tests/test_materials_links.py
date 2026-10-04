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

    def test_inventory_labels_use_physical_v2_recipes_for_a4_print(self):
        text = (ROOT / "site" / "labels.html").read_text(encoding="utf-8")
        self.assertIn("fetch('/assets/print-layouts-v2.json')", text)
        self.assertIn("width:70mm; height:50mm", text)
        self.assertIn("grid-template-columns:repeat(2,70mm)", text)
        self.assertIn("grid-template-rows:repeat(5,50mm)", text)
        self.assertIn("id === 'spool-a4' || entry.id === 'shelf-qr'", text)
        self.assertIn("data-block=", text)

    def test_seller_documents_use_their_a4_recipes(self):
        import json

        text = (MATERIALS / "памятка-продавцу.html").read_text(encoding="utf-8")
        spec = json.loads((ROOT / "site" / "assets" / "print-layouts-v2.json")
                          .read_text(encoding="utf-8"))
        recipes = {doc["id"]: doc for doc in spec["documents"]
                   if doc["id"] in ("seller", "seller-forms")}
        self.assertEqual(9, len(recipes["seller"]["blocks"]))
        self.assertEqual(15, len(recipes["seller-forms"]["blocks"]))
        self.assertIn("=== 'seller-forms'", text)
        self.assertIn("document.id === formId", text)
        self.assertIn("layout.width_mm !== 210 || layout.height_mm !== 297", text)
        self.assertIn("block.x_mm}mm;top:${block.y_mm}mm", text)
        self.assertIn("width:${block.width_mm}mm;` +", text)
        self.assertIn("height:${block.height_mm}mm;", text)
        self.assertIn("nozza-print-crop-${crop}.png", text)
        self.assertIn('href="?form=seller-forms"', text)
        self.assertIn("Object.prototype.hasOwnProperty.call(values, block.id)", text)
        for block in recipes["seller"]["blocks"]:
            if block["kind"] != "logo":
                self.assertRegex(text, rf"['\"]?{re.escape(block['id'])}['\"]?\s*:")
        self.assertIn(".page-frame { width: 100%; padding: 0 8px 16px; overflow-x: auto; }", text)
        self.assertIn("const overflowing = [...sheet.querySelectorAll", text)
        self.assertNotIn("обновлено: август 2026", text)
        self.assertNotIn("linear-gradient", text)

    def test_print_logo_keeps_light_ribbon_separator(self):
        """Печатные crop сохраняют светлый разрыв выбранного знака поверх цветной бумаги."""
        from PIL import Image

        brand = ROOT / "site" / "assets" / "brand"
        samples = (
            ("nozza-print-crop-335-253-910-326.png", (98, 195), (400, 20)),
            ("nozza-print-crop-598-715-355-126.png", (62, 98), (300, 20)),
        )
        for name, stripe, outside in samples:
            with self.subTest(asset=name), Image.open(brand / name) as image:
                rgba = image.convert("RGBA")
                self.assertEqual(255, rgba.getpixel(stripe)[3], "внутренний светлый разрыв не должен стать прозрачным")
                self.assertEqual(0, rgba.getpixel(outside)[3], "внешний фон печатного логотипа должен оставаться прозрачным")
                self.assertTrue(all(channel >= 220 for channel in rgba.getpixel(stripe)[:3]))

    def test_zone_sign_logo_uses_transparent_print_crop(self):
        """The zone wordmark has no baked-in background that can look pasted on."""
        from PIL import Image

        page = (MATERIALS / "таблички-зон.html").read_text(encoding="utf-8")
        self.assertIn("nozza-print-crop-335-253-910-326.png", page)
        self.assertNotIn("nozza-zone-logo-lavender.png", page)
        asset = ROOT / "site" / "assets" / "brand" / "nozza-print-crop-335-253-910-326.png"
        with Image.open(asset) as image:
            rgba = image.convert("RGBA")
            self.assertEqual((910, 326), rgba.size)
            self.assertEqual(0, rgba.getpixel((400, 20))[3], "фон вокруг логотипа должен быть прозрачным")
            stripe = rgba.getpixel((98, 195))
            self.assertEqual(255, stripe[3], "светлый разделитель внутри знака должен сохраниться")
            self.assertTrue(all(channel >= 220 for channel in stripe[:3]))

    def test_box_label_editor_uses_v2_screen_shell_and_preserves_print_card(self):
        text = (MATERIALS / "наклейки-на-коробки.html").read_text(encoding="utf-8")
        self.assertIn("../assets/nozza-design-v2.css", text)
        self.assertIn('<body class="pf-utility">', text)
        self.assertIn("@media screen {", text)
        self.assertIn("body.pf-utility .editor { background: #352A37", text)
        self.assertIn(".stk { position:relative; flex:none; width:92mm; height:52mm", text)
        self.assertIn("@media print { .stk { cursor:default; }", text)
        self.assertIn("boxLabelLayout.blocks.map", text)
        for action in ("btnAdd", "btnDup", "btnDel", "btnReset", "btnPrint", "e_qr"):
            with self.subTest(action=action):
                self.assertIn(f'id="{action}"', text)

    def test_production_stickers_use_v2_screen_shell_and_keep_print_recipes(self):
        text = (MATERIALS / "наклейки-и-бирки.html").read_text(encoding="utf-8")
        self.assertIn("../assets/nozza-design-v2.css", text)
        self.assertIn('<body class="pf-utility">', text)
        self.assertIn("@media screen {", text)
        self.assertIn("body.pf-utility .panel input, body.pf-utility .panel select", text)
        self.assertIn("printLayoutsV2?.documents?.find(x=>x.id===id)", text)
        self.assertIn("data-layout=\"${id}\" style=\"width:${mm(spec.width_mm)};height:${mm(spec.height_mm)}\"", text)
        self.assertIn('href="generator.css?v=18.23.1"', text)
        self.assertIn("background:#fff; color:#31242e", text)
        for action in ("id=\"print\"", "id=\"n40c\"", "id=\"n40l\"", "id=\"n30\"", "id=\"n50\"", "id=\"mono\"", "id=\"qrrect\"", "id=\"rectTemplate\""):
            with self.subTest(action=action):
                self.assertIn(action, text)

    def test_wobbler_editor_uses_v2_screen_shell_and_preserves_atlas_card(self):
        text = (MATERIALS / "воблеры.html").read_text(encoding="utf-8")
        self.assertIn("../assets/nozza-design-v2.css", text)
        self.assertIn('<body class="pf-utility">', text)
        self.assertIn("@media screen {", text)
        self.assertIn("body.pf-utility .editor { background: #352A37", text)
        self.assertIn(".wob { position:relative; flex:none; width:var(--wobbler-width,92mm); height:var(--wobbler-height,87mm)", text)
        self.assertIn("layout.blocks.forEach", text)
        self.assertIn("@media print { #sheets { overflow:visible; }", text)
        for action in ("printWobblers", "btnAdd", "btnDup", "btnDel", "btnReset", "confirmed", "qron"):
            with self.subTest(action=action):
                self.assertRegex(text, rf'id="{action}"|id={action}')

    def test_how_order_screen_shell_preserves_json_cards_and_duration_field(self):
        text = (MATERIALS / "как-заказать.html").read_text(encoding="utf-8")
        self.assertIn('<body class="pf-print-tool">', text)
        self.assertIn("@media screen {", text)
        self.assertIn("body.pf-print-tool .panel {", text)
        self.assertIn(".card { width:93.5mm; height:92mm", text)
        self.assertIn("data.documents||[]", text)
        self.assertIn("d.id==='how-order'", text)
        self.assertIn("Ответ в течение '+srok", text)
        self.assertIn("@media print { .panel { display: none; }", text)
        self.assertIn('id="srok"', text)

    def test_sign_constructor_maps_shop_and_shelf_sizes_to_v2_recipes(self):
        import json

        page = (MATERIALS / "вывески-и-таблички.html").read_text(encoding="utf-8")
        spec = json.loads((ROOT / "site" / "assets" / "print-layouts-v2.json")
                          .read_text(encoding="utf-8"))
        recipes = {doc["id"]: doc for doc in spec["documents"]
                   if doc["id"] in ("door-sign", "shop-sign", "shelf-sign")}
        self.assertEqual({
            "door-sign": (186, 86),
            "shop-sign": (92, 65),
            "shelf-sign": (90, 55),
        }, {key: (value["width_mm"], value["height_mm"]) for key, value in recipes.items()})
        self.assertIn('<option value="shop">', page)
        self.assertIn('<option value="shelf">', page)
        self.assertIn("shop: 'shop-sign'", page)
        self.assertIn("shelf: 'shelf-sign'", page)
        self.assertIn("const recipe = signRecipes[RECIPE_BY_SIZE[size]]", page)
        self.assertIn("body:not(.sign-recipes-ready) .sg.json-layout", page)
        for recipe in recipes.values():
            self.assertEqual(3, len(recipe["blocks"]))
            for block in recipe["blocks"]:
                self.assertGreaterEqual(block["x_mm"], 0)
                self.assertGreaterEqual(block["y_mm"], 0)
                self.assertLessEqual(block["x_mm"] + block["width_mm"], recipe["width_mm"])
                self.assertLessEqual(block["y_mm"] + block["height_mm"], recipe["height_mm"])

    def test_telegram_content_plan_uses_v2_tool_surface_and_keeps_actions(self):
        text = (MATERIALS / "контент-план-тг.html").read_text(encoding="utf-8")
        self.assertIn("../assets/nozza-design-v2.css", text)
        self.assertIn('<body class="pf-utility">', text)
        self.assertIn("background: #1D161F", text)
        self.assertIn(".plan-controls, .day, .block { background: #2C232E", text)
        self.assertIn("@media (max-width: 640px)", text)
        self.assertIn("@media (prefers-reduced-motion: reduce)", text)
        for action in ("btnShuffleAll", "btnCopyAll", "btnMd", "btnReset", "data-copy", "data-shuffle", "data-original"):
            with self.subTest(action=action):
                self.assertIn(action, text)
        self.assertIn("const LS = 'tgplan'", text)
        self.assertIn("NZ.pageStateSave({ [LS]: state })", text)

    def test_social_header_generator_uses_v2_shell_without_changing_png_exports(self):
        text = (MATERIALS / "шапки-соцсетей.html").read_text(encoding="utf-8")
        self.assertIn("../assets/nozza-design-v2.css", text)
        self.assertIn('<body class="pf-utility">', text)
        self.assertIn("#1D161F", text)
        self.assertIn("linear-gradient(145deg, #302832, #211B25)", text)
        self.assertIn("@media (max-width: 640px)", text)
        self.assertIn("@media (prefers-reduced-motion: reduce)", text)
        for export in ('width="512" height="512"', 'width="1280" height="640"', 'width="1200" height="400"'):
            with self.subTest(export=export):
                self.assertIn(export, text)
        for action in ("toDataURL('image/png')", "data-dl", "function avatar(", "function tgCover(", "function avitoCover(", "function saveContact("):
            with self.subTest(action=action):
                self.assertIn(action, text)

    def test_about_and_ai_prompt_uses_v2_tool_surface_and_keeps_outputs(self):
        text = (MATERIALS / "о-нас-и-промпт-для-ии.html").read_text(encoding="utf-8")
        self.assertIn("../assets/nozza-design-v2.css", text)
        self.assertIn('<body class="pf-utility">', text)
        self.assertIn("#1D161F", text)
        self.assertIn("linear-gradient(145deg,#302832,#211B25)", text)
        self.assertIn("@media(max-width:640px)", text)
        self.assertIn("@media(prefers-reduced-motion:reduce)", text)
        for field in ("brand", "tagline", "what", "audience", "offers", "materials", "leadtime", "limits", "process", "tone", "rules"):
            with self.subTest(field=field):
                self.assertIn(f'data-page="{field}"', text)
        for action in ("id=\"copy\"", "id=\"copyAbout\"", "id=\"download\"", "NZ.mount(build)", "NZ.contact()"):
            with self.subTest(action=action):
                self.assertIn(action, text)

    def test_certificate_coupon_editor_uses_v2_screen_shell_and_white_print_cards(self):
        text = (MATERIALS / "сертификаты-и-купоны.html").read_text(encoding="utf-8")
        self.assertIn("../assets/nozza-design-v2.css", text)
        self.assertIn('<body class="pf-utility">', text)
        self.assertIn("@media screen", text)
        self.assertIn("#1D161F", text)
        self.assertIn("#2C232E", text)
        self.assertIn(".layout-card { background:#fff; color:#31242e; }", text)
        self.assertIn("@media print { .layout-card { outline:none !important; } }", text)
        self.assertIn("data-nz-print disabled", text)
        self.assertIn("const copies = Math.min(8, Math.max(1, Number($('k_count').value) || 8))", text)
        self.assertIn("$('c_qr').checked ? qrState : 'off'", text)
        self.assertIn("$('k_qr').checked ? qrState : 'off'", text)
        self.assertIn("printLayouts?.documents?.find((item) => item.id === id)", text)
        self.assertIn("overflow", text)

    def test_promo_stand_editor_uses_v2_screen_shell_and_white_recipe_cards(self):
        text = (MATERIALS / "промостенды-67x57.html").read_text(encoding="utf-8")
        self.assertIn("../assets/nozza-design-v2.css", text)
        self.assertIn('<body class="pf-utility">', text)
        self.assertIn("@media screen", text)
        self.assertIn("@media (prefers-reduced-motion:reduce)", text)
        self.assertIn("body.pf-utility .ideas", text)
        self.assertIn(".stand.v2 { background:#fff; color:#31242e; }", text)
        self.assertIn("promo-offer", text)
        self.assertIn("promo-photo", text)
        self.assertIn('id="btnPrint" disabled', text)
        for action in ('id="shuffle"', 'id="addAbout"', 'id="copyIdeas"', 'id="promoVariant"', 'id="theme"', 'id="qrOn"'):
            with self.subTest(action=action):
                self.assertIn(action, text)

    def test_qr_links_not_hardcoded_domain(self):
        """Домен не куплен: новые конструкторы не зашивают nozza.ru в QR."""
        for name in ("наклейки-и-бирки.html", "визитки-и-таблички.html"):
            with self.subTest(file=name):
                text = (MATERIALS / name).read_text(encoding="utf-8")
                self.assertNotIn("nozza.ru/p/", text)


if __name__ == "__main__":
    unittest.main()

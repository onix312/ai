"""Проверки структуры основного HTML-интерфейса."""
from html.parser import HTMLParser
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = ROOT / "site" / "index.html"
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class _StructureParser(HTMLParser):
    """Строгая проверка вложенности для используемого нами HTML."""

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, dict[str, str | None]]] = []
        self.errors: list[str] = []
        self.view_parents: dict[str, list[str]] = {}
        self.report_parents: dict[str, list[tuple[str, str]]] = {}
        self.ids: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id") or ""
        classes = set((attributes.get("class") or "").split())
        parent_ids = [item[1].get("id") or "" for item in self.stack]

        if element_id:
            self.ids.append(element_id)

        if "view" in classes and element_id:
            self.view_parents[element_id] = parent_ids
        if element_id in {"rep_channels", "rep_expenses"}:
            self.report_parents[element_id] = [
                (item[1].get("id") or "", item[1].get("class") or "")
                for item in self.stack
            ]

        if tag not in VOID_TAGS:
            self.stack.append((tag, attributes))

    def handle_endtag(self, tag):
        if not self.stack:
            self.errors.append(f"лишний </{tag}> в строке {self.getpos()[0]}")
            return
        opened_tag, attributes = self.stack[-1]
        if opened_tag != tag:
            opened_id = attributes.get("id") or "без id"
            self.errors.append(
                f"строка {self.getpos()[0]}: </{tag}> закрывает "
                f"<{opened_tag} id={opened_id}>"
            )
            # Восстанавливаем стек, чтобы показать и последующие ошибки.
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    del self.stack[index:]
                    return
            return
        self.stack.pop()


class SiteMarkupTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = _StructureParser()
        cls.parser.feed(INDEX_HTML.read_text(encoding="utf-8"))

    def test_index_has_balanced_markup(self):
        errors = list(self.parser.errors)
        if self.parser.stack:
            errors.append(f"не закрыты теги: {self.parser.stack[-5:]}")
        self.assertEqual(errors, [])

    def test_no_duplicate_ids_across_all_pages(self):
        """id в одном документе уникальны: getElementById всегда попадает
        в нужный элемент, иначе JS читает значения из скрытых модалок."""
        for html_file in sorted((ROOT / "site").glob("*.html")) + sorted(
                (ROOT / "site").glob("materials/*.html")):
            parser = _StructureParser()
            parser.feed(html_file.read_text(encoding="utf-8"))
            dupes = sorted({i for i in parser.ids if parser.ids.count(i) > 1})
            with self.subTest(page=html_file.name):
                self.assertEqual(dupes, [])

    def test_every_view_stays_inside_main_views_container(self):
        self.assertGreaterEqual(len(self.parser.view_parents), 12)
        for view_id, parent_ids in self.parser.view_parents.items():
            with self.subTest(view=view_id):
                self.assertIn("main", parent_ids)
                self.assertIn("views", parent_ids)

    def test_order_card_has_waybill_documents_block(self):
        self.assertIn("of_docs_wrap", self.parser.ids)
        self.assertIn("of_docs_print", self.parser.ids)
        self.assertIn("of_docs_list", self.parser.ids)
        self.assertIn("order_create_waybill", self.parser.ids)

    def test_recalc_and_waybill_scripts_are_wired(self):
        products = (ROOT / "site" / "assets" / "products.js").read_text(encoding="utf-8")
        ops = (ROOT / "site" / "assets" / "ops.js").read_text(encoding="utf-8")
        self.assertIn("function nomRecalcId", products)
        self.assertIn("[object ", products)
        self.assertIn("recalcNomPrices()", products)
        self.assertIn("/api/nomenclature/recalc-price", products)
        self.assertIn("function createOrderWaybill", ops)
        self.assertIn("/api/order/waybill", ops)
        self.assertIn("/api/order/documents", ops)
        self.assertIn("/api/b2b/doc", ops)

    def test_finance_report_cards_stay_in_report_grid(self):
        for element_id in ("rep_channels", "rep_expenses"):
            with self.subTest(element=element_id):
                parents = self.parser.report_parents[element_id]
                self.assertTrue(any(parent_id == "finpane-reports" for parent_id, _ in parents))
                self.assertTrue(any("grid" in classes.split() for _, classes in parents))

    def test_printers_park_and_orders_tg_markup_12_2(self):
        """12.2: лента парка вместо вкладок, TG-контур карточки, фильтр каналов."""
        for element_id in ("pr_park", "pr_density", "pr_layers_fill", "pr_nozzle_bar",
                           "pr_bed_spark", "pr_health_card", "orders_chan",
                           "of_cancel_wrap", "of_tg_wrap", "of_tg_msgs", "of_tg_chips",
                           "of_tg_reply", "of_tg_send", "of_photo_files"):
            with self.subTest(element=element_id):
                self.assertIn(element_id, self.parser.ids)
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertNotIn('id="pr_tabs"', html)      # вкладки-кнопки заменены парком
        self.assertIn('class="printer-park"', html)
        self.assertIn('class="ams-rack" id="pr_ams"', html)

    def test_printers_and_orders_scripts_are_wired_12_2(self):
        printer = (ROOT / "site" / "assets" / "printer.js").read_text(encoding="utf-8")
        ops = (ROOT / "site" / "assets" / "ops.js").read_text(encoding="utf-8")
        core = (ROOT / "site" / "assets" / "core.js").read_text(encoding="utf-8")
        icons = (ROOT / "site" / "assets" / "icons.js").read_text(encoding="utf-8")
        # принтеры: парк, приборы, AMS-трубки, плотность
        self.assertIn("function renderTabs", printer)
        self.assertIn("$('pr_park')", printer)
        self.assertIn("ams-tube", printer)
        self.assertIn("renderTempGauge", printer)
        self.assertIn("sparkPath", printer)
        self.assertIn("pk-card", printer)
        self.assertIn("pf_printers_density", printer)
        # заказы: TG-нить, отмена, фото/файлы, фильтр канала
        self.assertIn("/api/client-bot/order-thread", ops)
        self.assertIn("/api/client-bot/cancel-ack", ops)
        self.assertIn("/api/client-bot/review/reply", ops)
        self.assertIn("/api/client-bot/payment", ops)
        self.assertIn("/api/order/photo/to-uploads", ops)
        self.assertIn("loadOrderPhotosFull", ops)
        self.assertIn("isTgOrder", ops)
        self.assertIn("orders_chan", ops)
        # ядро: единый просмотрщик; иконки: набор 12.2
        self.assertIn("function lightbox", core)
        self.assertIn("lightbox, lightboxClose", core)
        for name in ("telegram", "bolt", "cancel", "thermo", "wind", "wifi",
                     "shield", "timer", "cube", "image", "link", "drop",
                     "message", "star"):
            with self.subTest(icon=name):
                self.assertIn(f"{name}:", icons)

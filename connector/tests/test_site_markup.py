"""Проверки структуры основного HTML-интерфейса."""
import re
from html.parser import HTMLParser
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = ROOT / "site" / "index.html"
CASHIER_HTML = ROOT / "site" / "cashier.html"
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

    def test_cashier_has_balanced_markup(self):
        parser = _StructureParser()
        parser.feed(CASHIER_HTML.read_text(encoding="utf-8"))
        errors = list(parser.errors)
        if parser.stack:
            errors.append(f"не закрыты теги: {parser.stack[-5:]}")
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

    def test_bedmap_copy_has_no_typo(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn("задний-правый", html)
        self.assertNotIn("задний-правий", html)


class CashierLayoutTests(TestCase):
    """Адаптив кассы (17.0.11): QR от свободной области, safe-area, зоны ≥ 48px.

    Сверяем не «на глаз», а по строкам CSS внутри cashier.html — иначе регрессия
    в вёрстке приедет на телефон кассира и заказчик решит, что адаптации нет.
    """

    @classmethod
    def setUpClass(cls):
        cls.css = CASHIER_HTML.read_text(encoding="utf-8")

    def _rule(self, selector):
        m = re.search(re.escape(selector) + r"\{[^}]*\}", self.css)
        self.assertIsNotNone(m, f"правило {selector!r} не найдено")
        return m.group(0)

    def test_qr_sizes_from_free_area_not_just_width(self):
        # min(≈340px, 78vw, 58vh, 100%) — ширина и высота экрана, не только ширина
        self.assertIn(".qrbox svg{width:min(360px,78vw,58vh,100%)", self.css)

    def test_sheet_scrolls_on_every_screen(self):
        rule = self._rule(".sheet")
        self.assertIn("max-height", rule)
        self.assertIn("overflow:auto", rule)
        self.assertIn("100dvh", rule)

    def test_safe_area_top_is_handled(self):
        # шапка и модалка не прячутся под вырез/статусбар в PWA edge-to-edge
        self.assertIn("env(safe-area-inset-top)", self.css)

    def test_landscape_is_handled(self):
        self.assertIn("@media(orientation:landscape)", self.css)

    def test_touch_targets_are_at_least_48px(self):
        # зоны нажатия — 48px; `.n` (min-height:38px) — это текст названия
        # товара, а не кнопка, поэтому его в проверку не берём
        self.assertIn(".iconbtn{width:48px;height:48px", self.css)
        self.assertIn(".qbtn{width:48px;height:48px", self.css)
        self.assertIn("min-height:48px", self._rule(".tab"))
        self.assertIn("min-height:48px", self._rule(".cat"))
        self.assertIn("min-height:48px", self._rule(".btn.sm"))
        self.assertNotIn("min-height:42px", self.css)

    def test_shell_cache_was_bumped_for_this_round(self):
        sw = (ROOT / "site" / "sw.js").read_text(encoding="utf-8")
        m = re.search(r"const CACHE = 'printflow-shell-v(\d+)';", sw)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 38,
                                "правка cashier.html требует поднятия CACHE в sw.js")

    # --- 17.0.12: визуал и «скорость продажи» -----------------------------

    def test_no_tofu_symbols_on_old_android(self):
        # ⎋ (U+238B), ⌕ (U+2315) и 🧾 (U+1F9FE) — «квадратики» на Android 7–8
        for char in ("⎋", "⌕", "🧾"):
            self.assertNotIn(char, self.css, f"символ {char!r} даёт tofu на minSdk 24")

    def test_cart_sum_is_the_hero(self):
        # сумма крупнее и жирнее остального в корзине
        self.assertIn(".cart .sum b{font-size:25px;font-weight:900", self.css)

    def test_cart_shadow_and_row_focus(self):
        self.assertIn("box-shadow:0 -10px 28px -16px", self.css)
        self.assertIn(".row:hover,.row:focus-within", self.css)

    def test_skeleton_and_thin_scrollbar(self):
        self.assertIn(".skel", self.css)
        self.assertIn("@keyframes skelpulse", self.css)
        self.assertIn("scrollbar-width:thin", self.css)

    def test_focus_ring_covers_tiles(self):
        self.assertIn(".tile:focus-visible", self.css)

    def test_speed_of_sale_wiring(self):
        # штрихкод добавляется без Enter, «Повторить», «F» в поиск, индикатор связи
        self.assertIn("function autoAddExact", self.css)
        self.assertIn("function rememberLastSale", self.css)
        self.assertIn('id="bRepeat"', self.css)
        self.assertIn("function netState", self.css)
        self.assertIn('id="netDot"', self.css)

    def test_shift_quality_of_life(self):
        # живое «должно/факт», пресеты причин, фильтр журнала, замок выемки
        self.assertIn("function bindShiftDiff", self.css)
        self.assertIn("function notePresets", self.css)
        self.assertIn("function shiftFilterChips", self.css)
        self.assertIn("function applyShiftFilter", self.css)
        self.assertIn("🔒 Забрать из ящика", self.css)
        self.assertIn("Только старший", self.css)

    def test_offline_and_qr_quality_of_life(self):
        self.assertIn("function offAge", self.css)
        self.assertIn("function closeQrModal", self.css)
        self.assertIn("#bQrPaid{min-height:56px", self.css)

    # --- 17.0.13: надёжность канала «касса ↔ ПК» -------------------------

    def test_link_status_shows_pc_reachability_not_just_wifi(self):
        # точка в шапке отражает ответ коннектора, а не navigator.onLine
        self.assertIn('id="linkBar"', self.css)
        self.assertIn('id="linkText"', self.css)
        self.assertIn(".netdot.warn", self.css)
        self.assertIn("function netStreamAlive", self.css)
        self.assertIn("function netProbe", self.css)
        self.assertIn("function netStart", self.css)

    def test_requests_have_timeout_and_backoff(self):
        # без таймаута «мёртвый» Wi-Fi держал кассу в «Оплачиваю…» минутами
        self.assertIn("AbortController", self.css)
        self.assertIn("var API_TIMEOUT=8000", self.css)
        self.assertIn("PROBE_MIN=1000", self.css)
        self.assertIn("PROBE_MAX=15000", self.css)

    def test_offline_sale_keeps_the_same_request_id(self):
        # риск F1: очередь обязана повторить уже отправленный номер, иначе
        # сервер запишет вторую продажу (проверено на живом коннекторе)
        self.assertIn("function offAccept(method,requestId,pending)", self.css)
        self.assertIn('offAccept("cash",e.request_id,true)', self.css)
        self.assertIn("n.request_id=(body&&body.request_id)", self.css)

    def test_offline_queue_flushes_by_timer_with_progress(self):
        self.assertIn("OFF_GAP=20000", self.css)
        self.assertIn('id="offProgress"', self.css)
        self.assertIn("offProg", self.css)
        self.assertIn("offFlush(false)", self.css)
        self.assertIn("offFlush(true)", self.css)

    def test_prices_are_checked_before_payment(self):
        # сервер считает по своей цене: расхождение с экраном — предупредить
        self.assertIn("function priceDiffs", self.css)
        self.assertIn("function checkPrices", self.css)
        self.assertIn('id="priceWarn"', self.css)
        self.assertIn('id="priceWarnFix"', self.css)

    def test_catalog_changed_event_makes_the_cashier_reread(self):
        self.assertIn('addEventListener("catalog_changed"', self.css)
        self.assertIn("function onCatalogChanged", self.css)

    def test_offline_qr_ttl_is_visible(self):
        self.assertIn("function offQrState", self.css)
        self.assertIn("OFF_QR_TTL", self.css)

    def test_shell_cache_was_bumped_for_the_channel_round(self):
        sw = (ROOT / "site" / "sw.js").read_text(encoding="utf-8")
        m = re.search(r"const CACHE = 'printflow-shell-v(\d+)';", sw)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 42,
                                "правка cashier.html требует поднятия CACHE в sw.js")

    def test_update_banner_shows_size_hash_and_changelog(self):
        # «скачать APK» без размера и отпечатка — установка вслепую: в плашке
        # браузера видны версия, размер, sha256 и что нового (17.0.13).
        self.assertIn("r.sha256", self.css)
        self.assertIn("r.changelog", self.css)
        self.assertIn("Сверьте размер файла", self.css)
        self.assertIn(".install .upd", self.css)

    def test_offline_queue_is_backed_up_into_the_shell(self):
        # смена IP = другой origin: localStorage страницы пуст, очередь спасает
        # копия в памяти оболочки (PfApp.queueSave/queueLoad)
        self.assertIn("app.queueSave", self.css)
        self.assertIn("app.queueLoad", self.css)
        self.assertIn("function offStash", self.css)
        self.assertIn("function offRestore", self.css)
        self.assertIn("offRestore()", self.css)

    def test_stream_ping_is_observable_for_the_cashier(self):
        # по именованному кадру ping касса отличает «поток жив» от «молчит»
        self.assertIn('addEventListener("ping"', self.css)
        self.assertIn("LINK.stream", self.css)

    def test_cashier_uses_svg_icons_from_registry(self):
        # эмодзи заменены на PFIcons: касса подключает icons.js и ссылается
        # только на существующие имена (иначе fallback-глиф, а не SVG)
        self.assertIn("/assets/icons.js", self.css)
        registry = (ROOT / "site" / "assets" / "icons.js").read_text(encoding="utf-8")
        used = sorted(set(re.findall(r'data-icon="([a-z0-9_-]+)"', self.css)))
        self.assertTrue(used, "касса должна использовать data-icon")
        missing = [name for name in used
                   if not re.search(r"^\s+" + re.escape(name) + r"\s*:\s*'", registry, re.M)]
        self.assertEqual(missing, [], f"иконки не в реестре icons.js: {missing}")

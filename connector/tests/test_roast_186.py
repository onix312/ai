"""Прожарка остальных систем (18.6): деньги/касса → заказы → бот → страницы.

Каждый принятый инвариант — тестом (таблица — в docs/ОТЧЁТ-18.6.0.md):

* возврат продажи со скидкой возвращает уплаченное, а не каталожное;
* частичный возврат сверх остатка строки отклоняется словами;
* двойной клик «Сохранить» в заказе не плодит дублей (ключ + глушение кнопок);
* подтверждение корзины в боте называет вариант и цену;
* страницы СБП/банка: пины ведут на существующие файлы, ошибка сети видна.
"""
from __future__ import annotations

import pathlib
import re
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.cashier import Cashier  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402
from connector.printflow.shelf import Shelf  # noqa: E402

_held: list = []


def make_db(name: str = "roast.sqlite3") -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / name)


def add_item(db: Database, item_id: str = "s1", qty: float = 10,
             price: float = 500) -> None:
    db.upsert("shelf_items", {
        "id": item_id, "name": "Адресник", "qty": qty, "price": price,
        "cost_per_unit": 120, "active": 1})


class RoastCashierBase(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc, Shelf(self.db))
        self.db.set_settings({"cashier_code": "1234"})
        add_item(self.db)
        self.token = self.cashier.login("1234")["token"]


class DiscountedReturnTests(RoastCashierBase):
    def test_discounted_sale_returns_paid_not_list(self):
        sale = self.cashier.sell([{"item_id": "s1", "qty": 2}], "cash",
                                 self.token, discount_pct=10)
        self.assertEqual(900, sale["amount"])
        out = self.cashier.return_sale(sale["sale_id"], self.token, note="брак")
        self.assertEqual(900.0, out["returned"])
        tx = self.db.one("SELECT amount FROM transactions"
                         " WHERE kind='expense' AND category='refund'")
        self.assertEqual(900.0, tx["amount"])
        sale_row = self.db.one("SELECT refunded_amount FROM cashier_sales WHERE id=?",
                               (sale["sale_id"],))
        self.assertEqual(900.0, sale_row["refunded_amount"])

    def test_single_method_only(self):
        with self.assertRaisesRegex(ValueError, "Способ оплаты"):
            self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash+card", self.token)


class PartialOverlapReturnTests(RoastCashierBase):
    def test_second_return_beyond_remainder_is_refused(self):
        sale = self.cashier.sell([{"item_id": "s1", "qty": 5}], "cash", self.token)
        move_id = str(sale["items"][0]["move_id"])
        self.cashier.return_sale(sale["sale_id"], self.token,
                                 lines=[{"move_id": move_id, "qty": 4}])
        with self.assertRaisesRegex(ValueError, "нельзя"):
            self.cashier.return_sale(sale["sale_id"], self.token,
                                     lines=[{"move_id": move_id, "qty": 2}])
        # Отказ не списал ничего лишнего: на полке 10 - 5 + 4 = 9.
        qty = float(self.db.one("SELECT qty FROM shelf_items WHERE id='s1'")["qty"])
        self.assertEqual(9.0, round(qty, 2))

    def test_parallel_guard_message_exists(self):
        import inspect
        from connector.printflow import shelf as shelf_mod
        src = inspect.getsource(shelf_mod.Shelf.return_stock)
        self.assertIn("уже возвращают", src)


class OrderDoubleClickTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db("roast_orders.sqlite3")
        self.addCleanup(self.db.close)
        self.repo = Repo(self.db)

    def test_same_request_key_returns_one_order(self):
        first = self.repo.save_order({"product": "Брелок", "status": "new",
                                      "client_request_id": "panel-order-1"})
        second = self.repo.save_order({"product": "Брелок", "status": "new",
                                       "client_request_id": "panel-order-1"})
        self.assertEqual(first["id"], second["id"])
        count = self.db.one("SELECT COUNT(*) n FROM orders")["n"]
        self.assertEqual(1, count)

    def test_different_keys_make_different_orders(self):
        first = self.repo.save_order({"product": "Брелок", "status": "new",
                                      "client_request_id": "panel-order-1"})
        second = self.repo.save_order({"product": "Брелок", "status": "new",
                                       "client_request_id": "panel-order-2"})
        self.assertNotEqual(first["id"], second["id"])

    def test_panel_sends_key_and_blocks_buttons(self):
        src = (ROOT / "site" / "assets" / "ops.js").read_text(encoding="utf-8")
        self.assertIn("payload.client_request_id = orderRequestKey", src)
        self.assertIn("panel-order-${crypto.randomUUID()}", src)
        self.assertIn("'order_save', 'order_save_prepare', 'order_queue'", src)
        self.assertIn("b.disabled = true", src)


class OrderVariantStructureTests(unittest.TestCase):
    """Заказ на вариацию с составом получает катушки на списание (М2, 18.5).

    Покрытия не было: правило жило только в коде Api.save_order. Теперь
    инвариант прибит: spools JSON пропорционален количеству заказа, а явно
    указанные катушки составом не затираются.
    """

    def setUp(self):
        import uuid
        from connector.printflow.api import Api
        from connector.printflow.nomenclature import Nomenclature
        from connector.printflow.stock import Stock
        self.db = make_db("roast_struct.sqlite3")
        self.addCleanup(self.db.close)
        self.nom = Nomenclature(self.db)
        item = self.nom.save({"name": "Брелок", "kind": "product", "unit": "шт",
                              "material": "PLA", "grams": 8, "hours": 0.5,
                              "fit_per_plate": 6})
        self.nom.save_variant({"id": "vs1", "nom_id": item["id"], "name": "Двухцвет"})
        self.red = "sp-" + uuid.uuid4().hex[:10]
        self.white = "sp-" + uuid.uuid4().hex[:10]
        for sid, color in ((self.red, "Красный"), (self.white, "Белый")):
            self.db.upsert("spools", {
                "id": sid, "material": "PLA", "color_name": color,
                "price": 1600, "total_grams": 1000, "remaining_grams": 900})
        self.nom.save_variant_structures("vs1", [
            {"spool_id": self.red, "grams": 6},
            {"spool_id": self.white, "grams": 2}])
        self.api = Api.__new__(Api)
        self.api.db = self.db
        self.api.nom = self.nom
        self.api.repo = Repo(self.db)
        self.api.stock = Stock(self.db)

    def test_structures_become_order_spools(self):
        import json
        out = self.api.save_order({"product": "Брелок двухцвет", "status": "new",
                                   "qty": 3, "client_variant_id": "vs1"})
        spools = json.loads(out["order"]["spools"])
        self.assertEqual(2, len(spools))
        by_id = {s["spool_id"]: s["grams"] for s in spools}
        self.assertEqual(18.0, by_id[self.red])
        self.assertEqual(6.0, by_id[self.white])

    def test_explicit_spools_win_over_structures(self):
        import json
        explicit = [{"spool_id": self.red, "grams": 5}]
        out = self.api.save_order({"product": "Брелок", "status": "new",
                                   "qty": 2, "client_variant_id": "vs1",
                                   "spools": explicit})
        self.assertEqual(explicit, json.loads(out["order"]["spools"]))


class BotVariantCartTests(unittest.TestCase):
    def setUp(self):
        from connector.printflow.manager import PrinterManager
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(pathlib.Path(self._tmp.name) / "roast_bot.sqlite3")
        self.addCleanup(self.db.close)
        self.manager = PrinterManager(self.db, Repo(self.db))
        self.addCleanup(self.manager.shutdown)
        self.bot = self.manager.client_bot
        self.assertIsNotNone(self.bot)
        self.db.upsert("nomenclature", {
            "id": "a", "kind": "product", "name": "Адресник",
            "grams": 10, "hours": 0.5, "material": "PLA"})
        from connector.printflow.nomenclature import Nomenclature
        Nomenclature(self.db).set_price("a", 350)
        self.db.upsert("nom_variants", {
            "id": "v-red", "nom_id": "a", "name": "Красный / L",
            "color_name": "Красный", "size": "L"})

    def test_confirmation_names_variant_and_price(self):
        answer = self.bot._add_to_cart("555", "a", "v-red")
        self.assertIn("Красный / L", answer)
        self.assertIn("350", answer)

    def test_plain_product_confirmation_unchanged(self):
        answer = self.bot._add_to_cart("555", "a")
        self.assertIn("Адресник", answer)
        self.assertNotIn("—", answer)


class ShowcasePagesTests(unittest.TestCase):
    PAGES = ("sbp.html", "bank.html")

    def test_pins_point_to_existing_files(self):
        for page in self.PAGES:
            html = (ROOT / "site" / page).read_text(encoding="utf-8")
            for ref in re.findall(r"(?:src|href)=\"(/assets/[^\"]+?)\?v=", html):
                target = ROOT / "site" / ref.lstrip("/")
                self.assertTrue(target.is_file(), f"{page}: нет файла {ref}")
            for ref in re.findall(r"(?:src|href)=\"(assets/[^\"]+?)\?v=", html):
                target = ROOT / "site" / ref
                self.assertTrue(target.is_file(), f"{page}: нет файла {ref}")

    def test_network_error_is_visible(self):
        for page in self.PAGES:
            html = (ROOT / "site" / page).read_text(encoding="utf-8")
            self.assertIn("toast(\"Ошибка: \"", html, f"{page}: ошибка сети молчит")

    def test_pages_are_precached(self):
        sw = (ROOT / "site" / "sw.js").read_text(encoding="utf-8")
        for page in self.PAGES:
            self.assertIn(f"/{page}", sw, f"sw.js не кеширует {page}")


if __name__ == "__main__":
    unittest.main()

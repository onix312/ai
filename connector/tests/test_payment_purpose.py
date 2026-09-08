"""Назначение платежа из товаров + диагностика QR (Склад+СБП 18.0, итерация 1).

Проверяем ТЗ §25 «Тесты назначения» (20 сценариев): серверное построение
из состава, санитизация, лимит банка, идемпотентность, неизменяемость после
переименования, состав в журнале — и диагностику QR (открытие банка,
причина «почему текст»).
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from urllib.parse import unquote

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import payment_qr  # noqa: E402
from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.cashier import Cashier  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.payment_purpose import build, format_qty, plural, sanitize  # noqa: E402
from connector.printflow.sbp import Sbp  # noqa: E402

_held: list = []

GOOD = {
    "pay_payee_name": "ИП Иванов Иван Иванович",
    "pay_account": "40802810900000012345",
    "pay_bank_name": "ПАО СБЕРБАНК",
    "pay_bic": "044525225",
    "pay_corr_account": "30101810400000000225",
    "pay_payee_inn": "771234567890",
}


def make_db(**settings) -> Database:
    _held.append(tempfile.TemporaryDirectory())
    db = Database(pathlib.Path(_held[-1].name) / "purpose.sqlite3")
    if settings:
        db.set_settings(settings)
    return db


def add_shelf(db: Database, item_id: str, name: str,
              qty: float = 10, price: float = 500) -> None:
    db.upsert("shelf_items", {
        "id": item_id, "name": name, "qty": qty, "price": price,
        "cost_per_unit": 120, "active": 1})


def add_order(db: Database, **over) -> dict:
    data = {"id": "o1", "number": "1001", "product": "Адресник",
            "customer_name": "Мария", "status": "done", "price": 1000,
            "paid": 0, "created_at": "2026-09-06T10:00:00+03:00",
            "updated_at": "2026-09-06T10:00:00+03:00"}
    data.update(over)
    return db.upsert("orders", data)


class BuildTests(unittest.TestCase):
    def test_single_item_single_qty(self):
        self.assertEqual(build([{"name": "Органайзер настольный", "qty": 1}], "NOZZA"),
                         "NOZZA: Органайзер настольный × 1")

    def test_single_item_many_qty(self):
        self.assertEqual(build([{"name": "Адресник для собаки", "qty": 3}], "NOZZA"),
                         "NOZZA: Адресник для собаки × 3")

    def test_acceptance_basket(self):
        items = [{"name": "Органайзер", "qty": 1},
                 {"name": "Адресник", "qty": 2},
                 {"name": "Брелок", "qty": 1}]
        self.assertEqual(
            build(items, "NOZZA"),
            "NOZZA: Органайзер × 1; Адресник × 2; Брелок × 1")

    def test_ref_included(self):
        items = [{"name": "Органайзер", "qty": 1}]
        self.assertEqual(build(items, "NOZZA", "CS-1042"),
                         "NOZZA CS-1042: Органайзер × 1")

    def test_cyrillic_and_unicode_survive(self):
        out = build([{"name": "Брелок NOZZA 🐾 «Тузик»", "qty": 1}], "NOZZA")
        self.assertIn("Брелок NOZZA 🐾 «Тузик» × 1", out)

    def test_quotes_and_specials_stay_text(self):
        out = build([{"name": 'a&b"c<d>e', "qty": 1}], "NOZZA", limit=140)
        self.assertIn('a&b"c<d>e × 1', out)

    def test_controls_and_newlines_removed(self):
        out = build([{"name": "А\nБ\r\nВ\x00Г\x1fД\tЕ", "qty": 1}], "NOZZA")
        self.assertEqual(out, "NOZZA: А Б ВГД Е × 1")
        self.assertNotIn("\n", out)

    def test_spaces_normalized(self):
        out = build([{"name": "  Органайзер    большой  ", "qty": 1}], "NOZZA")
        self.assertEqual(out, "NOZZA: Органайзер большой × 1")

    def test_empty_names_skipped(self):
        out = build([{"name": "  ", "qty": 1}, {"name": "", "qty": 2},
                     {"name": "Адресник", "qty": 1}], "NOZZA")
        self.assertEqual(out, "NOZZA: Адресник × 1")

    def test_empty_basket_returns_head(self):
        self.assertEqual(build([], "NOZZA", "№5"), "NOZZA №5")
        self.assertEqual(build([], "", ""), "Оплата")

    def test_bank_limit_respected(self):
        items = [{"name": f"Товар номер {i} с длинным названием", "qty": i + 1}
                 for i in range(12)]
        out = build(items, "NOZZA", limit=140)
        self.assertLessEqual(len(out), 140)
        self.assertIn("+", out)
        self.assertTrue(out.rstrip().endswith(("товар", "товара", "товаров")))

    def test_never_cuts_mid_item(self):
        items = [{"name": "Органайзер", "qty": 1},
                 {"name": "Адресник", "qty": 2}]
        out = build(items, "NOZZA", limit=32)
        # Первая позиция целиком + счётчик, «Адресн…» недопустим.
        self.assertEqual(out, "NOZZA: Органайзер × 1; +1 товар")

    def test_single_long_item_cut_by_words(self):
        out = build([{"name": "Очень длинное название товара для проверки", "qty": 1}],
                    "NOZZA", limit=30)
        self.assertLessEqual(len(out), 30)
        self.assertTrue(out.endswith("…"))
        self.assertNotIn("…", out[:-1])

    def test_plural_forms(self):
        self.assertEqual(plural(1, "товар", "товара", "товаров"), "товар")
        self.assertEqual(plural(2, "товар", "товара", "товаров"), "товара")
        self.assertEqual(plural(4, "товар", "товара", "товаров"), "товара")
        self.assertEqual(plural(5, "товар", "товара", "товаров"), "товаров")
        self.assertEqual(plural(11, "товар", "товара", "товаров"), "товаров")
        self.assertEqual(plural(21, "товар", "товара", "товаров"), "товар")

    def test_qty_formatting(self):
        self.assertEqual(format_qty(1), "1")
        self.assertEqual(format_qty(2.0), "2")
        self.assertEqual(format_qty(1.5), "1.5")

    def test_sanitize_caps_by_chars_not_bytes(self):
        self.assertEqual(sanitize("Привет мир", 6), "Привет")

    def test_client_extra_keys_ignored(self):
        out = build([{"name": "Адресник", "qty": 1, "phone": "+7",
                      "token": "secret", "note": "позвонить"}], "NOZZA")
        self.assertEqual(out, "NOZZA: Адресник × 1")


class CashierPurposeTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db(cashier_code="1234", **GOOD)
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc)
        add_shelf(self.db, "s1", "Органайзер", price=700)
        add_shelf(self.db, "s2", "Адресник", price=500)
        add_shelf(self.db, "s3", "Брелок", price=300)
        self.token = self.cashier.login("1234")["token"]

    def tearDown(self):
        self.db.close()

    def test_sale_purpose_lists_goods(self):
        sale = self.cashier.sell(
            [{"item_id": "s1", "qty": 1}, {"item_id": "s2", "qty": 2},
             {"item_id": "s3", "qty": 1}], "sbp", self.token,
            request_id="r-accept")
        self.assertEqual(
            sale["payment"]["purpose"],
            "NOZZA: Органайзер × 1; Адресник × 2; Брелок × 1")

    def test_client_names_are_ignored(self):
        sale = self.cashier.sell(
            [{"item_id": "s1", "qty": 1, "name": "ПОДДЕЛКА"}], "sbp",
            self.token, request_id="r-fake")
        self.assertIn("Органайзер", sale["payment"]["purpose"])
        self.assertNotIn("ПОДДЕЛКА", sale["payment"]["purpose"])

    def test_repeat_returns_same_purpose_and_items(self):
        first = self.cashier.sell([{"item_id": "s1", "qty": 1}], "sbp",
                                  self.token, request_id="r-dup")
        second = self.cashier.sell([{"item_id": "s1", "qty": 1}], "sbp",
                                   self.token, request_id="r-dup")
        self.assertTrue(second.get("already_recorded"))
        self.assertEqual(second["payment"]["purpose"], first["payment"]["purpose"])
        self.assertEqual(second["payment"]["items"], first["payment"]["items"])
        self.assertEqual(second["qr"]["text"], first["qr"]["text"])

    def test_full_composition_stored(self):
        sale = self.cashier.sell(
            [{"item_id": "s1", "qty": 1}, {"item_id": "s2", "qty": 2}],
            "sbp", self.token, request_id="r-items")
        stored = json.loads(sale["payment"]["items"])
        self.assertEqual([(r["name"], r["qty"]) for r in stored],
                         [("Органайзер", 1), ("Адресник", 2)])

    def test_rename_does_not_change_old_purpose(self):
        sale = self.cashier.sell([{"item_id": "s1", "qty": 1}], "sbp",
                                  self.token, request_id="r-rename")
        pid = sale["payment"]["id"]
        before = sale["payment"]["purpose"]
        self.db.execute("UPDATE shelf_items SET name=? WHERE id='s1'",
                        ("Органайзер ПЕРЕИМЕНОВАН",))
        again = self.db.one("SELECT * FROM sbp_payments WHERE id=?", (pid,))
        self.assertEqual(again["purpose"], before)

    def test_purpose_reaches_qr_and_incoming(self):
        sale = self.cashier.sell([{"item_id": "s2", "qty": 2}], "sbp",
                                  self.token, request_id="r-qr")
        self.assertEqual(sale["qr"]["purpose"], sale["payment"]["purpose"])
        incoming = self.cashier.incoming()["payments"]
        self.assertEqual(incoming[0]["purpose"], sale["payment"]["purpose"])

    def test_purpose_limit_from_settings(self):
        self.db.set_settings({"sbp_purpose_limit": 30})
        sale = self.cashier.sell(
            [{"item_id": "s1", "qty": 1}, {"item_id": "s2", "qty": 2}],
            "sbp", self.token, request_id="r-limit")
        self.assertLessEqual(len(sale["payment"]["purpose"]), 30)
        # Состав при этом не потерян.
        stored = json.loads(sale["payment"]["items"])
        self.assertEqual(len(stored), 2)


class SbpOrderPurposeTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.sbp = Sbp(self.db, self.acc)

    def tearDown(self):
        self.db.close()

    def test_order_purpose_from_product(self):
        add_order(self.db)
        p = self.sbp.create(amount=1000, order_id="o1", request_id="r-o")
        self.assertEqual(p["purpose"], "NOZZA №1001: Адресник × 1")

    def test_order_purpose_from_positions(self):
        add_order(self.db, product="")
        self.db.upsert("order_items", {"id": "oi1", "order_id": "o1",
                                       "position": 1, "name": "Органайзер",
                                       "qty": 1, "price": 700})
        self.db.upsert("order_items", {"id": "oi2", "order_id": "o1",
                                       "position": 2, "name": "Брелок",
                                       "qty": 2, "price": 150})
        p = self.sbp.create(amount=1000, order_id="o1", request_id="r-oi")
        self.assertEqual(p["purpose"],
                         "NOZZA №1001: Органайзер × 1; Брелок × 2")

    def test_explicit_purpose_wins(self):
        add_order(self.db)
        p = self.sbp.create(amount=1000, order_id="o1", purpose="Ручное",
                            request_id="r-exp")
        self.assertEqual(p["purpose"], "Ручное")
        # Состав всё равно сохранён.
        self.assertIn("Адресник", p["items"])

    def test_template_wins_for_orders(self):
        add_order(self.db, number="2042")
        self.db.set_settings({"sbp_payment_note": "Заказ {number} · NOZZA"})
        p = self.sbp.create(amount=1000, order_id="o1", request_id="r-tpl")
        self.assertEqual(p["purpose"], "Заказ 2042 · NOZZA")

    def test_brand_from_settings(self):
        add_order(self.db)
        self.db.set_settings({"company_name": "ЛАВКА"})
        p = self.sbp.create(amount=1000, order_id="o1", request_id="r-brand")
        self.assertTrue(p["purpose"].startswith("ЛАВКА №1001:"))

    def test_items_visible_in_journal(self):
        add_order(self.db)
        p = self.sbp.create(amount=1000, order_id="o1", request_id="r-j")
        rows = self.sbp.list(limit=10)
        self.assertEqual(rows[0]["items"], p["items"])
        self.assertIn("Адресник", rows[0]["items"])


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db(**GOOD)

    def tearDown(self):
        self.db.close()

    def test_gost_has_no_open_button(self):
        qr = payment_qr.build(self.db, 1000, "NOZZA: Адресник × 1")
        self.assertEqual(qr["kind"], "gost")
        diag = qr["diagnostics"]
        self.assertFalse(diag["can_open"])
        self.assertEqual(qr["open_url"], "")
        self.assertIn("приложение банка", diag["why"])
        self.assertTrue(diag["amount_in_qr"])
        self.assertTrue(diag["purpose_in_qr"])
        self.assertFalse(diag["purpose_truncated"])
        self.assertEqual(diag["fallback"], "bank_app")

    def test_truncated_purpose_is_flagged(self):
        long = "Оплата товара " * 20
        qr = payment_qr.build(self.db, 1000, long)
        diag = qr["diagnostics"]
        self.assertTrue(diag["purpose_truncated"])
        # Полное назначение при этом не потеряно.
        self.assertEqual(qr["purpose"], long.strip())

    def test_bank_link_is_openable(self):
        self.db.set_settings({
            "pay_qr_mode": "link",
            "pay_qr_link": "https://pay.alfa-bank.ru/qr?sum={amount}&note={purpose}",
        })
        qr = payment_qr.build(self.db, 1000, "NOZZA: Адресник × 1")
        self.assertEqual(qr["kind"], "link")
        diag = qr["diagnostics"]
        self.assertTrue(diag["can_open"])
        self.assertIn("pay.alfa-bank.ru", diag["domain"])
        self.assertTrue(diag["expect_bank_chooser"])
        # Кириллица в ссылке закодирована, ссылка цела.
        self.assertIn("%D0%90", qr["text"])
        self.assertIn("NOZZA", unquote(qr["text"]))

    def test_dangerous_schemes_rejected(self):
        for bad in ("javascript:alert(1)", "data:text/plain,hi",
                    "alfa-bank://pay/123", "https://pay.ru/x y",
                    "https://user:pass@pay.ru/", "http://pay.ru/insecure"):
            self.assertEqual(payment_qr.safe_open_url(bad), "", bad)
        self.assertEqual(payment_qr.safe_open_url("https://qr.nspk.ru/AS123"),
                         "https://qr.nspk.ru/AS123")

    def test_static_qr_diagnostics(self):
        self.db.set_settings({"pay_qr_mode": "static",
                              "sbp_shop_qr": "https://qr.nspk.ru/AS100012345"})
        qr = payment_qr.build(self.db, 1000, "NOZZA: Адресник × 1")
        diag = qr["diagnostics"]
        self.assertTrue(diag["static"])
        self.assertTrue(diag["can_open"])
        self.assertEqual(diag["domain"], "qr.nspk.ru")
        self.assertIsNone(diag["purpose_in_qr"])

    def test_control_chars_stripped_from_qr(self):
        qr = payment_qr.build(self.db, 100, "А\x00Б\x1fВ")
        self.assertNotIn("\x00", qr["text"])
        self.assertEqual(qr["purpose"], "АБВ")


class MigrationTests(unittest.TestCase):
    def test_old_db_gets_items_column(self):
        folder = tempfile.TemporaryDirectory()
        _held.append(folder)
        path = pathlib.Path(folder.name) / "old-sbp.sqlite3"
        db = Database(path)
        db.close()
        import sqlite3
        connection = sqlite3.connect(path)
        connection.execute("ALTER TABLE sbp_payments DROP COLUMN items")
        connection.commit()
        connection.close()
        migrated = Database(path)
        try:
            columns = {row["name"] for row in
                       migrated.query("PRAGMA table_info(sbp_payments)")}
            self.assertIn("items", columns)
            row = migrated.one("SELECT items FROM sbp_payments LIMIT 1")
            self.assertTrue(row is None or row["items"] == "[]")
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()

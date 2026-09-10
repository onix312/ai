"""Платёжный QR PrintFlow: собственный генератор кода оплаты.

Динамический QR СБП выпускает банк-эквайер, а вот код «оплата по реквизитам»
(ГОСТ Р 56042-2014) магазин собирает сам: сумма и назначение уже внутри,
читает любое банковское приложение. Проверяем формат строки, валидацию
реквизитов, подгонку под размер QR, шаблон ссылки и приоритет режимов.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow import payment_qr  # noqa: E402

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
    db = Database(pathlib.Path(_held[-1].name) / "qr.sqlite3")
    if settings:
        db.set_settings(settings)
    return db


def fields(payload: str) -> dict[str, str]:
    out = {}
    for chunk in payload.split("|")[1:]:
        key, _, value = chunk.partition("=")
        out[key] = value
    return out


class GostPayloadTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db(**GOOD)
        self.req = payment_qr.requisites(self.db)

    def tearDown(self):
        self.db.close()

    def test_header_and_required_fields(self):
        payload = payment_qr.gost_payload(self.req, 950, "Оплата на кассе")
        self.assertTrue(payload.startswith("ST00012|"))
        got = fields(payload)
        self.assertEqual(got["Name"], "ИП Иванов Иван Иванович")
        self.assertEqual(got["PersonalAcc"], "40802810900000012345")
        self.assertEqual(got["BankName"], "ПАО СБЕРБАНК")
        self.assertEqual(got["BIC"], "044525225")
        self.assertEqual(got["CorrespAcc"], "30101810400000000225")
        self.assertEqual(got["PayeeINN"], "771234567890")

    def test_sum_is_in_kopecks(self):
        self.assertEqual(payment_qr.kopecks(950), 95000)
        self.assertEqual(payment_qr.kopecks(1234.56), 123456)
        got = fields(payment_qr.gost_payload(self.req, 1234.5, ""))
        self.assertEqual(got["Sum"], "123450")

    def test_zero_amount_leaves_sum_out(self):
        """Наклейка на кассу — без суммы: покупатель вводит её сам."""
        self.assertNotIn("Sum", fields(payment_qr.gost_payload(self.req, 0, "Оплата")))

    def test_separators_cannot_break_the_format(self):
        db = make_db(**{**GOOD, "pay_payee_name": "ИП |Иванов|  Иван"})
        payload = payment_qr.gost_payload(payment_qr.requisites(db), 10, "а|б")
        self.assertEqual(len(payload.split("|")), len(fields(payload)) + 1)
        self.assertEqual(fields(payload)["Name"], "ИП Иванов Иван")
        db.close()

    def test_long_purpose_is_trimmed_to_fit_the_qr(self):
        payload = payment_qr.gost_payload(self.req, 1000, "Оплата товара " * 20)
        self.assertLessEqual(len(payload.encode("utf-8")), payment_qr.MAX_QR_BYTES)
        self.assertTrue(fields(payload)["Purpose"].startswith("Оплата товара"))
        self.assertTrue(payment_qr.render_svg(payload).startswith("<svg"))

    def test_requisites_are_never_sacrificed(self):
        payload = payment_qr.gost_payload(self.req, 1000, "П" * 400)
        got = fields(payload)
        for key in ("Name", "PersonalAcc", "BankName", "BIC", "CorrespAcc", "Sum"):
            self.assertIn(key, got, key)


class RequisiteCheckTests(unittest.TestCase):
    def problems(self, **patch) -> dict[str, str]:
        db = make_db(**{**GOOD, **patch})
        found = {p["field"]: p["message"] for p in payment_qr.check(payment_qr.requisites(db))}
        db.close()
        return found

    def test_good_requisites_have_no_problems(self):
        self.assertEqual(self.problems(), {})

    def test_account_must_be_twenty_digits(self):
        self.assertIn("pay_account", self.problems(pay_account="4080281090"))

    def test_bic_must_be_nine_digits(self):
        self.assertIn("pay_bic", self.problems(pay_bic="04452"))

    def test_corr_account_tail_must_match_bic(self):
        found = self.problems(pay_corr_account="30101810400000000999")
        self.assertIn("pay_corr_account", found)
        self.assertIn("225", found["pay_corr_account"])

    def test_inn_length_is_checked(self):
        self.assertIn("pay_payee_inn", self.problems(pay_payee_inn="7712"))

    def test_payload_refuses_incomplete_requisites(self):
        db = make_db(pay_payee_name="ИП Иванов")
        with self.assertRaisesRegex(ValueError, "Не хватает реквизитов"):
            payment_qr.gost_payload(payment_qr.requisites(db), 100, "Оплата")
        db.close()

    def test_legal_name_and_inn_are_reused_from_documents(self):
        db = make_db(**{k: v for k, v in GOOD.items()
                        if k not in ("pay_payee_name", "pay_payee_inn")},
                     legal_name="ООО НОЗЗА", inn="7712345678")
        req = payment_qr.requisites(db)
        self.assertEqual(req["Name"], "ООО НОЗЗА")
        self.assertEqual(req["PayeeINN"], "7712345678")
        self.assertEqual(payment_qr.check(req), [])
        db.close()


class LinkTemplateTests(unittest.TestCase):
    def test_placeholders_are_substituted(self):
        link = payment_qr.link_payload(
            "https://www.tbank.ru/rm/shop/?amount={amount}&purpose={purpose}",
            1234.5, "Заказ 12")
        self.assertIn("amount=1234.5", link)
        # 18.0: назначение URL-кодируется — пробел иначе рвёт ссылку.
        self.assertIn("purpose=", link)
        self.assertNotIn("purpose=Заказ 12", link)
        from urllib.parse import unquote
        self.assertIn("purpose=Заказ 12", unquote(link))

    def test_kopecks_placeholder(self):
        self.assertIn("=95000", payment_qr.link_payload("https://pay?sum={amount_kop}", 950))

    def test_empty_template_gives_nothing(self):
        self.assertEqual(payment_qr.link_payload("", 100), "")


class BuildModeTests(unittest.TestCase):
    def test_auto_prefers_own_gost_code_over_static_qr(self):
        db = make_db(**GOOD, sbp_shop_qr="https://qr.nspk.ru/AS1000")
        built = payment_qr.build(db, 950, "Оплата на кассе")
        self.assertEqual(built["kind"], "gost")
        self.assertTrue(built["amount_in_qr"])
        self.assertTrue(built["svg"].startswith("<svg"))
        db.close()

    def test_camera_mode_puts_bank_url_first(self):
        """«QR для камеры»: обычная камера открывает ссылку, а не показывает ГОСТ-текст."""
        db = make_db(**GOOD, sbp_shop_qr="https://qr.nspk.ru/AS1000", pay_qr_camera=True)
        built = payment_qr.build(db, 950, "Оплата на кассе")
        self.assertEqual(built["kind"], "static")
        self.assertEqual(built["text"], "https://qr.nspk.ru/AS1000")
        self.assertTrue(built["can_open"])
        # сумму вводит покупатель — это цена «камерного» выбора
        self.assertFalse(built["amount_in_qr"])
        db.close()

    def test_camera_mode_prefers_link_that_carries_amount(self):
        """Ссылка с суммой лучше и камеры, и ГОСТ-кода: и открывается, и не ошибиться."""
        db = make_db(**GOOD, pay_qr_camera=True, sbp_shop_qr="https://qr.nspk.ru/AS1000",
                     pay_qr_link="https://pay.example.ru/qr?sum={amount}&n={number}")
        built = payment_qr.build(db, 950.25, "Оплата", number="777")
        self.assertEqual(built["kind"], "link")
        self.assertIn("sum=950.25", built["text"])
        self.assertTrue(built["amount_in_qr"])
        db.close()

    def test_default_mode_keeps_gost_first(self):
        """По умолчанию ничего не поменялось: сумма внутри кода важнее кнопки."""
        db = make_db(**GOOD, sbp_shop_qr="https://qr.nspk.ru/AS1000")
        built = payment_qr.build(db, 950)
        self.assertFalse(built["camera"])
        self.assertEqual(built["kind"], "gost")
        db.close()

    def test_auto_falls_back_to_static_qr_without_requisites(self):
        db = make_db(sbp_shop_qr="https://qr.nspk.ru/AS1000")
        built = payment_qr.build(db, 950)
        self.assertEqual(built["kind"], "static")
        self.assertFalse(built["amount_in_qr"])
        db.close()

    def test_dynamic_bank_payload_always_wins(self):
        db = make_db(**GOOD)
        built = payment_qr.build(db, 950, payment={"qr_payload": "https://qr.nspk.ru/AD42"})
        self.assertEqual(built["kind"], "dynamic")
        self.assertEqual(built["text"], "https://qr.nspk.ru/AD42")
        db.close()

    def test_mode_static_does_not_generate_gost(self):
        db = make_db(**GOOD, pay_qr_mode="static", sbp_shop_qr="https://qr.nspk.ru/AS1")
        self.assertEqual(payment_qr.build(db, 950)["kind"], "static")
        db.close()

    def test_mode_off_hides_qr(self):
        db = make_db(**GOOD, pay_qr_mode="off")
        built = payment_qr.build(db, 950)
        self.assertEqual(built["text"], "")
        self.assertFalse(built["enabled"])
        db.close()

    def test_missing_requisites_explain_what_to_fill(self):
        db = make_db(pay_qr_mode="gost")
        built = payment_qr.build(db, 100)
        self.assertEqual(built["text"], "")
        self.assertIn("Расчётный счёт", built["hint"])
        self.assertTrue(built["problems"])
        db.close()

    def test_svg_can_be_skipped_for_light_responses(self):
        db = make_db(**GOOD)
        self.assertEqual(payment_qr.build(db, 950, with_svg=False)["svg"], "")
        db.close()


class SettingsAndRouteTests(unittest.TestCase):
    def test_new_settings_are_described_in_schema(self):
        from connector.printflow.settings_schema import get_schema
        spec = get_schema()
        for key in ("pay_qr_mode", "pay_qr_link", "pay_account", "pay_bic",
                    "pay_corr_account", "pay_bank_name", "pay_payee_name",
                    "pay_payee_inn", "pay_payee_kpp"):
            self.assertIn(key, spec, key)
            self.assertTrue(spec[key]["label"], key)

    def test_routes_registered(self):
        from connector.printflow.api import register_routes
        from connector.printflow.router import router
        register_routes()
        paths = router.paths()
        self.assertIn("/api/payment/qr", paths)
        self.assertIn("/api/payment/qr/check", paths)


class CashierIntegrationTests(unittest.TestCase):
    """Касса показывает сгенерированный код с суммой продажи."""

    def setUp(self):
        from connector.printflow.accounting import Accounting
        from connector.printflow.cashier import Cashier
        self.db = make_db(**GOOD, cashier_code="1234")
        self.db.upsert("shelf_items", {"id": "s1", "name": "Адресник", "qty": 5,
                                       "price": 500, "active": 1})
        self.cashier = Cashier(self.db, Accounting(self.db))
        self.token = self.cashier.login("1234")["token"]

    def tearDown(self):
        self.db.close()

    def test_sbp_sale_returns_generated_qr_with_amount(self):
        sale = self.cashier.sell([{"item_id": "s1", "qty": 2}], "sbp", self.token)
        qr = sale["qr"]
        self.assertEqual(qr["kind"], "gost")
        self.assertIn("Sum=100000", qr["text"])
        self.assertTrue(qr["svg"].startswith("<svg"))
        self.assertTrue(qr["amount_in_qr"])

    def test_catalog_keeps_response_light(self):
        """В каталоге картинки нет — только режим и подсказка."""
        qr = self.cashier.catalog()["sbp"]
        self.assertEqual(qr["svg"], "")
        self.assertEqual(qr["kind"], "gost")


if __name__ == "__main__":
    unittest.main()

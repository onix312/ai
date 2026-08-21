"""Входящий заказ: локальный разбор текста и обогащение из справочников."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.api import Api  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.nomenclature import Nomenclature  # noqa: E402
from connector.printflow.order_intake import OrderIntake, parse_order_text  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402


class ParseOrderTextTests(unittest.TestCase):
    def test_short_telegram_format_remains_supported(self):
        parsed = parse_order_text(
            "новый адресник 2шт 900р Мария", today=date(2026, 8, 21))
        self.assertEqual(parsed["product"], "адресник")
        self.assertEqual(parsed["client"], "Мария")
        self.assertEqual(parsed["qty"], 2)
        self.assertEqual(parsed["price"], 900)

    def test_message_extracts_contacts_deadline_and_production_fields(self):
        parsed = parse_order_text(
            "Нужно 20 шт адресник PETG чёрный для Марии, "
            "телефон +7 999 123-45-67 до 25.08, бюджет 3600 руб",
            today=date(2026, 8, 21),
        )
        self.assertEqual(parsed["product"], "адресник")
        self.assertEqual(parsed["phone"], "+79991234567")
        self.assertEqual(parsed["due"], "2026-08-25")
        self.assertEqual(parsed["material"], "PETG")
        self.assertEqual(parsed["color"], "Чёрный")
        self.assertEqual(parsed["price"], 3600)

    def test_unit_price_is_converted_to_order_total(self):
        parsed = parse_order_text(
            "Адресник 3 шт по 450 ₽ @maria до завтра срочно",
            today=date(2026, 8, 21),
        )
        self.assertEqual(parsed["price"], 1350)
        self.assertEqual(parsed["messenger"], "@maria")
        self.assertEqual(parsed["due"], "2026-08-22")
        self.assertEqual(parsed["priority"], "urgent")


class OrderIntakeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "intake.sqlite3")
        self.db.upsert("customers", {
            "id": "cus-maria", "name": "Мария", "phone": "+79991234567",
            "messenger": "@maria", "created_at": "2026-08-01T10:00:00",
        })
        self.db.upsert("nomenclature", {
            "id": "nom-tag", "code": "000123", "sku": "TAG-PETG",
            "name": "Адресник", "kind": "product", "niche_id": "pets",
            "material": "PETG", "grams": 18, "hours": 0.6,
            "post_minutes": 3, "file": "tag.3mf", "archived": 0,
        })
        self.db.upsert("prices", {
            "id": "price-tag", "at": "2026-08-01T10:00:00",
            "nom_id": "nom-tag", "price_type_id": "retail", "price": 450,
        })
        self.intake = OrderIntake(self.db)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_preview_matches_customer_and_product_without_writing_order(self):
        before = self.db.one("SELECT COUNT(*) n FROM orders")["n"]
        result = self.intake.preview(
            "Нужно 3 шт адресник для Марии +7 999 123-45-67, PETG чёрный",
            "telegram",
        )
        draft = result["draft"]
        self.assertEqual(draft["customer_id"], "cus-maria")
        self.assertEqual(draft["customer_name"], "Мария")
        self.assertEqual(draft["nom_id"], "nom-tag")
        self.assertEqual(draft["product"], "Адресник")
        self.assertEqual(draft["price"], 1350)
        self.assertEqual(draft["grams"], 18)
        self.assertEqual(draft["file"], "tag.3mf")
        self.assertGreaterEqual(result["confidence"], 80)
        after = self.db.one("SELECT COUNT(*) n FROM orders")["n"]
        self.assertEqual(after, before)

    def test_unknown_product_stays_editable_and_has_warning(self):
        result = self.intake.preview("Необычная деталь 2 шт для Олега", "avito")
        self.assertEqual(result["draft"]["channel"], "avito")
        self.assertEqual(result["draft"]["qty"], 2)
        self.assertTrue(any("Изделие не найдено" in item for item in result["warnings"]))

    def test_storefront_order_reuses_catalog_price_and_production_norms(self):
        api = Api.__new__(Api)
        api.db = self.db
        api.repo = Repo(self.db)
        api.nom = Nomenclature(self.db)
        result = api.public_order({
            "name": "Мария", "phone": "+79991234567", "product": "Адресник",
            "nom_id": "nom-tag", "qty": 3, "color": "Чёрный",
        })
        order = self.db.one("SELECT * FROM orders WHERE id=?", (result["order_id"],))
        self.assertEqual(order["price"], 1350)
        self.assertEqual(order["material"], "PETG")
        self.assertEqual(order["grams"], 18)
        self.assertEqual(order["file"], "tag.3mf")

    def test_legacy_channel_labels_still_resolve_to_fee_rules(self):
        by_id = Accounting(self.db).channel("avito")
        by_old_label = Accounting(self.db).channel("Авито")
        self.assertEqual(by_old_label.get("id"), by_id.get("id"))
        self.assertEqual(by_old_label.get("fee_percent"), by_id.get("fee_percent"))


if __name__ == "__main__":
    unittest.main()

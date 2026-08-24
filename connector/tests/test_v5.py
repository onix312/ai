"""Тесты модулей PrintFlow 5.0: конструктор, конверты, клиенты, сеть, B2B, парсер."""
from __future__ import annotations

import pathlib
import struct
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402


class DesignTests(unittest.TestCase):
    def test_stl_layout(self):
        from connector.printflow.design import number_plate
        data = number_plate("12")
        self.assertGreater(len(data), 84)
        count = struct.unpack("<I", data[80:84])[0]
        self.assertEqual(len(data), 84 + count * 50)
        self.assertTrue(count > 0)

    def test_tag_disc(self):
        from connector.printflow.design import tag_disc
        self.assertGreater(len(tag_disc(30, 2)), 84)

    def test_cyrillic_now_supported(self):
        from connector.printflow.design import generate
        stl = generate("number_plate", {"number": "БАРСИК"})
        self.assertGreater(len(stl), 84)

    def test_latin_and_brand_card(self):
        from connector.printflow.design import generate
        stl = generate("number_plate", {"number": "Barsik"})
        self.assertGreater(len(stl), 84)
        card = generate("brand_card", {"text": "NOZZA"})
        self.assertGreater(len(card), 84)
        with self.assertRaises(ValueError):
            generate("number_plate", {"number": "☺"})

    def test_preview_svg(self):
        from connector.printflow.design import preview_svg
        svg = preview_svg("number_plate", {"number": "7", "width": 40, "height": 24})
        self.assertIn("<svg", svg)
        self.assertIn("7", svg)


class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_auto_allocate(self):
        from connector.printflow.envelopes import Envelopes, auto_allocate
        self.db.set_settings({"envelope_auto": True})
        env = Envelopes(self.db).save({"name": "Налог", "pct": 6})
        auto_allocate(self.db, {"kind": "income", "amount": 1000, "id": "tx1"})
        balances = {e["id"]: e["balance"] for e in Envelopes(self.db).list()}
        self.assertEqual(balances[env["id"]], 60.0)

    def test_withdraw(self):
        from connector.printflow.envelopes import Envelopes
        env = Envelopes(self.db).save({"name": "Принтер", "pct": 0, "goal": 50000})
        Envelopes(self.db).add_move(env["id"], 1000)
        Envelopes(self.db).withdraw(env["id"], 400)
        bal = {e["id"]: e["balance"] for e in Envelopes(self.db).list()}
        self.assertEqual(bal[env["id"]], 600.0)


class ClientsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_duplicates_and_merge(self):
        from connector.printflow.clients import Clients
        c = Clients(self.db)
        self.db.upsert("customers", {"id": "c1", "name": "Анна", "phone": "+7910"})
        self.db.upsert("customers", {"id": "c2", "name": "Анна", "phone": "+7910"})
        groups = c.duplicates()
        self.assertTrue(any(len(g) >= 2 for g in groups))
        self.db.upsert("orders", {"id": "o1", "customer_id": "c2", "product": "x",
                                  "status": "new", "created_at": "2026-08-01T00:00:00"})
        result = c.merge("c1", ["c2"])
        self.assertTrue(result["ok"])
        self.assertEqual(self.db.one("SELECT customer_id FROM orders WHERE id='o1'")["customer_id"], "c1")

    def test_rfm(self):
        from connector.printflow.clients import Clients
        self.db.upsert("customers", {"id": "c1", "name": "Мария"})
        rows = Clients(self.db).rfm()
        self.assertEqual(rows[0]["segment"], "Новый")


class InsightsV5Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_payback_not_ready(self):
        from connector.printflow.insights import Insights
        self.db.set_settings({"printer_investment": 100000})
        p = Insights(self.db).payback()
        self.assertFalse(p["ready"])
        self.assertEqual(p["pct"], 0.0)

    def test_tax_compare_sorted_with_best(self):
        from connector.printflow.insights import Insights
        rows = Insights(self.db).tax_compare()["rows"]
        self.assertEqual(rows[0]["best"], True)
        self.assertLessEqual(rows[0]["tax"], rows[1]["tax"])

    def test_cash_daily_shape(self):
        from connector.printflow.insights import Insights
        data = Insights(self.db).cash_forecast_daily(30)
        self.assertEqual(len(data["points"]), 31)
        self.assertEqual(data["points"][0]["day"], 0)


class NetworkTests(unittest.TestCase):
    def test_same_subnet(self):
        from connector.printflow.network import same_subnet
        self.assertTrue(same_subnet("192.168.1.10", "192.168.1.20"))
        self.assertFalse(same_subnet("192.168.1.10", "192.168.2.20"))

    def test_scan_empty(self):
        from connector.printflow.network import scan_ranges
        self.assertEqual(scan_ranges([]), [])

    def test_diagnose_shape(self):
        from connector.printflow.network import diagnose
        result = diagnose("127.0.0.1")
        self.assertIn("ports", result)
        self.assertEqual(len(result["ports"]), 3)

    def test_mdns_returns_list(self):
        from connector.printflow.network import mdns_discover
        self.assertIsInstance(mdns_discover(timeout=1.0), list)


class RepoV5Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        from connector.printflow.repo import Repo
        self.repo = Repo(self.db)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_duplicate_order(self):
        order = self.repo.save_order({"product": "Адресник", "price": 500, "status": "queue"})
        dup = self.repo.duplicate_order(order["id"])
        self.assertNotEqual(dup["number"], order["number"])
        self.assertEqual(dup["status"], "new")
        self.assertEqual(dup["paid"], 0.0)

    def test_order_history_recorded(self):
        order = self.repo.save_order({"product": "X", "price": 100, "status": "queue"})
        self.repo.save_order({"id": order["id"], "price": 200})
        hist = self.repo.order_history(order["id"])
        self.assertTrue(any(h["field"] == "price" for h in hist))

    def test_data_check_finds_no_price(self):
        self.repo.save_order({"product": "Y", "status": "queue"})
        result = self.repo.data_check()
        self.assertTrue(any(p["kind"] == "order_no_price" for p in result["problems"]))


class B2BTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        from connector.printflow.repo import Repo
        self.order = Repo(self.db).save_order({"product": "QR-стойка", "price": 1200, "status": "queue"})

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_invoice_html(self):
        from connector.printflow.b2b import B2B
        html = B2B(self.db).document(self.order["id"], "invoice")
        self.assertIn("Счёт", html)
        self.assertIn("1 200", html)

    def test_receipt_html(self):
        from connector.printflow.b2b import B2B
        self.assertIn("Товарный чек", B2B(self.db).document(self.order["id"], "receipt"))

    def test_waybill_html(self):
        from connector.printflow.b2b import B2B
        html = B2B(self.db).document(self.order["id"], "накладная")
        self.assertIn("Товарная накладная", html)
        self.assertIn("Отпустил", html)
        self.assertIn("Получил", html)
        self.assertIn("QR-стойка", html)


class OrderWaybillTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        from connector.printflow.documents import Documents
        from connector.printflow.nomenclature import Nomenclature
        from connector.printflow.repo import Repo
        self.docs = Documents(self.db)
        self.nom = Nomenclature(self.db)
        self.repo = Repo(self.db)
        self.item = self.nom.save({
            "name": "Адресник", "grams": 12, "hours": 0.3, "material": "PLA",
        })
        self.nom.set_price(self.item["id"], 450)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_waybill_from_order_and_list_by_order_id(self):
        order = self.repo.save_order({
            "product": "Адресник", "price": 450, "qty": 2,
            "nom_id": self.item["id"], "status": "queue",
        })
        payload = self.docs.for_order(order["id"])
        self.assertTrue(payload["can_create_waybill"])
        self.assertFalse(payload["has_waybill"])
        first = self.docs.waybill_from_order(order["id"])
        self.assertEqual(first["kind"], "sale")
        self.assertEqual(first["state"], "draft")
        self.assertEqual(first["order_id"], order["id"])
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(first["items"][0]["nom_id"], self.item["id"])
        second = self.docs.waybill_from_order(order["id"])
        self.assertEqual(second["id"], first["id"])
        listed = self.docs.list(order_id=order["id"])
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], first["id"])
        again = self.docs.for_order(order["id"])
        self.assertTrue(again["has_waybill"])
        self.assertEqual(again["waybill_id"], first["id"])


class TelegramParserTests(unittest.TestCase):
    def test_new_order_parse(self):
        from connector.printflow.telegram_bot import TelegramBot
        parsed = TelegramBot._parse_new_order(object(), "новый адресник 2шт 900р Мария")
        self.assertEqual(parsed["qty"], 2.0)
        self.assertEqual(parsed["price"], 900.0)
        self.assertEqual(parsed["client"], "Мария")
        self.assertIn("адресник", parsed["product"])


if __name__ == "__main__":
    unittest.main()

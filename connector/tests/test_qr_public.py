"""QR катушки и ценника должны вести на LAN, а не на localhost.

Панель почти всегда открыта как http://localhost:8080 — если эту строку
вшить в QR, телефон откроет сам себя и страница катушки «не работает».
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import config  # noqa: E402


class PublicBaseTests(unittest.TestCase):
    def test_setting_wins_over_localhost_header(self):
        info = config.public_base(
            "localhost:8080", "http://192.168.1.50:8080",
            lan_ips=["10.0.0.7"], listen_port=8080)
        self.assertEqual(info["base"], "http://192.168.1.50:8080")
        self.assertTrue(info["reachable"])
        self.assertEqual(info["source"], "setting")

    def test_setting_without_scheme(self):
        info = config.public_base("", "10.0.0.8:9000", lan_ips=[])
        self.assertEqual(info["base"], "http://10.0.0.8:9000")
        self.assertTrue(info["reachable"])

    def test_localhost_header_replaced_by_lan_ip(self):
        info = config.public_base(
            "localhost:8080", "", lan_ips=["192.168.0.14"], listen_port=8080)
        self.assertEqual(info["base"], "http://192.168.0.14:8080")
        self.assertEqual(info["source"], "lan")
        self.assertTrue(info["reachable"])
        self.assertNotIn("localhost", info["url"] if "url" in info else info["base"])

    def test_keeps_port_from_localhost_request(self):
        info = config.public_base(
            "127.0.0.1:9000", "", lan_ips=["192.168.1.2"], listen_port=8080)
        self.assertEqual(info["base"], "http://192.168.1.2:9000")

    def test_non_loopback_host_header_is_kept(self):
        info = config.public_base(
            "printflow.local:8080", "", lan_ips=["192.168.1.2"])
        self.assertEqual(info["base"], "http://printflow.local:8080")
        self.assertEqual(info["source"], "request")

    def test_no_lan_marks_unreachable(self):
        info = config.public_base("localhost:8080", "", lan_ips=[], listen_port=8080)
        self.assertFalse(info["reachable"])
        self.assertEqual(info["source"], "loopback")
        self.assertIn("localhost", info["base"])

    def test_loopback_names(self):
        self.assertTrue(config.is_loopback_host("localhost:8080"))
        self.assertTrue(config.is_loopback_host("127.0.0.1"))
        self.assertTrue(config.is_loopback_host("[::1]:8080"))
        self.assertFalse(config.is_loopback_host("192.168.1.50:8080"))
        self.assertFalse(config.is_loopback_host("printflow.local"))

    def test_page_url_encodes_query(self):
        info = config.public_page_url(
            "/spool.html", "id=sp_abc",
            host_header="localhost:8080", public_url="",
            lan_ips=["192.168.1.77"], listen_port=8080)
        self.assertEqual(info["url"], "http://192.168.1.77:8080/spool.html?id=sp_abc")
        self.assertNotIn("localhost", info["url"])


class ShelfQrLinkTests(unittest.TestCase):
    def test_returns_dict_with_lan_url(self):
        from connector.printflow.db import Database
        from connector.printflow.shelf import Shelf
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Database(pathlib.Path(tmp.name) / "t.sqlite3")
        self.addCleanup(db.close)
        link = Shelf(db).qr_link("shf-1", host="localhost:8080",
                                 public_url="", listen_port=8080)
        self.assertIsInstance(link, dict)
        self.assertIn("/shelf.html?id=shf-1", link["url"])
        # если LAN есть — localhost в ссылке быть не должно
        with patch.object(config, "get_local_ips", return_value=["10.1.2.3"]):
            link = Shelf(db).qr_link("shf-1", host="localhost:8080",
                                     listen_port=8080)
        self.assertTrue(link["url"].startswith("http://10.1.2.3:8080/shelf.html"))


class SpoolRepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from connector.printflow.db import Database
        from connector.printflow.repo import Repo
        self.db = Database(pathlib.Path(self.tmp.name) / "t.sqlite3")
        self.repo = Repo(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_spool_by_id(self):
        saved = self.repo.save_spool({
            "material": "PLA", "color_name": "Чёрный",
            "total_grams": 1000, "remaining_grams": 400, "price": 1600,
        })
        one = self.repo.spool(saved["id"])
        self.assertIsNotNone(one)
        self.assertEqual(one["material"], "PLA")
        self.assertEqual(one["percent"], 40.0)
        self.assertEqual(one["last_dry"], "")
        self.assertIsNone(self.repo.spool("нет-такой"))

    def test_last_dry_on_decorate(self):
        saved = self.repo.save_spool({
            "material": "PETG", "color_name": "Синий",
            "total_grams": 1000, "remaining_grams": 800, "price": 1800,
        })
        from connector.printflow.config import now_iso
        self.db.upsert("drying_sessions", {
            "id": "dry_t1", "at": now_iso(), "spool_id": saved["id"],
            "material": "PETG", "color_name": "Синий", "minutes": 240, "temp": 65,
        })
        one = self.repo.spool(saved["id"])
        self.assertTrue(one["last_dry"])
        self.assertEqual(one["last_dry_min"], 240)
        self.assertEqual(one["last_dry_temp"], 65)


class ApiQrRoutesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from connector.printflow.db import Database
        from connector.printflow.repo import Repo
        self.db = Database(pathlib.Path(self.tmp.name) / "t.sqlite3")
        self.repo = Repo(self.db)
        self.spool = self.repo.save_spool({
            "id": "sp_testqr", "material": "PLA", "color_name": "Белый",
            "total_grams": 1000, "remaining_grams": 900, "price": 1600,
        })

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _api(self):
        from connector.printflow.api import Api
        api = Api.__new__(Api)
        api.db = self.db
        api.repo = self.repo
        from connector.printflow.shelf import Shelf
        api.shelf = Shelf(self.db)
        api.last_host = "localhost:8080"
        api.listen_port = 8080
        return api

    def test_spool_qr_link_uses_lan(self):
        api = self._api()
        with patch.object(config, "get_local_ips", return_value=["192.168.10.4"]):
            code, payload = api.get("/api/spool/qr-link", {"id": ["sp_testqr"]})
        self.assertEqual(code, 200)
        self.assertEqual(payload["url"],
                         "http://192.168.10.4:8080/spool.html?id=sp_testqr")
        self.assertTrue(payload["reachable"])
        self.assertNotIn("localhost", payload["url"])

    def test_spool_qr_link_missing(self):
        code, payload = self._api().get("/api/spool/qr-link", {"id": ["нет"]})
        self.assertEqual(code, 404)
        self.assertIn("error", payload)

    def test_single_spool(self):
        code, payload = self._api().get("/api/spool", {"id": ["sp_testqr"]})
        self.assertEqual(code, 200)
        self.assertEqual(payload["spool"]["color_name"], "Белый")
        self.assertIn("suggest", payload)
        self.assertEqual(payload["suggest"]["slot"], "")

    def test_bind_and_unbind(self):
        api = self._api()
        code, payload = api.post("/api/spool/bind",
                                 {"id": "sp_testqr", "ams_slot": "2", "printer_id": ""}, {})
        self.assertEqual(code, 200)
        self.assertEqual(str(payload["spool"]["ams_slot"]), "2")
        self.assertFalse(payload.get("pushed"))
        code, payload = api.post("/api/spool/bind",
                                 {"id": "sp_testqr", "ams_slot": "", "printer_id": ""}, {})
        self.assertEqual(code, 200)
        self.assertFalse(payload["spool"].get("ams_slot"))

    def test_bind_rejects_bad_slot(self):
        with self.assertRaises(ValueError):
            self._api().post("/api/spool/bind",
                             {"id": "sp_testqr", "ams_slot": "99"}, {})

    def test_labels_spools(self):
        api = self._api()
        with patch.object(config, "get_local_ips", return_value=["10.8.0.4"]):
            code, payload = api.get("/api/labels", {"kind": ["spool"]})
        self.assertEqual(code, 200)
        self.assertEqual(len(payload["spools"]), 1)
        self.assertIn("sp_testqr", payload["spools"][0]["url"])
        self.assertNotIn("localhost", payload["spools"][0]["url"])
        self.assertEqual(payload["shelf"], [])

    def test_ops_today_without_manager(self):
        code, payload = self._api().get("/api/ops/today", {})
        self.assertEqual(code, 200)
        self.assertIn("lan", payload)
        self.assertIn("ready", payload)
        self.assertEqual(payload["next_id"], "")


class FrontendDoesNotHardcodeLocalhost(unittest.TestCase):
    def test_money_js_asks_server_for_spool_qr(self):
        text = (ROOT / "site" / "assets" / "money.js").read_text(encoding="utf-8")
        self.assertIn("/api/spool/qr-link", text)
        self.assertNotIn("location.host || '127.0.0.1:8080'", text)

    def test_spool_page_prefers_single_endpoint(self):
        text = (ROOT / "site" / "spool.html").read_text(encoding="utf-8")
        self.assertIn("/api/spool?id=", text)
        self.assertIn("/api/spool/bind", text)
        self.assertIn("push_ams", text)
        self.assertIn("last_dry", text)

    def test_labels_page_uses_api(self):
        text = (ROOT / "site" / "labels.html").read_text(encoding="utf-8")
        self.assertIn("/api/labels?kind=", text)
        self.assertIn("QR.svg", text)

    def test_phone_panel_uses_ops_today(self):
        text = (ROOT / "site" / "m.html").read_text(encoding="utf-8")
        self.assertIn("/api/ops/today", text)
        self.assertIn("b_resume", text)
        self.assertNotIn("localhost:8080", text)


if __name__ == "__main__":
    unittest.main()

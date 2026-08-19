"""Тесты Bambu Cloud: вход, облачная печать, мост и режимы принтера.

Управление принтером без LAN Only Mode: проверяем протокольные хелперы
(тело /my/task, маппинг AMS, заголовки), пайплайн облачной заливки на
моках, маршрутизацию облачного MQTT-моста и миграцию схемы (mode).
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import bambu_cloud  # noqa: E402
from connector.printflow.db import Database  # noqa: E402


class CloudProtocolTests(unittest.TestCase):
    def test_api_and_mqtt_hosts(self):
        self.assertEqual(bambu_cloud.api_host("global"), "https://api.bambulab.com")
        self.assertEqual(bambu_cloud.api_host("china"), "https://api.bambulab.cn")
        self.assertEqual(bambu_cloud.api_host(""), "https://api.bambulab.com")
        self.assertEqual(bambu_cloud.mqtt_host("global"), "us.mqtt.bambulab.com")
        self.assertEqual(bambu_cloud.mqtt_host("china"), "cn.mqtt.bambulab.com")

    def test_headers(self):
        headers = bambu_cloud.bbl_headers(token="tok", uid="123")
        self.assertEqual(headers["X-BBL-Client-Name"], "BambuStudio")
        self.assertEqual(headers["X-BBL-Client-Type"], "slicer")
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertIn("X-BBL-Client-ID", headers)
        headers_no_ct = bambu_cloud.bbl_headers(content_type=False)
        self.assertNotIn("Content-Type", headers_no_ct)

    def test_md5_upper(self):
        self.assertEqual(bambu_cloud.md5_hex_upper(b"printflow"),
                         "8EB0815E4198192D5139A048AE06B3DE")

    def test_ams_mapping2(self):
        self.assertEqual(bambu_cloud._ams_mapping2([0, -1, 5]),
                         [{"amsId": 0, "slotId": 0},
                          {"amsId": 255, "slotId": 0},
                          {"amsId": 1, "slotId": 1}])
        self.assertEqual(bambu_cloud._ams_mapping2(None), [])
        self.assertEqual(bambu_cloud._ams_mapping2([]), [])

    def test_build_task_body(self):
        body = bambu_cloud.build_task_body(
            "адресник.3mf", "SERIAL01", "m1", "p1", plate=2, use_ams=True,
            ams_mapping=[0, -1], timelapse=True)
        self.assertEqual(body["deviceId"], "SERIAL01")
        self.assertEqual(body["mode"], "cloud_file")
        self.assertEqual(body["plateIndex"], 2)
        self.assertEqual(body["modelId"], "m1")
        self.assertEqual(body["profileId"], "p1")
        self.assertTrue(body["useAms"])
        self.assertTrue(body["timelapse"])
        self.assertEqual(body["amsMapping2"],
                         [{"amsId": 0, "slotId": 0}, {"amsId": 255, "slotId": 0}])
        self.assertIn("sequence_id", body)
        self.assertIn("bedType", body)
        # Без маппинга ключи не отправляются — эндпоинт 400-ит на пустых.
        plain = bambu_cloud.build_task_body("x", "S", "m", "p")
        self.assertNotIn("amsMapping", plain)
        self.assertNotIn("amsMapping2", plain)


class CloudLoginTests(unittest.TestCase):
    def _login_mock(self, responses: list[dict]):
        call = {"n": 0}
        real_request = bambu_cloud.request

        def fake(method, url, **kwargs):
            body = kwargs.get("body") or {}
            if isinstance(body, bytes):
                body = json.loads(body.decode("utf-8"))
            index = call["n"]
            call["n"] += 1
            call["last"] = (method, url, body)
            if index >= len(responses):
                return {"_raw": "{}"}
            item = responses[index]
            if isinstance(item, Exception):
                raise item
            return item

        return fake, call, real_request

    def test_login_ok_and_uid(self):
        fake, call, real = self._login_mock([
            {"accessToken": "TOKEN123", "loginType": ""},
            {"uid": 424242},  # preference
        ])
        with mock.patch.object(bambu_cloud, "request", side_effect=fake):
            result = bambu_cloud.login("a@b.c", "pass", "global")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["token"], "TOKEN123")
        self.assertEqual(result["uid"], "424242")
        self.assertEqual(call["last"][0], "GET")

    def test_login_needs_code(self):
        fake, call, _ = self._login_mock([
            {"loginType": "verifyCode"},   # login
            {},                            # sendemail/code — не падает
        ])
        with mock.patch.object(bambu_cloud, "request", side_effect=fake):
            result = bambu_cloud.login("a@b.c", "pass")
        self.assertEqual(result["status"], "need_code")
        self.assertIn("код", result["message"].lower())

    def test_login_with_code(self):
        calls = []
        real_request = bambu_cloud.request

        def fake(method, url, **kwargs):
            body = kwargs.get("body") or {}
            calls.append((method, url, body))
            if len(calls) == 1:
                return {"accessToken": "T2", "loginType": ""}
            return {"uidStr": "777"}

        with mock.patch.object(bambu_cloud, "request", side_effect=fake):
            result = bambu_cloud.login("a@b.c", "", code="123456")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["uid"], "777")
        # первый шаг — логин с кодом, а не паролем
        self.assertEqual(calls[0][2], {"account": "a@b.c", "code": "123456"})

    def test_login_wrong_password_raises(self):
        def fail(*a, **k):
            raise bambu_cloud.CloudError("неверный логин")
        with mock.patch.object(bambu_cloud, "request", side_effect=fail):
            with self.assertRaises(bambu_cloud.CloudError):
                bambu_cloud.login("a@b.c", "bad")

    def test_devices_parsing(self):
        payload = {"devices": [{"dev_id": "01P123", "name": "Мой P1S",
                                "dev_product_name": "P1S", "online": True,
                                "print_status": "IDLE",
                                "dev_access_code": "SECRET"}]}
        with mock.patch.object(bambu_cloud, "request", return_value=payload):
            devices = bambu_cloud.get_devices("tok")
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["serial"], "01P123")
        self.assertEqual(devices[0]["model"], "P1S")
        # Access Code в результат не попадает — секрет наружу не уходит
        self.assertNotIn("access_code", devices[0])
        self.assertNotIn("SECRET", json.dumps(devices))


class CloudUploadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._tmp.name) / "model.3mf"
        self.path.write_bytes(b"3MF-PRINTFLOW-TEST")

    def tearDown(self):
        self._tmp.cleanup()

    def test_upload_project_pipeline(self):
        """Последовательность шагов Bambu Studio: project → PUT → notification
        → poll → upload url → PUT → PATCH."""
        seq = []

        def fake_request(method, url, **kwargs):
            seq.append((method, url.split("?")[0].split("bambulab.com")[-1]))
            if method == "POST" and url.endswith("/user/project"):
                return {"project_id": "P1", "model_id": "M1", "profile_id": "PR1",
                        "upload_url": "https://s3/up", "upload_ticket": "TICK"}
            if method == "PUT" and url.endswith("/user/notification"):
                return {"ok": True}
            if method == "GET" and "/user/notification" in url:
                return {"message": "success"}
            if method == "GET" and "/user/upload" in url:
                return {"urls": [{"url": "https://s3/main"}]}
            if method == "PATCH":
                return {"ok": True}
            return {}

        with mock.patch.object(bambu_cloud, "request", side_effect=fake_request), \
             mock.patch.object(bambu_cloud, "_s3_put", return_value=None) as s3:
            manifest = bambu_cloud.upload_project(self.path, "tok", "1", "global")
        self.assertEqual(manifest["project_id"], "P1")
        self.assertEqual(manifest["md5"], bambu_cloud.md5_hex_upper(b"3MF-PRINTFLOW-TEST"))
        self.assertEqual(s3.call_count, 2)  # конфиг + основной файл
        self.assertEqual(seq[0], ("POST", "/v1/iot-service/api/user/project"))
        self.assertIn(("PATCH", "/v1/iot-service/api/user/project/P1"), seq)
        self.assertIn(("GET", "/v1/iot-service/api/user/upload"), seq)
        # presigned PUT без Content-Type не проверяется здесь — но вызов был

    def test_upload_missing_file(self):
        with self.assertRaises(bambu_cloud.CloudError):
            bambu_cloud.upload_project(pathlib.Path(self._tmp.name) / "нет.3mf",
                                       "tok", "1")

    def test_create_task_dispatch(self):
        payload = {"id": 987654}
        with mock.patch.object(bambu_cloud, "request", return_value=payload) as req:
            result = bambu_cloud.create_task(
                "tok", "1", "global",
                {"name": "x.3mf", "model_id": "M", "profile_id": "P"},
                "SERIAL", plate=1)
        self.assertEqual(result["task_id"], "987654")
        body = req.call_args.kwargs["body"]  # request() получает dict
        self.assertEqual(body["mode"], "cloud_file")
        self.assertEqual(body["deviceId"], "SERIAL")

    def test_patch_project_url(self):
        with mock.patch.object(bambu_cloud, "request", return_value={}) as req:
            bambu_cloud.patch_project_url("tok", "1", "global",
                                          {"project_id": "P", "profile_id": "PR",
                                           "md5": "ABC"}, "ftp:///x.3mf")
        body = req.call_args.kwargs["body"]
        self.assertEqual(body["profile_print_3mf"][0]["url"], "ftp:///x.3mf")
        self.assertEqual(body["profile_print_3mf"][0]["md5"], "ABC")


class CloudBridgeTests(unittest.TestCase):
    def test_message_routing(self):
        """Топик device/{serial}/report → обработчик нужного принтера."""
        from connector.printflow.cloud_bridge import CloudBridge
        bridge = CloudBridge("global", "1", "tok")
        got = {}
        bridge.attach("SER-A", lambda serial, payload: got.update(
            {"a": payload.get("x")}), lambda c, e: None)
        bridge.attach("SER-B", lambda serial, payload: got.update(
            {"b": payload.get("x")}), lambda c, e: None)

        class Msg:
            topic = "device/SER-B/report"
            payload = json.dumps({"x": "hello"}).encode()

        bridge._on_message(None, None, Msg())
        self.assertEqual(got, {"b": "hello"})
        self.assertNotIn("a", got)
        bridge.shutdown()

    def test_publish_requires_connection(self):
        from connector.printflow.cloud_bridge import CloudBridge
        bridge = CloudBridge("global", "1", "tok")
        with self.assertRaises(ConnectionError):
            bridge.publish("SER-A", {"print": {}})
        bridge.shutdown()

    def test_state(self):
        from connector.printflow.cloud_bridge import CloudBridge
        CloudBridge.shutdown_all()
        bridge = CloudBridge("global", "1", "tok")
        state = bridge.state()
        self.assertTrue(state["logged"])
        self.assertFalse(state["connected"])
        bridge.shutdown()

    def test_connect_without_token(self):
        from connector.printflow import cloud_bridge
        if cloud_bridge.mqtt is None:
            self.skipTest("paho-mqtt не установлен")
        bridge = cloud_bridge.CloudBridge("global", "1", "")
        bridge.connect()
        self.assertFalse(bridge.connected)
        self.assertIn("вход", bridge.last_error.lower())
        bridge.shutdown()


class PrinterModeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_mode_column_and_defaults(self):
        cols = [r["name"] for r in self.db.query("PRAGMA table_info(printers)")]
        self.assertIn("mode", cols)
        row = self.db.upsert("printers", {"id": "p1", "name": "Старый"})
        self.assertEqual(row.get("mode"), "lan")  # существующие остаются на LAN

    def test_save_printer_new_defaults_cloud(self):
        from connector.printflow.repo import Repo
        repo = Repo(self.db)
        printer = repo.save_printer({"name": "Новый", "serial": "S1"})
        self.assertEqual(printer["mode"], "cloud")

    def test_save_printer_update_keeps_mode(self):
        from connector.printflow.repo import Repo
        repo = Repo(self.db)
        printer = repo.save_printer({"name": "Старый", "host": "1.2.3.4",
                                     "serial": "S1", "access_code": "12345678"})
        self.assertEqual(printer["mode"], "cloud")
        updated = repo.save_printer({"id": printer["id"], "name": "Старый+"})
        self.assertEqual(updated["mode"], "cloud")  # не сбросился на lan

    def test_bambu_printer_cloud_ready(self):
        from connector.printflow.bambu import BambuPrinter
        printer = BambuPrinter({"id": "p1", "name": "P", "serial": "S",
                                "enabled": 1, "mode": "cloud", "host": "",
                                "access_code": ""})
        self.assertFalse(printer.ready)  # нет токена — ещё не готов
        printer = BambuPrinter({"id": "p1", "name": "P", "serial": "S",
                                "enabled": 1, "mode": "cloud", "host": "",
                                "access_code": "", "cloud_token": "tok",
                                "cloud_uid": "1", "cloud_region": "global"})
        self.assertTrue(printer.ready)
        lan = BambuPrinter({"id": "p2", "name": "L", "serial": "S", "enabled": 1,
                            "mode": "lan", "host": "10.0.0.5",
                            "access_code": "12345678"})
        self.assertTrue(lan.ready)
        lan2 = BambuPrinter({"id": "p3", "name": "L", "serial": "S", "enabled": 1,
                             "mode": "lan", "host": "", "access_code": "12345678"})
        self.assertFalse(lan2.ready)

    def test_start_print_cloud_fallback_to_gcode(self):
        """Облачная печать недоступна, LAN есть: .gcode уходит по FTPS + MQTT."""
        from connector.printflow import manager as manager_module
        from connector.printflow.manager import PrinterManager
        from connector.printflow.bambu import BambuPrinter
        self.db.set_settings({"cloud_token": "tok", "cloud_uid": "1",
                              "cloud_region": "global"})
        manager = PrinterManager(self.db, None)  # type: ignore[arg-type]
        try:
            tmp_dir = pathlib.Path(self._tmp.name)
            local = tmp_dir / "деталь.gcode"
            local.write_bytes(b";TIME:100\n")
            printer = BambuPrinter({"id": "p1", "name": "P", "serial": "S",
                                    "enabled": 1, "mode": "cloud",
                                    "host": "10.0.0.5", "access_code": "12345678",
                                    "cloud_token": "tok", "cloud_uid": "1",
                                    "cloud_region": "global"})

            def cloud_fail(*a, **k):
                raise bambu_cloud.CloudError("облако недоступно")

            with mock.patch.object(manager_module, "UPLOAD_DIR", tmp_dir), \
                 mock.patch.object(bambu_cloud, "upload_and_dispatch",
                                   side_effect=cloud_fail), \
                 mock.patch.object(bambu_cloud, "upload_project",
                                   side_effect=cloud_fail), \
                 mock.patch("connector.printflow.ftps.PrinterFiles.upload",
                            return_value={"ok": True}) as ftp, \
                 mock.patch.object(printer, "command",
                                   return_value={"ok": True}) as cmd:
                result = manager.start_print_cloud(printer, "деталь.gcode", plate=1)
            self.assertTrue(result.get("gcode"))
            self.assertEqual(ftp.call_args[0][1], "cache/деталь.gcode")
            self.assertEqual(cmd.call_args[0][0], "print_gcode")
        finally:
            manager.shutdown()

    def test_sync_cloud_history_adds_missing_jobs(self):
        from connector.printflow.manager import PrinterManager
        from connector.printflow.bambu import BambuPrinter
        tasks = [{
            "id": 1, "title": "адресник.3mf", "status": 2,
            "startTime": "2026-08-19T10:00:00Z", "endTime": "2026-08-19T10:05:00Z",
            "weight": 30, "amsDetailMapping": [{"weight": 25}, {"weight": 5}],
            "cover": "",
        }]
        self.db.set_settings({"cloud_token": "tok", "cloud_uid": "1"})
        manager = PrinterManager(self.db, None)  # type: ignore[arg-type]
        try:
            printer = BambuPrinter({"id": "p1", "name": "P", "serial": "S",
                                    "enabled": 1, "mode": "cloud"})
            manager.printers["p1"] = printer
            with mock.patch.object(bambu_cloud, "get_tasks", return_value=tasks):
                result = manager.sync_cloud_history("p1")
            self.assertTrue(result["ok"])
            self.assertEqual(result["added"], 1)
            jobs = self.db.query("SELECT * FROM print_jobs WHERE name='адресник.3mf'")
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["state"], "done")
            self.assertEqual(jobs[0]["source"], "cloud")
            self.assertEqual(round(jobs[0]["grams"], 1), 30.0)
            self.assertEqual(round(jobs[0]["duration_min"], 1), 5.0)
            # повторный запуск дублей не создаёт
            with mock.patch.object(bambu_cloud, "get_tasks", return_value=tasks):
                again = manager.sync_cloud_history("p1")
            self.assertEqual(again["added"], 0)
        finally:
            manager.shutdown()


class CloudApiTests(unittest.TestCase):
    """HTTP-роуты облака без поднятия сервера: логика хелперов Api."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        from connector.printflow.api import Api
        # Api без конструктора: не трогаем реальный каталог данных и сеть.
        self.api = Api.__new__(Api)
        self.api.db = self.db
        from connector.printflow.manager import PrinterManager
        self.manager = PrinterManager(self.db, None)  # type: ignore[arg-type]
        self.api.manager = self.manager

    def tearDown(self):
        self.manager.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def test_cloud_status_masked(self):
        self.db.set_settings({"cloud_email": "a@b.c", "cloud_token": "SECRET",
                              "cloud_uid": "42"})
        status = self.api.cloud_status()
        self.assertTrue(status["logged"])
        self.assertEqual(status["email"], "a@b.c")
        self.assertNotIn("SECRET", json.dumps(status))
        public = self.db.settings()
        self.assertEqual(public["cloud_token"], "••••••••")
        self.assertTrue(public["has_cloud_token"])

    def test_logout_clears_secret(self):
        self.db.set_settings({"cloud_token": "SECRET", "cloud_uid": "42"})
        self.db.clear_settings(["cloud_token", "cloud_uid"])
        raw = self.db.settings(include_secrets=True)
        self.assertEqual(raw["cloud_token"], "")
        self.assertEqual(raw["cloud_uid"], "")

    def test_enrich_cloud_device(self):
        self.db.set_settings({"cloud_token": "TOK", "cloud_uid": "42"})
        with mock.patch.object(bambu_cloud, "get_devices", return_value=[
                {"serial": "01P999", "name": "Мой", "model": "P1S"}]):
            data = self.api._enrich_cloud_device({"cloud_device": "01P999", "name": "x"})
        self.assertEqual(data["serial"], "01P999")
        self.assertEqual(data["name"], "x")  # имя пользователя не перетираем
        self.assertEqual(data["model"], "P1S")
        self.assertEqual(data["mode"], "cloud")


if __name__ == "__main__":
    unittest.main()

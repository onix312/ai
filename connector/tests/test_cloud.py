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
from connector.printflow.accounting import num  # noqa: E402
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

    def test_login_with_code_pending_fallback(self):
        bambu_cloud._PENDING.clear()
        bambu_cloud._PENDING["fallback@bambu.com"] = {"region": "global"}
        calls = []

        def fake(method, url, **kwargs):
            body = kwargs.get("body") or {}
            calls.append((method, url, body))
            if len(calls) == 1:
                return {"accessToken": "T3", "loginType": ""}
            return {"uidStr": "888"}

        with mock.patch.object(bambu_cloud, "request", side_effect=fake):
            result = bambu_cloud.login("", "", code="999888")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["uid"], "888")
        self.assertEqual(calls[0][2], {"account": "fallback@bambu.com", "code": "999888"})

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

    def test_devices_include_access_code(self):
        """Сервер может запросить Access Code отдельно (для камеры/FTPS)."""
        payload = {"devices": [{"dev_id": "01P123", "name": "Мой P1S",
                                "dev_product_name": "P1S", "online": True,
                                "dev_access_code": "SECRET", "dev_ip": "10.0.0.7"}]}
        with mock.patch.object(bambu_cloud, "request", return_value=payload):
            devices = bambu_cloud.get_devices("tok", include_access_code=True)
        self.assertEqual(devices[0]["access_code"], "SECRET")
        self.assertEqual(devices[0]["host"], "10.0.0.7")


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
                {"serial": "01P999", "name": "Мой", "model": "P1S",
                 "access_code": "LANCODE", "host": "10.0.0.9"}]):
            data = self.api._enrich_cloud_device({"cloud_device": "01P999", "name": "x"})
        self.assertEqual(data["serial"], "01P999")
        self.assertEqual(data["name"], "x")  # имя пользователя не перетираем
        self.assertEqual(data["model"], "P1S")
        self.assertEqual(data["mode"], "cloud")
        # Камера/FTPS по локальной сети: сервер подставляет код и IP сам.
        self.assertEqual(data["access_code"], "LANCODE")
        self.assertEqual(data["host"], "10.0.0.9")

    def test_enrich_cloud_device_without_secrets(self):
        """Нет Access Code в ответе — подстановки не происходит, ничего не падает."""
        self.db.set_settings({"cloud_token": "TOK", "cloud_uid": "42"})
        with mock.patch.object(bambu_cloud, "get_devices", return_value=[
                {"serial": "01P999", "name": "Мой", "model": "P1S"}]), \
             mock.patch.object(self.api, "_discover_host", return_value=""):
            data = self.api._enrich_cloud_device({"cloud_device": "01P999", "name": "x"})
        self.assertEqual(data["serial"], "01P999")
        self.assertNotIn("access_code", data)
        self.assertNotIn("host", data)

    def test_login_need_code_flow_saves_email_and_confirms(self):
        """Проверка цепочки: /api/cloud/login (need_code) -> /api/cloud/code (ok).
        Email не теряется, даже если /api/cloud/code вызван только с {code}."""
        from connector.printflow.bus import EventBus
        self.api.bus = EventBus()
        bambu_cloud._PENDING.clear()

        # Шаг 1: login возвращает need_code
        with mock.patch.object(bambu_cloud, "login", return_value={
                "status": "need_code", "message": "Код отправлен"}):
            code, resp = self.api.post("/api/cloud/login",
                                       {"email": "mybambu@mail.com", "password": "pass", "region": "global"},
                                       {})
        self.assertEqual(code, 200)
        self.assertEqual(resp["status"], "need_code")
        self.assertEqual(self.db.setting("cloud_email"), "mybambu@mail.com")

        # Шаг 2: подтверждение кода без повторной передачи email
        with mock.patch.object(bambu_cloud, "login", return_value={
                "status": "ok", "token": "NEW_TOKEN", "uid": "999", "message": "Вход выполнен"}):
            code, resp = self.api.post("/api/cloud/code", {"code": "654321"}, {})
        self.assertEqual(code, 200)
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(self.db.setting("cloud_email"), "mybambu@mail.com")
        self.assertEqual(self.db.setting("cloud_token"), "NEW_TOKEN")
        self.assertEqual(self.db.setting("cloud_uid"), "999")


class FilamentAutoAccountTests(unittest.TestCase):
    """Автоматизация учёта филамента: автоархив пустой катушки и живая стоимость."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _spool(self, remaining: float, price: float = 1600.0, total: float = 1000.0) -> str:
        row = self.db.upsert("spools", {
            "id": "sp1", "material": "PLA", "color_name": "Чёрный",
            "total_grams": total, "remaining_grams": remaining,
            "price": price, "archived": 0})
        return row["id"]

    def test_consume_filament_archives_empty_spool(self):
        from connector.printflow.accounting import Accounting
        acc = Accounting(self.db)
        spool_id = self._spool(remaining=5.0)
        result = acc.consume_filament(10.0, spool_id=spool_id, job_id="j1")
        self.assertTrue(result["ok"])
        spool = self.db.one("SELECT * FROM spools WHERE id=?", (spool_id,))
        self.assertEqual(spool["archived"], 1)
        self.assertEqual(spool["remaining_grams"], 0)
        # событие «катушка закончилась» в журнале
        events = self.db.query("SELECT * FROM events WHERE kind='filament_low'")
        self.assertTrue(any("закончилась" in (e.get("title") or "") for e in events))

    def test_consume_filament_keeps_spool_below_threshold(self):
        from connector.printflow.accounting import Accounting
        acc = Accounting(self.db)
        spool_id = self._spool(remaining=300.0)
        acc.consume_filament(250.0, spool_id=spool_id, job_id="j1")
        spool = self.db.one("SELECT * FROM spools WHERE id=?", (spool_id,))
        self.assertEqual(spool["archived"], 0)  # ещё не пустая
        self.assertEqual(spool["remaining_grams"], 50.0)

    def test_register_job_costs_adds_purge_for_multicolor(self):
        from connector.printflow.accounting import Accounting
        acc = Accounting(self.db)
        self.db.upsert("orders", {"id": "o1", "number": "1001", "product": "x",
                                  "status": "new", "price": 1000, "qty": 1,
                                  "colors": '[{"color":"Белый","grams":30},'
                                            '{"color":"Чёрный","grams":30}]'})
        job = {"id": "j1", "order_id": "o1", "grams": 60, "duration_min": 120,
               "state": "done", "printer_id": "p1"}
        result = acc.register_job_costs(job)
        self.assertGreater(result["purge_grams"], 0)
        self.assertGreater(result["breakdown"]["purge_grams"], 0)

    def test_reprint_job_clones_failed_into_queue(self):
        from connector.printflow.manager import PrinterManager
        manager = PrinterManager(self.db, None)  # type: ignore[arg-type]
        try:
            self.db.upsert("print_jobs", {"id": "j1", "printer_id": "p1",
                                          "order_id": None, "name": "деталь.3mf",
                                          "state": "failed", "source": "printer",
                                          "file": "деталь.3mf", "finished_at": "2026-08-20T10:00:00",
                                          "grams": 20, "duration_min": 60, "cost": 5.0})
            row = manager.reprint_job("j1")
            self.assertEqual(row["state"], "queued")
            self.assertEqual(row["source"], "reprint")
            self.assertEqual(row["name"], "деталь.3mf (повтор)")
            # сброс счётчиков: у клона граммы/время/стоимость обнулены
            self.assertEqual(row.get("grams"), 0.0)
            self.assertEqual(row.get("duration_min"), 0.0)
            self.assertEqual(row.get("cost"), 0.0)
        finally:
            manager.shutdown()

    def test_reprint_last_failed_by_order(self):
        from connector.printflow.manager import PrinterManager
        manager = PrinterManager(self.db, None)  # type: ignore[arg-type]
        try:
            self.db.upsert("orders", {"id": "o1", "number": "1001", "product": "x",
                                      "status": "new"})
            self.db.upsert("print_jobs", {"id": "j9", "printer_id": "p1",
                                          "order_id": "o1", "name": "z.3mf",
                                          "state": "failed", "source": "printer",
                                          "finished_at": "2026-08-20T11:00:00"})
            row = manager.reprint_last_failed("1001")
            self.assertEqual(row["state"], "queued")
            self.assertEqual(row["order_id"], "o1")
            with self.assertRaises(ValueError):
                manager.reprint_last_failed("9999")
        finally:
            manager.shutdown()

    def test_flow_and_speed_pct_commands(self):
        from connector.printflow.bambu import BambuPrinter
        printer = BambuPrinter({"id": "p1", "name": "P", "serial": "S",
                                "enabled": 1, "mode": "cloud"})
        sent = {}
        printer.publish = lambda payload: sent.update(payload)
        printer.command("flow", 90)
        self.assertIn("M221 S90", sent.get("print", {}).get("param", ""))
        printer.command("speed_pct", 120)
        self.assertIn("M220 S120", sent.get("print", {}).get("param", ""))

    def test_job_summary_live_cost_uses_real_spool_price(self):
        from connector.printflow.manager import PrinterManager
        manager = PrinterManager(self.db, None)  # type: ignore[arg-type]
        try:
            self._spool(remaining=500.0, price=2000.0, total=1000.0)  # 2 ₽/г
            self.db.upsert("orders", {"id": "o1", "number": "1001", "product": "x",
                                      "status": "printing", "price": 1000,
                                      "grams": 100, "qty": 1})
            self.db.upsert("print_jobs", {"id": "j1", "printer_id": "p1",
                                          "order_id": "o1", "state": "running",
                                          "spool_id": "sp1", "name": "x.3mf"})
            snap = {
                "id": "p1",
                "printer": {"state": "RUNNING", "weight": 50, "progress": 50,
                            "elapsed_min": 60, "remaining_min": 60},
                "ams": {"trays": []},
            }
            summary = manager.job_summary(snap)
            # Реальная цена катушки 2 ₽/г: 50 г = 100 ₽ филамента (не тариф).
            self.assertGreaterEqual(summary["spent"], 100.0)
            self.assertEqual(summary["spool"]["material"], "PLA")
            self.assertEqual(summary["spool"]["price"], 2000.0)
            self.assertGreater(summary["cost_total"], summary["spent"])
            self.assertGreater(summary["remaining_grams"], 0)
            # прибыль = цена − полная себестоимость; точка безубыточности есть.
            self.assertIsNotNone(summary["profit"])
            self.assertIsNotNone(summary["break_even_pct"])
        finally:
            manager.shutdown()


class GuardAndTelegramTests(unittest.TestCase):
    """Сторож (перерасход) и новые команды Telegram (callback/медиагруппа)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_overrun_alert_when_projected_exceeds_estimate(self):
        from connector.printflow.watchdog import Watchdog
        from connector.printflow.manager import PrinterManager
        manager = PrinterManager(self.db, None)  # type: ignore[arg-type]
        try:
            guard = Watchdog(manager)
            self.db.upsert("print_jobs", {"id": "j1", "printer_id": "p1",
                                          "state": "running", "name": "x.3mf",
                                          "est_grams": 100})
            snap = {"printer": {"state": "RUNNING", "progress": 50,
                                "weight": 70, "problems": []},
                    "ams": {"trays": []},
                    "temperature": {"nozzle": 220, "nozzle_target": 220, "bed": 55,
                                    "bed_target": 55, "chamber": 30},
                    "fans": {"part": 0, "aux": 0}}
            alerts = guard._check_overrun(manager.get("p1") or _FakePrinter(), snap)
            # 70 г к 50% → проецируется 140 г против сметы 100 (+40% > 15%)
            self.assertTrue(any(a["code"] == "overrun" for a in alerts))
        finally:
            manager.shutdown()

    def test_overrun_silent_when_within_tolerance(self):
        from connector.printflow.watchdog import Watchdog
        from connector.printflow.manager import PrinterManager
        manager = PrinterManager(self.db, None)  # type: ignore[arg-type]
        try:
            guard = Watchdog(manager)
            self.db.upsert("print_jobs", {"id": "j1", "printer_id": "p1",
                                          "state": "running", "name": "x.3mf",
                                          "est_grams": 100})
            snap = {"printer": {"state": "RUNNING", "progress": 50,
                                "weight": 52, "problems": []},
                    "ams": {"trays": []},
                    "temperature": {"nozzle": 220, "nozzle_target": 220, "bed": 55,
                                    "bed_target": 55, "chamber": 30},
                    "fans": {"part": 0, "aux": 0}}
            alerts = guard._check_overrun(_FakePrinter(), snap)
            self.assertEqual(alerts, [])
        finally:
            manager.shutdown()

    def test_telegram_printer_selection(self):
        from connector.printflow.manager import PrinterManager
        from connector.printflow.telegram_bot import TelegramBot
        from connector.printflow.bambu import BambuPrinter
        manager = PrinterManager(self.db, None)  # type: ignore[arg-type]
        try:
            bot = TelegramBot.__new__(TelegramBot)
            bot.manager = manager
            bot.db = self.db
            bot._printer_choice = {}
            # Два принтера в парке
            manager.printers["p1"] = BambuPrinter({"id": "p1", "name": "Первый",
                                                   "serial": "S1", "enabled": 1, "mode": "cloud"})
            manager.printers["p2"] = BambuPrinter({"id": "p2", "name": "Второй",
                                                   "serial": "S2", "enabled": 1, "mode": "cloud"})
            listing = bot._list_printers("chat1")
            self.assertIn("Первый", listing)
            self.assertIn("Второй", listing)
            result = bot._select_printer("chat1", 2)
            self.assertIn("Второй", result)
            self.assertEqual(bot._printer_choice.get("chat1"), "p2")
            self.assertIn("1 до 2", bot._select_printer("chat1", 9))
        finally:
            manager.shutdown()

    def test_telegram_run_command_next_and_reprint(self):
        from connector.printflow.manager import PrinterManager
        from connector.printflow.telegram_bot import TelegramBot
        manager = PrinterManager(self.db, None)  # type: ignore[arg-type]
        try:
            bot = TelegramBot.__new__(TelegramBot)
            bot.manager = manager
            bot.db = self.db
            # reprint без failed-заданий — честная ошибка
            self.assertIn("Нет сорванных", bot._run_command("reprint"))
            self.assertIn("Принтеры не добавлены", bot._run_command("next"))
            self.assertIn("Деталь снята", bot._run_command("removed"))
        finally:
            manager.shutdown()


class _FakePrinter:
    id = "p1"
    record = {"name": "Принтер"}

    class _Camera:
        frame = None

        def snapshot(self, note=""):
            raise ValueError("no frame")

    camera = _Camera()


if __name__ == "__main__":
    unittest.main()


class RulesEngineTests(unittest.TestCase):
    """Конструктор правил «если-то»: триггеры, шаблоны, действия."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _engine(self):
        from connector.printflow.manager import PrinterManager
        from connector.printflow.rules import RulesEngine
        manager = PrinterManager(self.db, None)  # type: ignore[arg-type]
        return manager, manager.rules

    def test_seed_defaults_creates_rules(self):
        manager, engine = self._engine()
        try:
            rules = engine.rules()
            self.assertGreaterEqual(len(rules), 6)
            events = {r["event"] for r in rules}
            self.assertIn("print_failed", events)
            self.assertIn("debt_overdue", events)
            self.assertIn("order_status", events)
            # все стартовые правила выключены
            self.assertTrue(all(not int(num(r.get("enabled"))) for r in rules))
        finally:
            manager.shutdown()

    def test_template_render(self):
        from connector.printflow.rules import _render
        self.assertEqual(_render("Привет, {name}!", {"name": "Мир"}), "Привет, Мир!")
        self.assertEqual(_render("{missing} {x}", {"x": 1}), " 1")

    def test_run_fires_matching_rule(self):
        manager, engine = self._engine()
        try:
            rule = engine.save_rule({
                "name": "Тест", "event": "print_failed", "action": "event",
                "config": {"template": "Ошибка: {name}"}, "enabled": 1})
            fired = engine.run("print_failed", {"name": "деталь.3mf", "detail": "x"})
            self.assertEqual(len(fired), 1)
            self.assertEqual(fired[0]["id"], rule["id"])
            # событие записано в журнал с подставленным шаблоном
            events = self.db.events(50, kind="rule")
            self.assertTrue(any("деталь.3mf" in (e.get("detail") or "") for e in events))
            # счётчик срабатываний вырос
            saved = self.db.one("SELECT fires FROM automation_rules WHERE id=?", (rule["id"],))
            self.assertEqual(int(num(saved["fires"])), 1)
        finally:
            manager.shutdown()

    def test_run_skips_disabled_and_wrong_event(self):
        manager, engine = self._engine()
        try:
            engine.save_rule({"name": "A", "event": "print_failed", "action": "event",
                              "enabled": 0})
            engine.save_rule({"name": "B", "event": "print_complete", "action": "event",
                              "enabled": 1})
            fired = engine.run("print_failed", {"name": "x"})
            self.assertEqual(fired, [])
        finally:
            manager.shutdown()

    def test_order_status_trigger_via_repo_hook(self):
        from connector.printflow.repo import Repo
        from connector.printflow.manager import PrinterManager
        from connector.printflow.rules import RulesEngine
        manager = PrinterManager(self.db, Repo(self.db))  # type: ignore[arg-type]
        try:
            engine = manager.rules
            engine.save_rule({"name": "Готов", "event": "order_status", "action": "event",
                              "config": {"status": "ready", "template": "№{number}"},
                              "enabled": 1})
            repo = manager.repo
            order = repo.save_order({"product": "адресник", "status": "new",
                                     "customer_name": "Мария"})
            repo.save_order({"id": order["id"], "status": "ready"})
            events = self.db.events(50, kind="rule")
            self.assertTrue(any(("№" + order["number"]) in (e.get("detail") or "")
                                for e in events))
        finally:
            manager.shutdown()

    def test_toggle_and_delete(self):
        manager, engine = self._engine()
        try:
            rule = engine.save_rule({"name": "T", "event": "print_pause",
                                     "action": "notify", "enabled": 1})
            engine.toggle(rule["id"], False)
            row = self.db.one("SELECT enabled FROM automation_rules WHERE id=?",
                              (rule["id"],))
            self.assertEqual(int(num(row["enabled"])), 0)
            engine.delete_rule(rule["id"])
            self.assertIsNone(self.db.one("SELECT 1 FROM automation_rules WHERE id=?",
                                          (rule["id"],)))
        finally:
            manager.shutdown()


class SecretEncryptionTests(unittest.TestCase):
    """Шифрование секретов в базе (роадмап 10.10): stdlib, прозрачная миграция."""

    def test_crypto_roundtrip(self):
        from connector.printflow import crypto
        secret = "tok_AbCd1234-очень-секретный"
        token = crypto.encrypt(secret)
        self.assertTrue(token.startswith("enc:v1:"))
        self.assertNotIn(secret, token)
        self.assertEqual(crypto.decrypt(token), secret)
        # пустая строка остаётся пустой, старый текст читается как есть
        self.assertEqual(crypto.encrypt(""), "")
        self.assertEqual(crypto.decrypt("plain-old-value"), "plain-old-value")

    def test_crypto_tamper_detected(self):
        from connector.printflow import crypto
        token = crypto.encrypt("topsecret")
        # подменяем последний символ — целостность должна нарушиться
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        # decrypt не должен вернуть исходный секрет (пусто или повреждён)
        result = crypto.decrypt(tampered)
        self.assertNotEqual(result, "topsecret")

    def test_settings_secrets_encrypted_at_rest(self):
        import tempfile, pathlib
        from connector.printflow.db import Database
        tmp = tempfile.TemporaryDirectory()
        try:
            db = Database(pathlib.Path(tmp.name) / "t.sqlite3")
            db.set_settings({"telegram_token": "BOT_SECRET_123", "cloud_token": "CLOUD_SECRET_456"})
            # в таблице — зашифровано, не открытым текстом
            raw = db.one("SELECT value FROM settings WHERE key='telegram_token'")
            self.assertNotIn("BOT_SECRET_123", str(raw))
            # чтение через API настроек расшифровывает
            secret_settings = db.settings(include_secrets=True)
            self.assertEqual(secret_settings["telegram_token"], "BOT_SECRET_123")
            self.assertEqual(secret_settings["cloud_token"], "CLOUD_SECRET_456")
            # публичное чтение — маска, без секрета
            public = db.settings()
            self.assertEqual(public["telegram_token"], "••••••••")
            self.assertTrue(public["has_telegram_token"])
            db.close()
        finally:
            tmp.cleanup()

    def test_legacy_plaintext_secret_still_readable(self):
        import tempfile, pathlib, json
        from connector.printflow.db import Database
        tmp = tempfile.TemporaryDirectory()
        try:
            db = Database(pathlib.Path(tmp.name) / "t.sqlite3")
            # имитируем старую установку: секрет открытым текстом в базе
            db.execute("INSERT INTO settings(key,value) VALUES('cloud_token', ?)"
                       " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (json.dumps("OLD_PLAIN_TOKEN"),))
            self.assertEqual(db.settings(include_secrets=True)["cloud_token"],
                             "OLD_PLAIN_TOKEN")
            db.close()
        finally:
            tmp.cleanup()

    def test_access_code_encryption_opt_in(self):
        import tempfile, pathlib
        from connector.printflow.db import Database
        from connector.printflow.repo import Repo
        tmp = tempfile.TemporaryDirectory()
        try:
            db = Database(pathlib.Path(tmp.name) / "t.sqlite3")
            repo = Repo(db)
            db.set_settings({"encrypt_access_code": True})
            row = repo.save_printer({"name": "P", "host": "1.2.3.4",
                                     "serial": "S1", "access_code": "LANCODE123"})
            raw = db.one("SELECT access_code FROM printers WHERE id=?", (row["id"],))
            self.assertNotIn("LANCODE123", str(raw["access_code"]))
            self.assertTrue(str(raw["access_code"]).startswith("enc:v1:"))
            # repo.printers(include_secrets=True) расшифровывает
            secret_row = repo.printers(include_secrets=True)
            self.assertEqual(secret_row[0]["access_code"], "LANCODE123")
            # публичное чтение — маска
            public_row = repo.printers()
            self.assertEqual(public_row[0]["access_code"], "")
            self.assertTrue(public_row[0]["has_access_code"])
            db.close()
        finally:
            tmp.cleanup()


class TelegramPhotoAndWatchTests(unittest.TestCase):
    """«фото N» и «следи N» в Telegram: привязка фото, слежка за прогрессом."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _bot(self):
        from connector.printflow.manager import PrinterManager
        from connector.printflow.telegram_bot import TelegramBot
        manager = PrinterManager(self.db, None)  # type: ignore[arg-type]
        bot = TelegramBot.__new__(TelegramBot)
        bot.manager = manager
        bot.db = self.db
        bot._watched = {}
        bot._printer_choice = {}
        bot._live = {}
        bot._pending_stop = {}
        return manager, bot

    def test_watch_order_registers(self):
        manager, bot = self._bot()
        try:
            self.db.upsert("orders", {"id": "o1", "number": "1001", "product": "x",
                                      "status": "printing"})
            reply = bot._watch_order("chat1", "1001")
            self.assertIn("Слежу", reply)
            self.assertEqual(bot._watched["chat1"]["number"], "1001")
            self.assertEqual(bot._watched["chat1"]["last_milestone"], 0)
        finally:
            manager.shutdown()

    def test_watch_order_missing(self):
        manager, bot = self._bot()
        try:
            self.assertIn("не найден", bot._watch_order("chat1", "9999"))
            self.assertEqual(bot._watched, {})
        finally:
            manager.shutdown()

    def test_maybe_watch_sends_on_milestone(self):
        manager, bot = self._bot()
        try:
            self.db.upsert("orders", {"id": "o1", "number": "1001", "product": "адресник",
                                      "status": "printing"})
            self.db.upsert("print_jobs", {"id": "j1", "order_id": "o1", "state": "running",
                                          "progress": 25})
            sent = []
            # Прогресс уходит в тот же чат, из которого попросили следить, —
            # а не в чат по умолчанию через manager.notify_async.
            bot._reply = lambda chat, text: sent.append((chat, text))
            bot._watched["chat1"] = {"number": "1001", "last_milestone": 0}
            bot._maybe_watch()
            self.assertTrue(any("20%" in t for _, t in sent))
            self.assertEqual([c for c, _ in sent], ["chat1"])
            self.assertEqual(bot._watched["chat1"]["last_milestone"], 20)
        finally:
            manager.shutdown()

    def test_attach_photo_to_latest_active_order(self):
        manager, bot = self._bot()
        try:
            self.db.upsert("orders", {"id": "o9", "number": "1009", "product": "органайзер",
                                      "status": "printing"})
            bot._download_file = lambda file_id: b"\xff\xd8\xff\xd9JPEGDATA"
            bot._reply = lambda chat, text: None
            bot._attach_photo("chat1", [{"file_id": "abc123"}], "")
            photos = self.db.query("SELECT * FROM order_photos WHERE order_id='o9'")
            self.assertEqual(len(photos), 1)
            self.assertEqual(photos[0]["note"], "фото из Telegram")
            # файл реально записан в каталог фото
            from connector.printflow.config import PHOTO_DIR
            saved = PHOTO_DIR / photos[0]["file"]
            self.assertTrue(saved.exists())
        finally:
            manager.shutdown()


class ShoppingListTests(unittest.TestCase):
    """Список закупок: ручной + автозаполнение по катушкам и темпу расхода."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_add_and_toggle_and_delete(self):
        from connector.printflow.shopping import ShoppingList
        shop = ShoppingList(self.db)
        item = shop.add({"name": "PLA чёрный", "material": "PLA", "qty": 2,
                         "unit": "кг"})
        self.assertEqual(len(shop.items()), 1)
        shop.toggle(item["id"], True)
        self.assertEqual(len(shop.items()), 0)  # купленное не в открытом списке
        self.assertEqual(len(shop.items(include_done=True)), 1)
        shop.delete(item["id"])
        self.assertEqual(len(shop.items(include_done=True)), 0)

    def test_auto_fill_low_spool(self):
        from connector.printflow.shopping import ShoppingList
        shop = ShoppingList(self.db)
        self.db.upsert("spools", {"id": "sp1", "material": "PLA", "color_name": "Чёрный",
                                  "total_grams": 1000, "remaining_grams": 80,
                                  "price": 1600, "archived": 0})
        result = shop.auto_fill()
        self.assertEqual(result["count"], 1)
        item = shop.items()[0]
        self.assertEqual(item["material"], "PLA")
        self.assertEqual(item["source"], "auto")
        self.assertIn("осталось", item["reason"])
        # повторный запуск не дублирует
        again = shop.auto_fill()
        self.assertEqual(again["count"], 0)

    def test_auto_fill_runout_rate(self):
        from connector.printflow.shopping import ShoppingList
        from connector.printflow.config import now_iso
        shop = ShoppingList(self.db)
        self.db.set_settings({"shopping_runout_days": 14.0})
        # расход 600 г за 30 дней = 20 г/день, на складе 200 г (20%, выше порога
        # «мало пластика») → хватит на 10 дней < 14 → попадает по темпу расхода.
        self.db.upsert("spools", {"id": "sp2", "material": "PETG", "color_name": "Белый",
                                  "total_grams": 1000, "remaining_grams": 200,
                                  "price": 1800, "archived": 0})
        self.db.execute(
            "INSERT INTO filament_usage(at,spool_id,grams,cost) VALUES(?,?,?,?)",
            (now_iso(), "sp2", 600.0, 10.0))
        result = shop.auto_fill()
        self.assertTrue(any(i["material"] == "PETG" for i in result["added"]))
        self.assertTrue(any("темп" in (i.get("reason") or "") for i in result["added"]))

    def test_auto_fill_dry_run(self):
        from connector.printflow.shopping import ShoppingList
        shop = ShoppingList(self.db)
        self.db.upsert("spools", {"id": "sp3", "material": "ABS", "color_name": "Серый",
                                  "total_grams": 1000, "remaining_grams": 50,
                                  "price": 1500, "archived": 0})
        result = shop.auto_fill(dry_run=True)
        self.assertTrue(result["added"])
        self.assertEqual(len(shop.items()), 0)  # dry_run ничего не записал

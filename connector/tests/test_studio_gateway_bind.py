"""Шлюз Bambu Studio: почему Studio отдавала «код=-1».

Жалоба владельца: шлюз включён, MQTT :8883 и FTPS :990 поднимаются, файлы из
Studio доходят — но добавить шлюз в Studio нельзя: «Сбой подключения …
код=-1». Причина не в протоколе MQTT и не в Access Code: до MQTT сетевой
плагин Studio выполняет пробу личности принтера — обычный TCP на :3000
(``bambu_network_bind_detect``), кадр ``A5A5 <длина> JSON A7A7`` с командой
``login/detect``. Шлюз этот порт не слушал вовсе, соединение сбрасывалось, и
Studio возвращала «socket connect failed» = -1, даже не дойдя до MQTT.

Проверяется три слоя:

1. кадр и ответ detect — без сети (bind=False);
2. объявление SSDP полями, которые плагин разбирает в JSON;
3. живой прогон: шлюз поднимается на свободных портах, а тест играет роль
   Studio — detect по :3000 и :3002/TLS, MQTT CONNECT + pushall, FTPS
   с TLS на канале данных.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.config import DEFAULT_SETTINGS  # noqa: E402
from connector.printflow.studio_gateway import (  # noqa: E402
    BIND_PORT_PLAIN,
    BIND_PORT_TLS,
    FIRMWARE_VERSION,
    SSDP_BROADCAST,
    SSDP_NT,
    SSDP_PORTS,
    StudioGateway,
    decode_bind_frame,
    encode_bind_frame,
)
from connector.printflow.studio_mqtt import (  # noqa: E402
    CONNACK,
    PUBLISH,
    decode_publish,
    encode_connect,
    encode_publish,
    read_packet,
)
from connector.tests.test_phase11 import make_db  # noqa: E402
from connector.tests.test_studio_gateway import FakeMgr  # noqa: E402

APP_JS = (ROOT / "site" / "assets" / "app.js").read_text(encoding="utf-8")

DETECT_REQUEST = {"login": {"command": "detect", "sequence_id": "20000"}}


def free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def make_cert(cn: str = "01P00ATEST") -> tuple[pathlib.Path, pathlib.Path]:
    """Настоящий самоподписанный сертификат через openssl (как в проде)."""
    if shutil.which("openssl") is None:
        raise unittest.SkipTest("openssl не найден — TLS не поднять")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pf-bind-cert-"))
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "2",
         "-subj", f"/CN={cn}"],
        check=True, capture_output=True)
    return cert, key


class BindFrameTests(unittest.TestCase):
    """Кадр порта 3000: A5A5 + длина + JSON + A7A7 — как у прошивки."""

    def test_roundtrip(self):
        frame = encode_bind_frame(DETECT_REQUEST)
        self.assertEqual(b"\xa5\xa5", frame[:2])
        self.assertEqual(b"\xa7\xa7", frame[-2:])
        self.assertEqual(len(frame), int.from_bytes(frame[2:4], "little"))
        payload, rest = decode_bind_frame(frame)
        self.assertEqual(DETECT_REQUEST, payload)
        self.assertEqual(b"", rest)

    def test_partial_frame_waits_for_the_rest(self):
        frame = encode_bind_frame(DETECT_REQUEST)
        payload, rest = decode_bind_frame(frame[:len(frame) // 2])
        self.assertIsNone(payload, "неполный кадр нельзя разбирать")
        self.assertEqual(frame[:len(frame) // 2], rest, "буфер должен сохраниться")
        payload, _rest = decode_bind_frame(rest + frame[len(frame) // 2:])
        self.assertEqual(DETECT_REQUEST, payload)

    def test_broken_tail_is_skipped(self):
        frame = bytearray(encode_bind_frame(DETECT_REQUEST))
        frame[-2:] = b"\x00\x00"
        payload, rest = decode_bind_frame(bytes(frame))
        self.assertIsNone(payload)
        self.assertEqual(bytes(frame)[2:], rest,
                         "битый кадр пропускается по два байта, как в плагине")

    def test_too_large_payload_is_refused(self):
        with self.assertRaises(ValueError):
            encode_bind_frame({"login": {"command": "detect", "param": "x" * 70000}})


class BindDetectReplyTests(unittest.TestCase):
    """Ответ шлюза на пробу личности: поля, которые ждёт плагин Studio."""

    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.mgr = FakeMgr(self.db)
        self.gw = StudioGateway(self.db, self.mgr, bind=False)
        self.mgr.studio = self.gw

    def test_detect_reply_matches_printer_contract(self):
        reply = self.gw.bind_reply(DETECT_REQUEST)
        self.assertIsNotNone(reply, "detect без ответа — Studio уходит в -1")
        login = reply["login"]
        ident = self.gw.identity()
        self.assertEqual("detect", login["command"])
        self.assertEqual(ident["serial"], login["id"])
        self.assertEqual(ident["dev_model"], login["model"])
        self.assertEqual(ident["name"], login["name"])
        self.assertEqual("free", login["bind"], "занятый принтер Studio не возьмёт")
        self.assertEqual("lan", login["connect"])
        self.assertEqual(FIRMWARE_VERSION, login["version"])
        self.assertEqual(3021, login["sequence_id"])
        self.assertIsInstance(login["sequence_id"], int,
                              "принтер отвечает числом, плагин ждёт int")

    def test_detect_counts_and_reports_in_status(self):
        self.gw.bind_handle_bytes(encode_bind_frame(DETECT_REQUEST))
        status = self.gw.status()
        self.assertEqual(1, status["bind_requests"])
        self.assertEqual(1, status["bind_detects"])

    def test_handle_bytes_returns_a_wire_frame(self):
        raw = self.gw.bind_handle_bytes(encode_bind_frame(DETECT_REQUEST))
        self.assertTrue(raw.startswith(b"\xa5\xa5"))
        payload, _rest = decode_bind_frame(raw)
        self.assertEqual("detect", payload["login"]["command"])

    def test_plain_login_answers_lan_success(self):
        """Привязка к аккаунту — облако, но локальный вход не должен висеть."""
        frame = encode_bind_frame({"login": {"command": "login", "sequence_id": "20001"}})
        payload, _rest = decode_bind_frame(self.gw.bind_handle_bytes(frame))
        self.assertEqual("login_report", payload["login"]["command"])
        self.assertEqual("SUCCESS", payload["login"]["status"])

    def test_unknown_command_gets_no_answer(self):
        frame = encode_bind_frame({"login": {"command": "что-то ещё"}})
        self.assertEqual(b"", self.gw.bind_handle_bytes(frame))

    def test_status_reports_bind_service(self):
        status = self.gw.status()
        self.assertEqual(BIND_PORT_PLAIN, status["bind_port"])
        self.assertEqual(BIND_PORT_TLS, status["bind_tls_port"])
        self.assertIn("bind_running", status)
        self.assertIn("bind", self.gw._errors, "у сервиса своя ячейка ошибки")

    def test_certificate_is_issued_for_the_serial(self):
        """Сертификат выписывается на серийник (leaf CN=<serial>, как у станка)."""
        with tempfile.TemporaryDirectory(prefix="pf-cn-") as tmp:
            cert_dir = pathlib.Path(tmp)
            paths = {
                "CERT_DIR": cert_dir,
                "CERT_FILE": cert_dir / "cert.pem",
                "KEY_FILE": cert_dir / "key.pem",
                "CN_FILE": cert_dir / "cert.pem.cn",
            }
            with mock.patch.multiple("connector.printflow.studio_tls", **paths):
                from connector.printflow import studio_tls
                cert, _key = studio_tls.ensure_certificate("01P00ATEST")
                self.assertEqual("01P00ATEST", studio_tls.stored_cn())
                first = cert.read_bytes()
                # Другая личность (новый серийник) — сертификат перевыпускается.
                studio_tls.ensure_certificate("01P00AOTHER")
                self.assertEqual("01P00AOTHER", studio_tls.stored_cn())
                self.assertNotEqual(first, cert.read_bytes())


class SsdpAnnounceTests(unittest.TestCase):
    """Объявление, из которого плагин собирает JSON для DeviceManager."""

    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.mgr = FakeMgr(self.db)
        self.gw = StudioGateway(self.db, self.mgr, bind=False)
        self.mgr.studio = self.gw

    def test_notify_has_every_field_studio_parses(self):
        text = self.gw.ssdp_notify()
        for header in ("DevModel.bambu.com:", "DevName.bambu.com:",
                       "DevConnect.bambu.com:", "DevBind.bambu.com:",
                       "Devseclink.bambu.com:", "DevInf.bambu.com:",
                       "DevVersion.bambu.com:", "DevCap.bambu.com:"):
            self.assertIn(header, text, f"Studio не увидит {header}")
        self.assertIn("Location:", text)
        self.assertIn(f"DevVersion.bambu.com: {FIRMWARE_VERSION}", text)

    def test_search_response_has_the_same_fields(self):
        text = self.gw.ssdp_search_response()
        for header in ("DevVersion.bambu.com:", "DevCap.bambu.com:",
                       "Devseclink.bambu.com:", "DevInf.bambu.com:"):
            self.assertIn(header, text)

    def test_notify_goes_to_broadcast_2021_like_a_printer(self):
        """Станки шлют NOTIFY на 255.255.255.255:2021 — там его и ждут."""
        sent: list[tuple[str, int]] = []

        class FakeUdp:
            def __init__(self, *_a, **_k):
                pass

            def setsockopt(self, *_a, **_k):
                return None

            def sendto(self, payload, addr):
                sent.append(addr)

            def close(self):
                return None

        self.gw._last_notify = 0.0
        with mock.patch("connector.printflow.studio_gateway.socket.socket", FakeUdp):
            self.gw._broadcast_notify()
        self.assertIn((SSDP_BROADCAST, 2021), sent,
                      "без широковещательной рассылки Studio шлюз не увидит")
        self.assertIn((SSDP_BROADCAST, SSDP_PORTS[0]), sent)
        for port in SSDP_PORTS:
            self.assertIn(("239.255.255.250", port), sent)

    def test_notify_period_is_printer_like(self):
        from connector.printflow import studio_gateway as module
        self.assertLessEqual(module.SSDP_NOTIFY_PERIOD, 6.0,
                             "плагин слушает одно-два объявления, реже — не увидит")


class MqttReportTests(unittest.TestCase):
    """Отчёты, без которых Studio не считает принтер доступным."""

    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.db.set_settings({"studio_gateway_access_code": "abcd1234"})
        self.mgr = FakeMgr(self.db)
        self.gw = StudioGateway(self.db, self.mgr, bind=False)
        self.mgr.studio = self.gw

    def test_pushall_returns_a_full_snapshot(self):
        reports = self.gw.handle_mqtt_request(
            {"pushing": {"command": "pushall", "sequence_id": "0"}})
        self.assertTrue(reports)
        push = reports[0]["print"]
        self.assertEqual("push_status", push["command"])
        self.assertEqual(0, push["msg"], "msg: 0 — полный снимок, а не diff")

    def test_get_access_code_returns_the_code(self):
        reports = self.gw.handle_mqtt_request(
            {"system": {"command": "get_access_code", "sequence_id": "2"}})
        self.assertEqual("abcd1234", reports[0]["system"]["access_code"])


class LiveStudioHandshakeTests(unittest.TestCase):
    """Живой прогон: тест играет Studio, шлюз работает на свободных портах."""

    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.db.set_settings({
            "studio_gateway_enabled": True,
            "studio_gateway_access_code": "abcd1234",
            "studio_gateway_host": "127.0.0.1",
            # confirm — режим по умолчанию: файл ждёт оператора, а не очередь.
            # Здесь сквозной прогон, поэтому задания идут сразу в очередь.
            "studio_gateway_mode": "queue",
        })
        self.mgr = FakeMgr(self.db)
        self.gw = StudioGateway(self.db, self.mgr, bind=True)
        self.mgr.studio = self.gw
        self.cert, self.key = make_cert()
        self.addCleanup(shutil.rmtree, self.cert.parent, ignore_errors=True)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        self.ports = {
            "MQTT_PORT": free_port(),
            "FTP_PORT": free_port(),
            "BIND_PORT_PLAIN": free_port(),
            "BIND_PORT_TLS": free_port(),
        }
        patches = [
            mock.patch(f"connector.printflow.studio_gateway.{name}", port)
            for name, port in self.ports.items()
        ]
        patches.append(mock.patch("connector.printflow.studio_tls.ensure_certificate",
                                  return_value=(self.cert, self.key)))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.gw.start()
        self.addCleanup(self.gw.stop)

    # ------------------------------------------------------------- клиенты
    def detect(self, port: int, tls: bool = False) -> dict:
        """Ровно то, что делает плагин Studio на порту 3000/3002."""
        raw = socket.create_connection(("127.0.0.1", port), timeout=10)
        conn = self.ctx.wrap_socket(raw) if tls else raw
        try:
            conn.sendall(encode_bind_frame(DETECT_REQUEST))
            payload, _rest = decode_bind_frame(conn.recv(4096))
        finally:
            conn.close()
        return payload or {}

    def mqtt(self, request: dict) -> dict:
        ident = self.gw.identity()
        raw = socket.create_connection(("127.0.0.1", self.ports["MQTT_PORT"]), timeout=10)
        conn = self.ctx.wrap_socket(raw)
        try:
            conn.sendall(encode_connect(client_id="studio", username="bblp",
                                        password="abcd1234"))
            ptype, _flags, payload = read_packet(conn.recv)
            self.assertEqual(CONNACK, ptype)
            self.assertEqual(0, payload[1], f"CONNACK отклонил вход: {payload[1]}")
            conn.sendall(encode_publish(f"device/{ident['serial']}/request",
                                        json.dumps(request)))
            deadline = time.time() + 10
            while time.time() < deadline:
                ptype, flags, payload = read_packet(conn.recv)
                if ptype != PUBLISH:
                    continue
                published = decode_publish(flags, payload)
                body = json.loads(published["payload"].decode("utf-8"))
                if "print" in body:
                    return body
            raise AssertionError("шлюз не ответил на запрос")
        finally:
            conn.close()

    def ftps_upload(self, filename: str, blob: bytes, tls_data: bool = True) -> str:
        """implicit FTPS: USER/PASS/TYPE/PASV/STOR, канал данных по PASV."""
        raw = socket.create_connection(("127.0.0.1", self.ports["FTP_PORT"]), timeout=10)
        conn = self.ctx.wrap_socket(raw)
        try:
            self._expect(conn, "220")
            self._cmd(conn, "USER bblp", "331")
            self._cmd(conn, "PASS abcd1234", "230")
            self._cmd(conn, "TYPE I", "200")
            pasv = self._cmd(conn, "PASV", "227")
            numbers = re.search(r"\((\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\)", pasv)
            self.assertIsNotNone(numbers, f"PASV без адреса: {pasv}")
            host = ".".join(numbers.group(i) for i in range(1, 5))
            port = int(numbers.group(5)) * 256 + int(numbers.group(6))
            data_raw = socket.create_connection((host, port), timeout=10)
            data = self.ctx.wrap_socket(data_raw) if tls_data else data_raw
            try:
                self._cmd(conn, f"STOR {filename}", "150")
                data.sendall(blob)
                if hasattr(data, "unwrap"):
                    try:
                        data.unwrap()
                    except (OSError, ValueError):
                        pass
            finally:
                data.close()
            return self._expect(conn, "226")
        finally:
            conn.close()

    def _expect(self, conn, code: str) -> str:
        line = b""
        deadline = time.time() + 10
        while time.time() < deadline and not line.endswith(b"\n"):
            chunk = conn.recv(1)
            if not chunk:
                break
            line += chunk
        text = line.decode("utf-8", "replace").strip()
        self.assertTrue(text.startswith(code), f"ждали {code}, пришло: {text!r}")
        return text

    def _cmd(self, conn, command: str, code: str) -> str:
        conn.sendall((command + "\r\n").encode("utf-8"))
        return self._expect(conn, code)

    # ------------------------------------------------------------- прогон
    def test_studio_finds_gateway_on_3000_before_mqtt(self):
        """Главная причина «код=-1»: шлюз отвечает на пробу личности."""
        reply = self.detect(self.ports["BIND_PORT_PLAIN"])
        ident = self.gw.identity()
        self.assertEqual("detect", reply.get("login", {}).get("command"))
        self.assertEqual(ident["serial"], reply["login"]["id"])
        self.assertEqual("free", reply["login"]["bind"])
        self.assertTrue(self.gw.status()["bind_running"])

    def test_studio_finds_gateway_on_3002_over_tls(self):
        reply = self.detect(self.ports["BIND_PORT_TLS"], tls=True)
        self.assertEqual("detect", reply.get("login", {}).get("command"))
        self.assertEqual(self.gw.identity()["serial"], reply["login"]["id"])

    def test_studio_discovers_gateway_by_m_search(self):
        """Живой M-SEARCH: шлюз отвечает объявлением, которое парсит плагин."""
        sock = self.gw._ssdp_sock
        self.assertIsNotNone(sock, "SSDP не поднялся")
        port = sock.getsockname()[1]
        ident = self.gw.identity()
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        probe.settimeout(10)
        try:
            request = (
                "M-SEARCH * HTTP/1.1\r\n"
                f"HOST: 239.255.255.250:{port}\r\n"
                'MAN: "ssdp:discover"\r\n'
                f"ST: {SSDP_NT}\r\n"
                "MX: 1\r\n\r\n"
            )
            probe.sendto(request.encode("utf-8"), ("127.0.0.1", port))
            data, _addr = probe.recvfrom(4096)
        finally:
            probe.close()
        text = data.decode("utf-8", "replace")
        self.assertIn("HTTP/1.1 200 OK", text)
        self.assertIn(ident["serial"], text, "серийник не вернулся в ответе")
        self.assertIn("DevVersion.bambu.com:", text)
        self.assertIn("DevCap.bambu.com: 1", text)
        self.assertIn("Devseclink.bambu.com: secure", text)

    def test_mqtt_connack_and_pushall(self):
        body = self.mqtt({"pushing": {"command": "pushall", "sequence_id": "0"}})
        self.assertEqual("IDLE", body["print"]["gcode_state"])
        self.assertEqual(0, body["print"]["msg"])

    def test_ftps_upload_with_tls_data_channel(self):
        blob = b"3MF-BYTES-FROM-STUDIO"
        self.ftps_upload("plate.gcode.3mf", blob)
        self.assertEqual(1, len(self.mgr.enqueued),
                         "файл из Studio не попал в очередь")
        self.assertEqual("studio-gateway", self.mgr.enqueued[-1]["source"])

    def test_ftps_upload_with_plain_data_channel(self):
        """Часть клиентов не оборачивает канал данных в TLS — и так работает."""
        self.ftps_upload("plain.gcode.3mf", b"PLAIN-DATA-CHANNEL", tls_data=False)
        self.assertEqual(1, len(self.mgr.enqueued))


class SettingsKeysContractTests(unittest.TestCase):
    """Ключи из карточки настроек обязаны существовать в схеме.

    «Сетевой порт шлюза» (studio_gateway_port) рисовался в карточке, но не
    был известен конфигу: сохранение молча отбрасывало ключ, и владелец
    видел лишь «проигнорированы неизвестные ключи» в журнале.
    """

    def test_every_studio_key_exists_in_config(self):
        match = re.search(r"const STUDIO = \[(.*?)\n\];", APP_JS, re.S)
        self.assertTrue(match, "группа STUDIO пропала из app.js")
        keys = re.findall(r"^  \['([a-z0-9_]+)',", match.group(1), re.M)
        self.assertTrue(keys, "карточка шлюза пустая")
        unknown = [key for key in keys if key not in DEFAULT_SETTINGS]
        self.assertEqual([], unknown,
                         f"ключи есть в форме, но их нет в настройках: {unknown}")

    def test_dead_port_setting_is_gone(self):
        self.assertNotIn("studio_gateway_port", APP_JS,
                         "порт шлюза не настраивается: Studio использует 3000/3002/8883/990")
        self.assertNotIn("studio_gateway_port", DEFAULT_SETTINGS)

    def test_browser_notify_and_default_location_are_real_settings(self):
        """Оба ключа рисуются в панели — теперь они и сохраняются."""
        for key in ("browser_notify_enabled", "default_location"):
            self.assertIn(key, DEFAULT_SETTINGS, f"{key} по-прежнему не в конфиге")
            self.assertIn(f"'{key}'", APP_JS, f"{key} нет в разметке настроек")


if __name__ == "__main__":
    unittest.main()

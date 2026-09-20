"""Шлюз Bambu Studio 18.11: рукопожатие TLS вне accept() и ретранслятор SSDP.

Жалоба владельца: Studio показывает «код=-1» на исправном шлюзе. Разбор
показал две независимые причины, и обе закрываются здесь.

1. **Молчащий TCP-клиент блокировал приём.** Слушающая розетка была
   TLS-обёрткой, и ``accept()`` выполнял рукопожатие сам — блокирующе, в
   потоке приёма. Сканер портов, антивирус или «проверка, открыт ли порт»
   открывали TCP и молчали; Studio, пришедшая следом, ждала таймаут. Теперь
   ``accept()`` принимает чистый TCP, рукопожатие делает поток клиента с
   дедлайном :data:`TLS_HANDSHAKE_TIMEOUT`, а контекст TLS один на все
   каналы — Studio возобновляет сессию на FTPS-канале данных.

2. **Studio в другой подсети не видит станки.** Широковещательный SSDP
   реального принтера не маршрутизируется через VLAN/роутер. Ретранслятор
   объявляет Studio реальные станки фермы (адрес + серийник из вкладки
   «Принтеры») теми же полями, что шлёт станок, и шлёт unicast на адреса
   компьютеров со Studio (UDP :2021). Кнопка «Объявить сейчас» — внеочередная
   рассылка.

Живые тесты играют Studio на свободных портах (как в
``test_studio_gateway_bind``); сертификат — настоящий, через openssl.
"""
from __future__ import annotations

import ftplib
import io
import pathlib
import shutil
import socket
import ssl
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.config import DEFAULT_SETTINGS  # noqa: E402
from connector.printflow.router import router  # noqa: E402
from connector.printflow.studio_gateway import (  # noqa: E402
    SSDP_NT,
    StudioGateway,
)
from connector.tests.test_phase11 import make_api, make_db  # noqa: E402
from connector.tests.test_studio_gateway import FakeMgr  # noqa: E402
from connector.tests.test_studio_gateway_bind import free_port, make_cert  # noqa: E402


def _gateway(db, **settings) -> StudioGateway:
    base = {
        "studio_gateway_enabled": True,
        "studio_gateway_access_code": "abcd1234",
        "studio_gateway_host": "127.0.0.1",
        "studio_gateway_mode": "queue",
    }
    base.update(settings)
    db.set_settings(base)
    mgr = FakeMgr(db)
    gw = StudioGateway(db, mgr, bind=True)
    mgr.studio = gw
    return gw


class SettingsContractTests(unittest.TestCase):
    def test_relay_settings_exist_and_are_described(self):
        from connector.printflow.settings_schema import describe
        self.assertIn("studio_relay_enabled", DEFAULT_SETTINGS)
        self.assertIn("studio_relay_targets", DEFAULT_SETTINGS)
        self.assertFalse(DEFAULT_SETTINGS["studio_relay_enabled"],
                         "ретранслятор выключен по умолчанию: дубли объявлений в своей подсети не нужны")
        fields = {item["key"]: item
                  for rows in describe()["fields"].values() for item in rows}
        for key in ("studio_relay_enabled", "studio_relay_targets"):
            self.assertIn(key, fields)
            self.assertEqual("printers", fields[key].get("group"))
            self.assertTrue(fields[key].get("label"))
            self.assertTrue(fields[key].get("hint"), f"{key}: подсказка обязательна — иначе настройка непонятна")

    def test_status_has_new_fields_even_without_start(self):
        db = make_db()
        self.addCleanup(db.close)
        gw = _gateway(db, studio_gateway_enabled=False)
        status = gw.status()
        for key in ("relay_enabled", "relay_targets", "relay_printers", "relay_sent", "relay_note",
                    "tls_handshakes", "tls_resumed", "tls_handshake_timeouts",
                    "tls_handshake_timeout", "tls_sessions"):
            self.assertIn(key, status)
        self.assertEqual({}, status["tls_sessions"], "контекста ещё нет — пустая сводка, не падение")


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)

    def add_printer(self, pid, name, host, serial, enabled=1, model="P1S"):
        self.db.upsert("printers", {"id": pid, "name": name, "model": model, "host": host,
                                    "serial": serial, "enabled": enabled})

    def test_targets_parse_and_filter_garbage(self):
        gw = _gateway(self.db, studio_relay_targets=" 10.0.2.17, 192.168.10.5;мусор\n1.2.3 10.0.2.17 300.1.1.1")
        self.assertEqual(["10.0.2.17", "192.168.10.5"], gw.relay_targets())
        targets = gw._notify_targets()
        self.assertIn(("10.0.2.17", 2021), targets)
        self.assertIn(("192.168.10.5", 2021), targets)
        self.assertEqual(len(targets), len(set(targets)), "адресаты без дублей")

    def test_relay_printers_take_real_machines_only(self):
        self.add_printer("p1", "P1S-цех", "192.168.1.77", "01p00a123456789")
        self.add_printer("p2", "Без адреса", "", "01P00A000000001")
        self.add_printer("p3", "Без серийника", "192.168.1.78", "")
        self.add_printer("p4", "Выключен", "192.168.1.79", "01P00A000000004", enabled=0)
        self.add_printer("virtual", "NOZZA tour", "127.0.0.1", "VIRTUAL0001")
        gw = _gateway(self.db, studio_relay_enabled=True)
        items = gw.relay_printers()
        self.assertEqual(["p1"], [item["id"] for item in items])
        self.assertEqual("01P00A123456789", items[0]["serial"], "серийник — в верхнем регистре, как у станка")
        self.assertEqual("C12", items[0]["dev_model"])

    def test_notify_for_printer_looks_like_the_printer(self):
        self.add_printer("p1", "P1S-цех", "192.168.1.77", "01P00A123456789", model="X1C")
        gw = _gateway(self.db, studio_relay_enabled=True)
        text = gw.ssdp_notify_for(gw.relay_printers()[0])
        self.assertTrue(text.startswith("NOTIFY * HTTP/1.1\r\n"))
        self.assertIn("Location: 192.168.1.77\r\n", text)
        self.assertIn("USN: 01P00A123456789\r\n", text)
        self.assertIn(f"NT: {SSDP_NT}\r\n", text)
        self.assertIn("DevModel.bambu.com: BL-P001\r\n", text)
        self.assertIn("DevName.bambu.com: P1S-цех\r\n", text)
        self.assertIn("NTS: ssdp:alive\r\n", text)
        self.assertTrue(text.endswith("\r\n\r\n"))

    def test_relay_report_explains_empty_states(self):
        gw = _gateway(self.db, studio_relay_enabled=True)
        report = gw.relay_report()
        self.assertTrue(report["enabled"])
        self.assertIn("нечего", report["note"])
        self.add_printer("p1", "P1S-цех", "192.168.1.77", "01P00A123456789")
        report = gw.relay_report()
        self.assertIn("адресаты не заданы", report["note"])
        self.db.set_settings({"studio_relay_targets": "10.0.2.17"})
        self.assertEqual("", gw.relay_report()["note"])
        self.db.set_settings({"studio_relay_enabled": False})
        report = gw.relay_report()
        self.assertFalse(report["enabled"])
        self.assertEqual([], report["printers"], "выключен — станки не перечисляем и не читаем")

    def test_broadcast_sends_relay_payloads_to_every_target(self):
        self.add_printer("p1", "P1S-цех", "192.168.1.77", "01P00A123456789")
        self.add_printer("p2", "X1C", "192.168.1.78", "00M00A000000002", model="X1C")
        gw = _gateway(self.db, studio_relay_enabled=True, studio_relay_targets="10.0.2.17")
        sent: list[tuple[bytes, tuple]] = []
        fake_sock = object()
        with mock.patch.object(gw, "_socket_for", return_value=(fake_sock, False)), \
             mock.patch.object(gw, "_sendto_lan", side_effect=lambda s, payload, addr: sent.append((payload, addr)) or True), \
             mock.patch.object(gw, "_warn_vpn"):
            gw._broadcast_notify(force=True)
        targets = gw._notify_targets()
        own = [addr for payload, addr in sent if b"USN: " + gw.identity()["serial"].encode() in payload]
        relayed = [addr for payload, addr in sent if b"USN: 01P00A123456789" in payload]
        self.assertEqual(len(targets), len(own), "объявление шлюза — на каждый адрес")
        self.assertEqual(len(targets), len(relayed), "объявление станка — на те же адреса")
        self.assertIn(("10.0.2.17", 2021), relayed)
        status = gw.status()
        self.assertEqual(len(targets), status["ssdp_notify"])
        self.assertEqual(2 * len(targets), status["relay_sent"])
        self.assertEqual(["p1", "p2"], [item["id"] for item in status["relay_printers"]])

    def test_broadcast_respects_period_unless_forced(self):
        gw = _gateway(self.db)
        calls = []
        with mock.patch.object(gw, "_socket_for", return_value=(object(), False)), \
             mock.patch.object(gw, "_sendto_lan", side_effect=lambda *a: calls.append(a) or True), \
             mock.patch.object(gw, "_warn_vpn"):
            gw._broadcast_notify()
            first = len(calls)
            gw._broadcast_notify()
            self.assertEqual(first, len(calls), "период не вышел — повторной рассылки нет")
            gw._broadcast_notify(force=True)
            self.assertEqual(2 * first, len(calls), "force игнорирует период")

    def test_announce_now_reports_counts_and_refuses_when_disabled(self):
        self.add_printer("p1", "P1S-цех", "192.168.1.77", "01P00A123456789")
        gw = _gateway(self.db, studio_relay_enabled=True, studio_relay_targets="10.0.2.17")
        with mock.patch.object(gw, "_socket_for", return_value=(object(), False)), \
             mock.patch.object(gw, "_sendto_lan", return_value=True), \
             mock.patch.object(gw, "_warn_vpn"):
            result = gw.announce_now()
        self.assertTrue(result["ok"])
        self.assertEqual(len(gw._notify_targets()), result["sent"])
        self.assertEqual(result["sent"], result["relay"])
        self.assertEqual(["10.0.2.17"], result["relay_targets"])
        self.assertEqual(1, result["printers"])
        self.db.set_settings({"studio_gateway_enabled": False})
        result = gw.announce_now()
        self.assertFalse(result["ok"])
        self.assertIn("выключен", result["error"])

    def test_announce_route(self):
        import connector.printflow.routes_printers  # noqa: F401
        api = make_api(self.db)
        code, data = router.dispatch(api, "POST", "/api/studio/announce", body={})
        self.assertEqual(400, code, "шлюз не создан — понятная ошибка, не 500")
        gw = _gateway(self.db)
        api.manager.studio = gw
        with mock.patch.object(gw, "_socket_for", return_value=(object(), False)), \
             mock.patch.object(gw, "_sendto_lan", return_value=True), \
             mock.patch.object(gw, "_warn_vpn"):
            code, data = router.dispatch(api, "POST", "/api/studio/announce", body={})
        self.assertEqual(200, code)
        self.assertTrue(data["ok"])
        self.assertGreater(data["sent"], 0)
        self.db.set_settings({"studio_gateway_enabled": False})
        code, data = router.dispatch(api, "POST", "/api/studio/announce", body={})
        self.assertEqual(400, code)
        self.assertIn("error", data)


class LiveTlsTests(unittest.TestCase):
    """Живой прогон: молчун на порту, Studio рядом, возобновление сессии."""

    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.gw = _gateway(self.db)
        self.cert, self.key = make_cert()
        self.addCleanup(shutil.rmtree, self.cert.parent, ignore_errors=True)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        self.ports = {"MQTT_PORT": free_port(), "FTP_PORT": free_port(),
                      "BIND_PORT_PLAIN": free_port(), "BIND_PORT_TLS": free_port()}
        patches = [mock.patch(f"connector.printflow.studio_gateway.{name}", port)
                   for name, port in self.ports.items()]
        patches.append(mock.patch("connector.printflow.studio_gateway.SSDP_LISTEN_PORTS", (free_port(),)))
        patches.append(mock.patch("connector.printflow.studio_tls.ensure_certificate",
                                  return_value=(self.cert, self.key)))
        patches.append(mock.patch("connector.printflow.studio_gateway.TLS_HANDSHAKE_TIMEOUT", 1.5))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.gw.start()
        self.addCleanup(self.gw.stop)

    def test_listening_sockets_are_plain_tcp_and_context_is_shared(self):
        self.assertIsNotNone(self.gw._tls_ctx)
        for sock in (self.gw._mqtt_sock, self.gw._ftp_sock, *self.gw._bind_socks):
            self.assertNotIsInstance(sock, ssl.SSLSocket,
                                     "слушающая розетка — обычный TCP: рукопожатие делает поток клиента")

    def test_silent_client_does_not_block_studio(self):
        silent = socket.create_connection(("127.0.0.1", self.ports["MQTT_PORT"]), timeout=5)
        self.addCleanup(silent.close)
        time.sleep(0.2)
        started = time.time()
        raw = socket.create_connection(("127.0.0.1", self.ports["MQTT_PORT"]), timeout=5)
        conn = self.ctx.wrap_socket(raw)
        try:
            self.assertLess(time.time() - started, 1.0,
                            "рукопожатие Studio ждало молчуна — приём снова блокируется")
            self.assertTrue(conn.version())
        finally:
            conn.close()
        # Молчун отваливается по дедлайну и считается отдельно.
        deadline = time.time() + 5
        while time.time() < deadline and self.gw.status()["tls_handshake_timeouts"] < 1:
            time.sleep(0.1)
        status = self.gw.status()
        self.assertEqual(1, status["tls_handshake_timeouts"])
        self.assertGreaterEqual(status["dropped_connections"], 1)
        self.assertGreaterEqual(status["tls_handshakes"], 1)
        self.assertEqual("", status["errors"].get("mqtt", ""), "молчун — не сбой службы")

    def test_plain_probe_is_dropped_and_service_continues(self):
        probe = socket.create_connection(("127.0.0.1", self.ports["FTP_PORT"]), timeout=5)
        probe.sendall(b"GET / HTTP/1.0\r\n\r\n")
        probe.close()
        time.sleep(0.3)
        raw = socket.create_connection(("127.0.0.1", self.ports["FTP_PORT"]), timeout=5)
        conn = self.ctx.wrap_socket(raw)
        try:
            self.assertTrue(conn.recv(64).startswith(b"220"), "после мусорного клиента FTPS жив")
        finally:
            conn.close()
        self.assertGreaterEqual(self.gw.status()["dropped_connections"], 1)

    def test_studio_like_ftps_upload_reuses_session_on_data_channel(self):
        """Заливка как у Studio: неявный TLS на управлении, PROT P, PASV-канал
        данных с возобновлением сессии управления (ftplib так и делает)."""
        received: dict = {}
        self.gw.ftp_apply = lambda name, blob: received.__setitem__(name, blob)

        class ImplicitFTPS(ftplib.FTP_TLS):
            def __init__(self, *a, **k):
                self._sock = None
                super().__init__(*a, **k)

            @property
            def sock(self):
                return self._sock

            @sock.setter
            def sock(self, value):
                if value is not None and not isinstance(value, ssl.SSLSocket):
                    value = self.context.wrap_socket(value)
                self._sock = value

            def makepasv(self):
                _host, port = super().makepasv()
                return "127.0.0.1", port

            def ntransfercmd(self, cmd, rest=None):
                """Канал данных с возобновлением сессии управления.

                Python 3.11 ещё не передаёт ``session`` в ftplib (это 3.12+),
                поэтому возобновление делаем вручную — как Studio: управление
                открыто, канал данных продолжает его сессию.
                """
                conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
                conn = self.context.wrap_socket(
                    conn, server_hostname=self.host, session=self.sock.session)
                self._last_data_reused = conn.session_reused
                return conn, size

        blob = b"PK\x03\x04" + bytes(range(256)) * 300
        client = ImplicitFTPS(context=self.ctx, timeout=8)
        client.connect("127.0.0.1", self.ports["FTP_PORT"])
        try:
            client.login("bblp", "abcd1234")
            client.prot_p()
            client.storbinary("STOR /cache/деталь.3mf", io.BytesIO(blob))
        finally:
            try:
                client.quit()
            except Exception:
                client.close()
        self.assertEqual(received.get("деталь.3mf"), blob)
        deadline = time.time() + 3
        while time.time() < deadline and self.gw.status()["tls_resumed"] < 1:
            time.sleep(0.05)
        status = self.gw.status()
        self.assertGreaterEqual(status["tls_resumed"], 1,
                                "канал данных обязан возобновить сессию управления")
        self.assertGreaterEqual(status["ftp_uploads"], 1)

    def test_session_resumes_across_channels(self):
        raw = socket.create_connection(("127.0.0.1", self.ports["FTP_PORT"]), timeout=5)
        conn = self.ctx.wrap_socket(raw)
        conn.recv(64)                      # баннер: с ним приходит и тикет TLS 1.3
        session = conn.session
        conn.close()
        raw2 = socket.create_connection(("127.0.0.1", self.ports["BIND_PORT_TLS"]), timeout=5)
        conn2 = self.ctx.wrap_socket(raw2, session=session)
        try:
            self.assertTrue(conn2.session_reused,
                            "общий контекст обязан возобновлять сессию между каналами")
        finally:
            conn2.close()
        deadline = time.time() + 3
        while time.time() < deadline and self.gw.status()["tls_resumed"] < 1:
            time.sleep(0.05)
        status = self.gw.status()
        self.assertGreaterEqual(status["tls_resumed"], 1)
        self.assertGreaterEqual(status["tls_sessions"].get("hits", 0), 1)

    def test_many_silent_clients_at_once(self):
        """Десять молчунов разом — Studio всё равно проходит сразу."""
        silents = [socket.create_connection(("127.0.0.1", self.ports["BIND_PORT_TLS"]), timeout=5)
                   for _ in range(10)]
        for item in silents:
            self.addCleanup(item.close)
        results: list[float] = []

        def studio():
            t0 = time.time()
            raw = socket.create_connection(("127.0.0.1", self.ports["BIND_PORT_TLS"]), timeout=5)
            conn = self.ctx.wrap_socket(raw)
            results.append(time.time() - t0)
            conn.close()

        worker = threading.Thread(target=studio)
        worker.start()
        worker.join(5)
        self.assertEqual(1, len(results), "рукопожатие не завершилось")
        self.assertLess(results[0], 1.0)


if __name__ == "__main__":
    unittest.main()

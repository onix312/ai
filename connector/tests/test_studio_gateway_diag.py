"""Диагностика шлюза Bambu Studio: адрес, счётчики, журналирование сбоев.

Жалоба владельца: «Сбой подключения к NOZZA-PrintFlow, код=-1», при этом
рукопожатие шлюза корректно. Причины почти всегда в окружении Windows, и
раньше их было не видно: статус отдавал одно общее «running», last_error
перезаписывался следующим сервисом, а отказ авторизации не считался вовсе.

Проверяется поведение (без сети, bind=False): закреплённый адрес в SSDP и
PASV, флаги сервисов в статусе, рост счётчиков при отказе авторизации по
MQTT и FTP, запись сбоя старта в журнал коннектора.
"""
from __future__ import annotations

import json
import logging
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.studio_mqtt import encode_connect  # noqa: E402
from connector.printflow.studio_gateway import StudioGateway  # noqa: E402
from connector.tests.test_phase11 import make_db  # noqa: E402
from connector.tests.test_studio_gateway import FakeMgr  # noqa: E402


def mqtt_connect_packet(username: str, password: str) -> bytes:
    return encode_connect(client_id="01P00ATEST", username=username,
                          password=password)


class StudioCardMarkupTests(unittest.TestCase):
    """Карточка «Шлюз Bambu Studio» показывает адрес и состояние сервисов.

    Строковый контракт: рендер карточки живёт в браузере, а заглушка стенда
    panel-check отвечает цепочкой на любой querySelector (docs/ТЕСТЫ.md, п. 4).
    """

    def test_status_renderer_shows_host_and_service_marks(self):
        app_js = (ROOT / "site" / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn("/api/studio/status", app_js)
        self.assertIn("host_pinned", app_js, "карточка не отличает закреплённый адрес")
        self.assertIn("mqtt_running", app_js, "карточка не показывает состояние MQTT")
        self.assertIn("ftp_running", app_js, "карточка не показывает состояние FTPS")
        self.assertIn("ssdp_running", app_js, "карточка не показывает состояние SSDP")
        self.assertIn("код не подошёл", app_js,
                      "карточка не рассказывает про отказы авторизации")
        self.assertIn("ssdp_targets", app_js,
                      "карточка не показывает, куда уходит объявление Studio")
        self.assertIn("127.0.0.1:2021", app_js,
                      "карточка не объясняет, что Studio на этом же ПК видит шлюз "
                      "по loopback — это главный обход брандмауэра")

    def test_settings_form_has_pinned_host_row(self):
        app_js = (ROOT / "site" / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn("['studio_gateway_host'", app_js,
                      "поля «Адрес шлюза в сети» нет в группе STUDIO")


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.db.set_settings({"studio_gateway_access_code": "abcd1234"})
        self.mgr = FakeMgr(self.db)
        self.gw = StudioGateway(self.db, self.mgr, bind=False)
        self.mgr.studio = self.gw

    # ------------------------------------------------- закреплённый адрес
    def test_host_ip_respects_pinned_address(self):
        """studio_gateway_host важнее автодетекта: Studio уводится на реальный IP."""
        self.db.set_settings({"studio_gateway_host": "192.168.50.7"})
        self.assertEqual("192.168.50.7", self.gw._host_ip())
        self.assertEqual("192.168.50.7", self.gw.identity()["host"])
        self.assertTrue(self.gw.status()["host_pinned"])

    def test_pinned_address_flows_to_ssdp_and_pasv(self):
        """Адрес из настройки уходит и в SSDP Location, и в ответ PASV.

        Иначе Studio находит шлюз по одному адресу, а файл заливает на другой.
        """
        self.db.set_settings({"studio_gateway_host": "192.168.50.7"})
        notify = self.gw.ssdp_notify()
        self.assertIn("Location: 192.168.50.7", notify)
        response = self.gw.ssdp_search_response()
        self.assertIn("Location: 192.168.50.7", response)
        pasv = self.gw.ftp_command("PASV")
        self.assertTrue(pasv.startswith("227 "), pasv)
        self.assertIn("(192,168,50,7,", pasv)

    def test_empty_host_falls_back_to_auto(self):
        """Пустая настройка — прежнее поведение (авто), без пометки «закреплён»."""
        self.assertEqual("127.0.0.1", self.gw._host_ip())
        self.assertFalse(self.gw.status()["host_pinned"])
        self.assertIn("Location: 127.0.0.1", self.gw.ssdp_notify())

    def test_bogus_host_is_ignored_and_reported(self):
        """Опечатка в адресе не ломает шлюз: авто-адрес остаётся, причина видна."""
        self.db.set_settings({"studio_gateway_host": "192.168.50.999"})
        self.assertEqual("127.0.0.1", self.gw._host_ip())
        status = self.gw.status()
        self.assertTrue(status["host_pinned"], "настройка задана — пометка честно стоит")
        self.assertIn("host", status["errors"], "опечатка в адресе не видна в статусе")

    # ------------------------------------------------- флаги сервисов
    def test_status_reports_each_service_separately(self):
        """В статусе видно MQTT/FTPS/SSDP по отдельности, а не одно «running»."""
        status = self.gw.status()
        for key in ("mqtt_running", "ftp_running", "ssdp_running"):
            self.assertIn(key, status)
            self.assertFalse(status[key])
        self.assertFalse(status["running"])

    # ------------------------------------------------- счётчики авторизации
    def test_mqtt_auth_failure_increments_counter(self):
        """Неверный Access Code по MQTT: CONNACK 4 и счётчик отказов растёт."""
        packet = mqtt_connect_packet("bblp", "неверный-код")
        for _ in range(2):
            replies = self.gw.mqtt_handle_packet(packet)
            self.assertTrue(replies)
        status = self.gw.status()
        self.assertEqual(2, status["mqtt_auth_failures"])
        self.assertEqual(0, status["mqtt_connections"])
        self.assertTrue(status["last_auth_fail_at"], "время последнего отказа пустое")

    def test_mqtt_success_increments_connection_counter(self):
        replies = self.gw.mqtt_handle_packet(mqtt_connect_packet("bblp", "abcd1234"))
        self.assertTrue(replies)
        status = self.gw.status()
        self.assertEqual(1, status["mqtt_connections"])
        self.assertEqual(0, status["mqtt_auth_failures"])

    def test_ftp_auth_failure_increments_counter(self):
        """Неверный пароль по FTPS: 530 и счётчик отказов растёт."""
        self.assertEqual("331 Password required", self.gw.ftp_command("USER bblp"))
        self.assertEqual("530 Login incorrect", self.gw.ftp_command("PASS плохо"))
        self.assertEqual("331 Password required", self.gw.ftp_command("USER bblp"))
        self.assertEqual("530 Login incorrect", self.gw.ftp_command("PASS плохо"))
        status = self.gw.status()
        self.assertEqual(2, status["ftp_auth_failures"])
        self.assertEqual(2, status["ftp_connections"], "попытки входа считаются по USER")
        self.assertTrue(status["last_auth_fail_at"])

    def test_auth_failure_is_logged_for_connector_logs(self):
        """Отказ авторизации попадает в журнал коннектора (python pf.py logs)."""
        captured: list[logging.LogRecord] = []

        class Collector(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        collector = Collector()
        logger = logging.getLogger("printflow")
        logger.addHandler(collector)
        try:
            self.gw.ftp_command("USER bblp")
            self.gw.ftp_command("PASS нет")
        finally:
            logger.removeHandler(collector)
        self.assertTrue(any("Access Code" in record.getMessage()
                            for record in captured),
                        "отказ авторизации не виден в журнале")

    # ------------------------------------------------- сбой старта в журнал
    def test_start_failure_recorded_per_service_and_logged(self):
        """Занятый порт MQTT: сбой виден в статусе, в last_error и в журнале.

        bind=True без сети: MQTT_PORT подменяется на заведомо занятый порт,
        FTP_PORT — на свободный высокий (990 в песочнице без root недоступен),
        сертификат создаётся настоящий — TLS-контекст должен собраться, чтобы
        отказ случился именно на занятом порте, а не на отсутствии ключа.
        """
        if shutil.which("openssl") is None:
            self.skipTest("openssl не найден — сертификат не сгенерировать")
        cert_dir = pathlib.Path(tempfile.mkdtemp(prefix="pf-gw-cert-"))
        self.addCleanup(shutil.rmtree, cert_dir, ignore_errors=True)
        cert, key = cert_dir / "cert.pem", cert_dir / "key.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key), "-out", str(cert), "-days", "2",
             "-subj", "/CN=PrintFlow-Test"],
            check=True, capture_output=True)

        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy_port = blocker.getsockname()[1]
        self.addCleanup(blocker.close)

        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        free_port = free.getsockname()[1]
        free.close()

        captured: list[logging.LogRecord] = []

        class Collector(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        collector = Collector()
        logger = logging.getLogger("printflow")
        logger.addHandler(collector)
        gw = StudioGateway(self.db, self.mgr, bind=True)
        gw.db.set_settings({"studio_gateway_enabled": True})
        try:
            with mock.patch("connector.printflow.studio_gateway.MQTT_PORT", busy_port), \
                 mock.patch("connector.printflow.studio_gateway.FTP_PORT", free_port), \
                 mock.patch("connector.printflow.studio_tls.ensure_certificate",
                            return_value=(cert, key)):
                gw.start()
        finally:
            logger.removeHandler(collector)
            gw.stop()
        self.assertIn("MQTT:", gw.last_error, "в last_error не видно, какой сервис упал")
        self.assertTrue(gw.status()["errors"].get("mqtt"), "причина MQTT не в errors")
        self.assertFalse(gw.status()["mqtt_running"])
        self.assertTrue(any("Шлюз Bambu Studio" in record.getMessage()
                            for record in captured),
                        "сбой старта не попал в журнал коннектора")

    def test_status_fallback_has_the_same_fields(self):
        """Шлюз ещё не создан — карточка всё равно получает новые поля.

        Заглушка /api/studio/status должна идти нога в ногу с живым статусом,
        иначе карточка покажет «undefined» в первые секунды после запуска.
        """
        from connector.tests.test_phase11 import make_api
        api = make_api(self.db)
        api.manager = None
        code, payload = api.get("/api/studio/status", {})
        self.assertEqual(200, code)
        for key in ("ssdp_listen_ports", "ssdp_bound_port", "ssdp_note",
                    "ssdp_targets", "dropped_connections"):
            self.assertIn(key, payload)

    # ------------------------------------------------- статус без шлюза
    def test_status_payload_shape(self):
        """Статус несёт поля, которые ждёт карточка панели."""
        status = self.gw.status()
        for key in ("host", "host_pinned", "mqtt_port", "ftp_port",
                    "last_client", "last_auth_fail_at", "errors", "last_error",
                    "ssdp_listen_ports", "ssdp_bound_port", "ssdp_targets"):
            self.assertIn(key, status)


def load_gateway_check():
    """Импорт scripts/gateway-check.py (в имени дефис — только через spec)."""
    import importlib.util
    path = ROOT / "scripts" / "gateway-check.py"
    spec = importlib.util.spec_from_file_location("gateway_check_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GatewayCheckScriptTests(unittest.TestCase):
    """Скрипт-диагностика — то, что владелец запускает руками после 18.7.2."""

    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.db.set_settings({"studio_gateway_access_code": "abcd1234"})
        self.mgr = FakeMgr(self.db)

    def test_script_runs_and_prints_machine_readable_report(self):
        """`python scripts/gateway-check.py --json` не падает и говорит по-русски."""
        import subprocess
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "gateway-check.py"), "--json",
             "--panel", "http://127.0.0.1:9"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=120)
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        payload = json.loads(result.stdout)
        names = [item["name"] for item in payload["results"]]
        for expected in ("Панель PrintFlow", "Проба личности :3000",
                         "Брандмауэр TCP 3000", "UDP :2021 (розетка Studio)"):
            self.assertIn(expected, names)
        self.assertTrue(payload["advice"], "скрипт не сказал, что делать")

    def test_probe_detect_reads_a_live_gateway(self):
        """Живая проба личности тем же кадром, что шлёт Studio.

        Порты берём свободные (в песочнице 3000/3002 может занимать чужой
        процесс), а проверяем именно протокол: кадр, ответ, серийник.
        """
        if shutil.which("openssl") is None:
            self.skipTest("openssl не найден — TLS-контекст не собрать")
        from connector.printflow import studio_gateway as module_gw
        from connector.tests.test_studio_gateway_bind import free_port, make_cert
        cert, key = make_cert()
        self.addCleanup(shutil.rmtree, cert.parent, ignore_errors=True)
        plain_port, tls_port = free_port(), free_port()
        gw = StudioGateway(self.db, self.mgr, bind=True)
        self.mgr.studio = gw
        gw._tls_ctx = gw._tls_context(cert, key)
        with mock.patch.object(module_gw, "BIND_PORT_PLAIN", plain_port), \
             mock.patch.object(module_gw, "BIND_PORT_TLS", tls_port):
            gw._start_bind(cert, key)
            self.addCleanup(gw.stop)
            self.assertTrue(gw.status()["bind_running"],
                            f"порты {plain_port}/{tls_port} заняты — проба невозможна")

            module = load_gateway_check()
            plain = module.probe_detect("127.0.0.1", plain_port)
            self.assertEqual("ok", plain["state"], plain["detail"])
            self.assertEqual(gw.identity()["serial"], plain["serial"])
            tls = module.probe_detect("127.0.0.1", tls_port, tls=True)
            self.assertEqual("ok", tls["state"], tls["detail"])
        self.assertEqual("free", (gw.bind_reply({"login": {"command": "detect"}})
                                  or {}).get("login", {}).get("bind"))

    def test_probe_ssdp_and_studio_socket_are_reported(self):
        """M-SEARCH доходит, а UDP 2021 шлюз не занимает — его розетка у Studio."""
        module = load_gateway_check()
        gw = StudioGateway(self.db, self.mgr, bind=True)
        self.mgr.studio = gw
        gw._start_ssdp()
        self.addCleanup(gw.stop)
        answer = module.probe_ssdp("127.0.0.1", timeout=2.0)
        self.assertEqual("ok", answer["state"], answer["detail"])
        socket_state = module.port_2021_state()
        self.assertEqual("ok", socket_state["state"])
        self.assertNotIn(2021, gw.status()["ssdp_listen_ports"])

    def test_advice_names_the_real_cause(self):
        """Вердикт скрипта указывает на порт 3000, а не на Access Code."""
        module = load_gateway_check()
        results = [
            ("Проба личности :3000", {"state": "bad", "detail": "connection refused"}),
            ("Панель PrintFlow", {"state": "warn", "detail": "не ответила"}),
        ]
        advice = " ".join(module.speech(results))
        self.assertIn("3000", advice)
        self.assertIn("код=-1", advice)


if __name__ == "__main__":
    unittest.main()

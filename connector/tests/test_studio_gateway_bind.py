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
    SSDP_LISTEN_PORTS,
    SSDP_NT,
    SSDP_NOTIFY_LOOPBACK,
    SSDP_PORTS,
    MqttSession,
    StudioGateway,
    decode_bind_frame,
    encode_bind_frame,
    is_loopback,
)
from connector.printflow.studio_mqtt import (  # noqa: E402
    CONNACK,
    PUBLISH,
    SUBACK,
    decode_publish,
    encode_connect,
    encode_disconnect,
    encode_publish,
    encode_utf8,
    parse_fixed_header,
    read_packet,
    wrap_packet,
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
    """Настоящий самоподписанный сертификат через openssl (как в проде).

    С SAN: без ``subjectAltName`` Bambu Studio Beta рукопожатие режет, и тест
    проверял бы поведение, которого в проде не бывает.
    """
    if shutil.which("openssl") is None:
        raise unittest.SkipTest("openssl не найден — TLS не поднять")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pf-bind-cert-"))
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "2",
         "-subj", f"/CN={cn}",
         "-addext", f"subjectAltName=DNS:{cn},IP:127.0.0.1"],
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
        # Эхо, а не константа: плагин сверяет sequence_id ответа со своим и
        # отбрасывает ответ, который пришёл «не на его запрос».
        self.assertEqual(20000, login["sequence_id"])
        self.assertIsInstance(login["sequence_id"], int,
                              "принтер отвечает числом, плагин ждёт int")

    def test_detect_echoes_whatever_sequence_id_studio_sent(self):
        """Разные клиенты нумеруют запросы по-разному — шлюз повторяет число."""
        for value, expected in (("20000", 20000), ("1", 1), (777, 777),
                                ("0", 0), ("", 0), (None, 0), ("abc", 0)):
            login = self.gw.bind_reply(
                {"login": {"command": "detect", "sequence_id": value}})["login"]
            self.assertEqual(expected, login["sequence_id"], f"эхо {value!r}")
            self.assertIsInstance(login["sequence_id"], int)

    def test_login_echoes_sequence_id_too(self):
        payload, _rest = decode_bind_frame(self.gw.bind_handle_bytes(
            encode_bind_frame({"login": {"command": "login", "sequence_id": "20001"}})))
        self.assertEqual(20001, payload["login"]["sequence_id"])
        self.assertEqual("SUCCESS", payload["login"]["status"])

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
        if shutil.which("openssl") is None:
            self.skipTest("openssl не найден — сертификат не выпустить")
        with tempfile.TemporaryDirectory(prefix="pf-cn-") as tmp:
            cert_dir = pathlib.Path(tmp)
            paths = {
                "CERT_DIR": cert_dir,
                "CERT_FILE": cert_dir / "cert.pem",
                "KEY_FILE": cert_dir / "key.pem",
                "CN_FILE": cert_dir / "cert.pem.cn",
                "SAN_FILE": cert_dir / "cert.pem.san",
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

    def test_certificate_has_the_san_studio_beta_requires(self):
        """SAN DNS:<серийник> + IP:127.0.0.1: без него Beta режет рукопожатие.

        Сетевой плагин Studio Beta (22710816+) живёт на WebView2 и сверяет
        subjectAltName, а не CN. Сертификат, выпущенный до этой правки, лежал
        на диске с верным CN и без SAN — и Studio молча отдавала «код=-1».
        """
        if shutil.which("openssl") is None:
            self.skipTest("openssl не найден — сертификат не выпустить")
        from connector.printflow import studio_tls
        with tempfile.TemporaryDirectory(prefix="pf-san-") as tmp:
            cert_dir = pathlib.Path(tmp)
            with mock.patch.multiple(
                    "connector.printflow.studio_tls",
                    CERT_DIR=cert_dir, CERT_FILE=cert_dir / "cert.pem",
                    KEY_FILE=cert_dir / "key.pem",
                    CN_FILE=cert_dir / "cert.pem.cn",
                    SAN_FILE=cert_dir / "cert.pem.san"):
                cert, _key = studio_tls.ensure_certificate("01P00ASAN")
                san = studio_tls.certificate_san(cert)
                self.assertIn("DNS:01P00ASAN", san, f"нет DNS в SAN: {san}")
                self.assertTrue(any("127.0.0.1" in item for item in san),
                                f"нет IP:127.0.0.1 в SAN: {san}")
                self.assertEqual("DNS:01P00ASAN,IP:127.0.0.1",
                                 studio_tls.stored_san())
                # Выпуск без SAN (имитация старого сертификата) — перевыпуск.
                stale = cert.read_bytes()
                subprocess.run(
                    ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                     "-keyout", str(cert_dir / "key.pem"), "-out", str(cert),
                     "-days", "2", "-subj", "/CN=01P00ASAN"],
                    check=True, capture_output=True)
                self.assertEqual([], studio_tls.certificate_san(cert))
                studio_tls.ensure_certificate("01P00ASAN")
                self.assertNotEqual(stale, cert.read_bytes(),
                                    "сертификат без SAN не перевыпущен")
                self.assertIn("DNS:01P00ASAN", studio_tls.certificate_san(cert))

    def test_san_survives_a_space_in_the_device_name(self):
        """Имя с пробелом не должно ронять выпуск сертификата."""
        if shutil.which("openssl") is None:
            self.skipTest("openssl не найден — сертификат не выпустить")
        from connector.printflow import studio_tls
        with tempfile.TemporaryDirectory(prefix="pf-san-name-") as tmp:
            cert_dir = pathlib.Path(tmp)
            with mock.patch.multiple(
                    "connector.printflow.studio_tls",
                    CERT_DIR=cert_dir, CERT_FILE=cert_dir / "cert.pem",
                    KEY_FILE=cert_dir / "key.pem",
                    CN_FILE=cert_dir / "cert.pem.cn",
                    SAN_FILE=cert_dir / "cert.pem.san"):
                san = studio_tls.san_for("NOZZA PrintFlow")
                self.assertEqual("DNS:NOZZA-PrintFlow,IP:127.0.0.1", san)
                cert, _key = studio_tls.ensure_certificate("NOZZA PrintFlow")
                self.assertIn("DNS:NOZZA-PrintFlow",
                              studio_tls.certificate_san(cert))


class IdentityRaceTests(unittest.TestCase):
    """Серийник создаётся один раз, даже если шлюз спросили одновременно.

    Без блокировки два потока (``start()`` и поток SSDP, или два клиента на
    :3000) оба видели пустой серийник и записывали каждый свой: объявление
    уходило с одним USN, а в базе оставался другой. Для Studio это принтер,
    который меняет личность, — устройство приходится пересоздавать.
    """

    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.db.set_settings({"studio_gateway_access_code": "abcd1234"})
        self.mgr = FakeMgr(self.db)
        self.gw = StudioGateway(self.db, self.mgr, bind=False)
        self.mgr.studio = self.gw

    def test_concurrent_identity_calls_yield_one_serial(self):
        issued: list[str] = []

        def fake_serial() -> str:
            issued.append("01P00A%09d" % len(issued))
            return issued[-1]

        barrier = threading.Barrier(8)
        results: list[str] = []
        lock = threading.Lock()

        def ask() -> None:
            barrier.wait()          # все восемь стартуют в один момент
            serial = self.gw.identity()["serial"]
            with lock:
                results.append(serial)

        with mock.patch("connector.printflow.studio_gateway._new_serial", fake_serial):
            threads = [threading.Thread(target=ask) for _ in range(8)]
            for item in threads:
                item.start()
            for item in threads:
                item.join(timeout=10)
        self.assertEqual(1, len(issued),
                         f"серийник создан {len(issued)} раз(а): {issued} — "
                         "шлюз показывает Studio разную личность")
        self.assertEqual(1, len(set(results)),
                         f"потоки увидели разные серийники: {sorted(set(results))}")
        stored = str(self.db.setting("studio_gateway_serial", "") or "")
        self.assertEqual(issued[0], stored,
                         "в базе остался не тот серийник, который объявляли")

    def test_access_code_is_created_once_too(self):
        db = make_db()
        self.addCleanup(db.close)
        # Серийник задан: _new_serial() тоже берёт байты из secrets, и без
        # этого подсчёт ловил бы его вызов вместо Access Code.
        db.set_settings({"studio_gateway_serial": "01P00AFIXED0001"})
        # Access Code не задан — шлюз обязан создать его ровно один раз.
        mgr = FakeMgr(db)
        gw = StudioGateway(db, mgr, bind=False)
        mgr.studio = gw
        generated: list[str] = []

        def fake_token(_n: int) -> str:
            generated.append("code%04d" % len(generated))
            return generated[-1]

        barrier = threading.Barrier(6)

        def ask() -> None:
            barrier.wait()
            gw.identity()

        with mock.patch("connector.printflow.studio_gateway.secrets.token_hex",
                        fake_token):
            threads = [threading.Thread(target=ask) for _ in range(6)]
            for item in threads:
                item.start()
            for item in threads:
                item.join(timeout=10)
        self.assertEqual(1, len(generated),
                         f"Access Code создан {len(generated)} раз(а) — "
                         "Studio могла получить отказ уже после привязки")
        self.assertTrue(gw.status()["has_access_code"])
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
        """Станки шлют NOTIFY на 255.255.255.255:2021 — там его и ждут.

        И проверяем, ЧЕМ отправлен каждый адрес: ядро не выпустит пакет на
        127.0.0.1 с источника LAN-адреса (и наоборот), поэтому в loopback
        шлёт отдельный сокет. Раньше сокет был один, привязанный к LAN, — и
        как только у машины появлялся обычный адрес, объявления для Studio на
        этом же компьютере молча терялись (sendto → EINVAL).
        """
        # (с какого адреса отправлено, куда)
        sent: list[tuple[str, tuple[str, int]]] = []
        bound: list[tuple[str, int]] = []

        class FakeUdp:
            """Заглушка UDP-сокета: пишем и bind (привязка к LAN), и отправку."""

            def __init__(self, *_a, **_k):
                self.source = "0.0.0.0"

            def setsockopt(self, *_a, **_k):
                return None

            def bind(self, addr):
                self.source = str(addr[0])
                bound.append(tuple(addr))
                return None

            def sendto(self, payload, addr):
                sent.append((self.source, tuple(addr)))

            def sendmsg(self, buffers, control=None, flags=0, address=None):
                # Реальный вызов позиционный: sendmsg([data], [cmsg], 0, addr).
                assert buffers, "пустая дейтаграмма"
                if address is not None:
                    sent.append((self.source, tuple(address)))

            def getsockname(self):
                return (self.source, 0)

            def close(self):
                return None

        self.gw._last_notify = 0.0
        with mock.patch("connector.printflow.studio_gateway.socket.socket", FakeUdp):
            self.gw._broadcast_notify()
        destinations = [addr for _source, addr in sent]
        self.assertIn((SSDP_BROADCAST, 2021), destinations,
                      "без широковещательной рассылки Studio шлюз не увидит")
        self.assertIn((SSDP_BROADCAST, SSDP_PORTS[0]), destinations)
        for port in SSDP_PORTS:
            self.assertIn(("239.255.255.250", port), destinations)
        self.assertIn((SSDP_NOTIFY_LOOPBACK, SSDP_PORTS[0]), destinations,
                      "Studio на том же ПК узнаёт шлюз только по loopback")
        # В loopback (включая направленный broadcast 127.0.0.255) — только с
        # loopback-источника, иначе пакет не уйдёт вовсе.
        loopback_sends = [source for source, addr in sent if is_loopback(addr[0])]
        self.assertIn((SSDP_NOTIFY_LOOPBACK, SSDP_PORTS[0]),
                      [addr for _s, addr in sent if is_loopback(addr[0])],
                      "объявление в loopback не отправлено")
        self.assertTrue(all(source.startswith("127.") for source in loopback_sends),
                        f"в loopback шлём с {loopback_sends} — пакет не дойдёт до "
                        "сетевого плагина Studio на этом же компьютере")
        # Во внешнюю сеть — не с loopback-источника (иначе Studio не увидит
        # шлюз с другого компьютера, а ответ не совпадёт с Location).
        outside = [(source, addr) for source, addr in sent
                   if not is_loopback(addr[0])]
        self.assertTrue(outside, "во внешнюю сеть ничего не отправлено")
        self.assertEqual([], [item for item in outside if item[0].startswith("127.")],
                         "во внешнюю сеть незачем слать с loopback-источника")

    def test_notify_period_is_printer_like(self):
        from connector.printflow import studio_gateway as module
        self.assertLessEqual(module.SSDP_NOTIFY_PERIOD, 6.0,
                             "плагин слушает одно-два объявления, реже — не увидит")

    def test_udp_2021_is_never_taken_by_the_gateway(self):
        """UDP :2021 — розетка самого Studio: вторая на том же порту ломает поиск.

        Две розетки на одном порту с SO_REUSEADDR в Windows делят входящий
        трафик непредсказуемо: шлюз, поднявшийся раньше Studio, отбирал у неё
        объявления принтеров, Studio оставалась с пустым dev_ip и отдавала
        «код=-1» ещё до MQTT. Поэтому 2021 шлюз не слушает (только шлёт).
        """
        from connector.printflow import studio_gateway as module
        self.assertNotIn(2021, module.SSDP_LISTEN_PORTS,
                         "2021 принадлежит Studio, шлюз не смеет его занимать")
        self.assertIn(2021, module.SSDP_PORTS,
                      "объявление обязано уходить на 2021 — там его ждёт плагин")
        self.assertEqual((1900,), tuple(module.SSDP_LISTEN_PORTS))

    def test_own_address_and_directed_broadcast_are_announced(self):
        """Объявление уходит на loopback, свой адрес и в свою подсеть."""
        with mock.patch("connector.printflow.config.get_local_ips",
                        return_value=["192.168.1.50"]):
            self.gw._ips_cache = []
            self.gw._ips_cache_at = 0.0
            self.db.set_settings({"studio_gateway_host": "192.168.1.50"})
            targets = self.gw._notify_targets()
        for expected in ((SSDP_NOTIFY_LOOPBACK, 2021),
                         ("192.168.1.50", 2021),
                         ("192.168.1.255", 2021),
                         (SSDP_BROADCAST, 2021)):
            self.assertIn(expected, targets, f"нет адреса {expected}")
        self.assertEqual(len(targets), len(set(targets)), "адреса задвоились")

    def test_directed_broadcast_helper(self):
        from connector.printflow.studio_gateway import directed_broadcast
        self.assertEqual("192.168.1.255", directed_broadcast("192.168.1.50"))
        self.assertEqual("", directed_broadcast("192.168.1.999"))
        self.assertEqual("", directed_broadcast(""))
        self.assertEqual("", directed_broadcast("не адрес"))

    def test_ssdp_socket_is_not_shared_while_the_port_is_free(self):
        """Свободный 1900 слушаем единолично, без SO_REUSEPORT.

        С SO_REUSEPORT ядро делит входящие дейтаграммы между всеми сокетами на
        порту: зависший после перезапуска процесс или чужая служба SSDP
        перехватывают часть M-SEARCH, и Studio не находит принтер через раз.
        """
        options: list[int] = []
        binds: list[tuple[str, int]] = []

        class FakeUdp:
            def __init__(self, *_a, **_k):
                pass

            def setsockopt(self, level, option, value):
                if level == socket.SOL_SOCKET:
                    options.append(option)

            def bind(self, addr):
                binds.append(tuple(addr))

            def getsockname(self):
                return ("0.0.0.0", SSDP_LISTEN_PORTS[0])

            def settimeout(self, *_a):
                return None

            def close(self):
                return None

        self.gw._spawn = lambda *args, **kwargs: None
        with mock.patch("connector.printflow.studio_gateway.socket.socket", FakeUdp):
            self.gw._start_ssdp()
        self.addCleanup(self.gw.stop)
        self.assertEqual(SSDP_LISTEN_PORTS[0], self.gw._ssdp_bound_port)
        # Привязка исходящего сокета (к LAN-адресу) тут не в счёт: считаем
        # только розетку приёма, она привязывается к "".
        listen_binds = [addr for addr in binds if addr[0] == ""]
        self.assertEqual(1, len(listen_binds),
                         "порт свободен — вторая привязка не нужна")
        self.assertNotIn(socket.SO_REUSEPORT, options,
                         "SO_REUSEPORT на свободном порту делит M-SEARCH с чужаками")
        self.assertIn(socket.SO_REUSEADDR, options)
        self.assertEqual("", self.gw._ssdp_note)

    def test_ssdp_port_is_shared_only_when_someone_else_holds_it(self):
        """1900 занят (служба SSDP Discovery) — делим порт и честно это пишем."""
        options: list[int] = []
        binds: list[tuple[str, int]] = []
        attempts = {"n": 0}

        class BusyUdp:
            def __init__(self, *_a, **_k):
                pass

            def setsockopt(self, level, option, value):
                if level == socket.SOL_SOCKET:
                    options.append(option)

            def bind(self, addr):
                binds.append(tuple(addr))
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise OSError(98, "Address already in use")

            def getsockname(self):
                return ("0.0.0.0", SSDP_LISTEN_PORTS[0])

            def settimeout(self, *_a):
                return None

            def close(self):
                return None

        self.gw._spawn = lambda *args, **kwargs: None
        with mock.patch("connector.printflow.studio_gateway.socket.socket", BusyUdp):
            self.gw._start_ssdp()
        self.addCleanup(self.gw.stop)
        self.assertEqual(SSDP_LISTEN_PORTS[0], self.gw._ssdp_bound_port,
                         "занятый порт надо делить, а не уходить на случайный")
        self.assertIn(socket.SO_REUSEPORT, options)
        listen_binds = [addr for addr in binds if addr[0] == ""]
        self.assertEqual(2, len(listen_binds),
                         "занятый порт пробуем дважды: сначала единолично, "
                         "потом совместно")
        self.assertIn("SO_REUSEPORT", self.gw._ssdp_note,
                      "владелец должен видеть, что порт делится с чужой программой")

    def test_status_reports_listen_port_and_targets(self):
        status = self.gw.status()
        self.assertEqual(list(SSDP_LISTEN_PORTS), status["ssdp_listen_ports"])
        self.assertNotIn(2021, status["ssdp_listen_ports"])
        self.assertIsInstance(status["ssdp_bound_port"], int)
        self.assertIn("127.0.0.1:2021", status["ssdp_targets"])


class StudioOnTheSamePcTests(unittest.TestCase):
    """Главный сценарий владельца: Bambu Studio стоит на том же компьютере.

    Раньше шлюз занимал UDP 2021 — тот самый порт, который слушает сетевой
    плагин Studio, — и объявление могло не дойти до плагина. Теперь шлюз
    слушает 1900, а объявление шлёт в том числе по loopback, куда брандмауэр
    Windows не заглядывает вовсе.
    """

    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.db.set_settings({"studio_gateway_enabled": True,
                              "studio_gateway_access_code": "abcd1234"})
        self.mgr = FakeMgr(self.db)
        self.gw = StudioGateway(self.db, self.mgr, bind=True)
        self.mgr.studio = self.gw

    def test_studio_port_stays_free_and_announce_reaches_it(self):
        """Пока шлюз работает, чужой «Studio» занимает 2021 и получает NOTIFY."""
        self.gw._start_ssdp()
        self.addCleanup(self.gw.stop)
        self.assertTrue(self.gw._ssdp_sock, "SSDP-сокет шлюза не поднялся")
        self.assertNotEqual(2021, self.gw._ssdp_bound_port)

        studio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.addCleanup(studio.close)
        try:
            # Без SO_REUSEADDR: если бы шлюз держал 2021, здесь была бы ошибка
            # «адрес уже занят» — ровно та, из-за которой Studio не находила
            # принтер, пока шлюз поднимался раньше неё.
            studio.bind(("127.0.0.1", 2021))
        except OSError as exc:
            self.skipTest(f"UDP 2021 занят в системе: {exc}")
        studio.settimeout(10)

        self.gw._last_notify = 0.0
        self.gw._broadcast_notify()
        data, _addr = studio.recvfrom(4096)
        text = data.decode("utf-8", "replace")
        self.assertIn("NOTIFY * HTTP/1.1", text)
        self.assertIn(f"DevVersion.bambu.com: {FIRMWARE_VERSION}", text)
        self.assertIn("DevBind.bambu.com: free", text)
        self.assertIn("DevConnect.bambu.com: lan", text)
        self.assertIn(self.gw.identity()["serial"], text)


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



def subscribe_packet(packet_id: int = 1, topic: str = "device/01P00ATEST/request") -> bytes:
    """Пакет SUBSCRIBE (в studio_mqtt есть только декодер, кодируем руками)."""
    payload = packet_id.to_bytes(2, "big") + encode_utf8(topic) + b"\x00"
    return wrap_packet(8, payload, flags=2)


class MqttPerConnectionAuthTests(unittest.TestCase):
    """Авторизация MQTT — своя у каждого соединения.

    Настоящий станок авторизует клиентов независимо. Если флаг общий, то
    чужой вход с неверным Access Code (или чужой DISCONNECT) гасит отчёты у
    уже работающей Studio: соединение живое, CONNACK был 0, а
    ``device/<серийник>/report`` молчит. Выглядит это как «подключился, но
    принтер пустой», поэтому поведение проверяем отдельно.
    """

    CODE = "abcd1234"

    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.db.set_settings({"studio_gateway_access_code": self.CODE})
        self.mgr = FakeMgr(self.db)
        # bind=True: именно в этом режиме шлюз требует вход перед PUBLISH.
        self.gw = StudioGateway(self.db, self.mgr, bind=True)
        self.mgr.studio = self.gw
        self.topic = f"device/{self.gw.identity()['serial']}/request"

    def login(self, session: MqttSession, password: str) -> int:
        packet = encode_connect(client_id="studio", username="bblp",
                                password=password)
        replies = self.gw.mqtt_handle_packet(packet, session)
        ptype, _flags, payload = parse_fixed_header(replies[0])
        self.assertEqual(CONNACK, ptype)
        return payload[1]

    def reports(self, session: MqttSession) -> list[dict]:
        packet = encode_publish(self.topic, json.dumps(
            {"info": {"command": "get_version", "sequence_id": "2"}}))
        out = []
        for reply in self.gw.mqtt_handle_packet(packet, session):
            ptype, flags, payload = parse_fixed_header(reply)
            if ptype == PUBLISH:
                pub = decode_publish(flags, payload)
                out.append(json.loads(pub["payload"]))
        return out

    def test_two_clients_authorize_independently(self):
        studio, guest = MqttSession(peer="192.168.0.50"), MqttSession(peer="10.0.0.2")
        self.assertEqual(0, self.login(studio, self.CODE))
        self.assertEqual(4, self.login(guest, "неверный"))
        self.assertTrue(studio.authed)
        self.assertFalse(guest.authed)

    def test_failed_login_elsewhere_does_not_mute_the_working_client(self):
        studio, guest = MqttSession(), MqttSession()
        self.assertEqual(0, self.login(studio, self.CODE))
        self.assertTrue(self.reports(studio), "до чужого входа отчёты есть")
        # Чужой клиент ошибся кодом — у Studio отчёты обязаны остаться.
        self.assertEqual(4, self.login(guest, "oops1234"))
        reports = self.reports(studio)
        self.assertTrue(reports, "чужой неверный код заглушил работающую Studio")
        self.assertEqual(1, len(reports))
        self.assertEqual("get_version", reports[0]["info"]["command"])

    def test_disconnect_elsewhere_does_not_mute_the_working_client(self):
        studio, guest = MqttSession(), MqttSession()
        self.login(studio, self.CODE)
        self.login(guest, self.CODE)
        self.gw.mqtt_handle_packet(encode_disconnect(), guest)
        self.assertFalse(guest.authed)
        self.assertTrue(studio.authed, "чужой DISCONNECT сбросил нашу авторизацию")
        self.assertTrue(self.reports(studio))

    def test_another_guest_can_log_in_after_the_first_one_left(self):
        studio, guest = MqttSession(), MqttSession()
        self.login(studio, self.CODE)
        self.login(guest, self.CODE)
        self.gw.mqtt_handle_packet(encode_disconnect(), studio)
        self.assertTrue(guest.authed)
        self.assertTrue(self.reports(guest), "после чужого DISCONNECT отчёты пропали")

    def test_subscribe_before_login_gets_nothing(self):
        guest = MqttSession()
        self.assertEqual([], self.gw.mqtt_handle_packet(subscribe_packet(), guest),
                         "SUBACK до входа — так станок не делает")
        self.login(guest, self.CODE)
        replies = self.gw.mqtt_handle_packet(subscribe_packet(), guest)
        self.assertEqual(1, len(replies))
        self.assertEqual(SUBACK, parse_fixed_header(replies[0])[0])

    def test_publish_before_login_gets_nothing(self):
        guest = MqttSession()
        self.assertEqual([], self.reports(guest),
                         "PUBLISH до входа обязан отбрасываться")

    def test_default_session_keeps_the_socketless_api(self):
        """Вызов без сессии работает как раньше (тесты, диагностика, скрипты)."""
        packet = encode_connect(client_id="diag", username="bblp", password=self.CODE)
        ptype, _flags, payload = parse_fixed_header(
            self.gw.mqtt_handle_packet(packet)[0])
        self.assertEqual((CONNACK, 0), (ptype, payload[1]))
        self.assertTrue(self.gw._mqtt_authed, "свойство _mqtt_authed не отражает сессию")
        self.gw._mqtt_authed = False
        self.assertFalse(self.gw._mqtt_session.authed)

    def test_default_and_live_sessions_do_not_leak_into_each_other(self):
        live = MqttSession()
        self.login(live, self.CODE)
        # «Сброс» сессии по умолчанию не трогает живое соединение.
        self.gw._mqtt_authed = False
        self.assertTrue(live.authed)
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
        # SSDP-порт тоже свой: на 1900 в одном процессе могут слушать шлюзы
        # соседних тестов, и ядро начнёт делить между ними M-SEARCH.
        self.ssdp_port = free_port()
        patches = [
            mock.patch(f"connector.printflow.studio_gateway.{name}", port)
            for name, port in self.ports.items()
        ]
        patches.append(mock.patch(
            "connector.printflow.studio_gateway.SSDP_LISTEN_PORTS",
            (self.ssdp_port,)))
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

    def test_stray_connection_without_tls_does_not_kill_the_services(self):
        """Проверка «открыт ли порт» не должна гасить TLS-службы шлюза.

        accept() на TLS-розетке сам делает рукопожатие; клиент без TLS
        (Test-NetConnection, сканер сети, антивирус) поднимал исключение и
        раньше убивал цикл приёма целиком: порт оставался в LISTEN, но новых
        клиентов не принимал — Studio висела по таймауту и показывала «код=-1»
        на исправном шлюзе.
        """
        for port in (self.ports["MQTT_PORT"], self.ports["BIND_PORT_TLS"]):
            stray = socket.create_connection(("127.0.0.1", port), timeout=5)
            try:
                stray.sendall(b"GET / HTTP/1.0\r\n\r\n")  # это не TLS
            except OSError:
                pass
            finally:
                stray.close()
        time.sleep(0.3)  # даём циклу приёма обработать сбой рукопожатия

        reply = self.detect(self.ports["BIND_PORT_PLAIN"])
        self.assertEqual("detect", reply.get("login", {}).get("command"))
        tls_reply = self.detect(self.ports["BIND_PORT_TLS"], tls=True)
        self.assertEqual("detect", tls_reply.get("login", {}).get("command"))
        body = self.mqtt({"pushing": {"command": "pushall", "sequence_id": "0"}})
        self.assertEqual("IDLE", body["print"]["gcode_state"])
        self.assertGreaterEqual(self.gw.status()["dropped_connections"], 0)

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

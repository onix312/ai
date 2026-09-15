"""Автопоиск сервера телефоном: UDP-маяк, ответ на «кто здесь?» и разбор ответов.

Половина жалобы «касса не подключается и не может найти сервер» решается на
стороне ПК: сервер обязан сказать о себе сам, а не ждать, пока телефон обойдёт
254 адреса. Здесь проверяется именно это — протокол, ответ на запрос (вживую,
через UDP-сокет), остановка маяка и то, что чужие датаграммы в сети ничего не
ломают: «представиться кассой» в гостевой сети может кто угодно.
"""
from __future__ import annotations

import json
import pathlib
import socket
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
import sys  # noqa: E402  — после ROOT, чтобы пакет импортировался из репозитория
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import discovery  # noqa: E402


def free_udp_port() -> int:
    """Свободный UDP-порт: тесты не должны драться за 8765 с другими тестами."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class PayloadTests(unittest.TestCase):
    def test_payload_carries_what_the_phone_needs(self):
        data = discovery.payload_json(8765, version="17.0.27", name="PETYA-PC",
                                      lan=["192.168.1.50", "10.0.0.7"])
        info = discovery.parse(data)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["app"], "printflow")
        self.assertEqual(info["proto"], discovery.PROTO)
        self.assertEqual(info["port"], 8765)
        self.assertEqual(info["version"], "17.0.27")
        self.assertEqual(info["name"], "PETYA-PC")
        self.assertEqual(info["lan"], ["192.168.1.50", "10.0.0.7"])
        # Пути телефону нужны, чтобы открыть свою страницу, а не угадывать её
        self.assertEqual(info["paths"]["kassa"], "/cashier.html")
        self.assertEqual(info["paths"]["pult"], "/pult")

    def test_payload_is_small_enough_for_one_datagram(self):
        data = discovery.payload_json(8765, lan=["192.168.1.50"] * 10)
        self.assertLess(len(data), discovery.MAX_DATAGRAM)

    def test_loopback_and_apipa_addresses_are_not_offered(self):
        """127.0.0.1 и 169.254.x телефону бесполезны: он по ним не дойдёт."""
        info = discovery.parse(discovery.payload_json(
            8765, lan=["127.0.0.1", "169.254.0.21", "192.168.1.50"]))
        assert info is not None
        self.assertEqual(info["lan"], ["192.168.1.50"])

    def test_garbage_and_foreign_protocol_are_ignored(self):
        self.assertIsNone(discovery.parse(b""))
        self.assertIsNone(discovery.parse("{не json".encode("utf-8")))
        self.assertIsNone(discovery.parse(b"x" * (discovery.MAX_DATAGRAM + 1)))
        self.assertIsNone(discovery.parse(json.dumps({"hello": "world"}).encode()))
        # чужое приложение
        self.assertIsNone(discovery.parse(
            json.dumps({"app": "other", "proto": 1, "port": 8765}).encode()))
        # чужая версия протокола: лучше перебор, чем разговор «наугад»
        self.assertIsNone(discovery.parse(
            json.dumps({"app": "printflow", "proto": 99, "port": 8765}).encode()))
        # порт вне диапазона
        for broken in (0, -1, 70000, "восемь", None):
            self.assertIsNone(discovery.parse(json.dumps(
                {"app": "printflow", "proto": 1, "port": broken}).encode()))

    def test_call_is_recognised_in_both_forms(self):
        self.assertTrue(discovery.is_call(discovery.CALL.encode()))
        self.assertTrue(discovery.is_call(b'{"app":"printflow","want":"?"}'))
        self.assertFalse(discovery.is_call(b"PRINTFLOW"))
        self.assertFalse(discovery.is_call(discovery.payload_json(8765)))
        self.assertFalse(discovery.is_call(b""))

    def test_broadcast_targets_cover_the_subnet_and_the_world(self):
        targets = discovery.broadcast_targets(["192.168.1.50", "10.0.0.7"])
        self.assertIn("255.255.255.255", targets)
        self.assertIn("192.168.1.255", targets)
        self.assertIn("10.0.0.255", targets)
        # мусор не превращается в адрес вещания
        self.assertEqual(["255.255.255.255"], discovery.broadcast_targets(["мусор", ""]))


class BeaconTests(unittest.TestCase):
    """Маяк и ответ на запрос — на живом UDP-сокете, но на отдельном порту."""

    def setUp(self):
        self.udp_port = free_udp_port()
        self.beacon: discovery.Beacon | None = None

    def tearDown(self):
        if self.beacon is not None:
            self.beacon.stop()

    def test_beacon_answers_the_call(self):
        self.beacon = discovery.start(8765, "0.0.0.0", version="17.0.27",
                                      udp_port=self.udp_port, interval=0.3)
        self.assertIsNotNone(self.beacon)
        time.sleep(0.2)
        hits = discovery.probe(port=self.udp_port, timeout=0.8, targets=["127.0.0.1"])
        self.assertTrue(hits, "маяк не ответил на «кто здесь?»")
        self.assertEqual(hits[0]["port"], 8765)
        self.assertEqual(hits[0]["version"], "17.0.27")
        self.assertEqual(hits[0]["base"], "http://127.0.0.1:8765")
        self.assertTrue(discovery.reachable(port=self.udp_port, timeout=0.8))

    def test_stopped_beacon_is_silent(self):
        beacon = discovery.start(8765, "0.0.0.0", udp_port=self.udp_port, interval=0.3)
        assert beacon is not None
        time.sleep(0.2)
        beacon.stop()
        self.assertFalse(beacon.running())
        self.assertEqual([], discovery.probe(port=self.udp_port, timeout=0.4,
                                             targets=["127.0.0.1"]))

    def test_beacon_does_not_flood_the_network(self):
        """Вещание по времени, а не по обороту цикла: копия датаграммы приходит
        и отправителю, и наивный цикл выдавал тысячи пакетов в секунду."""
        beacon = discovery.start(8765, "0.0.0.0", udp_port=self.udp_port, interval=0.5)
        assert beacon is not None
        self.beacon = beacon
        time.sleep(1.6)
        self.assertLess(beacon.info()["sent"], 40, "маяк шлёт слишком часто")

    def test_local_only_server_has_no_beacon(self):
        """При `--local` маяк вреден: телефон нашёл бы адрес, где его не ждут."""
        self.assertIsNone(discovery.start(8765, "127.0.0.1", udp_port=self.udp_port))

    def test_second_beacon_on_a_busy_port_does_not_break_the_first(self):
        first = discovery.start(8765, "0.0.0.0", udp_port=self.udp_port, interval=0.3)
        second = discovery.start(8765, "0.0.0.0", udp_port=self.udp_port, interval=0.3)
        self.beacon = first
        try:
            self.assertIsNotNone(first)
            self.assertIsNotNone(second, "второй экземпляр мешает — не должен падать")
            time.sleep(0.2)
            hits = discovery.probe(port=self.udp_port, timeout=0.6, targets=["127.0.0.1"])
            self.assertTrue(hits, "занятый порт сломал ответ на запрос")
        finally:
            if second is not None:
                second.stop()

    def test_beacon_info_is_serialisable(self):
        beacon = discovery.start(8765, "0.0.0.0", udp_port=self.udp_port, interval=0.3)
        assert beacon is not None
        self.beacon = beacon
        info = beacon.info()
        json.dumps(info)
        self.assertTrue(info["running"])
        self.assertEqual(info["http_port"], 8765)


class ServerWiringTests(unittest.TestCase):
    """Маяк живёт вместе с сервером: закрылся сервер — замолчал и маяк."""

    def test_server_close_stops_the_beacon(self):
        from connector.printflow.api import Handler, Server

        class FakeBeacon:
            def __init__(self) -> None:
                self.stopped = 0

            def stop(self, timeout: float = 1.0) -> None:
                self.stopped += 1

        server = Server(("127.0.0.1", 0), Handler)
        beacon = FakeBeacon()
        server.beacon = beacon
        try:
            server.server_close()
            self.assertEqual(beacon.stopped, 1)
            self.assertIsNone(server.beacon, "ссылка на маяк осталась жить")
        finally:
            server.server_close()

    def test_serve_wires_the_beacon_to_the_real_port(self):
        """Контракт по исходнику: `serve()` обязан поднять маяк с тем же портом.

        Запускать настоящий сервер в тесте нельзя (он занимает порт и поднимает
        потоки), поэтому проверяем привязку строкой — как это уже сделано для
        остальных контрактов оболочки.
        """
        source = (ROOT / "connector" / "printflow" / "api.py").read_text(encoding="utf-8")
        self.assertIn("server.beacon = discovery.start(port, host, version=APP_VERSION)",
                      source)
        self.assertIn("def server_close(self) -> None:", source)


if __name__ == "__main__":
    unittest.main()

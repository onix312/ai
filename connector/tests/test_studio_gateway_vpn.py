"""Шлюз Bambu Studio при поднятом VPN: «код=-1» из-за перехвата LAN.

Жалоба владельца: ручные пробы портов проходят, а Bambu Studio и OrcaSlicer
всё равно пишут «Сбой подключения, код=-1». Причина в сети: на машине поднят
VPN, появляется ``tun0`` с адресом ``10.0.0.1/30``, и именно его ядро
называет «адресом этого компьютера». Адрес уезжал в SSDP ``Location`` и в
ответ ``PASV`` — Studio шла на 10.0.0.1, где принтера нет.

Проверяются все три защиты:

1. ``get_local_ips()`` перечисляет интерфейсы и отбрасывает туннели
   (``tun*``/``tap*``/``utun*``/WireGuard/Tailscale/PPP, мосты
   Docker/Hyper-V/VirtualBox) и префиксы /30 и уже — это точка-точка, а не
   сегмент LAN, широковещательного SSDP там не бывает;
2. ``studio_gateway_host`` (host_pinned) имеет безусловный приоритет, а
   исходящие объявления привязываются к LAN-адресу явно (bind + ``IP_PKTINFO``),
   поэтому ответ на M-SEARCH уходит с 192.168.0.108, а не с 10.0.0.1;
3. если VPN всё-таки перехватывает LAN, владелец видит внятный совет — в
   журнале и в ``/api/studio/status``, а не «код=-1».

Тесты не требуют прав администратора: список интерфейсов подменяется
синтетическим, а живой прогон выполняется на реальном LAN-адресе машины
(пропускается, если LAN-адреса нет).
"""
from __future__ import annotations

import pathlib
import socket
import struct
import sys
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import config  # noqa: E402
from connector.printflow.studio_gateway import (  # noqa: E402
    IP_PKTINFO,
    SSDP_BROADCAST,
    SSDP_GROUP,
    SSDP_NT,
    SSDP_PORTS,
    StudioGateway,
)
from connector.tests.test_phase11 import make_db  # noqa: E402
from connector.tests.test_studio_gateway import FakeMgr  # noqa: E402

# Типичная машина владельца: LAN-адрес, VPN-туннель, APIPA и loopback.
MIXED_INTERFACES = [
    {"name": "lo", "ip": "127.0.0.1", "prefix": 8},
    {"name": "eth0", "ip": "192.168.0.108", "prefix": 24},
    {"name": "tun0", "ip": "10.0.0.1", "prefix": 30},
    {"name": "eth0", "ip": "169.254.0.21", "prefix": 30},
]


def _patched(interfaces: list[dict]):
    """Подменить перечисление интерфейсов (без прав администратора)."""
    return mock.patch("connector.printflow.config.network_interfaces",
                      return_value=list(interfaces))


class LanAddressFilterTests(unittest.TestCase):
    """get_local_ips() обязан возвращать LAN, а не адрес VPN-туннеля."""

    def test_vpn_tunnel_is_filtered_out(self):
        with _patched(MIXED_INTERFACES):
            ips = config.get_local_ips()
        self.assertIn("192.168.0.108", ips, "настоящий LAN-адрес пропал")
        self.assertNotIn("10.0.0.1", ips, "адрес tun0 утёк в LAN-список")
        self.assertNotIn("169.254.0.21", ips, "APIPA не должна выглядеть как LAN")
        self.assertNotIn("127.0.0.1", ips, "loopback не LAN-адрес")
        self.assertEqual("192.168.0.108", ips[0],
                         "первым должен идти LAN-адрес: его берёт автодетект хоста")

    def test_every_tunnel_flavour_is_recognised(self):
        for name in ("tun0", "tun12", "tap0", "utun3", "wg0", "WireGuard",
                     "OpenVPN Data Channel Offload", "Tailscale", "ZeroTier",
                     "Pangolin Tunnel", "ppp0", "l2tp0", "AmneziaWG",
                     "TAP-Windows Adapter V9", "Ethernet 3 (WireGuard Tunnel)"):
            self.assertTrue(config.is_vpn_interface(name), f"не распознан {name!r}")
        for name in ("eth0", "Ethernet", "Wi-Fi", "wlan0", "en0",
                     "Realtek PCIe GbE Family Controller",
                     "Intel(R) Wi-Fi 6 AX201"):
            self.assertFalse(config.is_vpn_interface(name), f"LAN принят за VPN: {name!r}")

    def test_point_to_point_prefix_is_never_lan(self):
        """/30 и уже — туннель или аплинк провайдера: broadcast-сегмента нет."""
        for prefix in (30, 31, 32):
            with _patched([{"name": "eth1", "ip": "192.168.7.2", "prefix": prefix}]):
                self.assertEqual([], config.get_local_ips(),
                                 f"/{prefix} не должен попадать в LAN-список")
        with _patched([{"name": "eth1", "ip": "192.168.7.2", "prefix": 29}]):
            self.assertEqual(["192.168.7.2"], config.get_local_ips())

    def test_real_lan_in_10_network_survives(self):
        """10.0.0.0/24 — настоящая LAN цеха; режется именно точка-точка."""
        with _patched([{"name": "eth0", "ip": "10.0.0.5", "prefix": 24}]):
            self.assertEqual(["10.0.0.5"], config.get_local_ips())

    def test_virtual_bridges_are_not_lan(self):
        interfaces = [
            {"name": "eth0", "ip": "192.168.0.108", "prefix": 24},
            {"name": "docker0", "ip": "172.17.0.1", "prefix": 16},
            {"name": "vEthernet (WSL)", "ip": "172.25.16.1", "prefix": 20},
            {"name": "VirtualBox Host-Only Network", "ip": "192.168.56.1", "prefix": 24},
            {"name": "VMware Network Adapter VMnet8", "ip": "192.168.137.1", "prefix": 24},
        ]
        with _patched(interfaces):
            self.assertEqual(["192.168.0.108"], config.get_local_ips())

    def test_public_and_cgnat_addresses(self):
        interfaces = [
            {"name": "eth0", "ip": "192.168.0.108", "prefix": 24},
            {"name": "ppp0", "ip": "85.12.34.56", "prefix": 32},
            {"name": "utun4", "ip": "100.101.102.103", "prefix": 32},
            {"name": "eth1", "ip": "100.64.7.8", "prefix": 16},
        ]
        with _patched(interfaces):
            ips = config.get_local_ips()
        self.assertEqual(["192.168.0.108", "100.64.7.8"], ips,
                         "LAN идёт первой, CGNAT — запасной")
        self.assertNotIn("85.12.34.56", ips, "публичный адрес в LAN не анонсируем")

    def test_rejected_addresses_carry_a_reason(self):
        with _patched(MIXED_INTERFACES):
            rejected = config.vpn_addresses()
        by_ip = {item["ip"]: item for item in rejected}
        self.assertIn("10.0.0.1", by_ip)
        self.assertEqual("tun0", by_ip["10.0.0.1"]["name"])
        self.assertIn("туннель", by_ip["10.0.0.1"]["reason"])
        self.assertIn("точка-точка", by_ip["169.254.0.21"]["reason"])
        self.assertNotIn("192.168.0.108", by_ip, "LAN-адрес не должен быть отброшен")
        self.assertNotIn("127.0.0.1", by_ip, "loopback — не «перехват», а норма")

    def test_subnet_helper(self):
        self.assertEqual("192.168.0.0/24", config.subnet_of("192.168.0.108", 24))
        self.assertEqual("10.0.0.0/30", config.subnet_of("10.0.0.1", 30))
        self.assertEqual("", config.subnet_of("не адрес"))
        self.assertEqual("192.168.0.0/24", config.subnet_of("192.168.0.108"))

    def test_machine_without_vpn_reports_nothing(self):
        with _patched([{"name": "eth0", "ip": "192.168.0.108", "prefix": 24}]):
            report = config.vpn_interception("192.168.0.108")
        self.assertFalse(report["active"])
        self.assertFalse(report["intercepting"])
        self.assertEqual("", report["advice"])


class VpnInterceptionReportTests(unittest.TestCase):
    """Совет владельцу: что именно перехватило сеть и что с этим делать."""

    def test_advice_names_the_tunnel_and_the_subnet(self):
        with _patched(MIXED_INTERFACES), \
                mock.patch("connector.printflow.config._route_default_interface",
                           return_value="tun0"), \
                mock.patch("connector.printflow.config.prefix_of", return_value=24):
            report = config.vpn_interception("192.168.0.108")
        self.assertTrue(report["active"], "tun0 должен быть замечен")
        self.assertTrue(report["intercepting"],
                        "маршрут по умолчанию через tun0 — это и есть перехват")
        self.assertTrue(report["default_is_vpn"])
        self.assertEqual(
            "VPN перехватывает LAN, отключите tun0 "
            "или добавьте 192.168.0.0/24 в split-tunnel",
            report["advice"])

    def test_pinned_host_missing_from_interfaces_is_interception(self):
        """Закреплённого адреса нет ни на одном интерфейсе — VPN его забрал."""
        with _patched([{"name": "tun0", "ip": "10.0.0.1", "prefix": 30}]), \
                mock.patch("connector.printflow.config._route_default_interface",
                           return_value="eth0"):
            report = config.vpn_interception("192.168.0.108")
        self.assertTrue(report["intercepting"])
        self.assertTrue(report["host_missing"])
        self.assertIn("192.168.0.0/24", report["advice"])

    def test_no_tunnels_means_no_advice(self):
        with _patched([{"name": "eth0", "ip": "192.168.0.108", "prefix": 24}]), \
                mock.patch("connector.printflow.config._route_default_interface",
                           return_value="eth0"):
            report = config.vpn_interception("192.168.0.108")
        self.assertEqual("", report["advice"])
        self.assertFalse(report["intercepting"])


class PinnedHostPriorityTests(unittest.TestCase):
    """host_pinned важнее автодетекта и уводит за собой SSDP и PASV."""

    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.db.set_settings({"studio_gateway_access_code": "abcd1234"})
        self.mgr = FakeMgr(self.db)
        self.gw = StudioGateway(self.db, self.mgr, bind=False)
        self.mgr.studio = self.gw

    def test_pinned_host_wins_over_detected_lan(self):
        with _patched(MIXED_INTERFACES):
            self.gw._ips_cache = []
            self.gw._ips_cache_at = 0.0
            self.assertEqual(["192.168.0.108"], self.gw._local_ips())
            self.db.set_settings({"studio_gateway_host": "192.168.5.5"})
            self.assertEqual("192.168.5.5", self.gw._host_ip())
            self.assertEqual("192.168.5.5", self.gw.identity()["host"])

    def test_pinned_host_strips_other_ips_from_ssdp_targets(self):
        """Закреплён один адрес — объявляем один адрес, без дублей в Studio."""
        self.db.set_settings({"studio_gateway_host": "192.168.0.108"})
        with _patched(MIXED_INTERFACES + [
                {"name": "eth1", "ip": "192.168.9.9", "prefix": 24}]):
            self.gw._ips_cache = []
            self.gw._ips_cache_at = 0.0
            targets = self.gw._notify_targets()
        hosts = {host for host, _port in targets}
        self.assertIn("192.168.0.108", hosts)
        self.assertNotIn("192.168.9.9", hosts,
                         "при закреплённом адресе чужие LAN-адреса не анонсируем")
        self.assertEqual([
            ("127.0.0.1", 2021),
            ("192.168.0.108", 2021),
            ("192.168.0.255", 2021),
            (SSDP_BROADCAST, 2021),
            (SSDP_GROUP, 2021),
            (SSDP_GROUP, 1990),
            (SSDP_GROUP, 1900),
        ], targets, "список рассылки разошёлся с тем, что шлёт станок")

    def test_without_pin_every_lan_address_is_announced(self):
        with _patched(MIXED_INTERFACES + [
                {"name": "eth1", "ip": "192.168.9.9", "prefix": 24}]):
            self.gw._ips_cache = []
            self.gw._ips_cache_at = 0.0
            targets = self.gw._notify_targets()
        hosts = {host for host, _port in targets}
        self.assertIn("192.168.0.108", hosts)
        self.assertIn("192.168.9.9", hosts,
                      "без закрепления два роутера — две подсети, обе анонсируем")
        self.assertNotIn("10.0.0.1", hosts, "адрес VPN не должен попасть в рассылку")

    def test_location_and_pasv_use_the_pinned_host(self):
        self.db.set_settings({"studio_gateway_host": "192.168.0.108"})
        self.assertIn("Location: 192.168.0.108", self.gw.ssdp_notify())
        self.assertIn("Location: 192.168.0.108", self.gw.ssdp_search_response())
        self.assertIn("(192,168,0,108,", self.gw.ftp_command("PASV"))

    def test_source_ip_is_the_pinned_lan_address(self):
        with _patched(MIXED_INTERFACES):
            self.gw._ips_cache = []
            self.gw._ips_cache_at = 0.0
            self.db.set_settings({"studio_gateway_host": "192.168.0.108"})
            self.assertEqual("192.168.0.108", self.gw._lan_source_ip())

    def test_loopback_host_does_not_get_pinned_as_source(self):
        self.db.set_settings({"studio_gateway_host": "127.0.0.1"})
        with _patched([]):
            self.gw._ips_cache = []
            self.gw._ips_cache_at = 0.0
            self.assertEqual("", self.gw._lan_source_ip(),
                             "привязка к loopback сломала бы доставку на 2021")

    def test_gateway_logs_the_vpn_advice_once(self):
        """Совет про VPN пишется в журнал и в errors, но не каждый цикл."""
        import logging

        captured: list[logging.LogRecord] = []

        class Collector(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        collector = Collector()
        logger = logging.getLogger("printflow")
        logger.addHandler(collector)
        try:
            with _patched(MIXED_INTERFACES), \
                    mock.patch("connector.printflow.config._route_default_interface",
                               return_value="tun0"), \
                    mock.patch("connector.printflow.config.prefix_of",
                               return_value=24):
                self.db.set_settings({"studio_gateway_host": "192.168.0.108"})
                self.gw._vpn_cache = {}
                first = self.gw._warn_vpn()
                second = self.gw._warn_vpn()
                third = self.gw._warn_vpn()
        finally:
            logger.removeHandler(collector)
        self.assertIn("VPN перехватывает LAN", first)
        self.assertIn("192.168.0.0/24", first)
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        advice_records = [record for record in captured
                          if "VPN перехватывает LAN" in record.getMessage()]
        self.assertEqual(1, len(advice_records),
                         "совет должен писаться один раз, а не каждые 5 секунд")
        self.assertEqual(first, self.gw.status()["errors"]["vpn"])
        self.assertTrue(self.gw.status()["vpn_intercepting"])

    def test_status_exposes_vpn_and_source(self):
        status = self.gw.status()
        for key in ("vpn_active", "vpn_intercepting", "vpn_names", "vpn_advice",
                    "vpn_tunnels", "ssdp_source", "host_local", "lan_ips",
                    "dropped_connections", "bind_detects", "mqtt_connections",
                    "ftp_connections", "cert_cn", "cert_san"):
            self.assertIn(key, status, f"в статусе нет {key}")
        self.assertNotIn("access_code", status)


class OutgoingSocketPinningTests(unittest.TestCase):
    """Исходящий SSDP-сокет шлёт с LAN-адреса, даже когда tun0 держит маршрут."""

    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.db.set_settings({"studio_gateway_access_code": "abcd1234",
                              "studio_gateway_host": "192.168.0.108"})
        self.mgr = FakeMgr(self.db)
        self.gw = StudioGateway(self.db, self.mgr, bind=False)
        self.mgr.studio = self.gw
        # LAN-адрес задаём сами: подставляем в кэш, чтобы тест не зависел
        # от того, какая сеть оказалась на машине запуска.
        self.gw._ips_cache = ["192.168.0.108"]
        self.gw._ips_cache_at = time.time()

    def test_notify_socket_binds_to_the_lan_address(self):
        calls: dict[str, list] = {"bind": [], "options": []}
        real_socket = socket.socket

        class Recorder:
            def __init__(self, *_a, **_k):
                self._inner = real_socket(socket.AF_INET, socket.SOCK_DGRAM)

            def setsockopt(self, level, option, value):
                calls["options"].append((level, option, value))
                return self._inner.setsockopt(level, option, value)

            def bind(self, addr):
                calls["bind"].append(tuple(addr))

            def sendto(self, payload, addr):
                calls.setdefault("sendto", []).append(tuple(addr))

            def sendmsg(self, *args, **kwargs):
                calls.setdefault("sendmsg", []).append((args, kwargs))

            def close(self):
                self._inner.close()

        with mock.patch("connector.printflow.studio_gateway.socket.socket", Recorder):
            sock, source = self.gw._make_notify_socket()
        self.assertEqual("192.168.0.108", source)
        self.assertIn(("192.168.0.108", 0), calls["bind"],
                      "объявления не привязаны к LAN-адресу — уйдут с адреса VPN")
        sock.close()

    def test_bind_failure_is_reported_not_swallowed(self):
        """Адреса нет на интерфейсах: шлюз пишет причину, а не молчит."""
        real_socket = socket.socket

        class Refuses:
            def __init__(self, *_a, **_k):
                self._inner = real_socket(socket.AF_INET, socket.SOCK_DGRAM)

            def setsockopt(self, *a, **k):
                return None

            def bind(self, addr):
                raise OSError(99, "Cannot assign requested address")

            def close(self):
                self._inner.close()

        with mock.patch("connector.printflow.studio_gateway.socket.socket", Refuses):
            sock, source = self.gw._make_notify_socket()
        self.assertEqual("", source, "привязаться не удалось — источника нет")
        self.assertIn("ssdp", self.gw.status()["errors"])
        self.assertIn("192.168.0.108", self.gw.status()["errors"]["ssdp"])
        sock.close()

    @unittest.skipUnless(IP_PKTINFO,
                         "IP_PKTINFO есть только в Linux/BSD")
    def test_sendmsg_carries_the_lan_source_in_pktinfo(self):
        sent: list[tuple] = []

        class Sender:
            def sendmsg(self, buffers, control, flags, address):
                sent.append((list(buffers), list(control), flags, tuple(address)))

            def sendto(self, payload, addr):
                sent.append((payload, addr))

        self.assertTrue(self.gw._sendto_lan(Sender(), b"NOTIFY", ("192.168.0.255", 2021)))
        self.assertEqual(1, len(sent))
        buffers, control, _flags, address = sent[0]
        self.assertEqual(("192.168.0.255", 2021), address)
        self.assertTrue(control, "отправили без управляющего сообщения IP_PKTINFO")
        level, option, data = control[0]
        self.assertEqual(socket.IPPROTO_IP, level)
        self.assertEqual(IP_PKTINFO, option)
        index, spec_dst, dst = struct.unpack("=I4s4s", data)
        self.assertEqual(socket.inet_aton("192.168.0.108"), spec_dst,
                         "источник в IP_PKTINFO не LAN-адрес — Studio ответ не примет")

    @unittest.skipUnless(IP_PKTINFO,
                         "IP_PKTINFO есть только в Linux/BSD")
    def test_falls_back_to_sendto_when_pktinfo_is_rejected(self):
        sent: list[tuple] = []

        class Legacy:
            def sendmsg(self, *_a, **_k):
                raise OSError(92, "Protocol not available")

            def sendto(self, payload, addr):
                sent.append(tuple(addr))

        self.assertTrue(self.gw._sendto_lan(Legacy(), b"NOTIFY", ("127.0.0.1", 2021)))
        self.assertEqual([("127.0.0.1", 2021)], sent)


class LoopbackAnnounceTests(unittest.TestCase):
    """Объявление в loopback уходит с loopback-источника.

    Ядро не выпустит пакет на 127.0.0.1 с источника 192.168.0.108 (``sendto``
    отвечает EINVAL). Поэтому как только у машины появлялся обычный LAN-адрес,
    единственный привязанный к нему исходящий сокет молча терял и ``NOTIFY`` на
    127.0.0.1:2021, и ответ на ``M-SEARCH`` от Studio, стоящей на этом же
    компьютере, — самый частый сценарий владельца.
    """

    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.db.set_settings({"studio_gateway_access_code": "abcd1234",
                              "studio_gateway_host": "192.168.0.108"})
        self.mgr = FakeMgr(self.db)
        self.gw = StudioGateway(self.db, self.mgr, bind=False)
        self.mgr.studio = self.gw
        self.gw._ips_cache = ["192.168.0.108"]
        self.gw._ips_cache_at = time.time()
        self.addCleanup(self.gw.stop)

    def test_loopback_and_lan_get_different_sockets(self):
        lo_sock, lo_own = self.gw._socket_for("127.0.0.1")
        lan_sock, lan_own = self.gw._socket_for("192.168.0.108")
        self.assertIsNotNone(lo_sock, "для loopback сокета нет")
        self.assertIsNotNone(lan_sock, "для LAN сокета нет")
        self.assertIsNot(lo_sock, lan_sock,
                         "в loopback и в сеть шлём одним сокетом — один из "
                         "двух пакетов ядро отбросит")
        self.assertEqual("127.0.0.1", lo_sock.getsockname()[0])
        self.assertFalse(lo_own and lan_own, "временные сокеты остались незакрытыми")

    def test_directed_broadcast_of_loopback_is_still_loopback(self):
        sock, _own = self.gw._socket_for("127.0.0.255")
        self.assertEqual("127.0.0.1", sock.getsockname()[0])

    def test_lan_send_forces_the_lan_source_but_loopback_does_not(self):
        calls: list[tuple[str, object]] = []

        class Recorder:
            def sendmsg(self, buffers, control=None, flags=0, address=None):
                calls.append(("sendmsg", (address, control)))

            def sendto(self, payload, addr):
                calls.append(("sendto", addr))

        recorder = Recorder()
        self.gw._sendto_lan(recorder, b"x", ("192.168.0.108", 2021))
        self.gw._sendto_lan(recorder, b"x", ("127.0.0.1", 2021))
        self.assertEqual("sendmsg", calls[0][0],
                         "для LAN-адреса нужен явный источник (IP_PKTINFO) — "
                         "иначе при поднятом VPN пакет уйдёт с адреса туннеля")
        if IP_PKTINFO:
            control = calls[0][1][1]
            self.assertEqual(IP_PKTINFO, control[0][1],
                             "управляющее сообщение не IP_PKTINFO")
            self.assertIn(socket.inet_aton("192.168.0.108"), control[0][2],
                          "источником указан не LAN-адрес")
        self.assertEqual("sendto", calls[1][0],
                         "в loopback источник подменять нельзя — пакет не уйдёт")

    def test_notify_reaches_a_loopback_listener_next_to_a_lan_address(self):
        """Живая отправка: LAN-адрес есть, а объявление доходит до 127.0.0.1."""
        real_lan = config.lan_addresses(config.network_interfaces(use_cache=False))
        if not real_lan:
            self.skipTest("на этой машине нет LAN-адреса — проверять не на чем")
        lan = real_lan[0]
        self.db.set_settings({"studio_gateway_host": lan})
        self.gw._ips_cache = [lan]
        self.gw._ips_cache_at = time.time()
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", 2021))
        except OSError as exc:
            listener.close()
            self.skipTest(f"UDP 2021 занят в системе: {exc}")
        listener.settimeout(5)
        self.addCleanup(listener.close)
        self.gw._last_notify = 0.0
        self.gw._broadcast_notify()
        serial = self.gw.identity()["serial"]
        deadline = time.time() + 5
        text = ""
        while time.time() < deadline:
            try:
                data, _addr = listener.recvfrom(4096)
            except socket.timeout:
                break
            text = data.decode("utf-8", "replace")
            if serial in text:
                break
        self.assertIn("NOTIFY * HTTP/1.1", text,
                      "Studio на этом же компьютере не получила объявление, "
                      "хотя LAN-адрес у машины есть")
        self.assertIn(serial, text)
        self.assertIn(f"Location: {lan}", text)
class LiveVpnBypassTests(unittest.TestCase):
    """Живой прогон: M-SEARCH отвечает LAN-адрес, а не адрес туннеля."""

    def setUp(self):
        self.lan = config.lan_addresses()
        if not self.lan:
            raise unittest.SkipTest(
                "на этой машине нет LAN-адреса (только туннели) — живой прогон "
                "невозможен; проверка фильтра есть в LanAddressFilterTests")
        self.host = self.lan[0]
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.db.set_settings({
            "studio_gateway_enabled": True,
            "studio_gateway_access_code": "abcd1234",
            "studio_gateway_host": self.host,
            "studio_gateway_mode": "queue",
        })
        self.mgr = FakeMgr(self.db)
        self.gw = StudioGateway(self.db, self.mgr, bind=True)
        self.mgr.studio = self.gw
        self.gw._start_ssdp()
        self.addCleanup(self.gw.stop)

    def test_m_search_reply_comes_from_the_lan_address(self):
        port = self.gw._ssdp_bound_port
        self.assertNotEqual(2021, port, "шлюз занял розетку Studio")
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        probe.settimeout(10)
        try:
            request = (
                "M-SEARCH * HTTP/1.1\r\n"
                f"HOST: {SSDP_GROUP}:{port}\r\n"
                'MAN: "ssdp:discover"\r\n'
                f"ST: {SSDP_NT}\r\n"
                "MX: 1\r\n\r\n"
            ).encode("utf-8")
            probe.sendto(request, (self.host, port))
            data, addr = probe.recvfrom(4096)
        finally:
            probe.close()
        text = data.decode("utf-8", "replace")
        self.assertIn("HTTP/1.1 200 OK", text)
        self.assertIn(f"Location: {self.host}", text)
        self.assertEqual(self.host, addr[0],
                         f"ответ пришёл с {addr[0]}, а объявлен {self.host} — "
                         "Studio такой ответ отбросит (так и выглядит «код=-1» "
                         "при поднятом VPN)")
        self.assertNotIn("10.0.0.1", addr, "источник ответа — адрес VPN-туннеля")
        self.assertEqual(self.host, self.gw.status()["ssdp_source"])

    def test_notify_is_sent_from_the_lan_address(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            listener.bind((self.host, 2021))
        except OSError as exc:
            listener.close()
            raise unittest.SkipTest(f"не удалось занять {self.host}:2021 — {exc}")
        listener.settimeout(10)
        try:
            self.gw._last_notify = 0.0
            self.gw._broadcast_notify()
            data, addr = listener.recvfrom(4096)
        finally:
            listener.close()
        self.assertIn("NOTIFY * HTTP/1.1", data.decode("utf-8", "replace"))
        self.assertEqual(self.host, addr[0],
                         f"объявление ушло с {addr[0]} вместо {self.host}")

    def test_targets_do_not_include_any_tunnel_address(self):
        with _patched(MIXED_INTERFACES):
            self.gw._ips_cache = []
            self.gw._ips_cache_at = 0.0
            tunnels = {item["ip"] for item in config.vpn_addresses()}
        hosts = {host for host, _port in self.gw._notify_targets()}
        self.assertFalse(hosts & tunnels,
                         f"в рассылке остались адреса туннелей: {hosts & tunnels}")
        for port in SSDP_PORTS:
            self.assertIn((SSDP_GROUP, port), self.gw._notify_targets())


class RealMachineVpnTests(unittest.TestCase):
    """Если туннель действительно поднят — проверяем на живой машине."""

    def test_real_tunnels_never_reach_the_lan_list(self):
        interfaces = config.network_interfaces(use_cache=False)
        tunnels = [item for item in interfaces
                   if config.is_vpn_interface(str(item.get("name")))]
        if not tunnels:
            self.skipTest("на этой машине нет туннельных интерфейсов")
        lan = config.lan_addresses(interfaces)
        leaked = [item["ip"] for item in tunnels if item["ip"] in lan]
        self.assertEqual([], leaked,
                         f"адреса туннелей попали в LAN-список: {leaked}")

    def test_real_point_to_point_never_reach_the_lan_list(self):
        interfaces = config.network_interfaces(use_cache=False)
        narrow = [item for item in interfaces
                  if int(item.get("prefix") or 0) >= config.POINT_TO_POINT_PREFIX]
        if not narrow:
            self.skipTest("на этой машине нет префиксов /30 и уже")
        lan = config.lan_addresses(interfaces)
        leaked = [item["ip"] for item in narrow if item["ip"] in lan]
        self.assertEqual([], leaked,
                         f"адреса точка-точка попали в LAN-список: {leaked}")


if __name__ == "__main__":
    unittest.main()

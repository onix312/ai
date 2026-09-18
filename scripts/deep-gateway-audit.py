#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Глубокая проверка шлюза Bambu Studio — до 100% уверенности, без Studio.

Запуск:

    python scripts/deep-gateway-audit.py
    python scripts/deep-gateway-audit.py --host 192.168.0.108   # живой VPN-прогон
    python scripts/deep-gateway-audit.py --skip-tests           # без unit-тестов

Скрипт проходит все слои, которые могут дать «Сбой подключения, код=-1», и
печатает ``ГЛУБОКАЯ ПРОВЕРКА: ВСЁ ОК — 100% ГОТОВО`` только если не провалилась
ни одна проверка. Предупреждения (``[ ?? ]``) вердикт не портят: ими
помечается то, что зависит от окружения (нет openssl, занят UDP 2021, на
машине нет LAN-адреса).

Разделы:

 1. Конфиг и секреты                      10. Живой прогон: FTPS, STOR, очередь
 2. Кодек кадра A5A5/A7A7                 11. Живой прогон при поднятом VPN
 3. Направленный broadcast /24            12. TLS-сертификат: CN и SAN
 4. Личность шлюза и закреплённый адрес   13. Статус: счётчики и паритет полей
 5. Эхо sequence_id                       14. Парсер брандмауэра (pf.py)
 6. SSDP: объявление и цели рассылки      15. Фронтенд карточки Studio
 7. VPN-фильтр адресов                    16. Секреты не утекают в API
 8. Перехват VPN и совет владельцу        17. Скрипт gateway-check.py
 9. Привязка исходящего сокета к LAN      18. Unit-тесты шлюза
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "connector"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from connector.printflow import config as pf_config  # noqa: E402
from connector.printflow.config import (  # noqa: E402
    DEFAULT_SETTINGS,
    SECRET_SETTINGS,
    lan_addresses,
    subnet_of,
    vpn_addresses,
    vpn_interception,
)
from connector.printflow.studio_gateway import (  # noqa: E402
    BIND_PORT_PLAIN,
    FIRMWARE_VERSION,
    IP_PKTINFO,
    MQTT_USER,
    SSDP_BROADCAST,
    SSDP_GROUP,
    SSDP_LISTEN_PORTS,
    SSDP_NOTIFY_LOOPBACK,
    SSDP_NOTIFY_PERIOD,
    SSDP_NT,
    SSDP_PORTS,
    StudioGateway,
    decode_bind_frame,
    directed_broadcast,
    encode_bind_frame,
    sequence_id_int,
)
from connector.printflow.studio_mqtt import (  # noqa: E402
    CONNACK,
    PUBLISH,
    decode_publish,
    decode_remaining_length,
    encode_connect,
    encode_publish,
    parse_fixed_header,
    read_packet,
)
from connector.printflow import studio_tls  # noqa: E402
from connector.tests.test_phase11 import make_api, make_db  # noqa: E402
from connector.tests.test_studio_gateway import FakeMgr  # noqa: E402

OK = "\033[32m[ OK ]\033[0m"
WARN = "\033[33m[ ?? ]\033[0m"
FAIL = "\033[31m[ !! ]\033[0m"

FAILURES: list[str] = []
WARNINGS: list[str] = []
PASSED = [0]

ACCESS_CODE = "12345678"          # 8 символов — как в настоящем Access Code
DETECT = {"login": {"command": "detect", "sequence_id": "20000"}}
# Полный снимок состояния: поля, без которых DeviceManager Studio падает.
PUSH_STATUS_FIELDS = (
    "command", "sequence_id", "msg", "gcode_state", "gcode_file",
    "mc_percent", "mc_remaining_time", "mc_print_stage", "mc_print_sub_stage",
    "print_error", "print_type", "lifecycle", "wifi_signal", "bed_temper",
    "bed_target_temper", "nozzle_temper", "nozzle_target_temper",
    "nozzle_diameter", "nozzle_type", "chamber_temper", "cooling_fan_speed",
    "big_fan1_speed", "big_fan2_speed", "auxiliary_fans", "spd_mag", "spd_lvl",
    "hw_switch_state", "home_flag", "door_open", "sdcard", "layer_num",
    "total_layer_num", "stg", "stg_cur", "subtask_name", "s_obj", "net",
    "online", "ipcam", "lights_report", "upgrade_state", "vt_tray", "xcam",
    "xcam_status", "device", "ams", "ams_exist_bits", "tray_now", "queue_est",
    "queue_number", "queue_sts", "queue_total", "maintain", "clean_percent",
    "mess_production_state", "ctt", "fail_reason", "job_id", "task_id",
    "project_id", "profile_id", "print_percentage", "print_real_action",
    "print_gcode_action", "force_upgrade", "fan_gear", "gcode_start_time",
    "gcode_file_prepare_percent",
)
# Смешанная машина владельца: LAN, VPN-туннель, APIPA, loopback.
MIXED_INTERFACES = [
    {"name": "lo", "ip": "127.0.0.1", "prefix": 8},
    {"name": "eth0", "ip": "192.168.0.108", "prefix": 24},
    {"name": "tun0", "ip": "10.0.0.1", "prefix": 30},
    {"name": "eth0", "ip": "169.254.0.21", "prefix": 30},
]


def ok(message: str) -> None:
    PASSED[0] += 1
    print(f"{OK} {message}")


def warn(message: str) -> None:
    WARNINGS.append(message)
    print(f"{WARN} {message}")


def fail(message: str) -> None:
    FAILURES.append(message)
    print(f"{FAIL} {message}")


def check(condition: bool, success_message: str, fail_message: str) -> bool:
    if condition:
        ok(success_message)
        return True
    fail(fail_message)
    return False


def section(title: str) -> None:
    print(f"\n\033[1m━━━ {title} ━━━\033[0m")


def free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


def make_cert(cn: str = "01P00ATEST", san: bool = True):
    """Самоподписанный сертификат через openssl — как выпускает шлюз в проде."""
    if shutil.which("openssl") is None:
        return None, None
    tmp = Path(tempfile.mkdtemp(prefix="pf-deep-"))
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    command = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
               "-keyout", str(key), "-out", str(cert), "-days", "2",
               "-subj", f"/CN={cn}"]
    if san:
        command += ["-addext", f"subjectAltName=DNS:{cn},IP:127.0.0.1"]
    subprocess.run(command, check=True, capture_output=True)
    return cert, key


def gateway(**settings) -> tuple[object, object, StudioGateway]:
    """Шлюз на временной базе (bind=False — без сети)."""
    db = make_db()
    manager = FakeMgr(db)
    db.set_settings({"studio_gateway_access_code": ACCESS_CODE, **settings})
    instance = StudioGateway(db, manager, bind=False)
    manager.studio = instance
    return db, manager, instance


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Глубокая проверка шлюза Bambu Studio (18 разделов)")
    parser.add_argument("--host", default="",
                        help="LAN-адрес для живого VPN-прогона "
                             "(по умолчанию — первый найденный)")
    parser.add_argument("--skip-tests", action="store_true",
                        help="не запускать unit-тесты шлюза (раздел 18)")
    return parser.parse_args(argv)


ARGS = parse_args()


def patched_interfaces(interfaces: list[dict]):
    return mock.patch("connector.printflow.config.network_interfaces",
                      return_value=list(interfaces))


def tls_client() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ---------------------------------------------------------------- 1. Конфиг
section("1. Конфиг и секреты")
check(not DEFAULT_SETTINGS["studio_gateway_enabled"],
      "по умолчанию выключен (безопасно)", "должен быть выключен по умолчанию")
check(DEFAULT_SETTINGS["studio_gateway_name"] == "NOZZA-PrintFlow",
      "имя по умолчанию NOZZA-PrintFlow", "имя не то")
check(DEFAULT_SETTINGS["studio_gateway_mode"] == "confirm",
      "режим confirm по умолчанию", "режим не confirm")
check("studio_gateway_access_code" in SECRET_SETTINGS,
      "access_code в SECRET_SETTINGS (маскируется)", "access_code не в секретах")
check("studio_gateway_access_code" not in str(DEFAULT_SETTINGS.get("studio_gateway_serial")),
      "серийник не содержит кода", "утечка?")
check(DEFAULT_SETTINGS["studio_gateway_host"] == "",
      "адрес по умолчанию пустой — автоопределение с VPN-фильтром",
      "studio_gateway_host не должен быть зашит")
check(hasattr(pf_config, "vpn_interception") and hasattr(pf_config, "lan_addresses"),
      "в config есть vpn_interception/lan_addresses",
      "нет функций VPN-диагностики в config")

# ------------------------------------------------------------- 2. Кодек кадра
section("2. Кодек кадра 3000/3002 (A5A5 / A7A7)")
frame = encode_bind_frame(DETECT)
check(frame[:2] == b"\xa5\xa5" and frame[-2:] == b"\xa7\xa7",
      "магия A5A5/A7A7 верна", "магия не верна")
check(len(frame) == int.from_bytes(frame[2:4], "little"),
      f"длина LE верна ({len(frame)} байт = поле длины)", "длина не верна")
payload, rest = decode_bind_frame(frame)
check(payload == DETECT and rest == b"", "roundtrip decode ок", "roundtrip сломан")
part = frame[:len(frame) // 2]
part_payload, part_rest = decode_bind_frame(part)
check(part_payload is None and part_rest == part,
      "неполный кадр ждёт остаток", "неполный кадр сломан")
glued, _ = decode_bind_frame(part_rest + frame[len(frame) // 2:])
check(glued == DETECT, "склейка буфера ок", "склейка сломана")
broken = bytearray(frame)
broken[-2:] = b"\x00\x00"
broken_payload, _ = decode_bind_frame(bytes(broken))
check(broken_payload is None, "битый хвост отбрасывается", "битый хвост не отбрасывается")
try:
    encode_bind_frame({"login": {"command": "detect", "param": "x" * 70000}})
    check(False, "", "большой кадр должен быть ValueError")
except ValueError:
    ok("большой кадр корректно ValueError")

# ------------------------------------------------------- 3. Broadcast-адрес
section("3. Направленный broadcast /24")
check(directed_broadcast("192.168.1.50") == "192.168.1.255",
      "192.168.1.50 → .255", "directed broadcast сломан")
check(directed_broadcast("192.168.0.108") == "192.168.0.255",
      "192.168.0.108 → 192.168.0.255", "адрес владельца считается неверно")
check(directed_broadcast("10.0.0.1") == "10.0.0.255",
      "10.0.0.1 → .255", "10→255 сломан")
for bad in ("192.168.1.999", "", "не адрес", "192.168.1"):
    check(directed_broadcast(bad) == "", f"{bad!r} → ''", f"{bad!r} не отфильтрован")
check(subnet_of("192.168.0.108", 24) == "192.168.0.0/24",
      "subnet_of → 192.168.0.0/24", "subnet_of сломан")

# --------------------------------------------------- 4. Личность и адрес
section("4. Личность шлюза и закреплённый адрес")
db, mgr, gw = gateway()
try:
    ident = gw.identity()
    check(ident["serial"].startswith("01P00A") and len(ident["serial"]) == 15,
          f"серийник {ident['serial']} ок (15 символов)", "серийник не верный")
    check(ident["dev_model"] == "C12", "dev_model P1S → C12 ок", "dev_model не C12")
    check("access_code" not in ident, "access_code не в identity (не утекает)",
          "утечка access_code!")
    check(ident["host"] == "127.0.0.1", "bind=False → 127.0.0.1",
          f"host {ident['host']} не 127.0.0.1")
    check(ident["version"] == FIRMWARE_VERSION, "версия прошивки 01.07.00.00",
          "версия прошивки не та")

    db.set_settings({"studio_gateway_host": "192.168.0.108"})
    check(gw._host_ip() == "192.168.0.108", "host_pinned имеет приоритет",
          "закреплённый адрес не применён")
    check("Location: 192.168.0.108" in gw.ssdp_notify(),
          "SSDP Location = закреплённый адрес", "SSDP не pinned")
    check("Location: 192.168.0.108" in gw.ssdp_search_response(),
          "ответ M-SEARCH Location = закреплённый адрес", "M-SEARCH Location не pinned")
    check("(192,168,0,108," in gw._pasv_reply(12345),
          "PASV отдаёт закреплённый адрес", "PASV не pinned")

    db.set_settings({"studio_gateway_host": "192.168.50.999"})
    check(gw._host_ip() == "127.0.0.1", "опечатка в адресе → откат к авто",
          "опечатка не откатывается")
    check("host" in gw.status()["errors"], "опечатка видна в status.errors.host",
          "опечатка не видна в диагностике")

    db.set_settings({"studio_gateway_host": ""})
    check(gw._host_ip() == "127.0.0.1", "пустой адрес → авто", "пустой не авто")
    check(not gw.status()["host_pinned"], "host_pinned=false, когда настройка пуста",
          "host_pinned не false")

    from connector.printflow.studio_gateway import _dev_model
    for model, code in (("X1C", "BL-P001"), ("P1S", "C12"), ("P1P", "C11"),
                        ("A1 mini", "N1"), ("неизвестная", "C12")):
        check(_dev_model(model) == code, f"{model} → {code}", f"{model} не → {code}")

    reply = gw.bind_reply(DETECT)
    check(reply is not None and reply["login"]["bind"] == "free",
          "bind=free (принтер не привязан к облаку)", "bind reply не free")
    check(reply["login"]["id"] == ident["serial"], "id = серийник шлюза",
          "id не серийник")
    check(reply["login"]["model"] == "C12", "model = C12", "model не C12")
    check(reply["login"]["connect"] == "lan", "connect = lan", "connect не lan")
    check(reply["login"]["dev_cap"] == 1, "dev_cap = 1", "dev_cap не 1")
    check(reply["login"]["version"] == FIRMWARE_VERSION, "version в ответе detect",
          "version в detect не та")
    login_frame = encode_bind_frame({"login": {"command": "login", "sequence_id": "20001"}})
    login_payload, _ = decode_bind_frame(gw.bind_handle_bytes(login_frame))
    check(login_payload["login"]["command"] == "login_report"
          and login_payload["login"]["status"] == "SUCCESS",
          "login → login_report SUCCESS", "login не SUCCESS")
    check(gw.bind_handle_bytes(encode_bind_frame({"login": {"command": "blah"}})) == b"",
          "неизвестная команда → b'' (станок так же молчит)", "unknown не b''")
finally:
    db.close()

# ------------------------------------------------------- 5. Эхо sequence_id
section("5. Эхо sequence_id (числом, не константой)")
db, mgr, gw = gateway()
try:
    for value, expected in (("20000", 20000), ("1", 1), ("3021", 3021),
                            (777, 777), ("0", 0), ("", 0), (None, 0),
                            ("abc", 0), ("seq-42", -42)):
        answer = gw.bind_reply({"login": {"command": "detect", "sequence_id": value}})
        got = answer["login"]["sequence_id"]
        check(got == expected and isinstance(got, int),
              f"detect sequence_id {value!r} → {got!r} (int)",
              f"detect sequence_id {value!r} → {got!r}, ждали {expected!r}")
    check(sequence_id_int("20000") == 20000 and sequence_id_int(7) == 7,
          "sequence_id_int разбирает строку и число", "sequence_id_int сломан")
    fixed = gw.bind_reply(DETECT)["login"]["sequence_id"]
    check(fixed != 3021 or DETECT["login"]["sequence_id"] == "3021",
          f"ответ не зашит константой 3021 (пришло {fixed})",
          "шлюз всё ещё отвечает фиксированным 3021")
    login_answer, _ = decode_bind_frame(gw.bind_handle_bytes(
        encode_bind_frame({"login": {"command": "login", "sequence_id": "20001"}})))
    check(login_answer["login"]["sequence_id"] == 20001,
          "login_report тоже эхо (20001)", "login_report не эхо")
    wire = gw.bind_handle_bytes(encode_bind_frame(DETECT))
    decoded, _ = decode_bind_frame(wire)
    check(json.loads(wire[4:-2].decode("utf-8")) == decoded,
          "кадр на проводе разбирается тем же JSON", "кадр на проводе расходится")
finally:
    db.close()

# ------------------------------------------------- 6. SSDP: объявление/цели
section("6. SSDP: объявление и цели рассылки")
db, mgr, gw = gateway()
try:
    text = gw.ssdp_notify()
    for header in ("DevModel.bambu.com:", "DevName.bambu.com:", "DevConnect.bambu.com:",
                   "DevBind.bambu.com:", "Devseclink.bambu.com:", "DevInf.bambu.com:",
                   "DevVersion.bambu.com:", "DevCap.bambu.com:"):
        check(header in text, f"NOTIFY содержит {header}", f"нет {header}")
    check(f"DevVersion.bambu.com: {FIRMWARE_VERSION}" in text,
          "версия прошивки в NOTIFY", "нет версии")
    check("DevBind.bambu.com: free" in text and "DevConnect.bambu.com: lan" in text,
          "DevBind=free, DevConnect=lan", "DevBind/DevConnect не те")
    check("NTS: ssdp:alive" in text and SSDP_NT in text,
          "NTS ssdp:alive и urn bambulab", "нет NTS/urn")
    search = gw.ssdp_search_response()
    check(search.startswith("HTTP/1.1 200 OK"), "ответ M-SEARCH — 200 OK",
          "ответ M-SEARCH не 200 OK")
    for header in ("DevModel.bambu.com:", "DevName.bambu.com:", "Devseclink.bambu.com:",
                   "DevVersion.bambu.com:", "DevCap.bambu.com:", "Location:"):
        check(header in search, f"200 OK содержит {header}", f"в 200 OK нет {header}")

    db.set_settings({"studio_gateway_host": "192.168.0.108"})
    with patched_interfaces(MIXED_INTERFACES + [
            {"name": "eth1", "ip": "192.168.9.9", "prefix": 24}]):
        gw._ips_cache = []
        gw._ips_cache_at = 0.0
        targets = gw._notify_targets()
    expected = [("127.0.0.1", 2021), ("192.168.0.108", 2021),
                ("192.168.0.255", 2021), (SSDP_BROADCAST, 2021),
                (SSDP_GROUP, 2021), (SSDP_GROUP, 1990), (SSDP_GROUP, 1900)]
    check(targets == expected,
          "при закреплённом адресе цели ровно станочные: "
          + ", ".join(f"{h}:{p}" for h, p in targets),
          f"цели разошлись: {targets}")
    hosts = {host for host, _ in targets}
    check("192.168.9.9" not in hosts and "10.0.0.1" not in hosts,
          "при host_pinned чужие LAN- и VPN-адреса не добавляются",
          f"в целях лишние адреса: {sorted(hosts)}")
    db.set_settings({"studio_gateway_host": ""})
    with patched_interfaces(MIXED_INTERFACES):
        gw._ips_cache = []
        gw._ips_cache_at = 0.0
        auto_targets = gw._notify_targets()
    check(("192.168.0.108", 2021) in auto_targets,
          "без закрепления свой LAN-адрес анонсируется", "свой адрес не анонсируется")
    check(not any(host == "10.0.0.1" for host, _ in auto_targets),
          "адрес tun0 не попадает в цели рассылки", "адрес VPN в целях рассылки!")

    check(2021 not in SSDP_LISTEN_PORTS, "2021 не в SSDP_LISTEN_PORTS",
          "2021 в LISTEN — ломает Studio!")
    check(2021 in SSDP_PORTS, "2021 в SSDP_PORTS (туда шлём)", "2021 не в PORTS")
    check(tuple(SSDP_LISTEN_PORTS) == (1900,), "слушаем только :1900",
          f"LISTEN {SSDP_LISTEN_PORTS}")
    check(SSDP_NOTIFY_PERIOD <= 6.0, f"период объявлений {SSDP_NOTIFY_PERIOD} с ≤ 6 с",
          "период слишком редкий — плагин не дождётся")
    check(SSDP_NOTIFY_LOOPBACK == "127.0.0.1", "loopback-цель 127.0.0.1",
          "loopback-цель не та")
    status = gw.status()
    check("127.0.0.1:2021" in status["ssdp_targets"],
          "status.ssdp_targets включает 127.0.0.1:2021", "нет loopback в статусе")
finally:
    db.close()

# --------------------------------------------------- 7. VPN-фильтр адресов
section("7. VPN-фильтр адресов (причина «код=-1» при поднятом tun0)")
with patched_interfaces(MIXED_INTERFACES):
    lan = lan_addresses()
    tunnels = vpn_addresses()
check(lan == ["192.168.0.108"], f"LAN-список = {lan} (tun0 отфильтрован)",
      f"LAN-список неверный: {lan}")
check("10.0.0.1" not in lan, "10.0.0.1/30 (tun0) не в LAN-списке",
      "адрес VPN-туннеля утёк в LAN-список!")
check("169.254.0.21" not in lan, "APIPA 169.254.0.21/30 не в LAN-списке",
      "APIPA утёк в LAN-список")
check("127.0.0.1" not in lan, "loopback не в LAN-списке", "loopback утёк")
by_ip = {item["ip"]: item for item in tunnels}
check("10.0.0.1" in by_ip and by_ip["10.0.0.1"]["name"] == "tun0",
      "tun0 10.0.0.1 распознан как туннель", "tun0 не распознан")
check("туннель" in by_ip["10.0.0.1"]["reason"],
      f"причина отбраковки tun0: {by_ip['10.0.0.1']['reason']}", "нет причины для tun0")
check("точка-точка" in by_ip["169.254.0.21"]["reason"],
      f"причина отбраковки /30: {by_ip['169.254.0.21']['reason']}",
      "нет причины для /30")
for name in ("tun0", "tap0", "utun3", "wg0", "WireGuard", "Tailscale", "ZeroTier",
             "ppp0", "TAP-Windows Adapter V9", "docker0", "vEthernet (WSL)",
             "VirtualBox Host-Only Network", "VMware Network Adapter VMnet8"):
    check(pf_config.is_vpn_interface(name), f"{name} — туннель/виртуальный",
          f"{name} не распознан как VPN")
for name in ("eth0", "Ethernet", "Wi-Fi", "wlan0", "en0",
             "Realtek PCIe GbE Family Controller"):
    check(not pf_config.is_vpn_interface(name), f"{name} — обычный LAN-адаптер",
          f"{name} ошибочно принят за VPN")
with patched_interfaces([{"name": "eth0", "ip": "10.0.0.5", "prefix": 24}]):
    check(lan_addresses() == ["10.0.0.5"],
          "настоящая LAN 10.0.0.0/24 survives (режется только точка-точка)",
          "10.0.0.0/24 ошибочно отфильтрован")
for prefix in (30, 31, 32):
    with patched_interfaces([{"name": "eth9", "ip": "192.168.7.2", "prefix": prefix}]):
        check(lan_addresses() == [], f"/{prefix} — не LAN (broadcast-сегмента нет)",
              f"/{prefix} попал в LAN-список")
with patched_interfaces([{"name": "eth0", "ip": "192.168.0.108", "prefix": 24},
                         {"name": "utun4", "ip": "100.101.102.103", "prefix": 32},
                         {"name": "eth1", "ip": "100.64.7.8", "prefix": 16}]):
    check(lan_addresses() == ["192.168.0.108", "100.64.7.8"],
          "CGNAT остаётся запасным, LAN — первым", "порядок адресов неверный")
real_interfaces = pf_config.network_interfaces(use_cache=False)
real_tunnels = [item for item in real_interfaces
                if pf_config.is_vpn_interface(str(item.get("name")))]
real_lan = lan_addresses(real_interfaces)
leaked = [item["ip"] for item in real_tunnels if item["ip"] in real_lan]
check(not leaked,
      f"на живой машине туннели ({len(real_tunnels)}) не попали в LAN-список {real_lan}",
      f"на живой машине утекли адреса туннелей: {leaked}")

# ---------------------------------------------- 8. Перехват VPN и совет
section("8. Перехват VPN: совет владельцу")
with patched_interfaces(MIXED_INTERFACES), \
        mock.patch("connector.printflow.config._route_default_interface",
                   return_value="tun0"), \
        mock.patch("connector.printflow.config.prefix_of", return_value=24):
    report = vpn_interception("192.168.0.108")
check(report["active"], "туннели замечены (active=true)", "туннели не замечены")
check(report["intercepting"],
      "маршрут по умолчанию через tun0 → intercepting=true",
      "перехват не определён, хотя default route через tun0")
check(report["advice"] == "VPN перехватывает LAN, отключите tun0 "
                          "или добавьте 192.168.0.0/24 в split-tunnel",
      f"совет: {report['advice']}",
      f"совет не тот: {report['advice']!r}")
check("eth0" not in report["names"],
      "в совете только туннель (APIPA-адрес eth0 не подмешан)",
      f"в совете лишние адаптеры: {report['names']}")
with patched_interfaces([{"name": "tun0", "ip": "10.0.0.1", "prefix": 30}]), \
        mock.patch("connector.printflow.config._route_default_interface",
                   return_value="eth0"):
    missing = vpn_interception("192.168.0.108")
check(missing["intercepting"] and missing["host_missing"],
      "закреплённого адреса нет на интерфейсах → intercepting",
      "пропажа закреплённого адреса не замечена")
check("192.168.0.0/24" in missing["advice"],
      f"совет называет подсеть: {missing['advice']}", "в совете нет подсети")
with patched_interfaces([{"name": "eth0", "ip": "192.168.0.108", "prefix": 24}]), \
        mock.patch("connector.printflow.config._route_default_interface",
                   return_value="eth0"):
    clean = vpn_interception("192.168.0.108")
check(not clean["intercepting"] and clean["advice"] == "",
      "без туннелей совет не выдаётся (не пугаем зря)",
      "совет выдаётся на чистой машине")
with patched_interfaces(MIXED_INTERFACES):
    loopback = vpn_interception("127.0.0.1")
check(not loopback["intercepting"],
      "закреплённый 127.0.0.1 — не перехват (Studio и PrintFlow на одном ПК)",
      "loopback-хост признан перехватом")

import logging  # noqa: E402


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


db, mgr, gw = gateway(studio_gateway_host="192.168.0.108")
collector = _Collector()
logger = logging.getLogger("printflow")
logger.addHandler(collector)
try:
    with patched_interfaces(MIXED_INTERFACES), \
            mock.patch("connector.printflow.config._route_default_interface",
                       return_value="tun0"), \
            mock.patch("connector.printflow.config.prefix_of", return_value=24):
        gw._vpn_cache = {}
        first = gw._warn_vpn()
        gw._warn_vpn()
        gw._warn_vpn()
        status = gw.status()
finally:
    logger.removeHandler(collector)
    db.close()
check("VPN перехватывает LAN" in first and "192.168.0.0/24" in first,
      f"шлюз пишет совет в журнал: {first}", "шлюз не пишет совет про VPN")
advice_lines = [line for line in collector.records if "VPN перехватывает LAN" in line]
check(len(advice_lines) == 1,
      "совет написан один раз (не спамим каждые 5 секунд)",
      f"совет написан {len(advice_lines)} раз")
check(status["vpn_intercepting"] and status["errors"].get("vpn") == first,
      "перехват виден в /api/studio/status (vpn_intercepting + errors.vpn)",
      "перехват не виден в статусе")
check("access_code" not in status, "Access Code в статус не попадает",
      "утечка access_code в статусе")

# ------------------------------------- 9. Привязка исходящего сокета к LAN
section("9. Исходящий SSDP-сокет привязан к LAN-адресу (обход tun0)")
db, mgr, gw = gateway(studio_gateway_host="192.168.0.108")
try:
    with patched_interfaces(MIXED_INTERFACES):
        gw._ips_cache = []
        gw._ips_cache_at = 0.0
        check(gw._lan_source_ip() == "192.168.0.108",
              "источник объявлений = закреплённый LAN-адрес",
              f"источник = {gw._lan_source_ip()!r}")
        recorded: dict[str, list] = {"bind": [], "options": []}
        real_socket = socket.socket

        class Recorder:
            def __init__(self, *_a, **_k):
                self._inner = real_socket(socket.AF_INET, socket.SOCK_DGRAM)

            def setsockopt(self, level, option, value):
                recorded["options"].append((level, option, value))
                return self._inner.setsockopt(level, option, value)

            def bind(self, addr):
                recorded["bind"].append(tuple(addr))

            def sendmsg(self, buffers, control=None, flags=0, address=None):
                recorded.setdefault("sendmsg", []).append((list(control), address))

            def sendto(self, payload, addr):
                recorded.setdefault("sendto", []).append(tuple(addr))

            def close(self):
                self._inner.close()

        with mock.patch("connector.printflow.studio_gateway.socket.socket", Recorder):
            sock, source = gw._make_notify_socket()
        check(source == "192.168.0.108", f"сокет объявляет источник {source}",
              f"источник не LAN: {source!r}")
        check(("192.168.0.108", 0) in recorded["bind"],
              "сокет привязан (bind) к 192.168.0.108 — ядро не выберет tun0",
              f"bind не вызван или не к тому адресу: {recorded['bind']}")
        if IP_PKTINFO:
            check((socket.IPPROTO_IP, IP_PKTINFO, 1) in recorded["options"],
                  "IP_PKTINFO включён на исходящем сокете",
                  "IP_PKTINFO не включён")
        sock.close()

        class Sender:
            def __init__(self):
                self.calls = []

            def sendmsg(self, buffers, control=None, flags=0, address=None):
                self.calls.append(("sendmsg", list(control or []), address))

            def sendto(self, payload, addr):
                self.calls.append(("sendto", [], addr))

        sender = Sender()
        check(gw._sendto_lan(sender, b"NOTIFY", ("192.168.0.255", 2021)),
              "дейтаграмма отправлена", "отправка не удалась")
        if IP_PKTINFO:
            kind, control, address = sender.calls[0]
            check(kind == "sendmsg" and control,
                  "отправка идёт через sendmsg с IP_PKTINFO",
                  f"отправка без IP_PKTINFO: {sender.calls[0]}")
            if control:
                level, option, data = control[0]
                _index, spec_dst, _addr = struct.unpack("=I4s4s", data)
                check(level == socket.IPPROTO_IP and option == IP_PKTINFO,
                      "cmsg — IPPROTO_IP/IP_PKTINFO", f"cmsg не тот: {level}/{option}")
                check(spec_dst == socket.inet_aton("192.168.0.108"),
                      "ipi_spec_dst = 192.168.0.108 (источник задан явно)",
                      f"ipi_spec_dst = {spec_dst!r}")
                check(address == ("192.168.0.255", 2021),
                      "адрес назначения сохранён", f"адрес назначения {address}")
        else:
            warn("IP_PKTINFO недоступен на этой платформе — источник задаётся bind")

        class Legacy:
            def __init__(self):
                self.sent = []

            def sendmsg(self, *_a, **_k):
                raise OSError(92, "Protocol not available")

            def sendto(self, payload, addr):
                self.sent.append(tuple(addr))

        legacy = Legacy()
        check(gw._sendto_lan(legacy, b"NOTIFY", ("127.0.0.1", 2021))
              and legacy.sent == [("127.0.0.1", 2021)],
              "без IP_PKTINFO работает откат на sendto (привязанный сокет)",
              "откат на sendto сломан")

        class Refuses:
            def __init__(self, *_a, **_k):
                self._inner = real_socket(socket.AF_INET, socket.SOCK_DGRAM)

            def setsockopt(self, *_a, **_k):
                return None

            def bind(self, addr):
                raise OSError(99, "Cannot assign requested address")

            def close(self):
                self._inner.close()

        with mock.patch("connector.printflow.studio_gateway.socket.socket", Refuses):
            refused, refused_source = gw._make_notify_socket()
        check(refused_source == "",
              "если привязаться не удалось — источника нет (не молча чужой адрес)",
              f"источник {refused_source!r} при неудачном bind")
        check("не удалось привязать" in gw.status()["errors"].get("ssdp", ""),
              "причина неудачной привязки видна в errors.ssdp",
              "неудачная привязка не видна в диагностике")
        refused.close()
finally:
    db.close()

# ------------------------------------------------------ 10. Живой прогон
section("10. Живой прогон: шлюз глазами Studio")
cert, key = make_cert()
if cert is None:
    warn("openssl нет — живые TLS-проверки пропущены (на Windows ставится с Git for Windows)")
else:
    db = make_db()
    mgr = FakeMgr(db)
    db.set_settings({"studio_gateway_enabled": True,
                     "studio_gateway_access_code": ACCESS_CODE,
                     "studio_gateway_host": "127.0.0.1",
                     "studio_gateway_mode": "queue"})
    gw = StudioGateway(db, mgr, bind=True)
    mgr.studio = gw
    ports = {"MQTT_PORT": free_port(), "FTP_PORT": free_port(),
             "BIND_PORT_PLAIN": free_port(), "BIND_PORT_TLS": free_port()}
    patches = [mock.patch(f"connector.printflow.studio_gateway.{name}", port)
               for name, port in ports.items()]
    patches.append(mock.patch("connector.printflow.studio_tls.ensure_certificate",
                              return_value=(cert, key)))
    for patch in patches:
        patch.start()
    try:
        gw.start()
        time.sleep(0.5)
        st = gw.status()
        check(st["bind_running"],
              f"bind_running на {ports['BIND_PORT_PLAIN']}/{ports['BIND_PORT_TLS']}",
              "bind не поднялся")
        check(st["mqtt_running"], f"mqtt_running на {ports['MQTT_PORT']}",
              "mqtt не поднялся")
        check(st["ftp_running"], f"ftp_running на {ports['FTP_PORT']}",
              "ftp не поднялся")
        check(st["ssdp_running"], "ssdp_running", "ssdp не поднялся")
        check(st["ssdp_bound_port"] != 2021,
              f"ssdp слушает :{st['ssdp_bound_port']}, не 2021", "ssdp bound 2021!")
        check(not st["errors"], f"ошибок служб нет: {st['errors']}",
              f"при старте возникли ошибки: {st['errors']}")

        def detect(port: int, use_tls: bool = False) -> dict:
            raw = socket.create_connection(("127.0.0.1", port), timeout=5)
            conn = tls_client().wrap_socket(raw) if use_tls else raw
            try:
                conn.sendall(encode_bind_frame(DETECT))
                data, _ = decode_bind_frame(conn.recv(4096))
                return data or {}
            finally:
                conn.close()

        plain = detect(ports["BIND_PORT_PLAIN"])
        check(plain.get("login", {}).get("command") == "detect",
              "detect на :3000 отвечает", f"plain не отвечает: {plain}")
        check(plain["login"]["id"] == gw.identity()["serial"],
              "id в ответе = серийник шлюза", "id не тот")
        check(plain["login"]["sequence_id"] == 20000,
              f"эхо sequence_id = {plain['login']['sequence_id']}",
              "эхо sequence_id неверное")
        over_tls = detect(ports["BIND_PORT_TLS"], True)
        check(over_tls.get("login", {}).get("command") == "detect",
              "detect на :3002 через TLS отвечает", f"TLS не отвечает: {over_tls}")

        for port in (ports["MQTT_PORT"], ports["BIND_PORT_TLS"]):
            stray = socket.create_connection(("127.0.0.1", port), timeout=5)
            try:
                stray.sendall(b"GET / HTTP/1.0\r\n\r\n")
            except OSError:
                pass
            stray.close()
        time.sleep(0.3)
        check(detect(ports["BIND_PORT_PLAIN"]).get("login", {}).get("command") == "detect",
              "после мусорного клиента :3000 жив", "после мусора :3000 сломался")
        check(detect(ports["BIND_PORT_TLS"], True).get("login", {}).get("command") == "detect",
              "после мусорного клиента :3002 жив", "после мусора :3002 сломался")
        check(gw.status()["dropped_connections"] >= 2,
              f"dropped_connections = {gw.status()['dropped_connections']}",
              "отброшенные соединения не считаются")

        ctx = tls_client()

        def mqtt_connect(password: str):
            raw = socket.create_connection(("127.0.0.1", ports["MQTT_PORT"]), timeout=5)
            conn = ctx.wrap_socket(raw)
            conn.sendall(encode_connect(client_id="audit", username=MQTT_USER,
                                        password=password))
            buf = conn.recv(1) + conn.recv(1)
            while buf[-1] & 0x80:
                buf += conn.recv(1)
            length, offset = decode_remaining_length(buf, 1)
            need = offset + length - len(buf)
            while need > 0:
                chunk = conn.recv(need)
                if not chunk:
                    break
                buf += chunk
                need -= len(chunk)
            ptype, _flags, payload = parse_fixed_header(buf)
            return conn, ptype, payload

        conn_ok, ptype, payload = mqtt_connect(ACCESS_CODE)
        check(ptype == CONNACK and payload[1] == 0,
              f"MQTT CONNECT bblp/{ACCESS_CODE} → CONNACK 0",
              f"MQTT good rc={payload[1] if payload else 'нет ответа'}")
        conn_bad, ptype_bad, payload_bad = mqtt_connect("неверный")
        check(ptype_bad == CONNACK and payload_bad[1] == 4,
              "MQTT с неверным кодом → CONNACK 4",
              f"MQTT bad rc={payload_bad[1] if payload_bad else 'нет ответа'}")
        conn_bad.close()

        # Авторизация у каждого соединения своя. Чужой неверный вход (или чужой
        # DISCONNECT) не должен глушить уже работающую Studio: иначе соединение
        # живое, CONNACK был 0, а device/<серийник>/report молчит.
        live_topic = f"device/{gw.identity()['serial']}/report"

        def live_report(conn) -> str:
            """get_version по живому соединению → пришедшая тема ('' если нет)."""
            conn.sendall(encode_publish(
                f"device/{gw.identity()['serial']}/request",
                json.dumps({"info": {"command": "get_version",
                                     "sequence_id": "3"}})))
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    rtype, rflags, rpayload = read_packet(
                        conn.recv, max(0.2, deadline - time.time()))
                except Exception:
                    return ""
                if rtype != PUBLISH:
                    continue
                pub = decode_publish(rflags, rpayload)
                if pub.get("topic") == live_topic:
                    return live_topic
            return ""

        before = live_report(conn_ok)
        check(before == live_topic,
              "отчёт get_version пришёл по живому MQTT-соединению",
              "по живому соединению отчёта нет ещё до чужого входа")
        conn_bad2, _ptype_bad2, _payload_bad2 = mqtt_connect("неверный-код")
        conn_bad2.close()
        after = live_report(conn_ok)
        check(after == live_topic,
              "чужой неверный вход не заглушил работающее соединение",
              "после чужой неудачной авторизации отчёты пропали "
              "(авторизация общая на весь шлюз, а должна быть у каждого своя)")
        conn_ok.close()
        conn_left, _ptype_left, _payload_left = mqtt_connect(ACCESS_CODE)
        check(live_report(conn_left) == live_topic,
              "новый клиент получает отчёты после ухода предыдущего",
              "после закрытия соединения шлюз перестал отвечать новым клиентам")
        conn_left.close()
        check(gw.status()["mqtt_connections"] >= 1,
              f"mqtt_connections = {gw.status()['mqtt_connections']} (не -1, не 0)",
              f"mqtt_connections = {gw.status()['mqtt_connections']}")
        check(gw.status()["mqtt_auth_failures"] >= 1,
              f"mqtt_auth_failures = {gw.status()['mqtt_auth_failures']}",
              "счётчик отказов MQTT не растёт")

        raw = socket.create_connection(("127.0.0.1", ports["FTP_PORT"]), timeout=5)
        conn = ctx.wrap_socket(raw)

        def ftp_read() -> str:
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(1)
                if not chunk:
                    break
                buf += chunk
            return buf.decode("utf-8", "replace")

        banner = ftp_read()
        check(banner.startswith("220"), f"FTPS banner: {banner.strip()}",
              f"нет 220: {banner!r}")
        conn.sendall(b"USER bblp\r\n")
        check("331" in ftp_read(), "USER bblp → 331", "нет 331")
        conn.sendall(b"PASS wrong\r\n")
        check("530" in ftp_read(), "PASS неверный → 530", "нет 530")
        conn.sendall(b"USER bblp\r\n")
        ftp_read()
        conn.sendall(f"PASS {ACCESS_CODE}\r\n".encode())
        check("230" in ftp_read(), f"PASS {ACCESS_CODE} → 230", "нет 230")
        conn.sendall(b"PASV\r\n")
        pasv = ftp_read()
        check("227" in pasv and "(127,0,0,1," in pasv,
              f"PASV → {pasv.strip()}", f"PASV не тот: {pasv!r}")
        conn.close()

        def ftps_upload(filename: str, blob: bytes, tls_data: bool = True) -> str:
            raw2 = socket.create_connection(("127.0.0.1", ports["FTP_PORT"]), timeout=5)
            client = ctx.wrap_socket(raw2)

            def read() -> str:
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = client.recv(1)
                    if not chunk:
                        break
                    buf += chunk
                return buf.decode("utf-8", "replace")

            read()
            client.sendall(b"USER bblp\r\n")
            read()
            client.sendall(f"PASS {ACCESS_CODE}\r\n".encode())
            read()
            client.sendall(b"TYPE I\r\n")
            read()
            client.sendall(b"PASV\r\n")
            answer = read()
            numbers = re.search(r"\((\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\)", answer)
            host = ".".join(numbers.group(index) for index in range(1, 5))
            port = int(numbers.group(5)) * 256 + int(numbers.group(6))
            data_raw = socket.create_connection((host, port), timeout=5)
            data = ctx.wrap_socket(data_raw) if tls_data else data_raw
            client.sendall(f"STOR {filename}\r\n".encode())
            read()
            data.sendall(blob)
            if hasattr(data, "unwrap"):
                try:
                    data.unwrap()
                except (OSError, ValueError):
                    pass
            data.close()
            result = read()
            client.close()
            return result

        result = ftps_upload("plate.gcode.3mf", b"3MF-BYTES", True)
        check("226" in result, "STOR с TLS-каналом данных → 226", f"TLS data: {result!r}")
        check(len(mgr.enqueued) == 1 and mgr.enqueued[-1]["source"] == "studio-gateway",
              "файл попал в очередь PrintFlow (source=studio-gateway)",
              "файл не попал в очередь")
        check(gw.status()["ftp_uploads"] >= 1,
              f"ftp_uploads = {gw.status()['ftp_uploads']}", "загрузки не считаются")
        result_plain = ftps_upload("plain.gcode.3mf", b"PLAIN", False)
        check("226" in result_plain, "STOR с plain-каналом данных → 226",
              f"plain data: {result_plain!r}")

        ssdp_sock = gw._ssdp_sock
        ssdp_port = ssdp_sock.getsockname()[1]
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(5)
        request = (f'M-SEARCH * HTTP/1.1\r\nHOST: {SSDP_GROUP}:{ssdp_port}\r\n'
                   f'MAN: "ssdp:discover"\r\nST: {SSDP_NT}\r\nMX: 1\r\n\r\n')
        probe.sendto(request.encode("utf-8"), ("127.0.0.1", ssdp_port))
        data, source = probe.recvfrom(4096)
        probe.close()
        answer_text = data.decode("utf-8", "replace")
        check("HTTP/1.1 200 OK" in answer_text and "DevVersion.bambu.com:" in answer_text,
              f"M-SEARCH → 200 OK с {source[0]} + DevVersion",
              f"M-SEARCH не ок: {answer_text[:200]}")
        check(f"Location: {gw.identity()['host']}" in answer_text,
              "в ответе M-SEARCH есть Location", "нет Location в ответе")

        studio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            studio_sock.bind(("127.0.0.1", 2021))
            studio_sock.settimeout(8)
            gw._last_notify = 0.0
            gw._broadcast_notify()
            data, notify_source = studio_sock.recvfrom(4096)
            notify_text = data.decode("utf-8", "replace")
            check("NOTIFY * HTTP/1.1" in notify_text
                  and gw.identity()["serial"] in notify_text,
                  f"NOTIFY доходит на 127.0.0.1:2021 (источник {notify_source[0]})",
                  "NOTIFY не дошёл на 2021")
        except OSError as exc:
            warn(f"UDP 2021 занят в системе ({exc}) — NOTIFY-прогон пропущен")
        finally:
            studio_sock.close()

        ident = gw.identity()
        gw.mqtt_handle_packet(encode_connect(username=MQTT_USER, password=ACCESS_CODE))
        gw._incoming["z.gcode.3mf"] = b"data-123"
        packet = encode_publish(
            f"device/{ident['serial']}/request",
            json.dumps({"print": {"command": "project_file",
                                  "url": "ftp:///z.gcode.3mf", "sequence_id": "9"}}))
        replies = gw.mqtt_handle_packet(packet)
        success = False
        for reply in replies:
            ptype, flags, payload = parse_fixed_header(reply)
            if ptype != PUBLISH:
                continue
            body = json.loads(decode_publish(flags, payload)["payload"])
            if body.get("print", {}).get("result") == "success":
                success = True
        check(success, "MQTT project_file → success в device/<serial>/report",
              "project_file не success")
        check(len(mgr.enqueued) >= 2, "project_file тоже положил задание в очередь",
              "project_file не дошёл до очереди")

        # Размеры отчётов: get_version у станка занимает 272 байта пакета.
        report_topic = f"device/{ident['serial']}/report"
        sizes = {}
        for name, body in (("get_version",
                            {"info": {"command": "get_version", "sequence_id": "2"}}),
                           ("pushall",
                            {"pushing": {"command": "pushall", "sequence_id": "0"}})):
            reports = gw.handle_mqtt_request(body)
            raw = json.dumps(reports[0], ensure_ascii=False, separators=(",", ":"))
            sizes[name] = len(encode_publish(report_topic, raw))
        check(sizes["get_version"] == 272,
              f"пакет get_version = {sizes['get_version']} байт (как у станка 272)",
              f"get_version = {sizes['get_version']} байт, у станка 272")
        check(sizes["pushall"] >= 727,
              f"пакет pushall = {sizes['pushall']} байт (полный снимок, ≥727)",
              f"pushall = {sizes['pushall']} байт — снимок урезан")
        push = gw.handle_mqtt_request({"pushing": {"command": "pushall"}})[0]["print"]
        absent = [field for field in PUSH_STATUS_FIELDS if field not in push]
        check(not absent,
              f"в push_status {len(push)} полей станка, все на месте",
              f"в push_status нет полей: {absent}")
        check(push["msg"] == 0 and push["gcode_state"] == "IDLE",
              "msg=0 (полный снимок), gcode_state=IDLE", "msg/gcode_state не те")
        check(isinstance(push["ams"], list),
              "ams — список (как у станка), а не вложенный объект",
              f"ams неверной формы: {type(push['ams'])}")
        version = gw.handle_mqtt_request(
            {"info": {"command": "get_version", "sequence_id": "7"}})[0]["info"]
        check(version["module"][0]["sn"] == ident["serial"]
              and version["module"][0]["sw_ver"] == FIRMWARE_VERSION,
              "get_version: sn = серийник, sw_ver = 01.07.00.00",
              "get_version неверный")
        check(version["sequence_id"] == "7", "get_version эхо sequence_id",
              "get_version не эхо")
        code_report = gw.handle_mqtt_request(
            {"system": {"command": "get_access_code", "sequence_id": "2"}})[0]
        check(code_report["system"]["access_code"] == ACCESS_CODE,
              "system/get_access_code возвращает код (плагин его ждёт)",
              "get_access_code не вернул код")

        st = gw.status()
        for field in ("host", "host_pinned", "mqtt_port", "ftp_port", "bind_port",
                      "bind_tls_port", "bind_running", "ssdp_targets", "errors",
                      "dropped_connections", "mqtt_connections", "ftp_connections",
                      "bind_requests", "bind_detects", "ssdp_source", "vpn_active",
                      "vpn_intercepting", "cert_cn", "cert_san", "cert_expires",
                      "lan_ips"):
            check(field in st, f"status содержит {field}", f"нет {field} в status")
        check(st["mqtt_port"] == ports["MQTT_PORT"], "status.mqtt_port совпадает",
              "mqtt_port не совпадает")
        # В живом прогоне четыре пробы личности: :3000, :3002 и по одной
        # повторной после мусорного клиента (проверка, что служба не умерла).
        check(st["bind_requests"] >= 4 and st["bind_detects"] >= 4,
              f"bind_requests={st['bind_requests']}, bind_detects={st['bind_detects']}",
              "счётчики проб личности не растут")
        check(st["cert_cn"] == ident["serial"], f"cert_cn = {st['cert_cn']}",
              "cert_cn не серийник")
        # Просроченный сертификат Studio отвергает так же молча, как сертификат
        # без SAN, поэтому срок действия показываем в статусе и проверяем здесь.
        expiry = str(st.get("cert_expires") or "")
        check(bool(expiry) and expiry > datetime.now(timezone.utc).date().isoformat(),
              f"cert_expires = {expiry or 'не заполнен'} (сертификат действующий)",
              f"cert_expires = {expiry or 'не заполнен'} — сертификат истёк "
              "или срок не показан")
        check(gw._tls_ctx is not None, "TLS-контекст создан", "нет TLS-контекста")
        check(gw._tls_ctx.minimum_version == ssl.TLSVersion.TLSv1_2,
              "TLS minimum_version = 1.2", "min TLS не 1.2")
        cipher_names = [item["name"] for item in gw._tls_ctx.get_ciphers()]
        check(any("GCM" in name for name in cipher_names),
              f"в наборах есть AES-GCM ({len(cipher_names)} наборов)", "нет GCM-наборов")

        db.close()
        try:
            gw.identity()
            gw.status()
            gw._broadcast_notify()
            check(True, "после закрытия БД identity/status/broadcast не падают", "")
            time.sleep(0.2)
            check(gw._ssdp_sock is not None, "SSDP-поток жив после закрытия БД",
                  "SSDP-поток упал")
        except Exception as exc:
            fail(f"после закрытия БД упало: {exc}")
    finally:
        for patch in patches:
            patch.stop()
        try:
            gw.stop()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass
        shutil.rmtree(Path(cert).parent, ignore_errors=True)

# --------------------------------------------- 11. Живой прогон при VPN
section("11. Живой прогон при поднятом VPN: источник объявлений")
real_interfaces = pf_config.network_interfaces(use_cache=False)
real_lan = lan_addresses(real_interfaces)
real_tunnels = vpn_addresses(real_interfaces)
if ARGS.host.strip():
    wanted = ARGS.host.strip()
    if wanted in real_lan:
        real_lan = [wanted] + [ip for ip in real_lan if ip != wanted]
    else:
        warn(f"--host {wanted} не найден среди LAN-адресов машины {real_lan} — "
             "берём первый найденный")
if not real_lan:
    warn("на этой машине нет LAN-адреса (только туннели/APIPA) — живой VPN-прогон "
         "пропущен; логика проверена на синтетических интерфейсах в разделах 7–9")
else:
    lan_host = real_lan[0]
    if real_tunnels:
        listed = ", ".join(f"{item['name']} {item['ip']}" for item in real_tunnels)
        ok(f"на машине есть туннели ({listed}) — прогон выполняем при них, "
           f"LAN-адрес {lan_host}")
    else:
        warn(f"туннелей сейчас нет (LAN {lan_host}): поднимите VPN для полной картины, "
             "проверка всё равно идёт на реальном адресе")
    cert2, key2 = make_cert()
    if cert2 is None:
        warn("openssl нет — VPN-прогон с сокетами пропущен")
    else:
        db = make_db()
        mgr = FakeMgr(db)
        db.set_settings({"studio_gateway_enabled": True,
                         "studio_gateway_access_code": ACCESS_CODE,
                         "studio_gateway_host": lan_host,
                         "studio_gateway_mode": "queue"})
        gw = StudioGateway(db, mgr, bind=True)
        mgr.studio = gw
        gw._start_ssdp()
        try:
            status = gw.status()
            check(status["ssdp_source"] == lan_host,
                  f"ssdp_source = {status['ssdp_source']} (закреплённый адрес)",
                  f"ssdp_source = {status['ssdp_source']!r}, ждали {lan_host}")
            check(status["host_local"],
                  "host_local=true: объявляемый адрес есть на интерфейсах",
                  "host_local=false — адрес объявлен, но машине не принадлежит")
            port = gw._ssdp_bound_port
            check(port in SSDP_LISTEN_PORTS,
                  f"шлюз слушает M-SEARCH на штатном :{port}",
                  f"шлюз слушает M-SEARCH на :{port} (штатный 1900 занят): "
                  f"{gw._ssdp_note or status['errors'].get('ssdp') or 'причина не сообщена'}")
            # Поток приёма запускается отдельной нитью: даём ему войти в recvfrom.
            time.sleep(0.25)
            request = (f'M-SEARCH * HTTP/1.1\r\nHOST: {SSDP_GROUP}:{port}\r\n'
                       f'MAN: "ssdp:discover"\r\nST: {SSDP_NT}\r\nMX: 1\r\n\r\n')
            # UDP ненадёжен по природе, поэтому пробуем несколько раз: один
            # потерянный пакет не должен выглядеть как поломка шлюза.
            answer_text, source, probe_error = "", (), ""
            for _attempt in range(4):
                probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                probe.settimeout(3)
                try:
                    probe.sendto(request.encode("utf-8"), (lan_host, port))
                    data, source = probe.recvfrom(4096)
                    answer_text = data.decode("utf-8", "replace")
                    break
                except OSError as exc:
                    probe_error = f"{type(exc).__name__}: {exc}"
                finally:
                    probe.close()
                time.sleep(0.3)
            if not answer_text:
                tx_name = "нет"
                try:
                    tx_name = str(gw._ssdp_tx.getsockname()) if gw._ssdp_tx else "не создан"
                except OSError:
                    pass
                alive = [item.name for item in gw._threads if item.is_alive()]
                fail(f"M-SEARCH на {lan_host}:{port} не ответил ({probe_error or 'таймаут'}); "
                     f"поток приёма: {alive or 'мёртв'}, исходящий сокет: {tx_name}, "
                     f"ошибки: {status['errors'] or 'нет'}, примечание: {gw._ssdp_note or 'нет'}")
            check("HTTP/1.1 200 OK" in answer_text,
                  "M-SEARCH на LAN-адресе → 200 OK", "нет 200 OK на LAN-адресе")
            if answer_text and source:
                check(source[0] == lan_host,
                      f"ответ M-SEARCH пришёл С {source[0]} (= объявленному адресу)",
                      f"ответ пришёл с {source[0]}, а объявлен {lan_host} — "
                      "Studio такой ответ отбросит (это и есть «код=-1» при VPN)")
                check(not any(str(source[0]).startswith(hint) for hint in ("10.0.0.",)),
                      f"источник ответа не VPN-адрес ({source[0]})",
                      f"источник ответа — VPN-адрес {source[0]}")
                check(f"Location: {lan_host}" in answer_text,
                      f"Location: {lan_host} в ответе", "Location не совпал с адресом")

            listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                listener.bind((lan_host, 2021))
                listener.settimeout(8)
                gw._last_notify = 0.0
                gw._broadcast_notify()
                data, notify_source = listener.recvfrom(4096)
                check(notify_source[0] == lan_host,
                      f"NOTIFY на {lan_host}:2021 ушёл с {notify_source[0]}",
                      f"NOTIFY ушёл с {notify_source[0]} вместо {lan_host}")
            except OSError as exc:
                warn(f"не удалось занять {lan_host}:2021 для проверки NOTIFY: {exc}")
            finally:
                listener.close()
        finally:
            gw.stop()
            db.close()
            shutil.rmtree(Path(cert2).parent, ignore_errors=True)

# --------------------------------------------------- 12. TLS-сертификат
section("12. TLS-сертификат: CN=<серийник> и SAN (Studio Beta 22710816)")
if shutil.which("openssl") is None:
    warn("openssl нет — выпуск сертификата проверить нельзя")
else:
    with tempfile.TemporaryDirectory(prefix="pf-audit-cert-") as tmp:
        cert_dir = Path(tmp)
        paths = {"CERT_DIR": cert_dir, "CERT_FILE": cert_dir / "cert.pem",
                 "KEY_FILE": cert_dir / "key.pem",
                 "CN_FILE": cert_dir / "cert.pem.cn",
                 "SAN_FILE": cert_dir / "cert.pem.san"}
        with mock.patch.multiple("connector.printflow.studio_tls", **paths):
            serial = "01P00AAUDIT01"
            issued, _key = studio_tls.ensure_certificate(serial)
            check(studio_tls.stored_cn() == serial, f"CN = {serial}",
                  f"CN = {studio_tls.stored_cn()!r}")
            san = studio_tls.certificate_san(issued)
            check(f"DNS:{serial}" in san, f"SAN содержит DNS:{serial} → {san}",
                  f"в SAN нет DNS:{serial}: {san}")
            check(any("127.0.0.1" in item for item in san),
                  f"SAN содержит IP:127.0.0.1 → {san}", "в SAN нет IP:127.0.0.1")
            check(studio_tls.stored_san() == f"DNS:{serial},IP:127.0.0.1",
                  "метка SAN записана рядом с сертификатом",
                  f"метка SAN = {studio_tls.stored_san()!r}")
            check(not studio_tls.cert_expiring_soon(issued),
                  f"сертификат действует до {studio_tls.cert_not_after(issued):%Y-%m-%d}",
                  "сертификат истекает раньше месяца")
            first = issued.read_bytes()
            studio_tls.ensure_certificate(serial)
            check(issued.read_bytes() == first,
                  "повторный вызов не перевыпускает сертификат",
                  "сертификат перевыпускается на каждом старте")
            studio_tls.ensure_certificate("01P00AOTHER01")
            check(issued.read_bytes() != first
                  and studio_tls.stored_cn() == "01P00AOTHER01",
                  "смена серийника → перевыпуск сертификата",
                  "сертификат не перевыпущен после смены серийника")
            # Старый выпуск без SAN (так было до правки) — обязан перевыпуститься.
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(cert_dir / "key.pem"), "-out", str(issued),
                 "-days", "2", "-subj", "/CN=01P00AOTHER01"],
                check=True, capture_output=True)
            stale = issued.read_bytes()
            check(studio_tls.certificate_san(issued) == [],
                  "контрольный выпуск действительно без SAN",
                  "openssl выпустил SAN сам — проверка нечестная")
            studio_tls.ensure_certificate("01P00AOTHER01")
            check(issued.read_bytes() != stale
                  and "DNS:01P00AOTHER01" in studio_tls.certificate_san(issued),
                  "сертификат без SAN перевыпущен с SAN (Beta 22710816 больше не режет)",
                  "сертификат без SAN не перевыпущен")
            check(studio_tls.san_for("NOZZA PrintFlow") == "DNS:NOZZA-PrintFlow,IP:127.0.0.1",
                  "имя с пробелом → корректный DNS в SAN",
                  f"san_for сломан: {studio_tls.san_for('NOZZA PrintFlow')}")

# --------------------------------------------- 13. Статус и паритет полей
section("13. Статус: счётчики и паритет с заглушкой API")
db, mgr, gw = gateway()
try:
    status = gw.status()
    counters = ("bind_requests", "bind_detects", "bind_logins", "mqtt_connections",
                "mqtt_auth_failures", "mqtt_publishes", "ftp_connections",
                "ftp_auth_failures", "ftp_uploads", "ssdp_notify", "ssdp_searches",
                "dropped_connections")
    for counter in counters:
        check(counter in status, f"счётчик {counter} есть в статусе",
              f"нет счётчика {counter}")
        check(isinstance(status[counter], int), f"{counter} — целое число",
              f"{counter} не int")
    for service in ("ssdp", "mqtt", "ftps", "bind", "tls", "host", "vpn", "cert"):
        check(service in gw._errors, f"у службы «{service}» своя ячейка ошибки",
              f"нет ячейки ошибки для {service}")
    api = make_api(db)
    api.manager = mgr
    code, payload = api.get("/api/studio/status", {})
    check(code == 200 and "access_code" not in payload,
          "GET /api/studio/status → 200 без access_code",
          "статус через API течёт или не отвечает")
    api.manager = None
    code2, fallback = api.get("/api/studio/status", {})
    check(code2 == 200, "заглушка статуса отвечает, когда шлюз ещё не создан",
          "заглушка не отвечает")
    missing = sorted(set(status) - set(fallback))
    extra = sorted(set(fallback) - set(status))
    check(not missing, "в заглушке есть все поля живого статуса",
          f"в заглушке нет полей: {missing}")
    check(not extra, "в заглушке нет лишних полей",
          f"лишние поля в заглушке: {extra}")
    check(not any(str(value).find(ACCESS_CODE) >= 0 for value in payload.values()
                  if isinstance(value, str)),
          "Access Code не виден в значениях статуса", "Access Code в статусе!")
finally:
    db.close()

# ------------------------------------------------ 14. Парсер брандмауэра
section("14. Парсер брандмауэра (pf.py)")
from pf import _port_field_matches, firewall_allows, firewall_state  # noqa: E402

check(_port_field_matches("8765", 8765), "порт 8765 точно", "точный порт не распознан")
check(_port_field_matches("8000-9000", 8765), "диапазон 8000-9000 ловит 8765",
      "диапазон не распознан")
check(not _port_field_matches("8000-9000", 9876), "диапазон не ловит чужой порт",
      "диапазон ловит чужой")
check(_port_field_matches("8765,8883", 8883), "список 8765,8883 ловит 8883",
      "список не распознан")
EN_RULE = ("Rule Name: PrintFlow\nEnabled: Yes\nDirection: In\n"
           "Action: Allow\nProtocol: TCP\nLocalPort: 8765\n")
check(firewall_allows(EN_RULE, 8765) is True, "firewall_allows: Enabled/Allow (EN)",
      "не распознал Allow")
RU_RULE = ("Правило: PrintFlow\nВключено: Да\nНаправление: Входящий\n"
           "Действие: Разрешить\nПротокол: TCP\nЛокальный порт: 3000\n")
check(firewall_allows(RU_RULE, 3000) is True, "firewall_allows: русская локаль",
      "не парсит RU")
check(firewall_allows("", 3000) is None, "пустой вывод → None («неизвестно»)",
      "пустой вывод не None")
state = firewall_state(BIND_PORT_PLAIN)
check(isinstance(state, dict) and "known" in state and "fix" in state,
      f"firewall_state вернул known={state.get('known')}", "firewall_state сломан")

# ---------------------------------------------------- 15. Фронтенд карточки
section("15. Фронтенд карточки Studio")
app_js = (ROOT / "site" / "assets" / "app.js").read_text(encoding="utf-8")
for needle, why in (("/api/studio/status", "app.js запрашивает /api/studio/status"),
                    ("host_pinned", "host_pinned в карточке"),
                    ("mqtt_running", "mqtt_running в карточке"),
                    ("ftp_running", "ftp_running в карточке"),
                    ("ssdp_running", "ssdp_running в карточке"),
                    ("ssdp_targets", "ssdp_targets в карточке"),
                    ("127.0.0.1:2021", "пояснение про loopback"),
                    ("код не подошёл", "подсказка про Access Code"),
                    ("['studio_gateway_host'", "поле «Адрес шлюза в сети» в форме")):
    check(needle in app_js, why, f"во фронтенде нет {needle!r}")

# ------------------------------------------------------ 16. Секреты в API
section("16. Секреты не утекают в API")
db = make_db()
try:
    db.set_settings({"studio_gateway_access_code": "super-secret"})
    from connector.printflow.api import Api
    from connector.printflow.repo import Repo
    api = Api.__new__(Api)
    api.db = db
    api.repo = Repo(db)
    api.manager = None
    code, payload = api.get("/api/studio/status", {})
    dump = json.dumps(payload, ensure_ascii=False)
    check("access_code" not in payload, "status не содержит ключ access_code",
          "утечка access_code в status")
    check("super-secret" not in dump, "секрет не в payload", "утечка секрета")
    settings = db.settings()
    check(settings["studio_gateway_access_code"] == "••••••••",
          "маскировка •••••••• в настройках", "не маскируется")
finally:
    db.close()

# --------------------------------------------- 17. Скрипт gateway-check.py
section("17. Скрипт gateway-check.py (10 проверок для владельца)")
check_path = ROOT / "scripts" / "gateway-check.py"
source = check_path.read_text(encoding="utf-8")
check("CHECKS_TOTAL = 10" in source, "в скрипте ровно 10 проверяемых шагов",
      "число шагов не 10")
for needle in ("Панель PrintFlow", "LAN-адреса и VPN", "SSDP M-SEARCH :1900",
               "Проба личности :3000", "Проба личности :3002/TLS",
               "Сертификат шлюза (CN/SAN)", "MQTT/TLS :8883", "FTPS :990",
               "UDP :2021 (розетка Studio)", "SSDP: объявление и цели"):
    check(f'"{needle}"' in source, f"шаг «{needle}» есть в скрипте",
          f"в скрипте нет шага «{needle}»")
check("expect_source" in source,
      "скрипт сверяет, С КАКОГО адреса пришёл ответ M-SEARCH",
      "скрипт не проверяет источник ответа M-SEARCH")
check("VPN перехватывает LAN" in source, "скрипт повторяет совет про split-tunnel",
      "в скрипте нет совета про VPN")
result = subprocess.run(
    [sys.executable, str(check_path), "--json", "--panel", "http://127.0.0.1:9",
     "--host", "127.0.0.1"],
    capture_output=True, text=True, cwd=str(ROOT), timeout=180)
check("Traceback" not in result.stderr,
      "скрипт не падает без запущенной панели", result.stderr[-400:] or "скрипт упал")
try:
    report = json.loads(result.stdout)
    names = [item["name"] for item in report["results"]]
    tallied = [item for item in report["results"] if item.get("tally", True)]
    check(report["total"] == 10 and len(tallied) == 10,
          f"в вердикт входят 10 шагов (всего строк {len(names)})",
          f"в вердикте {len(tallied)} шагов вместо 10")
    check(report["advice"], "скрипт всегда говорит, что делать",
          "скрипт не дал совета")
    check("Брандмауэр TCP 3000" in names, "правила брандмауэра показаны отдельно",
          "нет строки про брандмауэр")
except (ValueError, KeyError) as exc:
    fail(f"--json не разбирается: {exc}")

# -------------------------------------------------- 18. Unit-тесты шлюза
section("18. Unit-тесты шлюза (регрессии)")
if ARGS.skip_tests:
    warn("unit-тесты пропущены (--skip-tests)")
else:
    modules = ("test_studio_gateway", "test_studio_gateway_bind",
               "test_studio_gateway_confirm", "test_studio_gateway_diag",
               "test_studio_gateway_vpn", "test_bambu_studio_ams")
    total = 0
    broken = []
    for name in modules:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", f"connector.tests.{name}", "-q"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=900)
        tail = (proc.stderr or "") + (proc.stdout or "")
        match = re.search(r"Ran (\d+) tests?", tail)
        count = int(match.group(1)) if match else 0
        total += count
        if proc.returncode == 0 and count:
            ok(f"{name}: {count} тестов ok")
        else:
            broken.append(name)
            last = tail.strip().splitlines()[-1] if tail.strip() else "нет вывода"
            fail(f"{name}: {count} тестов, код {proc.returncode} — {last}")
    check(not broken, f"все {total} тестов шлюза проходят",
          f"сломаны модули: {broken}")
    check(total >= 14, f"в наборе {total} тестов (≥14)", "тестов меньше 14")

# ------------------------------------------------------------------ итог
print("\n" + "━" * 60)
print(f"Проверок пройдено: {PASSED[0]}, предупреждений: {len(WARNINGS)}, "
      f"провалов: {len(FAILURES)}")
if WARNINGS:
    print("\033[33mПредупреждения (на вердикт не влияют):\033[0m")
    for item in WARNINGS:
        print("  · " + item)
if FAILURES:
    print(f"\033[31mГЛУБОКАЯ ПРОВЕРКА: {len(FAILURES)} провалов\033[0m")
    for item in FAILURES:
        print(" - " + item)
    sys.exit(1)
print("\033[32mГЛУБОКАЯ ПРОВЕРКА: ВСЁ ОК — 100% ГОТОВО\033[0m")
print("Шлюз Bambu Studio отвечает на все четыре канала рукопожатия, объявления")
print("уходят с LAN-адреса даже при поднятом VPN, сертификат с SAN, sequence_id")
print("эхо числом. В Bambu Studio / OrcaSlicer добавьте принтер по IP из карточки")
print("Настройки → Принтеры и Bambu → Шлюз Bambu Studio, Access Code — там же.")
sys.exit(0)

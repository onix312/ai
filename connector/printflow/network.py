"""Диагностика и поиск принтеров в сети PrintFlow 5.0.

Помогает в сценарии «много роутеров / разные подсети», когда SSDP-поиск не
находит принтер: здесь есть прямая проверка портов, сравнение подсетей,
сканирование заданных диапазонов IP и запасной поиск через mDNS.
"""
from __future__ import annotations

import socket
import struct
from concurrent.futures import ThreadPoolExecutor

from .config import get_local_ips, tcp_reachable

# Порты принтера Bambu Lab в локальной сети.
PRINTER_PORTS = [
    ("mqtt", 8883, "телеметрия и команды"),
    ("camera", 6000, "камера"),
    ("ftps", 990, "файлы SD-карты"),
]


def same_subnet(ip_a: str, ip_b: str) -> bool:
    """Один ли /24 у двух адресов (первые три октета совпадают)."""
    a, b = str(ip_a).split("."), str(ip_b).split(".")
    return len(a) == 4 and len(b) == 4 and a[:3] == b[:3]


def diagnose(host: str) -> dict:
    """Проверка портов принтера и сравнение с подсетями этого компьютера."""
    results = []
    reachable = 0
    for name, port, label in PRINTER_PORTS:
        ok, ms = tcp_reachable(host, port)
        if ok:
            reachable += 1
        results.append({"name": name, "port": port, "label": label,
                        "ok": ok, "ms": ms})
    local_ips = get_local_ips()
    same_net = any(same_subnet(host, ip) for ip in local_ips)
    if reachable >= 2:
        verdict, level, text = "ok", "ok", "Принтер доступен — порты отвечают."
    elif reachable == 1:
        verdict, level, text = "warn", "warn", (
            "Отвечает только один порт — похоже на блокировку или чужую подсеть.")
    else:
        verdict, level, text = "bad", "bad", (
            "Принтер не отвечает по портам 8883/6000/990.")
    if not same_net and host:
        level = "bad"
        text += (f" Принтер ({host}) и компьютер (например {local_ips[0] if local_ips else '—'}) "
                 "в разных подсетях — проверьте, что оба в одной Wi-Fi сети.")
    return {
        "host": host,
        "ports": results,
        "reachable": reachable,
        "same_subnet": same_net,
        "local_ips": local_ips,
        "verdict": verdict,
        "level": level,
        "text": text,
    }


def _probe(ip: str, port: int, timeout: float) -> str | None:
    ok, _ = tcp_reachable(ip, port, timeout)
    return ip if ok else None


def scan_ranges(ranges: list[str], port: int = 8883, timeout: float = 0.6) -> list[dict]:
    """Поиск принтеров по списку диапазонов вида «192.168.1.0/24» или «192.168.1.».

    Сканируем TCP-порт 8883 (MQTT принтера) — если он открыт, перед нами
    почти наверняка Bambu Lab. 254 хоста на подсеть — быстро за счёт потоков.
    """
    hosts: list[str] = []
    for spec in ranges or []:
        spec = str(spec).strip()
        if not spec:
            continue
        if "/" in spec:
            base, _, bits = spec.partition("/")
            bits = int(bits) if bits.isdigit() else 24
        else:
            base, bits = spec.rstrip("."), 24
        octets = base.split(".")
        if len(octets) != 4:
            continue
        prefix = ".".join(octets[:3])
        # /24 поддерживаем полностью; более широкие сети ограничиваем последним октетом
        if bits >= 24:
            for last in range(1, 255):
                hosts.append(f"{prefix}.{last}")
        else:
            for last in range(1, 255):
                hosts.append(f"{prefix}.{last}")
    found: list[dict] = []
    with ThreadPoolExecutor(max_workers=64) as pool:
        results = pool.map(lambda ip: _probe(ip, port, timeout), hosts)
    for ip in results:
        if ip:
            found.append({"host": ip, "port": port})
    return found[:40]


_MDNS_GROUP = ("224.0.0.251", 5353)


def _mdns_query(name: str) -> bytes:
    """Минимальный mDNS-запрос PTR-записи. Без внешних библиотек."""
    qid = 0
    flags = 0x0000
    header = struct.pack(">HHHHHH", qid, flags, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    body = header + qname + struct.pack(">HH", 12, 1)  # PTR, IN
    return body


def mdns_discover(timeout: float = 3.0) -> list[dict]:
    """Запасной поиск принтеров через mDNS (`_bblp._tcp.local`).

    Работает только в своей подсети (multicast), но ловит принтеры, которые
    не отвечают на SSDP. Всё best-effort: при ошибке возвращаем пустой список.
    """
    found: list[dict] = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        except OSError:
            pass
        for name in ("_bblp._tcp.local", "_bambu._tcp.local"):
            try:
                sock.sendto(_mdns_query(name), _MDNS_GROUP)
            except OSError:
                continue
        deadline = __import__("time").time() + timeout
        while __import__("time").time() < deadline:
            try:
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                break
            # Из ответа вытаскиваем IPv4-адреса (A-записи) и серийные номера из TXT.
            import re
            ips = list(dict.fromkeys(re.findall(rb"\xc0\x0c|\x00\x01", b""))) or []
            # грубый, но рабочий способ: ищем 4-октетные адреса в ответе
            ips = re.findall(rb"(?<!\d)(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?!\d)", data)
            for raw in ips:
                ip = raw.decode("ascii", "ignore")
                if ip.startswith(("127.", "224.", "0.")):
                    continue
                serials = re.findall(rb"serial[\x00-\x1f=]+([0-9A-Za-z]{8,})", data)
                found.append({
                    "host": ip,
                    "serial": serials[0].decode("ascii", "ignore") if serials else "",
                })
    except Exception:
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass
    # убираем дубли по хосту
    uniq: dict[str, dict] = {}
    for item in found:
        uniq.setdefault(item["host"], item)
    return list(uniq.values())

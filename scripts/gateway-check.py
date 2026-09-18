#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Диагностика шлюза Bambu Studio: «Сбой подключения … код=-1» по шагам.

Десять проверок — ровно те десять шагов, которые Bambu Studio и OrcaSlicer
делают при добавлении принтера по IP. Скрипт проходит их по порядку и в конце
печатает вердикт ``10/10 ok`` или список того, что сломано:

     1. Панель PrintFlow          /api/studio/status: шлюз включён, службы подняты
     2. LAN-адреса и VPN          туннели отфильтрованы, закреплённый адрес на месте
     3. SSDP: объявление и цели   NOTIFY уходит туда же, куда шлёт станок
     4. SSDP M-SEARCH :1900       200 OK, и ответ пришёл С <host>, а не с VPN
     5. Проба личности :3000      кадр A5A5…A7A7, detect, эхо sequence_id
     6. Проба личности :3002/TLS  то же через TLS
     7. Сертификат шлюза          CN=<серийник>, SAN DNS:<серийник> + IP:127.0.0.1
     8. MQTT/TLS :8883            bblp/<код> → CONNACK 0, отчёты в …/report
     9. FTPS :990                 USER/PASS → 230, PASV с <host>, STOR → 226
    10. UDP :2021                 шлюз не занял розетку сетевого плагина Studio

Запуск на компьютере, где стоит Bambu Studio (там же, где PrintFlow, или
рядом — тогда укажите адрес машины с PrintFlow):

    python scripts/gateway-check.py --host 192.168.0.108
    python scripts/gateway-check.py --host 192.168.0.108 --code 12345678
    python scripts/gateway-check.py --host 192.168.0.108 --upload   (проверка STOR)
    python scripts/gateway-check.py --add-firewall   (нужны права администратора)

В PowerShell, чтобы кириллица в выводе не поехала:

    $env:PYTHONUTF8=1; python scripts/gateway-check.py --host 192.168.0.108

Без ``--code`` скрипт пробует прочитать Access Code из базы PrintFlow (он
нужен для проверок 8 и 9). ``--upload`` по умолчанию выключен: полная
проверка STOR кладёт файл в библиотеку и в очередь PrintFlow.

Скрипт ничего не меняет, пока не передан ``--add-firewall``.
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "connector"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from printflow.config import (  # noqa: E402
    DEFAULT_PORT,
    get_local_ips,
    lan_addresses,
    network_interfaces,
    vpn_addresses,
    vpn_interception,
)
from printflow.studio_gateway import (  # noqa: E402
    BIND_PORT_PLAIN,
    BIND_PORT_TLS,
    FIRMWARE_VERSION,
    FTP_PORT,
    MQTT_PORT,
    MQTT_USER,
    SSDP_BROADCAST,
    SSDP_GROUP,
    SSDP_NT,
    SSDP_PORTS,
    decode_bind_frame,
    directed_broadcast,
    encode_bind_frame,
    sequence_id_int,
)
from printflow.studio_mqtt import (  # noqa: E402
    CONNACK,
    PUBLISH,
    decode_publish,
    encode_connect,
    encode_publish,
    read_packet,
)

CHECKS_TOTAL = 10
DETECT_SEQUENCE = "20000"
DETECT_REQUEST = {"login": {"command": "detect", "sequence_id": DETECT_SEQUENCE}}
GATEWAY_PORTS = (BIND_PORT_PLAIN, BIND_PORT_TLS, MQTT_PORT, FTP_PORT)
OK, WARN, BAD = "ok", "warn", "bad"
MARK = {OK: "[ ok ]", WARN: "[ ?? ]", BAD: "[ !! ]"}
# Адреса, которые Studio видеть не должна: они означают, что сеть перехвачена.
VPN_HINTS = ("10.0.0.1", "tun0", "tap0", "utun", "wireguard", "tailscale")


# ------------------------------------------------------------------ утилиты
def _tls_client(verify: bool = False) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED if verify else ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def access_code_from_settings(explicit: str = "") -> tuple[str, str]:
    """Access Code для проверок MQTT/FTPS: аргумент → база PrintFlow → пусто."""
    code = str(explicit or "").strip()
    if code:
        return code, "--code"
    try:
        from printflow.db import Database
        db = Database()
        try:
            value = str(db.setting("studio_gateway_access_code", "") or "").strip()
        finally:
            try:
                db.close()
            except Exception:
                pass
        if value:
            return value, "база PrintFlow"
    except Exception:
        pass
    return "", ""


def panel_status(url: str, timeout: float = 3.0) -> dict:
    """Состояние шлюза из панели PrintFlow (если она отвечает)."""
    request = urllib.request.Request(url.rstrip("/") + "/api/studio/status")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            payload = json.loads(answer.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"state": WARN, "detail": f"панель не ответила: {exc}", "payload": {}}
    if not payload.get("enabled"):
        return {"state": BAD, "detail": "шлюз выключен: Настройки → Принтеры и Bambu → "
                                        "«Шлюз Bambu Studio»", "payload": payload}
    services = (("опрос :3000", "bind_running"), ("MQTT :8883", "mqtt_running"),
                ("FTPS :990", "ftp_running"), ("SSDP", "ssdp_running"))
    running = [name for name, key in services if payload.get(key)]
    missing = [name for name, key in services if not payload.get(key)]
    detail = (f"адрес {payload.get('host')}, поднято: {', '.join(running) or '—'}")
    errors = payload.get("errors") or {}
    if errors:
        detail += "; ошибки: " + "; ".join(f"{k}: {v}" for k, v in errors.items())
    return {"state": BAD if missing else OK,
            "detail": detail,
            "missing": missing,
            "errors": errors,
            "payload": payload}


def lan_and_vpn_state(host: str) -> dict:
    """Проверка 2: LAN-адреса перечислены, VPN-туннели в них не попали."""
    try:
        interfaces = network_interfaces(use_cache=False)
    except Exception as exc:
        return {"state": WARN, "detail": f"интерфейсы не перечислились: {exc}"}
    lan = lan_addresses(interfaces)
    tunnels = vpn_addresses(interfaces)
    report = vpn_interception(host)
    leaked = [item for item in tunnels if item["ip"] in lan]
    tunnel_names = ", ".join(f"{item['name']} {item['ip']}" for item in tunnels)
    detail = (f"LAN-адреса: {', '.join(lan) or '—'}"
              + (f"; пропущено туннелей: {len(tunnels)} ({tunnel_names})"
                 if tunnels else "; туннелей нет"))
    if leaked:
        return {"state": BAD,
                "detail": detail + f"; УТЕЧКА в LAN-список: {[i['ip'] for i in leaked]}"}
    pinned = str(host or "").strip()
    if pinned and not pinned.startswith("127.") and pinned not in lan:
        advice = report.get("advice") or (
            f"VPN перехватывает LAN, отключите {report.get('names') or 'туннель'} "
            f"или добавьте {report.get('subnet') or 'подсеть принтера'} в split-tunnel")
        return {"state": BAD,
                "detail": f"закреплённого адреса {pinned} нет ни на одном интерфейсе. "
                          + advice,
                "advice": advice}
    if report.get("intercepting"):
        advice = report.get("advice") or ""
        return {"state": BAD, "detail": detail + f". {advice}", "advice": advice}
    if tunnels:
        detail += " — в объявления они не попадают, Studio их не увидит"
    return {"state": OK, "detail": detail, "lan": lan, "tunnels": tunnels}


def probe_tcp(host: str, port: int, timeout: float = 2.0) -> dict:
    """Открывается ли обычный TCP-порт (как это делает плагин Studio)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"state": OK, "detail": "порт открыт"}
    except OSError as exc:
        return {"state": BAD, "detail": f"порт закрыт: {exc}"}


def probe_detect(host: str, port: int, tls: bool = False, timeout: float = 5.0,
                 expect_sequence: str = DETECT_SEQUENCE) -> dict:
    """Проба личности ровно тем кадром, которым её делает Studio.

    Это решающий шаг: нет ответа на ``login/detect`` — Studio покажет «код=-1»,
    не дойдя ни до Access Code, ни до MQTT. Заодно сверяем эхо
    ``sequence_id``: плагин отбрасывает ответ, который пришёл «не на его
    запрос», и фиксированное число в ответе выглядит именно так.
    """
    result: dict = {"state": BAD, "detail": "нет ответа на login/detect"}
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        result["detail"] = f"не удалось подключиться: {exc}"
        return result
    conn = raw
    try:
        if tls:
            conn = _tls_client().wrap_socket(raw, server_hostname=host)
        conn.settimeout(timeout)
        request = {"login": {"command": "detect", "sequence_id": expect_sequence}}
        conn.sendall(encode_bind_frame(request))
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = conn.recv(4096)
            except (socket.timeout, TimeoutError):
                break
            if not chunk:
                break
            buf += chunk
            payload, _rest = decode_bind_frame(buf)
            if payload is None:
                continue
            login = payload.get("login") or {}
            echo = login.get("sequence_id")
            detail = (f"серийник {login.get('id')}, модель {login.get('model')}, "
                      f"привязка {login.get('bind')}, sequence_id {echo!r}")
            if not isinstance(echo, int) or echo != sequence_id_int(expect_sequence):
                result = {"state": BAD,
                          "detail": f"эхо sequence_id неверное: ждали число "
                                    f"{sequence_id_int(expect_sequence)}, "
                                    f"пришло {echo!r} — плагин отбросит ответ",
                          "serial": login.get("id")}
                break
            for key, expected in (("bind", "free"), ("connect", "lan"),
                                  ("dev_cap", 1), ("version", FIRMWARE_VERSION)):
                if login.get(key) != expected:
                    result = {"state": BAD,
                              "detail": f"поле {key} = {login.get(key)!r}, "
                                        f"станок отдаёт {expected!r}",
                              "serial": login.get("id")}
                    break
            else:
                result = {"state": OK, "detail": f"шлюз ответил: {detail}",
                          "serial": login.get("id"), "model": login.get("model"),
                          "sequence_id": echo}
            break
    except (OSError, ssl.SSLError) as exc:
        result["detail"] = f"обмен не состоялся: {exc}"
    finally:
        try:
            conn.close()
        except OSError:
            pass
    return result


def probe_tls_certificate(host: str, port: int, serial: str = "",
                          timeout: float = 5.0) -> dict:
    """Проверка 7: CN=<серийник> и SAN DNS:<серийник> + IP:127.0.0.1.

    Bambu Studio Beta (22710816 и новее) живёт на WebView2 и сверяет
    ``subjectAltName``, а не CN: сертификат без SAN отбрасывается, и владелец
    видит «код=-1» при полностью живом шлюзе.
    """
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return {"state": BAD, "detail": f"не удалось подключиться к :{port}: {exc}"}
    try:
        conn = _tls_client().wrap_socket(raw, server_hostname=host)
    except (ssl.SSLError, OSError) as exc:
        try:
            raw.close()
        except OSError:
            pass
        return {"state": BAD, "detail": f"TLS-рукопожатие не состоялось: {exc}"}
    try:
        der = conn.getpeercert(binary_form=True) or b""
        version = ""
        try:
            version = conn.version() or ""
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass
    if not der:
        return {"state": WARN, "detail": "сертификат не отдан в рукопожатии"}
    parsed = _parse_certificate(der)
    cn = parsed.get("cn", "")
    san = parsed.get("san", [])
    if not parsed.get("parsed"):
        return {"state": WARN,
                "detail": f"рукопожатие прошло ({version}), но разобрать "
                          "сертификат нечем: нужен openssl"}
    problems = []
    if serial and cn != serial:
        problems.append(f"CN={cn!r}, а серийник шлюза {serial!r}")
    if not san:
        problems.append("нет subjectAltName — Studio Beta такой сертификат режет")
    else:
        joined = ",".join(san)
        if serial and f"DNS:{serial}" not in joined:
            problems.append(f"в SAN нет DNS:{serial}: {joined}")
        if "127.0.0.1" not in joined:
            problems.append(f"в SAN нет IP:127.0.0.1: {joined}")
    expires = str(parsed.get("expires") or "")
    detail = f"{version}, CN={cn}, SAN={', '.join(san) or '—'}"
    if expires:
        detail += f", действует до {expires}"
    if expires and expires < date.today().isoformat():
        problems.append(f"сертификат истёк {expires} — Studio его отвергнет; "
                        "удалите папку studio-gateway в каталоге данных "
                        "PrintFlow и перезапустите коннектор")
    if problems:
        return {"state": BAD, "detail": detail + " | " + "; ".join(problems),
                "cn": cn, "san": san, "expires": expires}
    return {"state": OK, "detail": detail, "cn": cn, "san": san,
            "expires": expires}


def _parse_certificate(der: bytes) -> dict:
    """CN и SAN из DER: сначала cryptography, затем openssl (без зависимостей)."""
    pem = ssl.DER_cert_to_PEM_cert(der)
    try:
        from cryptography import x509
        from cryptography.x509.oid import ExtensionOID, NameOID
        cert = x509.load_pem_x509_certificate(pem.encode("ascii"))
        cn = ""
        try:
            cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        except IndexError:
            pass
        san: list[str] = []
        try:
            ext = cert.extensions.get_extension_for_oid(
                ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
            for value in ext.value:
                # Приводим к тому же виду, что печатает openssl: DNS:/IP:.
                if isinstance(value, x509.DNSName):
                    san.append(f"DNS:{value.value}")
                elif isinstance(value, x509.IPAddress):
                    san.append(f"IP:{value.value}")
                else:
                    san.append(str(getattr(value, "value", value)))
        except x509.ExtensionNotFound:
            pass
        expiry = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
        return {"parsed": True, "cn": cn, "san": [item for item in san if item],
                "expires": expiry.date().isoformat()}
    except Exception:
        pass
    tmp = Path(tempfile.mkdtemp(prefix="pf-cert-")) / "cert.pem"
    try:
        tmp.write_text(pem, encoding="ascii")
        proc = subprocess.run(
            ["openssl", "x509", "-in", str(tmp), "-noout", "-subject",
             "-enddate", "-text"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return {"parsed": False, "cn": "", "san": [], "expires": ""}
    finally:
        try:
            tmp.unlink()
            tmp.parent.rmdir()
        except OSError:
            pass
    if proc.returncode != 0:
        return {"parsed": False, "cn": "", "san": [], "expires": ""}
    text = proc.stdout or ""
    expires = ""
    match_end = re.search(r"notAfter=(.+)", text)
    if match_end:
        try:
            stamp = datetime.strptime(match_end.group(1).strip(),
                                      "%b %d %H:%M:%S %Y %Z")
            expires = stamp.date().isoformat()
        except ValueError:
            expires = match_end.group(1).strip()
    cn = ""
    match = re.search(r"CN\s*=\s*([^\n,/]+)", text)
    if match:
        cn = match.group(1).strip()
    san: list[str] = []
    if "Subject Alternative Name" in text:
        block = text.split("Subject Alternative Name", 1)[1].split("\n")
        for line in block[1:3]:
            for chunk in line.split(","):
                chunk = chunk.strip()
                if chunk.startswith("DNS:"):
                    san.append(chunk)
                elif chunk.startswith("IP Address:"):
                    san.append("IP:" + chunk.split(":", 1)[1].strip())
    return {"parsed": True, "cn": cn, "san": san, "expires": expires}


def probe_ssdp(host: str, timeout: float = 3.0, expect_source: str = "") -> dict:
    """Отвечает ли шлюз на M-SEARCH — и С КАКОГО адреса он отвечает.

    Источник ответа важнее самого ответа: если он пришёл с адреса
    VPN-туннеля (10.0.0.1), а в ``Location`` объявлен 192.168.0.108, Studio
    расхождение видит и принтер не добавляет — это и есть «код=-1» при
    поднятом VPN.
    """
    request = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_GROUP}:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"ST: {SSDP_NT}\r\n"
        "MX: 1\r\n\r\n"
    ).encode("utf-8")
    targets = [(host, 1900)] if host and not host.startswith("127.") else []
    targets += [("239.255.255.250", 1900), ("127.0.0.1", 1900)]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    answer: dict = {"state": WARN,
                    "detail": "ответа на M-SEARCH нет — объявления (NOTIFY) всё равно шлются"}
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)
        for target in targets:
            try:
                sock.sendto(request, target)
            except OSError:
                continue
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except (socket.timeout, TimeoutError):
                break
            text = data.decode("utf-8", "replace")
            if SSDP_NT not in text:
                continue
            if "200 OK" not in text:
                continue
            location = ""
            match = re.search(r"^Location:\s*(\S+)", text, re.MULTILINE)
            if match:
                location = match.group(1)
            fields = [name for name in ("DevModel", "DevName", "DevConnect",
                                        "DevBind", "Devseclink", "DevVersion",
                                        "DevCap") if f"{name}.bambu.com:" in text]
            missing = [name for name in ("DevModel", "DevName", "DevConnect",
                                         "DevBind", "Devseclink", "DevVersion",
                                         "DevCap") if name not in fields]
            answer = {"state": OK, "source": addr[0], "location": location,
                      "detail": f"200 OK ответил {addr[0]}, Location: {location or '—'}"}
            if missing:
                answer = {"state": BAD, "source": addr[0], "location": location,
                          "detail": f"в ответе нет полей {', '.join(missing)} — "
                                    "плагин Studio не соберёт устройство"}
                break
            if not location:
                answer = {"state": BAD, "source": addr[0], "location": "",
                          "detail": "в ответе нет Location — Studio не узнает адрес"}
                break
            if expect_source and addr[0] != expect_source:
                answer = {"state": BAD, "source": addr[0], "location": location,
                          "detail": f"ответ пришёл с {addr[0]}, а объявлен "
                                    f"{expect_source}: Studio расхождение "
                                    "отбрасывает (так выглядит перехват LAN "
                                    "туннелем tun0)"}
                break
            if expect_source and location != expect_source:
                answer = {"state": BAD, "source": addr[0], "location": location,
                          "detail": f"Location: {location}, а ждали {expect_source}"}
                break
            break
    except OSError as exc:
        answer = {"state": WARN, "detail": f"M-SEARCH не отправился: {exc}"}
    finally:
        sock.close()
    return answer


def ssdp_targets_state(payload: dict, host: str) -> dict:
    """Проверка 3: цели объявления совпадают с тем, куда шлёт станок."""
    targets = [str(item) for item in (payload.get("ssdp_targets") or [])]
    if not targets:
        return {"state": BAD, "detail": "панель не отдала ssdp_targets"}
    wanted = {f"127.0.0.1:{SSDP_PORTS[0]}"}
    if host and not host.startswith("127."):
        wanted.add(f"{host}:{SSDP_PORTS[0]}")
        broadcast = directed_broadcast(host)
        if broadcast:
            wanted.add(f"{broadcast}:{SSDP_PORTS[0]}")
    wanted.add(f"{SSDP_BROADCAST}:{SSDP_PORTS[0]}")
    wanted.update(f"{SSDP_GROUP}:{port}" for port in SSDP_PORTS)
    missing = sorted(wanted - set(targets))
    listen = payload.get("ssdp_listen_ports") or []
    if 2021 in listen:
        return {"state": BAD,
                "detail": f"шлюз слушает UDP 2021 ({listen}) — это розетка "
                          "плагина Studio, её занимать нельзя"}
    source = str(payload.get("ssdp_source") or "")
    detail = (f"{len(targets)} адрес(ов), слушаем :{payload.get('ssdp_bound_port')}"
              + (f", источник объявлений {source}" if source else "")
              + (f"; примечание: {payload.get('ssdp_note')}"
                 if payload.get("ssdp_note") else ""))
    if missing:
        return {"state": BAD, "detail": detail + f"; не хватает {', '.join(missing)}",
                "targets": targets}
    if host and not host.startswith("127.") and source and source != host:
        return {"state": WARN,
                "detail": detail + f"; источник объявлений {source} != {host}",
                "targets": targets}
    tunnels = [item for item in targets
               if any(hint in item for hint in VPN_HINTS)]
    if tunnels:
        return {"state": BAD,
                "detail": detail + f"; в целях рассылки адреса туннелей: {tunnels}",
                "targets": targets}
    return {"state": OK, "detail": detail, "targets": targets}


def port_2021_state() -> dict:
    """UDP :2021 — розетка самого Studio; PrintFlow обязан его не занимать."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.bind(("0.0.0.0", 2021))
        return {"state": OK, "detail": "UDP 2021 свободен (Studio ещё не запущена)"}
    except OSError as exc:
        return {"state": OK,
                "detail": f"UDP 2021 занят ({exc.strerror or exc}) — так и должно "
                          "быть, если запущена Bambu Studio; PrintFlow его не слушает"}
    finally:
        sock.close()


def probe_mqtt(host: str, port: int, code: str, serial: str,
               timeout: float = 8.0) -> dict:
    """Проверка 8: MQTT/TLS — вход по Access Code и отчёты в …/report."""
    if not code:
        return {"state": WARN,
                "detail": "нет Access Code: передайте --code (проверка входа "
                          "и отчётов пропущена)"}
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return {"state": BAD, "detail": f"не удалось подключиться к :{port}: {exc}"}
    try:
        conn = _tls_client().wrap_socket(raw, server_hostname=host)
    except (ssl.SSLError, OSError) as exc:
        try:
            raw.close()
        except OSError:
            pass
        return {"state": BAD, "detail": f"TLS-рукопожатие не состоялось: {exc}"}
    report_topic = f"device/{serial}/report" if serial else ""
    try:
        conn.settimeout(timeout)
        conn.sendall(encode_connect(client_id="gateway-check",
                                    username=MQTT_USER, password=code))
        ptype, _flags, payload = read_packet(conn.recv, timeout)
        if ptype != CONNACK:
            return {"state": BAD, "detail": f"ждали CONNACK, пришёл тип {ptype}"}
        rc = payload[1] if len(payload) > 1 else -1
        if rc != 0:
            return {"state": BAD,
                    "detail": f"CONNACK {rc}: вход отклонён (username должен быть "
                              f"{MQTT_USER!r}, пароль — Access Code). "
                              "mqtt_connections при этом остаётся 0/-1"}
        # Неверный код пробуем ОТДЕЛЬНЫМ соединением: второй CONNECT на том же
        # соединении сбросил бы авторизацию, и отчёты перестали бы приходить —
        # проверка показала бы несуществующую поломку.
        wrong = _probe_wrong_code(host, port, timeout)
        sizes: dict[str, int] = {}
        if report_topic:
            # Studio сначала подписывается на темы устройства — SUBACK обязан
            # прийти, иначе плагин считает соединение неполноценным.
            conn.sendall(_subscribe_packet(1, [f"device/{serial}/request"]))
            try:
                subtype, _sf, subpayload = read_packet(conn.recv, 2.0)
                subscribed = subtype == 9 and len(subpayload) >= 3
            except Exception:
                subscribed = False
            for name, body in (
                ("get_version", {"info": {"command": "get_version", "sequence_id": "2"}}),
                ("pushall", {"pushing": {"command": "pushall", "sequence_id": "0"}}),
            ):
                conn.sendall(encode_publish(f"device/{serial}/request",
                                            json.dumps(body)))
                deadline = time.time() + timeout
                while time.time() < deadline:
                    try:
                        ptype3, flags3, payload3 = read_packet(conn.recv, timeout)
                    except Exception:
                        break
                    if ptype3 != PUBLISH:
                        continue
                    published = decode_publish(flags3, payload3)
                    if published.get("topic") != report_topic:
                        continue
                    sizes[name] = len(encode_publish(report_topic,
                                                     published.get("payload") or b""))
                    break
        detail = (f"CONNACK 0 (вход принят), на неверный код — {wrong}"
                  + (f", SUBACK на {serial}/request есть" if subscribed
                     else ", SUBACK не получен"))
        if sizes:
            detail += ", отчёты в " + report_topic + ": " + ", ".join(
                f"{key} {value} байт" for key, value in sizes.items())
        state = OK
        if report_topic and not sizes:
            state = BAD
            detail += "; отчётов в device/<серийник>/report не дождались"
        elif wrong not in (4, "не проверен"):
            state = WARN
            detail += "; на неверный код ждали CONNACK 4"
        return {"state": state, "detail": detail, "connack": rc, "sizes": sizes}
    except (OSError, ssl.SSLError) as exc:
        return {"state": BAD, "detail": f"обмен сорвался: {exc}"}
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _subscribe_packet(packet_id: int, topics: list[str]) -> bytes:
    """Пакет SUBSCRIBE (QoS 0) — то, чем Studio подписывается на темы устройства."""
    from printflow.studio_mqtt import encode_utf8, wrap_packet
    payload = packet_id.to_bytes(2, "big")
    for topic in topics:
        payload += encode_utf8(topic) + b"\x00"
    return wrap_packet(8, payload, flags=2)


def _probe_wrong_code(host: str, port: int, timeout: float) -> object:
    """CONNACK на неверный Access Code (4 = «код не подошёл»). Отдельным входом."""
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
        conn = _tls_client().wrap_socket(raw, server_hostname=host)
    except (OSError, ssl.SSLError):
        return "не проверен"
    try:
        conn.settimeout(min(timeout, 3.0))
        conn.sendall(encode_connect(client_id="gateway-check-bad",
                                    username=MQTT_USER, password="неверный-код"))
        ptype, _flags, payload = read_packet(conn.recv, 3.0)
        if ptype == CONNACK and len(payload) > 1:
            return payload[1]
        return "?"
    except Exception:
        return "не проверен"
    finally:
        try:
            conn.close()
        except OSError:
            pass

def probe_ftps(host: str, port: int, code: str, expect_host: str = "",
               upload: bool = False, timeout: float = 10.0) -> dict:
    """Проверка 9: implicit FTPS — вход, PASV с закреплённым адресом, STOR."""
    if not code:
        return {"state": WARN,
                "detail": "нет Access Code: передайте --code (проверка входа "
                          "и PASV пропущена)"}
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
        conn = _tls_client().wrap_socket(raw, server_hostname=host)
    except (OSError, ssl.SSLError) as exc:
        return {"state": BAD, "detail": f"implicit FTPS не поднялся: {exc}"}
    steps: list[str] = []
    try:
        conn.settimeout(timeout)

        def expect(prefix: str) -> str:
            line = b""
            deadline = time.time() + timeout
            while time.time() < deadline and not line.endswith(b"\n"):
                chunk = conn.recv(1)
                if not chunk:
                    break
                line += chunk
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith(prefix):
                raise AssertionError(f"ждали {prefix}, пришло {text!r}")
            steps.append(text.split(" ", 1)[0])
            return text

        def command(text: str, prefix: str) -> str:
            conn.sendall((text + "\r\n").encode("utf-8"))
            return expect(prefix)

        expect("220")
        command(f"USER {MQTT_USER}", "331")
        command(f"PASS {code}", "230")
        command("TYPE I", "200")
        pasv = command("PASV", "227")
        numbers = re.search(r"\((\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\)", pasv)
        if not numbers:
            return {"state": BAD, "detail": f"PASV без адреса: {pasv}"}
        pasv_host = ".".join(numbers.group(index) for index in range(1, 5))
        pasv_port = int(numbers.group(5)) * 256 + int(numbers.group(6))
        detail = "220/331/230/227, PASV → " + pasv_host
        if expect_host and pasv_host != expect_host and not expect_host.startswith("127."):
            return {"state": BAD,
                    "detail": f"PASV вернул {pasv_host}, а объявлен {expect_host}: "
                              "Studio зальёт файл не туда (так выглядит "
                              "перехват LAN туннелем)"}
        if not upload:
            return {"state": OK,
                    "detail": detail + " (STOR не проверялся: нужен --upload)"}
        data_raw = socket.create_connection((pasv_host, pasv_port), timeout=timeout)
        data = _tls_client().wrap_socket(data_raw, server_hostname=pasv_host) \
            if pasv_host == host else data_raw
        try:
            command(f"STOR gateway-check-{int(time.time())}.gcode.3mf", "150")
            data.sendall(b"; gateway-check probe\nM30\n")
            if hasattr(data, "unwrap"):
                try:
                    data.unwrap()
                except (OSError, ValueError):
                    pass
        finally:
            data.close()
        expect("226")
        return {"state": OK, "detail": detail + ", STOR → 226 (файл ушёл в очередь)"}
    except AssertionError as exc:
        return {"state": BAD, "detail": f"{exc} (пройдено: {'/'.join(steps) or '—'})"}
    except (OSError, ssl.SSLError) as exc:
        return {"state": BAD, "detail": f"обмен сорвался: {exc} "
                                        f"(пройдено: {'/'.join(steps) or '—'})"}
    finally:
        try:
            conn.close()
        except OSError:
            pass


def counters_state(payload: dict) -> dict:
    """Счётчики подключений: их отсутствие означает, что до шлюза никто не дошёл."""
    keys = ("bind_requests", "bind_detects", "mqtt_connections",
            "mqtt_auth_failures", "ftp_connections", "ftp_auth_failures",
            "ftp_uploads", "dropped_connections", "ssdp_notify", "ssdp_searches")
    missing = [key for key in keys if key not in payload]
    if missing:
        return {"state": BAD, "detail": f"в статусе нет счётчиков {', '.join(missing)}"}
    detail = ", ".join(f"{key}={payload.get(key)}" for key in keys)
    errors = payload.get("errors") or {}
    if errors:
        return {"state": WARN, "detail": detail + "; ошибки: "
                + "; ".join(f"{k}: {v}" for k, v in errors.items())}
    return {"state": OK, "detail": detail}


def firewall_state(port: int) -> dict:
    """Входящее правило брандмауэра для порта (Windows; иначе «неизвестно»)."""
    if sys.platform != "win32":
        return {"known": False, "allowed": None,
                "fix": f"разрешите входящий TCP {port} в брандмауэре системы"}
    try:
        import pf  # launcher: разбор netsh уже протестирован в connector/tests
        return pf.firewall_state(port)
    except Exception as exc:  # pragma: no cover - зависит от окружения
        return {"known": False, "allowed": None,
                "fix": f"netsh advfirewall firewall add rule ... localport={port}",
                "detail": str(exc)}


def add_firewall_rules(ports=GATEWAY_PORTS) -> list[tuple[int, bool, str]]:
    """Добавить входящие правила (нужны права администратора)."""
    done: list[tuple[int, bool, str]] = []
    for port in ports:
        command = ["netsh", "advfirewall", "firewall", "add", "rule",
                   f"name=PrintFlow Studio {port}", "dir=in", "action=allow",
                   "protocol=TCP", f"localport={port}"]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=10)
            ok = result.returncode == 0
            message = (result.stdout or result.stderr or "").strip().splitlines()
            done.append((port, ok, message[-1] if message else ""))
        except (OSError, subprocess.SubprocessError) as exc:
            done.append((port, False, str(exc)))
    return done


# ------------------------------------------------------------------- вывод
def speech(results: list[tuple[str, dict]]) -> list[str]:
    """Что делать владельцу — по результатам проверок."""
    advice: list[str] = []
    by_name = {name: item for name, item in results}
    detect = by_name.get("Проба личности :3000", {})
    if detect.get("state") == BAD:
        advice.append(
            "Порт 3000 не отвечает — это и есть «код=-1». Проверьте, что шлюз "
            "включён (Настройки → Принтеры и Bambu → Шлюз Bambu Studio) и что "
            "порт 3000 не занят: python pf.py logs")
    for name, item in results:
        if item.get("advice"):
            advice.append(item["advice"])
    lan = by_name.get("LAN-адреса и VPN", {})
    if lan.get("state") == BAD and not lan.get("advice"):
        advice.append("VPN перехватывает LAN, отключите tun0 или добавьте "
                      "подсеть принтера в split-tunnel")
    panel = by_name.get("Панель PrintFlow", {})
    for service, text in (panel.get("errors") or {}).items():
        advice.append(f"Ошибка службы «{service}»: {text}")
    for service in panel.get("missing") or []:
        advice.append(f"В панели не поднято: {service}")
    panel_payload = panel.get("payload") or {}
    if panel_payload.get("ssdp_bound_port") and panel_payload["ssdp_bound_port"] != 1900:
        advice.append("UDP 1900 занят другой программой — слушаем "
                      f":{panel_payload['ssdp_bound_port']}; объявления Studio всё "
                      "равно получает, но ответ на M-SEARCH может не дойти")
    cert = by_name.get("Сертификат шлюза (CN/SAN)", {})
    if cert.get("state") == BAD:
        advice.append("Перевыпустите сертификат: удалите папку studio-gateway в "
                      "каталоге данных PrintFlow и перезапустите коннектор")
    for name, item in results:
        if name.startswith("Брандмауэр") and item.get("state") == BAD:
            advice.append(item.get("fix") or "разрешите входящие порты в брандмауэре")
            break
    seen: set[str] = set()
    unique = []
    for line in advice:
        if line not in seen:
            seen.add(line)
            unique.append(line)
    advice = unique
    if not advice:
        advice.append("Всё по шагам рукопожатия в порядке. Если Studio всё ещё "
                      "пишет «код=-1»: закройте её, удалите "
                      "%APPDATA%\\BambuStudio\\BambuNetworkEngine.conf (и .bak) и "
                      "папку WebView2Cache, запустите заново и добавьте принтер по "
                      "IP, который печатает этот скрипт")
    return advice


def print_report(host: str, results: list[tuple[str, dict]], advice: list[str],
                 passed: int, total: int) -> None:
    print("Проверка шлюза PrintFlow для Bambu Studio (код=-1)")
    print(f"Адрес шлюза: {host}")
    print("-" * 68)
    for name, item in results:
        prefix = "" if item.get("tally", True) else "  · "
        print(f"{prefix}{MARK[item['state']]} {name}: {item['detail']}")
    print("-" * 68)
    verdict = f"ИТОГ: {passed}/{total} ok"
    if passed == total:
        print(verdict + " — Studio и OrcaSlicer найдут шлюз и напечатают через очередь")
    else:
        print(verdict + f" — {total - passed} проверок не прошли")
    print("Что делать:")
    for line in advice:
        print(f"  • {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Проверить шлюз Bambu Studio по шагам (10 проверок)")
    parser.add_argument("--host", default="", help="адрес компьютера с PrintFlow")
    parser.add_argument("--panel", default="", help="адрес панели PrintFlow")
    parser.add_argument("--code", default="", help="Access Code шлюза (8 символов)")
    parser.add_argument("--upload", action="store_true",
                        help="проверить STOR целиком (файл попадёт в очередь PrintFlow)")
    parser.add_argument("--bind-port", type=int, default=BIND_PORT_PLAIN)
    parser.add_argument("--bind-tls-port", type=int, default=BIND_PORT_TLS)
    parser.add_argument("--mqtt-port", type=int, default=MQTT_PORT)
    parser.add_argument("--ftp-port", type=int, default=FTP_PORT)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--add-firewall", action="store_true",
                        help="добавить входящие правила брандмауэра (Windows, права админа)")
    parser.add_argument("--json", action="store_true", help="машинночитаемый вывод")
    args = parser.parse_args(argv)

    site_hosts = get_local_ips()
    host = args.host.strip() or (site_hosts[0] if site_hosts else "127.0.0.1")
    panel_url = args.panel.strip() or f"http://127.0.0.1:{DEFAULT_PORT}"
    code, code_source = access_code_from_settings(args.code)

    results: list[tuple[str, dict]] = []

    def add(name: str, item: dict, tally: bool = True) -> dict:
        item = dict(item)
        item.setdefault("name", name)
        item["tally"] = tally
        results.append((name, item))
        return item

    # 1. Панель
    panel = add("Панель PrintFlow", panel_status(panel_url))
    payload = panel.get("payload") or {}
    serial = str(payload.get("serial") or "")

    # 2. LAN-адреса и VPN
    add("LAN-адреса и VPN", lan_and_vpn_state(host))

    # 3. Цели объявления SSDP
    if payload:
        add("SSDP: объявление и цели", ssdp_targets_state(payload, host))
    else:
        add("SSDP: объявление и цели",
            {"state": WARN, "detail": "панель не ответила — цели рассылки не проверить"})

    # 4. M-SEARCH
    add("SSDP M-SEARCH :1900",
        probe_ssdp(host, timeout=args.timeout, expect_source=host))

    # 5-6. Проба личности
    plain = add("Проба личности :3000",
                probe_detect(host, args.bind_port, timeout=args.timeout))
    if not serial:
        serial = str(plain.get("serial") or "")
    tls_probe = add("Проба личности :3002/TLS",
                    probe_detect(host, args.bind_tls_port, tls=True,
                                 timeout=args.timeout))
    if not serial:
        serial = str(tls_probe.get("serial") or "")

    # 7. Сертификат
    add("Сертификат шлюза (CN/SAN)",
        probe_tls_certificate(host, args.bind_tls_port, serial=serial,
                              timeout=args.timeout))

    # 8. MQTT
    add("MQTT/TLS :8883",
        probe_mqtt(host, args.mqtt_port, code, serial, timeout=args.timeout + 3))

    # 9. FTPS
    add("FTPS :990",
        probe_ftps(host, args.ftp_port, code, expect_host=host,
                   upload=args.upload, timeout=args.timeout + 5))

    # 10. Розетка Studio
    add("UDP :2021 (розетка Studio)", port_2021_state())

    # Дополнительно: счётчики и брандмауэр (в вердикт 10/10 не входят).
    # Статус перечитываем ПОСЛЕ проб: иначе счётчики будут нулевыми.
    after = panel_status(panel_url)
    counters_payload = after.get("payload") or payload
    if counters_payload:
        add("Счётчики подключений", counters_state(counters_payload), tally=False)
    for port in (args.bind_port, args.bind_tls_port, args.mqtt_port, args.ftp_port):
        state = firewall_state(port)
        if state.get("known") is False:
            item = {"state": WARN,
                    "detail": "посмотреть правила не удалось — проверьте вручную",
                    "fix": state.get("fix", "")}
        elif state.get("allowed") is False:
            item = {"state": BAD, "detail": f"входящий TCP {port} запрещён",
                    "fix": state.get("fix", "")}
        else:
            item = {"state": OK, "detail": f"входящий TCP {port} разрешён"}
        add(f"Брандмауэр TCP {port}", item, tally=False)

    if args.add_firewall:
        for port, ok, message in add_firewall_rules():
            add(f"Правило брандмауэра {port}",
                {"state": OK if ok else BAD,
                 "detail": message or ("добавлено" if ok else "не добавилось; "
                                       "запустите консоль от администратора")},
                tally=False)

    tallied = [item for _name, item in results if item.get("tally", True)]
    passed = sum(1 for item in tallied if item["state"] == OK)
    advice = speech(results)
    if args.json:
        print(json.dumps({
            "host": host, "serial": serial,
            "access_code_source": code_source,
            "passed": passed, "total": CHECKS_TOTAL,
            "results": [{k: v for k, v in item.items() if k != "payload"}
                        for _name, item in results],
            "advice": advice,
        }, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(host, results, advice, passed, CHECKS_TOTAL)
    return 0 if passed == CHECKS_TOTAL and not any(
        item["state"] == BAD for _name, item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

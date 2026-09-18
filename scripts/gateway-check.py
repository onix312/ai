#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Диагностика шлюза Bambu Studio: «Сбой подключения … код=-1» по шагам.

Запускать на компьютере, где стоит Bambu Studio — там же, где PrintFlow, или
рядом (тогда добавьте ``--host <адрес компьютера с PrintFlow>``):

    python scripts/gateway-check.py
    python scripts/gateway-check.py --host 192.168.1.50
    python scripts/gateway-check.py --add-firewall   (нужны права администратора)

Что проверяется — ровно в том порядке, в котором это делает Bambu Studio:

1. панель PrintFlow: ``/api/studio/status`` — включён ли шлюз, поднят ли
   опрос личности :3000/3002, слушает ли SSDP, что с ошибками;
2. TCP-порты 3000/3002/8883/990 на 127.0.0.1 и на адресе из объявления;
3. проба личности: настоящий кадр ``login/detect`` на :3000 (и :3002/TLS) —
   тот самый шаг, без ответа на котором Studio отдаёт «код=-1»;
4. SSDP: M-SEARCH на UDP :1900 — отвечает ли шлюз объявлением; свободен ли
   UDP :2021 (на нём слушает сам Studio — PrintFlow его не занимает);
5. брандмауэр Windows: входящие правила для 3000/3002/8883/990.

Скрипт ничего не меняет, пока не передан ``--add-firewall``.
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "connector"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from printflow.config import DEFAULT_PORT, get_local_ips  # noqa: E402
from printflow.studio_gateway import (  # noqa: E402
    BIND_PORT_PLAIN,
    BIND_PORT_TLS,
    FTP_PORT,
    MQTT_PORT,
    SSDP_GROUP,
    SSDP_NT,
    decode_bind_frame,
    encode_bind_frame,
)

DETECT_REQUEST = {"login": {"command": "detect", "sequence_id": "20000"}}
GATEWAY_PORTS = (BIND_PORT_PLAIN, BIND_PORT_TLS, MQTT_PORT, FTP_PORT)
OK, WARN, BAD = "ok", "warn", "bad"
MARK = {OK: "[ ok ]", WARN: "[ ?? ]", BAD: "[ !! ]"}


# ------------------------------------------------------------------ проверки
def probe_tcp(host: str, port: int, timeout: float = 2.0) -> dict:
    """Открывается ли обычный TCP-порт (как это делает плагин Studio)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"state": OK, "detail": "порт открыт"}
    except OSError as exc:
        return {"state": BAD, "detail": f"порт закрыт: {exc}"}


def probe_detect(host: str, port: int, tls: bool = False,
                 timeout: float = 5.0) -> dict:
    """Проба личности ровно тем кадром, которым её делает Studio.

    Это решающий шаг: нет ответа на ``login/detect`` — Studio покажет «код=-1»,
    не дойдя ни до Access Code, ни до MQTT.
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
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = ctx.wrap_socket(raw, server_hostname=host)
        conn.settimeout(timeout)
        conn.sendall(encode_bind_frame(DETECT_REQUEST))
        buf = b""
        deadline = timeout
        while deadline > 0:
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
            result = {
                "state": OK,
                "detail": (f"шлюз ответил: серийник {login.get('id')}, "
                           f"модель {login.get('model')}, привязка {login.get('bind')}"),
                "serial": login.get("id"),
                "model": login.get("model"),
            }
            break
    except (OSError, ssl.SSLError) as exc:
        result["detail"] = f"обмен не состоялся: {exc}"
    finally:
        try:
            conn.close()
        except OSError:
            pass
    return result


def probe_ssdp(host: str, timeout: float = 3.0) -> dict:
    """Отвечает ли шлюз на M-SEARCH (запасной канал поиска рядом с NOTIFY)."""
    request = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_GROUP}:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"ST: {SSDP_NT}\r\n"
        "MX: 1\r\n\r\n"
    ).encode("utf-8")
    targets = [("239.255.255.250", 1900)]
    if host and not host.startswith("127."):
        targets.append((host, 1900))
    targets.append(("127.0.0.1", 1900))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)
        for target in targets:
            try:
                sock.sendto(request, target)
            except OSError:
                continue
        deadline = timeout
        while deadline > 0:
            try:
                data, addr = sock.recvfrom(4096)
            except (socket.timeout, TimeoutError):
                break
            text = data.decode("utf-8", "replace")
            if SSDP_NT in text:
                return {"state": OK, "detail": f"ответил {addr[0]}: {SSDP_NT}"}
    except OSError as exc:
        return {"state": WARN, "detail": f"M-SEARCH не отправился: {exc}"}
    finally:
        sock.close()
    return {"state": WARN,
            "detail": "ответа на M-SEARCH нет — объявления (NOTIFY) всё равно шлются"}


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


def panel_status(url: str, timeout: float = 3.0) -> dict:
    """Состояние шлюза из панели PrintFlow (если она отвечает)."""
    request = urllib.request.Request(url.rstrip("/") + "/api/studio/status")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            payload = json.loads(answer.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"state": WARN, "detail": f"панель не ответила: {exc}"}
    if not payload.get("enabled"):
        return {"state": BAD, "detail": "шлюз выключен: Настройки → Принтеры и Bambu → "
                                        "«Шлюз Bambu Studio»"}
    running = [name for name, key in (("опрос :3000", "bind_running"),
                                      ("MQTT :8883", "mqtt_running"),
                                      ("FTPS :990", "ftp_running"),
                                      ("SSDP", "ssdp_running")) if payload.get(key)]
    missing = [name for name, key in (("опрос :3000", "bind_running"),
                                      ("MQTT :8883", "mqtt_running"),
                                      ("FTPS :990", "ftp_running"),
                                      ("SSDP", "ssdp_running")) if not payload.get(key)]
    targets = payload.get("ssdp_targets") or []
    detail = (f"адрес {payload.get('host')}, поднято: {', '.join(running) or '—'}"
              f"; объявление уходит на {len(targets)} адрес(ов)")
    if "127.0.0.1:2021" in targets:
        detail += ", включая loopback (Studio на этом же ПК)"
    return {"state": BAD if missing else OK,
            "detail": detail,
            "missing": missing,
            "errors": payload.get("errors") or {},
            "payload": payload}


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
    for name, item in results:
        if name.startswith("Брандмауэр") and item.get("state") == BAD:
            advice.append(item.get("fix") or "разрешите входящие порты в брандмауэре")
            break
    if not advice:
        advice.append("Всё по шагам рукопожатия в порядке. Если Studio всё ещё "
                      "пишет «код=-1»: закройте её, удалите "
                      "%APPDATA%\\BambuStudio\\BambuNetworkEngine.conf (и .bak) и "
                      "папку WebView2Cache, запустите заново и добавьте принтер по "
                      "IP, который печатает этот скрипт")
    return advice


def print_report(host: str, results: list[tuple[str, dict]], advice: list[str]) -> None:
    print("Проверка шлюза PrintFlow для Bambu Studio (код=-1)")
    print(f"Адрес шлюза: {host}")
    print("-" * 68)
    for name, item in results:
        print(f"{MARK[item['state']]} {name}: {item['detail']}")
    print("-" * 68)
    print("Что делать:")
    for line in advice:
        print(f"  • {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверить шлюз Bambu Studio по шагам")
    parser.add_argument("--host", default="", help="адрес компьютера с PrintFlow")
    parser.add_argument("--panel", default="", help="адрес панели PrintFlow")
    parser.add_argument("--add-firewall", action="store_true",
                        help="добавить входящие правила брандмауэра (Windows, права админа)")
    parser.add_argument("--json", action="store_true", help="машинночитаемый вывод")
    args = parser.parse_args(argv)

    site_hosts = get_local_ips()
    host = args.host.strip() or (site_hosts[0] if site_hosts else "127.0.0.1")
    panel_url = args.panel.strip() or f"http://127.0.0.1:{DEFAULT_PORT}"

    results: list[tuple[str, dict]] = [("Панель PrintFlow", panel_status(panel_url))]
    addresses = ["127.0.0.1"] + [ip for ip in (host,) if ip != "127.0.0.1"]
    for ip in addresses:
        for port in GATEWAY_PORTS:
            if ip == "127.0.0.1" and port == FTP_PORT:
                # 990 — привилегированный порт: без прав администратора он не
                # поднимется, и это ожидаемо на части систем.
                item = probe_tcp(ip, port)
                if item["state"] == BAD:
                    item = {**item, "state": WARN, "detail": item["detail"] +
                            " (для порта 990 нужны права администратора)"}
                results.append((f"TCP {ip}:{port}", item))
                continue
            results.append((f"TCP {ip}:{port}", probe_tcp(ip, port)))
    results.append(("Проба личности :3000", probe_detect(host, BIND_PORT_PLAIN)))
    results.append(("Проба личности :3002/TLS", probe_detect(host, BIND_PORT_TLS, tls=True)))
    results.append(("SSDP M-SEARCH :1900", probe_ssdp(host)))
    results.append(("UDP :2021 (розетка Studio)", port_2021_state()))
    for port in GATEWAY_PORTS:
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
        results.append((f"Брандмауэр TCP {port}", item))

    if args.add_firewall:
        for port, ok, message in add_firewall_rules():
            results.append((f"Правило брандмауэра {port}",
                            {"state": OK if ok else BAD,
                             "detail": message or ("добавлено" if ok else "не добавилось; "
                                                   "запустите консоль от администратора")}))

    advice = speech(results)
    if args.json:
        print(json.dumps({"host": host, "results": [
            {"name": name, **item} for name, item in results], "advice": advice},
            ensure_ascii=False, indent=2))
    else:
        print_report(host, results, advice)
    return 1 if any(item["state"] == BAD for _name, item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

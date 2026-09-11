#!/usr/bin/env python3
"""Замер канала «касса ↔ ПК» на живом стенде владельца (шаг 1 раунда 17.0.13).

Скрипт **только читает**: health, каталог кассы, поток событий, раздача APK.
Ни одной продажи, ни одной записи в базу — прогонять можно на рабочей установке.

Запуск из корня репозитория (на ПК, где работает коннектор)::

    python3 scripts/kassa-drill.py http://192.168.1.50:8765
    python3 scripts/kassa-drill.py http://192.168.1.50:8765 --code 1234   # + каталог
    python3 scripts/kassa-drill.py http://192.168.1.50:8765 --seconds 40  # длиннее поток

Что делать руками (телефон, adb, роутер) — печатается в конце прогона: сценарии
«обрыв Wi-Fi посреди продажи», «смена IP», «две кассы», «погашенный экран».
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def get(base: str, path: str, timeout: float = 6.0) -> tuple[int, dict, float]:
    """GET с замером времени. Ошибку возвращаем кодом, а не исключением."""
    started = time.time()
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as resp:
            raw = resp.read()
            spent = (time.time() - started) * 1000
            try:
                return resp.status, json.loads(raw or b"{}"), spent
            except json.JSONDecodeError:
                return resp.status, {"_raw": raw[:200].decode("utf-8", "replace")}, spent
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {"_raw": raw[:200].decode("utf-8", "replace")}
        return exc.code, payload, (time.time() - started) * 1000
    except Exception as exc:  # таймаут, отказ соединения, DNS
        return 0, {"error": str(exc)}, (time.time() - started) * 1000


def post(base: str, path: str, body: dict, timeout: float = 6.0) -> tuple[int, dict, float]:
    started = time.time()
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base + path, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
            spent = (time.time() - started) * 1000
            try:
                return resp.status, json.loads(raw or b"{}"), spent
            except json.JSONDecodeError:
                return resp.status, {"_raw": raw[:200].decode("utf-8", "replace")}, spent
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {"_raw": raw[:200].decode("utf-8", "replace")}
        return exc.code, payload, (time.time() - started) * 1000
    except Exception as exc:
        return 0, {"error": str(exc)}, (time.time() - started) * 1000


def installed_version_code() -> int:
    """versionCode из android/app/build.gradle — чтобы спросить /api/app/android честно."""
    gradle = ROOT / "android" / "app" / "build.gradle"
    try:
        found = re.search(r"versionCode\s+(\d+)", gradle.read_text(encoding="utf-8"))
        return int(found.group(1)) if found else 0
    except OSError:
        return 0


def host_port(base: str) -> tuple[str, int]:
    parts = urllib.parse.urlsplit(base)
    return parts.hostname or "127.0.0.1", parts.port or (443 if parts.scheme == "https" else 80)


def stream_probe(base: str, seconds: float) -> None:
    """Сколько живёт поток и как часто в нём что-то происходит."""
    host, port = host_port(base)
    print(f"\n== Поток событий /api/stream ({seconds:.0f} с наблюдения) ==")
    try:
        sock = socket.create_connection((host, port), timeout=8)
    except OSError as exc:
        print(f"  поток не открылся: {exc}")
        return
    started = time.time()
    first_byte = None
    frames: list[tuple[float, str]] = []
    ping = 0
    try:
        sock.sendall(b"GET /api/stream HTTP/1.1\r\nHost: " + f"{host}:{port}".encode()
                     + b"\r\nAccept: text/event-stream\r\n\r\n")
        sock.settimeout(seconds)
        deadline = started + seconds
        buffer = b""
        while time.time() < deadline:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            if first_byte is None:
                first_byte = time.time() - started
            buffer += chunk
            while b"\n\n" in buffer:
                frame, buffer = buffer.split(b"\n\n", 1)
                text = frame.decode("utf-8", "replace")
                if text.startswith(":"):
                    ping += 1
                    continue
                kind = next((line[7:] for line in text.splitlines()
                             if line.startswith("event: ")), "")
                if kind:  # HTTP-заголовки и retry: — не события
                    frames.append((time.time() - started, kind))
    finally:
        sock.close()
    if first_byte is None:
        print("  за всё время не пришло ни байта — поток мёртв (проверьте порт и firewall)")
        return
    print(f"  первый байт: {first_byte:.2f} с")
    print(f"  кадров событий: {len(frames)}, пингов-«жизнь»: {ping}")
    if frames:
        gaps = [b[0] - a[0] for a, b in zip(frames, frames[1:])]
        longest = max(gaps) if gaps else 0.0
        print(f"  максимальная пауза между кадрами: {longest:.1f} с")
    kinds = sorted({kind for _, kind in frames})
    print(f"  типы кадров: {', '.join(kinds) if kinds else 'нет'}")
    print("  (в 17.0.12 касса показывает только «есть/нет сети», а не «поток жив»)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Замер канала касса ↔ ПК (только чтение)")
    parser.add_argument("base", help="адрес коннектора, например http://192.168.1.50:8765")
    parser.add_argument("--code", default="", help="код кассы/PIN — добавить замер каталога")
    parser.add_argument("--seconds", type=float, default=25.0,
                        help="сколько секунд слушать поток (по умолчанию 25)")
    args = parser.parse_args(argv)
    base = args.base.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    print(f"Стенд: {base}\n")

    print("== Проверка «жив ли ПК» (/api/health), 3 замера ==")
    timings = []
    version = ""
    for _ in range(3):
        code, payload, ms = get(base, "/api/health")
        timings.append(ms)
        if code == 200:
            version = str(payload.get("version") or "")
        print(f"  {code or 'нет ответа':>3}  {ms:7.1f} мс")
    if timings:
        print(f"  мин {min(timings):.1f} мс, среднее {sum(timings) / len(timings):.1f} мс, "
              f"макс {max(timings):.1f} мс")
    if not version:
        print("  сервер не ответил — дальше смысла нет: проверьте адрес, порт, firewall")
        return 2
    print(f"  версия коннектора: {version}")

    installed = installed_version_code()
    code, app, ms = get(base, f"/api/app/android?installed={installed}")
    print(f"\n== Раздача оболочки (/api/app/android?installed={installed}) ==")
    print(f"  {code}  {ms:.1f} мс  сборка: {'есть' if app.get('available') else 'нет'}")
    if app.get("available"):
        print(f"  версия сборки {app.get('version')} (code {app.get('version_code')}), "
              f"файл {app.get('file')}, {app.get('size_mb')} МБ")
        print(f"  обновление предлагается: {bool(app.get('update_available'))}")
        missing = [key for key in ("changelog", "sha256", "size_bytes") if key not in app]
        if missing:
            print(f"  в ответе нет полей: {', '.join(missing)} — changelog и целостность "
                  f"сверить нечем (это и правит 17.0.13)")

    token = ""
    if args.code:
        code, login, ms = post(base, "/api/cashier/login", {"code": args.code})
        print(f"\n== Вход кассира ==")
        print(f"  {code}  {ms:.1f} мс  роль: {login.get('role') or login.get('error', '—')}")
        token = str(login.get("token") or "")
        if token:
            code, catalog, ms = get(
                base, "/api/cashier/catalog?" + urllib.parse.urlencode({"token": token}))
            items = catalog.get("items") or []
            print(f"\n== Каталог кассы ==")
            print(f"  {code}  {ms:.1f} мс  позиций: {len(items)}")
            code, incoming, ms = get(
                base, "/api/cashier/incoming?" + urllib.parse.urlencode({"token": token}))
            print(f"  входящие СБП: {len(incoming.get('payments') or [])} за {ms:.1f} мс")

    stream_probe(base, args.seconds)

    print("\n== Что осталось проверить руками (телефон + роутер) ==")
    print("  1. Оборвать Wi-Fi посреди продажи (выключить Wi-Fi между нажатием")
    print("     «Оплатить» и ответом) → в журнале кассы должна быть РОВНО одна продажа.")
    print("  2. Сменить IP ПК (перезагрузка роутера) → касса возвращается сама, ≤10 с,")
    print("     офлайн-очередь цела, адрес руками не вводится.")
    print("  3. Рестарт коннектора посреди смены → код кассира не спрашивается.")
    print("  4. Две кассы одновременно → обе видны в панели, продажи не путаются.")
    print("  5. Погашенный экран 10 минут → звонок о СБП слышен, поток восстанавливается.")
    print("  6. Правка цены в панели при открытой кассе → касса перечитала каталог.")
    print("  7. Офлайн-очередь из 5 продаж → выгрузилась сама, прогресс «N из M»,")
    print("     дублей нет.")
    print("\n  Экраны (docs/ANDROID.md):")
    print("    adb shell wm size; adb shell wm density")
    print("    adb shell settings get system font_scale")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

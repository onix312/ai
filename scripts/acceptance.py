#!/usr/bin/env python3
"""Живая приёмка: поднять коннектор на пустой базе и пройти маршрут владельца.

Юнит-тесты проверяют функции, а владелец смотрит на работающий сервер. Этот
скрипт делает ровно то, что делается руками при приёмке, — поднимает
коннектор, создаёт заказ, двигает его по статусам, снимает в архив, ищет
через палитру — и печатает таблицу «что проверили / что получили».

База всегда своя: каталог данных создаётся во временной папке, настоящая
`~/.config/printflow` не открывается ни на чтение, ни на запись.

Запуск:
    python3 scripts/acceptance.py            # свой порт, своя база
    python3 scripts/acceptance.py --port 8790

Код возврата: 0 — все проверки прошли, 1 — есть провал, 2 — сервер не поднялся.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONNECTOR = ROOT / "connector" / "printflow_connector.py"


class Probe:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, fn) -> None:
        try:
            detail = fn()
            self.rows.append((name, True, str(detail)))
        except Exception as exc:  # noqa: BLE001 - приёмка печатает всё как есть
            self.rows.append((name, False, f"{type(exc).__name__}: {exc}"))

    def report(self) -> int:
        failed = [row for row in self.rows if not row[1]]
        width = max(len(row[0]) for row in self.rows)
        for name, ok, detail in self.rows:
            print(f"  {'OK  ' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
        print(f"\n{len(self.rows) - len(failed)}/{len(self.rows)} проверок пройдено")
        return 1 if failed else 0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base + urllib.parse.quote(path, safe="/?=&"),
            data=data,
            headers={"Content-Type": "application/json", "Origin": self.base},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as reply:
                return reply.status, json.loads(reply.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


def wait_health(client: Client, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, payload = client.call("/api/health")
            if code == 200:
                return payload
        except Exception:  # noqa: BLE001 - сервер ещё поднимается
            time.sleep(0.4)
    raise RuntimeError("сервер не ответил на /api/health")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="порт (0 — свободный)")
    parser.add_argument("--keep", action="store_true",
                        help="не удалять временный каталог данных")
    args = parser.parse_args(argv)

    if not CONNECTOR.exists():
        print(f"нет {CONNECTOR}", file=sys.stderr)
        return 2
    data_dir = Path(tempfile.mkdtemp(prefix="pf-acceptance-"))
    port = args.port or free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, XDG_CONFIG_HOME=str(data_dir))
    print(f"Каталог данных: {data_dir}")
    print(f"Сервер: {base}\n")
    process = subprocess.Popen(
        [sys.executable, str(CONNECTOR), "--host", "127.0.0.1",
         "--port", str(port), "--no-browser"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    probe = Probe()
    client = Client(base)
    try:
        health = wait_health(client)
        print(f"Сервер поднялся: версия {health.get('version')}\n")
        run_checks(probe, client)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        if args.keep:
            print(f"\nКаталог данных оставлен: {data_dir}")
        else:
            shutil.rmtree(data_dir, ignore_errors=True)
    print()
    return probe.report()


def run_checks(probe: Probe, client: Client) -> None:
    state: dict = {}

    def health():
        code, payload = client.call("/api/health")
        assert code == 200, code
        return f"версия {payload['version']}"

    def statuses():
        code, payload = client.call("/api/statuses")
        assert code == 200, code
        state["statuses"] = [s["id"] for s in payload["statuses"]]
        return f"{len(state['statuses'])} статусов: {', '.join(state['statuses'][:4])}…"

    def create_order():
        code, payload = client.call("/api/order/save", {
            "product": "Приёмочная деталь", "customer_name": "Приёмка",
            "status": "queue", "price": 1500})
        assert code == 200, payload
        state["order"] = payload.get("order") or payload
        return f"№{state['order'].get('number')} создан"

    def next_statuses():
        code, payload = client.call(f"/api/order?id={state['order']['id']}")
        assert code == 200, payload
        state["next"] = payload.get("next") or []
        assert state["next"], "сервер не сказал, куда можно шагнуть"
        return f"из «queue» можно в {state['next']}"

    def paging():
        for _ in range(3):
            client.call("/api/order/save", {"product": "Ещё деталь",
                                            "customer_name": "Приёмка",
                                            "status": "new", "price": 100})
        _, page = client.call("/api/orders?limit=2")
        _, rest = client.call("/api/orders?limit=2&offset=2")
        assert len(page["orders"]) == 2, len(page["orders"])
        overlap = {o["id"] for o in page["orders"]} & {o["id"] for o in rest["orders"]}
        assert not overlap, overlap
        return f"limit=2 → 2 заказа, offset=2 → {len(rest['orders'])}, пересечений нет"

    def arrow():
        target = next(s for s in state["next"] if s != "queue")
        code, payload = client.call("/api/order/status",
                                    {"id": state["order"]["id"], "status": target})
        assert code == 200, payload
        return f"«→ {target}» применён"

    def bulk_skip():
        code, payload = client.call("/api/orders/bulk-status",
                                    {"ids": [state["order"]["id"]], "status": "done"})
        assert code == 200, payload
        assert payload.get("skipped"), payload
        return f"updated={payload['updated']}, пропущено: {payload['skipped'][0]['error'][:38]}…"

    def bulk_unknown():
        code, payload = client.call("/api/orders/bulk-status",
                                    {"ids": [state["order"]["id"]], "status": "нет"})
        assert code == 400, (code, payload)
        return f"400 {payload['error']}"

    def archive():
        code, payload = client.call("/api/order/archive", {"id": state["order"]["id"]})
        assert code == 200, payload
        _, live = client.call("/api/orders")
        assert state["order"]["id"] not in {o["id"] for o in live["orders"]}
        _, box = client.call("/api/orders?archived=1")
        assert state["order"]["id"] in {o["id"] for o in box["orders"]}
        _, one = client.call(f"/api/order?id={state['order']['id']}")
        assert one["price"] == 1500, one["price"]
        return "с доски убран, в архиве есть, цена и строка на месте"

    def restore():
        code, payload = client.call("/api/order/archive",
                                    {"id": state["order"]["id"], "archived": False})
        assert code == 200, payload
        _, live = client.call("/api/orders")
        assert state["order"]["id"] in {o["id"] for o in live["orders"]}
        return "заказ вернулся на доску"

    def search():
        code, payload = client.call("/api/search?q=приём")
        assert code == 200, payload
        assert "groups" in payload, sorted(payload)
        kinds = [(g["kind"], g["count"]) for g in payload["groups"]]
        return f"total={payload['total']}, группы {kinds}"

    def routes_inventory():
        code, payload = client.call("/api/openapi.json")
        assert code == 200, code
        paths = payload.get("paths") or {}
        return f"в спецификации {len(paths)} путей"

    for name, fn in (
        ("сервер отвечает", health),
        ("статусы приходят с сервера", statuses),
        ("заказ создаётся", create_order),
        ("следующий статус говорит сервер", next_statuses),
        ("страницы списка не пересекаются", paging),
        ("кнопка «→» меняет статус", arrow),
        ("пакет статуса перечисляет отказы", bulk_skip),
        ("неизвестный статус отклоняется", bulk_unknown),
        ("архив не теряет заказ и деньги", archive),
        ("из архива можно вернуть", restore),
        ("единый поиск отдаёт группы", search),
        ("спецификация API живая", routes_inventory),
    ):
        probe.check(name, fn)


if __name__ == "__main__":
    sys.exit(main())

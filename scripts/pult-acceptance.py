#!/usr/bin/env python3
"""Живая приёмка пульта цеха (18.0.8): десять сценариев ТЗ на копии базы.

Владелец проверяет пульт на телефоне; этот скрипт делает то, что можно
проверить без пальца и без принтера — поднимает коннектор на **копии** данных и
проходит те же сценарии по серверной части: страница и сводка, команды с
подтверждением, очередь, AMS, камера и свет, «сводка ничего не пишет»,
оценка из файла, раскладка AMS и отправка выбранной плиты, файл без данных
слайсера, автономность словами сервера, неприкосновенность рабочей базы.

Копия обязательна: приёмка не имеет права трогать рабочую базу. Если рабочих
данных на компьютере нет, скрипт работает на пустой базе и честно помечает те
проверки, которые на пустой базе неполные (например, «принтер не настроен» —
это ответ сервера, а не подтверждение физической команды).

Запуск:
    python3 scripts/pult-acceptance.py                 # своя копия и свой порт
    python3 scripts/pult-acceptance.py --port 8790     # свой порт
    python3 scripts/pult-acceptance.py --keep          # не удалять копию данных

Код возврата: 0 — все проверки прошли, 1 — есть провал, 2 — сервер не поднялся.

Не запускайте приёмку одновременно с полным прогоном тестов: последняя проверка
сравнивает время изменения рабочей базы, а тесты тоже трогают рабочий каталог.
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
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONNECTOR = ROOT / "connector" / "printflow_connector.py"
PAGE = ROOT / "site" / "control.html"


class Probe:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, fn) -> None:
        try:
            self.rows.append((name, True, str(fn())))
        except Exception as exc:  # noqa: BLE001 — приёмка печатает всё как есть
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

    def _send(self, request: urllib.request.Request) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(request, timeout=20) as reply:
                return reply.status, json.loads(reply.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b"{}"
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"raw": raw.decode("utf-8", "replace")[:200]}

    def call(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        return self._send(urllib.request.Request(
            self.base + urllib.parse.quote(path, safe="/?=&"),
            data=data,
            headers={"Content-Type": "application/json", "Origin": self.base}))

    def upload(self, path: str, filename: str, payload: bytes,
               fields: dict | None = None) -> tuple[int, dict]:
        """Файл на сервер ровно так, как это делает пульт из браузера."""
        boundary = "----printflow-pult-acceptance"
        chunks: list[bytes] = []
        for key, value in (fields or {}).items():
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"'
                          f'\r\n\r\n{value}\r\n'.encode())
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file";'
                      f' filename="{filename}"\r\nContent-Type: application/octet-stream'
                      f'\r\n\r\n'.encode())
        chunks.append(payload)
        chunks.append(f'\r\n--{boundary}--\r\n'.encode())
        return self._send(urllib.request.Request(
            self.base + path, data=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     "Origin": self.base}))


def wait_health(client: Client, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, payload = client.call("/api/health")
            if code == 200:
                return payload
        except Exception:  # noqa: BLE001 — сервер ещё поднимается
            time.sleep(0.4)
    raise RuntimeError("сервер не ответил на /api/health")


def source_data_dir() -> Path:
    """Рабочий каталог данных PrintFlow — откуда берём копию для приёмки."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "printflow"


def copy_workdir(source: Path, target: Path) -> bool:
    """Копия рабочих данных. Оригинал открывается только на чтение."""
    if not source.exists():
        return False
    shutil.copytree(source, target, dirs_exist_ok=True)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="порт (0 — свободный)")
    parser.add_argument("--keep", action="store_true", help="не удалять копию данных")
    parser.add_argument("--from", dest="source", default="",
                        help="каталог данных для копии (по умолчанию рабочий)")
    args = parser.parse_args(argv)

    if not CONNECTOR.exists():
        print(f"нет {CONNECTOR}", file=sys.stderr)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="pf-pult-acceptance-"))
    config = workdir / "config"
    data = config / "printflow"
    source = Path(args.source) if args.source else source_data_dir()
    copied = copy_workdir(source, data)
    port = args.port or free_port()
    base = f"http://127.0.0.1:{port}"

    print(f"Копия данных: {data}")
    print(f"Источник:     {source}" if copied else
          "Источник:     рабочих данных нет — приёмка идёт на пустой базе")
    print(f"Сервер:       {base}\n")

    before = sorted(data.glob("*")) if copied else []
    stamps = {p.name: p.stat().st_mtime for p in before}

    env = dict(os.environ, XDG_CONFIG_HOME=str(config))
    process = subprocess.Popen(
        [sys.executable, str(CONNECTOR), "--host", "127.0.0.1",
         "--port", str(port), "--no-browser"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    probe = Probe()
    client = Client(base)
    try:
        health = wait_health(client)
        print(f"Сервер поднялся: версия {health.get('version')}"
              f"{'' if copied else ' · база пустая — часть проверок неполная'}\n")
        run_checks(probe, client, copied=copied)
        untouched(probe, source, stamps) if copied else None
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
            print(f"\nКопия данных оставлена: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    print()
    return probe.report()


def untouched(probe: Probe, source: Path, stamps: dict[str, float]) -> None:
    """Рабочая база за время приёмки не изменилась — иначе это не приёмка."""
    def check():
        changed = [name for name, stamp in stamps.items()
                   if (source / name).exists() and (source / name).stat().st_mtime != stamp]
        assert not changed, f"изменились файлы рабочей базы: {changed}"
        return f"{len(stamps)} файлов рабочей базы не тронуты"
    probe.check("10. рабочая база не тронута", check)


def run_checks(probe: Probe, client: Client, copied: bool) -> None:
    state: dict = {}

    def scene1_park():
        code, page = None, None
        with urllib.request.urlopen(client.base + "/pult", timeout=20) as reply:
            code, page = reply.status, reply.read().decode("utf-8")
        assert code == 200, code
        assert "/api/pult/summary" in page, "страница не спрашивает сводку"
        with urllib.request.urlopen(client.base + "/pult.webmanifest", timeout=20) as reply:
            manifest = json.loads(reply.read())
        assert manifest.get("shortcuts"), "в манифесте нет ярлыков экранов"
        status, summary = client.call("/api/pult/summary")
        assert status == 200, summary
        for key in ("at", "printers", "queue", "farm", "ams", "spools", "alerts"):
            assert key in summary, f"в сводке нет ключа {key}"
        state["printers"] = summary["printers"]
        first = state["printers"][0]["id"] if state["printers"] else ""
        status, one = client.call(f"/api/pult/summary?printer_id={first}" if first else
                                  "/api/pult/summary?printer_id=prn_hint")
        assert status == 200, one
        return (f"страница 200, ярлыков {len(manifest['shortcuts'])}, "
                f"сводка: принтеров {len(summary['printers'])}, "
                f"очередь {len(summary['queue'])}, слотов AMS {len(summary['ams']['slots'])}")

    def scene2_commands():
        """Пауза/продолжение не проходят без подтверждения; стоп — тоже."""
        pid = state["printers"][0]["id"] if state["printers"] else "prn_hint"
        status, plain = client.call("/api/printer/command",
                                    {"printer_id": pid, "command": "pause"})
        text = json.dumps(plain, ensure_ascii=False)
        assert status != 500, text
        if not state["printers"]:
            return f"принтеров в базе нет — сервер честно ответил: {text[:60]}"
        assert "одтвер" in text, f"без confirmed команда не отклонена: {text[:80]}"
        status, full = client.call("/api/printer/command",
                                   {"printer_id": pid, "command": "pause", "confirmed": True})
        assert "одтвер" not in json.dumps(full, ensure_ascii=False), \
            "с подтверждением сервер всё ещё требует подтверждение"
        return f"без confirmed — «{text[:42]}», с confirmed — «{json.dumps(full, ensure_ascii=False)[:42]}»"

    def scene3_queue():
        ids = []
        for i in range(3):
            status, reply = client.call("/api/jobs/enqueue",
                                        {"name": f"Приёмка {i}", "file": f"accept{i}.gcode.3mf",
                                         "est_minutes": 20 + i, "est_grams": 30 + i})
            assert status == 200, reply
            ids.append(reply["job"]["id"])
        status, order = client.call("/api/pult/summary")
        queued = [j["id"] for j in order["queue"] if j["state"] == "queued"]
        assert set(ids) <= set(queued), f"задания не попали в очередь: {queued}"
        status, moved = client.call("/api/jobs/reorder", {"id": ids[-1], "direction": "up"})
        assert status == 200, moved
        status, cancelled = client.call("/api/jobs/cancel", {"id": ids[0]})
        assert status == 200, cancelled
        assert cancelled["job"]["state"] == "cancelled", cancelled
        status, gate = client.call("/api/jobs/start", {"id": ids[-1]})
        assert status != 200 and "одтвер" in json.dumps(gate, ensure_ascii=False), \
            f"запуск без confirmed прошёл: {gate}"
        return (f"3 задания в очереди, «выше» сработало, отмена → cancelled, "
                f"старт без confirmed: «{json.dumps(gate, ensure_ascii=False)[:46]}»")

    def scene4_ams():
        pid = state["printers"][0]["id"] if state["printers"] else "prn_hint"
        status, memory = client.call(f"/api/ams/memory?printer_id={pid}")
        assert status == 200 and "slots" in memory, memory
        status, cleared = client.call("/api/ams/memory/clear", {"printer_id": pid, "slot": "1"})
        assert status == 200 and "removed" in cleared, cleared
        status, bind = client.call("/api/spool/bind",
                                   {"id": "sp_hint", "printer_id": pid, "ams_slot": "1",
                                    "push_ams": True})
        text = json.dumps(bind, ensure_ascii=False)
        assert status != 500, text
        if state["printers"]:
            assert "одтвер" in text, f"привязка без confirmed не отклонена: {text[:80]}"
        return (f"память слотов: {len(memory['slots'])}, очистка слота 1 → removed "
                f"{cleared['removed']}, привязка без confirmed: «{text[:44]}»")

    def scene5_camera():
        pid = state["printers"][0]["id"] if state["printers"] else "prn_hint"
        answers = []
        for path, body in (("/api/printer/snapshot", {"printer_id": pid, "note": "приёмка"}),
                           ("/api/printer/command",
                            {"printer_id": pid, "command": "light", "value": True,
                             "confirmed": True})):
            status, reply = client.call(path, body)
            assert status != 500, f"{path} → {reply}"
            answers.append(json.dumps(reply, ensure_ascii=False)[:40])
        return f"снимок и свет отвечают без 500: «{answers[0]}» / «{answers[1]}»"

    def scene6_readonly():
        """Сводка — только чтение: N запросов подряд ничего не меняют."""
        def snapshot():
            status, reply = client.call("/api/pult/summary")
            assert status == 200, reply
            return (len(reply["queue"]), len(reply["spools"]),
                    sum(len(p.get("ams", {}).get("trays") or []) for p in reply["printers"]))
        before = snapshot()
        for _ in range(5):
            client.call("/api/pult/summary")
        after = snapshot()
        assert before == after, f"сводка изменила данные: {before} → {after}"
        return f"5 сводок подряд: очередь {after[0]}, катушек {after[1]}, треев {after[2]} — без изменений"

    def scene7_file_estimate():
        """Загрузка «как в слайсере»: оценка 3MF и отправка выбранной плиты."""
        with tempfile.TemporaryDirectory(prefix="pult-3mf-") as td:
            path = Path(td) / "pult-acceptance.3mf"
            slice_info = {"plates": [
                {"index": 1, "prediction": 4824, "weight": 23.4,
                 "filaments": [{"type": "PETG", "color": "#1F2937", "used_g": 23.4}]},
                {"index": 2, "prediction": 5760, "weight": 41.2,
                 "filaments": [{"type": "PETG", "color": "#1F2937", "used_g": 41.2}]},
            ]}
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("[Content_Types].xml", "<xml/>")
                zf.writestr("3D/3dmodel.model", "<model/>")
                zf.writestr("Metadata/slice_info.config", json.dumps(slice_info))
            payload = path.read_bytes()
            status, reply = client.upload("/api/estimate/upload", path.name, payload)
            assert status == 200, reply
            plates = (reply.get("estimate") or {}).get("plates") or []
            assert len(plates) == 2, f"плит в оценке: {len(plates)}"
            assert abs(float(reply.get("grams") or 0) - 64.6) < 0.3, reply.get("grams")
            assert abs(float(reply.get("minutes") or 0) - 176.4) < 0.5, reply.get("minutes")
            status, queued = client.upload("/api/jobs/upload", path.name, payload,
                                           {"plate": 2, "allow_auto_start": "false",
                                            "ams_mapping": "0,1"})
            assert status == 200, queued
            job = queued.get("job") or {}
            assert int(job.get("plate") or 0) == 2, f"у задания плита {job.get('plate')}"
            assert float(job.get("est_grams") or 0) > 0, "очередь не увидела вес файла"
            assert job.get("ams_mapping") == "[0, 1]", \
                f"раскладка AMS не доехала: {job.get('ams_mapping')!r}"
            return (f"оценка: плит {len(plates)}, {reply['grams']} г · {reply['minutes']} мин; "
                    f"в очереди задание «{job.get('name')}» плита {job.get('plate')}, "
                    f"раскладка {job.get('ams_mapping')}")

    def scene8_empty_estimate():
        """Файл без данных слайсера не выдумывает цифры, но и не теряется."""
        with tempfile.TemporaryDirectory(prefix="pult-raw-") as td:
            path = Path(td) / "raw.3mf"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("[Content_Types].xml", "<xml/>")
                zf.writestr("3D/3dmodel.model", "<model/>")
            status, reply = client.upload("/api/estimate/upload", path.name, path.read_bytes())
            assert status == 200, reply
            assert not float(reply.get("grams") or 0), f"граммы появились из ниоткуда: {reply}"
            assert not float(reply.get("minutes") or 0), f"минуты появились из ниоткуда: {reply}"
            assert reply.get("file"), "файл обязан лечь в библиотеку даже без оценки"
            return (f"без данных слайсера: {reply['grams']} г, {reply['minutes']} мин, "
                    f"файл сохранён как «{reply['file']}»")

    def scene9_autonomy():
        """Почему очередь идёт сама или стоит — словами сервера (18.0.8)."""
        status, summary = client.call("/api/pult/summary")
        assert status == 200, summary
        auto = summary.get("autonomy") or {}
        assert auto, "в сводке нет отчёта автономности"
        for key in ("auto_queue", "safety_gate", "armed", "quiet", "reasons",
                    "printers", "next", "rules"):
            assert key in auto, f"в отчёте нет ключа {key}"
        assert auto["rules"], "правила очереди не пришли — оператор не поймёт, кто решает"
        armed = bool(auto["auto_queue"] and auto["safety_gate"] and not auto["quiet"])
        assert auto["armed"] == armed, f"armed не сходится с флагами: {auto}"
        if not auto["armed"]:
            assert auto["reasons"], "автозапуск не действует, а причины не сказаны"
        nxt = (auto.get("next") or {}).get("job") or {}
        return (f"автозапуск {'включён' if auto['auto_queue'] else 'выключен'}, "
                f"без присмотра {'разрешён' if auto['safety_gate'] else 'запрещён'}, "
                f"правил {len(auto['rules'])}, принтеров {len(auto['printers'])}, "
                f"следующее «{nxt.get('name') or '—'}»"
                + (f", причина: «{auto['reasons'][0][:60]}»" if auto["reasons"] else ""))

    probe.check("1. парк и сводка одним запросом", scene1_park)
    probe.check("2. команды требуют подтверждения", scene2_commands)
    probe.check("3. очередь: порядок, отмена, гейт старта", scene3_queue)
    probe.check("4. AMS: память слота и привязка", scene4_ams)
    probe.check("5. камера и свет отвечают", scene5_camera)
    probe.check("6. сводка ничего не пишет", scene6_readonly)
    probe.check("7. оценка из файла и отправка плиты", scene7_file_estimate)
    probe.check("8. файл без данных слайсера", scene8_empty_estimate)
    probe.check("9. почему очередь идёт сама или стоит", scene9_autonomy)
    if not copied:
        probe.check("10. рабочая база не тронута",
                    lambda: "рабочих данных нет — приёмка шла на пустой базе")


if __name__ == "__main__":
    sys.exit(main())

"""Парк принтеров: подключения, очередь заданий и автоматический учёт.

PrinterManager связывает MQTT-мост, базу данных и бухгалтерию: следит за
состоянием каждого принтера, пишет журнал печати, списывает пластик и
запускает следующее задание из общей очереди.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

from .accounting import Accounting, num, uid
from .bambu import BambuPrinter
from .config import now_iso
from .db import Database
from .repo import Repo


class PrinterManager:
    def __init__(self, db: Database, repo: Repo):
        self.db = db
        self.repo = repo
        self.acc = Accounting(db)
        self.printers: dict[str, BambuPrinter] = {}
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self.reload()
        self._poller = threading.Thread(target=self._loop, name="pf-manager", daemon=True)
        self._poller.start()

    # ------------------------------------------------------------- управление
    def reload(self) -> None:
        """Синхронизировать список подключений с таблицей printers."""
        records = {r["id"]: r for r in self.db.query("SELECT * FROM printers")}
        with self.lock:
            for pid in list(self.printers):
                if pid not in records:
                    self.printers.pop(pid).shutdown()
            for pid, record in records.items():
                if pid in self.printers:
                    self.printers[pid].update_record(record)
                else:
                    printer = BambuPrinter(record, self._make_handler(pid))
                    self.printers[pid] = printer
                    if record.get("enabled", 1):
                        printer.start()

    def get(self, printer_id: str = "") -> BambuPrinter | None:
        with self.lock:
            if printer_id:
                return self.printers.get(printer_id)
            for printer in self.printers.values():
                if printer.connected:
                    return printer
            return next(iter(self.printers.values()), None)

    def shutdown(self) -> None:
        self._stop.set()
        with self.lock:
            for printer in self.printers.values():
                printer.shutdown()

    # ---------------------------------------------------------------- события
    def _make_handler(self, printer_id: str):
        def handler(kind: str, title: str, detail: str, data: dict):
            try:
                self._handle_event(printer_id, kind, title, detail, data)
            except Exception as exc:  # журнал не должен ронять MQTT-поток
                self.db.add_event("error", "Ошибка обработки события", str(exc), printer_id)
        return handler

    def _handle_event(self, printer_id: str, kind: str, title: str, detail: str, data: dict) -> None:
        self.db.add_event(kind, title, detail, printer_id, data)
        if kind == "start":
            self._on_print_start(printer_id, detail, data)
        elif kind in ("complete", "error", "stop"):
            self._on_print_end(printer_id, kind, detail, data)
        self._notify(kind, title, detail, printer_id)

    def _on_print_start(self, printer_id: str, name: str, data: dict) -> None:
        job = self.db.one(
            "SELECT * FROM print_jobs WHERE printer_id=? AND state='running'", (printer_id,))
        if job:
            return
        job = self.db.one(
            "SELECT * FROM print_jobs WHERE printer_id=? AND state='starting'"
            " ORDER BY datetime(created_at) DESC LIMIT 1", (printer_id,))
        order_id = job.get("order_id") if job else self._guess_order(name)
        if job:
            self.db.execute(
                "UPDATE print_jobs SET state='running', started_at=?, name=? WHERE id=?",
                (now_iso(), name or job.get("name", ""), job["id"]))
        else:
            self.db.upsert("print_jobs", {
                "id": uid("job"), "printer_id": printer_id, "order_id": order_id,
                "name": name, "file": name, "state": "running", "source": "printer",
                "started_at": now_iso(), "created_at": now_iso()})
        if order_id:
            printing = self.db.one("SELECT id FROM statuses WHERE id='printing'")
            if printing:
                self.db.execute("UPDATE orders SET status='printing', updated_at=? WHERE id=?",
                                (now_iso(), order_id))

    def _on_print_end(self, printer_id: str, kind: str, name: str, data: dict) -> None:
        job = self.db.one(
            "SELECT * FROM print_jobs WHERE printer_id=? AND state IN ('running','starting')"
            " ORDER BY datetime(created_at) DESC LIMIT 1", (printer_id,))
        state = {"complete": "done", "error": "failed", "stop": "cancelled"}[kind]
        duration = num(data.get("duration_min"))
        grams = num(data.get("weight"))
        if not grams and job and job.get("order_id"):
            order = self.db.one("SELECT grams, qty FROM orders WHERE id=?", (job["order_id"],))
            if order:
                grams = num(order["grams"]) * max(1.0, num(order["qty"], 1))
        if not job:
            job = self.db.upsert("print_jobs", {
                "id": uid("job"), "printer_id": printer_id, "order_id": self._guess_order(name),
                "name": name, "state": state, "source": "printer",
                "started_at": now_iso(), "created_at": now_iso()})
        self.db.execute(
            "UPDATE print_jobs SET state=?, finished_at=?, duration_min=?, grams=?,"
            " progress=?, layers=?, result=? WHERE id=?",
            (state, now_iso(), round(duration, 1), round(grams, 1),
             num(data.get("progress")), int(num(data.get("total_layers"))), kind, job["id"]))
        job = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job["id"],)) or job
        if state in ("done", "failed"):
            printer = self.get(printer_id)
            if printer and not job.get("spool_id"):
                snapshot = printer.snapshot()
                active = next((t for t in snapshot["ams"]["trays"] if t["active"]), None)
                if active:
                    spool = self.acc.pick_spool(printer_id, str(active["slot"]),
                                                active["type"], active["uuid"])
                    if spool:
                        self.db.execute("UPDATE print_jobs SET spool_id=? WHERE id=?",
                                        (spool["id"], job["id"]))
                        job["spool_id"] = spool["id"]
            self.acc.register_job_costs(job)
        if state == "done" and job.get("order_id"):
            self.db.execute("UPDATE orders SET status='post', updated_at=? WHERE id=? AND status='printing'",
                            (now_iso(), job["order_id"]))
        self._maybe_start_next(printer_id)

    def _guess_order(self, name: str) -> str | None:
        """Связать печать с заказом по имени файла (если включено)."""
        if not name or not self.db.setting("auto_link_orders", True):
            return None
        clean = name.lower().rsplit("/", 1)[-1]
        for order in self.db.query(
                "SELECT id, number, product, file FROM orders WHERE status NOT IN"
                " (SELECT id FROM statuses WHERE is_final=1)"):
            for field in (order.get("file"), order.get("product")):
                value = (field or "").strip().lower()
                if value and (value in clean or clean.startswith(value[:12])):
                    return order["id"]
            number = str(order.get("number") or "")
            if number and number in clean:
                return order["id"]
        return None

    # ----------------------------------------------------------------- очередь
    def queue(self) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM print_jobs WHERE state IN ('queued','starting','running')"
            " ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'starting' THEN 1 ELSE 2 END,"
            " priority DESC, datetime(created_at)")
        for row in rows:
            if row.get("order_id"):
                order = self.db.one("SELECT number, product, customer_name FROM orders WHERE id=?",
                                    (row["order_id"],))
                row["order"] = order
        return rows

    def history(self, limit: int = 100) -> list[dict]:
        return self.db.query(
            "SELECT * FROM print_jobs WHERE state IN ('done','failed','cancelled')"
            " ORDER BY datetime(finished_at) DESC LIMIT ?", (int(limit),))

    def enqueue(self, data: dict) -> dict:
        job = {
            "id": uid("job"),
            "printer_id": data.get("printer_id") or None,
            "order_id": data.get("order_id") or None,
            "name": data.get("name") or (data.get("file") or "").rsplit("/", 1)[-1],
            "file": data.get("file", ""),
            "state": "queued",
            "source": data.get("source", "queue"),
            "plate": int(num(data.get("plate"), 1) or 1),
            "use_ams": 1 if data.get("use_ams", True) else 0,
            "bed_level": 1 if data.get("bed_level", True) else 0,
            "flow_cali": 1 if data.get("flow_cali") else 0,
            "timelapse": 1 if data.get("timelapse") else 0,
            "ams_mapping": json.dumps(data.get("ams_mapping") or []),
            "priority": int(num(data.get("priority"))),
            "spool_id": data.get("spool_id") or None,
            "queued_at": now_iso(), "created_at": now_iso(),
        }
        row = self.db.upsert("print_jobs", job)
        self.db.add_event("queue", "Задание добавлено в очередь", job["name"],
                          job["printer_id"] or "", {"job_id": job["id"]})
        if self.db.setting("auto_queue", False):
            self._maybe_start_next(job["printer_id"] or "")
        return row

    def cancel_job(self, job_id: str) -> dict:
        job = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))
        if not job:
            raise ValueError("Задание не найдено")
        if job["state"] == "running":
            printer = self.get(job["printer_id"] or "")
            if printer:
                printer.command("stop")
        self.db.execute("UPDATE print_jobs SET state='cancelled', finished_at=? WHERE id=?",
                        (now_iso(), job_id))
        return self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,)) or {}

    def start_job(self, job_id: str, printer_id: str = "") -> dict:
        job = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))
        if not job:
            raise ValueError("Задание не найдено")
        printer = self.get(printer_id or job.get("printer_id") or "")
        if not printer:
            raise ValueError("Нет доступного принтера")
        if not job.get("file"):
            raise ValueError("В задании не указан файл на принтере")
        try:
            mapping = json.loads(job.get("ams_mapping") or "[]")
        except json.JSONDecodeError:
            mapping = []
        printer.start_print(job["file"], plate=int(num(job.get("plate"), 1) or 1),
                            use_ams=bool(job.get("use_ams", 1)), ams_mapping=mapping,
                            bed_level=bool(job.get("bed_level", 1)),
                            flow_cali=bool(job.get("flow_cali")),
                            timelapse=bool(job.get("timelapse")),
                            subtask_name=job.get("name", ""))
        self.db.execute(
            "UPDATE print_jobs SET state='starting', printer_id=?, started_at=? WHERE id=?",
            (printer.id, now_iso(), job_id))
        return self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,)) or {}

    def _maybe_start_next(self, printer_id: str) -> None:
        if not self.db.setting("auto_queue", False):
            return
        printer = self.get(printer_id)
        if not printer or not printer.connected:
            return
        state = printer.snapshot()["printer"]["state"]
        if state not in ("IDLE", "FINISH"):
            return
        job = self.db.one(
            "SELECT * FROM print_jobs WHERE state='queued' AND (printer_id IS NULL OR printer_id=?)"
            " AND file<>'' ORDER BY priority DESC, datetime(created_at) LIMIT 1", (printer_id,))
        if job:
            try:
                self.start_job(job["id"], printer_id)
            except Exception as exc:
                self.db.add_event("error", "Автозапуск задания не удался", str(exc), printer_id)

    # ------------------------------------------------------------ уведомления
    def _notify(self, kind: str, title: str, detail: str, printer_id: str) -> None:
        settings = self.db.settings(include_secrets=True)
        if not settings.get("telegram_enabled") or not settings.get("telegram_token"):
            return
        flags = {"complete": "notify_complete", "error": "notify_error", "pause": "notify_pause",
                 "filament_low": "notify_filament_low"}
        flag = flags.get(kind)
        if not flag or not settings.get(flag):
            return
        printer = self.get(printer_id)
        name = printer.record.get("name", "Принтер") if printer else "PrintFlow"
        text = f"PrintFlow · {name}\n{title}\n{detail}".strip()
        threading.Thread(target=self.send_telegram, args=(text,), daemon=True).start()

    def send_telegram(self, text: str) -> dict:
        settings = self.db.settings(include_secrets=True)
        token, chat = settings.get("telegram_token"), settings.get("telegram_chat_id")
        if not token or not chat:
            return {"ok": False, "error": "Не заданы токен или chat_id"}
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
            request = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data)
            with urllib.request.urlopen(request, timeout=10) as response:
                return {"ok": response.status == 200}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # -------------------------------------------------------------- состояние
    def snapshot(self, printer_id: str = "") -> dict:
        with self.lock:
            printers = [p.snapshot() for p in self.printers.values()]
        active_id = printer_id or (printers[0]["id"] if printers else "")
        active = next((p for p in printers if p["id"] == active_id), printers[0] if printers else None)
        return {
            "at": now_iso(),
            "printers": printers,
            "active": active,
            "queue": self.queue(),
            "farm": self.farm_stats(printers),
        }

    def farm_stats(self, printers: list[dict] | None = None) -> dict:
        printers = printers if printers is not None else [p.snapshot() for p in self.printers.values()]
        printing = [p for p in printers if p["printer"]["state"] in ("RUNNING", "PREPARE")]
        online = [p for p in printers if p["connection"]["connected"]]
        today = self.db.one(
            "SELECT COALESCE(SUM(print_minutes),0) m, COALESCE(SUM(grams),0) g,"
            " COALESCE(SUM(jobs_done),0) d FROM printer_stats WHERE day=date('now','localtime')") or {}
        return {
            "total": len(printers),
            "online": len(online),
            "printing": len(printing),
            "queued": len([j for j in self.queue() if j["state"] == "queued"]),
            "today_hours": round(num(today.get("m")) / 60, 1),
            "today_grams": round(num(today.get("g")), 1),
            "today_jobs": int(num(today.get("d"))),
            "utilization": round(len(printing) / len(printers) * 100) if printers else 0,
        }

    # ------------------------------------------------------------ фоновый цикл
    def _loop(self) -> None:
        """Раз в 30 секунд обновляем прогресс активных заданий."""
        while not self._stop.wait(30):
            try:
                with self.lock:
                    printers = list(self.printers.values())
                for printer in printers:
                    if not printer.connected:
                        continue
                    snap = printer.snapshot()
                    job = self.db.one(
                        "SELECT id FROM print_jobs WHERE printer_id=? AND state='running'",
                        (printer.id,))
                    if job:
                        self.db.execute(
                            "UPDATE print_jobs SET progress=?, layers=? WHERE id=?",
                            (snap["printer"]["progress"], snap["printer"]["total_layers"], job["id"]))
                    if snap["printer"]["state"] in ("IDLE", "FINISH"):
                        self._maybe_start_next(printer.id)
            except Exception:
                continue

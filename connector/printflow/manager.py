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
from .telegram_bot import TelegramBot
from .watchdog import Watchdog


class PrinterManager:
    def __init__(self, db: Database, repo: Repo):
        self.db = db
        self.repo = repo
        self.acc = Accounting(db)
        self.printers: dict[str, BambuPrinter] = {}
        self.guard = Watchdog(self)
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self.reload()
        self._poller = threading.Thread(target=self._loop, name="pf-manager", daemon=True)
        self._poller.start()
        self.bot = TelegramBot(self)

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
            for pid in records:
                try:
                    self.guard.seed_maintenance(pid)
                except Exception:
                    continue

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
        bot = getattr(self, "bot", None)
        if bot:
            bot.shutdown()
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
            # Наработка принтера — основа напоминаний об обслуживании.
            try:
                self.guard.add_runtime(printer_id, duration, grams)
            except Exception as exc:
                self.db.add_event("error", "Не удалось учесть наработку", str(exc), printer_id)
        if state == "failed":
            self._register_failure(printer_id, job, duration, grams)
        if state == "done" and job.get("order_id"):
            self.db.execute("UPDATE orders SET status='post', updated_at=? WHERE id=? AND status='printing'",
                            (now_iso(), job["order_id"]))
        self._maybe_start_next(printer_id)

    def _register_failure(self, printer_id: str, job: dict, minutes: float, grams: float) -> None:
        """Сорванная печать — это потерянные пластик, время и электричество."""
        if not self.db.setting("guard_count_loss", True):
            return
        settings = self.db.settings()
        spool = None
        if job.get("spool_id"):
            spool = self.db.one("SELECT * FROM spools WHERE id=?", (job["spool_id"],))
        price = num(spool.get("price")) if spool else num(settings.get("default_spool_price"), 1600)
        weight = num(spool.get("total_grams")) if spool else num(settings.get("default_spool_weight"), 1000)
        per_gram = price / weight if weight else 1.6
        filament = round(num(grams) * per_gram, 2)
        hours = max(0.0, num(minutes)) / 60
        energy = round(hours * num(settings.get("power_kw"), 0.15)
                       * num(settings.get("energy_price"), 6.0), 2)
        amort = round(hours * num(settings.get("amortization_per_hour"), 12.0), 2)
        loss = round(filament + energy + amort, 2)
        if loss <= 0:
            return
        self.db.add_event(
            "loss", "Брак: печать не удалась",
            f"Потеряно {loss:.0f} ₽ — пластик {filament:.0f} ₽, "
            f"электричество {energy:.0f} ₽, износ {amort:.0f} ₽",
            printer_id, {"job_id": job.get("id"), "grams": grams, "minutes": minutes,
                         "filament": filament, "energy": energy, "amortization": amort,
                         "total": loss})

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

    # ------------------------------------------------------- умная очередь
    def quiet_now(self) -> bool:
        """Идут ли тихие часы: ночью принтер не запускаем."""
        if not self.db.setting("quiet_hours_enabled", False):
            return False
        try:
            start = str(self.db.setting("quiet_from", "23:00"))
            end = str(self.db.setting("quiet_to", "08:00"))
            now = time.strftime("%H:%M")
            if start <= end:
                return start <= now < end
            return now >= start or now < end  # интервал через полночь
        except Exception:
            return False

    def _job_material(self, job: dict) -> str:
        """Материал задания: из катушки, из заказа или из каталога."""
        if job.get("spool_id"):
            spool = self.db.one("SELECT material FROM spools WHERE id=?", (job["spool_id"],))
            if spool and spool.get("material"):
                return str(spool["material"]).upper()
        if job.get("order_id"):
            order = self.db.one("SELECT material FROM orders WHERE id=?", (job["order_id"],))
            if order and order.get("material"):
                return str(order["material"]).upper()
        return ""

    def _enough_filament(self, job: dict, snap: dict) -> tuple[bool, str]:
        """Хватит ли пластика в активном слоте на это задание."""
        if not self.db.setting("queue_check_filament", True):
            return True, ""
        need = 0.0
        if job.get("order_id"):
            order = self.db.one("SELECT grams, qty FROM orders WHERE id=?", (job["order_id"],))
            if order:
                need = num(order.get("grams")) * max(1.0, num(order.get("qty"), 1))
        if not need:
            return True, ""
        active = next((t for t in snap["ams"].get("trays", []) if t.get("active")), None)
        if not active:
            return True, ""
        spool = self.acc.pick_spool(job.get("printer_id") or "", str(active.get("slot")),
                                    active.get("type"), active.get("uuid"))
        if not spool:
            return True, ""
        left = num(spool.get("remaining_grams"))
        if left and left < need:
            return False, (f"Нужно {need:.0f} г, в катушке «{spool.get('name', '')}» "
                           f"осталось {left:.0f} г")
        return True, ""

    def next_job(self, printer_id: str, snap: dict | None = None) -> dict | None:
        """Выбрать следующее задание с учётом материала в AMS."""
        jobs = self.db.query(
            "SELECT * FROM print_jobs WHERE state='queued' AND (printer_id IS NULL OR printer_id=?)"
            " AND file<>'' ORDER BY priority DESC, datetime(created_at)", (printer_id,))
        if not jobs:
            return None
        if not self.db.setting("queue_group_material", True) or not snap:
            return jobs[0]
        loaded = {str(t.get("type") or "").upper()
                  for t in snap["ams"].get("trays", []) if t.get("type")}
        if not loaded:
            return jobs[0]
        # Сначала то, что печатается уже заправленным материалом:
        # меньше смен катушки — меньше отходов на продувку.
        same = [j for j in jobs if not self._job_material(j)
                or self._job_material(j) in loaded]
        return same[0] if same else jobs[0]

    def _maybe_start_next(self, printer_id: str) -> None:
        if not self.db.setting("auto_queue", False):
            return
        printer = self.get(printer_id)
        if not printer or not printer.connected:
            return
        snap = printer.snapshot()
        if snap["printer"]["state"] not in ("IDLE", "FINISH"):
            return
        if self.quiet_now():
            return
        if snap["printer"].get("problems"):
            self.db.add_event("queue", "Автозапуск отложен",
                              "Принтер сообщает об ошибке — сначала разберитесь с ней",
                              printer_id, {})
            return
        job = self.next_job(printer_id, snap)
        if not job:
            return
        ok, reason = self._enough_filament(job, snap)
        if not ok:
            self.db.add_event("queue", "Автозапуск отложен: мало пластика",
                              reason, printer_id, {"job_id": job["id"]})
            return
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
        # К завершению и ошибке прикладываем кадр: сразу видно результат.
        photo = None
        if (printer and kind in ("complete", "error")
                and settings.get("notify_photo", True)):
            photo = printer.camera.frame
        self.notify_async(text, photo)

    def notify_async(self, text: str, photo: bytes | None = None) -> None:
        """Отправить сообщение в фоне, чтобы не тормозить поток телеметрии."""
        threading.Thread(target=self.send_telegram, args=(text, photo), daemon=True).start()

    def send_telegram(self, text: str, photo: bytes | None = None) -> dict:
        settings = self.db.settings(include_secrets=True)
        token, chat = settings.get("telegram_token"), settings.get("telegram_chat_id")
        if not token or not chat:
            return {"ok": False, "error": "Не заданы токен или chat_id"}
        try:
            if photo:
                return self._send_photo(token, str(chat), text, photo)
            data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
            request = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data)
            with urllib.request.urlopen(request, timeout=10) as response:
                return {"ok": response.status == 200}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _send_photo(token: str, chat: str, caption: str, photo: bytes) -> dict:
        """Кадр камеры прямо в сообщении: видно, что происходит, без захода в дом."""
        boundary = "----printflow" + uid("b").replace("b_", "")
        parts: list[bytes] = []
        for name, value in (("chat_id", chat), ("caption", caption[:1000])):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                         f"{value}\r\n".encode())
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\";"
                     " filename=\"frame.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode())
        parts.append(photo)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendPhoto", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return {"ok": response.status == 200}

    # -------------------------------------------------------------- состояние
    def snapshot(self, printer_id: str = "") -> dict:
        with self.lock:
            printers = [p.snapshot() for p in self.printers.values()]
        for snap in printers:
            snap["guard"] = {
                "enabled": bool(self.db.setting("guard_enabled", True)),
                "alerts": self.guard.alerts(snap["id"]),
            }
            snap["maintenance"] = self.maintenance_summary(snap["id"])
            snap["job"] = self.job_summary(snap)
        active_id = printer_id or (printers[0]["id"] if printers else "")
        active = next((p for p in printers if p["id"] == active_id), printers[0] if printers else None)
        return {
            "at": now_iso(),
            "printers": printers,
            "active": active,
            "queue": self.queue(),
            "farm": self.farm_stats(printers),
            "quiet": self.quiet_now(),
        }

    def wall(self) -> dict:
        """Компактная сводка для режима «Стена»: только то, что видно издалека."""
        state = self.snapshot()
        tiles = []
        for snap in state["printers"]:
            info = snap["printer"]
            job = snap.get("job") or {}
            order = job.get("order") or {}
            alerts = (snap.get("guard") or {}).get("alerts") or []
            tiles.append({
                "id": snap["id"],
                "name": snap["name"],
                "online": snap["connection"]["connected"],
                "state": info["state"],
                "state_label": info["state_label"],
                "task": info["task"],
                "progress": round(num(info.get("progress"))),
                "layer": info.get("layer"),
                "total_layers": info.get("total_layers"),
                "remaining_min": round(num(info.get("remaining_min"))),
                "eta": info.get("eta"),
                "nozzle": snap["temperature"]["nozzle"],
                "bed": snap["temperature"]["bed"],
                "camera": snap["camera"],
                "severity": info.get("severity", ""),
                "alerts": alerts,
                "order": {"number": order.get("number"), "product": order.get("product"),
                          "customer": order.get("customer_name")} if order else None,
                "spent": job.get("spent"),
                "maintenance_due": (snap.get("maintenance") or {}).get("due", 0),
            })
        return {
            "at": now_iso(),
            "tiles": tiles,
            "farm": state["farm"],
            "queue": [
                {"id": j["id"], "name": j.get("name"), "state": j.get("state"),
                 "order": (j.get("order") or {}).get("number")}
                for j in state["queue"][:8]
            ],
            "quiet": state["quiet"],
        }

    def maintenance_summary(self, printer_id: str) -> dict:
        """Короткая сводка по обслуживанию для карточки принтера."""
        try:
            tasks = self.guard.maintenance(printer_id)
        except Exception:
            tasks = []
        due = [t for t in tasks if t["due"]]
        soon = [t for t in tasks if t["soon"]]
        return {
            "hours": self.guard.runtime_hours(printer_id),
            "due": len(due),
            "soon": len(soon),
            "next": min((t for t in tasks if t.get("left_hours") is not None),
                        key=lambda t: t["left_hours"], default=None),
            "tasks": tasks,
        }

    def job_summary(self, snap: dict) -> dict:
        """Во что обходится текущая печать прямо сейчас."""
        info = snap.get("printer") or {}
        if info.get("state") not in ("RUNNING", "PAUSE", "PREPARE"):
            return {}
        settings = self.db.settings()
        elapsed_h = num(info.get("elapsed_min")) / 60
        remaining_h = num(info.get("remaining_min")) / 60
        total_h = elapsed_h + remaining_h
        grams = num(info.get("weight"))
        per_gram = (num(settings.get("default_spool_price"), 1600)
                    / max(1.0, num(settings.get("default_spool_weight"), 1000)))
        energy_rate = num(settings.get("power_kw"), 0.15) * num(settings.get("energy_price"), 6.0)
        wear_rate = (num(settings.get("amortization_per_hour"), 12.0)
                     + num(settings.get("maintenance_per_hour"), 3.0))
        spent = round(grams * per_gram * (num(info.get("progress")) / 100)
                      + elapsed_h * (energy_rate + wear_rate), 2)
        total = round(grams * per_gram + total_h * (energy_rate + wear_rate), 2)
        job = self.db.one(
            "SELECT * FROM print_jobs WHERE printer_id=? AND state='running'", (snap["id"],))
        order = None
        if job and job.get("order_id"):
            order = self.db.one(
                "SELECT id, number, product, customer_name, price, due FROM orders WHERE id=?",
                (job["order_id"],))
        return {
            "job_id": job["id"] if job else "",
            "order": order,
            "grams": round(grams, 1),
            "spent": spent,
            "cost_total": total,
            "energy_kwh": round(total_h * num(settings.get("power_kw"), 0.15), 2),
            "per_hour": round(energy_rate + wear_rate, 2),
            "elapsed_min": round(num(info.get("elapsed_min"))),
            "remaining_min": round(num(info.get("remaining_min"))),
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
        """Раз в 30 секунд: прогресс, телеметрия, сторож и очередь."""
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
                    try:
                        self.guard.record_telemetry(printer, snap, job["id"] if job else "")
                        self.guard.check(printer, snap)
                    except Exception as exc:
                        self.db.add_event("error", "Сбой сторожа печати", str(exc), printer.id)
                    if snap["printer"]["state"] in ("IDLE", "FINISH"):
                        self._maybe_start_next(printer.id)
            except Exception:
                continue

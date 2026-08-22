"""Парк принтеров: подключения, очередь заданий и автоматический учёт.

PrinterManager связывает MQTT-мост, базу данных и бухгалтерию: следит за
состоянием каждого принтера, пишет журнал печати, списывает пластик и
запускает следующее задание из общей очереди.
"""
from __future__ import annotations

import io
import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from .accounting import Accounting, num, uid
from .bambu import BambuPrinter
from .config import (BACKUP_DIR, DANGEROUS_AUTOMATION_COMMANDS, UPLOAD_DIR,
                     now_iso, rotate_backups)
from .db import Database
from .repo import Repo
from .telegram_bot import TelegramBot
from .watchdog import Watchdog


class PrinterManager:
    def __init__(self, db: Database, repo: Repo):
        self.db = db
        self.repo = repo
        self.acc = Accounting(db)
        # Конструктор правил «если-то»: настраиваемая автоматизация поверх событий.
        from .rules import RulesEngine
        self.rules = RulesEngine(self)
        # Хук перехода заказа между статусами (repo не знает о менеджере).
        if self.repo is not None:
            self.repo._on_status_change = self.rules.on_order_status
        self.printers: dict[str, BambuPrinter] = {}
        self.guard = Watchdog(self)
        from .spaghetti import SpaghettiWatch
        self.spaghetti = SpaghettiWatch(self)
        # Учёт партий подключает api.py после создания менеджера (см. Api.__init__).
        self.batches = None
        self.lock = threading.RLock()
        self._stop = threading.Event()
        # Память мониторинга: tray_uuid слотов, отчёты о расхождениях, напоминания
        self._tray_uuids: dict[str, dict[str, str]] = {}
        self._ams_reported: dict[str, set[str]] = {}
        self._finish_reminded: set[str] = set()
        self._restock_reported: set[str] = set()
        self._cost_limit_reported: set[str] = set()
        self._dry_reported: float = 0.0
        self._last_ams_sync = 0.0
        self._last_cloud_sync: dict[str, float] = {}
        self._last_backup = 0.0
        self._last_backup_attempt = 0.0
        # Авто-продолжение при сбое питания (Крым): трекинг пауз и попыток
        self._user_paused: dict[str, float] = {}
        # 8.5: видео печати, первый слой
        self._keyframe_last: dict[str, float] = {}
        self._first_layer_ref: dict[str, float] = {}
        self._first_layer_alerted: set[str] = set()
        self._auto_resume_attempts: dict[str, dict] = {}
        self._auto_resume_blocked_at: dict[str, float] = {}
        self._startup_ts = time.time()
        self.reload()
        # Если коннектор перезапустился во время печати, сохраняем кандидата
        # на power-loss recovery. Ручная пауза хранится отдельно и не будет
        # возобновлена автоматически.
        self._mark_restart_recovery_candidates()
        # 8.0: Watch Folder
        try:
            from .watch_folder import WatchFolder
            self.watch = WatchFolder(self.db, self, getattr(self.db, 'bus', None))
            self.watch.start()
        except Exception:
            self.watch = None
        self._poller = threading.Thread(target=self._loop, name="pf-manager", daemon=True)
        self._poller.start()
        # Мгновенная проверка при запуске скрипта для авто-продолжения печати
        self._startup_thread = threading.Thread(target=self._startup_auto_resume_loop, name="pf-auto-resume", daemon=True)
        self._startup_thread.start()
        self.bot = TelegramBot(self)

    # ------------------------------------------------------------- управление
    def reload(self) -> None:
        """Синхронизировать список подключений с таблицей printers."""
        from .virtual import VIRTUAL_ID
        records = {r["id"]: r for r in self.db.query("SELECT * FROM printers")}
        # Access Code может лежать зашифрованным — расшифровываем для подключения.
        try:
            from .crypto import decrypt, is_encrypted
            for record in records.values():
                if is_encrypted(record.get("access_code") or ""):
                    record["access_code"] = decrypt(record["access_code"])
        except Exception:
            pass
        cloud = self._cloud_creds()
        runtime = {
            key: self.db.setting(key, default)
            for key, default in (
                ("ftps_timeout", 8), ("ftps_retries", 3), ("ftps_block_kb", 256),
                ("mqtt_keepalive", 30), ("mqtt_backoff", True),
            )
        }
        with self.lock:
            for pid in list(self.printers):
                if pid not in records and pid != VIRTUAL_ID:
                    self.printers.pop(pid).shutdown()
            for pid, record in records.items():
                enriched = dict(record)
                enriched["cloud_token"] = cloud.get("token", "")
                enriched["cloud_uid"] = cloud.get("uid", "")
                enriched["cloud_region"] = cloud.get("region", "global")
                enriched.update(runtime)
                if pid in self.printers:
                    self.printers[pid].update_record(enriched)
                else:
                    printer = BambuPrinter(enriched, self._make_handler(pid))
                    self.printers[pid] = printer
                    if record.get("enabled", 1):
                        printer.start()
            for pid in records:
                try:
                    self.guard.seed_maintenance(pid)
                except Exception:
                    continue
            # Виртуальный принтер (идея 7): демо-P1S не живёт в таблице
            # printers, его жизненным циклом управляет demo_printer_enabled.
            from .virtual import VirtualPrinter
            demo_on = bool(self.db.setting("demo_printer_enabled", False))
            existing = self.printers.get(VIRTUAL_ID)
            demo_record = {"id": VIRTUAL_ID, "name": "P1S (виртуальный)",
                           "model": "P1S", "enabled": 1, "mode": "virtual"}
            if demo_on and not existing:
                printer = VirtualPrinter(self.db, demo_record,
                                         self._make_handler(VIRTUAL_ID))
                self.printers[VIRTUAL_ID] = printer
                printer.start()
            elif demo_on and existing:
                existing.update_record(demo_record)
            elif existing:
                self.printers.pop(VIRTUAL_ID).shutdown()

    def _cloud_creds(self) -> dict:
        """Токен/uid/регион аккаунта Bambu из настроек (для облачных принтеров)."""
        settings = self.db.settings(include_secrets=True)
        return {
            "token": str(settings.get("cloud_token") or ""),
            "uid": str(settings.get("cloud_uid") or ""),
            "region": str(settings.get("cloud_region") or "global"),
        }

    def refresh_cloud(self) -> None:
        """Вход/выход из Bambu Cloud: перезапустить облачные подключения."""
        from .cloud_bridge import CloudBridge
        if not self._cloud_creds()["token"]:
            CloudBridge.shutdown_all()
        self.reload()

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
        if getattr(self, "watch", None):
            try:
                self.watch.stop()
            except Exception:
                pass
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
            self._auto_resume_attempts.pop(printer_id, None)
            self._on_print_start(printer_id, detail, data)
        elif kind in ("complete", "error", "stop"):
            self._auto_resume_attempts.pop(printer_id, None)
            if kind in ("complete", "stop"):
                self.clear_user_paused(printer_id)
            self._on_print_end(printer_id, kind, detail, data)
        elif kind == "pause":
            self._on_print_pause(printer_id, detail, data)
        elif kind == "power_loss_confirmed":
            self._confirm_power_loss_candidate(printer_id, data)
        elif kind == "offline":
            # Offline — только кандидат на power loss. Команду resume здесь
            # не отправляем: сеть может восстановиться без отключения света.
            self._mark_power_loss_candidate(printer_id, "connection_lost", data)
        if kind in ("start", "complete", "error", "pause"):
            self._auto_photo(printer_id, kind, detail)
        # Конструктор правил: событие принтера может запустить правила.
        try:
            self.rules.on_print_event(kind, title, detail, printer_id, data)
        except Exception as exc:
            self.db.add_event("error", "Правила: сбой обработки", str(exc), printer_id)
        self._notify(kind, title, detail, printer_id)

    def _on_print_pause(self, printer_id: str, name: str, data: dict) -> None:
        """Событие паузы: проверяем авто-продолжение (Крым / сбой питания)."""
        self.check_auto_resume(printer_id)

    def _auto_photo(self, printer_id: str, kind: str, note: str) -> None:
        """Авто-снимок камеры при событиях печати: кадр прикрепляется к заданию."""
        try:
            printer = self.get(printer_id)
            if not printer or not printer.camera.frame:
                return
            printer.camera.snapshot(note=f"авто: {note}")
            job = self.db.one(
                "SELECT id, order_id FROM print_jobs WHERE printer_id=?"
                " ORDER BY datetime(created_at) DESC LIMIT 1", (printer_id,))
            if not job:
                return
            from .config import PHOTO_DIR
            PHOTO_DIR.mkdir(parents=True, exist_ok=True)
            name = f"job_{job['id']}_{kind}_{int(time.time())}.jpg"
            (PHOTO_DIR / name).write_bytes(printer.camera.frame)
            self.db.execute(
                "INSERT INTO order_photos(id,order_id,at,file,note,kind) VALUES(?,?,?,?,?,?)",
                (uid("ph"), job.get("order_id") or None, now_iso(), name,
                 f"авто-снимок: {note}", "camera"))
        except Exception:
            pass

    def _on_print_start(self, printer_id: str, name: str, data: dict) -> None:
        remote_task_id = str(
            data.get("remote_task_id") or data.get("subtask_id") or data.get("task_id") or ""
        ).strip()
        if remote_task_id == "0":
            remote_task_id = ""
        job = self.db.one(
            "SELECT * FROM print_jobs WHERE printer_id=? AND state='running'", (printer_id,))
        if job:
            # В базе висит «печатается», а принтер начал ДРУГУЮ задачу —
            # старое задание зависло (событие конца потерялось). Закрываем
            # его, иначе новая печать не отслеживается вовсе.
            old = (job.get("name") or job.get("file") or "").strip().lower()
            new = (name or "").strip().lower()
            same = not new or not old or old == new or old in new.rsplit("/", 1)[-1] \
                or new.rsplit("/", 1)[-1].startswith(old[:12])
            if not same:
                self.db.add_event(
                    "job", "Предыдущее задание закрыто",
                    f"«{job.get('name') or 'печать'}» не завершилось по событию — "
                    f"принтер начал «{name}»", printer_id, {"job_id": job["id"]})
                self._finalize_job(job, "cancelled", "replaced",
                                   0.0, num(job.get("grams")), {})
            else:
                return
        job = self.db.one(
            "SELECT * FROM print_jobs WHERE printer_id=? AND state='starting'"
            " ORDER BY datetime(created_at) DESC LIMIT 1", (printer_id,))
        # Восстановление после сбоя питания или перезапуска коннектора:
        # активного running-задания нет (или его закрыли по ошибке), но
        # сама печать та же — тот же remote_task_id или то же имя файла в
        # недавнем закрытом задании. Возьмём привязку к заказу оттуда и
        # переоткроем задание, вместо того чтобы плодить осиротевшие печати.
        if not job:
            job = self._find_recent_job_for_task(printer_id, name, remote_task_id)
            if job:
                self.db.add_event(
                    "job", "Печать восстановлена",
                    f"«{job.get('name') or name or 'печать'}»: продолжаем"
                    " прежнее задание после разрыва / сбоя питания",
                    printer_id,
                    {"job_id": job["id"], "order_id": job.get("order_id") or "",
                     "remote_task_id": remote_task_id})
        order_id = job.get("order_id") if job else self._guess_order(name)
        if job:
            self.db.execute(
                "UPDATE print_jobs SET state='running', started_at=COALESCE(started_at, ?),"
                " name=?, remote_task_id=?, file_version=COALESCE(NULLIF(file_version,''), ?),"
                " finished_at=NULL, result=NULL, accounted_at=NULL,"
                " resume_eligible=1, manual_paused=0, power_loss_at='',"
                " resume_attempts=0, resume_reason='' WHERE id=?",
                (now_iso(), name or job.get("name", ""), remote_task_id, remote_task_id, job["id"]))
        else:
            self.db.upsert("print_jobs", {
                "id": uid("job"), "printer_id": printer_id, "order_id": order_id,
                "name": name, "file": name, "state": "running", "source": "printer",
                "remote_task_id": remote_task_id, "file_version": remote_task_id,
                "started_at": now_iso(), "resume_eligible": 1, "manual_paused": 0,
                "power_loss_at": "", "resume_attempts": 0, "resume_reason": "",
                "created_at": now_iso()})
        if order_id:
            printing = self.db.one("SELECT id FROM statuses WHERE id='printing'")
            if printing:
                self.db.execute("UPDATE orders SET status='printing', updated_at=? WHERE id=?",
                                (now_iso(), order_id))

    def _on_print_end(self, printer_id: str, kind: str, name: str, data: dict) -> None:
        remote_task_id = str(
            data.get("remote_task_id") or data.get("subtask_id") or data.get("task_id") or ""
        ).strip()
        if remote_task_id == "0":
            remote_task_id = ""
        job = self.db.one(
            "SELECT * FROM print_jobs WHERE printer_id=? AND state IN ('running','starting')"
            " ORDER BY datetime(created_at) DESC LIMIT 1", (printer_id,))
        state = {"complete": "done", "error": "failed", "stop": "cancelled"}[kind]
        if not job:
            # При переподключении принтер способен прислать тот же FINISH ещё
            # раз. Сначала используем стабильный task id, а для старых прошивок
            # — короткое окно по имени. Это не даёт создать вторую «печать».
            if remote_task_id:
                duplicate = self.db.one(
                    "SELECT id FROM print_jobs WHERE printer_id=?"
                    " AND state IN ('done','failed','cancelled')"
                    " AND (remote_task_id=? OR (COALESCE(remote_task_id,'')=''"
                    " AND lower(COALESCE(name,file,''))=lower(?)"
                    " AND datetime(finished_at)>=datetime('now','-30 seconds')))"
                    " ORDER BY datetime(finished_at) DESC LIMIT 1",
                    (printer_id, remote_task_id, name or ""),
                )
            else:
                duplicate = self.db.one(
                    "SELECT id FROM print_jobs WHERE printer_id=?"
                    " AND state IN ('done','failed','cancelled')"
                    " AND lower(COALESCE(name,file,''))=lower(?)"
                    " AND datetime(finished_at)>=datetime('now','-30 seconds')"
                    " ORDER BY datetime(finished_at) DESC LIMIT 1",
                    (printer_id, name or ""),
                )
            if duplicate:
                return
        duration = num(data.get("duration_min"))
        grams = num(data.get("weight"))
        if not grams and job and job.get("order_id"):
            order = self.db.one("SELECT grams, qty FROM orders WHERE id=?", (job["order_id"],))
            if order:
                # У мультизаказа grams — вся плита: на количество не умножаем.
                has_items = self.db.one(
                    "SELECT id FROM order_items WHERE order_id=? LIMIT 1",
                    (job["order_id"],)) is not None
                grams = num(order["grams"]) * (1 if has_items
                                               else max(1.0, num(order.get("qty"), 1)))
        if not job:
            job = self.db.upsert("print_jobs", {
                "id": uid("job"), "printer_id": printer_id, "order_id": self._guess_order(name),
                "name": name, "state": state, "source": "printer",
                "remote_task_id": remote_task_id,
                "started_at": now_iso(), "created_at": now_iso()})
        elif remote_task_id and not job.get("remote_task_id"):
            self.db.execute(
                "UPDATE print_jobs SET remote_task_id=? WHERE id=?",
                (remote_task_id, job["id"]),
            )
            job["remote_task_id"] = remote_task_id
        self._finalize_job(job, state, kind, duration, grams, data)
        if kind == "complete":
            self.watch_bed(printer_id)
        self._maybe_start_next(printer_id)

    def _finalize_job(self, job: dict, state: str, kind: str,
                      duration: float, grams: float, data: dict | None = None) -> dict:
        """Атомарно и идемпотентно завершить задание.

        Статус, списание пластика, фактическая себестоимость, статистика,
        наработка и связанные складские операции входят в одну транзакцию.
        ``accounted_at`` является маркером полностью завершённой операции.
        """
        data = data or {}
        job_id = str(job.get("id") or "")
        if not job_id:
            raise ValueError("Не указано задание печати")
        with self.db.transaction():
            stored = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))
            if not stored:
                raise ValueError("Задание печати не найдено")
            # Второй FINISH или повторное согласование после успешного commit.
            if (stored.get("state") in ("done", "failed", "cancelled")
                    and stored.get("accounted_at")):
                return stored

            printer_id = stored.get("printer_id") or ""
            self.db.execute(
                "UPDATE print_jobs SET state=?, finished_at=?, duration_min=?, grams=?,"
                " progress=?, layers=?, result=? WHERE id=?",
                (state, now_iso(), round(duration, 1), round(grams, 1),
                 num(data.get("progress")), int(num(data.get("total_layers"))), kind, job_id))
            job = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,)) or stored
            if state in ("done", "failed"):
                printer = self.get(printer_id)
                if printer and not job.get("spool_id"):
                    snapshot = printer.snapshot()
                    active = next(
                        (tray for tray in snapshot["ams"]["trays"] if tray["active"]), None
                    )
                    if active:
                        spool = self.acc.pick_spool(
                            printer_id, str(active["slot"]), active["type"], active["uuid"]
                        )
                        if spool:
                            self.db.execute(
                                "UPDATE print_jobs SET spool_id=? WHERE id=?",
                                (spool["id"], job_id),
                            )
                            job["spool_id"] = spool["id"]
                self.acc.register_job_costs(job)
                # Наработка принтера — основа напоминаний об обслуживании.
                try:
                    self.guard.add_runtime(printer_id, duration, grams)
                except Exception as exc:
                    self.db.add_event(
                        "error", "Не удалось учесть наработку", str(exc), printer_id
                    )
            else:
                # Для отмены нет расходов, но нужен тот же маркер завершения.
                self.db.execute(
                    "UPDATE print_jobs SET accounted_at=? WHERE id=?",
                    (now_iso(), job_id),
                )

            # Партия печати: годные штуки приходуются на склад, брак идёт в потери.
            if job.get("batch_id") and getattr(self, "batches", None):
                try:
                    self.batches.on_job_finished(job)
                except Exception as exc:
                    self.db.add_event(
                        "error", "Партия: не удалось учесть задание", str(exc),
                        printer_id, {"job_id": job_id},
                    )
            if state == "failed":
                self._register_failure(printer_id, job, duration, grams)
            if job.get("order_id"):
                if state == "done":
                    self.db.execute(
                        "UPDATE orders SET status='post', updated_at=?"
                        " WHERE id=? AND status='printing'",
                        (now_iso(), job["order_id"]),
                    )
                else:
                    # Сорвалась или остановлена: заказ не должен навсегда
                    # остаться в «печати» — возвращаем его в очередь.
                    self._release_order(job["order_id"], kind)
            return self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,)) or job

    def _release_order(self, order_id: str, reason: str = "") -> None:
        """Снять «в печати», если по заказу больше ничего не печатается."""
        if not order_id:
            return
        order = self.db.one("SELECT status FROM orders WHERE id=?", (order_id,))
        if not order or order.get("status") != "printing":
            return
        running = self.db.one(
            "SELECT id FROM print_jobs WHERE order_id=? AND state IN ('running','starting')",
            (order_id,))
        if running:
            return
        target = self.db.one("SELECT id FROM statuses WHERE id='queue' AND is_final=0") \
            or self.db.one("SELECT id FROM statuses WHERE is_final=0 ORDER BY position LIMIT 1")
        if not target:
            return
        self.db.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?",
                        (target["id"], now_iso(), order_id))
        self.db.add_event(
            "order", "Заказ вернулся в очередь",
            "Печать не завершилась" + (f" ({reason})" if reason else "")
            + " — статус «в печати» снят.", "", {"order_id": order_id})

    def _reconcile_printer(self, printer, snap: dict) -> None:
        """Согласовать задания с фактом: принтер свободен, а в базе «печатается».

        Событие окончания может потеряться (перезапуск коннектора, сбой
        питания, остановка с экрана принтера). Без сверки заказ навсегда
        остаётся «в печати», а новое задание не отслеживается — старое
        висит в running. Закрываем такие задания по фактам телеметрии.

        Осторожность важнее скорости: после сбоя питания принтер сначала
        рапортует пустое состояние, а факт печати приходит секунд через 20.
        Если поспешить закрыть задание — теряется привязка к заказу и
        сорванный заказ висит без печати. Поэтому здесь несколько «страховок»:
        свежее подключение не считаем свободным, задачу с тем же именем
        трогать не даём, а FINISH требует ещё и большого прогресса.
        """
        state = snap["printer"].get("state")
        if state not in ("IDLE", "FINISH", "FAILED"):
            return
        # 1) Принтер только что подключился — телеметрия ещё не пришла.
        #    Первый snapshot после сбоя питания часто без gcode_state.
        connected_since = getattr(printer, "connected_since", 0.0)
        if connected_since and (time.time() - connected_since) < 90:
            return
        # 2) Задачу с тем же файлом на принтере не трогаем: принтер, скорее
        #    всего, сам продолжает после сбоя питания (Bambu Power Loss
        #    Recovery) — задание в базе принадлежит именно этой печати.
        current_task = str(snap["printer"].get("task") or "").strip().lower()
        for job in self.db.query(
                "SELECT * FROM print_jobs WHERE printer_id=? AND state IN ('running','starting')",
                (printer.id,)):
            started = 0.0
            try:
                started = datetime.fromisoformat(
                    str(job.get("started_at") or job.get("created_at") or "")
                    .replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                started = time.time()
            age_min = max(0.0, (time.time() - started) / 60)
            # Льготный период: между постановкой и стартом принтер может
            # минуту показывать IDLE — только начатые задания не трогаем.
            if age_min < 3:
                continue
            job_name = str(job.get("name") or job.get("file") or "").strip().lower()
            if current_task and job_name:
                job_tail = job_name.rsplit("/", 1)[-1]
                task_tail = current_task.rsplit("/", 1)[-1]
                if (job_tail == task_tail or job_tail in current_task
                        or (task_tail and task_tail.startswith(job_tail[:12]))):
                    # На принтере то же задание — не закрываем, ждём
                    # события RUNNING/FINISH/FAILED от самого принтера.
                    continue
            progress = num(job.get("progress"))
            # FINISH без прогресса — сомнительный «успех». После сбоя питания
            # принтер иногда мигает FINISH до появления фактических данных
            # прошлой печати; лучше закрыть как «потеряно», чем ошибочно
            # списать пластик и закрыть заказ.
            done = state == "FINISH" and progress >= 90
            note = ("закрыто как выполненное — событие окончания потерялось"
                    if done else "печать не отслежена: принтер свободен, задание висело")
            self.db.add_event(
                "job", "Задание закрыто автоматически",
                f"{job.get('name') or 'печать'} · {note}", printer.id,
                {"job_id": job["id"], "progress": progress})
            self._finalize_job(job, "done" if done else "cancelled",
                               "auto-done" if done else "lost", age_min,
                               num(job.get("grams")) or num(job.get("est_grams")),
                               {"progress": progress})

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

    def _find_recent_job_for_task(self, printer_id: str, name: str,
                                  remote_task_id: str = "") -> dict | None:
        """Найти недавно закрытое задание того же принтера, к которому
        относится начинающаяся печать. Нужно после сбоя питания или
        перезапуска коннектора: печать та же — задание и заказ должны
        остаться прежними, а не создаваться заново.
        """
        cleaned = str(name or "").strip().lower().rsplit("/", 1)[-1]
        # 1) Точное совпадение по remote_task_id — самый надёжный признак.
        if remote_task_id:
            row = self.db.one(
                "SELECT * FROM print_jobs WHERE printer_id=? AND remote_task_id=?"
                " AND datetime(COALESCE(finished_at, created_at))>=datetime('now','-6 hours')"
                " ORDER BY datetime(COALESCE(finished_at, created_at)) DESC LIMIT 1",
                (printer_id, remote_task_id))
            if row:
                return row
        if not cleaned:
            return None
        # 2) Совпадение по имени файла среди недавно «потерянных» / отменённых.
        #    Ищем именно свежие задания (последние 6 часов), чтобы не хватать
        #    старую печать с тем же именем из истории.
        rows = self.db.query(
            "SELECT * FROM print_jobs WHERE printer_id=?"
            " AND state IN ('cancelled','failed','running','starting')"
            " AND datetime(COALESCE(finished_at, created_at))>=datetime('now','-6 hours')"
            " ORDER BY datetime(COALESCE(finished_at, created_at)) DESC LIMIT 20",
            (printer_id,))
        for row in rows:
            old = str(row.get("name") or row.get("file") or "").strip().lower().rsplit("/", 1)[-1]
            if not old:
                continue
            if old == cleaned or old in cleaned or cleaned.startswith(old[:12]):
                # «lost» — задание, которое реконсиляция закрыла зря.
                # Для остальных считаем совпадение только при явной причине
                # (тот же remote_task_id — уже проверили) либо когда старое
                # задание помечено как reprint/lost.
                if row.get("state") in ("running", "starting"):
                    return row
                if str(row.get("result") or "") in ("lost", "replaced"):
                    return row
        return None

    def link_active_to_order(self, printer_id: str, order_id: str) -> dict:
        """Привязать текущую (или последнюю активную) печать принтера к
        указанному заказу. Используется после сбоя питания или ручной
        распечатки, когда PrintFlow не смог сам сопоставить файл с заказом.
        """
        printer = self.get(printer_id)
        if not printer:
            raise ValueError("Принтер не найден")
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            raise ValueError("Заказ не найден")
        # Ищем именно активное задание, а не завершённое: привязка меняет
        # текущий процесс. Если running нет — берём последнее по времени.
        job = self.db.one(
            "SELECT * FROM print_jobs WHERE printer_id=?"
            " AND state IN ('running','starting','queued')"
            " ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'starting' THEN 1 ELSE 2 END,"
            " datetime(created_at) DESC LIMIT 1", (printer_id,))
        if not job:
            # На принтере что-то печатается, но задания в базе нет —
            # событие _on_print_start ещё не пришло. Создадим заглушку по
            # текущей телеметрии, чтобы связка не потерялась.
            snap = printer.snapshot()
            task = snap["printer"].get("task") or ""
            state = snap["printer"].get("state") or "IDLE"
            if not task:
                raise ValueError("На принтере нет активного задания — привязывать нечего")
            job = self.db.upsert("print_jobs", {
                "id": uid("job"), "printer_id": printer_id, "order_id": order_id,
                "name": task, "file": task,
                "state": "running" if state in ("RUNNING", "PREPARE", "PAUSE") else "queued",
                "source": "printer", "started_at": now_iso(), "created_at": now_iso(),
                "progress": num(snap["printer"].get("progress")),
            })
        else:
            self.db.execute("UPDATE print_jobs SET order_id=? WHERE id=?",
                            (order_id, job["id"]))
            job["order_id"] = order_id
        # Переводим заказ в «в печати», если такой статус есть.
        if self.db.one("SELECT id FROM statuses WHERE id='printing'"):
            self.db.execute("UPDATE orders SET status='printing', updated_at=? WHERE id=?",
                            (now_iso(), order_id))
        self.db.add_event(
            "order", "Печать привязана к заказу",
            f"Заказ №{order.get('number')} · {order.get('product') or job.get('name') or ''}",
            printer_id, {"order_id": order_id, "job_id": job["id"]})
        return {"ok": True, "order": self.db.one("SELECT * FROM orders WHERE id=?", (order_id,)),
                "job": self.db.one("SELECT * FROM print_jobs WHERE id=?", (job["id"],))}

    def link_job_to_order(self, job_id: str, order_id: str) -> dict:
        """Привязать конкретное задание печати к существующему заказу."""
        job = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))
        if not job:
            raise ValueError("Задание не найдено")
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            raise ValueError("Заказ не найден")
        self.db.execute("UPDATE print_jobs SET order_id=? WHERE id=?", (order_id, job_id))
        if job.get("state") in ("running", "starting") and \
                self.db.one("SELECT id FROM statuses WHERE id='printing'"):
            self.db.execute("UPDATE orders SET status='printing', updated_at=? WHERE id=?",
                            (now_iso(), order_id))
        self.db.add_event(
            "order", "Задание привязано к заказу",
            f"Заказ №{order.get('number')} · {order.get('product') or job.get('name') or ''}",
            job.get("printer_id") or "", {"order_id": order_id, "job_id": job_id})
        return {"ok": True, "order": self.db.one("SELECT * FROM orders WHERE id=?", (order_id,)),
                "job": self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))}

    # ----------------------------------------------------------------- очередь
    def queue(self) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM print_jobs WHERE state IN ('queued','uploading','starting','running')"
            " ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'starting' THEN 1"
            " WHEN 'uploading' THEN 2 ELSE 3 END,"
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
        file_value = str(data.get("file") or "").strip()
        job = {
            "id": uid("job"),
            "printer_id": data.get("printer_id") or None,
            "order_id": data.get("order_id") or None,
            "name": data.get("name") or file_value.rsplit("/", 1)[-1],
            "file": file_value,
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
            "est_minutes": max(0.0, num(data.get("est_minutes"))),
            "est_grams": max(0.0, num(data.get("est_grams"))),
            "queued_at": now_iso(), "created_at": now_iso(),
        }
        # оценка печати до запуска: время и граммы из файла
        estimate = {}
        try:
            from .config import UPLOAD_DIR
            from .estimate import estimate_file
            local = UPLOAD_DIR / (job.get("file") or "").rsplit("/", 1)[-1]
            if local.exists():
                estimate = estimate_file(local)
                # Многоплитные проекты: сумма по всем плитам, а не первая плита.
                job["est_minutes"] = (num(estimate.get("total_minutes"))
                                      or num(estimate.get("minutes")))
                job["est_grams"] = (num(estimate.get("total_grams"))
                                    or num(estimate.get("grams")))
        except Exception:
            pass
        # 3MF/G-code автозаполняет заказ: вес, время, материал и цвет, если пустые.
        if job.get("order_id") and estimate:
            order = self.db.one("SELECT * FROM orders WHERE id=?", (job["order_id"],))
            if order:
                total_g = num(estimate.get("total_grams")) or num(estimate.get("grams"))
                total_m = num(estimate.get("total_minutes")) or num(estimate.get("minutes"))
                fill = {
                    "grams": total_g,
                    "hours": round(total_m / 60.0, 2),
                    "material": estimate.get("material") or "",
                    "color": estimate.get("color") or "",
                }
                # Если файл привязали к заказу из окна очереди, карточка
                # заказа тоже должна знать о нём — иначе планировщик продолжит
                # показывать ложный блокер «нет файла».
                if not str(order.get("file") or "").strip() and file_value:
                    fill["file"] = file_value
                sets, params = [], []
                for field, value in fill.items():
                    if value and not str(order.get(field) or "").strip():
                        sets.append(f"{field}=?")
                        params.append(value if field in ("material", "color") else num(value))
                if sets:
                    self.db.execute(
                        f"UPDATE orders SET {', '.join(sets)}, updated_at=? WHERE id=?",
                        (*params, now_iso(), job["order_id"]))
        # Связь файла не должна зависеть от того, удалось ли разобрать смету.
        # Даже «пустой» G-code остаётся файлом заказа, а не осиротевшей печатью.
        if job.get("order_id") and file_value:
            self.db.execute(
                "UPDATE orders SET file=?, updated_at=? WHERE id=?"
                " AND COALESCE(file,'')=''",
                (file_value, now_iso(), job["order_id"]))
        row = self.db.upsert("print_jobs", job)
        self.db.add_event("queue", "Задание добавлено в очередь", job["name"],
                          job["printer_id"] or "", {"job_id": job["id"]})
        if (self.db.setting("auto_queue", False)
                and self.db.setting("unattended_dangerous_actions", False)
                and data.get("allow_auto_start", True)):
            self._maybe_start_next(job["printer_id"] or "")
        # Автозапуск может синхронно перевести это задание в starting/running.
        # Не возвращаем устаревшую копию «queued» — UI должен показывать факт.
        return self.db.one("SELECT * FROM print_jobs WHERE id=?", (job["id"],)) or row

    def cancel_job(self, job_id: str) -> dict:
        job = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))
        if not job:
            raise ValueError("Задание не найдено")
        if job.get("state") in ("done", "failed", "cancelled"):
            return job  # повторный клик не меняет историю и не освобождает её повторно
        if job.get("state") == "running":
            printer = self.get(job["printer_id"] or "")
            if printer:
                printer.command("stop")
            # Событие STOP может прийти синхронно или из другого потока.
            # Не затираем его результат отменой после успешной финализации.
            current = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))
            if current and current.get("state") not in ("running", "starting"):
                return current
        self.db.execute(
            "UPDATE print_jobs SET state='cancelled', finished_at=?, accounted_at=?"
            " WHERE id=? AND state IN ('queued','uploading','starting','running')",
            (now_iso(), now_iso(), job_id),
        )
        # Заказ не должен остаться «в печати» после отмены задания вручную.
        self._release_order(job.get("order_id") or "", "задание отменено")
        return self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,)) or {}

    def _local_job_file(self, filename: str) -> Path | None:
        """Локальная копия задания, если она загружена в uploads."""
        name = Path(str(filename or "").replace("\\", "/")).name
        if not name:
            return None
        candidate = (UPLOAD_DIR / name).resolve()
        try:
            candidate.relative_to(UPLOAD_DIR.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def start_job(self, job_id: str, printer_id: str = "") -> dict:
        job = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))
        if not job:
            raise ValueError("Задание не найдено")
        if job.get("state") != "queued":
            raise ValueError("Запустить можно только задание из очереди")
        printer = self.get(printer_id or job.get("printer_id") or "")
        if not printer:
            raise ValueError("Нет доступного принтера")
        if not job.get("file"):
            raise ValueError("В задании не указан файл")
        try:
            mapping = json.loads(job.get("ams_mapping") or "[]")
        except (json.JSONDecodeError, TypeError):
            mapping = []

        local = self._local_job_file(job["file"])
        remote_name = Path(str(job["file"]).replace("\\", "/")).name or job["file"]
        # Большой файл может грузиться дольше трёх минут. Отдельное состояние
        # uploading не даёт reconcile принять принтер за свободный и отменить
        # реальное задание, пока SD ещё получает модель.
        claim_state = "uploading" if local is not None else "starting"
        claimed = self.db.execute(
            f"UPDATE print_jobs SET state=?, printer_id=?, started_at=?,"
            " resume_eligible=1, manual_paused=0, power_loss_at='',"
            " resume_attempts=0, resume_reason='' WHERE id=?"
            " AND state='queued'", (claim_state, printer.id, now_iso(), job_id))
        if claimed.rowcount != 1:
            raise ValueError("Задание уже запускается или не стоит в очереди")
        try:
            if printer.mode == "cloud" and local is not None:
                # Облачный запуск сам использует uploads и не требует FTPS.
                self.start_print_cloud(printer, local.name,
                                       plate=int(num(job.get("plate"), 1) or 1),
                                       use_ams=bool(job.get("use_ams", 1)),
                                       ams_mapping=mapping,
                                       bed_level=bool(job.get("bed_level", 1)),
                                       flow_cali=bool(job.get("flow_cali")),
                                       timelapse=bool(job.get("timelapse")))
                transitioned = self.db.execute(
                    "UPDATE print_jobs SET state='starting' WHERE id=?"
                    " AND state='uploading'", (job_id,))
                if transitioned.rowcount != 1:
                    raise ValueError("Задание отменено во время облачной загрузки")
            else:
                if local is not None:
                    # Файл с компьютера становится доступным принтеру только
                    # после явной загрузки на его SD-карту.
                    printer.files.upload(local, remote_name)
                elif printer.mode == "cloud" and not printer.record.get("host"):
                    raise FileNotFoundError(
                        "Облачному принтеру нужна локальная копия файла. "
                        "Загрузите файл с компьютера в очередь заново.")
                if local is not None:
                    # MQTT START должен видеть уже starting, а не uploading:
                    # иначе обработчик не найдёт задание для восстановления.
                    transitioned = self.db.execute(
                        "UPDATE print_jobs SET state='starting' WHERE id=?"
                        " AND state='uploading'", (job_id,))
                    if transitioned.rowcount != 1:
                        raise ValueError("Задание отменено во время загрузки файла")
                printer.start_print(remote_name, plate=int(num(job.get("plate"), 1) or 1),
                                    use_ams=bool(job.get("use_ams", 1)), ams_mapping=mapping,
                                    bed_level=bool(job.get("bed_level", 1)),
                                    flow_cali=bool(job.get("flow_cali")),
                                    timelapse=bool(job.get("timelapse")),
                                    subtask_name=job.get("name", ""))
        except Exception:
            # Ошибка загрузки/старта не должна оставлять задание в процессе.
            self.db.execute(
                "UPDATE print_jobs SET state='queued', started_at=NULL, resume_eligible=0,"
                "manual_paused=0, power_loss_at='', resume_attempts=0, resume_reason='' WHERE id=?"
                " AND state IN ('uploading','starting')", (job_id,))
            raise
        return self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,)) or {}

    # ----------------------------------------------------- облачный запуск
    @staticmethod
    def cloud_manifest_path(name: str) -> Path:
        return UPLOAD_DIR / f"{name}.cloud.json"

    def cloud_manifest(self, name: str) -> dict:
        """Сохранённый манифест облачной заливки, если файл не менялся."""
        try:
            manifest = json.loads(self.cloud_manifest_path(name).read_text("utf-8"))
        except Exception:
            return {}
        local = UPLOAD_DIR / name
        if not local.exists():
            return {}
        from . import bambu_cloud
        if manifest.get("md5") != bambu_cloud.md5_hex_upper(local.read_bytes()):
            return {}
        return manifest

    def start_print_cloud(self, printer: BambuPrinter, filename: str, plate: int = 1,
                          use_ams: bool = True, ams_mapping: list[int] | None = None,
                          bed_level: bool = True, flow_cali: bool = False,
                          timelapse: bool = False,
                          progress=None) -> dict:
        """Запуск печати через Bambu Cloud (без LAN Only Mode).

        Основной путь — облачная заливка + POST /my/task (как Bambu Studio).
        Фолбэки: 1) FTPS на SD + /my/task с mode=lan_file (если LAN доступен);
        2) для .gcode — FTPS в cache/ + MQTT-команда gcode_file.
        """
        from . import bambu_cloud
        creds = self._cloud_creds()
        token, uid, region = creds["token"], creds["uid"], creds["region"]
        if not token or not uid:
            raise ValueError("Не выполнен вход в Bambu Cloud (Настройки → Bambu Cloud)")
        name = (filename or "").rsplit("/", 1)[-1]
        local = UPLOAD_DIR / name
        if not local.exists():
            raise FileNotFoundError(f"Файл не найден локально: {name}")
        device_id = printer.record.get("serial", "")
        if not device_id:
            raise ValueError("У принтера не задан серийный номер")
        manifest = self.cloud_manifest(name)
        try:
            if manifest:
                result = bambu_cloud.create_task(
                    token, uid, region, manifest, device_id, plate=plate,
                    use_ams=use_ams, ams_mapping=ams_mapping,
                    bed_level=bed_level, flow_cali=flow_cali,
                    vibration_cali=True, layer_inspect=True, timelapse=timelapse,
                    mode="cloud_file")
                result.update({"name": name, "cloud": True})
                return result
            result = bambu_cloud.upload_and_dispatch(
                local, token, uid, region, device_id, plate=plate,
                use_ams=use_ams, ams_mapping=ams_mapping,
                bed_level=bed_level, flow_cali=flow_cali,
                vibration_cali=True, layer_inspect=True, timelapse=timelapse,
                progress=progress)
            manifest = {k: result.get(k) for k in
                        ("project_id", "model_id", "profile_id", "url", "md5", "name", "bytes")}
            manifest["at"] = now_iso()
            try:
                self.cloud_manifest_path(name).write_text(
                    json.dumps(manifest), encoding="utf-8")
            except OSError:
                pass
            result.update({"name": name, "cloud": True})
            return result
        except bambu_cloud.CloudError as exc:
            lan_ok = bool(printer.record.get("host") and printer.record.get("access_code"))
            if not lan_ok:
                raise ConnectionError(
                    f"{exc}. Локальная сеть не настроена — принтер не сможет принять файл.") from None
            # Фолбэк 1: FTPS на SD + диспетчеризация через /my/task (lan_file).
            self.db.add_event("cloud", "Облачная заливка не удалась — пробуем по локальной сети",
                              str(exc), printer.id, {"file": name})
            try:
                printer.files.upload(local, name)
                manifest = bambu_cloud.upload_project(local, token, uid, region)
                bambu_cloud.patch_project_url(token, uid, region, manifest,
                                              f"ftp:///{name}")
                result = bambu_cloud.create_task(
                    token, uid, region, manifest, device_id, plate=plate,
                    use_ams=use_ams, ams_mapping=ams_mapping,
                    bed_level=bed_level, flow_cali=flow_cali,
                    vibration_cali=True, layer_inspect=True, timelapse=timelapse,
                    mode="lan_file")
                result.update({"name": name, "cloud": False, "hybrid": True})
                return result
            except Exception as exc2:
                if name.lower().endswith(".gcode"):
                    # Фолбэк 2: G-code через cache/ + MQTT gcode_file.
                    try:
                        printer.files.upload(local, f"cache/{name}")
                        printer.command("print_gcode", f"cache/{name}")
                        return {"ok": True, "name": name, "gcode": True}
                    except Exception as exc3:
                        raise ConnectionError(
                            f"Облачная печать не удалась ({exc}), локальная тоже "
                            f"({exc2}); G-code: {exc3}") from None
                raise ConnectionError(
                    f"Облачная печать не удалась: {exc}. По локальной сети: {exc2}."
                    f" Можно экспортировать файл как .gcode и повторить.") from None

    def sync_cloud_history(self, printer_id: str) -> dict:
        """Дополнить журнал печатями из облачной истории Bambu.

        Ловит печати, запущенные из Bambu Handy / Studio: PrintFlow видит
        их событиями MQTT, но фактические граммы/минуты/фото дозаписывает
        из облака (startTime/endTime/weight/cover).
        """
        printer = self.get(printer_id)
        if not printer or printer.mode != "cloud":
            return {"ok": False, "error": "Принтер не в облачном режиме"}
        creds = self._cloud_creds()
        if not creds["token"] or not creds["uid"]:
            return {"ok": False, "error": "Не выполнен вход в Bambu Cloud"}
        from . import bambu_cloud
        from datetime import datetime
        try:
            tasks = bambu_cloud.get_tasks(creds["token"], creds["region"],
                                          printer.record.get("serial", ""), 20)
        except bambu_cloud.CloudError as exc:
            return {"ok": False, "error": str(exc)}

        def parse_ts(value) -> float:
            if not value:
                return 0.0
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                return 0.0

        added = 0
        for task in tasks:
            state = {2: "done", 3: "failed"}.get(int(task.get("status") or 0))
            if not state:
                continue
            name = str(task.get("title") or "").strip()
            started_ts = parse_ts(task.get("startTime"))
            if not name or not started_ts:
                continue
            dupe = False
            for job in self.db.query("SELECT started_at FROM print_jobs WHERE name=?",
                                     (name,)):
                if abs(parse_ts(job.get("started_at")) - started_ts) < 300:
                    dupe = True
                    break
            if dupe:
                continue
            end_ts = parse_ts(task.get("endTime")) or started_ts
            duration = max(0.0, round((end_ts - started_ts) / 60, 1))
            grams = round(sum(float(x.get("weight") or 0)
                              for x in (task.get("amsDetailMapping") or [])), 1)
            started_iso = datetime.fromtimestamp(started_ts).astimezone().isoformat(
                timespec="seconds")
            finished_iso = datetime.fromtimestamp(end_ts).astimezone().isoformat(
                timespec="seconds")
            order_id = self._guess_order(name)
            job = self.db.upsert("print_jobs", {
                "id": uid("job"), "printer_id": printer_id, "order_id": order_id,
                "name": name, "file": name, "state": state, "source": "cloud",
                "started_at": started_iso, "finished_at": finished_iso,
                "duration_min": duration, "grams": grams,
                "created_at": now_iso(),
            })
            added += 1
            # Обложка из облачной истории — фото результата к заказу.
            cover = str(task.get("cover") or "")
            if cover.startswith("http") and order_id:
                try:
                    from .config import PHOTO_DIR
                    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
                    photo_name = f"job_{job['id']}_cloud.jpg"
                    req = __import__("urllib.request", fromlist=["Request"]).Request(
                        cover, headers={"User-Agent": "Mozilla/5.0"})
                    with __import__("urllib.request", fromlist=["urlopen"]).urlopen(
                            req, timeout=20) as response:
                        (PHOTO_DIR / photo_name).write_bytes(response.read())
                    self.db.execute(
                        "INSERT INTO order_photos(id,order_id,at,file,note,kind)"
                        " VALUES(?,?,?,?,?,?)",
                        (uid("ph"), order_id, now_iso(), photo_name,
                         "обложка из облачной истории", "cloud"))
                except Exception:
                    pass
        return {"ok": True, "added": added}

    def maybe_sync_cloud_history(self) -> None:
        """Фоновая сверка с облачной историей — не чаще раза в N минут."""
        if not self.db.setting("cloud_history_sync", True):
            return
        try:
            interval = max(1.0, float(self.db.setting("cloud_sync_minutes", 5.0)))
        except (TypeError, ValueError):
            interval = 5.0
        now = time.time()
        with self.lock:
            printers = [p for p in self.printers.values() if p.mode == "cloud"]
        for printer in printers:
            if now - self._last_cloud_sync.get(printer.id, 0) < interval * 60:
                continue
            self._last_cloud_sync[printer.id] = now
            try:
                result = self.sync_cloud_history(printer.id)
                if result.get("added"):
                    self.db.add_event("cloud", "История Bambu Cloud",
                                      f"Добавлено записей в журнал: {result['added']}",
                                      printer.id, {})
            except Exception as exc:
                self.db.add_event("error", "Сбой сверки с облачной историей",
                                  str(exc), printer.id)

    # ------------------------------------------------------- умная очередь
    def _watch_firmware(self, printer: BambuPrinter, snap: dict) -> None:
        """Контроль прошивки (B.1.3): заметить обновление и сообщить о нём."""
        firmware = str(snap["printer"].get("firmware") or "")
        if not firmware or firmware == str(printer.record.get("firmware") or ""):
            return
        old = str(printer.record.get("firmware") or "")
        self.repo.save_printer({"id": printer.id, "firmware": firmware})
        printer.record["firmware"] = firmware
        self.db.add_event("printer", "Прошивка обновлена",
                          f"{printer.record.get('name') or 'Принтер'}: "
                          f"{old or '—'} → {firmware}",
                          printer.id, {"old": old, "new": firmware})
        if old and self.db.setting("notify_firmware", True):
            self.notify_async(
                f"PrintFlow · {printer.record.get('name') or 'Принтер'}\n"
                f"Прошивка обновлена: {old} → {firmware}")

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

    def _material_matches(self, job: dict, snap: dict, force: bool = False) -> tuple[bool, str]:
        """Совпадает ли материал задания с материалом в активном слоте AMS.

        Печать PETG, когда в слоте PLA, даст брак (температуры не подходят) —
        лучше не начинать вовсе.
        """
        if not force and not self.db.setting("queue_check_material", True):
            return True, ""
        need = self._job_material(job)
        if not need:
            return (False, "Материал задания не сохранён") if force else (True, "")
        active = next((t for t in snap["ams"].get("trays", []) if t.get("active")), None)
        if not active:
            return (False, "Активный слот AMS не подтверждён") if force else (True, "")
        loaded = str(active.get("type") or "").upper()
        if not loaded and force:
            return False, "Материал активного слота AMS неизвестен"
        if loaded and loaded != need:
            return False, (f"Задание требует {need}, а в активном слоте {loaded}. "
                           f"Поставьте {need} в слот {active.get('label', '')}.")
        return True, ""

    def _enough_filament(self, job: dict, snap: dict, force: bool = False) -> tuple[bool, str]:
        """Хватит ли пластика в активном слоте на это задание."""
        if not force and not self.db.setting("queue_check_filament", True):
            return True, ""
        need = num(job.get("est_grams"))  # оценка слайсера — самый точный план
        if not need and job.get("order_id"):
            order = self.db.one("SELECT grams, qty FROM orders WHERE id=?", (job["order_id"],))
            if order:
                # У мультизаказа grams — вся плита, qty — сумма единиц позиций.
                has_items = self.db.one(
                    "SELECT id FROM order_items WHERE order_id=? LIMIT 1",
                    (job["order_id"],)) is not None
                need = num(order.get("grams")) * (1 if has_items
                                                  else max(1.0, num(order.get("qty"), 1)))
        if not need:
            return (False, "Расход пластика задания не сохранён") if force else (True, "")
        active = next((t for t in snap["ams"].get("trays", []) if t.get("active")), None)
        if not active:
            return (False, "Активный слот AMS не подтверждён") if force else (True, "")
        spool = self.acc.pick_spool(job.get("printer_id") or "", str(active.get("slot")),
                                    active.get("type"), active.get("uuid"))
        if not spool:
            return (False, "Катушка AMS не найдена в проверенном складе") if force else (True, "")
        if not int(num(spool.get("verified"), 1)):
            return False, "Катушка из AMS не подтверждена оператором в карточке склада"
        left = num(spool.get("remaining_grams"))
        if left and left < need:
            return False, (f"Нужно {need:.0f} г, в катушке «{spool.get('name', '')}» "
                           f"осталось {left:.0f} г")
        return True, ""

    def next_job(self, printer_id: str, snap: dict | None = None) -> dict | None:
        """Выбрать следующее задание с учётом материала в AMS.

        Ночная смена (9.3.1): при включённом ``night_shift_enabled`` в тихие
        часы вперёд идут самые длинные задания внутри ручного приоритета —
        принтер работает до утра; днём сначала учитывается приоритет, затем срок.
        """
        jobs = self.db.query(
            "SELECT j.*, o.hours AS order_hours, o.due AS due"
            " FROM print_jobs j LEFT JOIN orders o ON o.id=j.order_id"
            " WHERE j.state='queued' AND (j.printer_id IS NULL OR j.printer_id=?)"
            " AND j.file<>'' AND COALESCE(j.source,'')"
            " NOT IN ('order-prepared','defect-recovery','reprint-confirmed')", (printer_id,))
        if not jobs:
            return None
        night = bool(self.db.setting("night_shift_enabled", True)) and self.quiet_now()
        if night:
            # Ручной приоритет — первый ключ: кнопки «выше/ниже» и экран
            # очереди должны менять именно фактический порядок запуска. Внутри
            # одного приоритета ночью выгоднее закрывать длинные задания.
            jobs.sort(key=lambda j: (-int(num(j.get("priority"))),
                                     -num(j.get("order_hours")),
                                     str(j.get("created_at") or "")))
        else:
            # Дедлайн уточняет порядок только внутри одного приоритета. Раньше
            # экран показывал priority DESC, а диспетчер запускал сначала
            # более ранний due — пользователь видел противоречивый порядок.
            jobs.sort(key=lambda j: (-int(num(j.get("priority"))),
                                     str(j.get("due") or "9999-12-31"),
                                     str(j.get("created_at") or "")))
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
        if (not self.db.setting("auto_queue", False)
                or not self.db.setting("unattended_dangerous_actions", False)):
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
        ok, reason = self._material_matches(job, snap)
        if not ok:
            self.db.add_event("queue", "Автозапуск отложен: не тот материал",
                              reason, printer_id, {"job_id": job["id"]})
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

    def reprint_job(
        self,
        job_id: str,
        printer_id: str = "",
        *,
        confirmed: bool = False,
        request_id: str = "",
        defect_id: str = "",
    ) -> dict:
        """Идемпотентно подготовить подтверждённый повтор, но не запускать его.

        Сорванное задание можно повторить только после фиксации причины брака.
        Источник очереди исключён из автостарта: физический запуск остаётся
        отдельным действием оператора.
        """
        if not confirmed:
            raise ValueError("Подтвердите подготовку повторной печати")
        request_id = str(request_id or "").strip()[:120]
        if not request_id:
            raise ValueError("Не указан ключ операции повтора")
        with self.db.transaction():
            by_request = self.db.one(
                "SELECT * FROM print_jobs WHERE reprint_request_id=?", (request_id,)
            )
            if by_request:
                if by_request.get("reprint_of_job_id") != job_id:
                    raise ValueError("Ключ операции уже использован для другого повтора")
                by_request["already_prepared"] = True
                return by_request

            job = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))
            if not job:
                raise ValueError("Задание не найдено")
            if job.get("state") not in ("failed", "done"):
                raise ValueError("Повтор доступен только после завершения печати")
            if not str(job.get("file") or "").strip():
                raise ValueError("У задания нет файла для повторной печати")

            defect = None
            if defect_id:
                defect = self.db.one(
                    "SELECT * FROM defects WHERE id=? AND job_id=? AND confirmed_at<>''",
                    (defect_id, job_id),
                )
                if not defect:
                    raise ValueError("Подтверждённый разбор брака не найден")
            elif job.get("state") == "failed":
                defect = self.db.one(
                    "SELECT * FROM defects WHERE job_id=? AND confirmed_at<>''"
                    " ORDER BY datetime(confirmed_at) DESC LIMIT 1", (job_id,)
                )
                if not defect:
                    raise ValueError("Сначала подтвердите причину брака")
                defect_id = defect["id"]

            existing = self.db.one(
                "SELECT * FROM print_jobs WHERE reprint_of_job_id=?", (job_id,)
            )
            if existing:
                existing["already_prepared"] = True
                return existing

            clone = dict(job)
            for key in (
                "started_at", "finished_at", "duration_min", "grams", "progress",
                "layers", "result", "error", "cost", "energy_kwh", "batch_id",
                "batch_qty", "remote_task_id", "accounted_at", "reprint_of_job_id",
                "reprint_request_id", "defect_id",
            ):
                clone.pop(key, None)
            clone["id"] = uid("job")
            clone["state"] = "queued"
            clone["source"] = "defect-recovery" if defect_id else "reprint-confirmed"
            clone["reprint_of_job_id"] = job_id
            clone["reprint_request_id"] = request_id
            clone["defect_id"] = defect_id
            clone["queued_at"] = now_iso()
            clone["created_at"] = now_iso()
            if printer_id:
                clone["printer_id"] = printer_id
            base = (job.get("name") or "").rstrip()
            if base.endswith(" (повтор)"):
                base = base[: -len(" (повтор)")]
            clone["name"] = base + " (повтор)"
            row = self.db.upsert("print_jobs", clone)
            if defect_id:
                self.db.execute(
                    "UPDATE defects SET reprint_requested=1,reprint_job_id=? WHERE id=?",
                    (row["id"], defect_id),
                )
            self.db.add_event(
                "queue", "Повтор подготовлен — нужен ручной запуск", row["name"],
                clone["printer_id"] or "",
                {"job_id": row["id"], "source_job_id": job_id,
                 "defect_id": defect_id, "auto_start": False},
            )
            row["already_prepared"] = False
            return row

    def reprint_last_failed(
        self,
        order_number: str = "",
        *,
        confirmed: bool = False,
        request_id: str = "",
    ) -> dict:
        """Подготовить повтор последнего сорванного задания после разбора причины."""
        if order_number:
            rows = self.db.query(
                "SELECT j.* FROM print_jobs j JOIN orders o ON o.id=j.order_id"
                " WHERE j.state='failed' AND o.number=? ORDER BY datetime(j.finished_at) DESC LIMIT 1",
                (order_number,))
        else:
            rows = self.db.query(
                "SELECT * FROM print_jobs WHERE state='failed'"
                " ORDER BY datetime(finished_at) DESC LIMIT 1")
        if not rows:
            raise ValueError("Нет сорванных заданий для повтора")
        return self.reprint_job(
            rows[0]["id"], confirmed=confirmed, request_id=request_id
        )

    # ------------------------------------------------------------- преобразование печати в заказ
    def _slicer_estimate(self, printer, task: str) -> dict:
        """Оценка веса и времени из файла печати, без выдуманных значений.

        Порядок источников:
        1) локальная копия файла (uploads и папка наблюдения);
        2) шапка G-code прямо с SD-карты принтера по FTPS — для .gcode;
           для .3mf — скачиваем файл целиком (если <100МБ) и парсим slice_info.config;
        3) телеметрия принтера (print_weight) если принтер печатает этот файл;
        4) ничего не нашли — {grams: 0, minutes: 0, source: ""}, заказ создаётся
           с честным нулём и пометкой, а не с магическим «30 г».

        Вернуть: {"grams": float, "minutes": float, "material": str, "color": str, "source": str}
        """
        from .config import UPLOAD_DIR
        from .estimate import estimate_file, _parse_gcode_head

        name = (task or "").rsplit("/", 1)[-1].strip()
        if not name:
            return {"grams": 0.0, "minutes": 0.0, "material": "", "color": "", "source": ""}
        # 1) локальная копия
        candidates = [UPLOAD_DIR / name]
        watch = str(self.db.setting("watch_folder_path", "") or "").strip()
        if watch:
            candidates.append(Path(watch).expanduser() / name)
        # также пробуем найти файл по подстроке: имя на принтере может быть
        # "model @ 0.2mm layer, 3 walls, 15% infill" а локально "model.3mf"
        # — ищем в UPLOAD_DIR все файлы, чьё имя содержится в task или наоборот
        extra_candidates = []
        try:
            base_lower = name.lower()
            for p in UPLOAD_DIR.glob("*.3mf"):
                if p.name.lower() in base_lower or base_lower in p.name.lower() or p.stem.lower() in base_lower:
                    extra_candidates.append(p)
            for p in UPLOAD_DIR.glob("*.gcode"):
                if p.name.lower() in base_lower or base_lower in p.name.lower() or p.stem.lower() in base_lower:
                    extra_candidates.append(p)
        except Exception:
            pass

        for local in candidates + extra_candidates:
            try:
                if local.exists():
                    est = estimate_file(local)
                    total_g = num(est.get("total_grams")) or num(est.get("grams"))
                    total_m = num(est.get("total_minutes")) or num(est.get("minutes"))
                    if total_g or total_m:
                        return {"grams": total_g, "minutes": total_m,
                                "material": est.get("material", ""),
                                "color": est.get("color", ""), "source": "file"}
                    if est.get("material") or est.get("color"):
                        return {"grams": 0.0, "minutes": 0.0,
                                "material": est.get("material", ""),
                                "color": est.get("color", ""), "source": "file"}
            except Exception:
                continue

        # 2) попытка с SD-карты принтера
        if printer is not None:
            try:
                # G-code — читаем шапку
                if name.lower().endswith(".gcode"):
                    head = printer.files.read_head("/" + name, max_bytes=131072)
                    if head:
                        est = _parse_gcode_head(head.decode("utf-8", "ignore"))
                        if est.get("grams") or est.get("minutes"):
                            est["source"] = "sd"
                            return est
                        if est.get("material") or est.get("color"):
                            return {"grams": 0.0, "minutes": 0.0,
                                    "material": est.get("material", ""),
                                    "color": est.get("color", ""), "source": "sd"}
                # 3MF — пробуем скачать целиком (до 100МБ) и распарсить
                elif name.lower().endswith(".3mf"):
                    # ограничение 100МБ чтобы не забивать память
                    data = printer.files.download("/" + name, max_bytes=100 * 1024 * 1024)
                    if data and len(data) > 100:
                        import tempfile
                        # записать во временный файл и вызвать estimate_3mf
                        with tempfile.NamedTemporaryFile(suffix=".3mf", delete=False) as tf:
                            tf.write(data)
                            tmp_path = Path(tf.name)
                        try:
                            est = estimate_file(tmp_path)
                            total_g = num(est.get("total_grams")) or num(est.get("grams"))
                            total_m = num(est.get("total_minutes")) or num(est.get("minutes"))
                            if total_g or total_m:
                                return {"grams": total_g, "minutes": total_m,
                                        "material": est.get("material", ""),
                                        "color": est.get("color", ""), "source": "sd-3mf"}
                            if est.get("material") or est.get("color"):
                                return {"grams": 0.0, "minutes": 0.0,
                                        "material": est.get("material", ""),
                                        "color": est.get("color", ""), "source": "sd-3mf"}
                        finally:
                            try:
                                tmp_path.unlink()
                            except Exception:
                                pass
            except Exception:
                pass

            # 3) телеметрия принтера как фолбэк. print_weight — полный вес плиты
            # только после FINISH: во время печати это частичный расход, и его
            # нельзя выдавать за вес изделия (иначе в заказ молча уезжает
            # «35 г» вместо честного нуля с пометкой). Время печати
            # (elapsed+remaining) можно использовать в любом состоянии.
            try:
                snap = printer.snapshot()
                task_snap = str(snap["printer"].get("task") or "").lower()
                # только если задача совпадает с запрашиваемой (или запрашиваемая — часть снапшота)
                if name.lower() in task_snap or task_snap in name.lower():
                    state = str(snap["printer"].get("state") or "")
                    w = num(snap["printer"].get("weight"))
                    if state != "FINISH":
                        w = 0.0
                    elapsed = num(snap["printer"].get("elapsed_min"))
                    remaining = num(snap["printer"].get("remaining_min"))
                    minutes = elapsed + remaining if (elapsed or remaining) else 0.0
                    if w or minutes:
                        return {"grams": w, "minutes": minutes,
                                "material": "", "color": "", "source": "printer"}
            except Exception:
                pass

        return {"grams": 0.0, "minutes": 0.0, "material": "", "color": "", "source": ""}

    def _find_remote_print_file(self, printer, name: str) -> str:
        """Путь к 3MF/G-code на SD: точное имя, корень, cache/, поиск по списку."""
        clean = Path(name or "").name.strip()
        if not clean:
            return ""
        low = clean.lower()
        guessed = clean
        try:
            entries = printer.files.list_files("/")
        except Exception:
            entries = []
        matches: list[str] = []
        dirs: list[str] = []
        for item in entries or []:
            if item.get("dir"):
                dirs.append(str(item.get("path") or item.get("name") or ""))
                continue
            fn = str(item.get("name") or "")
            path = str(item.get("path") or ("/" + fn))
            fn_low = fn.lower()
            if fn_low == low or fn_low.endswith(low) or low in fn_low:
                matches.append(path)
        if not matches:
            for folder in dirs[:8]:
                try:
                    nested = printer.files.list_files(folder)
                except Exception:
                    nested = []
                for item in nested or []:
                    if item.get("dir"):
                        continue
                    fn = str(item.get("name") or "")
                    if fn.lower() == low or low in fn.lower():
                        matches.append(str(item.get("path") or fn))
                        break
                if matches:
                    break
        return matches[0] if matches else guessed

    def pull_print_file(self, printer_id: str = "", filename: str = "",
                        save_dir: Path | None = None) -> dict:
        """Скачать файл печати с принтера в uploads и прочитать вес/время слайсера.

        Нужен LAN/FTPS (IP + Access Code). 3MF качается целиком (до 100 МБ),
        у G-code достаточно шапки. Повторный вызов использует уже скачанную копию.
        """
        from .config import UPLOAD_DIR, ensure_dirs
        from .estimate import estimate_file

        printer = self.get(printer_id)
        if not printer:
            raise ValueError("Принтер не найден")
        snap = printer.snapshot()
        task = (filename or "").strip() or str(snap["printer"].get("task") or "")
        name = Path(task).name.strip()
        if not name:
            raise ValueError("Нет имени файла: укажите «Файл на принтере» или дождитесь печати")
        ensure_dirs()
        dest = Path(save_dir) if save_dir else UPLOAD_DIR
        dest.mkdir(parents=True, exist_ok=True)
        local = dest / name
        source = "uploads"
        if not local.exists() or local.stat().st_size < 80:
            if not printer.record.get("host") or not printer.record.get("access_code"):
                raise ValueError("Чтобы скачать файл с принтера, укажите IP и Access Code в карточке принтера")
            remote = self._find_remote_print_file(printer, name)
            is_plain_gcode = name.lower().endswith(".gcode") and not name.lower().endswith(".gcode.3mf")
            data = b""
            try:
                if is_plain_gcode and hasattr(printer.files, "read_head"):
                    data = printer.files.read_head(remote, max_bytes=400_000) or b""
                if (not data or len(data) < 80) and hasattr(printer.files, "download"):
                    data = printer.files.download(remote, max_bytes=100 * 1024 * 1024) or b""
            except Exception as exc:
                raise ValueError(f"Не удалось скачать {name} с принтера: {exc}") from exc
            if not data or len(data) < 50:
                raise ValueError(f"Файл {name} не найден на SD-карте принтера")
            local.write_bytes(data)
            source = "printer"
        est = estimate_file(local) or {}
        grams = num(est.get("total_grams")) or num(est.get("grams"))
        minutes = num(est.get("total_minutes")) or num(est.get("minutes"))
        if grams and not est.get("total_grams"):
            est["total_grams"] = grams
        if minutes and not est.get("total_minutes"):
            est["total_minutes"] = minutes
        return {
            "ok": True,
            "file": name,
            "saved": local.name,
            "bytes": local.stat().st_size if local.exists() else 0,
            "source": source,
            "grams": grams,
            "minutes": minutes,
            "hours": round(minutes / 60.0, 2) if minutes else 0.0,
            "material": est.get("material") or "",
            "color": est.get("color") or "",
            "estimate": est,
        }

    def convert_active_to_order(self, printer_id: str = "", extra: dict | None = None) -> dict:
        """Преобразовать активную/текущую печать принтера в заказ."""
        printer = self.get(printer_id)
        if not printer:
            raise ValueError("Принтер не найден")
        snap = printer.snapshot()
        task = snap["printer"].get("task") or ""
        job = self.db.one(
            "SELECT * FROM print_jobs WHERE printer_id=? AND state IN ('running','starting','queued')"
            " ORDER BY datetime(created_at) DESC LIMIT 1", (printer.id,))
        if not task and job:
            task = job.get("name") or job.get("file") or ""
        if not task:
            task = f"Печать {printer.record.get('name', 'Bambu Lab')}"

        # Если задание уже связано с заказом — возвращаем его
        if job and job.get("order_id"):
            order = self.db.one("SELECT * FROM orders WHERE id=?", (job["order_id"],))
            if order:
                return {"ok": True, "order": order, "job": job, "created": False}

        # Очищаем имя для названия изделия
        clean = task.rsplit("/", 1)[-1]
        for ext in (".gcode.3mf", ".3mf", ".gcode"):
            if clean.lower().endswith(ext):
                clean = clean[:-len(ext)]
        product_name = clean.replace("_", " ").strip() or "Изделие из печати"

        # Материал и цвет из AMS или катушки
        active_tray = next((t for t in snap["ams"].get("trays", []) if t.get("active")), None)
        if not active_tray and snap["ams"].get("trays"):
            active_tray = snap["ams"]["trays"][0]
        material = (active_tray.get("type") or "PLA") if active_tray else "PLA"
        color = (active_tray.get("color") or "") if active_tray else ""

        spool = None
        if active_tray:
            spool = self.acc.pick_spool(printer.id, str(active_tray.get("slot")),
                                        active_tray.get("type"), active_tray.get("uuid"))
        if spool:
            if spool.get("material"):
                material = spool["material"]
            if spool.get("color_name"):
                color = spool["color_name"]

        # Вес и время печати. Порядок: оценка из файла (локальная копия или
        # шапка G-code с SD) → факт принтера. Пока печать идёт, print_weight —
        # частичный расход, а не полный вес изделия, поэтому его используем
        # только для завершённой печати. Никаких «30 г по умолчанию»: если
        # данных нет — заказ создаётся с нулём и честной пометкой.
        est = self._slicer_estimate(printer, task)
        grams = num(est.get("grams"))
        minutes = num(est.get("minutes"))
        grams_source = est.get("source") or ""
        if not grams and snap["printer"].get("state") == "FINISH":
            grams = num(snap["printer"].get("weight"))
            if grams:
                grams_source = "printer"
        elapsed_min = num(snap["printer"].get("elapsed_min"))
        remaining_min = num(snap["printer"].get("remaining_min"))
        if not minutes and (elapsed_min or remaining_min):
            minutes = round(elapsed_min + remaining_min, 1)
            grams_source = grams_source or "printer"
        hours = round(minutes / 60.0, 2) if minutes else 0.0
        if est.get("material") and not material:
            material = est["material"]
        if est.get("color") and not color:
            color = est["color"]

        grams_note = ""
        if not grams:
            grams_note = ("Вес печати неизвестен — данные слайсера не найдены; укажите "
                          "вручную или возьмётся с принтера по завершении печати.")
        elif grams_source and grams_source != "printer":
            grams_note = f"Оценка из слайсера: {grams} г / {hours} ч."

        # Обеспечиваем наличие записи в print_jobs
        if not job:
            job = self.db.upsert("print_jobs", {
                "id": uid("job"),
                "printer_id": printer.id,
                "name": task,
                "file": task,
                "state": "running" if snap["printer"].get("state") in ("RUNNING", "PREPARE", "PAUSE") else "queued",
                "source": "printer",
                "started_at": now_iso(),
                "created_at": now_iso(),
                "grams": grams,
                "duration_min": round(elapsed_min, 1),
                "progress": num(snap["printer"].get("progress")),
                "layers": int(num(snap["printer"].get("total_layers"))),
            })

        order_data = {
            "product": (extra or {}).get("product") or product_name,
            "material": (extra or {}).get("material") or material,
            "color": (extra or {}).get("color") or color,
            "grams": num((extra or {}).get("grams"), grams),
            "hours": num((extra or {}).get("hours"), hours),
            "qty": max(1, int(num((extra or {}).get("qty"), 1))),
            "file": task,
            "status": "printing",
            "customer_name": (extra or {}).get("customer_name") or "",
            "phone": (extra or {}).get("phone") or "",
            "price": num((extra or {}).get("price")),
            "channel": (extra or {}).get("channel") or "Полка магазина",
            "niche_id": (extra or {}).get("niche_id") or "",
            "notes": ("Преобразовано из активной печати на "
                      f"{printer.record.get('name', 'Bambu Lab')}"
                      + (f". {grams_note}" if grams_note else "")),
            "auto_cost": 1,
        }
        order = self.repo.save_order(order_data)
        self.db.execute("UPDATE print_jobs SET order_id=? WHERE id=?", (order["id"], job["id"]))
        job["order_id"] = order["id"]
        self.db.add_event(
            "order", "Печать преобразована в заказ",
            f"Заказ №{order.get('number')} · {order.get('product')}",
            printer.id, {"order_id": order["id"], "job_id": job["id"]})
        return {"ok": True, "order": order, "job": job, "created": True}

    def convert_job_to_order(self, job_id: str, extra: dict | None = None) -> dict:
        """Преобразовать задание из очереди или истории в заказ."""
        job = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))
        if not job:
            raise ValueError("Задание не найдено")
        if job.get("order_id"):
            order = self.db.one("SELECT * FROM orders WHERE id=?", (job["order_id"],))
            if order:
                return {"ok": True, "order": order, "job": job, "created": False}
        task = job.get("name") or job.get("file") or "Задание печати"
        clean = task.rsplit("/", 1)[-1]
        for ext in (".gcode.3mf", ".3mf", ".gcode"):
            if clean.lower().endswith(ext):
                clean = clean[:-len(ext)]
        product_name = clean.replace("_", " ").strip() or "Изделие из задания"
        grams = num(job.get("grams")) or num(job.get("est_grams"))
        minutes = num(job.get("duration_min")) or num(job.get("est_minutes"))
        # Если факта нет — пробуем оценку из файла на SD/локально, а не «30 г».
        if not grams and not minutes:
            printer = self.get(job.get("printer_id") or "")
            est = self._slicer_estimate(printer, task) if printer else {
                "grams": 0.0, "minutes": 0.0, "source": ""}
            grams = num(est.get("grams"))
            minutes = num(est.get("minutes"))
        hours = round(minutes / 60.0, 2) if minutes else 0.0
        material = self._job_material(job) or "PLA"
        status = "printing" if job.get("state") == "running" else "new"
        order_data = {
            "product": (extra or {}).get("product") or product_name,
            "material": (extra or {}).get("material") or material,
            "color": (extra or {}).get("color") or "",
            "grams": num((extra or {}).get("grams"), grams),
            "hours": num((extra or {}).get("hours"), hours),
            "qty": max(1, int(num((extra or {}).get("qty"), 1))),
            "file": job.get("file") or task,
            "status": status,
            "customer_name": (extra or {}).get("customer_name") or "",
            "phone": (extra or {}).get("phone") or "",
            "price": num((extra or {}).get("price")),
            "channel": (extra or {}).get("channel") or "Полка магазина",
            "niche_id": (extra or {}).get("niche_id") or "",
            "notes": f"Преобразовано из задания {job.get('id')}",
            "auto_cost": 1,
        }
        order = self.repo.save_order(order_data)
        self.db.execute("UPDATE print_jobs SET order_id=? WHERE id=?", (order["id"], job["id"]))
        job["order_id"] = order["id"]
        self.db.add_event(
            "order", "Задание преобразовано в заказ",
            f"Заказ №{order.get('number')} · {order.get('product')}",
            job.get("printer_id") or "", {"order_id": order["id"], "job_id": job["id"]})
        return {"ok": True, "order": order, "job": job, "created": True}

    # ------------------------------------------------------------- авто-продолжение (Крым / сбои питания)
    def _mark_restart_recovery_candidates(self) -> None:
        """Пометить незавершённые печати как кандидатов после рестарта.

        Это единственный безопасный способ восстановить печать, если свет
        отключился вместе с компьютером и память процесса была потеряна.
        ``manual_paused`` сохраняется в SQLite и исключает намеренную паузу.
        """
        stamp = now_iso()
        self.db.execute(
            "UPDATE print_jobs SET power_loss_at=?, resume_reason='connector_restart',"
            " power_loss_state=COALESCE(NULLIF(power_loss_state,''),'RUNNING'),"
            " power_loss_progress=COALESCE(progress,0), power_loss_layer=COALESCE(layers,0)"
            " WHERE state IN ('running','starting','uploading')"
            " AND COALESCE(resume_eligible,1)=1 AND COALESCE(manual_paused,0)=0"
            " AND COALESCE(power_loss_at,'')=''",
            (stamp,))

    def _mark_power_loss_candidate(self, printer_id: str, reason: str = "connection_lost",
                                   context: dict | None = None) -> None:
        """Записать потерю связи у активной печати, не отправляя команду."""
        stamp = now_iso()
        context = context or {}
        changed = self.db.execute(
            "UPDATE print_jobs SET power_loss_at=?, resume_reason=?,"
            " power_loss_state=?, power_loss_progress=?, power_loss_layer=?,"
            " power_loss_total_layers=?, power_loss_task=?"
            " WHERE printer_id=? AND state IN ('running','starting','uploading')"
            " AND COALESCE(resume_eligible,1)=1 AND COALESCE(manual_paused,0)=0",
            (stamp, reason, str(context.get("last_state") or "RUNNING").upper(),
             num(context.get("progress")), int(num(context.get("layer"))),
             int(num(context.get("total_layers"))), str(context.get("task") or ""),
             printer_id))
        if changed.rowcount:
            self.db.add_event(
                "security", "Печать помечена для восстановления питания",
                "Связь с принтером потеряна; это лишь кандидат, не подтверждение "
                "power loss. Нужен явный marker firmware/bridge и safety-gate",
                printer_id, {"reason": reason, "at": stamp, "last_snapshot": context})

    def _confirm_power_loss_candidate(self, printer_id: str, evidence: dict | None = None) -> None:
        """Перевести кандидат в подтверждённый recovery по сигналу принтера."""
        evidence = evidence or {}
        self.db.execute(
            "UPDATE print_jobs SET resume_reason='power_loss_confirmed'"
            " WHERE printer_id=? AND state IN ('running','starting','uploading')"
            " AND COALESCE(resume_eligible,1)=1 AND COALESCE(manual_paused,0)=0"
            " AND resume_reason IN ('connection_lost','connector_restart')",
            (printer_id,))
        self.db.add_event(
            "security", "Подтверждено восстановление питания",
            "Авто-resume разрешён только для этой прерванной печати после preflight",
            printer_id, evidence)

    def mark_non_resumable_pause(self, printer_id: str, reason: str) -> None:
        """Пометить автоматическую защитную паузу как не-resumable."""
        self.db.execute(
            "UPDATE print_jobs SET manual_paused=1, resume_eligible=0,"
            "power_loss_at='', resume_reason=?"
            " WHERE printer_id=? AND state IN ('running','starting','uploading')",
            (reason or "protective_pause", printer_id))

    def mark_user_paused(self, printer_id: str) -> None:
        """Сохранить ручную паузу, чтобы она пережила рестарт коннектора."""
        self._user_paused[printer_id] = time.time()
        self.db.execute(
            "UPDATE print_jobs SET manual_paused=1, resume_eligible=0,"
            "power_loss_at='', resume_reason='manual_pause'"
            " WHERE printer_id=? AND state IN ('running','starting','uploading')",
            (printer_id,))

    def _clear_recovery_when_running(self, printer_id: str, snap: dict) -> None:
        """Печать уже RUNNING: закрыть старую recovery-сессию."""
        state = str(snap.get("printer", {}).get("state") or "").upper()
        if state not in ("RUNNING", "PREPARE", "SLICING"):
            return
        self.db.execute(
            "UPDATE print_jobs SET power_loss_at='', resume_attempts=0, resume_reason=''"
            " WHERE printer_id=? AND state='running' AND COALESCE(power_loss_at,'')<>''",
            (printer_id,))

    def clear_user_paused(self, printer_id: str) -> None:
        """Снять ручную паузу после явной команды resume или завершения печати."""
        self._user_paused.pop(printer_id, None)
        self.db.execute(
            "UPDATE print_jobs SET manual_paused=0, resume_eligible=1,"
            "power_loss_at='', resume_attempts=0, resume_reason=''"
            " WHERE printer_id=? AND state IN ('running','starting','uploading')",
            (printer_id,))

    def is_user_paused(self, printer_id: str) -> bool:
        """Была ли пауза явно нажата в текущем процессе."""
        last = self._user_paused.get(printer_id, 0.0)
        return (time.time() - last) < 300.0

    def _recovery_block(self, printer_id: str, job: dict, code: str,
                        title: str, detail: str) -> None:
        """Журналировать блокировку recovery, но не спамить на каждом snapshot."""
        key = f"{printer_id}:{job.get('id') or ''}:{code}"
        now = time.time()
        if now - self._auto_resume_blocked_at.get(key, 0.0) < 60:
            return
        self._auto_resume_blocked_at[key] = now
        self.db.add_event("security", title, detail, printer_id,
                          {"job_id": job.get("id"), "block": code})

    def check_auto_resume(self, printer_id: str, snap: dict | None = None) -> bool:
        """Resume только для доказанного power-loss recovery.

        Статус PAUSE сам по себе ничего не доказывает: это также ручная пауза,
        filament runout, HMS/fatal и другие ошибки. Доказательством здесь
        является устойчивый marker потери связи плюс явный marker firmware/
        bridge о восстановлении питания; один offline/online не подтверждает
        причину. Marker для ручной паузы сбрасывается в SQLite.
        """
        # Power-loss recovery — отдельный safety-policy gate. Общий
        # unattended_dangerous_actions сюда не протекает: обычные физические
        # команды по-прежнему требуют его и/или confirmed=true.
        if not self.db.setting("auto_resume_paused", False):
            return False
        printer = self.get(printer_id)
        if not printer or not printer.connected:
            return False
        if snap is None:
            snap = printer.snapshot()
        state = str(snap.get("printer", {}).get("state", "")).upper()
        if state not in ("PAUSE", "PAUSED"):
            return False
        if self.is_user_paused(printer_id):
            return False

        job = self.db.one(
            "SELECT * FROM print_jobs WHERE printer_id=?"
            " AND state IN ('running','starting','uploading')"
            " ORDER BY datetime(created_at) DESC LIMIT 1", (printer_id,))
        # Нет устойчивого job/marker — причина паузы неизвестна, оставляем
        # оператору. COALESCE нужен для legacy-строк до миграции.
        marker = job.get("power_loss_at") if job else ""
        if (not job or not marker or int(job.get("manual_paused") or 0)
                or not int(job.get("resume_eligible", 1))):
            return False

        printer_data = snap.get("printer", {})
        explicit_power_signal = bool(printer_data.get("power_loss_recovery"))
        reason = str(job.get("resume_reason") or "")
        if explicit_power_signal and reason in ("connection_lost", "connector_restart"):
            self._confirm_power_loss_candidate(printer_id, {
                "source": "printer_snapshot", "power_loss_recovery": True,
                "job_id": job.get("id")})
            job = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job["id"],)) or job
            reason = str(job.get("resume_reason") or "")
        # A connection cycle is only a candidate. Без явного marker firmware/
        # bridge (или уже сохранённого подтверждения) причина остаётся
        # неизвестной, поэтому даже при PAUSE resume не отправляем.
        if reason != "power_loss_confirmed":
            return False
        task = str(printer_data.get("task") or "").strip()
        current_remote_id = str(printer_data.get("remote_task_id") or "").strip()
        expected_names = [str(job.get("name") or "").strip(),
                          str(job.get("file") or "").strip()]
        expected_names = [x for x in expected_names if x]
        def same_file(current: str, expected: str) -> bool:
            current, expected = current.lower(), expected.lower()
            return (current == expected
                    or current.rsplit("/", 1)[-1] == expected.rsplit("/", 1)[-1])
        if (not task or (expected_names and not any(same_file(task, x) for x in expected_names))):
            self.db.add_event(
                "security", "Авто-resume заблокирован: изменено задание",
                f"На принтере «{task or 'неизвестно'}», в очереди «{expected_names[0] if expected_names else 'неизвестно'}»",
                printer_id, {"job_id": job.get("id"), "marker": marker,
                             "current_remote_task_id": current_remote_id,
                             "expected_remote_task_id": job.get("remote_task_id") or ""})
            return False
        expected_remote_id = str(job.get("remote_task_id") or "").strip()
        if expected_remote_id and expected_remote_id != current_remote_id:
            self.db.add_event(
                "security", "Авто-resume заблокирован: версия задания изменена",
                "Идентификатор задачи на принтере не совпадает с сохранённым",
                printer_id, {"job_id": job.get("id"), "marker": marker,
                             "current_remote_task_id": current_remote_id,
                             "expected_remote_task_id": expected_remote_id})
            return False
        current_version = str(printer_data.get("file_version") or "").strip()
        expected_version = str(job.get("file_version") or "").strip()
        if expected_version and expected_version != current_version:
            self.db.add_event(
                "security", "Авто-resume заблокирован: версия файла изменена",
                "Хэш/версия файла на принтере не совпадает с сохранённым",
                printer_id, {"job_id": job.get("id"), "marker": marker})
            return False

        # Старый marker не является достаточным доказательством того, что
        # принтер всё ещё удерживает resumable-задачу.
        try:
            marker_ts = datetime.fromisoformat(str(marker).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError):
            return False
        max_delay = max(0, int(num(self.db.setting("auto_resume_max_delay_minutes", 1440), 1440)))
        if max_delay and time.time() - marker_ts > max_delay * 60:
            self.db.execute(
                "UPDATE print_jobs SET resume_reason='auto_resume_expired' WHERE id=?", (job["id"],))
            self.db.add_event(
                "security", "Авто-resume истёк",
                "Печать оставлена оператору: окно power-loss recovery завершилось",
                printer_id, {"job_id": job["id"], "marker": marker})
            return False

        # Recovery — не обычный запуск: повторяем preflight для материала,
        # активной катушки и состояния принтера, но не считаем PAUSE «готовым».
        if not isinstance(snap.get("ams"), dict):
            snap["ams"] = {}
        ok, reason_text = self._material_matches(job, snap, force=True)
        if not ok:
            self._recovery_block(printer_id, job, "material",
                                 "Авто-resume заблокирован: материал", reason_text)
            return False
        ok, reason_text = self._enough_filament(job, snap, force=True)
        if not ok:
            self._recovery_block(printer_id, job, "filament",
                                 "Авто-resume заблокирован: пластик", reason_text)
            return False

        # Проверяем, нет ли runout, HMS/fatal или неизвестной ошибки. Любой
        # blocking/error HMS оставляем оператору — нельзя угадывать причину
        # паузы и отправлять физический resume вслепую.
        printer_status = snap.get("printer", {})
        problems = printer_status.get("problems") or []
        if printer_status.get("hms"):
            self._recovery_block(printer_id, job, "hms",
                                 "Авто-resume заблокирован: HMS", "На принтере есть HMS-сообщение")
            return False
        if problems:
            prob = problems[0] if isinstance(problems[0], dict) else {}
            code = str(prob.get("code") or "")
            self._recovery_block(printer_id, job, "printer_error",
                                 "Авто-resume заблокирован: ошибка принтера",
                                 str(prob.get("title") or code or "Неизвестная ошибка"))
            return False
        raw_error = printer_status.get("print_error")
        if raw_error and str(raw_error) not in ("0", "0000-0000"):
            self._recovery_block(printer_id, job, "print_error",
                                 "Авто-resume заблокирован: print_error", str(raw_error))
            return False

        now = time.time()
        stored_attempts = int(job.get("resume_attempts") or 0)
        attempt_info = self._auto_resume_attempts.setdefault(
            printer_id, {"count": stored_attempts, "last_ts": 0.0, "task": task, "job_id": job["id"]})
        if attempt_info.get("job_id") != job["id"] or attempt_info.get("task") != task:
            attempt_info.update({"count": 0, "last_ts": 0.0, "task": task, "job_id": job["id"]})
        if now - attempt_info.get("last_ts", 0.0) < 4.0:
            return False
        if attempt_info.get("count", 0) >= 5:
            self.db.execute(
                "UPDATE print_jobs SET resume_reason='auto_resume_exhausted' WHERE id=?", (job["id"],))
            return False
        attempt_info["count"] += 1
        attempt_info["last_ts"] = now
        self.db.execute(
            "UPDATE print_jobs SET resume_attempts=?, resume_reason='auto_resume_attempt' WHERE id=?",
            (attempt_info["count"], job["id"]))
        try:
            printer.command("resume")
            # Команда принята брокером — recovery-сессия исчерпана. Сразу
            # убираем marker, чтобы последующая ручная PAUSE не была принята
            # за ту же аварию; retry остаются только для исключения до ACK.
            self.db.execute(
                "UPDATE print_jobs SET power_loss_at='', resume_reason='auto_resume_sent' WHERE id=?",
                (job["id"],))
            self.db.add_event(
                "printer", "⚡ Авто-resume после power loss",
                f"Принтер «{printer.record.get('name', 'Принтер')}»: «{task}» продолжена автоматически",
                printer_id, {"task": task, "job_id": job["id"],
                            "progress": snap["printer"].get("progress", 0),
                            "attempt": attempt_info["count"], "marker": marker})
            self.notify_async(
                f"⚡ PrintFlow · {printer.record.get('name', 'Принтер')}\n"
                f"Восстановление после отключения питания\n"
                f"Печать «{task}» автоматически продолжена (попытка {attempt_info['count']}).",
                None)
            return True
        except Exception as exc:
            if attempt_info["count"] >= 5:
                self.db.execute(
                    "UPDATE print_jobs SET resume_reason='auto_resume_exhausted' WHERE id=?",
                    (job["id"],))
                self.db.add_event(
                    "security", "Авто-resume остановлен после лимита",
                    "Не удалось безопасно продолжить печать; требуется оператор",
                    printer_id, {"job_id": job["id"], "attempts": attempt_info["count"]})
            else:
                self.db.execute(
                    "UPDATE print_jobs SET resume_reason='power_loss_confirmed' WHERE id=?",
                    (job["id"],))
            self.db.add_event("error", "Сбой авто-resume после power loss", str(exc), printer_id,
                              {"job_id": job["id"], "attempt": attempt_info["count"]})
            self.notify_async(
                f"⚠ PrintFlow · не удалось продолжить печать после power loss\n"
                f"Задание «{task}», попытка {attempt_info['count']}/5: {exc}", None)
            return False

    def _startup_auto_resume_loop(self) -> None:
        """Проверка при старте скрипта: если печать на стопе/паузе — продолжить сразу.

        Окно раскрыто до 15 минут: после сбоя питания принтер сам грузится
        60–120 секунд и ещё столько же ждёт MQTT-подключения. Прежние
        60 секунд гарантированно истекали до того, как принтер выходил
        в PAUSE, и авто-продолжение печати не срабатывало. Реконсиляцию
        (закрытие «зависших» заданий) делаем только один раз — после того
        как все принтеры реально успели прислать актуальную телеметрию.
        """
        start = time.time()
        reconciled_ids: set[str] = set()
        # Задание считается «зависшим» не сразу после подключения, а после
        # того, как принтер несколько раз подтвердил свободное состояние.
        idle_seen: dict[str, int] = {}
        while not self._stop.wait(1.5) and (time.time() - start < 900):
            with self.lock:
                printers = list(self.printers.values())
            for printer in printers:
                if not printer.connected:
                    continue
                # После сбоя питания первый snapshot приходит с пустой
                # телеметрией: даём ей 30 секунд «устаканиться».
                connected_since = getattr(printer, "connected_since", 0.0)
                if connected_since and (time.time() - connected_since) < 30:
                    continue
                snap = printer.snapshot()
                state = snap["printer"].get("state")
                if state in ("PAUSE", "PAUSED"):
                    self.check_auto_resume(printer.id, snap)
                    idle_seen.pop(printer.id, None)
                elif state in ("RUNNING", "PREPARE", "SLICING"):
                    self._clear_recovery_when_running(printer.id, snap)
                    idle_seen.pop(printer.id, None)
                elif printer.id not in reconciled_ids:
                    # Свободное состояние подтверждаем несколькими подряд
                    # snapshot'ами, чтобы не закрыть висящее задание по
                    # моменту «телеметрии ещё нет».
                    idle_seen[printer.id] = idle_seen.get(printer.id, 0) + 1
                    if idle_seen[printer.id] >= 5:
                        try:
                            self._reconcile_printer(printer, snap)
                        except Exception:
                            pass
                        reconciled_ids.add(printer.id)
            # Если все принтеры уже реконсилированы или сейчас печатают —
            # цикл больше не нужен, ждать нечего.
            if printers:
                done = True
                for p in printers:
                    if p.id in reconciled_ids:
                        continue
                    if not p.connected:
                        done = False
                        break
                    st = p.snapshot()["printer"].get("state")
                    if st not in ("RUNNING", "PREPARE", "PAUSE", "PAUSED", "SLICING"):
                        done = False
                        break
                if done:
                    break

    def _check_cost_limit(self, printer: BambuPrinter, snap: dict) -> None:
        """Лимит стоимости: пауза, если живая себестоимость перешла порог."""
        limit = num(self.db.setting("guard_cost_limit", 0.0))
        if limit <= 0 or snap["printer"]["state"] != "RUNNING":
            return
        job = snap.get("job") or {}
        spent = num(job.get("spent"))
        job_id = job.get("job_id") or ""
        if spent < limit or not job_id or job_id in self._cost_limit_reported:
            return
        self._cost_limit_reported.add(job_id)
        if len(self._cost_limit_reported) > 200:
            self._cost_limit_reported.clear()
        try:
            self.mark_non_resumable_pause(printer.id, "cost_limit_pause")
            printer.command("pause")
            acted = "печать поставлена на паузу"
        except Exception as exc:
            acted = f"паузу отправить не удалось: {exc}"
        detail = f"Потрачено {spent:.0f} ₽ при лимите {limit:.0f} ₽ — {acted}"
        self.db.add_event("guard", "Сторож: превышен лимит стоимости",
                          detail, printer.id,
                          {"job_id": job_id, "spent": spent, "limit": limit})
        self.notify_async(
            f"PrintFlow · {printer.record.get('name', 'Принтер')}\n"
            f"⚠ Превышен лимит стоимости печати\n{detail}", None)

    def idle_stats(self) -> dict:
        """Простой принтеров и упущенная прибыль (норма × часы простоя)."""
        target = num(self.db.setting("target_profit_per_hour", 250.0), 250.0)
        rows = self.db.query(
            "SELECT printer_id, MAX(finished_at) last_done FROM print_jobs"
            " WHERE state='done' GROUP BY printer_id")
        last_done = {r["printer_id"]: r["last_done"] for r in rows}
        idle_minutes = 0.0
        detail = []
        from datetime import datetime
        for printer in self.printers.values():
            snap = printer.snapshot()
            if snap["printer"]["state"] not in ("IDLE", "FINISH", "OFFLINE"):
                continue
            last = last_done.get(printer.id)
            if not last:
                continue
            try:
                done_at = datetime.fromisoformat(last)
                minutes = max(0.0, (datetime.now().astimezone() - done_at).total_seconds() / 60)
            except (ValueError, TypeError):
                continue
            idle_minutes += minutes
            detail.append({"printer": printer.record.get("name", "Принтер"),
                           "minutes": round(minutes, 1),
                           "lost": round(minutes / 60 * target, 2)})
        return {
            "idle_minutes": round(idle_minutes, 1),
            "idle_hours": round(idle_minutes / 60, 2),
            "lost_profit": round(idle_minutes / 60 * target, 2),
            "rate_per_hour": target,
            "printers": detail,
        }

    # 8.0: preflight wrapper
    def preflight(self, printer_id: str, filename: str, plate: int = 1, ams_mapping=None) -> dict:
        try:
            from .preflight import check_preflight
            return check_preflight(self.db, self, printer_id, filename, plate, ams_mapping)
        except Exception as exc:
            return {"ok": True, "blocks": [], "warns": [], "infos": [], "error": str(exc)}

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
        # Уведомления с действиями: к завершению/ошибке добавляем inline-кнопки,
        # чтобы реагировать не выходя из чата.
        buttons = []
        if kind == "complete":
            buttons = [("📷 Кадр", "cmd:frame"), ("▶ Следующее", "cmd:next"),
                       ("🤚 Снял", "cmd:removed")]
        elif kind == "error":
            buttons = [("📷 Кадр", "cmd:frame"), ("↻ Повторить", "cmd:reprint"),
                       ("▶ Продолжить", "cmd:resume")]
        self.notify_async(text, photo, buttons=buttons or None)

    def notify_async(self, text: str, photo: bytes | None = None,
                     buttons: list[tuple[str, str]] | None = None,
                     critical: bool = False) -> None:
        """Отправить сообщение в фоне, чтобы не тормозить поток телеметрии."""
        threading.Thread(target=self.send_telegram,
                         args=(text, photo, buttons, critical),
                         daemon=True).start()

    def tg_quiet_now(self) -> bool:
        """Тихие часы бота: ночью некритичные уведомления не уходят.

        Своя маска, независимая от тихих часов принтера (автозапуск).
        Пустые или равные границы — тихие часы выключены.
        """
        try:
            start = str(self.db.setting("telegram_quiet_from", "") or "").strip()
            end = str(self.db.setting("telegram_quiet_to", "") or "").strip()
            if not start or not end or start == end:
                return False
            time.strptime(start, "%H:%M")
            time.strptime(end, "%H:%M")
            now = time.strftime("%H:%M")
            if start <= end:
                return start <= now < end
            return now >= start or now < end  # интервал через полночь
        except Exception:
            return False

    def send_telegram(self, text: str, photo: bytes | None = None,
                      buttons: list[tuple[str, str]] | None = None,
                      critical: bool = False) -> dict:
        settings = self.db.settings(include_secrets=True)
        token, chat = settings.get("telegram_token"), settings.get("telegram_chat_id")
        if not token or not chat:
            return {"ok": False, "error": "Не заданы токен или chat_id"}
        if not critical and self.tg_quiet_now():
            return {"ok": True, "skipped": "quiet"}
        reply_markup = ""
        if buttons:
            reply_markup = json.dumps({"inline_keyboard": [
                [{"text": t, "callback_data": d} for t, d in buttons]]})
        try:
            if photo:
                return self._send_photo(token, str(chat), text, photo, reply_markup)
            data = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                           "reply_markup": reply_markup,
                                           "disable_web_page_preview": "true"}).encode()
            request = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data)
            with urllib.request.urlopen(request, timeout=10) as response:
                return {"ok": response.status == 200}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _send_photo(token: str, chat: str, caption: str, photo: bytes,
                    reply_markup: str = "") -> dict:
        """Кадр камеры прямо в сообщении: видно, что происходит, без захода в дом."""
        boundary = "----printflow" + uid("b").replace("b_", "")
        parts: list[bytes] = []
        for name, value in (("chat_id", chat), ("caption", caption[:1000]),
                            ("reply_markup", reply_markup)):
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
            # 8.0: health
            try:
                pr = self.get(snap["id"])
                snap["health"] = pr.health() if pr else {}
            except Exception:
                snap["health"] = {}
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
        low_threshold = num(self.db.setting("filament_low_threshold", 15.0), 15.0)
        tiles = []
        for snap in state["printers"]:
            info = snap["printer"]
            job = snap.get("job") or {}
            order = job.get("order") or {}
            alerts = (snap.get("guard") or {}).get("alerts") or []
            ams_low = 0
            for tray in snap["ams"].get("trays", []):
                remain = tray.get("remain")
                if remain is not None and remain >= 0 and remain < low_threshold:
                    ams_low += 1
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
                "ams_low": ams_low,
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
        """Во что обходится текущая печать прямо сейчас (живой расчёт).

        Считает по факту с принтера: граммы из телеметрии, цену — из реальной
        катушки активного слота AMS (а не тариф по умолчанию), плюс прогноз
        остатка, прибыль и точку безубыточности. Обновляется на каждом снимке.
        """
        info = snap.get("printer") or {}
        if info.get("state") not in ("RUNNING", "PAUSE", "PREPARE"):
            return {}
        settings = self.db.settings()
        progress = max(0.0, min(100.0, num(info.get("progress"))))
        elapsed_h = num(info.get("elapsed_min")) / 60
        remaining_h = num(info.get("remaining_min")) / 60
        total_h = elapsed_h + remaining_h
        grams = num(info.get("weight"))
        job = self.db.one(
            "SELECT * FROM print_jobs WHERE printer_id=? AND state='running'", (snap["id"],))
        order = None
        if job and job.get("order_id"):
            order = self.db.one(
                "SELECT id, number, product, customer_name, price, due, colors, grams, qty"
                " FROM orders WHERE id=?", (job["order_id"],))

        # Если принтер ещё не отдал вес — прикидываем из сметы заказа по прогрессу.
        if grams <= 0 and order:
            grams = num(order.get("grams")) * max(1.0, num(order.get("qty"), 1)) * progress / 100

        # Катушка активного слота AMS (или привязанная к заданию) — реальная цена.
        spool = None
        if job and job.get("spool_id"):
            spool = self.db.one("SELECT * FROM spools WHERE id=?", (job["spool_id"],))
        if not spool:
            active = next((t for t in (snap.get("ams") or {}).get("trays", [])
                           if t.get("active")), None)
            if active:
                spool = self.acc.pick_spool(snap["id"], str(active.get("slot")),
                                            active.get("type"), active.get("uuid"))
        if spool:
            per_gram = num(spool.get("price")) / max(1.0, num(spool.get("total_grams"), 1000))
            spool_info = {
                "material": spool.get("material") or "",
                "color": spool.get("color_name") or "",
                "price": num(spool.get("price")),
                "total_grams": num(spool.get("total_grams"), 1000),
                "remaining_grams": num(spool.get("remaining_grams")),
            }
        else:
            per_gram = (num(settings.get("default_spool_price"), 1600)
                        / max(1.0, num(settings.get("default_spool_weight"), 1000)))
            spool_info = None

        # Продувка AMS при многоцветной печати: смена цвета ≈ 12 г (на плиту).
        purge_grams = 0.0
        if order:
            try:
                colors = json.loads(str(order.get("colors") or ""))
                if isinstance(colors, list) and len(colors) > 1:
                    purge_grams = max(0, len(colors) - 1) * 12.0
            except (json.JSONDecodeError, TypeError):
                purge_grams = 0.0

        energy_rate = num(settings.get("power_kw"), 0.15) * num(settings.get("energy_price"), 6.0)
        wear_rate = (num(settings.get("amortization_per_hour"), 12.0)
                     + num(settings.get("maintenance_per_hour"), 3.0))
        time_rate = energy_rate + wear_rate

        # Прогноз полного веса по прогрессу (вес в телеметрии — уже напечатанный).
        remaining_grams = 0.0
        if grams > 0 and progress > 0:
            remaining_grams = max(0.0, grams * (100.0 - progress) / progress)

        spent = round(grams * per_gram + elapsed_h * time_rate, 2)
        remaining_cost = round(remaining_grams * per_gram + remaining_h * time_rate, 2)
        total = round(spent + remaining_cost, 2)

        price = num(order.get("price")) if order else 0.0
        profit = round(price - total, 2) if price else None
        margin = round(profit / price * 100, 1) if profit is not None and price else None
        # Точка безубыточности: сколько % цены уже «съедено» текущими затратами.
        break_even_pct = round(spent / price * 100, 1) if price else None
        return {
            "job_id": job["id"] if job else "",
            "order": order,
            "grams": round(grams, 1),
            "remaining_grams": round(remaining_grams, 1),
            "purge_grams": round(purge_grams, 1),
            "spent": spent,
            "remaining_cost": remaining_cost,
            "cost_total": total,
            "energy_kwh": round(total_h * num(settings.get("power_kw"), 0.15), 2),
            "per_hour": round(time_rate, 2),
            "price": price,
            "profit": profit,
            "margin_pct": margin,
            "break_even_pct": break_even_pct,
            "spool": spool_info,
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
        idle = {}
        try:
            idle = self.idle_stats()
        except Exception:
            idle = {}
        return {
            "total": len(printers),
            "online": len(online),
            "printing": len(printing),
            "queued": len([j for j in self.queue() if j["state"] == "queued"]),
            "today_hours": round(num(today.get("m")) / 60, 1),
            "today_grams": round(num(today.get("g")), 1),
            "today_jobs": int(num(today.get("d"))),
            "utilization": round(len(printing) / len(printers) * 100) if printers else 0,
            "idle": idle,
        }

    # ------------------------------------------- 5.0: автобэкап и снятие детали
    def auto_backup_if_due(self) -> None:
        """Сделать плановую копию и применить общий лимит ``backup_keep``."""
        days = int(num(self.db.setting("auto_backup_days", 1), 1))
        if days <= 0:
            return
        now = time.time()
        last = self._last_backup
        try:
            latest = max(BACKUP_DIR.glob("printflow-auto-*.sqlite3"),
                         key=lambda path: path.stat().st_mtime, default=None)
            if latest is not None:
                last = max(last, latest.stat().st_mtime)
        except OSError:
            pass
        if now - last < days * 24 * 3600:
            return
        # Не спамим диск/журнал каждые 30 секунд при постоянной ошибке, но и
        # не откладываем следующую попытку на целые сутки.
        if now - self._last_backup_attempt < 3600:
            return
        self._last_backup_attempt = now
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = now_iso()[:16].replace(":", "").replace("T", "-")
            target = BACKUP_DIR / f"printflow-auto-{stamp}.sqlite3"
            self.db.backup_to(target)
            rotate_backups(BACKUP_DIR, self.db.setting("backup_keep", 20))
            self._last_backup = now
            self.db.add_event("backup", "Автобэкап", f"Снимок базы: {target.name}")
        except Exception as exc:
            self.db.add_event("error", "Автобэкап не удался", str(exc))

    def part_removed(self, printer_id: str = "") -> dict:
        """«Деталь снята» — зафиксировать ручное действие и замерить простой."""
        printer = self.get(printer_id)
        job = self.db.one(
            "SELECT * FROM print_jobs WHERE state='done' ORDER BY datetime(finished_at) DESC LIMIT 1")
        idle = 0
        if job and job.get("finished_at"):
            try:
                from datetime import datetime
                done_at = datetime.fromisoformat(job["finished_at"])
                idle = max(0, round((datetime.now().astimezone() - done_at).total_seconds() / 60, 1))
            except Exception:
                pass
        name = printer.record.get("name", "Принтер") if printer else "Принтер"
        self.db.add_event("production", "Деталь снята",
                          f"{name} · простой после печати {idle} мин",
                          printer_id, {"idle_min": idle})
        if idle >= 5:
            self.notify_async(f"PrintFlow · {name}\nДеталь снята.\n"
                              f"Принтер простаивал {int(idle)} мин после завершения печати.")
        return {"ok": True, "idle_min": idle}

    # ------------------------------------------------------------ мониторинг AMS
    def ams_monitor(self, printer: BambuPrinter, snap: dict) -> None:
        """Сверка остатков AMS со складом и история смены катушек в слотах.

        Запускается из фонового цикла не чаще раза в 5 минут:
          1) сменился tray_uuid слота → событие «в слот поставили новую катушку»;
          2) остаток слота (AMS) расходится с остатком катушки (склад) больше
             чем на 20 п.п. → событие «проверьте катушку» — похоже, в AMS
             поставили другую катушку, чем учтено на складе.
        """
        now = time.time()
        if now - self._last_ams_sync < 300:
            return
        self._last_ams_sync = now
        # Автосбор: карточка принтера и катушки AMS → база (можно править руками)
        try:
            from .ams_sync import sync_ams_spools, sync_printer_info
            sync_printer_info(self.db, printer.id, snap)
            sync_ams_spools(self.db, printer.id, snap)
        except Exception as exc:
            self.db.add_event("error", "Сбой автосинка AMS", str(exc), printer.id)
        trays = snap["ams"].get("trays", []) or []
        if not trays:
            return
        pid = printer.id
        memory = self._tray_uuids.setdefault(pid, {})
        reported = self._ams_reported.setdefault(pid, set())
        for tray in trays:
            slot = str(tray.get("slot"))
            uuid = str(tray.get("uuid") or "")
            previous = memory.get(slot)
            if previous is not None and uuid and uuid != previous:
                self.db.add_event(
                    "ams", "В AMS заменили катушку",
                    f"{tray.get('label', 'Слот ' + slot)}: поставлена новая катушка",
                    pid, {"slot": slot, "tray_uuid": uuid})
                reported.discard(f"diff:{slot}")
            if uuid:
                memory[slot] = uuid
            # сверка остатков: AMS-процент против остатка катушки на складе
            remain = tray.get("remain")
            if remain is None or remain < 0:
                continue
            spool = self.acc.pick_spool(pid, slot, tray.get("type") or "", uuid)
            if not spool:
                continue
            total = max(1.0, num(spool.get("total_grams"), 1000))
            stock_pct = num(spool.get("remaining_grams")) / total * 100
            diff = abs(remain - stock_pct)
            key = f"diff:{slot}"
            if diff > 20 and key not in reported:
                reported.add(key)
                self.db.add_event(
                    "ams", "Остаток AMS не сходится со складом",
                    f"{tray.get('label', 'Слот ' + slot)}: AMS говорит {round(remain)}%, "
                    f"по складу {round(stock_pct)}%. Похоже, в слоте другая катушка — "
                    f"проверьте и поправьте остаток.",
                    pid, {"slot": slot, "ams_pct": remain, "stock_pct": round(stock_pct, 1)})
                if self.db.setting("notify_guard", True):
                    self.notify_async(
                        f"PrintFlow · {printer.record.get('name', 'Принтер')}\n"
                        f"Остаток AMS не сходится со складом\n"
                        f"{tray.get('label', 'Слот ' + slot)}: AMS {round(remain)}% vs склад {round(stock_pct)}%",
                        None)
            elif diff <= 20:
                reported.discard(key)

    def check_filament_stock(self) -> None:
        """Напоминания о закупке пластика: катушки ниже порога, раз в сутки."""
        if not self.db.setting("restock_remind", True):
            return
        threshold = num(self.db.setting("filament_low_threshold", 15.0), 15.0)
        today = now_iso()[:10]
        for spool in self.db.query(
                "SELECT * FROM spools WHERE archived=0 AND remaining_grams>0"):
            total = max(1.0, num(spool.get("total_grams"), 1000))
            pct = num(spool.get("remaining_grams")) / total * 100
            if pct > threshold:
                continue
            key = f"{spool['id']}:{today}"
            if key in self._restock_reported:
                continue
            self._restock_reported.add(key)
            if len(self._restock_reported) > 500:
                self._restock_reported.clear()
            self.db.add_event(
                "filament_low", "Пора закупить пластик",
                f"{spool.get('material')} {spool.get('color_name')}: "
                f"осталось {round(num(spool.get('remaining_grams')))} г ({round(pct)}%)",
                spool.get("printer_id") or "", {"spool_id": spool["id"]})
            # Конструктор правил: событие «пластик ниже порога».
            try:
                self.rules.run("filament_low", {
                    "material": spool.get("material"), "color": spool.get("color_name"),
                    "grams": round(num(spool.get("remaining_grams"))),
                    "pct": round(pct), "printer_id": spool.get("printer_id") or "",
                })
            except Exception:
                pass
            if self.db.setting("notify_filament_low", True):
                self.notify_async(
                    f"PrintFlow · закупка пластика\n"
                    f"{spool.get('material')} {spool.get('color_name')}: "
                    f"осталось {round(num(spool.get('remaining_grams')))} г",
                    None)

    def check_dry_humidity(self, printer: BambuPrinter, snap: dict) -> None:
        """Влажность AMS выше порога — пора сушить пластик (не чаще раза в 6 часов)."""
        humidity = snap["ams"].get("humidity")
        if humidity is None:
            return
        threshold = num(self.db.setting("dry_humidity_threshold", 55.0), 55.0)
        now = time.time()
        if num(humidity) <= threshold or now - self._dry_reported < 6 * 3600:
            return
        self._dry_reported = now
        self.db.add_event(
            "ams", "Влажность в AMS высокая",
            f"{round(num(humidity))}% при пороге {round(threshold)}% — "
            f"просушите пластик, иначе будут пузыри и хрупкие детали.",
            printer.id, {"humidity": humidity})
        if self.db.setting("notify_guard", True):
            self.notify_async(
                f"PrintFlow · {printer.record.get('name', 'Принтер')}\n"
                f"Влажность в AMS {round(num(humidity))}% — пора сушить пластик", None)

    def remind_finish(self, printer: BambuPrinter, snap: dict) -> None:
        """«Готово через N минут»: напомнить подойти к принтеру до конца печати."""
        remind_min = num(self.db.setting("notify_finish_remind_min", 10.0), 10.0)
        if remind_min <= 0:
            return
        info = snap["printer"]
        if info.get("state") != "RUNNING":
            return
        remaining = num(info.get("remaining_min"))
        if remaining <= 0 or remaining > remind_min:
            return
        job = self.db.one(
            "SELECT id FROM print_jobs WHERE printer_id=? AND state='running'",
            (printer.id,))
        if not job or job["id"] in self._finish_reminded:
            return
        self._finish_reminded.add(job["id"])
        if len(self._finish_reminded) > 200:
            self._finish_reminded.clear()
        text = (f"PrintFlow · {printer.record.get('name', 'Принтер')}\n"
                f"Печать закончится через ~{int(remaining)} мин — подойдите снять деталь.")
        photo = printer.camera.frame if self.db.setting("notify_photo", True) else None
        self.notify_async(text, photo)

    def run_scheduled(self) -> None:
        """Отложенные команды: выполнить те, чьё время наступило."""
        due = self.db.query(
            "SELECT * FROM scheduled_commands WHERE done=0 AND datetime(at)<=datetime(?)"
            " ORDER BY datetime(at)",
            (now_iso(),))
        for cmd in due:
            command_name = str(cmd.get("command") or "").strip()
            if (command_name in DANGEROUS_AUTOMATION_COMMANDS
                    and not self.db.setting("unattended_dangerous_actions", False)):
                ok, err = False, "Заблокировано safety-gate: опасные действия без оператора запрещены"
                self.db.execute(
                    "UPDATE scheduled_commands SET done=1, result=? WHERE id=?",
                    (err, cmd["id"]))
                self.db.add_event(
                    "security", "Отложенная команда заблокирована",
                    f"{command_name} · {cmd.get('note') or ''}",
                    cmd.get("printer_id") or "", {"ok": False, "blocked": True, "command": command_name})
                if self.db.setting("notify_guard", True):
                    self.notify_async(f"PrintFlow: опасная отложенная команда заблокирована\n{command_name}", None)
                continue
            printer = self.get(cmd.get("printer_id") or "")
            ok, err = False, "Принтер не найден"
            if printer:
                try:
                    value = None
                    try:
                        value = json.loads(cmd.get("value") or "null")
                    except json.JSONDecodeError:
                        value = None
                    printer.command(cmd.get("command", ""), value)
                    ok, err = True, ""
                except Exception as exc:
                    err = str(exc)
            self.db.execute(
                "UPDATE scheduled_commands SET done=1, result=? WHERE id=?",
                (err or "ok", cmd["id"]))
            self.db.add_event(
                "command", "Отложенная команда выполнена",
                f"{cmd.get('command') or ''} · {cmd.get('note') or ''}"
                + (f" — {err}" if err else ""),
                cmd.get("printer_id") or "", {"ok": ok, "error": err})
            if not ok and self.db.setting("notify_guard", True):
                self.notify_async(f"PrintFlow: отложенная команда не выполнилась\n{err}", None)

    # ------------------------------------------------ 8.5: наблюдение за цехом
    def capture_keyframes(self, printer, job: dict) -> None:
        """Видео печати (идеи 61, 87): кадр раз в N минут в архив задания."""
        interval = num(self.db.setting("keyframe_interval_min", 0.0), 0.0)
        if interval < 0.5:
            return
        frame = getattr(printer.camera, "frame", None)
        if not frame:
            return
        now = time.time()
        if now - self._keyframe_last.get(job["id"], 0.0) < interval * 60:
            return
        self._keyframe_last[job["id"]] = now
        try:
            from .config import PHOTO_DIR
            d = PHOTO_DIR / "keyframes" / str(job["id"])
            d.mkdir(parents=True, exist_ok=True)
            (d / time.strftime("%Y%m%d-%H%M%S.jpg")).write_bytes(frame)
            # Архив живёт 14 дней — дальше можно удалять без сожалений.
            for old in d.parent.iterdir():
                if old.is_dir() and time.time() - old.stat().st_mtime > 14 * 86400:
                    import shutil
                    shutil.rmtree(old, ignore_errors=True)
        except Exception:
            pass

    def watch_first_layer(self, printer, snap: dict, job: dict) -> None:
        """Первый слой (идея 60): первые N минут следим за столом в кадре."""
        minutes = num(self.db.setting("first_layer_watch_min", 5.0), 5.0)
        if not minutes or (snap.get("camera") or {}).get("demo"):
            return
        elapsed = num((snap.get("printer") or {}).get("elapsed_min"), 0.0)
        if elapsed <= 0 or elapsed > minutes or job["id"] in self._first_layer_alerted:
            return
        frame = getattr(printer.camera, "frame", None)
        if not frame:
            return
        try:
            from PIL import Image
            from .spaghetti import (edge_score, first_layer_decision,
                                    grayscale_matrix)
            img = Image.open(io.BytesIO(frame)).convert("L").resize((160, 120))
            score = edge_score(grayscale_matrix(img))
        except Exception:
            return
        ref = self._first_layer_ref.get(job["id"])
        if ref is None:
            self._first_layer_ref[job["id"]] = score
            return
        if first_layer_decision(ref, score):
            self._first_layer_alerted.add(job["id"])
            self.db.add_event(
                "guard", "Первый слой: печать могла оторваться от стола",
                "Плотность кромок в кадре резко упала — проверьте деталь",
                printer.id, {"job_id": job["id"]})
            if self.db.setting("notify_guard", True):
                self.notify_async(
                    "PrintFlow: первый слой — печать могла оторваться от стола. "
                    "Пауза и проверка.", frame,
                    buttons=[("⏸ Пауза", "cmd:pause"), ("📷 Кадр", "cmd:frame")],
                    critical=True)

    def watch_bed(self, printer_id: str) -> None:
        """Деталь осталась на столе (идея 10): кадр финиша vs эталон стола."""
        if not self.db.setting("bed_watch_enabled", False):
            return
        printer = self.get(printer_id)
        if not printer:
            return
        frame = getattr(printer.camera, "frame", None)
        if not frame:
            return
        from .config import PHOTO_DIR
        ref_file = PHOTO_DIR / "bed_reference.jpg"
        if not ref_file.exists():
            return
        from .spaghetti import frame_diff_ratio
        ratio = frame_diff_ratio(frame, ref_file.read_bytes())
        if ratio is None:
            return
        threshold = num(self.db.setting("bed_watch_threshold", 6.0), 6.0)
        if ratio > threshold:
            self.db.add_event(
                "guard", "Деталь могла остаться на столе",
                f"Кадр отличается от пустого стола на {ratio}% — снимите деталь",
                printer_id, {"diff_pct": ratio})
            if self.db.setting("notify_guard", True):
                self.notify_async(
                    f"PrintFlow: печать завершена, стол не пустой (разница {ratio}%). "
                    "Снимите деталь.", frame,
                    buttons=[("🤚 Снял", "cmd:removed")], critical=True)

    def set_bed_reference(self, printer_id: str) -> dict:
        """Калибровка: сохранить текущий кадр как эталон пустого стола."""
        printer = self.get(printer_id)
        if not printer:
            raise ValueError("Принтер не найден")
        frame = getattr(printer.camera, "frame", None)
        if not frame:
            raise ValueError("Кадр камеры ещё не получен")
        from .config import PHOTO_DIR
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        (PHOTO_DIR / "bed_reference.jpg").write_bytes(frame)
        self.db.add_event("system", "Эталон пустого стола сохранён",
                          "Для проверки «деталь на столе»", printer_id)
        return {"ok": True}

    def night_reset_if_due(self) -> None:
        """Ночной сброс цеха (идея 85): один раз в день — итоги дня в базу."""
        if not self.db.setting("night_reset_enabled", True):
            return
        today = time.strftime("%Y-%m-%d")
        if time.strftime("%H:%M") < str(self.db.setting("night_reset_time", "23:00") or "23:00"):
            return
        done = self.db.one(
            "SELECT id FROM events WHERE kind='system' AND title='Ночной сброс цеха'"
            " AND date(at)=date(?)", (today,))
        if done:
            return
        try:
            stats = self.db.one(
                "SELECT COALESCE(SUM(print_minutes),0) m, COALESCE(SUM(grams),0) g,"
                " COALESCE(SUM(jobs_done),0) d, COALESCE(SUM(jobs_failed),0) f"
                " FROM printer_stats WHERE date(day)=date(?)", (today,)) or {}
            income = self.db.one(
                "SELECT COALESCE(SUM(amount),0) v FROM transactions"
                " WHERE kind='income' AND date(at)=date(?)", (today,)) or {}
            text = (f"Сегодня: {int(num(stats.get('d')))} печатей "
                    f"({num(stats.get('m')) / 60:.1f} ч, {num(stats.get('g')):.0f} г), "
                    f"сбоев {int(num(stats.get('f')))}, "
                    f"доход {num(income.get('v')):,.0f} ₽")
            self.db.add_event("system", "Ночной сброс цеха", text, "",
                              {"hours": round(num(stats.get("m")) / 60, 1),
                               "grams": round(num(stats.get("g")), 0),
                               "income": round(num(income.get("v")), 0)})
        except Exception as exc:
            self.db.add_event("error", "Ночной сброс не удался", str(exc), "")

    # ------------------------------------------------------------ фоновый цикл
    def _loop(self) -> None:
        """Раз в 30 секунд: прогресс, телеметрия, сторож и очередь."""
        while not self._stop.wait(30):
            try:
                self.auto_backup_if_due()
                with self.lock:
                    printers = list(self.printers.values())
                for printer in printers:
                    if not printer.connected:
                        continue
                    snap = printer.snapshot()
                    try:
                        self.ams_monitor(printer, snap)
                        self.check_dry_humidity(printer, snap)
                        self.remind_finish(printer, snap)
                        self._watch_firmware(printer, snap)
                    except Exception as exc:
                        self.db.add_event("error", "Сбой мониторинга AMS", str(exc), printer.id)
                    job = self.db.one(
                        "SELECT id FROM print_jobs WHERE printer_id=? AND state='running'",
                        (printer.id,))
                    if job:
                        self.db.execute(
                            "UPDATE print_jobs SET progress=?, layers=? WHERE id=?",
                            (snap["printer"]["progress"], snap["printer"]["total_layers"], job["id"]))
                        self.capture_keyframes(printer, job)
                        self.watch_first_layer(printer, snap, job)
                    else:
                        self._reconcile_printer(printer, snap)
                    if job and snap["printer"]["state"] in ("IDLE", "FINISH", "FAILED"):
                        # Задание ещё «печатается», а принтер уже свободен —
                        # сверяем без ожидания следующего цикла.
                        self._reconcile_printer(printer, snap)
                    try:
                        self.guard.record_telemetry(printer, snap, job["id"] if job else "")
                        self.guard.check(printer, snap)
                        # Демо-камера (виртуальный принтер) отдаёт заготовленные
                        # кадры — спагетти-детект по ним был бы ложным.
                        if not (snap.get("camera") or {}).get("demo"):
                            self.spaghetti.check(printer, snap)
                        self._check_cost_limit(printer, snap)
                    except Exception as exc:
                        self.db.add_event("error", "Сбой сторожа печати", str(exc), printer.id)
                    if snap["printer"]["state"] in ("RUNNING", "PREPARE", "SLICING"):
                        self._clear_recovery_when_running(printer.id, snap)
                    elif snap["printer"]["state"] in ("IDLE", "FINISH"):
                        self._maybe_start_next(printer.id)
                    elif snap["printer"]["state"] in ("PAUSE", "PAUSED"):
                        self.check_auto_resume(printer.id, snap)
                try:
                    self.run_scheduled()
                    self.night_reset_if_due()
                    self.check_filament_stock()
                    self.maybe_sync_cloud_history()
                    self.rules.check_debts()
                except Exception:
                    continue
                try:
                    self._reconcile_orphan_orders()
                except Exception:
                    pass
            except Exception:
                continue

    def _reconcile_orphan_orders(self) -> None:
        """Заказы «в печати», по которым ничего не печатается.

        Бывает, когда задание удалили руками или связь заказа с заданием
        потерялась: панель показывает «принтер готов», а заказ висит.
        Работаем консервативно: снимаем статус только если принтеры на
        связи и все свободны (значит, ничего не печатается в принципе),
        и прошло больше 30 минут с изменения заказа — вручную поставленные
        в «печать» заказы не дёргаем зря.
        """
        with self.lock:
            printers = list(self.printers.values())
        connected = [p for p in printers if p.connected]
        if not connected:
            return  # принтеры не на связи — реальность неизвестна
        for printer in connected:
            try:
                if printer.snapshot()["printer"]["state"] in ("RUNNING", "PREPARE"):
                    return  # что-то печатается — не вмешиваемся
            except Exception:
                continue
        for order in self.db.query("SELECT id, updated_at FROM orders WHERE status='printing'"):
            running = self.db.one(
                "SELECT id FROM print_jobs WHERE order_id=? AND state IN ('running','starting')",
                (order["id"],))
            if running:
                continue
            try:
                touched = datetime.fromisoformat(
                    str(order.get("updated_at") or "").replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                continue
            if time.time() - touched < 1800:
                continue
            self._release_order(order["id"], "нет активного задания")

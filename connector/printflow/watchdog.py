"""Сторож печати: следит за принтером и вмешивается сам.

Три задачи модуля:

1. **Ошибки.** Расшифровывает HMS-коды и при серьёзном уровне ставит печать
   на паузу, сохраняет кадр камеры, пишет событие и считает потерю в рублях.
2. **Зависание.** Если статус «печать», а прогресс не растёт или сопло
   остыло — поднимает тревогу: обычно это оторвавшаяся деталь или засор.
3. **Обслуживание.** Копит наработку принтера в часах и напоминает о ТО.

Модуль ничего не делает молча: каждое действие попадает в журнал событий,
а деньги трогаются только если это разрешено в настройках.
"""
from __future__ import annotations

import time

from .accounting import num
from .config import DEFAULT_MAINTENANCE, now_iso

SEVERITY_ORDER = ["info", "warn", "error", "fatal"]

# HMS-коды «закончился пластик»: печать встала на паузу, нужен человек.
FILAMENT_RUNOUT_CODES = {
    "05002001", "05002002", "05002003", "05002004",
    "05002005", "05002006", "05002007", "05002008",
    "05003001", "05003002",
}


def severity_at_least(level: str, minimum: str) -> bool:
    if not level:
        return False
    try:
        return SEVERITY_ORDER.index(level) >= SEVERITY_ORDER.index(minimum)
    except ValueError:
        return False


class Watchdog:
    """Наблюдение за одним парком принтеров."""

    def __init__(self, manager):
        self.manager = manager
        self.db = manager.db
        # Память по каждому принтеру: последний прогресс и время его роста.
        self._progress: dict[str, tuple[float, float]] = {}
        # HMS-коды живут отдельно от производных тревог. Иначе отсутствие HMS
        # на каждом тике сбрасывало low/cold/overrun и уведомления повторялись.
        self._problem_codes: dict[str, set[str]] = {}
        self._reported: dict[str, set[str]] = {}
        self._cold_since: dict[str, float] = {}
        self._alerts: dict[str, list[dict]] = {}
        self._last_telemetry = 0.0
        self._last_cleanup = 0.0

    # ------------------------------------------------------------ настройки
    def enabled(self) -> bool:
        return bool(self.db.setting("guard_enabled", True))

    # ------------------------------------------------------------- проверки
    def check(self, printer, snap: dict) -> list[dict]:
        """Полная проверка одного принтера. Возвращает список тревог."""
        alerts: list[dict] = []
        if not self.enabled() or not printer.record.get("guard_enabled", 1):
            return alerts
        alerts += self._check_problems(printer, snap)
        alerts += self._check_stall(printer, snap)
        alerts += self._check_filament(printer, snap)
        alerts += self._check_overrun(printer, snap)
        self._alerts[printer.id] = alerts
        return alerts

    def _check_problems(self, printer, snap: dict) -> list[dict]:
        """Ошибки, о которых сообщил сам принтер."""
        problems = snap["printer"].get("problems") or []
        if not problems:
            self._problem_codes.pop(printer.id, None)
            return []
        seen = self._problem_codes.setdefault(printer.id, set())
        seen.intersection_update({str(item.get("code") or "") for item in problems})
        alerts = []
        minimum = str(self.db.setting("guard_pause_severity", "error"))
        for item in problems:
            if item["code"] in seen:
                continue
            seen.add(item["code"])
            alert = {
                "kind": "problem",
                "code": item["code"],
                "title": item["title"],
                "reason": item["reason"],
                "advice": item["advice"],
                "severity": item["severity"],
                "at": now_iso(),
            }
            acted = []
            if (severity_at_least(item["severity"], minimum)
                    and self.db.setting("guard_pause_on_error", True)
                    and snap["printer"]["state"] in ("RUNNING", "PREPARE")
                    and item["blocking"]):
                try:
                    self.manager.mark_non_resumable_pause(printer.id, "watchdog_pause")
                    printer.command("pause")
                    acted.append("печать поставлена на паузу")
                except Exception as exc:
                    acted.append(f"паузу отправить не удалось: {exc}")
            # «Закончился пластик» — особый случай: печать уже стоит, нужен человек
            # с новой катушкой. Пишем отдельное событие и шлём Telegram с кадром.
            if item["code"] in FILAMENT_RUNOUT_CODES:
                acted.append("нужна новая катушка")
                alert["title"] = "Закончился пластик в AMS"
                alert["advice"] = "Поставьте новую катушку и продолжите печать."
                tray = next((t for t in snap["ams"].get("trays", []) if t.get("active")), None)
                if tray:
                    alert["reason"] = f"{tray.get('label', 'Слот')}: филамент закончился"
                self.db.add_event(
                    "filament_low", "Закончился пластик в AMS",
                    alert["reason"], printer.id,
                    {"code": item["code"], "tray": (tray or {}).get("id", "")})
                self._notify(printer, alert)
            shot = self._snapshot(printer, item["title"])
            if shot:
                acted.append("сохранён кадр камеры")
            alert["actions"] = acted
            alerts.append(alert)
            self.db.add_event("guard", f"Сторож: {item['title']}",
                              item["advice"], printer.id,
                              {"code": item["code"], "severity": item["severity"],
                               "actions": acted})
            self._notify(printer, alert)
        return alerts

    def _check_stall(self, printer, snap: dict) -> list[dict]:
        """Печать идёт, а дело не двигается."""
        state = snap["printer"]["state"]
        if state not in ("RUNNING",):
            self._progress.pop(printer.id, None)
            self._cold_since.pop(printer.id, None)
            self._reported.setdefault(printer.id, set()).discard(f"cold:{printer.id}")
            return []
        progress = num(snap["printer"].get("progress"))
        layer = num(snap["printer"].get("layer"))
        marker = progress * 1000 + layer
        previous = self._progress.get(printer.id)
        now = time.time()
        if not previous or marker > previous[0]:
            self._progress[printer.id] = (marker, now)
            return []
        limit = num(self.db.setting("guard_stall_minutes", 20.0), 20.0)
        stuck_min = (now - previous[1]) / 60
        alerts = []
        if limit and stuck_min >= limit:
            self._progress[printer.id] = (marker, now)  # не повторять каждые 30 сек
            alert = {
                "kind": "stall",
                "code": "stall",
                "title": "Печать не двигается",
                "reason": f"Прогресс стоит на {round(progress)}% уже {int(stuck_min)} мин.",
                "advice": "Посмотрите камеру: деталь могла оторваться или засорилось сопло.",
                "severity": "error",
                "at": now_iso(),
                "actions": [],
            }
            if self._snapshot(printer, "Печать не двигается"):
                alert["actions"].append("сохранён кадр камеры")
            alerts.append(alert)
            self.db.add_event("guard", "Сторож: печать не двигается",
                              alert["reason"], printer.id, {"progress": progress})
            self._notify(printer, alert)
        # Холодное сопло при активной печати — тревога только после заданной
        # выдержки. Раньше ``guard_cold_minutes`` показывался в UI, но не
        # использовался: уведомление приходило на первом же тике прогрева.
        nozzle = num(snap["temperature"].get("nozzle"))
        target = num(snap["temperature"].get("nozzle_target"))
        key = f"cold:{printer.id}"
        cold = target > 100 and nozzle < target - 40
        if not cold:
            self._cold_since.pop(printer.id, None)
            self._reported.setdefault(printer.id, set()).discard(key)
            return alerts
        since = self._cold_since.setdefault(printer.id, now)
        limit = max(0.0, num(self.db.setting("guard_cold_minutes", 10.0), 10.0))
        cold_min = (now - since) / 60
        if cold_min >= limit and key not in self._reported.setdefault(printer.id, set()):
            self._reported[printer.id].add(key)
            alert = {
                "kind": "cold", "code": "cold",
                "title": "Сопло не догревается",
                "reason": (f"Задано {round(target)} °C, фактически {round(nozzle)} °C "
                           f"уже {max(1, round(cold_min))} мин."),
                "advice": "Проверьте нагреватель и термистор: печать пойдёт с браком.",
                "severity": "error", "at": now_iso(), "actions": [],
            }
            alerts.append(alert)
            self.db.add_event("guard", "Сторож: сопло не догревается",
                              alert["reason"], printer.id, {})
            self._notify(printer, alert)
        return alerts

    def _check_filament(self, printer, snap: dict) -> list[dict]:
        """Хватит ли пластика в активном слоте до конца печати."""
        if snap["printer"]["state"] != "RUNNING":
            return []
        threshold = num(self.db.setting("filament_low_threshold", 15.0), 15.0)
        alerts = []
        for tray in snap["ams"].get("trays", []):
            remain = tray.get("remain")
            if not tray.get("active") or remain is None or remain < 0:
                continue
            if num(remain) > threshold:
                continue
            key = f"low:{tray.get('id')}"
            seen = self._reported.setdefault(printer.id, set())
            if key in seen:
                continue
            seen.add(key)
            alert = {
                "kind": "filament", "code": "filament_low",
                "title": "Пластик заканчивается",
                "reason": f"{tray.get('label', 'Слот')}: осталось {round(num(remain))}%.",
                "advice": "Приготовьте новую катушку того же цвета.",
                "severity": "warn", "at": now_iso(), "actions": [],
            }
            alerts.append(alert)
            self.db.add_event("filament_low", "Пластик заканчивается",
                              alert["reason"], printer.id, {"tray": tray.get("id")})
            self._notify(printer, alert)
        return alerts

    def _check_overrun(self, printer, snap: dict) -> list[dict]:
        """Перерасход пластика против сметы слайсера посреди печати.

        Если к текущему проценту прогресса израсходовано заметно больше
        граммов, чем обещал слайсер, — ранний признак спагетти/поддержек/
        неверного профиля. Предупреждаем до того, как деталь и пластик пропадут.
        """
        if snap["printer"]["state"] != "RUNNING":
            return []
        threshold = num(self.db.setting("guard_overrun_pct", 15.0), 15.0)
        if threshold <= 0:
            return []
        progress = num(snap["printer"].get("progress"))
        if progress <= 5:
            return []
        job = self.db.one(
            "SELECT id, est_grams FROM print_jobs WHERE printer_id=? AND state='running'",
            (printer.id,))
        if not job or not num(job.get("est_grams")):
            return []
        est = num(job["est_grams"])
        actual = num(snap["printer"].get("weight"))
        if actual <= 0:
            return []
        # Проецируем фактический расход на полную печать и сравниваем со сметой.
        projected = actual * 100.0 / progress
        overrun = (projected - est) / est * 100.0
        if overrun < threshold:
            return []
        key = f"overrun:{job['id']}"
        if key in self._reported.setdefault(printer.id, set()):
            return []
        self._reported[printer.id].add(key)
        alert = {
            "kind": "overrun", "code": "overrun",
            "title": "Расход пластика выше сметы",
            "reason": (f"Смета {est:.0f} г, по темпу выйдет ≈{projected:.0f} г "
                       f"(+{overrun:.0f}%)."),
            "advice": "Проверьте камеру: возможны спагетти, лишние поддержки или неверный профиль.",
            "severity": "warn", "at": now_iso(), "actions": [],
        }
        if self._snapshot(printer, "Перерасход пластика"):
            alert["actions"].append("сохранён кадр камеры")
        self.db.add_event("guard", "Сторож: перерасход пластика",
                          alert["reason"], printer.id,
                          {"job_id": job["id"], "est": est, "projected": round(projected, 1)})
        self._notify(printer, alert)
        return [alert]

    # --------------------------------------------------------------- помощь
    def _snapshot(self, printer, note: str) -> bool:
        if not self.db.setting("guard_snapshot", True):
            return False
        try:
            printer.camera.snapshot(note=note)
            return True
        except Exception:
            return False

    def _notify(self, printer, alert: dict) -> None:
        if not self.db.setting("notify_guard", True):
            return
        name = printer.record.get("name", "Принтер")
        lines = [f"PrintFlow · {name}", alert["title"], alert["reason"], alert["advice"]]
        if alert.get("actions"):
            lines.append("Сделано: " + ", ".join(alert["actions"]))
        text = "\n".join(x for x in lines if x)
        photo = printer.camera.frame if self.db.setting("notify_photo", True) else None
        # Тревоги сторожа критичны: приходят даже в тихие часы бота.
        self.manager.notify_async(text, photo, critical=True)

    def alerts(self, printer_id: str = "") -> list[dict]:
        if printer_id:
            return self._alerts.get(printer_id, [])
        out = []
        for pid, items in self._alerts.items():
            for item in items:
                out.append({**item, "printer_id": pid})
        return out

    def clear(self, printer_id: str) -> None:
        self._problem_codes.pop(printer_id, None)
        self._reported.pop(printer_id, None)
        self._cold_since.pop(printer_id, None)
        self._alerts.pop(printer_id, None)

    # ----------------------------------------------------------- телеметрия
    def record_telemetry(self, printer, snap: dict, job_id: str = "") -> None:
        """Точка истории температур и скорости — раз в 30 секунд."""
        if not self.db.setting("telemetry_enabled", True):
            return
        if snap["printer"]["state"] not in ("RUNNING", "PREPARE", "PAUSE"):
            return
        self.db.execute(
            "INSERT INTO telemetry(at,printer_id,job_id,state,progress,layer,nozzle,"
            "nozzle_target,bed,bed_target,chamber,fan_part,fan_aux,speed,wifi)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), printer.id, job_id, snap["printer"]["state"],
             num(snap["printer"].get("progress")), int(num(snap["printer"].get("layer"))),
             num(snap["temperature"].get("nozzle")), num(snap["temperature"].get("nozzle_target")),
             num(snap["temperature"].get("bed")), num(snap["temperature"].get("bed_target")),
             num(snap["temperature"].get("chamber")), num(snap["fans"].get("part")),
             num(snap["fans"].get("aux")), num(snap["printer"].get("speed_percent")),
             str(snap["printer"].get("wifi") or "")))
        self._cleanup()

    def _cleanup(self) -> None:
        """Раз в час удаляем устаревшие точки, чтобы база не пухла."""
        if time.time() - self._last_cleanup < 3600:
            return
        self._last_cleanup = time.time()
        days = int(num(self.db.setting("telemetry_keep_days", 14), 14))
        self.db.execute("DELETE FROM telemetry WHERE at < datetime('now', ?)",
                        (f"-{max(1, days)} days",))

    def telemetry(self, printer_id: str, minutes: int = 180) -> list[dict]:
        return self.db.query(
            "SELECT * FROM telemetry WHERE printer_id=? AND at >= datetime('now', ?)"
            " ORDER BY at", (printer_id, f"-{max(5, int(minutes))} minutes"))

    # --------------------------------------------------------- обслуживание
    def seed_maintenance(self, printer_id: str) -> None:
        """Создать регламент ТО для нового принтера."""
        exists = self.db.one("SELECT 1 FROM maintenance WHERE printer_id=? LIMIT 1", (printer_id,))
        if exists:
            return
        for index, (task_id, task, hours, note) in enumerate(DEFAULT_MAINTENANCE):
            self.db.upsert("maintenance", {
                "id": f"{printer_id}:{task_id}", "printer_id": printer_id,
                "task": task, "every_hours": hours, "note": note,
                "last_at": now_iso(), "last_hours": 0.0, "active": 1, "position": index})

    def add_runtime(self, printer_id: str, minutes: float, grams: float = 0.0) -> None:
        """Учесть наработку после завершения печати."""
        self.db.execute(
            "UPDATE printers SET total_minutes=COALESCE(total_minutes,0)+?,"
            " total_grams=COALESCE(total_grams,0)+? WHERE id=?",
            (max(0.0, num(minutes)), max(0.0, num(grams)), printer_id))
        self.check_maintenance(printer_id)

    def runtime_hours(self, printer_id: str) -> float:
        row = self.db.one("SELECT total_minutes FROM printers WHERE id=?", (printer_id,)) or {}
        return round(num(row.get("total_minutes")) / 60, 1)

    def maintenance(self, printer_id: str) -> list[dict]:
        """Список задач ТО с остатком до срока."""
        hours = self.runtime_hours(printer_id)
        rows = self.db.query(
            "SELECT * FROM maintenance WHERE printer_id=? AND active=1 ORDER BY position",
            (printer_id,))
        out = []
        for row in rows:
            every = num(row.get("every_hours"))
            done_at = num(row.get("last_hours"))
            used = max(0.0, hours - done_at)
            left = round(every - used, 1) if every else None
            out.append({
                **row,
                "used_hours": round(used, 1),
                "left_hours": left,
                "percent": round(min(100.0, used / every * 100), 1) if every else 0,
                "due": bool(every and used >= every),
                "soon": bool(every and not used >= every and used >= every * 0.85),
            })
        return out

    def check_maintenance(self, printer_id: str) -> list[dict]:
        """Напомнить о задачах, срок которых наступил."""
        if not self.db.setting("maintenance_enabled", True):
            return []
        due = [t for t in self.maintenance(printer_id) if t["due"]]
        if not due:
            return []
        seen = self._reported.setdefault(f"maint:{printer_id}", set())
        fresh = [t for t in due if t["id"] not in seen]
        for task in fresh:
            seen.add(task["id"])
            self.db.add_event("maintenance", "Пора обслужить принтер",
                              task["task"], printer_id, {"task_id": task["id"]})
            if self.db.setting("notify_maintenance", True):
                printer = self.manager.get(printer_id)
                name = printer.record.get("name", "Принтер") if printer else "Принтер"
                self.manager.notify_async(
                    f"PrintFlow · {name}\nПора обслужить принтер\n"
                    f"{task['task']}\n{task.get('note', '')}", None)
        return fresh

    def complete_maintenance(self, task_id: str) -> dict:
        """Отметить задачу выполненной — отсчёт пойдёт заново."""
        row = self.db.one("SELECT * FROM maintenance WHERE id=?", (task_id,))
        if not row:
            raise ValueError("Задача обслуживания не найдена")
        hours = self.runtime_hours(row["printer_id"])
        self.db.execute("UPDATE maintenance SET last_at=?, last_hours=? WHERE id=?",
                        (now_iso(), hours, task_id))
        self._reported.get(f"maint:{row['printer_id']}", set()).discard(task_id)
        self.db.add_event("maintenance", "Обслуживание выполнено", row["task"],
                          row["printer_id"], {"task_id": task_id})
        return self.db.one("SELECT * FROM maintenance WHERE id=?", (task_id,)) or {}

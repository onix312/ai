"""Виртуальный принтер PrintFlow 8.5 (идея 7) — P1S-симулятор.

Мок принтера для трёх задач:
    • тестировать всю цепочку (очередь → учёт → сторож → уведомления) без железа;
    • демонстрировать систему (режим «NOZZA tour», идея 27);
    • «горячий» дымовой тест в CI.

Интерфейс совпадает с тем, что менеджер берёт от `BambuPrinter`: snapshot(),
command(), start_print(), события on_event. Телеметрия — честная симуляция:
прогресс идёт по оценке задания и скорости демо (`demo_speed`, минут печати
за одну реальную секунду), кадры — из демо-режима камеры.

Виртуальный принтер НЕ живёт в таблице printers: его жизненным циклом
управляет менеджер по настройке `demo_printer_enabled`.
"""
from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable

from .accounting import num
from .bambu import STATE_NAMES, CameraWorker
from .db import Database

VIRTUAL_ID = "virtual"


class _FilesStub:
    """FTPS-заглушка: у виртуального принтера нет SD, файлы «загружаются» мгновенно."""

    def upload(self, path, name: str) -> str:
        return name

    def list(self) -> list[dict[str, Any]]:
        return []

    def delete(self, name: str) -> dict[str, Any]:
        return {"ok": True}


class VirtualPrinter:
    """Симуляция Bambu Lab P1S: состояние, телеметрия, события."""

    def __init__(self, db: Database, record: dict, on_event: Callable | None = None):
        self.db = db
        self.id = record.get("id") or VIRTUAL_ID
        self.record = dict(record)
        self.on_event = on_event or (lambda *a, **k: None)
        self.mode = "virtual"
        self.connected = True
        self.connecting = False
        self.connected_since = time.time()
        self.last_error = ""
        self.last_message = time.time()
        self.camera = CameraWorker(lambda: {"host": "", "access_code": "",
                                            "demo": True, "cloud": False})
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # состояние симуляции
        self._state = "IDLE"
        self._filename = ""
        self._subtask_name = ""
        self._task_id = ""
        self._started_ts = 0.0
        self._est_min = 120.0
        self._total_layers = 300
        self._est_grams = 0.0
        self._speed_level = 2
        self._light = "off"
        self._finish_at = 0.0
        self._accumulated = 0.0  # минут печати, накопленных до текущей паузы
        self.session: dict[str, Any] | None = None

    # ------------------------------------------------------------- жизненный цикл
    @property
    def files(self) -> _FilesStub:
        return _FilesStub()

    def update_record(self, record: dict) -> None:
        self.record = dict(record)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._tick_loop, name="pf-virtual", daemon=True)
        self._thread.start()
        try:
            # Демо-камера: без неё не будет кадров для кейфреймов и фото-отчётов.
            self.camera.start()
        except Exception:
            pass

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        try:
            self.camera.stop()
        except Exception:
            pass

    def _tick_loop(self) -> None:
        while not self._stop.wait(1.0):
            self.last_message = time.time()
            try:
                self._tick()
            except Exception:
                pass

    # ------------------------------------------------------------- симуляция
    def _demo_speed(self) -> float:
        return max(0.1, num(self.db.setting("demo_speed", 1.0), 1.0))

    def _elapsed_min(self) -> float:
        """Минут печати с учётом пауз: накопленное + текущий отрезок."""
        if self._started_ts:
            return self._accumulated + (time.time() - self._started_ts) * self._demo_speed()
        return self._accumulated

    def _active_job(self) -> dict | None:
        return self.db.one(
            "SELECT * FROM print_jobs WHERE printer_id=? AND state IN ('running','starting')"
            " ORDER BY datetime(created_at) DESC LIMIT 1", (self.id,))

    def _tick(self) -> None:
        with self._lock:
            if self._state in ("PRINTING", "FINISH"):
                job = self._active_job()
                if not job:
                    # Задание отменили или убрали — станок останавливаем молча:
                    # менеджер сам закрыл задание, событие не нужно.
                    self._state = "IDLE"
                    return
                if not self._est_min:
                    self._est_min = max(1.0, num(job.get("est_minutes"), 0) or 120.0)
                    self._est_grams = num(job.get("est_grams"), 0)
                    self._total_layers = max(50, min(2000, int(self._est_min * 1.2)))
                if self._state == "PRINTING":
                    elapsed = self._elapsed_min()
                    if elapsed >= self._est_min:
                        self._state = "FINISH"
                        self._finish_at = time.time()
                        self.on_event("complete", "Печать завершена",
                                      self._display_name(), self._session_data())
                        return
                elif self._state == "FINISH":
                    # Как у реального принтера: FINISH держим пару секунд, потом IDLE.
                    if time.time() - self._finish_at > 3:
                        self._state = "IDLE"
                        self._filename = ""

    def start_print(self, filename: str, plate: int = 1, use_ams: bool = True,
                    ams_mapping=None, bed_level: bool = True, flow_cali: bool = False,
                    timelapse: bool = False, subtask_name: str = "") -> dict:
        with self._lock:
            if self._state == "PRINTING":
                raise ValueError("Виртуальный принтер уже печатает")
            self._state = "PRINTING"
            self._filename = filename
            self._subtask_name = subtask_name or filename
            self._task_id = f"vt{int(time.time() * 1000)}"
            self._started_ts = time.time()
            self._accumulated = 0.0
            self._est_min = 0.0
            self._est_grams = 0.0
            self.session = self._session_data()
            self.on_event("start", "Печать началась", self._display_name(),
                          self._session_data())
            return {"ok": True}

    def _display_name(self) -> str:
        return self._subtask_name or self._filename or "Печать"

    def _session_data(self) -> dict:
        return {"remote_task_id": self._task_id, "filename": self._filename,
                "started_ts": self._started_ts, "plate": 1}

    def command(self, name: str, value: Any = None) -> dict:
        with self._lock:
            if name == "pause" and self._state == "PRINTING":
                # Пауза не засчитывается: отрезок уводим в накопленное.
                self._accumulated = self._elapsed_min()
                self._started_ts = 0.0
                self._state = "PAUSE"
                self.on_event("pause", "Печать приостановлена",
                              self._display_name(), self._session_data())
                return {"ok": True}
            if name == "resume" and self._state == "PAUSE":
                self._started_ts = time.time()
                self._state = "PRINTING"
                return {"ok": True}
            if name == "stop" and self._state in ("PRINTING", "PAUSE", "FINISH"):
                self.on_event("stop", "Печать остановлена",
                              self._display_name(), self._session_data())
                self._state = "IDLE"
                self._filename = ""
                return {"ok": True}
            if name == "light":
                self._light = "on" if self._light == "off" else "off"
                return {"ok": True}
            if name == "speed_level" and value is not None:
                self._speed_level = int(num(value, 2))
                return {"ok": True}
        return {"ok": True, "virtual": True}

    # ------------------------------------------------------------- телеметрия
    def _ams_trays(self) -> list[dict[str, Any]]:
        trays = []
        try:
            spools = self.db.query(
                "SELECT * FROM spools WHERE printer_id=? AND ams_slot IS NOT NULL"
                " AND ams_slot<>'' AND remaining_grams>0 ORDER BY ams_slot", (self.id,))
            running = self._state in ("PRINTING", "PAUSE")
            active_slot = None
            if running:
                job = self._active_job()
                if job and job.get("spool_id"):
                    row = self.db.one("SELECT ams_slot FROM spools WHERE id=?",
                                      (job["spool_id"],))
                    if row:
                        active_slot = str(row.get("ams_slot"))
            for s in spools:
                slot = int(num(s.get("ams_slot")))
                trays.append({
                    "id": f"0{slot}", "unit": 0, "slot": slot,
                    "label": f"AMS 1 · слот {slot + 1}",
                    "type": s.get("material") or "PLA",
                    "color": s.get("color") or "#cbd5e1",
                    "remain": min(100.0, num(s.get("remaining_grams")) / 10.0),
                    "uuid": s.get("tray_uuid") or f"virt-{s['id']}",
                    "nozzle_min": None, "nozzle_max": None,
                    "active": str(slot) == active_slot,
                })
        except Exception:
            pass
        return trays

    def snapshot(self) -> dict:
        with self._lock:
            state = self._state
            running = state in ("PRINTING", "PAUSE", "FINISH")
            progress = 0.0
            remaining = 0.0
            layer = 0
            elapsed = 0.0
            weight = 0.0
            if self._state == "FINISH":
                progress, layer = 100.0, self._total_layers
                elapsed = self._est_min
            elif running:
                elapsed = self._elapsed_min()
                if self._est_min:
                    progress = min(100.0, elapsed / self._est_min * 100.0)
                    remaining = max(0.0, self._est_min - elapsed)
                layer = int(progress / 100.0 * self._total_layers)
                weight = self._est_grams * progress / 100.0
            if self._state == "PAUSE":
                state = "PAUSE"
            return {
                "id": self.id,
                "name": self.record.get("name") or "P1S (виртуальный)",
                "model": self.record.get("model") or "P1S",
                "enabled": bool(self.record.get("enabled", 1)),
                "connection": {
                    "connected": True, "connecting": False, "configured": True,
                    "host": "virtual", "last_message": self.last_message,
                    "last_error": "", "mode": "virtual",
                },
                "printer": {
                    "state": state,
                    "state_label": STATE_NAMES.get(state, state),
                    "task": self._display_name() if running else "",
                    "file": self._filename if running else "",
                    "remote_task_id": self._task_id,
                    "file_version": self._task_id,
                    "power_loss_recovery": False,
                    "progress": round(progress, 1),
                    "remaining_min": round(remaining, 1),
                    "eta": time.time() + remaining * 60 if remaining else None,
                    "layer": layer,
                    "total_layers": self._total_layers,
                    "speed_level": self._speed_level,
                    "speed_label": "Standard",
                    "speed_percent": 100,
                    "wifi": "virtual", "firmware": "8.5.0-virtual",
                    "print_error": 0, "hms": [], "problems": [], "severity": "",
                    "sdcard": False, "weight": round(weight, 1),
                    "started_ts": self._started_ts or None,
                    "elapsed_min": round(elapsed, 1),
                },
                "temperature": {
                    "nozzle": 218.0 if running else 25.0,
                    "nozzle_target": 215.0 if running else 0.0,
                    "bed": 60.0 if running else 25.0,
                    "bed_target": 60.0 if running else 0.0,
                    "chamber": 30.0,
                },
                "fans": {"part": 100 if running else 0,
                         "aux": 50 if running else 0, "chamber": 0},
                "light": self._light,
                "ams": {
                    "units": 1, "humidity": 35.0, "temperature": 25.0,
                    "trays": self._ams_trays(), "active_tray": "",
                },
                "camera": copy.deepcopy(self.camera.state()),
            }

    def health(self) -> dict:
        return {"mode": "virtual", "virtual": True, "ports": {},
                "ok": True, "note": "Виртуальный принтер — симуляция"}

    def reconnect(self) -> None:
        pass

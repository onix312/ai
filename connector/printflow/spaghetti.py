"""Детектор «спагетти» по кадрам камеры PrintFlow 4.0.

Когда деталь отрывается от стола, принтер продолжает «печатать»: выдавливает
пластик в пустоту, и в кадре растёт мешанина из хаотичных нитей. Камера это
видит раньше, чем человек, — по резкому скачку количества кромок в кадре.

Модуль работает локально и ничего не отправляет в интернет. Анализ кадров
делается через Pillow (PIL); если библиотека не установлена, детектор честно
выключается и пишет об этом один раз в журнал, а не падает.

Решение о тревоге вынесено в чистую функцию `spaghetti_decision`, чтобы его
можно было тестировать без камеры и принтера.
"""
from __future__ import annotations

import io
import time
from typing import Any

from .accounting import num
from .config import now_iso

try:
    from PIL import Image
    HAS_PIL = True
except Exception:  # Pillow не установлен — детектор недоступен
    Image = None
    HAS_PIL = False

# Как часто брать кадр на анализ (сек). Камера отдаёт ~0,5–1 кадр/с,
# анализировать чаще не имеет смысла.
SAMPLE_INTERVAL = 90.0
# Сколько кадров подряд должны быть «подозрительными», чтобы поднять тревогу.
REQUIRED_STRIKES = 3
# Прогресс, ниже которого не анализируем: первый слой и так полон движения.
MIN_PROGRESS = 5.0


def edge_score(gray: list[list[int]]) -> float:
    """Плотность кромок в сером кадре: средняя |разница| соседних пикселей.

    Спагетти — это множество резких переходов свет/тень, поэтому у кадра с
    мешаниной нитей оценка заметно выше, чем у ровной детали или стола.
    """
    height = len(gray)
    width = len(gray[0]) if height else 0
    if not height or not width:
        return 0.0
    total = 0.0
    count = 0
    for y in range(height):
        row = gray[y]
        for x in range(width - 1):
            total += abs(row[x + 1] - row[x])
            count += 1
    for y in range(height - 1):
        for x in range(width):
            total += abs(gray[y + 1][x] - gray[y][x])
            count += 1
    return total / max(1, count)


def spaghetti_decision(ratio: float, strikes: int, sensitivity: float) -> tuple[int, bool]:
    """Накопить «подозрения» и решить, пора ли бить тревогу.

    ratio — во сколько раз плотность кромок текущего кадра выше базовой.
    sensitivity — порог, после которого кадр считается подозрительным.
    Возвращает (новое число подозрений, тревога?).
    """
    sensitivity = max(1.05, num(sensitivity, 3.0))
    if ratio > sensitivity:
        strikes += 1
        if strikes >= REQUIRED_STRIKES:
            return 0, True  # тревога и сброс счётчика
        return strikes, False
    # Кадр не подозрительный — не стреляем, но постепенно прощаем прошлые.
    return max(0, strikes - 1), False


def grayscale_matrix(image) -> list[list[int]]:
    """PIL-изображение (уже в оттенках серого) → двумерный список 0..255."""
    width, height = image.size
    data = list(image.getdata())
    return [data[y * width:(y + 1) * width] for y in range(height)]


class SpaghettiWatch:
    """Наблюдение за кадрами одного парка принтеров."""

    def __init__(self, manager):
        self.manager = manager
        self.db = manager.db
        # printer_id -> {"baseline", "strikes", "next_sample", "warned_no_pil"}
        self._memory: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ настройки
    def available(self) -> bool:
        return HAS_PIL

    def enabled(self) -> bool:
        return HAS_PIL and bool(self.db.setting("spaghetti_enabled", False))

    # ------------------------------------------------------------- проверка
    def check(self, printer, snap: dict) -> dict | None:
        """Проверить принтер по свежему кадру. Возвращает тревогу или None."""
        if not self.db.setting("spaghetti_enabled", False):
            return None
        if not HAS_PIL:
            self._note_missing(printer)
            return None
        if not printer.record.get("guard_enabled", 1):
            return None
        state = snap["printer"]["state"]
        if state != "RUNNING":
            self._memory.pop(printer.id, None)
            return None
        if num(snap["printer"].get("progress")) < MIN_PROGRESS:
            return None
        frame = printer.camera.frame
        if not frame:
            return None

        mem = self._memory.get(printer.id)
        now = time.time()
        if mem is not None and now - mem.get("next_sample", 0.0) < SAMPLE_INTERVAL:
            return None
        try:
            image = Image.open(io.BytesIO(frame)).convert("L").resize((64, 64))
        except Exception:
            return None
        score = edge_score(grayscale_matrix(image))

        if mem is None or not mem.get("baseline"):
            self._memory[printer.id] = {
                "baseline": score, "strikes": 0, "next_sample": now + SAMPLE_INTERVAL,
            }
            return None
        # База медленно догоняет норму печати, чтобы не реагировать на
        # закономерное появление новых слоёв и поддержек.
        baseline = num(mem["baseline"])
        mem["baseline"] = baseline * 0.95 + score * 0.05
        ratio = score / max(1e-3, baseline)
        strikes, fire = spaghetti_decision(
            ratio, int(mem.get("strikes", 0)),
            num(self.db.setting("spaghetti_sensitivity", 3.0), 3.0))
        mem["strikes"] = strikes
        mem["next_sample"] = now + SAMPLE_INTERVAL
        if fire:
            mem["strikes"] = 0
            alert = {
                "kind": "spaghetti",
                "code": "spaghetti",
                "title": "Похоже, печать пошла «спагетти»",
                "reason": (f"Плотность кромок в кадре выросла в {ratio:.1f} раза "
                           f"и держится несколько минут."),
                "advice": "Проверьте камеру: деталь могла оторваться от стола. "
                          "Остановите печать, если это так.",
                "severity": "error",
                "at": now_iso(),
                "actions": [],
            }
            if self.db.setting("guard_pause_on_error", True):
                try:
                    self.manager.mark_non_resumable_pause(printer.id, "spaghetti_pause")
                    printer.command("pause")
                    alert["actions"].append("печать поставлена на паузу")
                except Exception as exc:
                    alert["actions"].append(f"паузу отправить не удалось: {exc}")
            if self.db.setting("guard_snapshot", True):
                try:
                    printer.camera.snapshot(note="спагетти-детект")
                    alert["actions"].append("сохранён кадр камеры")
                except Exception:
                    pass
            self.db.add_event("guard", "Сторож: спагетти-детект", alert["reason"],
                              printer.id, {"ratio": round(ratio, 2)})
            self._notify(printer, alert)
            return alert
        return None

    def _note_missing(self, printer) -> None:
        mem = self._memory.setdefault(printer.id, {})
        if mem.get("warned_no_pil"):
            return
        mem["warned_no_pil"] = True
        self.db.add_event(
            "system", "Спагетти-детект недоступен",
            "Для анализа кадров нужен пакет pillow — установите его и перезапустите PrintFlow.",
            printer.id, {})

    def _notify(self, printer, alert: dict) -> None:
        if not self.db.setting("notify_guard", True):
            return
        name = printer.record.get("name", "Принтер")
        lines = [f"PrintFlow · {name}", alert["title"], alert["reason"], alert["advice"]]
        if alert.get("actions"):
            lines.append("Сделано: " + ", ".join(alert["actions"]))
        text = "\n".join(x for x in lines if x)
        photo = printer.camera.frame if self.db.setting("notify_photo", True) else None
        self.manager.notify_async(text, photo)

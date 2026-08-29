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


def first_layer_decision(ref_score: float, score: float, min_ratio: float = 0.4) -> bool:
    """Первый слой: тревога, если плотность кромок резко упала к эталону.

    ref_score — оценка кадра в начале печати (этап, когда слой ложится),
    score — текущая. Если деталь отошла от стола, на кадре остаётся почти
    пустой стол — кромок заметно меньше. Чистая функция — тестируется
    без камеры.
    """
    if ref_score <= 0 or score <= 0:
        return False
    return score < ref_score * max(0.1, min_ratio)


def frame_diff_ratio(a: bytes, b: bytes) -> float | None:
    """Доля пикселей, различающихся между двумя JPEG-кадрами (0..100).

    Для «деталь осталась на столе» (идея 10): кадр после финиша против
    эталона пустого стола. None — если Pillow не установлен или кадр
    не распознал.
    """
    if not HAS_PIL:
        return None
    try:
        img_a = Image.open(io.BytesIO(a)).convert("L").resize((160, 120))
        img_b = Image.open(io.BytesIO(b)).convert("L").resize((160, 120))
        data_a = list(img_a.getdata())
        data_b = list(img_b.getdata())
    except Exception:
        return None
    if not data_a:
        return None
    diff = sum(1 for x, y in zip(data_a, data_b) if abs(x - y) > 24)
    return round(diff / len(data_a) * 100, 1)


def inspect_bed_clear(frame: bytes | None, reference: bytes | None,
                      threshold: float = 6.0) -> dict:
    """Я40: стол чист до старта. Не #10 (после финиша).

    ``ok=True`` — старт не блокировать. Без эталона или кадра проверка
    честно выключена, печать не стопорим.
    """
    if not reference:
        return {
            "ok": True, "level": "info", "code": "bed_no_ref",
            "title": "Нет эталона пустого стола",
            "detail": "Снимите эталон кнопкой «Пустой стол» — иначе проверка до старта выключена",
            "diff_pct": None,
        }
    if not frame:
        return {
            "ok": True, "level": "info", "code": "bed_no_camera",
            "title": "Камера не видит стол",
            "detail": "Нет кадра — проверка «стол чист» пропущена, печать не блокируется",
            "diff_pct": None,
        }
    ratio = frame_diff_ratio(frame, reference)
    if ratio is None:
        return {
            "ok": True, "level": "info", "code": "bed_no_compare",
            "title": "Не сравнить кадр со столом",
            "detail": "Нет Pillow или кадр не JPEG — проверка пропущена",
            "diff_pct": None,
        }
    limit = float(threshold if threshold is not None else 6.0)
    if ratio > limit:
        return {
            "ok": False, "level": "block", "code": "bed_dirty",
            "title": "На столе что-то лежит",
            "detail": (
                f"Кадр отличается от пустого стола на {ratio}% "
                f"(порог {limit}%) — уберите деталь, скребок или плёнку"
            ),
            "diff_pct": ratio,
        }
    return {
        "ok": True, "level": "ok", "code": "bed_clear",
        "title": "Стол чист",
        "detail": f"Разница с эталоном {ratio}%",
        "diff_pct": ratio,
    }


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

"""Оценка печати до запуска: время и граммы из файла задания.

BambuStudio кладёт в начало G-code служебные строки:
    ;TIME:8082            — секунды печати
    ;Filament used [g]: 5.1   — граммы (новый формат)
    ;Filament used: 12.3      — метры (старый формат, пересчитываем ~1.24 г/м для PLA)
3MF — это zip, внутри Metadata/plate_1.gcode с тем же форматом.

Оценка нужна до старта: показать «≈ 2 ч 10 мин · ≈ 38 г», зарезервировать
катушку и предупредить, что пластика может не хватить.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

_TIME_RE = re.compile(r";\s*TIME\s*:\s*(\d+)")
_GRAMS_RE = re.compile(r";\s*Filament used \[g\]\s*:\s*([\d.]+)")
_METERS_RE = re.compile(r";\s*Filament used\s*:\s*([\d.]+)")
# Материал и цвет из заголовка G-code: Bambu Studio пишет их в нескольких
# вариантах — через '=' или ':', с пробелами или без, "colour" и "color".
_TYPE_RE = re.compile(r";\s*filament[_ ]type\s*[:=]\s*([A-Za-z0-9+\-]+)")
_COLOR_HEX_RE = re.compile(r";\s*filament[_ ](?:colour|color)\s*[:=]\s*#?([0-9A-Fa-f]{6,8})")


def _hex_to_name(value: str) -> str:
    """Шестнадцатеричный цвет → имя по простой палитре (для заказа)."""
    value = value.lstrip("#")
    if len(value) >= 6:
        r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    else:
        return ""
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 30:
        if mx < 60:
            return "Чёрный"
        if mx > 200:
            return "Белый"
        return "Серый"
    if r >= g and r >= b:
        return "Оранжевый" if g > 90 else "Красный"
    if g >= r and g >= b:
        return "Зелёный"
    return "Синий"


def estimate_file(path: str | Path) -> dict:
    """Оценка файла .gcode или .3mf: {minutes, grams, material, color} или {}."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        if path.suffix.lower() == ".3mf":
            text = _read_3mf_gcode(path)
        elif path.suffix.lower() == ".gcode":
            text = _read_head(path)
        else:
            return {}
    except Exception:
        return {}
    if not text:
        return {}
    minutes = 0.0
    grams = 0.0
    m = _TIME_RE.search(text)
    if m:
        minutes = round(int(m.group(1)) / 60.0, 1)
    m = _GRAMS_RE.search(text)
    if m:
        grams = round(float(m.group(1)), 1)
    else:
        m = _METERS_RE.search(text)
        if m:
            # метры → граммы: PLA ~1.24 г/м; это оценка, точность не критична
            grams = round(float(m.group(1)) * 1.24, 1)
    result = {"minutes": minutes, "grams": grams}
    m = _TYPE_RE.search(text)
    if m:
        result["material"] = m.group(1).strip().upper()
    m = _COLOR_HEX_RE.search(text)
    if m:
        name = _hex_to_name(m.group(1))
        if name:
            result["color"] = name
    return result


def _read_head(path: Path, limit: int = 4000) -> str:
    """Первые строки gcode — служебный заголовок слайсера."""
    lines: list[str] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for i, line in enumerate(f):
            if i >= limit:
                break
            lines.append(line)
    return "\n".join(lines)


def _read_3mf_gcode(path: Path) -> str:
    """Достать Metadata/plate_*.gcode из 3MF-архива."""
    with zipfile.ZipFile(path) as zf:
        for name in sorted(zf.namelist()):
            if name.startswith("Metadata/") and name.endswith(".gcode"):
                raw = zf.read(name)
                return raw.decode("utf-8", "ignore")[:200_000]
    return ""

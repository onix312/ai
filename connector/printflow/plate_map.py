"""Карта плиты из 3MF и аудит G-code. Идеи 28, 54, 55.

Только стандартная библиотека: zipfile + xml.etree. Если 3MF устроен не так,
как ожидает парсер, честно возвращаем пустой результат, а не падаем: карта
плиты — наглядность, а не источник правды.
"""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# Плита P1S/X1C по умолчанию, мм (для A1 — меньше, но карта наглядна и без точности)
DEFAULT_PLATE = {"w": 256.0, "h": 256.0}

_NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}

_MAX_GCODE_LINES = 3_000_000


# ---------------------------------------------------------------- G-code аудит
def _float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def audit_gcode(path: str | Path) -> dict[str, Any]:
    """Прогон G-code: слои, высота, ретракты, максимальная скорость.

    Идея 28. Файл читается построчно (поточно), лимит строк защищает от
    многомеговых файлов: при обрезке честно помечаем ``truncated``.
    """
    path = Path(path)
    if not path.exists() or path.suffix.lower() != ".gcode":
        return {}
    layers: set[float] = set()
    retracts = 0
    max_speed = 0.0
    truncated = False
    lines = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                lines += 1
                if lines > _MAX_GCODE_LINES:
                    truncated = True
                    break
                s = line.strip()
                if not s or s.startswith(";"):
                    continue
                head = s.split(" ", 1)[0].upper()
                if head in ("G0", "G1"):
                    # позиция Z
                    m = re.search(r"\bZ([-\d.]+)", s)
                    if m:
                        z = _float(m.group(1))
                        if z >= 0:
                            layers.add(round(z, 2))
                    # скорость подачи
                    m = re.search(r"\bF([-\d.]+)", s)
                    if m:
                        max_speed = max(max_speed, _float(m.group(1)))
                    # ретракция: G1 E-… (отрицательный E) или G10
                    m = re.search(r"\bE([-\d.]+)", s)
                    if m and _float(m.group(1)) < 0:
                        retracts += 1
                elif head == "G10":
                    retracts += 1
                elif head == "M83":
                    pass  # относительные E — ничего делать не нужно
    except OSError:
        return {}
    if not layers:
        return {}
    height = max(layers)
    out: dict[str, Any] = {
        "layers": len(layers),
        "height_mm": round(height, 1),
        "retracts": retracts,
        "max_speed_mm_min": round(max_speed, 1),
        "truncated": truncated,
        "warnings": [],
    }
    if max_speed > 300:
        out["warnings"].append(
            f"Скорость подачи {max_speed:.0f} мм/мин выше типовых профилей — проверить слайсер")
    if height > 300:
        out["warnings"].append("Высота близка к пределу рабочего объёма принтера")
    if len(layers) > 3000:
        out["warnings"].append("Очень много слоёв — мелкий шаг, печать будет долгой")
    if retracts == 0 and len(layers) > 100:
        out["warnings"].append("Ретракций не найдено — возможно, профиль без ретракта (FDM) или другой формат E")
    return out


# ---------------------------------------------------------------- карта плиты
def _model_name(path: Path) -> str | None:
    """Имя XML с геометрией внутри 3MF (3D/3dmodel.model и варианты)."""
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                low = name.lower()
                if low.endswith((".model", ".3mm")) and ("3d" in low or "model" in low):
                    return name
    except zipfile.BadZipFile:
        return None
    return None


def _num(value: str | None, default: float = 0.0) -> float:
    return _float(value, default)


def plate_map_3mf(path: str | Path,
                  plate: dict[str, float] | None = None) -> dict[str, Any]:
    """Объекты на плите из 3MF: позиции, размеры (bbox × scale), заполнение.

    Идея 54. Возврат: {plate: {w,h}, objects: [{id, name, x, y, w, h}],
    fill_pct} или {} если парсер не понял файл.
    """
    path = Path(path)
    if not path.exists() or path.suffix.lower() != ".3mf":
        return {}
    name = _model_name(path)
    if not name:
        return {}
    try:
        with zipfile.ZipFile(path) as zf:
            raw = zf.read(name)
    except (zipfile.BadZipFile, KeyError, OSError):
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}

    objects: dict[str, dict[str, Any]] = {}
    for obj in root.iter(f"{{{_NS['m']}}}object"):
        oid = str(obj.get("id") or "")
        if not oid:
            continue
        minx, miny = _num(obj.get("minx")), _num(obj.get("miny"))
        maxx, maxy = _num(obj.get("maxx")), _num(obj.get("maxy"))
        objects[oid] = {
            "id": int(_num(oid, -1)),
            "name": str(obj.get("name") or f"Объект {int(_num(oid, 1))}"),
            "w": maxx - minx, "h": maxy - miny,
            "minx": minx, "miny": miny,
        }
    instances: list[dict[str, Any]] = []
    # Инстансы в 3MF — элементы <node objectid="..."> с позицией и масштабом.
    for inst in root.iter(f"{{{_NS['m']}}}node"):
        oid = str(inst.get("objectid") or "")
        if not oid or oid not in objects:
            continue
        pos = str(inst.get("position") or "0 0 0").split()
        scale = str(inst.get("scale") or "1 1 1").split()
        x = _num(pos[0]) if len(pos) > 0 else 0.0
        y = _num(pos[1]) if len(pos) > 1 else 0.0
        sx = _num(scale[0], 1.0) if len(scale) > 0 else 1.0
        sy = _num(scale[1], 1.0) if len(scale) > 1 else 1.0
        base = objects[oid]
        instances.append({
            "id": base["id"], "name": base["name"],
            "x": round(x + base["minx"] * sx, 1),
            "y": round(y + base["miny"] * sy, 1),
            "w": round(max(0.1, base["w"] * sx), 1),
            "h": round(max(0.1, base["h"] * sy), 1),
        })
    if not instances:
        return {}
    plate = dict(plate or DEFAULT_PLATE)
    area = max(1.0, _num(plate.get("w"), 256) * _num(plate.get("h"), 256))
    used = 0.0
    for o in instances:
        # пересечения объектов не вычитаем: для KPI «заполнения» честнее
        # сумма площадей с ограничением в 100 %
        used += o["w"] * o["h"]
    return {
        "plate": {"w": _num(plate.get("w"), 256), "h": _num(plate.get("h"), 256)},
        "objects": instances,
        "fill_pct": round(min(100.0, used / area * 100), 1),
    }


def plate_fill_pct(objects: list[dict[str, Any]],
                   plate: dict[str, float] | None = None) -> float:
    """Заполнение плиты по спискам объектов (идея 55)."""
    plate = dict(plate or DEFAULT_PLATE)
    area = max(1.0, _num(plate.get("w"), 256) * _num(plate.get("h"), 256))
    used = sum(_num(o.get("w")) * _num(o.get("h")) for o in (objects or []))
    return round(min(100.0, used / area * 100), 1)


# ---------------------------------------------------------------- интеграция
def enrich_estimate(path: str | Path) -> dict[str, Any]:
    """Набор расширенных данных для файла: аудит G-code или карта плиты 3MF."""
    path = Path(path)
    if not path.exists():
        return {}
    out: dict[str, Any] = {}
    if path.suffix.lower() == ".gcode":
        audit = audit_gcode(path)
        if audit:
            out["audit"] = audit
    elif path.suffix.lower() == ".3mf":
        plate = plate_map_3mf(path)
        if plate:
            out["plate"] = plate
    return out

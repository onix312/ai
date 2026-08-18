"""Оценка печати до запуска: время и граммы из файла задания.

BambuStudio кладёт в начало G-code служебные строки:
    ;TIME:8082            — секунды печати
    ;Filament used [g]: 5.1   — граммы (новый формат)
    ;Filament used: 12.3      — метры (старый формат, пересчитываем ~1.24 г/м для PLA)
3MF — это zip, внутри Metadata/plate_1.gcode с тем же форматом.

Оценка нужна до старта: показать «≈ 2 ч 10 мин · ≈ 38 г», зарезервировать
катушку и предупредить, что пластика может не хватить.

8.0 расширяет: парсит все плиты, thumbnails, slice_info, project_settings.
"""
from __future__ import annotations

import base64
import json
import re
import zipfile
from pathlib import Path

_TIME_RE = re.compile(r";\s*TIME\s*:\s*(\d+)")
_GRAMS_RE = re.compile(r";\s*Filament used \[g\]\s*:\s*([\d.]+)")
_METERS_RE = re.compile(r";\s*Filament used\s*:\s*([\d.]+)")
_TYPE_RE = re.compile(r";\s*filament[_ ]type\s*[:=]\s*([A-Za-z0-9+\-]+)")
_COLOR_HEX_RE = re.compile(r";\s*filament[_ ](?:colour|color)\s*[:=]\s*#?([0-9A-Fa-f]{6,8})")
_BED_RE = re.compile(r";\s*bed[_ ]type\s*[:=]\s*([^\r\n;]+)")
_NOZZLE_RE = re.compile(r";\s*nozzle[_ ]diamete?r?\s*[:=]\s*([\d.]+)")
_PLATE_RE = re.compile(r"plate_(\d+)\.gcode")


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


def _parse_gcode_head(text: str) -> dict:
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
        result["color_hex"] = "#" + m.group(1).strip().lstrip("#")[:6]
    m = _BED_RE.search(text)
    if m:
        result["bed_type"] = m.group(1).strip()
    m = _NOZZLE_RE.search(text)
    if m:
        try:
            result["nozzle_diameter"] = float(m.group(1))
        except ValueError:
            pass
    # собрать все материалы/цвета если многоцвет
    types = _TYPE_RE.findall(text)
    colors = _COLOR_HEX_RE.findall(text)
    if types:
        result["filaments"] = [{"type": t.upper(), "color": "#" + (colors[i] if i < len(colors) else "CCCCCC").lstrip("#")[:6]} for i, t in enumerate(types)]
    return result


def estimate_file(path: str | Path) -> dict:
    """Оценка файла .gcode или .3mf: {minutes, grams, material, color} или {}.

    8.0: для 3MF возвращает также plates, plate_count, thumbnails.
    """
    path = Path(path)
    if not path.exists():
        return {}
    try:
        if path.suffix.lower() == ".3mf":
            return estimate_3mf(path)
        elif path.suffix.lower() == ".gcode":
            text = _read_head(path)
            return _parse_gcode_head(text) if text else {}
        else:
            return {}
    except Exception:
        return {}


def estimate_3mf(path: Path) -> dict:
    """Полный парсер 3MF: все плиты, slice_info, thumbnails."""
    try:
        detail = parse_3mf_complete(path)
    except Exception:
        return {}
    plates = detail.get("plates", [])
    if not plates:
        return {}
    # суммарно по всем плитам, но основное — первая плита для совместимости
    total_minutes = round(sum(p.get("minutes", 0) for p in plates), 1)
    total_grams = round(sum(p.get("grams", 0) for p in plates), 1)
    first = plates[0]
    result = dict(first)
    result["total_minutes"] = total_minutes
    result["total_grams"] = total_grams
    result["plates"] = plates
    result["plate_count"] = len(plates)
    result["thumbnails"] = detail.get("thumbnails", {})
    result["slice_info"] = detail.get("slice_info", {})
    result["project_settings"] = detail.get("project_settings", {})
    # если есть несколько филаментов — собрать все
    all_fils = []
    for p in plates:
        for f in p.get("filaments", []):
            if f not in all_fils:
                all_fils.append(f)
    if all_fils:
        result["filaments"] = all_fils
    return result


def parse_3mf_complete(path: str | Path) -> dict:
    """Достать всё полезное из 3MF-архива.

    Возвращает {plates: [{minutes, grams, material, ...}], thumbnails: {name: base64}, slice_info: {}, project_settings: {}, bounding_box: {}}
    """
    path = Path(path)
    plates: list[dict] = []
    thumbnails: dict[str, str] = {}
    slice_info: dict = {}
    project_settings: dict = {}
    bounding_box: dict = {}
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        # плиты
        for name in sorted(names):
            if name.startswith("Metadata/") and name.endswith(".gcode"):
                try:
                    raw = zf.read(name).decode("utf-8", "ignore")[:300_000]
                except Exception:
                    continue
                data = _parse_gcode_head(raw)
                m = _PLATE_RE.search(name)
                data["plate_index"] = int(m.group(1)) if m else len(plates) + 1
                data["gcode_file"] = name
                plates.append(data)
        # превью
        for name in names:
            if name.startswith("Metadata/") and name.endswith(".png"):
                try:
                    raw = zf.read(name)
                    if len(raw) < 5_000_000:
                        thumbnails[name] = base64.b64encode(raw).decode("ascii")
                except Exception:
                    continue
            if name.startswith("Thumbnail/") and name.lower().endswith(".png"):
                try:
                    raw = zf.read(name)
                    if len(raw) < 2_000_000:
                        thumbnails[name] = base64.b64encode(raw).decode("ascii")
                except Exception:
                    continue
        # slice_info.config
        for cand in ("Metadata/slice_info.config", "Metadata/slice_info.config "):
            if cand.strip() in names:
                try:
                    raw = zf.read(cand.strip()).decode("utf-8", "ignore")
                    # формат может быть json или ini-like
                    try:
                        slice_info = json.loads(raw)
                    except json.JSONDecodeError:
                        slice_info = {"raw": raw[:5000]}
                    break
                except Exception:
                    pass
        # project_settings
        for cand in ("Metadata/project_settings.config", "Metadata/print_settings.config"):
            if cand in names:
                try:
                    raw = zf.read(cand).decode("utf-8", "ignore")
                    try:
                        project_settings = json.loads(raw)
                    except json.JSONDecodeError:
                        project_settings = {"raw": raw[:5000]}
                    break
                except Exception:
                    pass
        # bounding box из 3dmodel.model (приближённо)
        if "3D/3dmodel.model" in names:
            try:
                raw = zf.read("3D/3dmodel.model").decode("utf-8", "ignore")
                # искать <vertex x=".." y=".." z="..">
                import re as _re
                vals = _re.findall(r'x="([^"]+)"\s+y="([^"]+)"\s+z="([^"]+)"', raw)
                if vals:
                    xs = [float(v[0]) for v in vals]
                    ys = [float(v[1]) for v in vals]
                    zs = [float(v[2]) for v in vals]
                    bounding_box = {
                        "min_x": min(xs), "max_x": max(xs),
                        "min_y": min(ys), "max_y": max(ys),
                        "min_z": min(zs), "max_z": max(zs),
                        "size_x": round(max(xs) - min(xs), 1),
                        "size_y": round(max(ys) - min(ys), 1),
                        "size_z": round(max(zs) - min(zs), 1),
                    }
            except Exception:
                pass
    # сортировать плиты по индексу
    plates.sort(key=lambda p: p.get("plate_index", 99))
    return {"plates": plates, "thumbnails": thumbnails, "slice_info": slice_info, "project_settings": project_settings, "bounding_box": bounding_box, "plate_count": len(plates)}


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
    """Достать Metadata/plate_*.gcode из 3MF-архива (совместимость)."""
    with zipfile.ZipFile(path) as zf:
        for name in sorted(zf.namelist()):
            if name.startswith("Metadata/") and name.endswith(".gcode"):
                raw = zf.read(name)
                return raw.decode("utf-8", "ignore")[:200_000]
    return ""


def extract_thumbnails(path: str | Path) -> dict[str, str]:
    """Быстро достать только превью из 3MF (base64)."""
    try:
        return parse_3mf_complete(path).get("thumbnails", {})
    except Exception:
        return {}


def color_distance(a_hex: str, b_hex: str) -> float:
    """Евклидово расстояние между цветами #RRGGBB."""
    try:
        a = a_hex.lstrip("#")[:6].ljust(6, "0")
        b = b_hex.lstrip("#")[:6].ljust(6, "0")
        ar, ag, ab = int(a[0:2], 16), int(a[2:4], 16), int(a[4:6], 16)
        br, bg, bb = int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)
        return ((ar - br) ** 2 + (ag - bg) ** 2 + (ab - bb) ** 2) ** 0.5
    except Exception:
        return 999.0


def auto_ams_map(required: list[dict], trays: list[dict]) -> list[int]:
    """Автоподбор слотов AMS по material+цвет.

    required: [{type: "PLA", color: "#FF0000"}, ...]
    trays: [{slot:0, type:"PLA", color:"#FF0000"}, ...]
    Возвращает [slot_index, ...] или -1 если нет подходящего материала.
    """
    mapping: list[int] = []
    for req in required:
        req_type = str(req.get("type") or req.get("material") or "").upper()
        req_color = str(req.get("color") or "#CCCCCC")
        best = None
        best_score = 9999
        for t in trays:
            t_type = str(t.get("type") or "").upper()
            t_color = str(t.get("color") or "#CCCCCC")
            if t_type != req_type:
                continue
            score = color_distance(req_color, t_color)
            if score < best_score:
                best_score = score
                best = t
        if best is not None:
            mapping.append(int(best.get("slot", 0)))
        else:
            # нет материала — пробуем любой слот с тем же типом, иначе -1
            mapping.append(-1)
    return mapping

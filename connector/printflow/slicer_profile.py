"""Профиль, материалы и аудит собственного слайсера PrintFlow (Stage 1, P1S).

Слой, который отвечает за «можно ли это нарезать», а не за саму нарезку:
геометрия живёт в `slicer_engine.py`, а здесь — физические пределы принтера,
справочник материалов, проверка настроек, аудит модели до нарезки и аудит
G-code после. Устроено так же, как `farmloop.py`: профиль нельзя ослабить
настройкой, а непроверенное состояние всегда блокирует, а не «попробуем».

Движок намеренно не притворяется взрослым слайсером: у него нет переменной
высоты слоя, тонких стен, деревьев-поддержек и мультиматериала. Всё, что
он не умеет, попадает в `unsupported` отчёта, а не маскируется молчанием.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .materials import MATERIALS, get_material
from .slicer import SlicerError
from .slicer_engine import (
    BEGIN,
    END,
    ENGINE_ID,
    ENGINE_VERSION,
    MAX_MODEL_MB,
    MAX_TRIANGLES,
    SliceSettings,
    load_mesh,
    mesh_stats,
)

PROFILE_ID = "bambu-p1s-printflow-stage1"

INFILL_PATTERNS = ("lines", "grid", "triangles", "concentric")
# Заявленные, но не реализованные в Stage 1: движок честно подменяет их.
FALLBACK_PATTERNS = {"gyroid": "grid", "cubic": "grid", "honeycomb": "grid"}
SEAM_MODES = ("nearest", "aligned")


@dataclass(frozen=True)
class SlicerProfile:
    """Физические пределы принтера: их нельзя обойти настройкой."""

    id: str = PROFILE_ID
    printer: str = "Bambu Lab P1S"
    bed_width_mm: float = 256.0
    bed_height_mm: float = 256.0
    max_print_height_mm: float = 256.0
    nozzle_mm: float = 0.4
    filament_mm: float = 1.75
    min_layer_height: float = 0.06
    max_layer_height: float = 0.32
    min_nozzle_temp: int = 170
    max_nozzle_temp: int = 300
    min_bed_temp: int = 0
    max_bed_temp: int = 110
    max_speed_mm_s: float = 500.0
    max_travel_mm_s: float = 500.0
    accel_mm_s2: float = 5000.0
    park_x_mm: float = 10.0
    park_y_mm: float = 246.0
    input_formats: tuple = (".stl",)
    requires_verified_first_print: bool = True


P1S = SlicerProfile()


# ============================================================ материалы


def material_preset(key: str) -> dict:
    """Плотность, температуры и обдув — из общего справочника материалов.

    Своего справочника здесь нет намеренно: если цена или температура
    пластика поменяется в `materials.py`, слайсер подхватит это сам.
    """
    raw_key = str(key or "").strip().upper()
    info = get_material(raw_key) if raw_key else {}
    if not info:
        info = MATERIALS.get(raw_key) or MATERIALS.get("PLA") or {}
    lo_nozzle, hi_nozzle = _range(info.get("temp_nozzle"), 200, 230)
    lo_bed, hi_bed = _range(info.get("temp_bed"), 45, 60)
    return {
        "key": info.get("name") or raw_key or "PLA",
        "density": float(info.get("density") or 1.24),
        "nozzle_min": int(lo_nozzle),
        "nozzle_max": int(hi_nozzle),
        "bed_min": int(lo_bed),
        "bed_max": int(hi_bed),
        "nozzle_temp": _default_nozzle(lo_nozzle, hi_nozzle),
        "bed_temp": int(hi_bed),
        "fan": int(info.get("fan") or 100),
        "speed_factor": float(info.get("speed_factor") or 1.0),
    }


def _range(value: Any, low: float, high: float) -> tuple:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return low, high
    return low, high


def _default_nozzle(low: float, high: float) -> int:
    """Температура по умолчанию: верхняя треть диапазона, с шагом 5 °C."""
    value = low + 0.7 * (high - low)
    return int(round(value / 5.0) * 5)


def material_keys() -> list:
    return sorted(MATERIALS)


# ============================================================ настройки


# Ключ настройки PrintFlow → поле SliceSettings и тип.
SETTING_MAP: dict[str, tuple] = {
    "slicer_layer_height": ("layer_height", float),
    "slicer_first_layer_height": ("first_layer_height", float),
    "slicer_walls": ("walls", int),
    "slicer_infill_percent": ("infill_percent", float),
    "slicer_infill_pattern": ("infill_pattern", str),
    "slicer_supports": ("supports", bool),
    "slicer_support_spacing_mm": ("support_spacing_mm", float),
    "slicer_brim": ("brim", bool),
    "slicer_brim_width": ("brim_width_mm", float),
    "slicer_nozzle_mm": ("nozzle_mm", float),
    "slicer_nozzle_temp": ("nozzle_temp", int),
    "slicer_bed_temp": ("bed_temp", int),
    "slicer_speed_mm_s": ("speed_mm_s", float),
    "slicer_travel_speed_mm_s": ("travel_speed_mm_s", float),
    "slicer_material": ("material", str),
    "slicer_ams_slot": ("ams_slot", str),
    "slicer_extrusion_width_mm": ("extrusion_width_mm", float),
    "slicer_top_solid_layers": ("top_solid_layers", int),
    "slicer_bottom_solid_layers": ("bottom_solid_layers", int),
    "slicer_seam": ("seam", str),
    "slicer_retract_mm": ("retract_mm", float),
    "slicer_retract_speed_mm_s": ("retract_speed_mm_s", float),
    "slicer_retract_min_travel_mm": ("retract_min_travel_mm", float),
    "slicer_zhop_mm": ("zhop_mm", float),
    "slicer_fan_percent": ("fan_percent", int),
    "slicer_flow": ("flow", float),
    "slicer_center_model": ("center_model", bool),
}

def settings_from(raw: dict | None, profile: SlicerProfile = P1S,
                  overrides: dict | None = None) -> SliceSettings:
    """Собрать SliceSettings из настроек PrintFlow и точечных правок запроса.

    Температуры, плотность и обдув берутся из справочника материалов, если
    в настройках стоит 0 (авто). Правки из запроса не могут выйти за
    физические пределы — это проверяет `validate_settings`.
    """
    raw = raw if isinstance(raw, dict) else {}
    values: dict[str, Any] = {}
    for key, (attr, cast) in SETTING_MAP.items():
        if key in raw:
            values[attr] = _cast(raw.get(key), cast)
    for key, value in (overrides or {}).items():
        if value is None or value == "":
            continue
        if key in SETTING_MAP:
            attr, cast = SETTING_MAP[key]
            values[attr] = _cast(value, cast)
        elif hasattr(SliceSettings, key):
            values[key] = value

    material_key = str(values.get("material") or raw.get("slicer_material") or "PLA")
    preset = material_preset(material_key)
    values.setdefault("material", preset["key"])
    if not values.get("nozzle_temp"):
        values["nozzle_temp"] = preset["nozzle_temp"]
    if not values.get("bed_temp"):
        values["bed_temp"] = preset["bed_temp"]
    if not values.get("fan_percent"):
        values["fan_percent"] = preset["fan"]
    if not values.get("density_g_cm3"):
        values["density_g_cm3"] = preset["density"]
    values.setdefault("nozzle_mm", profile.nozzle_mm)
    values.setdefault("filament_mm", profile.filament_mm)
    values.setdefault("accel_mm_s2", profile.accel_mm_s2)
    # Медленный пластик: скорость режется сама, иначе TPU не продавится.
    # В справочнике `speed_factor` — множитель скорости к PLA (TPU 0.25 = в
    # четыре раза медленнее), ровно как его считает калькулятор себестоимости.
    factor = float(preset.get("speed_factor") or 1.0)
    if 0.0 < factor < 1.0:
        base_speed = float(values.get("speed_mm_s", 100.0))
        values["speed_mm_s"] = max(8.0, round(base_speed * factor, 1))
    allowed = {field_name: values[field_name]
               for field_name in SliceSettings.__dataclass_fields__
               if field_name in values}
    return SliceSettings(**allowed)


def _cast(value: Any, cast) -> Any:
    try:
        if cast is bool:
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "да")
            return bool(value)
        if cast is int:
            return int(float(value))
        if cast is float:
            return float(value)
        return str(value)
    except (TypeError, ValueError):
        return cast() if cast is not str else ""


def validate_settings(settings: SliceSettings,
                      profile: SlicerProfile = P1S) -> list:
    """Проверить настройки нарезания. Ошибка — исключение, сомнение — warning.

    Возвращает список предупреждений: они не блокируют нарезку, но
    оператор обязан их видеть до печати (например, «слой толще 0,75 сопла
    для этого сопла не продавится»).
    """
    return _check(settings, profile)


def _check(settings: SliceSettings, profile: SlicerProfile) -> list:
    warnings: list = []
    if not (profile.min_layer_height - 1e-9 <= settings.layer_height
            <= profile.max_layer_height + 1e-9):
        raise SlicerError(
            f"Высота слоя {settings.layer_height} мм вне предела "
            f"{profile.min_layer_height}–{profile.max_layer_height} мм")
    if settings.layer_height > settings.nozzle_mm * 0.75 + 1e-9:
        raise SlicerError(
            f"Слой {settings.layer_height} мм толще 0,75 диаметра сопла "
            f"({settings.nozzle_mm} мм): пластик не продавится")
    if settings.first_layer_height < settings.layer_height - 1e-9:
        raise SlicerError("Первый слой не может быть тоньше обычного")
    if settings.first_layer_height > settings.nozzle_mm * 0.9 + 1e-9:
        raise SlicerError(
            f"Первый слой {settings.first_layer_height} мм не прилипнет к столу "
            f"через сопло {settings.nozzle_mm} мм")
    if settings.walls < 1:
        raise SlicerError("Нужен хотя бы один периметр")
    if not (0 <= settings.infill_percent <= 100):
        raise SlicerError("Заполнение задаётся в процентах: 0–100")
    if settings.infill_pattern not in INFILL_PATTERNS:
        if settings.infill_pattern in FALLBACK_PATTERNS:
            warnings.append(
                f"Узор «{settings.infill_pattern}» в Stage 1 не поддерживается: "
                f"нарезано «{FALLBACK_PATTERNS[settings.infill_pattern]}»")
        else:
            raise SlicerError(
                f"Неизвестный узор заполнения: {settings.infill_pattern}. "
                f"Доступно: {', '.join(INFILL_PATTERNS)}")
    if settings.seam not in SEAM_MODES:
        raise SlicerError(f"Неизвестный режим шва: {settings.seam}")
    if not (profile.min_nozzle_temp <= settings.nozzle_temp <= profile.max_nozzle_temp):
        raise SlicerError(
            f"Температура сопла {settings.nozzle_temp} °C вне предела принтера "
            f"{profile.min_nozzle_temp}–{profile.max_nozzle_temp} °C")
    if not (profile.min_bed_temp <= settings.bed_temp <= profile.max_bed_temp):
        raise SlicerError(
            f"Температура стола {settings.bed_temp} °C вне предела принтера "
            f"{profile.min_bed_temp}–{profile.max_bed_temp} °C")
    preset = material_preset(settings.material)
    if not (preset["nozzle_min"] - 1 <= settings.nozzle_temp <= preset["nozzle_max"] + 1):
        warnings.append(
            f"Температура сопла {settings.nozzle_temp} °C вне диапазона "
            f"{settings.material}: {preset['nozzle_min']}–{preset['nozzle_max']} °C")
    if not (preset["bed_min"] - 3 <= settings.bed_temp <= preset["bed_max"] + 3):
        warnings.append(
            f"Температура стола {settings.bed_temp} °C вне диапазона "
            f"{settings.material}: {preset['bed_min']}–{preset['bed_max']} °C")
    if settings.speed_mm_s <= 0 or settings.speed_mm_s > profile.max_speed_mm_s:
        raise SlicerError(
            f"Скорость {settings.speed_mm_s} мм/с вне предела принтера "
            f"(до {profile.max_speed_mm_s:.0f} мм/с)")
    if settings.travel_speed_mm_s <= 0 or settings.travel_speed_mm_s > profile.max_travel_mm_s:
        raise SlicerError(
            f"Скорость переездов {settings.travel_speed_mm_s} мм/с вне предела "
            f"принтера (до {profile.max_travel_mm_s:.0f} мм/с)")
    if settings.extrusion_width_mm:
        if not (settings.nozzle_mm * 0.6 <= settings.extrusion_width_mm
                <= settings.nozzle_mm * 2.0):
            raise SlicerError(
                f"Ширина экструзии {settings.extrusion_width_mm} мм недопустима "
                f"для сопла {settings.nozzle_mm} мм")
    if not (0.0 <= settings.retract_mm <= 5.0):
        raise SlicerError("Ретракт задаётся в мм: 0–5")
    if settings.retract_speed_mm_s <= 0:
        raise SlicerError("Скорость ретракта должна быть больше нуля")
    if not (0.0 <= settings.zhop_mm <= 5.0):
        raise SlicerError("Подъём по Z задаётся в мм: 0–5")
    if not (0 <= settings.fan_percent <= 100):
        raise SlicerError("Обдув задаётся в процентах: 0–100")
    if not (0.5 <= settings.flow <= 1.5):
        raise SlicerError("Поток задаётся коэффициентом: 0,5–1,5")
    if settings.top_solid_layers < 0 or settings.bottom_solid_layers < 0:
        raise SlicerError("Число сплошных слоёв не может быть отрицательным")
    if settings.support_spacing_mm <= 0:
        raise SlicerError("Шаг поддержек должен быть больше нуля")
    if settings.nozzle_mm <= 0 or settings.filament_mm <= 0:
        raise SlicerError("Диаметр сопла и филамента должны быть больше нуля")
    if settings.walls * settings.ext_width * 2 > min(profile.bed_width_mm,
                                                     profile.bed_height_mm):
        warnings.append("Слишком толстая стенка: деталь может не иметь внутреннего объёма")
    return warnings


# ============================================================ аудит модели


def audit_model(source: str | Path, settings: SliceSettings,
                profile: SlicerProfile = P1S,
                max_mb: float = MAX_MODEL_MB) -> dict:
    """Проверить модель до нарезки: формат, размер, влезаемость, сетка.

    Отказ — исключение `SlicerError`, сомнение — предупреждение в ответе.
    """
    path = Path(str(source)).expanduser()
    mesh = load_mesh(path, max_mb=max_mb)
    stats = mesh_stats(mesh)
    box = stats["bbox"]
    warnings: list = []
    if box["x"] > profile.bed_width_mm + 1e-6 or box["y"] > profile.bed_height_mm + 1e-6:
        raise SlicerError(
            f"Модель {box['x']:.1f} × {box['y']:.1f} мм не влезает в стол "
            f"{profile.bed_width_mm:.0f} × {profile.bed_height_mm:.0f} мм")
    if box["z"] > profile.max_print_height_mm + 1e-6:
        raise SlicerError(
            f"Высота {box['z']:.1f} мм больше предела {profile.max_print_height_mm:.0f} мм")
    if stats["closed"] is False:
        warnings.append("Сетка не замкнута: возможны дыры в стенках")
    if stats["closed"] is None:
        warnings.append("Замкнутость не проверена: модель слишком большая")
    if stats["triangles"] > 200_000:
        warnings.append(
            f"{stats['triangles']} треугольников: нарезка займёт заметное время")
    layers = _layer_count(box["z"], settings.first_layer_height, settings.layer_height)
    if layers <= 0:
        raise SlicerError("Модель без объёма по высоте: нарезать нечего")
    thin = settings.nozzle_mm * 2
    if min(box["x"], box["y"]) < thin:
        warnings.append(
            f"Модель тоньше {thin:.1f} мм: стенки сольются в одну линию")
    fits = (box["x"] <= profile.bed_width_mm and box["y"] <= profile.bed_height_mm
            and box["z"] <= profile.max_print_height_mm)
    return {
        "ok": True,
        "model": path.name,
        "path": str(path),
        "triangles": stats["triangles"],
        "closed": stats["closed"],
        "bbox_mm": [round(box["x"], 2), round(box["y"], 2), round(box["z"], 2)],
        "layers": layers,
        "fits_bed": fits,
        "warnings": warnings,
        "profile": profile.id,
    }


def _layer_count(height: float, first: float, step: float) -> int:
    if height <= 0 or step <= 0:
        return 0
    if height <= first:
        return 1
    return int(max(1, round((height - first) / step)) + 1)


# ============================================================ аудит G-code


def has_slicer_block(text: str) -> bool:
    """Нарезано ли этим движком: полный блок маркеров, а не случайная строка."""
    return BEGIN in text and END in text


_Z_RE = re.compile(r"^\s*G[01]\b.*\bZ(-?\d+(?:\.\d+)?)", re.IGNORECASE | re.MULTILINE)


def audit_gcode(text: str, profile: SlicerProfile = P1S) -> dict:
    """Проверить готовый G-code до того, как он уедет в библиотеку.

    Проверка намеренно жёсткая: файл без температур, без движений или выше
    предела по Z не должен попасть в очередь, даже если слайсер его выдал.
    """
    if not text or not text.strip():
        raise SlicerError("G-code пустой")
    upper = text.upper()
    if "M104" not in upper and "M109" not in upper:
        raise SlicerError("В G-code нет команды температуры сопла")
    if "M140" not in upper and "M190" not in upper:
        raise SlicerError("В G-code нет команды температуры стола")
    moves = [line for line in text.splitlines()
             if line.strip().upper().startswith(("G0 ", "G1 ", "G0\t", "G1\t"))]
    if len(moves) < 10:
        raise SlicerError("В G-code слишком мало движений: файл не похож на нарезку")
    if not any(" E" in line.upper() for line in moves):
        raise SlicerError("В G-code нет выдавливания: печатать нечем")
    z_values = [float(match.group(1)) for match in _Z_RE.finditer(text)]
    height = max(z_values, default=0.0)
    lowest = min(z_values, default=0.0)
    warnings: list = []
    if height > profile.max_print_height_mm + 1e-6:
        raise SlicerError(
            f"Высота печати {height:.1f} мм больше предела "
            f"{profile.max_print_height_mm:.0f} мм")
    if lowest < -1e-6:
        raise SlicerError(f"В G-code есть движение ниже стола: Z{lowest:.3f}")
    if not has_slicer_block(text):
        warnings.append("Файл нарезан не движком PrintFlow: маркеры Stage 1 не найдены")
    estimates = _parse_estimates(text)
    layers = len(re.findall(r"^;LAYER:(\d+)", text, re.MULTILINE))
    return {
        "ok": True,
        "engine_block": has_slicer_block(text),
        "lines": len(text.splitlines()),
        "moves": len(moves),
        "layers": layers,
        "height_mm": round(height, 2),
        "warnings": warnings,
        **estimates,
    }


def _parse_estimates(text: str) -> dict:
    def grab(key: str, cast=float):
        match = re.search(rf"^;\s*{key}:\s*([-\d.]+)", text, re.MULTILINE)
        if not match:
            return 0
        try:
            return cast(match.group(1))
        except ValueError:
            return 0

    return {
        "estimated_weight_g": round(grab("estimated_weight_g"), 2),
        "estimated_time_s": int(grab("estimated_time_s", int)),
        "estimated_filament_mm": round(grab("estimated_filament_mm"), 1),
        "estimated_minutes": round(grab("estimated_time_s", int) / 60.0, 1),
    }


# ============================================================ ответы API


def profile_payload(profile: SlicerProfile = P1S,
                    settings: SliceSettings | None = None) -> dict:
    payload = {
        "id": profile.id,
        "printer": profile.printer,
        "engine": ENGINE_ID,
        "engine_version": ENGINE_VERSION,
        "bed_mm": [profile.bed_width_mm, profile.bed_height_mm],
        "max_print_height_mm": profile.max_print_height_mm,
        "nozzle_mm": profile.nozzle_mm,
        "filament_mm": profile.filament_mm,
        "input_formats": list(profile.input_formats),
        "infill_patterns": list(INFILL_PATTERNS),
        "unsupported_patterns": sorted(FALLBACK_PATTERNS),
        "seam_modes": list(SEAM_MODES),
        "materials": material_keys(),
        "requires_verified_first_print": profile.requires_verified_first_print,
        "limits": {
            "layer_height_mm": [profile.min_layer_height, profile.max_layer_height],
            "nozzle_temp_c": [profile.min_nozzle_temp, profile.max_nozzle_temp],
            "bed_temp_c": [profile.min_bed_temp, profile.max_bed_temp],
            "speed_mm_s": profile.max_speed_mm_s,
            "model_mb": MAX_MODEL_MB,
            "triangles": MAX_TRIANGLES,
        },
    }
    if settings is not None:
        payload["settings"] = settings_payload(settings, profile)
    return payload


def settings_payload(settings: SliceSettings,
                     profile: SlicerProfile = P1S) -> dict:
    return {
        "layer_height": settings.layer_height,
        "first_layer_height": settings.first_layer_height,
        "walls": settings.walls,
        "infill_percent": settings.infill_percent,
        "infill_pattern": settings.infill_pattern,
        "supports": bool(settings.supports),
        "support_spacing_mm": settings.support_spacing_mm,
        "brim": bool(settings.brim),
        "brim_width_mm": settings.brim_width_mm,
        "nozzle_mm": settings.nozzle_mm,
        "extrusion_width_mm": settings.ext_width,
        "nozzle_temp": settings.nozzle_temp,
        "bed_temp": settings.bed_temp,
        "material": settings.material,
        "fan_percent": settings.fan_percent,
        "speed_mm_s": round(settings.speed_mm_s, 1),
        "travel_speed_mm_s": round(settings.travel_speed_mm_s, 1),
        "top_solid_layers": settings.top_solid_layers,
        "bottom_solid_layers": settings.bottom_solid_layers,
        "seam": settings.seam,
        "retract_mm": settings.retract_mm,
        "zhop_mm": settings.zhop_mm,
        "flow": settings.flow,
        "ams_slot": settings.ams_slot,
        "center_model": bool(settings.center_model),
        "density_g_cm3": settings.density_g_cm3,
    }

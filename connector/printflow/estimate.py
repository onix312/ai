"""Оценка печати до запуска: время и граммы из файла задания.

BambuStudio кладёт в начало G-code служебные строки:
    ;TIME:8082            — секунды печати
    ;Filament used [g]: 5.1   — граммы (новый формат)
    ;Filament used: 12.3      — метры (старый формат; пересчитываем в граммы
                               по плотности материала: метр 1.75-мм PLA ≈ 2.98 г)
3MF — это zip, внутри Metadata/plate_1.gcode с тем же форматом.

Оценка нужна до старта: показать «≈ 2 ч 10 мин · ≈ 38 г», зарезервировать
катушку и предупредить, что пластика может не хватить.

8.0 расширяет: парсит все плиты, thumbnails, slice_info, project_settings.
8.2.1: более гибкий парсер — поддержка Orca/Bambu форматов:
  ; total_filament_used: 8.42g
  ; total filament used [g] = 12.3
  ; filament used [g] = 5.1g
  ; estimated_time: 47m 12s
  + разбор Metadata/slice_info.config (XML) — самый надёжный источник
    веса и времени для .3mf/.gcode.3mf (weight/prediction + used_g).
"""
from __future__ import annotations

import base64
import json
import math
import re
import zipfile
from pathlib import Path

from .materials import density_of

# Пластик по умолчанию — 1.75 мм; диаметр можно переопределить настройкой
# filament_diameter_mm, если цех печатает 2.85 мм или калибровал фактический.
DEFAULT_FILAMENT_DIAMETER = 1.75

# --- базовые старые паттерны (совместимость) ---
_TIME_RE = re.compile(r";\s*TIME\s*:\s*(\d+)", re.IGNORECASE)
_GRAMS_RE = re.compile(r";\s*Filament used \[g\]\s*:\s*([\d.]+)", re.IGNORECASE)
_METERS_RE = re.compile(r";\s*Filament used\s*:\s*([\d.]+)", re.IGNORECASE)
_TYPE_RE = re.compile(r";\s*filament[_ ]type\s*[:=]\s*([A-Za-z0-9+\-]+)", re.IGNORECASE)
_COLOR_HEX_RE = re.compile(r";\s*filament[_ ](?:colour|color)\s*[:=]\s*#?([0-9A-Fa-f]{6,8})", re.IGNORECASE)
_BED_RE = re.compile(r";\s*bed[_ ]type\s*[:=]\s*([^\r\n;]+)", re.IGNORECASE)
_NOZZLE_RE = re.compile(r";\s*nozzle[_ ]diamete?r?\s*[:=]\s*([\d.]+)", re.IGNORECASE)
_PLATE_RE = re.compile(r"plate_(\d+)\.gcode", re.IGNORECASE)

# --- расширенные паттерны для современных слайсеров ---
# Время: "estimated_time: 47m 12s", "total estimated time: 1h 20m",
# "estimated printing time (normal mode) = 284m 19s" (Bambu/Orca/Prusa),
# "total estimated time (silent mode) = 1h 20m", "model printing time: 1d 2h",
# "print time = ...", "print_time = ...". В скобках может быть режим (normal/silent/sport).
# Ключевые слова допускают варианты estimated_time/estimated time/print_time/print time.
_TIME_HUMAN_RE = re.compile(
    r";\s*"
    r"(?:total\s+)?"
    r"(?:model\s+)?"
    r"(?:estimated[\s_]?(?:printing[\s_]?)?time|print(?:ing)?[\s_]?time|estimated[\s_]?time)"
    r"(?:\s*\([^)]*\))?"
    r"\s*[:=]\s*"
    r"([^\r\n;]+)",
    re.IGNORECASE,
)
# Альтернативно: "; prediction: 4823" (секунды) из некоторых экспортов
_PREDICTION_RE = re.compile(r";\s*prediction\s*[:=]\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
# Плейсхолдер для отметки того, что паттерн времени уже учитывает режим в скобках
# — оставлен для совместимость с импортами из старых тестов.

# Граммы — много вариантов, все case-insensitive, ":" или "=", с [g] или без, с "g" суффиксом, с "_" или пробелом
# Приоритет: total -> filament
_GRAMS_PATTERNS = [
    # total filament used [g] : 8.42 / = 8.42g
    re.compile(r";\s*total\s+filament\s+used\s*\[g\]\s*[:=]\s*([\d.]+)\s*g?\b", re.IGNORECASE),
    # total filament used: 8.42g
    re.compile(r";\s*total\s+filament\s+used\s*[:=]\s*([\d.]+)\s*g\b", re.IGNORECASE),
    # total_filament_used: 8.42g / = 8.42
    re.compile(r";\s*total_filament_used\s*[:=]\s*([\d.]+)\s*g?\b", re.IGNORECASE),
    # filament used [g] = 5.1 / : 5.1g
    re.compile(r";\s*filament\s+used\s*\[g\]\s*[:=]\s*([\d.]+)\s*g?\b", re.IGNORECASE),
    # filament used: 12.3g (с g — считаем что это граммы, а не метры, если есть g)
    re.compile(r";\s*filament\s+used\s*[:=]\s*([\d.]+)\s*g\b", re.IGNORECASE),
]

# Общий вес модели/детали: некоторые слайсеры (Prusa, Orca) пишут
# «; weight: 92.18g» или «; estimated weight: 92.18g» без слова «filament».
# Это тот же вес в граммах — хороший фолбэк, когда остальные паттерны не сработали.
_WEIGHT_RE = re.compile(r";\s*(?:estimated\s+)?weight\s*[:=]\s*([\d.]+)\s*g\b", re.IGNORECASE)

# Метры / мм фолбэки
_METERS_PATTERNS = [
    re.compile(r";\s*filament\s+used\s*\[mm\]\s*[:=]\s*([\d.]+)", re.IGNORECASE),
    re.compile(r";\s*filament\s+used\s*:\s*([\d.]+)\s*m\b", re.IGNORECASE),  # без [g] и без g суффикса — метры
    re.compile(r";\s*filament\s+used\s*[:=]\s*([\d.]+)\s*mm\b", re.IGNORECASE),
]

_HUMAN_TIME_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([dhms])", re.IGNORECASE)


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


def _parse_human_time_to_minutes(text: str) -> float:
    """Парсит '47m 12s', '1h 20m', '1d 2h', '02:10:30', '1h 20m 30s' -> минуты."""
    if not text:
        return 0.0
    s = text.strip().lower()
    # Формат HH:MM:SS или MM:SS
    if re.match(r"^\d+:\d+(:\d+)?$", s):
        parts = s.split(":")
        try:
            if len(parts) == 3:
                h, m, sec = map(float, parts)
                return round(h * 60 + m + sec / 60, 1)
            elif len(parts) == 2:
                m, sec = map(float, parts)
                return round(m + sec / 60, 1)
        except Exception:
            pass
    # Токены вида 1d 2h 3m 4s
    total_sec = 0.0
    found = False
    for val, unit in _HUMAN_TIME_TOKEN_RE.findall(s):
        try:
            v = float(val)
        except ValueError:
            continue
        found = True
        if unit.lower() == "d":
            total_sec += v * 86400
        elif unit.lower() == "h":
            total_sec += v * 3600
        elif unit.lower() == "m":
            total_sec += v * 60
        elif unit.lower() == "s":
            total_sec += v
    if found and total_sec > 0:
        return round(total_sec / 60.0, 1)
    # Если просто число секунд (>100) — считаем секунды
    # Если число маленькое (<1000) и без единиц — может быть минуты? Но по умолчанию секунды как в TIME
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if m:
        try:
            num = float(m.group(1))
            # Если в строке есть 'm' без 's' и число < 1000, возможно это уже минуты
            # Эвристика: если есть буква 'h' или 'm' в тексте, а токенов не нашли, пробуем другое
            if "h" in s or "m" in s:
                # Если число > 100, скорее секунды
                if num > 300:
                    return round(num / 60.0, 1)
                return round(num, 1)
            # Иначе считаем секунды
            if num > 300:  # >5 мин — явно секунды
                return round(num / 60.0, 1)
        except ValueError:
            pass
    return 0.0


def _extract_grams_from_text(text: str) -> float:
    """Ищет граммы по всем расширенным паттернам. Суммирует per-filament если total не найден.
    Поддерживает строки вида "filament used [g] = 12.34, 3.2" (сумма).
    """
    if not text:
        return 0.0
    # 1) total — приоритет, берём первое найденное значение
    for pat in _GRAMS_PATTERNS[:3]:
        m = pat.search(text)
        if m:
            try:
                # в total может быть список через запятую — суммируем
                # но обычно одно число
                return round(float(m.group(1)), 1)
            except ValueError:
                continue
    # 1b) total через общий разбор строки (на случай "total filament used: 8.42g, 2.1g")
    for line in text.splitlines():
        ll = line.lower()
        if "total" in ll and "filament" in ll and "used" in ll:
            # граммы ли?
            if "[g]" in ll or "total_filament_used" in ll or re.search(r"\d\s*g\b", ll):
                part_match = re.search(r"[:=]\s*(.+)$", line)
                if part_match:
                    part = part_match.group(1)
                    nums = re.findall(r"(\d+(?:\.\d+)?)", part)
                    if nums:
                        try:
                            # если несколько чисел — сумма (редкий случай, но безопасно)
                            vals = [float(n) for n in nums]
                            # эвристика: если первое число >1000 и второе маленькое — это не список
                            # но для total обычно одно
                            if len(vals) == 1:
                                return round(vals[0], 1)
                            # если несколько и все <500 — считаем суммой
                            if all(v < 5000 for v in vals):
                                return round(sum(vals), 1)
                            return round(vals[0], 1)
                        except ValueError:
                            pass

    # 2) все filament used [g] — суммируем по всем строкам и по запятой
    total = 0.0
    found = False
    # сначала через паттерны (быстро)
    for pat in _GRAMS_PATTERNS[3:]:
        for mm in pat.finditer(text):
            try:
                total += float(mm.group(1))
                found = True
            except ValueError:
                continue
    # затем через построчный разбор с запятыми (покрывает "12.34, 3.2")
    for line in text.splitlines():
        ll = line.lower()
        if "filament" not in ll or "used" not in ll:
            continue
        # это граммы?
        is_grams = ("[g]" in ll) or ("total_filament_used" in ll) or re.search(r"filament\s+used\s*\[g\]", ll) or re.search(r"filament\s+used\s*[:=].*g\b", ll)
        # также строка вида "; filament used [g] = 5.1" без g суффикса но с [g] уже покрыта
        # для совместимости: если есть [g] — граммы
        if not is_grams:
            # если строка содержит [g] — уже граммы, иначе проверяем наличие 'g' рядом с числом и отсутствие 'mm'/'m' как метров
            if "[g]" not in ll:
                continue
        # Извлечь часть после : или = — числа паттерны уже учли выше;
        # этот проход оставлен для совместимости, фактическое суммирование
        # выполняется ниже в total2 (см. «пересчёт через все строки»).

    # Пересчёт через все строки с учётом запятых — самый надёжный для многоцвета
    total2 = 0.0
    found2 = False
    for line in text.splitlines():
        ll = line.lower()
        if "filament" not in ll or "used" not in ll:
            continue
        if "[g]" not in ll and "total_filament_used" not in ll and "filament used" in ll:
            # если нет [g] и нет g суффикса — это могут быть метры, пропустим (обработается в meters)
            # но если есть g буква рядом с числом — считаем граммами
            if not re.search(r"\d\s*g\b", ll):
                if "[g]" not in ll:
                    # проверим паттерн total_filament_used без [g] но с g
                    if "total_filament_used" not in ll:
                        continue
        # часть после : =
        m = re.search(r"[:=]\s*(.+)$", line)
        if not m:
            continue
        part = m.group(1)
        part = re.sub(r"\(.*?\)", "", part)
        # если есть запятая — суммируем все числа
        nums = re.findall(r"(\d+(?:\.\d+)?)", part)
        if not nums:
            continue
        # для total — берём первое и выходим (приоритет)
        if "total" in ll:
            try:
                # если уже нашли total выше, не перезаписываем, но этот блок для случая когда total паттерн не сработал
                if not total2:
                    total2 = float(nums[0])
                    found2 = True
                    # если в total несколько чисел через запятую — сумма
                    if "," in part and len(nums) > 1:
                        total2 = sum(float(n) for n in nums)
                    # total найден — возвращаем сразу (приоритет над filament)
                    return round(total2, 1)
            except ValueError:
                continue
        else:
            try:
                # filament used [g] — суммируем все числа в строке
                s = sum(float(n) for n in nums)
                total2 += s
                found2 = True
            except ValueError:
                continue

    if found2 and total2 > 0:
        return round(total2, 1)
    if found and total > 0:
        return round(total, 1)
    m = _GRAMS_RE.search(text)
    if m:
        try:
            return round(float(m.group(1)), 1)
        except ValueError:
            pass
    # Последний фолбэк: общий вес модели («; weight: 92.18g»), когда
    # специфичные «filament used»-строки отсутствуют.
    m = _WEIGHT_RE.search(text)
    if m:
        try:
            return round(float(m.group(1)), 1)
        except ValueError:
            pass
    return 0.0


def _extract_meters_as_grams(text: str, material: str = "",
                             diameter: float = DEFAULT_FILAMENT_DIAMETER) -> float:
    """Фолбэк: метры/мм -> граммы по плотности материала.

    ``material`` берётся из ``; filament_type`` того же файла: метр PETG
    тяжелее метра PLA, и это влияет и на смету, и на списание катушки.
    Значение больше 1000 в «метровом» паттерне — это миллиметры.
    """
    if not text:
        return 0.0
    mat = str(material or "").strip().upper()
    # Сначала ищем mm
    for pat in _METERS_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                val = float(m.group(1))
            except ValueError:
                continue
            # если паттерн был mm — переводим мм -> г; иначе метры -> г,
            # но слишком большое значение в «метрах» на самом деле миллиметры.
            if "mm" in pat.pattern.lower() or val > 1000:
                return mm_to_grams(val, mat, diameter)
            return meters_to_grams(val, mat, diameter)
    # старый паттерн
    m = _METERS_RE.search(text)
    if m:
        try:
            val = float(m.group(1))
        except ValueError:
            return 0.0
        if val > 1000:
            return mm_to_grams(val, mat, diameter)
        return meters_to_grams(val, mat, diameter)
    return 0.0


def _parse_gcode_head(text: str, diameter: float = DEFAULT_FILAMENT_DIAMETER) -> dict:
    minutes = 0.0
    grams = 0.0

    # --- материал нужен раньше граммов: метры пересчитываем по его плотности ---
    material = ""
    m = _TYPE_RE.search(text)
    if m:
        material = m.group(1).strip().upper()

    # --- время ---
    m = _TIME_RE.search(text)
    if m:
        try:
            minutes = round(int(m.group(1)) / 60.0, 1)
        except ValueError:
            pass
    if not minutes:
        m = _PREDICTION_RE.search(text)
        if m:
            try:
                minutes = round(float(m.group(1)) / 60.0, 1)
            except ValueError:
                pass
    if not minutes:
        m = _TIME_HUMAN_RE.search(text)
        if m:
            minutes = _parse_human_time_to_minutes(m.group(1))

    # --- граммы ---
    grams = _extract_grams_from_text(text)
    if not grams:
        grams = _extract_meters_as_grams(text, material, diameter)

    result = {"minutes": minutes, "grams": grams}
    if material:
        result["material"] = material
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
        result["filaments"] = [
            {"type": t.upper(), "color": "#" + (colors[i] if i < len(colors) else "CCCCCC").lstrip("#")[:6]}
            for i, t in enumerate(types)
        ]
    return result


def estimate_file(path: str | Path, diameter: float = DEFAULT_FILAMENT_DIAMETER) -> dict:
    """Оценка файла .gcode или .3mf: {minutes, grams, material, color} или {}.

    8.0: для 3MF возвращает также plates, plate_count, thumbnails.
    """
    path = Path(path)
    if not path.exists():
        return {}
    try:
        if path.suffix.lower() == ".3mf":
            return estimate_3mf(path, diameter)
        elif path.suffix.lower() == ".gcode":
            text = _read_head(path)
            est = _parse_gcode_head(text, diameter) if text else {}
            # 8.5: аудит G-code — слои, высота, скорости (идея 28)
            if est:
                try:
                    from .plate_map import audit_gcode
                    audit = audit_gcode(path)
                    if audit:
                        est["gcode_audit"] = audit
                except Exception:
                    pass
            return est
        else:
            return {}
    except Exception:
        return {}


def estimate_3mf(path: Path, diameter: float = DEFAULT_FILAMENT_DIAMETER) -> dict:
    """Полный парсер 3MF: все плиты, slice_info, thumbnails."""
    try:
        detail = parse_3mf_complete(path, diameter)
    except Exception:
        return {}
    plates = detail.get("plates", [])
    # Если gcode-плиты пустые, но есть данные из slice_info — используем их
    if not plates:
        slice_info = normalize_slice_info(detail.get("slice_info", {}), diameter)
        si_plates = slice_info.get("plates") if isinstance(slice_info, dict) else None
        if si_plates:
            for i, sp in enumerate(si_plates, start=1):
                try:
                    grams = _float(sp.get("weight"))
                    # prediction в секундах
                    minutes = round(_float(sp.get("prediction")) / 60.0, 1)
                    idx = int(sp.get("index") or i)
                    filaments = []
                    for f in sp.get("filaments", []):
                        if not isinstance(f, dict):
                            continue
                        fg = _float(f.get("used_g"))
                        if fg:
                            filaments.append(
                                {
                                    "type": str(f.get("type") or "").upper(),
                                    "color": str(f.get("color") or "#CCCCCC"),
                                    "grams": round(fg, 2),
                                }
                            )
                    # если weight пустой, суммируем used_g
                    if not grams and filaments:
                        grams = round(sum(x.get("grams", 0) for x in filaments), 1)
                    plates.append(
                        {
                            "minutes": minutes,
                            "grams": round(grams, 1) if grams else 0.0,
                            "plate_index": idx,
                            "filaments": filaments,
                            "source": "slice_info",
                        }
                    )
                except Exception:
                    continue
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
    # 8.5: карта плиты — где что лежит и % заполнения (идеи 54, 55)
    try:
        from .plate_map import plate_map_3mf
        pm = plate_map_3mf(path)
        if pm:
            result["plate_map"] = pm
    except Exception:
        pass
    return result


def diameter_from(db) -> float:
    """Диаметр прутка из настроек цеха (по умолчанию 1.75 мм).

    Метры и миллиметры переводятся в граммы через площадь сечения, поэтому
    диаметр влияет на смету так же, как плотность материала.
    """
    try:
        value = float(db.setting("filament_diameter_mm", DEFAULT_FILAMENT_DIAMETER))
    except Exception:
        return DEFAULT_FILAMENT_DIAMETER
    return value if 0 < value < 10 else DEFAULT_FILAMENT_DIAMETER


def _cm3_per_meter(diameter: float = DEFAULT_FILAMENT_DIAMETER) -> float:
    """Объём одного метра прутка, см³.

    Площадь сечения πr² в мм² численно равна объёму метра прутка в см³:
    1 м = 1000 мм, а 1000 мм³ = 1 см³. Для 1.75 мм это 2.405 см³/м.
    """
    try:
        d = float(diameter)
    except (TypeError, ValueError):
        d = DEFAULT_FILAMENT_DIAMETER
    if d <= 0:
        d = DEFAULT_FILAMENT_DIAMETER
    return math.pi * (d / 2.0) ** 2


def meters_to_grams(meters: float, material: str = "PLA",
                    diameter: float = DEFAULT_FILAMENT_DIAMETER) -> float:
    """Метры прутка → граммы с учётом плотности материала.

    Раньше метры пересчитывались константой 1.24 г/м — это плотность PLA,
    а не масса метра: масса метра 1.75-мм PLA равна ≈2.98 г. Оценка
    занижалась в 2.4 раза и не различала материалы.
    """
    try:
        length = float(meters)
    except (TypeError, ValueError):
        return 0.0
    if length <= 0:
        return 0.0
    return round(length * _cm3_per_meter(diameter) * density_of(material), 1)


def mm_to_grams(mm: float, material: str = "PLA",
                diameter: float = DEFAULT_FILAMENT_DIAMETER) -> float:
    """Миллиметры прутка → граммы (тот же пересчёт, длина в мм)."""
    try:
        length = float(mm)
    except (TypeError, ValueError):
        return 0.0
    if length <= 0:
        return 0.0
    return round(length * _cm3_per_meter(diameter) / 1000.0 * density_of(material), 1)


# Плотность пластика по умолчанию для пересчёта метров -> граммы, когда в
# slice_info.config нет used_g, а есть только used_m (PLA ~1.24 г/см³,
# 1.75 мм → ~2.98 мг/мм ≈ 2.98 г/м). Оставлена для совместимости старых
# вызовов; новый код считает через meters_to_grams/mm_to_grams.
_MM_TO_GRAMS_PLA = 0.002976


def _float(value, default: float = 0.0) -> float:
    """Безопасное приведение к float: строки/числа/мусор -> float или default."""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_slice_plate(raw: dict,
                           diameter: float = DEFAULT_FILAMENT_DIAMETER) -> dict | None:
    """Привести одну запись плиты к единому виду.

    Унифицирует разнобой форматов Bambu/Orca/старых сборок:
      - ключ списка филаментов: ``filaments`` или ``filament``;
      - вес плиты: ``weight`` (иногда строка), иначе сумма ``used_g``;
      - если ``used_g`` пуст/0, но есть ``used_m`` — оценить граммы по плотности
        этого пластика (диаметр прутка учитывается);
      - время: ``prediction`` в секундах;
      - индекс плиты: ``index``/``plate_index``/``plate`` (1-based).
    """
    if not isinstance(raw, dict):
        return None
    raw_fils = raw.get("filaments")
    if raw_fils is None:
        raw_fils = raw.get("filament")
    if isinstance(raw_fils, dict):
        # {"1": {...}} или одиночный объект
        try:
            raw_fils = list(raw_fils.values())
        except Exception:
            raw_fils = [raw_fils]
    if not isinstance(raw_fils, list):
        raw_fils = []

    filaments: list[dict] = []
    total_g = 0.0
    for f in raw_fils:
        if not isinstance(f, dict):
            continue
        used_g = _float(f.get("used_g"))
        used_m = _float(f.get("used_m"))
        ftype = str(f.get("type") or f.get("filament_type") or "").strip().upper()
        # Bambu иногда отдаёт used_g=0, а used_m заполнен — пересчитываем
        # по плотности именно этого пластика (метр PETG тяжелее метра PLA).
        if used_g <= 0 and used_m > 0:
            used_g = meters_to_grams(used_m, ftype, diameter)
        if used_g > 0:
            total_g += used_g
        filaments.append({
            "type": str(f.get("type") or f.get("filament_type") or "").strip().upper(),
            "color": str(f.get("color") or f.get("filament_color") or "#CCCCCC").strip() or "#CCCCCC",
            "used_g": round(used_g, 3),
            "used_m": round(used_m, 3),
            "tray_info_idx": str(f.get("tray_info_idx") or ""),
        })

    weight = _float(raw.get("weight"))
    if weight <= 0 and total_g > 0:
        weight = round(total_g, 2)
    prediction = _float(raw.get("prediction"))
    idx = _float(raw.get("index", raw.get("plate_index", raw.get("plate", 0))))
    plate = {
        "index": int(idx) if idx else 0,
        "prediction": prediction,
        "weight": round(weight, 3) if weight else 0.0,
        "minutes": round(prediction / 60.0, 1) if prediction else 0.0,
        "grams": round(weight, 1) if weight else 0.0,
        "filaments": filaments,
    }
    # Сохранить остальные полезные поля как есть (printer_model_id, support_used и т.п.)
    for k, v in raw.items():
        if k not in plate and k not in ("filament", "filaments"):
            plate[k] = v
    return plate


def normalize_slice_info(slice_info: object,
                         diameter: float = DEFAULT_FILAMENT_DIAMETER) -> dict:
    """Привести содержимое ``Metadata/slice_info.config`` к виду ``{"plates": [...]}``.

    Поддерживает:
      * актуальный XML-формат Bambu Studio (``<plate>``), который уже
        возвращает :func:`_parse_slice_info_xml`;
      * старый/JSON-формат с ключом ``plate`` (dict или list);
      * JSON-формат с ключом ``plates``;
      * различные вложенные структуры старых сборок (``plate_1``, ``plate: {}``).
    Ничего не ломает: если структура не распознана, возвращает исходный объект
    как есть под ключом ``raw``.
    """
    if not isinstance(slice_info, dict):
        return {"plates": [], "raw": slice_info}

    # Уже нормализовано XML-парсером?
    if isinstance(slice_info.get("plates"), list) and slice_info["plates"]:
        # Все равно нормализуем каждую запись, чтобы добить единообразие
        plates = []
        for p in slice_info["plates"]:
            np = _normalize_slice_plate(p, diameter)
            if np:
                plates.append(np)
        out = dict(slice_info)
        out["plates"] = plates
        return out

    plate_source = None
    for key in ("plates", "plate", "Plate", "plate_list"):
        if key in slice_info:
            plate_source = slice_info[key]
            break
    if plate_source is None:
        # старый формат: {"plate_count": N, "plate_1": {...}, "plate_2": {...}}
        plate_kv = []
        for k, v in slice_info.items():
            if isinstance(k, str) and k.lower().startswith("plate_") and isinstance(v, dict):
                plate_kv.append((k, v))
        if plate_kv:
            plate_kv.sort(key=lambda kv: kv[0])
            plate_source = [v for _, v in plate_kv]

    plates: list[dict] = []
    if isinstance(plate_source, dict):
        # одиночная плита или словарь { "1": {...}, "2": {...} }
        if any(k.isdigit() for k in plate_source.keys() if isinstance(k, str)):
            ordered = sorted(
                ((k, v) for k, v in plate_source.items() if isinstance(v, dict)),
                key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 999,
            )
            plate_source = [v for _, v in ordered]
        else:
            plate_source = [plate_source]
    if isinstance(plate_source, list):
        for i, p in enumerate(plate_source, start=1):
            np = _normalize_slice_plate(p, diameter)
            if not np:
                continue
            if not np.get("index"):
                np["index"] = i
            plates.append(np)

    out = dict(slice_info)
    out["plates"] = plates
    return out


def _parse_slice_info_xml(raw: str) -> dict:
    """Разобрать Metadata/slice_info.config (XML) в структуру."""
    out: dict = {"raw": raw[:5000], "plates": []}
    if not raw or "<" not in raw:
        return out
    # Попытка через ElementTree
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(raw)
        plates = []
        for plate_elem in root.findall(".//plate"):
            meta = {}
            for md in plate_elem.findall("metadata"):
                k = md.get("key")
                v = md.get("value")
                if k:
                    meta[k] = v
            # также header_item внутри header — не плита, но может содержать версию
            filaments = []
            for fil in plate_elem.findall("filament"):
                # сохранить все атрибуты
                filaments.append(dict(fil.attrib))
            if meta or filaments:
                # нормализуем ключи
                plate_info = dict(meta)
                plate_info["filaments"] = filaments
                plates.append(plate_info)
        if plates:
            out["plates"] = plates
            # также попробуем вытащить версию из header
            for hi in root.findall(".//header_item"):
                if hi.get("key") == "X-BBL-Client-Version":
                    out["slicer_version"] = hi.get("value")
            return out
    except Exception:
        pass

    # Фолбэк: regex парсинг
    try:
        plate_blocks = re.findall(r"<plate>(.*?)</plate>", raw, re.DOTALL | re.IGNORECASE)
        plates = []
        for block in plate_blocks:
            meta = {}
            for k, v in re.findall(
                r'<metadata\s+key="([^"]+)"\s+value="([^"]*)"', block, re.IGNORECASE
            ):
                meta[k] = v
            filaments = []
            for fil_match in re.finditer(r"<filament\s+([^>]+)/?>", block, re.IGNORECASE):
                attr_str = fil_match.group(1)
                attrs = dict(re.findall(r'(\w+)="([^"]*)"', attr_str))
                filaments.append(attrs)
            if meta or filaments:
                meta["filaments"] = filaments
                plates.append(meta)
        if plates:
            out["plates"] = plates
    except Exception:
        pass
    return out


def parse_3mf_complete(path: str | Path,
                       diameter: float = DEFAULT_FILAMENT_DIAMETER) -> dict:
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
        # плиты — парсим gcode заголовки
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
        # slice_info.config — самый надёжный источник веса/времени
        for cand in ("Metadata/slice_info.config", "Metadata/slice_info.config "):
            if cand.strip() in names:
                try:
                    raw = zf.read(cand.strip()).decode("utf-8", "ignore")
                    # формат XML, но может быть json в старых версиях
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        parsed = _parse_slice_info_xml(raw)
                    slice_info = normalize_slice_info(parsed)
                    break
                except Exception:
                    pass
        # дополнить плиты данными из slice_info если gcode дал 0
        if slice_info and isinstance(slice_info, dict) and slice_info.get("plates"):
            si_by_index = {}
            for sp in slice_info["plates"]:
                try:
                    idx = int(sp.get("index") or 0)
                    if idx:
                        si_by_index[idx] = sp
                except Exception:
                    continue
            for p in plates:
                idx = p.get("plate_index")
                si = si_by_index.get(idx) if idx else None
                if not si:
                    continue
                # если граммы 0 — берём из slice_info
                if not p.get("grams"):
                    w = _float(si.get("weight"))
                    if w:
                        p["grams"] = round(w, 1)
                    else:
                        # суммируем used_g по филаментам (нормализатор уже пересчитал used_m)
                        total_g = 0.0
                        for f in si.get("filaments", []):
                            total_g += _float(f.get("used_g") if isinstance(f, dict) else 0)
                        if total_g:
                            p["grams"] = round(total_g, 1)
                if not p.get("minutes"):
                    pred = _float(si.get("prediction"))
                    if pred:
                        p["minutes"] = round(pred / 60.0, 1)
                # филаменты из slice_info если нет
                if not p.get("filaments") and si.get("filaments"):
                    try:
                        fils = []
                        for f in si["filaments"]:
                            if not isinstance(f, dict):
                                continue
                            fils.append(
                                {
                                    "type": str(f.get("type") or "").upper(),
                                    "color": str(f.get("color") or "#CCCCCC"),
                                    "grams": round(_float(f.get("used_g")), 2),
                                }
                            )
                        if fils:
                            p["filaments"] = fils
                            # также material/color для совместимости — первый филамент
                            if not p.get("material") and fils[0].get("type"):
                                p["material"] = fils[0]["type"]
                            if not p.get("color_hex") and fils[0].get("color"):
                                p["color_hex"] = fils[0]["color"]
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
                        "min_x": min(xs),
                        "max_x": max(xs),
                        "min_y": min(ys),
                        "max_y": max(ys),
                        "min_z": min(zs),
                        "max_z": max(zs),
                        "size_x": round(max(xs) - min(xs), 1),
                        "size_y": round(max(ys) - min(ys), 1),
                        "size_z": round(max(zs) - min(zs), 1),
                    }
            except Exception:
                pass
    # сортировать плиты по индексу
    plates.sort(key=lambda p: p.get("plate_index", 99))
    return {
        "plates": plates,
        "thumbnails": thumbnails,
        "slice_info": slice_info,
        "project_settings": project_settings,
        "bounding_box": bounding_box,
        "plate_count": len(plates),
    }


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


def auto_ams_map(required: list[dict], trays: list[dict], db=None,
                 printer_id: str = "") -> list[int]:
    """Автоподбор слотов AMS: сначала свои катушки, затем по material+цвет.

    required: [{type: "PLA", color: "#FF0000", grams: 40}, ...]
    trays: [{slot:0, type:"PLA", color:"#FF0000", uuid:"..."} , ...]

    Раньше слот выбирался только по типу и оттенку: два одинаковых чёрных
    PLA в соседних слотах были неразличимы, и печать могла уйти на катушку,
    которой в базе нет или которой не хватит. Теперь слот, в котором стоит
    известная нам катушка (совпадение по RFID-метке ``tray_uuid``), получает
    приоритет над «просто похожим цветом»: учёт расхода и остатка остаётся
    честным. Один и тот же слот не предлагается дважды.

    Возвращает [slot_index, ...] или -1 если нет подходящего материала.
    """
    mapping: list[int] = []

    known: dict[str, dict] = {}
    prefer_known = True
    if db is not None:
        try:
            prefer_known = bool(db.setting("ams_map_prefer_known", True))
            if prefer_known:
                rows = db.query(
                    "SELECT * FROM spools WHERE archived=0"
                    " AND COALESCE(tray_uuid,'')<>''")
                for row in rows:
                    key = str(row.get("tray_uuid") or "").strip()
                    if key and set(key) != {"0"}:
                        known[key] = row
        except Exception:
            known = {}

    for req in required:
        req_type = str(req.get("type") or req.get("material") or "").upper()
        req_color = str(req.get("color") or "#CCCCCC")
        req_grams = 0.0
        try:
            req_grams = float(req.get("grams") or 0.0) or 0.0
        except (TypeError, ValueError):
            req_grams = 0.0
        best = None
        best_score = None
        for t in trays:
            t_type = str(t.get("type") or "").upper()
            t_color = str(t.get("color") or "#CCCCCC")
            if t_type != req_type:
                continue
            slot = int(t.get("slot", 0))
            score = color_distance(req_color, t_color)
            if known:
                spool = known.get(str(t.get("uuid") or "").strip())
                if spool is not None:
                    # Своя катушка: приоритет важнее оттенка. Непроверенную и
                    # пустую почти не рассматриваем — по ней нельзя списать расход.
                    score -= 10000.0
                    if int(float(spool.get("verified") or 0)):
                        score -= 500.0
                    left = float(spool.get("remaining_grams") or 0)
                    if req_grams and left < req_grams:
                        score += 5000.0
                    elif left > 0:
                        score -= 100.0
                else:
                    # В слоте катушка, которой нет в базе: расход будет «ничей».
                    score += 2000.0
            if slot in mapping:
                # Слот уже отдан предыдущему филаменту — только если больше нечего.
                score += 50000.0
            if best_score is None or score < best_score:
                best_score = score
                best = t
        if best is not None:
            mapping.append(int(best.get("slot", 0)))
        else:
            mapping.append(-1)
    return mapping

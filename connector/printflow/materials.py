"""Справочник материалов для 3D-печати: свойства, цены, рекомендации.

Единый источник правды о пластиках — калькулятор, склад, планировщик
и рекомендации используют одни и те же данные, а не хардкодят значения
в разных местах.

Данные собраны из практики Bambu Lab P1S (2026) и справочника
docs/ТЕХНИКА-ПЕЧАТИ.md. Цены — ориентир розницы РФ; в калькуляторе
используется фактическая цена катушки из склада.
"""
from __future__ import annotations

from typing import Any

# ──────────────────────────────────────────────────────────────────────
# Справочник: один материал — все его свойства
# ──────────────────────────────────────────────────────────────────────
# speed_factor — во сколько раз медленнее PLA (PLA = 1.0):
#   TPU печатается в 3–5 раз медленнее, ABS чуть медленнее PLA.
# density — г/см³, для пересчёта объёма в вес.
# support_factor — типичная доля поддержек в весе модели (0 = почти нет,
#   0.3 = много нависаний). Зависит от геометрии, это только ориентир.
# price_per_kg — ориентир розничной цены РФ, 2026.
# temp_nozzle / temp_bed / chamber — температуры для BambuStudio.
# dry_temp / dry_hours — сушка перед печатью.
# shrinkage — усадка в %, для допуска размеров.
# strengths / weaknesses — для подсказок в калькуляторе.
# use_cases — типичные изделия NOZZA.

MATERIALS: dict[str, dict[str, Any]] = {
    "PLA": {
        "name": "PLA",
        "full_name": "Полилактид (PLA)",
        "density": 1.24,
        "speed_factor": 1.0,
        "support_factor": 0.10,
        "price_per_kg": 1550,
        "temp_nozzle": (200, 230),
        "temp_bed": (45, 60),
        "chamber": "open",
        "fan": 100,
        "shrinkage": 0.25,
        "dry_temp": 50,
        "dry_hours": 5,
        "heat_resistance": 58,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Лучшая детализация, самый дешёвый, без запаха",
        "weaknesses": "Хрупкий, боится нагрева >55°C и солнца",
        "use_cases": "Декор, образцы, топперы, подставки, витринные таблички",
    },
    "PLA+": {
        "name": "PLA+",
        "full_name": "PLA Tough / PLA+",
        "density": 1.24,
        "speed_factor": 1.0,
        "support_factor": 0.10,
        "price_per_kg": 1850,
        "temp_nozzle": (210, 235),
        "temp_bed": (50, 60),
        "chamber": "open",
        "fan": 90,
        "shrinkage": 0.25,
        "dry_temp": 50,
        "dry_hours": 5,
        "heat_resistance": 60,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Вязче обычного PLA, не колется",
        "weaknesses": "Чуть дороже, те же ограничения по температуре",
        "use_cases": "Брелоки, органайзеры для сухих помещений",
    },
    "PLA_SILK": {
        "name": "PLA Silk",
        "full_name": "PLA шёлк (Silk)",
        "density": 1.24,
        "speed_factor": 0.85,
        "support_factor": 0.10,
        "price_per_kg": 2100,
        "temp_nozzle": (215, 240),
        "temp_bed": (50, 60),
        "chamber": "open",
        "fan": 80,
        "shrinkage": 0.25,
        "dry_temp": 50,
        "dry_hours": 5,
        "heat_resistance": 55,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Красивый блеск, отлично для подарков и фото",
        "weaknesses": "Хрупкий, слоится, хуже детализация",
        "use_cases": "Подарочные изделия, витрина, фотосъёмка",
    },
    "PLA_MATTE": {
        "name": "PLA Matte",
        "full_name": "PLA матовый",
        "density": 1.24,
        "speed_factor": 1.0,
        "support_factor": 0.10,
        "price_per_kg": 1950,
        "temp_nozzle": (200, 230),
        "temp_bed": (45, 60),
        "chamber": "open",
        "fan": 100,
        "shrinkage": 0.25,
        "dry_temp": 50,
        "dry_hours": 5,
        "heat_resistance": 58,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Без бликов, скрывает слои, приятная текстура",
        "weaknesses": "Те же ограничения PLA",
        "use_cases": "Ценники, номерки, таблички",
    },
    "PETG": {
        "name": "PETG",
        "full_name": "Полиэтилентерефталат-гликоль (PETG)",
        "density": 1.27,
        "speed_factor": 0.80,
        "support_factor": 0.15,
        "price_per_kg": 1900,
        "temp_nozzle": (230, 260),
        "temp_bed": (70, 85),
        "chamber": "closed",
        "fan": 45,
        "shrinkage": 0.50,
        "dry_temp": 63,
        "dry_hours": 7,
        "heat_resistance": 73,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Вязкий, ударопрочный, не боится воды и тепла",
        "weaknesses": "Стрингинг, мутнеет на солнце за сезон",
        "use_cases": "Адресники, держатели, крепления, кухня, ванная",
    },
    "TPU": {
        "name": "TPU 95A",
        "full_name": "Термополиуретан (TPU 95A)",
        "density": 1.21,
        "speed_factor": 0.25,
        "support_factor": 0.05,
        "price_per_kg": 3250,
        "temp_nozzle": (220, 245),
        "temp_bed": (35, 50),
        "chamber": "closed",
        "fan": 45,
        "shrinkage": 0.75,
        "dry_temp": 65,
        "dry_hours": 10,
        "heat_resistance": 75,
        "uv_resistant": True,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Резиноподобный, тянется до 400%, не трескается",
        "weaknesses": "Очень медленная печать, без AMS, без боудена",
        "use_cases": "Ножки, прокладки, вставки, фиксаторы, чехлы",
    },
    "ABS": {
        "name": "ABS",
        "full_name": "Акрилонитрилбутадиенстирол (ABS)",
        "density": 1.04,
        "speed_factor": 0.85,
        "support_factor": 0.20,
        "price_per_kg": 1850,
        "temp_nozzle": (250, 270),
        "temp_bed": (90, 100),
        "chamber": "closed_hot",
        "fan": 10,
        "shrinkage": 0.80,
        "dry_temp": 75,
        "dry_hours": 5,
        "heat_resistance": 95,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Теплостойкий 95°C, вязкий, растворим в ацетоне",
        "weaknesses": "Сильный запах, усадка, коробление, желтеет на солнце",
        "use_cases": "Корпуса, оснастка, детали в помещении",
    },
    "ASA": {
        "name": "ASA",
        "full_name": "Акрилонитрилстиролакрилат (ASA)",
        "density": 1.07,
        "speed_factor": 0.85,
        "support_factor": 0.20,
        "price_per_kg": 2200,
        "temp_nozzle": (250, 280),
        "temp_bed": (90, 105),
        "chamber": "closed_hot",
        "fan": 10,
        "shrinkage": 0.80,
        "dry_temp": 75,
        "dry_hours": 5,
        "heat_resistance": 100,
        "uv_resistant": True,
        "food_safe": False,
        "abrasive": False,
        "strengths": "УФ-стойкий, для улицы, теплостойкий 100°C",
        "weaknesses": "Сильный запах, усадка, закрытая камера обязательна",
        "use_cases": "Уличные таблички, номера домов, вывески",
    },
    "PC": {
        "name": "PC",
        "full_name": "Поликарбонат (PC)",
        "density": 1.20,
        "speed_factor": 0.70,
        "support_factor": 0.20,
        "price_per_kg": 4000,
        "temp_nozzle": (260, 290),
        "temp_bed": (90, 110),
        "chamber": "closed_hot",
        "fan": 5,
        "shrinkage": 0.70,
        "dry_temp": 80,
        "dry_hours": 10,
        "heat_resistance": 125,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Самая высокая ударная прочность, прозрачный",
        "weaknesses": "Гигроскопичен, склонен к расслоению, дорогой",
        "use_cases": "Прозрачные и теплонагруженные детали",
    },
    "PAHT_CF": {
        "name": "PAHT-CF",
        "full_name": "Нейлон + углеволокно (PAHT-CF)",
        "density": 1.10,
        "speed_factor": 0.70,
        "support_factor": 0.15,
        "price_per_kg": 7500,
        "temp_nozzle": (280, 300),
        "temp_bed": (90, 100),
        "chamber": "closed_hot",
        "fan": 10,
        "shrinkage": 0.45,
        "dry_temp": 85,
        "dry_hours": 10,
        "heat_resistance": 140,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": True,
        "strengths": "Жёсткий, износостойкий, теплостойкий 140°C",
        "weaknesses": "Абразивный (нужно закалённое сопло), гигроскопичен",
        "use_cases": "Втулки, шестерни, силовые кронштейны",
    },
    "PET_CF": {
        "name": "PET-CF",
        "full_name": "PET + углеволокно (PET-CF)",
        "density": 1.30,
        "speed_factor": 0.70,
        "support_factor": 0.15,
        "price_per_kg": 5750,
        "temp_nozzle": (270, 300),
        "temp_bed": (80, 100),
        "chamber": "closed",
        "fan": 30,
        "shrinkage": 0.30,
        "dry_temp": 75,
        "dry_hours": 8,
        "heat_resistance": 100,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": True,
        "strengths": "Очень жёсткий, меньше усадка чем PA-CF",
        "weaknesses": "Хрупче PETG, абразивный",
        "use_cases": "Жёсткие кронштейны, оснастка",
    },
    "PLA_CF": {
        "name": "PLA-CF",
        "full_name": "PLA + углеволокно (PLA-CF)",
        "density": 1.22,
        "speed_factor": 0.90,
        "support_factor": 0.10,
        "price_per_kg": 3250,
        "temp_nozzle": (210, 240),
        "temp_bed": (45, 60),
        "chamber": "open",
        "fan": 90,
        "shrinkage": 0.15,
        "dry_temp": 55,
        "dry_hours": 6,
        "heat_resistance": 60,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": True,
        "strengths": "Жёсткий, красивая текстура, низкая усадка",
        "weaknesses": "Хрупкий, абразивный",
        "use_cases": "Жёсткий декор и корпуса без нагрева",
    },
    "HIPS": {
        "name": "HIPS",
        "full_name": "Ударопрочный полистирол (HIPS)",
        "density": 1.04,
        "speed_factor": 0.85,
        "support_factor": 0.25,
        "price_per_kg": 2200,
        "temp_nozzle": (230, 250),
        "temp_bed": (90, 100),
        "chamber": "closed",
        "fan": 15,
        "shrinkage": 0.70,
        "dry_temp": 63,
        "dry_hours": 4,
        "heat_resistance": 90,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Растворим в лимонене — поддержка для ABS",
        "weaknesses": "Только как поддержка или для прототипов",
        "use_cases": "Растворимая поддержка для ABS",
    },
    "PVA": {
        "name": "PVA",
        "full_name": "Поливиниловый спирт (PVA)",
        "density": 1.23,
        "speed_factor": 0.50,
        "support_factor": 0.30,
        "price_per_kg": 5500,
        "temp_nozzle": (195, 225),
        "temp_bed": (45, 60),
        "chamber": "closed",
        "fan": 70,
        "shrinkage": 0.0,
        "dry_temp": 45,
        "dry_hours": 8,
        "heat_resistance": 0,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Растворим в воде — идеальная поддержка для PLA/PETG",
        "weaknesses": "Дорогой, крайне гигроскопичен, только поддержка",
        "use_cases": "Растворимая поддержка для сложных геометрий",
    },
}

# ──────────────────────────────────────────────────────────────────────
# Профили качества печати: множитель времени и расхода
# ──────────────────────────────────────────────────────────────────────

QUALITY_PROFILES: dict[str, dict[str, Any]] = {
    "draft": {
        "name": "Черновой",
        "layer_height": "0.24–0.28 мм",
        "walls": 2,
        "infill": 12,
        "time_factor": 0.60,
        "filament_factor": 0.85,
        "description": "Проверка размеров и прототипы. Быстро, но видно слои.",
    },
    "standard": {
        "name": "Стандарт",
        "layer_height": "0.20 мм",
        "walls": 3,
        "infill": 18,
        "time_factor": 1.00,
        "filament_factor": 1.00,
        "description": "Большинство товаров. Баланс скорости и качества.",
    },
    "detail": {
        "name": "Детальный",
        "layer_height": "0.12–0.16 мм",
        "walls": 3,
        "infill": 18,
        "time_factor": 1.80,
        "filament_factor": 1.05,
        "description": "Текст, подарочные изделия, мелкие детали. В 1.8× дольше.",
    },
    "strong": {
        "name": "Прочный",
        "layer_height": "0.20 мм",
        "walls": 5,
        "infill": 40,
        "time_factor": 1.30,
        "filament_factor": 1.35,
        "description": "Функциональные детали под нагрузкой. Больше стенок и заполнения.",
    },
}


def get_material(key: str) -> dict[str, Any]:
    """Материал по ключу (PLA, PETG, ...). Неизвестный → PLA."""
    key = (key or "").strip().upper().replace(" ", "_")
    if key in MATERIALS:
        return MATERIALS[key]
    # Простые алиасы
    aliases = {
        "PLA TOUGH": "PLA+",
        "PLA SILK": "PLA_SILK",
        "PLA MATTE": "PLA_MATTE",
        "TPU95A": "TPU",
        "TPU 95": "TPU",
        "NYLON CF": "PAHT_CF",
        "PA CF": "PAHT_CF",
        "PA-CF": "PAHT_CF",
        "PET CF": "PET_CF",
        "PET-CF": "PET_CF",
        "PLA CF": "PLA_CF",
        "PLA-CF": "PLA_CF",
    }
    return MATERIALS.get(aliases.get(key, ""), MATERIALS["PLA"])


def get_profile(key: str) -> dict[str, Any]:
    """Профиль качества по ключу. Неизвестный → стандарт."""
    return QUALITY_PROFILES.get((key or "standard").strip().lower(),
                                QUALITY_PROFILES["standard"])


def material_list() -> list[dict[str, Any]]:
    """Плоский список для выпадающего меню в калькуляторе."""
    return [
        {
            "key": k,
            "name": m["name"],
            "full_name": m["full_name"],
            "price_per_kg": m["price_per_kg"],
            "density": m["density"],
            "speed_factor": m["speed_factor"],
            "heat_resistance": m["heat_resistance"],
            "uv_resistant": m["uv_resistant"],
            "abrasive": m["abrasive"],
            "strengths": m["strengths"],
            "weaknesses": m["weaknesses"],
            "use_cases": m["use_cases"],
        }
        for k, m in MATERIALS.items()
    ]


def profile_list() -> list[dict[str, Any]]:
    """Плоский список профилей качества для калькулятора."""
    return [
        {"key": k, **p}
        for k, p in QUALITY_PROFILES.items()
    ]


def recommend_material(use_case: str = "") -> list[str]:
    """Рекомендация материала по назначению (простой keyword-матчер)."""
    use = (use_case or "").lower()
    if any(w in use for w in ("уличн", "улиц", "sun", "uv", "вывеск", "номер дом", "улица")):
        return ["ASA", "PETG"]
    if any(w in use for w in ("гибк", "рези", "ножк", "проклад", "чехол")):
        return ["TPU"]
    if any(w in use for w in ("шестер", "втулк", "нагруз", "силов", "кронштейн")):
        return ["PAHT_CF", "PET_CF"]
    if any(w in use for w in ("прозрач", "окно", "световод")):
        return ["PETG", "PC"]
    if any(w in use for w in ("корпус", "нагрев", "горяч", "автомобил")):
        return ["ABS", "ASA"]
    if any(w in use for w in ("адресник", "бирк", "брелок", "питом", "вод")):
        return ["PETG", "PLA+"]
    return ["PLA", "PLA_MATTE"]

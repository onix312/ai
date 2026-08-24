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

from .config import now_iso

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
    "BVOH": {
        "name": "BVOH",
        "full_name": "Бутандиол-виниловый сополимер (BVOH)",
        "density": 1.14,
        "speed_factor": 0.50,
        "support_factor": 0.30,
        "price_per_kg": 9000,
        "temp_nozzle": (190, 220),
        "temp_bed": (30, 60),
        "chamber": "open",
        "fan": 60,
        "shrinkage": 0.0,
        "dry_temp": 45,
        "dry_hours": 6,
        "heat_resistance": 0,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Растворим в воде быстрее PVA, липнет к большинству пластиков",
        "weaknesses": "Очень дорогой, крайне гигроскопичен",
        "use_cases": "Растворимая поддержка для сложных геометрий и внутренних полостей",
    },
    "PLA_WOOD": {
        "name": "PLA Wood",
        "full_name": "Древеснонаполненный PLA (Wood)",
        "density": 1.18,
        "speed_factor": 0.80,
        "support_factor": 0.10,
        "price_per_kg": 2400,
        "temp_nozzle": (195, 220),
        "temp_bed": (45, 60),
        "chamber": "open",
        "fan": 90,
        "shrinkage": 0.30,
        "dry_temp": 50,
        "dry_hours": 6,
        "heat_resistance": 55,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Выглядит как дерево: шлифуется, морится, пахнет деревом",
        "weaknesses": "Забивает сопло при длинной печати, хрупкий",
        "use_cases": "Декор, таблички, подставки под дерево",
    },
    "PLA_MARBLE": {
        "name": "PLA Marble",
        "full_name": "Мраморный PLA с минеральным наполнителем",
        "density": 1.30,
        "speed_factor": 0.80,
        "support_factor": 0.10,
        "price_per_kg": 2300,
        "temp_nozzle": (195, 225),
        "temp_bed": (45, 60),
        "chamber": "open",
        "fan": 90,
        "shrinkage": 0.30,
        "dry_temp": 50,
        "dry_hours": 6,
        "heat_resistance": 55,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Скрывает слои, вид мрамора без постобработки",
        "weaknesses": "Минерал слегка абразивен для латунного сопла",
        "use_cases": "Вазы, декор, сувениры",
    },
    "PLA_GLOW": {
        "name": "PLA Glow",
        "full_name": "Светящийся в темноте PLA",
        "density": 1.30,
        "speed_factor": 0.70,
        "support_factor": 0.10,
        "price_per_kg": 3200,
        "temp_nozzle": (195, 225),
        "temp_bed": (45, 60),
        "chamber": "open",
        "fan": 90,
        "shrinkage": 0.30,
        "dry_temp": 50,
        "dry_hours": 6,
        "heat_resistance": 55,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": True,
        "strengths": "Светится после зарядки светом — эффектный декор",
        "weaknesses": "Люминофор быстро стачивает латунное сопло — нужно закалённое",
        "use_cases": "Ночники, брелоки, декор для детей",
    },
    "PLA_METAL": {
        "name": "PLA Metal",
        "full_name": "Металлонаполненный PLA (бронза/медь/сталь)",
        "density": 1.70,
        "speed_factor": 0.60,
        "support_factor": 0.10,
        "price_per_kg": 3800,
        "temp_nozzle": (200, 230),
        "temp_bed": (45, 60),
        "chamber": "open",
        "fan": 90,
        "shrinkage": 0.30,
        "dry_temp": 50,
        "dry_hours": 6,
        "heat_resistance": 55,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": True,
        "strengths": "Настоящий металлический вес и вид, полируется",
        "weaknesses": "Сильно абразивный, хрупкий, тяжёлый",
        "use_cases": "Брелоки, ювелирные прототипы, сувениры",
    },
    "PET": {
        "name": "PET",
        "full_name": "Полиэтилентерефталат (PET)",
        "density": 1.30,
        "speed_factor": 0.75,
        "support_factor": 0.15,
        "price_per_kg": 2100,
        "temp_nozzle": (240, 260),
        "temp_bed": (70, 85),
        "chamber": "closed",
        "fan": 40,
        "shrinkage": 0.60,
        "dry_temp": 65,
        "dry_hours": 6,
        "heat_resistance": 75,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Прозрачнее PETG, прочнее, лёгкий блеск",
        "weaknesses": "Капризнее PETG: налипает на сопло, требует сушки",
        "use_cases": "Прозрачные детали, бутылочные формы",
    },
    "PETG_CF": {
        "name": "PETG-CF",
        "full_name": "PETG + углеволокно (PETG-CF)",
        "density": 1.28,
        "speed_factor": 0.70,
        "support_factor": 0.15,
        "price_per_kg": 4800,
        "temp_nozzle": (250, 270),
        "temp_bed": (70, 85),
        "chamber": "closed",
        "fan": 40,
        "shrinkage": 0.40,
        "dry_temp": 65,
        "dry_hours": 8,
        "heat_resistance": 85,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": True,
        "strengths": "Жёсткость углеволокна + ударная вязкость PETG",
        "weaknesses": "Абразивный, дороже PETG",
        "use_cases": "Кронштейны, оснастка, корпуса под нагрузкой",
    },
    "PETG_ESD": {
        "name": "PETG ESD",
        "full_name": "Антистатический PETG (ESD-safe)",
        "density": 1.27,
        "speed_factor": 0.80,
        "support_factor": 0.15,
        "price_per_kg": 4200,
        "temp_nozzle": (240, 260),
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
        "strengths": "Отводит статику — безопасен для электроники",
        "weaknesses": "Дороже обычного PETG, матовый",
        "use_cases": "Корпуса электроники, ложементы плат",
    },
    "ABS_GF": {
        "name": "ABS-GF",
        "full_name": "ABS + стекловолокно (ABS-GF)",
        "density": 1.12,
        "speed_factor": 0.80,
        "support_factor": 0.20,
        "price_per_kg": 2600,
        "temp_nozzle": (255, 275),
        "temp_bed": (90, 105),
        "chamber": "closed_hot",
        "fan": 10,
        "shrinkage": 0.60,
        "dry_temp": 75,
        "dry_hours": 5,
        "heat_resistance": 100,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": True,
        "strengths": "Жёстче ABS, меньше коробление",
        "weaknesses": "Абразивный, запах как у ABS",
        "use_cases": "Оснастка, корпуса, крепления в тепле",
    },
    "PC_ABS": {
        "name": "PC-ABS",
        "full_name": "Смесь поликарбоната и ABS (PC-ABS)",
        "density": 1.10,
        "speed_factor": 0.75,
        "support_factor": 0.20,
        "price_per_kg": 3000,
        "temp_nozzle": (260, 280),
        "temp_bed": (90, 105),
        "chamber": "closed_hot",
        "fan": 10,
        "shrinkage": 0.60,
        "dry_temp": 80,
        "dry_hours": 8,
        "heat_resistance": 110,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Автомобильный стандарт: ударопрочность PC + технологичность ABS",
        "weaknesses": "Запах, сушка обязательна",
        "use_cases": "Корпуса, салонные детали, инструмент",
    },
    "PA": {
        "name": "PA (Nylon)",
        "full_name": "Полиамид / нейлон PA6/PA12",
        "density": 1.13,
        "speed_factor": 0.70,
        "support_factor": 0.15,
        "price_per_kg": 3200,
        "temp_nozzle": (260, 290),
        "temp_bed": (80, 100),
        "chamber": "closed",
        "fan": 15,
        "shrinkage": 1.00,
        "dry_temp": 80,
        "dry_hours": 10,
        "heat_resistance": 110,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Самый износостойкий без наполнителя, скользит, гнётся",
        "weaknesses": "Впитывает воду, коробится, обязательна сушка",
        "use_cases": "Шестерни, втулки, шарниры, хомуты",
    },
    "PA_GF": {
        "name": "PA-GF",
        "full_name": "Нейлон + стекловолокно (PA-GF)",
        "density": 1.30,
        "speed_factor": 0.70,
        "support_factor": 0.15,
        "price_per_kg": 4200,
        "temp_nozzle": (270, 300),
        "temp_bed": (80, 100),
        "chamber": "closed",
        "fan": 15,
        "shrinkage": 0.60,
        "dry_temp": 80,
        "dry_hours": 10,
        "heat_resistance": 130,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": True,
        "strengths": "Жёсткий нейлон, меньше усадка",
        "weaknesses": "Абразивный, гигроскопичный",
        "use_cases": "Силовые кронштейны, оснастка",
    },
    "PPA_CF": {
        "name": "PPA-CF",
        "full_name": "Полифталамид + углеволокно (PPA-CF)",
        "density": 1.10,
        "speed_factor": 0.65,
        "support_factor": 0.15,
        "price_per_kg": 8500,
        "temp_nozzle": (280, 300),
        "temp_bed": (90, 110),
        "chamber": "closed_hot",
        "fan": 10,
        "shrinkage": 0.40,
        "dry_temp": 85,
        "dry_hours": 10,
        "heat_resistance": 160,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": True,
        "strengths": "Держит 160°C, очень жёсткий — почти замена металла",
        "weaknesses": "Дорогой, абразивный, сушка обязательна",
        "use_cases": "Детали у двигателя, разъёмы, оснастка",
    },
    "PPS": {
        "name": "PPS",
        "full_name": "Полифениленсульфид (PPS)",
        "density": 1.35,
        "speed_factor": 0.60,
        "support_factor": 0.15,
        "price_per_kg": 9500,
        "temp_nozzle": (290, 300),
        "temp_bed": (100, 110),
        "chamber": "closed_hot",
        "fan": 5,
        "shrinkage": 0.70,
        "dry_temp": 90,
        "dry_hours": 10,
        "heat_resistance": 220,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Химически стойкий, держит 220°C",
        "weaknesses": "Предел P1S по температуре; капризная адгезия",
        "use_cases": "Химия и нагрев — на P1S только как эксперимент",
    },
    "PPS_CF": {
        "name": "PPS-CF",
        "full_name": "PPS + углеволокно (PPS-CF)",
        "density": 1.40,
        "speed_factor": 0.55,
        "support_factor": 0.15,
        "price_per_kg": 12000,
        "temp_nozzle": (300, 320),
        "temp_bed": (100, 110),
        "chamber": "closed_hot",
        "fan": 5,
        "shrinkage": 0.50,
        "dry_temp": 90,
        "dry_hours": 10,
        "heat_resistance": 240,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": True,
        "strengths": "Максимум теплостойкости и жёсткости",
        "weaknesses": "Только X1E/высокотемпературная камера — P1S не тянет",
        "use_cases": "Промышленная оснастка на X1E",
    },
    "TPE": {
        "name": "TPE 83A",
        "full_name": "Термоэластопласт (TPE 83A)",
        "density": 1.15,
        "speed_factor": 0.20,
        "support_factor": 0.05,
        "price_per_kg": 3200,
        "temp_nozzle": (210, 235),
        "temp_bed": (30, 45),
        "chamber": "open",
        "fan": 60,
        "shrinkage": 1.00,
        "dry_temp": 60,
        "dry_hours": 6,
        "heat_resistance": 60,
        "uv_resistant": True,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Мягче TPU: прокладки и амортизация",
        "weaknesses": "Ещё медленнее TPU, очень мягкий",
        "use_cases": "Амортизаторы, мягкие вставки, игрушки",
    },
    "PP": {
        "name": "PP",
        "full_name": "Полипропилен (PP)",
        "density": 0.90,
        "speed_factor": 0.70,
        "support_factor": 0.15,
        "price_per_kg": 2800,
        "temp_nozzle": (220, 245),
        "temp_bed": (80, 100),
        "chamber": "closed",
        "fan": 50,
        "shrinkage": 1.50,
        "dry_temp": 60,
        "dry_hours": 4,
        "heat_resistance": 100,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Живой шарнир, химически стойкий, лёгкий",
        "weaknesses": "Плохо липнет к столу (нужна подложка), коробится",
        "use_cases": "Петли-шарниры, ёмкости, упаковка",
    },
    "POM": {
        "name": "POM",
        "full_name": "Полиоксиметилен / ацеталь (POM)",
        "density": 1.41,
        "speed_factor": 0.60,
        "support_factor": 0.15,
        "price_per_kg": 2600,
        "temp_nozzle": (210, 230),
        "temp_bed": (90, 100),
        "chamber": "closed",
        "fan": 40,
        "shrinkage": 1.20,
        "dry_temp": 80,
        "dry_hours": 4,
        "heat_resistance": 110,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Самый скользкий пластик: шестерни без смазки",
        "weaknesses": "Токсичен при перегреве, капризная адгезия",
        "use_cases": "Шестерни, защёлки, подшипники скольжения",
    },
    "PVB": {
        "name": "PVB",
        "full_name": "Поливинилбутираль (PVB / Polysmooth)",
        "density": 1.10,
        "speed_factor": 0.80,
        "support_factor": 0.10,
        "price_per_kg": 2800,
        "temp_nozzle": (190, 215),
        "temp_bed": (45, 60),
        "chamber": "open",
        "fan": 80,
        "shrinkage": 0.30,
        "dry_temp": 45,
        "dry_hours": 4,
        "heat_resistance": 55,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Разглаживается парами изопропанола — глянец без шлифовки",
        "weaknesses": "Печатается как PLA, прочность как у PLA",
        "use_cases": "Изделия «как литые»: фигурки, декор, подарки",
    },
    "PMMA": {
        "name": "PMMA",
        "full_name": "Полиметилметакрилат / акрил (PMMA)",
        "density": 1.18,
        "speed_factor": 0.70,
        "support_factor": 0.15,
        "price_per_kg": 2600,
        "temp_nozzle": (235, 255),
        "temp_bed": (90, 100),
        "chamber": "closed",
        "fan": 40,
        "shrinkage": 0.50,
        "dry_temp": 70,
        "dry_hours": 6,
        "heat_resistance": 85,
        "uv_resistant": True,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Прозрачный, атмосферостойкий, как литой акрил",
        "weaknesses": "Хрупкий, нужна сушка",
        "use_cases": "Световоды, витрины, прозрачный декор",
    },
    "PBT": {
        "name": "PBT",
        "full_name": "Полибутилентерефталат (PBT)",
        "density": 1.30,
        "speed_factor": 0.70,
        "support_factor": 0.15,
        "price_per_kg": 3000,
        "temp_nozzle": (240, 260),
        "temp_bed": (70, 85),
        "chamber": "closed",
        "fan": 30,
        "shrinkage": 0.80,
        "dry_temp": 80,
        "dry_hours": 6,
        "heat_resistance": 130,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Стойкий к химии и износу, как POM, но печатается проще",
        "weaknesses": "Сушка обязательна, умеренная усадка",
        "use_cases": "Электротехнические детали, разъёмы",
    },
    "PEI": {
        "name": "PEI (ULTEM)",
        "full_name": "Полиэфиримид (PEI / ULTEM)",
        "density": 1.27,
        "speed_factor": 0.50,
        "support_factor": 0.15,
        "price_per_kg": 15000,
        "temp_nozzle": (350, 380),
        "temp_bed": (130, 160),
        "chamber": "closed_hot",
        "fan": 5,
        "shrinkage": 0.60,
        "dry_temp": 120,
        "dry_hours": 10,
        "heat_resistance": 200,
        "uv_resistant": False,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Авиационный стандарт: прочность и 200°C",
        "weaknesses": "На P1S печать невозможна (нужны 350°C+ и горячая камера) — справочно",
        "use_cases": "Промышленность: X1E/промышленные принтеры",
    },
    "PEEK": {
        "name": "PEEK",
        "full_name": "Полиэфирэфиркетон (PEEK)",
        "density": 1.32,
        "speed_factor": 0.40,
        "support_factor": 0.15,
        "price_per_kg": 25000,
        "temp_nozzle": (360, 410),
        "temp_bed": (130, 170),
        "chamber": "closed_hot",
        "fan": 5,
        "shrinkage": 0.70,
        "dry_temp": 120,
        "dry_hours": 12,
        "heat_resistance": 250,
        "uv_resistant": True,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Замена металла: прочность, 250°C, химическая стойкость",
        "weaknesses": "На P1S печать невозможна (нужны 400°C+) — справочно",
        "use_cases": "Импланты, авиация, нефтегаз — промышленные принтеры",
    },
    "PEKK": {
        "name": "PEKK",
        "full_name": "Полиэфиркетонкетон (PEKK)",
        "density": 1.30,
        "speed_factor": 0.40,
        "support_factor": 0.15,
        "price_per_kg": 22000,
        "temp_nozzle": (360, 400),
        "temp_bed": (130, 160),
        "chamber": "closed_hot",
        "fan": 5,
        "shrinkage": 0.60,
        "dry_temp": 120,
        "dry_hours": 12,
        "heat_resistance": 240,
        "uv_resistant": True,
        "food_safe": False,
        "abrasive": False,
        "strengths": "Как PEEK, но проще спекается слоями",
        "weaknesses": "На P1S печать невозможна — справочно",
        "use_cases": "Авиация и оснастка на промышленных принтерах",
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


def _norm_key(key: str) -> str:
    return (key or "").strip().upper().replace(" ", "_")


# Ключи, которые сушат обязательно, даже если в weaknesses нет слова «гигроскоп».
_HYGRO_KEYS = {
    "PC", "PA", "PA_GF", "PAHT_CF", "PVA", "BVOH", "PETG", "TPU", "TPE",
    "PET", "PETG_CF", "PETG_ESD", "PC_ABS", "PPA_CF", "PPS", "PPS_CF",
}


def is_hygroscopic(key: str, material: dict | None = None) -> bool:
    """Гигроскопичный пластик: влажность AMS — предупреждение, PLA — нет."""
    norm = _norm_key(key)
    if norm in ("PLA", "PLA+", "PLA_SILK", "PLA_MATTE", "PLA_CF", "PLA_WOOD",
                "PLA_MARBLE", "PLA_GLOW", "PLA_METAL"):
        return False
    if norm in _HYGRO_KEYS or norm.startswith("PA") or norm.startswith("PC"):
        return True
    src = material if isinstance(material, dict) else MATERIALS.get(norm, {})
    blob = " ".join(str(src.get(k) or "") for k in ("weaknesses", "name", "full_name", "key"))
    return "гигроскоп" in blob.casefold()


def material_from_row(row: dict) -> dict:
    """Строка таблицы materials → справочник материала.

    Для встроенных типов (builtin=1) строка в базе — это «настройка под
    себя» поверх каталога: чего нет в строке, добирается из каталога.
    """
    key = str(row.get("key") or "")
    builtin = bool(row.get("builtin"))
    base = MATERIALS.get(_norm_key(str(row.get("base") or "")), {})
    if builtin and not base:
        base = MATERIALS.get(_norm_key(key), {})
    price = num(row.get("price_per_kg"))
    if price <= 0:
        price = num(base.get("price_per_kg"))
    return {
        "key": key,
        "name": str(row.get("name") or base.get("name") or key or "Материал"),
        "full_name": str(row.get("full_name") or base.get("full_name", "")),
        "density": num(row.get("density")) or num(base.get("density"), 1.24),
        "speed_factor": num(row.get("speed_factor")) or num(base.get("speed_factor"), 1.0),
        "support_factor": num(row.get("support_factor"), 0.10)
        if row.get("support_factor") not in (None, "")
        else num(base.get("support_factor"), 0.10),
        "price_per_kg": price,
        "temp_nozzle": (num(row.get("temp_nozzle_min"), 210),
                        num(row.get("temp_nozzle_max"), 240)),
        "temp_bed": (num(row.get("temp_bed_min"), 45),
                     num(row.get("temp_bed_max"), 65)),
        "chamber": str(row.get("chamber") or base.get("chamber") or "open"),
        "fan": num(row.get("fan"), 100),
        "shrinkage": num(row.get("shrinkage"), 0.25),
        "dry_temp": num(row.get("dry_temp"), 50),
        "dry_hours": num(row.get("dry_hours"), 5),
        "heat_resistance": num(row.get("heat_resistance"), 58),
        "uv_resistant": bool(row.get("uv_resistant")),
        "food_safe": bool(row.get("food_safe")),
        "abrasive": bool(row.get("abrasive")),
        "strengths": str(row.get("strengths") or base.get("strengths") or ""),
        "weaknesses": str(row.get("weaknesses") or base.get("weaknesses") or ""),
        "use_cases": str(row.get("use_cases") or base.get("use_cases") or ""),
        "note": str(row.get("note") or ""),
        "base": str(row.get("base") or ""),
        "id": row.get("id"),
        "builtin": builtin,
        "custom": not builtin,
    }


def seed_builtin_materials(db) -> int:
    """Занести весь встроенный каталог пластиков в базу (таблица materials).

    Делается один раз (флаг materials_seeded): дальше строки каталога живут
    в базе, их можно править под себя, а свои материалы добавлять рядом.
    """
    if db is None:
        return 0
    if db.setting("materials_seeded"):
        return 0
    count = 0
    try:
        for key, m in MATERIALS.items():
            row = db.one("SELECT id FROM materials WHERE key=?", (key,))
            if row:
                continue
            nozzle = tuple(m.get("temp_nozzle") or (210, 240))
            bed = tuple(m.get("temp_bed") or (45, 65))
            db.upsert("materials", {
                "id": f"mat_builtin_{key}",
                "key": key,
                "name": m.get("name", key),
                "full_name": m.get("full_name", ""),
                "base": key,
                "builtin": 1,
                "density": m.get("density", 1.24),
                "speed_factor": m.get("speed_factor", 1.0),
                "support_factor": m.get("support_factor", 0.10),
                "price_per_kg": m.get("price_per_kg", 0),
                "temp_nozzle_min": nozzle[0],
                "temp_nozzle_max": nozzle[1],
                "temp_bed_min": bed[0],
                "temp_bed_max": bed[1],
                "chamber": m.get("chamber", "open"),
                "fan": m.get("fan", 100),
                "shrinkage": m.get("shrinkage", 0.25),
                "dry_temp": m.get("dry_temp", 50),
                "dry_hours": m.get("dry_hours", 5),
                "heat_resistance": m.get("heat_resistance", 58),
                "uv_resistant": 1 if m.get("uv_resistant") else 0,
                "food_safe": 1 if m.get("food_safe") else 0,
                "abrasive": 1 if m.get("abrasive") else 0,
                "strengths": m.get("strengths", ""),
                "weaknesses": m.get("weaknesses", ""),
                "use_cases": m.get("use_cases", ""),
                "note": "",
                "archived": 0,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            })
            count += 1
        db.set_settings({"materials_seeded": True})
    except Exception:
        pass  # нет таблицы или read-only база — каталог остаётся в коде
    return count


def _material_rows(db) -> dict:
    """Все активные строки таблицы materials: ключ → строка."""
    if db is None:
        return {}
    try:
        rows = db.query("SELECT * FROM materials WHERE archived=0")
    except Exception:
        return {}
    return {str(r.get("key") or ""): r for r in rows}


def custom_materials(db=None) -> list[dict]:
    """Только свои пластики из базы (builtin=0)."""
    return [material_from_row(r) for r in _material_rows(db).values()
            if not bool(r.get("builtin"))]


def get_material(key: str, db=None) -> dict[str, Any]:
    """Материал по ключу (PLA, PETG, …) — сначала свои из базы, потом встроенные.

    Неизвестный → PLA.
    """
    norm = _norm_key(key)
    if db is not None and norm:
        try:
            rows = db.query(
                "SELECT * FROM materials WHERE archived=0 AND key=? LIMIT 1",
                (norm,))
            if rows:
                return material_from_row(rows[0])
        except Exception:
            pass
    if norm in MATERIALS:
        return MATERIALS[norm]
    # Простые алиасы
    aliases = {
        "PLA TOUGH": "PLA+",
        "PLA SILK": "PLA_SILK",
        "PLA MATTE": "PLA_MATTE",
        "PLA WOOD": "PLA_WOOD",
        "WOOD": "PLA_WOOD",
        "PLA MARBLE": "PLA_MARBLE",
        "MARBLE": "PLA_MARBLE",
        "PLA GLOW": "PLA_GLOW",
        "GLOW": "PLA_GLOW",
        "PLA METAL": "PLA_METAL",
        "METAL PLA": "PLA_METAL",
        "TPU95A": "TPU",
        "TPU 95": "TPU",
        "TPE 83A": "TPE",
        "NYLON CF": "PAHT_CF",
        "PA CF": "PAHT_CF",
        "PA-CF": "PAHT_CF",
        "PAHT": "PAHT_CF",
        "NYLON": "PA",
        "PA6": "PA",
        "PA12": "PA",
        "PA GF": "PA_GF",
        "PA-GF": "PA_GF",
        "PPA CF": "PPA_CF",
        "PPA-CF": "PPA_CF",
        "PPS CF": "PPS_CF",
        "PPS-CF": "PPS_CF",
        "PET CF": "PET_CF",
        "PET-CF": "PET_CF",
        "PETG CF": "PETG_CF",
        "PETG-CF": "PETG_CF",
        "ESD PETG": "PETG_ESD",
        "ABS GF": "ABS_GF",
        "ABS-GF": "ABS_GF",
        "PC ABS": "PC_ABS",
        "PC-ABS": "PC_ABS",
        "PLA CF": "PLA_CF",
        "PLA-CF": "PLA_CF",
        "ACETAL": "POM",
        "DELRIN": "POM",
        "POLYSMOOTH": "PVB",
        "ACRYLIC": "PMMA",
        "ULTEM": "PEI",
        "PEI ULTEM": "PEI",
    }
    return MATERIALS.get(aliases.get(norm, ""), MATERIALS["PLA"])


def get_profile(key: str) -> dict[str, Any]:
    """Профиль качества по ключу. Неизвестный → стандарт."""
    return QUALITY_PROFILES.get((key or "standard").strip().lower(),
                                QUALITY_PROFILES["standard"])


def num(value, default: float = 0.0) -> float:
    """Локальный num — без импорта accounting (избегаем цикла импортов)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _flat(m: dict, key: str, custom: bool) -> dict[str, Any]:
    return {
        "key": key,
        "name": m.get("name", key),
        "full_name": m.get("full_name", ""),
        "price_per_kg": m.get("price_per_kg", 0),
        "density": m.get("density", 1.24),
        "speed_factor": m.get("speed_factor", 1.0),
        "heat_resistance": m.get("heat_resistance", 58),
        "uv_resistant": m.get("uv_resistant", False),
        "abrasive": m.get("abrasive", False),
        "strengths": m.get("strengths", ""),
        "weaknesses": m.get("weaknesses", ""),
        "use_cases": m.get("use_cases", ""),
        "builtin": not custom,
        "custom": custom,
    }


def material_list(db=None) -> list[dict[str, Any]]:
    """Плоский список для выпадающего меню в калькуляторе.

    Встроенный каталог (с настройками из базы, если пользователь их правил),
    затем свои пластики из базы (custom=True).
    """
    rows = _material_rows(db)
    out = []
    for key, m in MATERIALS.items():
        row = rows.get(key)
        full = material_from_row(row) if row else m
        out.append(_flat(full, key, custom=False))
    for row_key, row in rows.items():
        if row_key not in MATERIALS:
            out.append(_flat(material_from_row(row), row_key, custom=True))
    return out


def material_full_list(db=None) -> list[dict[str, Any]]:
    """Полный справочник для настроек: каталог + свои со всеми параметрами."""
    rows = _material_rows(db)
    out: list[dict[str, Any]] = []
    for key, m in MATERIALS.items():
        row = rows.get(key)
        if row:
            full = material_from_row(row)
            full["key"] = key
            full["builtin"] = True
            full["custom"] = False
        else:
            full = {**m, "key": key, "builtin": True, "custom": False,
                    "id": None, "note": "", "base": key}
        out.append(full)
    for row_key, row in rows.items():
        if row_key not in MATERIALS:
            out.append(material_from_row(row))
    return out


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

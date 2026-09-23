"""Настройки слота AMS: что должно стоять и как это уезжает в принтер (18.13).

Склад — источник правды. Если в слоте стоит катушка склада, то тип, цвет,
бренд и температуры сопла в этом слоте должны совпадать с её карточкой: иначе
Bambu Studio режет под один пластик, а станок печатает другим, и «грязный»
результат списывают на модель.

Здесь две половины, и разделены они намеренно:

  * **чистая логика** — ``desired_slot``, ``slot_diff``, ``slot_signature``:
    считают, что должно быть в слоте и что с ним не так. Ничего не отправляют,
    поэтому проверяются тестами без принтера;
  * **отправка** — ``push_slot_settings``: единственное место, где настройки
    уходят в MQTT командой ``ams_filament``. Вызывается только автопилотом
    (``manager.ams_monitor``) и откатом из журнала действий.

Автопилот не спорит с печатью: пока станок печатает или готовится, настройки
слота не трогаются — смена типа филамента на ходу ломает задание.
"""
from __future__ import annotations

import json
from typing import Any

from .accounting import num
from .ams_defaults import color_name_for, normalize_hex

#: Состояния станка, в которых слот не трогаем: печать идёт или вот-вот начнётся.
BUSY_STATES = {"RUNNING", "PREPARE", "PAUSE", "SLICING"}

#: Поля слота, которыми управляет автопилот (порядок — как в ленте).
FIELD_LABELS: dict[str, str] = {
    "type": "тип",
    "color": "цвет",
    "brand": "бренд",
    "temp": "температуры сопла",
}


def _rec_temps(spool: dict) -> tuple | None:
    """Температуры со стикера катушки (``rec_settings``), если владелец их ввёл."""
    raw = str((spool or {}).get("rec_settings") or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    temps = data.get("nozzle") or data.get("temp_nozzle") or data.get("nozzle_temp")
    if isinstance(temps, (list, tuple)) and len(temps) == 2:
        return (num(temps[0]), num(temps[1]))
    return None


def desired_slot(spool: dict) -> dict:
    """Каким должен быть слот по карточке катушки.

    Тип, цвет, бренд и температуры берутся из того же справочника Bambu, что
    уходит в Bambu Studio при ручной привязке (``materials.bambu_filament_preset``):
    одна точка правды на панель, на пульт и на автопилот.
    """
    from .materials import bambu_filament_preset

    material = str((spool or {}).get("material") or "").strip()
    brand = str((spool or {}).get("brand") or "").strip()
    preset = bambu_filament_preset(material or "PLA", brand, _rec_temps(spool or {}))
    color = normalize_hex((spool or {}).get("color_hex")) or ""
    multi = str((spool or {}).get("colors_json") or "").strip()
    return {
        "type": str(preset.get("tray_type") or material or "").strip(),
        "color": "" if multi else color,
        "brand": brand or str(preset.get("tray_sub_brands") or ""),
        "temp_min": int(num(preset.get("nozzle_temp_min"))),
        "temp_max": int(num(preset.get("nozzle_temp_max"))),
        "tray_info_idx": str(preset.get("tray_info_idx") or ""),
        "preset": str(preset.get("preset_name") or ""),
    }


def slot_diff(tray: dict, spool: dict) -> list[dict]:
    """Что не сходится между слотом принтера и катушкой склада.

    Цвет сравниваем по hex и только у однотонных катушек: градиент, радуга и
    шёлк в один код слота не влезают — там цвет не трогаем (``color`` пустой в
    :func:`desired_slot`). Тип сравниваем без учёта регистра и дефисов: принтер
    отдаёт «PLA», карточка может хранить «pla».
    """
    want = desired_slot(spool)
    tray = tray or {}
    out: list[dict] = []

    def key(value: Any) -> str:
        return str(value or "").strip().upper().replace(" ", "")

    if want["type"] and key(tray.get("type")) != key(want["type"]):
        out.append({"field": "type", "label": FIELD_LABELS["type"],
                    "actual": str(tray.get("type") or ""), "want": want["type"]})
    if want["color"]:
        actual_color = normalize_hex(tray.get("color"))
        if actual_color and actual_color != want["color"]:
            out.append({"field": "color", "label": FIELD_LABELS["color"],
                        "actual": actual_color, "want": want["color"],
                        "want_name": color_name_for(want["color"])})
    actual_min, actual_max = num(tray.get("nozzle_min")), num(tray.get("nozzle_max"))
    if want["temp_min"] and want["temp_max"] and actual_min and actual_max:
        if (int(actual_min), int(actual_max)) != (want["temp_min"], want["temp_max"]):
            out.append({"field": "temp", "label": FIELD_LABELS["temp"],
                        "actual": f"{int(actual_min)}–{int(actual_max)} °C",
                        "want": f"{want['temp_min']}–{want['temp_max']} °C"})
    return out


def slot_signature(spool: dict) -> str:
    """Подпись желаемого состояния слота — защита от повторов в MQTT.

    Пока подпись та же, повторно в принтер ничего не уходит: иначе каждые пять
    минут в сеть летела бы одна и та же команда, а принтер бы её показывал
    сообщением в Studio.
    """
    want = desired_slot(spool)
    return "|".join(str(want.get(field) or "") for field in
                    ("type", "color", "brand", "temp_min", "temp_max", "tray_info_idx"))


def push_slot_settings(db, printer: Any, slot: Any, settings: dict | None = None,
                       *, reason: str = "auto") -> dict:
    """Отправить настройки слота в принтер командой ``ams_filament``.

    ``settings`` — готовый набор полей (им пользуется откат: он возвращает
    прежние значения). Без него набор считается по катушке слота.
    """
    slot_num = int(num(slot, -1))
    if slot_num < 0:
        return {"pushed": False, "error": "Не указан слот"}
    if settings is None:
        return {"pushed": False, "error": "Нет настроек для слота"}
    command = "ams_filament"
    payload = {
        "ams_id": slot_num // 4 if slot_num < 254 else 255,
        "tray_id": slot_num % 4 if slot_num < 254 else 0,
        "type": str(settings.get("type") or ""),
        "color": str(settings.get("color") or "").lstrip("#"),
        "brand": str(settings.get("brand") or ""),
        "temp_min": int(num(settings.get("temp_min"))),
        "temp_max": int(num(settings.get("temp_max"))),
        "idx": str(settings.get("tray_info_idx") or ""),
    }
    if not payload["type"]:
        return {"pushed": False, "error": "Не известен тип пластика"}
    try:
        printer.command(command, payload)
    except Exception as exc:  # сеть, белый список команд, отключённый принтер
        return {"pushed": False, "error": str(exc)}
    return {"pushed": True, "slot": slot_num, "settings": payload, "reason": reason}


def print_in_progress(snap: dict) -> bool:
    """Идёт ли печать (или подготовка к ней) — в это время слот не трогаем."""
    state = str(((snap or {}).get("printer") or {}).get("state") or "").upper()
    return state in BUSY_STATES

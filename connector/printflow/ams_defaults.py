"""Источники значений новой катушки AMS: правило владельца → материалы → встроенные.

Цена справочника — ориентир, а не фактическая закупочная цена. Источник всегда
сохраняется в price_source и показывается в панели; нулевую цену автопилот не создаёт.
Правила не выполняют код: это только фильтры по RFID/типу/цвету и разрешённые
значения карточки. Ручные поля существующей катушки не перезаписываются.
"""
from __future__ import annotations

import re
from typing import Any

from .accounting import num, uid
from .config import now_iso
from .materials import MATERIALS

_HEX = re.compile(r"^#[0-9A-F]{6}$")


def hex_color(value: Any) -> str:
    """Нормальный RGB или пусто: не превращаем мусор в чёрный цвет."""
    text = str(value or "").strip().lstrip("#").upper()
    return "#" + text[:6] if len(text) in (6, 8) and _HEX.fullmatch("#" + text[:6]) else ""


def rules(db) -> list[dict]:
    return db.query("SELECT * FROM ams_rules ORDER BY priority, id")


def save_rule(db, data: dict) -> dict:
    """Сохранить только разрешённые поля, не принимая SQL/произвольные ключи."""
    if not isinstance(data, dict):
        raise ValueError("Правило AMS должно быть объектом")
    ident = str(data.get("id") or uid("ar"))[:100]
    existing = db.one("SELECT * FROM ams_rules WHERE id=?", (ident,)) or {}
    allowed = ("priority", "enabled", "match_uuid", "match_material", "match_color",
               "material", "brand", "color_hex", "total_grams", "price", "temp_min",
               "temp_max")
    row = {**existing, **{key: data[key] for key in allowed if key in data}, "id": ident}
    for key in ("match_uuid", "match_material", "material", "brand"):
        row[key] = str(row.get(key) or "").strip()[:100]
    for key in ("match_material", "material"):
        row[key] = row[key].upper()
    row["match_uuid"] = row["match_uuid"].upper()
    if not any(row.get(key) for key in ("match_uuid", "match_material", "match_color")):
        raise ValueError("Укажите RFID, материал или цвет для правила")
    for key in ("match_color", "color_hex"):
        if row.get(key):
            row[key] = hex_color(row[key])
            if not row[key]:
                raise ValueError(f"Цвет правила {key}: нужен #RRGGBB")
        else:
            row[key] = ""
    for key in ("total_grams", "price"):
        value = num(row.get(key))
        if not 0 <= value <= 100000:
            raise ValueError(f"{key}: ожидается неотрицательное число")
        row[key] = value
    for key in ("temp_min", "temp_max"):
        value = int(num(row.get(key)))
        if not 0 <= value <= 350:
            raise ValueError("Температура должна быть в пределах 0–350 °C")
        row[key] = value
    if row["temp_min"] and row["temp_max"] and row["temp_min"] > row["temp_max"]:
        raise ValueError("Минимальная температура выше максимальной")
    row["priority"] = max(0, min(9999, int(num(row.get("priority"), 100))))
    row["enabled"] = 1 if row.get("enabled", True) not in (False, 0, "0", "false") else 0
    row["updated_at"] = now_iso()
    return db.upsert("ams_rules", row)


def matching_rule(db, tray: dict) -> dict | None:
    material = str(tray.get("type") or "").strip().upper()
    uuid = str(tray.get("uuid") or "").strip().upper()
    color = hex_color(tray.get("color"))
    choices = []
    for row in rules(db):
        if not row["enabled"]:
            continue
        if row["match_uuid"] and row["match_uuid"] != uuid:
            continue
        if row["match_material"] and row["match_material"] != material:
            continue
        if row["match_color"] and row["match_color"] != color:
            continue
        specificity = sum(bool(row[key]) for key in
                          ("match_uuid", "match_material", "match_color"))
        choices.append((row["priority"], -specificity, row["id"], row))
    return sorted(choices)[0][-1] if choices else None


def defaults(db, tray: dict) -> dict | None:
    """Обязательные поля новой карточки, источник цены и настройки для принтера."""
    rule = matching_rule(db, tray)
    material = str((rule or {}).get("material") or tray.get("type") or "").strip().upper()
    if not material:
        return None  # пустая телеметрия не повод выдумывать материал
    from .materials import get_material

    catalog = db.one("SELECT * FROM materials WHERE UPPER(key)=? AND archived=0", (material,))
    builtin = MATERIALS.get(material, {})
    effective = get_material(material, db)
    total = num((rule or {}).get("total_grams")) or 1000.0
    price_from_rule = num((rule or {}).get("price"))
    price_from_catalog = num((catalog or {}).get("price_per_kg"))
    price_from_builtin = num(builtin.get("price_per_kg"))
    if price_from_rule:
        source = "правило владельца"
        price = price_from_rule
    elif price_from_catalog:
        source = "таблица материалов"
        price = price_from_catalog * total / 1000
    else:
        source = "встроенный справочник" if price_from_builtin else "цена по умолчанию"
        price = (price_from_builtin * total / 1000 if price_from_builtin
                 else num(db.setting("default_spool_price", 1600)) or 1600)
    color = hex_color((rule or {}).get("color_hex") or tray.get("color"))
    from .ams_sync import _hex_to_name  # только обозначение цвета, без импорта логики вверху
    nozzle = effective.get("temp_nozzle") or (210, 240)
    low = int(num((rule or {}).get("temp_min"))) or int(num(nozzle[0]))
    high = int(num((rule or {}).get("temp_max"))) or int(num(nozzle[1]))
    return {"material": material,
            "brand": str((rule or {}).get("brand") or tray.get("brand") or "").strip()[:100],
            "color_hex": color or "#4B5563",
            "color_name": _hex_to_name(color) if color else "",
            "total_grams": total,
            "price": round(price, 2),
            "temp_min": low,
            "temp_max": high,
            "source": source,
            "rule_id": (rule or {}).get("id") or ""}


def material_defaults(db) -> list[dict]:
    """Материалы для доктора: живой каталог первым, встроенные — запасной вариант."""
    rows = {str(r["key"]).upper(): r for r in db.query(
        "SELECT key, name, price_per_kg, temp_nozzle_min, temp_nozzle_max"
        " FROM materials WHERE archived=0 AND key IS NOT NULL")}
    for key, builtin in MATERIALS.items():
        rows.setdefault(key, {"key": key, "name": builtin.get("name") or key,
                              "price_per_kg": builtin.get("price_per_kg"),
                              "temp_nozzle_min": builtin.get("temp_nozzle", (0, 0))[0],
                              "temp_nozzle_max": builtin.get("temp_nozzle", (0, 0))[1]})
    return sorted(rows.values(), key=lambda r: r["key"])

"""Умные значения для катушек, которые PrintFlow заводит из AMS (18.13).

Зачем. Принтер рассказывает про пластик в слоте не всё: тип (``PLA``), цвет в
hex, остаток в процентах и диапазон температур сопла. Полная масса бобины,
цена, бренд и человеческое имя цвета — это то, чего AMS не знает, и раньше
после каждого синка карточку приходилось править руками: автосоздание ставило
жёстко 1000 г и цену 0, а имя цвета бралось из палитры на шесть слов
(любой фиолетовый и бирюзовый назывались «Синий»).

Здесь три источника значений, по убыванию доверия:

  1. **правила** (таблица ``ams_rules``) — то, что владелец правил дважды:
     разовая правка не правило, иначе одна случайная опечатка разъедется по
     всему складу;
  2. **таблица материалов** (настройка ``ams_material_defaults``) — что задано
     руками: материал → масса, цена, бренд;
  3. **встроенные значения** — 1 кг и цена по умолчанию; такая катушка честно
     помечается «не проверена».

Имя цвета считается из hex: сначала точное совпадение по словарю ходовых
цветов, иначе тон и светлота (HSV) дают имя с уточнением — «Тёмно-синий»,
«Светло-серый», «Бирюзовый».
"""
from __future__ import annotations

import json
from typing import Any

from .accounting import num, uid
from .config import now_iso

#: Сколько раз надо увидеть одну и ту же правку, чтобы она стала правилом.
RULE_CONFIRMATIONS = 2

#: Масса катушки, когда про неё ничего не известно (стандартная бобина 1 кг).
DEFAULT_WEIGHT = 1000.0

#: Словарь точных цветов: у этих hex имя не считаем, а знаем.
KNOWN_COLORS: dict[str, str] = {
    "#000000": "Чёрный",
    "#FFFFFF": "Белый",
    "#808080": "Серый",
    "#C0C0C0": "Серебристый",
    "#FF0000": "Красный",
    "#FF7F00": "Оранжевый",
    "#FFFF00": "Жёлтый",
    "#00FF00": "Зелёный",
    "#00AE42": "Зелёный",          # ходовой «Bambu Green»
    "#00FFFF": "Голубой",
    "#0000FF": "Синий",
    "#7F00FF": "Фиолетовый",
    "#FF00FF": "Пурпурный",
    "#FFC0CB": "Розовый",
    "#8B4513": "Коричневый",
    "#D2B48C": "Бежевый",
    "#FFD700": "Золотой",
}

#: Материалы, для которых масса бобины отличается от килограмма по умолчанию.
WEIGHT_BY_MATERIAL: dict[str, float] = {
    "TPU": 500.0,
    "PVA": 500.0,
    "PA": 500.0,
    "PA6-CF": 500.0,
    "PC": 500.0,
    "PET-CF": 500.0,
    "PPA-CF": 500.0,
}

#: Тон → имя (границы по кругу HSV, градусы).
HUE_NAMES: tuple[tuple[float, str], ...] = (
    (12.0, "Красный"),
    (25.0, "Оранжевый"),
    (48.0, "Жёлтый"),
    (95.0, "Салатовый"),
    (160.0, "Зелёный"),
    (190.0, "Бирюзовый"),
    (215.0, "Голубой"),
    (255.0, "Синий"),
    (290.0, "Фиолетовый"),
    (330.0, "Пурпурный"),
    (348.0, "Розовый"),
    (360.0, "Красный"),
)


# --------------------------------------------------------------- цвет по hex
def normalize_hex(value: Any) -> str:
    """Hex к виду ``#RRGGBB``. Мусор и «нет цвета» — пустая строка."""
    text = str(value or "").strip().lstrip("#").upper()
    if len(text) >= 8 and text[:8] == "00000000":
        return ""
    if len(text) < 6:
        return ""
    try:
        int(text[:6], 16)
    except ValueError:
        return ""
    return "#" + text[:6]


def _hsv(red: int, green: int, blue: int) -> tuple[float, float, float]:
    """RGB 0–255 → HSV (тон 0–360, насыщенность и яркость 0–1)."""
    r, g, b = red / 255.0, green / 255.0, blue / 255.0
    high, low = max(r, g, b), min(r, g, b)
    span = high - low
    if span == 0:
        return 0.0, 0.0, high
    if high == r:
        hue = (60 * ((g - b) / span)) % 360
    elif high == g:
        hue = 60 * ((b - r) / span) + 120
    else:
        hue = 60 * ((r - g) / span) + 240
    return hue, (span / high if high else 0.0), high


def color_name_for(value: Any) -> str:
    """Человеческое имя цвета по hex: «Тёмно-синий», «Светло-серый», «Бирюзовый»."""
    hex_norm = normalize_hex(value)
    if not hex_norm:
        return ""
    known = KNOWN_COLORS.get(hex_norm)
    if known:
        return known
    red = int(hex_norm[1:3], 16)
    green = int(hex_norm[3:5], 16)
    blue = int(hex_norm[5:7], 16)
    hue, sat, val = _hsv(red, green, blue)
    # Серые и почти серые: тон не помогает, решает яркость.
    if sat < 0.12:
        if val < 0.15:
            return "Чёрный"
        if val < 0.40:
            return "Тёмно-серый"
        if val < 0.72:
            return "Серый"
        if val < 0.94:
            return "Светло-серый"
        return "Белый"
    base = next(name for edge, name in HUE_NAMES if hue < edge)
    # Светлота идёт вперёд насыщенности: пастельный «#FFD6E0» — это «светло-
    # розовый», а не «серо-розовый», хотя насыщенность у него и низкая.
    if val >= 0.82 and sat < 0.55:
        return f"Светло-{base.lower()}"
    if val < 0.28:
        return f"Тёмно-{base.lower()}"
    if sat < 0.30:
        # Оттенок еле виден: так и говорим — «серо-зелёный».
        return f"Серо-{base.lower()}"
    if val >= 0.90 and sat >= 0.65:
        return f"Ярко-{base.lower()}"
    return base


# ------------------------------------------------- значения по материалу
def material_key(value: Any) -> str:
    """Ключ материала для правил и таблицы: ``PLA``, ``PETG``, ``PA6-CF``."""
    return str(value or "").strip().upper().replace(" ", "-")


def rule_key(material: str, brand: str = "") -> str:
    """Ключ правила: материал и (если известен) бренд — разными бобинами."""
    return f"{material_key(material)}|{str(brand or '').strip().lower()}"


def builtin_weight(material: str) -> float:
    """Масса бобины по материалу; неизвестный материал — стандартный килограмм."""
    return WEIGHT_BY_MATERIAL.get(material_key(material), DEFAULT_WEIGHT)


def material_defaults(db) -> dict[str, dict]:
    """Таблица «материал → масса, цена, бренд» из настроек.

    Хранится настройкой ``ams_material_defaults`` (словарь), потому что это
    данные заказчика, а не константа кода: у кого-то PLA по 950 г, у кого-то
    PETG от конкретного поставщика.
    """
    raw = db.setting("ams_material_defaults", {}) or {}
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for material, payload in raw.items():
        key = material_key(material)
        if not key or not isinstance(payload, dict):
            continue
        item: dict[str, Any] = {}
        grams = num(payload.get("total_grams"), 0)
        if grams > 0:
            item["total_grams"] = round(grams, 1)
        price = num(payload.get("price"), 0)
        if price > 0:
            item["price"] = round(price, 2)
        brand = str(payload.get("brand") or "").strip()
        if brand:
            item["brand"] = brand
        if item:
            out[key] = item
    return out


def update_material_default(db, material: str, **fields: Any) -> dict:
    """Записать строку таблицы материалов: масса, цена, бренд (0 и '' — убрать)."""
    key = material_key(material)
    if not key:
        raise ValueError("Укажите материал")
    raw = db.setting("ams_material_defaults", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    raw = {material_key(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
    row = raw.get(key) or {}
    if "total_grams" in fields:
        grams = num(fields.get("total_grams"), 0)
        if grams > 0:
            row["total_grams"] = round(grams, 1)
        else:
            row.pop("total_grams", None)
    if "price" in fields:
        price = num(fields.get("price"), 0)
        if price > 0:
            row["price"] = round(price, 2)
        else:
            row.pop("price", None)
    if "brand" in fields:
        brand = str(fields.get("brand") or "").strip()
        if brand:
            row["brand"] = brand
        else:
            row.pop("brand", None)
    if row:
        raw[key] = row
    else:
        raw.pop(key, None)
    db.set_settings({"ams_material_defaults": raw})
    return raw


# ----------------------------------------------------------- выученные правила
def _signature(payload: dict) -> str:
    """Короткая подпись значения правила — чтобы один и тот же набор не плодился."""
    import hashlib

    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def _slug(value: str) -> str:
    keep = [c if c.isalnum() else "-" for c in str(value or "").lower()]
    text = "".join(keep).strip("-")
    return text[:40] or "any"


def rules(db) -> list[dict]:
    """Все правила: что запомнили и сколько раз это видели."""
    out = []
    for row in db.query("SELECT * FROM ams_rules ORDER BY applied DESC, updated_at DESC"):
        item = dict(row)
        try:
            item["value"] = json.loads(item.get("value") or "{}")
        except json.JSONDecodeError:
            item["value"] = {}
        item["applied"] = bool(num(item.get("applied")))
        item["seen"] = int(num(item.get("seen")))
        out.append(item)
    return out


def applied_rules(db) -> dict[tuple[str, str], dict]:
    """Действующие правила: (вид, ключ) → значение. Позднее правило важнее."""
    out: dict[tuple[str, str], dict] = {}
    for row in db.query("SELECT * FROM ams_rules WHERE applied=1 ORDER BY updated_at"):
        key = (str(row.get("kind") or ""), str(row.get("key") or ""))
        try:
            out[key] = json.loads(row.get("value") or "{}")
        except json.JSONDecodeError:
            continue
    return out


def set_rule(db, kind: str, key: str, value: dict, *, seen: int | None = None) -> dict:
    """Записать правило вручную (из таблицы правил на панели)."""
    kind = str(kind or "").strip()
    key = str(key or "").strip()
    if not kind or not key or not isinstance(value, dict) or not value:
        raise ValueError("Правило: нужны вид, ключ и значение")
    ident = f"amsr_{kind}_{_slug(key)}_{_signature(value)}"
    row = db.one("SELECT * FROM ams_rules WHERE id=?", (ident,))
    seen_value = int(seen if seen is not None else max(RULE_CONFIRMATIONS,
                                                        int(num((row or {}).get("seen")))))
    db.upsert("ams_rules", {
        "id": ident, "kind": kind, "key": key,
        "value": json.dumps(value, ensure_ascii=False),
        "seen": seen_value,
        "applied": 1 if seen_value >= RULE_CONFIRMATIONS else 0,
        "created_at": (row or {}).get("created_at") or now_iso(),
        "updated_at": now_iso(),
    })
    return {"id": ident, "kind": kind, "key": key, "value": value,
            "seen": seen_value, "applied": seen_value >= RULE_CONFIRMATIONS}


def forget_rule(db, ident: str) -> int:
    """Забыть правило: одна строка или (пустой ident) все правила владельца."""
    ident = str(ident or "").strip()
    if not ident:
        cur = db.execute("DELETE FROM ams_rules")
    else:
        cur = db.execute("DELETE FROM ams_rules WHERE id=?", (ident,))
    return int(getattr(cur, "rowcount", 0) or 0)


def learn_observation(db, kind: str, key: str, payload: dict) -> dict:
    """Отметить наблюдение. Одинаковое значение подтверждается, разное — своё.

    Счётчик считается по паре «значение + ключ»: правило включается только
    после :data:`RULE_CONFIRMATIONS` одинаковых наблюдений. Пока правило не
    подтвердилось, оно лежит в таблице как «кандидат» и в подстановке не
    участвует — иначе одна опечатка в карточке уехала бы во все катушки.
    """
    if not payload:
        raise ValueError("Пустое наблюдение")
    ident = f"amsr_{kind}_{_slug(key)}_{_signature(payload)}"
    row = db.one("SELECT * FROM ams_rules WHERE id=?", (ident,))
    seen = int(num((row or {}).get("seen"))) + 1
    applied = 1 if seen >= RULE_CONFIRMATIONS else 0
    db.upsert("ams_rules", {
        "id": ident, "kind": kind, "key": key,
        "value": json.dumps(payload, ensure_ascii=False),
        "seen": seen, "applied": applied,
        "created_at": (row or {}).get("created_at") or now_iso(),
        "updated_at": now_iso(),
    })
    return {"id": ident, "kind": kind, "key": key, "value": payload,
            "seen": seen, "applied": bool(applied)}


def _clean_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def learn_from_edit(db, before: dict, after: dict) -> list[dict]:
    """Запомнить правку карточки катушки как кандидата в правило.

    Учимся только на осмысленных значениях: пустой бренд и нулевая цена —
    это «не знаю», а не правило. Масса и цена живут правилом на материал и
    бренд, имя цвета — на материал и hex: у «чёрного PLA» и «чёрного PETG»
    имена совпадают не всегда.
    """
    before = dict(before or {})
    after = dict(after or {})
    if not before or not after:
        return []
    material = _clean_text(after.get("material")) or _clean_text(before.get("material"))
    learned: list[dict] = []

    payload: dict[str, Any] = {}
    brand_before = _clean_text(before.get("brand"))
    brand_after = _clean_text(after.get("brand"))
    if brand_after and brand_after != brand_before:
        payload["brand"] = brand_after
    grams_before, grams_after = num(before.get("total_grams")), num(after.get("total_grams"))
    if grams_after > 0 and abs(grams_after - grams_before) > 0.05:
        payload["total_grams"] = round(grams_after, 1)
    price_before, price_after = num(before.get("price")), num(after.get("price"))
    if price_after > 0 and abs(price_after - price_before) > 0.005:
        payload["price"] = round(price_after, 2)
    if payload:
        learned.append(learn_observation(
            db, "spool_defaults",
            rule_key(material, brand_after or brand_before), payload))

    hex_before = normalize_hex(before.get("color_hex"))
    hex_after = normalize_hex(after.get("color_hex"))
    name_before = _clean_text(before.get("color_name"))
    name_after = _clean_text(after.get("color_name"))
    hex_for_rule = hex_after or hex_before
    if hex_for_rule and name_after and (name_after != name_before or hex_after != hex_before):
        learned.append(learn_observation(
            db, "color_name", f"{material_key(material)}|{hex_for_rule}",
            {"color_name": name_after}))
    return learned


# ------------------------------------------------------------------ подстановка
def resolve_defaults(db, *, material: str, color_hex: str = "",
                     brand_hint: str = "", color_name_hint: str = "",
                     ) -> dict:
    """Что записать в карточку новой катушки, которую завёл AMS.

    Порядок источников: правило → таблица материалов → встроенные значения.
    Бренд с принтера (RFID-метка Bambu) важнее правила: он про конкретную
    бобину, а правило — про «что обычно бывает».
    """
    material_text = _clean_text(material) or "PLA"
    hex_norm = normalize_hex(color_hex)
    key = material_key(material_text)
    brand = _clean_text(brand_hint)
    brand_source = "printer" if brand else ""
    weight = 0.0
    price = 0.0
    source = "builtin"
    learned = applied_rules(db)
    for candidate in (rule_key(material_text, brand), rule_key(material_text, "")):
        payload = learned.get(("spool_defaults", candidate)) or {}
        if not weight and num(payload.get("total_grams")) > 0:
            weight = num(payload["total_grams"])
            source = "rule"
        if not price and num(payload.get("price")) > 0:
            price = num(payload["price"])
            source = "rule"
        if not brand and _clean_text(payload.get("brand")):
            brand = _clean_text(payload["brand"])
            brand_source = "rule"
            source = "rule"
    table = material_defaults(db).get(key) or {}
    if not weight and num(table.get("total_grams")) > 0:
        weight = num(table["total_grams"])
        source = "table" if source == "builtin" else source
    if not price and num(table.get("price")) > 0:
        price = num(table["price"])
        source = "table" if source == "builtin" else source
    if not brand and _clean_text(table.get("brand")):
        brand = _clean_text(table["brand"])
        brand_source = "table"
        source = "table" if source == "builtin" else source
    if weight <= 0:
        weight = builtin_weight(material_text)
    if price <= 0:
        price = max(0.0, num(db.setting("default_spool_price", 1600.0), 1600.0))

    color_name = _clean_text(color_name_hint)
    if not color_name:
        rule_color = learned.get(("color_name", f"{key}|{hex_norm}")) or {}
        color_name = _clean_text(rule_color.get("color_name")) or color_name_for(hex_norm)

    # «Проверена» — когда масса и цена пришли не из встроенных значений, а
    # бренд вообще известен. Иначе в карточке стоит честная пометка.
    known_weight = source in ("rule", "table")
    verified = 1 if (known_weight and brand) else 0
    missing = []
    if not known_weight:
        missing.append("масса бобины")
    if source == "builtin":
        missing.append("цена")
    if not brand:
        missing.append("бренд")
    note = ""
    if not verified:
        note = ("Заведено автопилотом AMS: уточните, что тут верно — "
                + ", ".join(missing) + ". Тогда PrintFlow подставит это сам")
    return {
        "material": material_text,
        "brand": brand,
        "brand_source": brand_source,
        "color_name": color_name,
        "color_hex": hex_norm or "#4B5563",
        "total_grams": round(weight, 1),
        "price": round(price, 2),
        "source": source,
        "verified": verified,
        "missing": missing,
        "note": note,
    }


def resolve_data(db, *, material: str, color_hex: str = "", brand_hint: str = "",
                 spool_id: str = "") -> dict:
    """Готовая строка для таблицы ``spools`` — что записать новой катушке."""
    values = resolve_defaults(db, material=material, color_hex=color_hex,
                              brand_hint=brand_hint)
    values["id"] = spool_id or uid("sp")
    values["remaining_grams"] = values["total_grams"]
    return values

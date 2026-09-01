"""Автосбор данных с принтера и AMS в базу.

Раз в несколько минут менеджер передаёт сюда свежий снапшот принтера:

  * в карточку принтера записываются прошивка, сигнал Wi-Fi, влажность AMS
    и время последней связи (колонки firmware/wifi/ams_humidity/last_seen);
  * катушки, вставленные в AMS, автоматически появляются на складе
    (таблица spools) и обновляют остаток по данным датчиков.

Всё внесённое автоматически можно править вручную:

  * материал, бренд, цвет, цену и вес катушки автосинк не трогает никогда —
    они принадлежат пользователю;
  * остаток и привязку к слоту автосинк обновляет только у катушек
    с включённой галочкой «Обновлять из AMS» (поле ams_sync = 1);
  * автосоздание и синхронизацию остатка можно выключить целиком
    в настройках (ams_auto_spools, ams_sync_remaining, printer_info_sync).
"""
from __future__ import annotations

from typing import Any

from .accounting import num, uid
from .config import now_iso

# Пустой слот AMS отдаёт uuid из одних нулей — считаем его отсутствием метки.
ZERO_UUID = "0" * 32


def _hex_to_name(value: str) -> str:
    """Hex #RRGGBB → человеческое имя (упрощённая палитра)."""
    value = (value or "").strip().lstrip("#")
    if len(value) < 6:
        return ""
    try:
        r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
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


def _normalize_hex(value: str) -> str:
    v = str(value or "").strip().lstrip("#")
    if len(v) >= 6:
        return "#" + v[:6].upper()
    return ""


def _clean_uuid(value: Any) -> str:
    text = str(value or "").strip()
    return "" if not text or set(text) <= {"0"} else text


def _tray_occupied(tray: dict) -> bool:
    """Слот занят катушкой — в том числе сторонней без RFID и без типа."""
    if tray.get("present") is False:
        return False
    if tray.get("present") is True or tray.get("generic") is True:
        return True
    if str(tray.get("type") or "").strip():
        return True
    if _clean_uuid(tray.get("uuid")):
        return True
    return False


def _tray_generic(tray: dict) -> bool:
    """Сторонний пластик: нет RFID Bambu, слот при этом занят."""
    if tray.get("generic") is True:
        return True
    if tray.get("bambulab") is True:
        return False
    if tray.get("present") is True and not _clean_uuid(tray.get("uuid")):
        return True
    return False


def sync_printer_info(db, printer_id: str, snap: dict) -> bool:
    """Записать в карточку принтера данные, которые он сообщил сам."""
    if not db.setting("printer_info_sync", True):
        return False
    if not db.one("SELECT id FROM printers WHERE id=?", (printer_id,)):
        return False
    info = snap.get("printer") or {}
    ams = snap.get("ams") or {}
    humidity = ams.get("humidity")
    db.execute(
        "UPDATE printers SET firmware=?, wifi=?, ams_humidity=?, last_seen=? WHERE id=?",
        (str(info.get("firmware") or ""), str(info.get("wifi") or ""),
         "" if humidity is None else str(humidity), now_iso(), printer_id))
    return True


def sync_ams_spools(db, printer_id: str, snap: dict) -> dict:
    """Свести катушки в AMS с таблицей spools.

    Возвращает счётчики: created / updated / unbound.
    Фиксы:
      * пустой слот (present=False) отвязывает катушку с ams_sync=1, чистит
        tray_uuid и location=shop, чтобы не плодить 50 AMS-фантомов;
      * unbind всегда чистит tray_uuid + location;
      * проверяем ams_sync старой катушки перед отвязкой;
      * обновляем location=ams при привязке.
    """
    result = {"created": 0, "updated": 0, "unbound": 0}
    trays = (snap.get("ams") or {}).get("trays") or []
    if not trays:
        return result
    auto_create = bool(db.setting("ams_auto_spools", True))
    sync_remaining = bool(db.setting("ams_sync_remaining", True))
    for tray in trays:
        tray_uuid = _clean_uuid(tray.get("uuid"))
        material = str(tray.get("type") or "").strip()
        slot = "" if tray.get("slot") is None else str(tray.get("slot"))
        label = tray.get("label") or (f"Слот {slot}" if slot else "AMS")
        remain = tray.get("remain")
        color = _normalize_hex(str(tray.get("color") or ""))
        generic = _tray_generic(tray)

        if not _tray_occupied(tray):
            if slot != "":
                by_slot = db.one(
                    "SELECT * FROM spools WHERE printer_id=? AND ams_slot=? AND archived=0",
                    (printer_id, slot))
                if by_slot and int(num(by_slot.get("ams_sync"), 1)) == 1:
                    db.execute(
                        "UPDATE spools SET ams_slot='', tray_uuid='', location='shop', updated_at=? WHERE id=?",
                        (now_iso(), by_slot["id"]))
                    result["unbound"] += 1
                    db.add_event(
                        "spool", "Катушка отвязана от слота",
                        f"{label}: слот опустел — катушка возвращена на склад",
                        printer_id, {"spool_id": by_slot["id"], "slot": slot})
            continue

        spool = None
        if tray_uuid:
            spool = db.one("SELECT * FROM spools WHERE tray_uuid=? AND archived=0",
                           (tray_uuid,))
        if not spool and slot != "":
            by_slot = db.one(
                "SELECT * FROM spools WHERE printer_id=? AND ams_slot=? AND archived=0",
                (printer_id, slot))
            if by_slot:
                if int(num(by_slot.get("ams_sync"), 1)) != 1:
                    spool = by_slot
                else:
                    old_uuid = _clean_uuid(by_slot.get("tray_uuid"))
                    swapped = bool(tray_uuid and old_uuid and old_uuid != tray_uuid)
                    replaced_by_generic = bool(old_uuid and not tray_uuid and generic)
                    if swapped or replaced_by_generic:
                        db.execute(
                            "UPDATE spools SET ams_slot='', tray_uuid='', location='shop', updated_at=? WHERE id=?",
                            (now_iso(), by_slot["id"]))
                        result["unbound"] += 1
                        db.add_event(
                            "spool", "Катушка отвязана от слота",
                            f"{label}: в AMS теперь другая катушка",
                            printer_id, {"spool_id": by_slot["id"], "slot": slot})
                    else:
                        spool = by_slot

        if spool:
            if not int(num(spool.get("ams_sync"), 1)):
                continue
            updates: list[str] = []
            params: list[Any] = []
            if tray_uuid and tray_uuid != _clean_uuid(spool.get("tray_uuid")):
                updates.append("tray_uuid=?")
                params.append(tray_uuid)
            if material and not str(spool.get("material") or "").strip():
                updates.append("material=?")
                params.append(material)
            if str(spool.get("printer_id") or "") != printer_id:
                updates.append("printer_id=?")
                params.append(printer_id)
            if slot != "" and str(spool.get("ams_slot") or "") != slot:
                updates.append("ams_slot=?")
                params.append(slot)
            if sync_remaining and remain is not None and num(remain, -1) >= 0:
                total = max(1.0, num(spool.get("total_grams"), 1000))
                fresh = round(min(100.0, num(remain)) / 100.0 * total, 1)
                if abs(fresh - num(spool.get("remaining_grams"))) > 1.0:
                    updates.append("remaining_grams=?")
                    params.append(fresh)
            if color:
                cur_hex = _normalize_hex(str(spool.get("color_hex") or ""))
                cur_name = str(spool.get("color_name") or "").strip()
                if (not cur_name) or cur_hex in ("", "#4B5563", "#333333", "#CBD5E1"):
                    if cur_hex != color:
                        updates.append("color_hex=?")
                        params.append(color)
                    if not cur_name:
                        cname = _hex_to_name(color)
                        if cname:
                            updates.append("color_name=?")
                            params.append(cname)
            if str(spool.get("location") or "") != "ams":
                updates.append("location=?")
                params.append("ams")
            updates.append("synced_at=?")
            params.append(now_iso())
            if updates:
                db.execute(
                    f"UPDATE spools SET {', '.join(updates)}, updated_at=? WHERE id=?",
                    (*params, now_iso(), spool["id"]))
                result["updated"] += 1
        elif auto_create and material:
            total = 1000.0
            remaining = total
            if remain is not None and num(remain, -1) >= 0:
                remaining = round(min(100.0, max(0.0, num(remain))) / 100.0 * total, 1)
            hex_norm = color or "#4b5563"
            cname = _hex_to_name(hex_norm) if hex_norm != "#4b5563" else ""
            row = db.upsert("spools", {
                "id": uid("sp"),
                "material": material,
                "brand": "",
                "color_name": cname,
                "color_hex": hex_norm,
                "total_grams": total,
                "remaining_grams": remaining,
                "price": 0,
                "printer_id": printer_id,
                "ams_slot": slot,
                "tray_uuid": tray_uuid,
                "location": "ams",
                "ams_sync": 1,
                "synced_at": now_iso(),
                "verified": 0,
                "note": "Импортировано из AMS: проверьте массу, цену, бренд и цвет",
                "archived": 0,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            })
            result["created"] += 1
            db.add_event(
                "spool", "Катушка добавлена из AMS",
                f"{label}: {material}, остаток {round(num(remain, 100))}%."
                " Уточните бренд, цвет и цену в карточке склада.",
                printer_id, {"spool_id": row.get("id"), "slot": slot,
                             "tray_uuid": tray_uuid})
    return result

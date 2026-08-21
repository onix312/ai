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
    return "" if not text or set(text) == {"0"} else text


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
        if not material and not tray_uuid:
            continue  # пустой слот
        slot = "" if tray.get("slot") is None else str(tray.get("slot"))
        label = tray.get("label") or (f"Слот {slot}" if slot else "AMS")
        remain = tray.get("remain")
        color = _normalize_hex(str(tray.get("color") or ""))

        # 1) ищем катушку по RFID-метке, затем по привязке принтер+слот
        spool = None
        if tray_uuid:
            spool = db.one("SELECT * FROM spools WHERE tray_uuid=? AND archived=0",
                           (tray_uuid,))
        if not spool and slot != "":
            by_slot = db.one(
                "SELECT * FROM spools WHERE printer_id=? AND ams_slot=? AND archived=0",
                (printer_id, slot))
            if by_slot:
                old_uuid = _clean_uuid(by_slot.get("tray_uuid"))
                if tray_uuid and old_uuid and old_uuid != tray_uuid:
                    # В слоте теперь другая катушка: старую отвязываем,
                    # ниже заведём новую. Остаток старой не трогаем.
                    db.execute(
                        "UPDATE spools SET ams_slot='', updated_at=? WHERE id=?",
                        (now_iso(), by_slot["id"]))
                    result["unbound"] += 1
                    db.add_event(
                        "spool", "Катушка отвязана от слота",
                        f"{label}: в AMS теперь другая катушка",
                        printer_id, {"spool_id": by_slot["id"], "slot": slot})
                else:
                    spool = by_slot

        if spool:
            # 2) катушка известна — обновляем только «автоматические» поля
            if not int(num(spool.get("ams_sync"), 1)):
                continue  # пользователь ведёт её вручную
            updates: list[str] = []
            params: list[Any] = []
            if tray_uuid and tray_uuid != _clean_uuid(spool.get("tray_uuid")):
                updates.append("tray_uuid=?")
                params.append(tray_uuid)
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
            # цвет из AMS: заполняем только если у катушки пустое имя/hex-дефолт
            if color:
                cur_hex = _normalize_hex(str(spool.get("color_hex") or ""))
                cur_name = str(spool.get("color_name") or "").strip()
                if (not cur_name) or cur_hex in ("", "#4B5563", "#333333", "#CBD5E1"):
                    # не затираем ручной выбор, но пустые/дефолтные — заполняем
                    if cur_hex != color:
                        updates.append("color_hex=?")
                        params.append(color)
                    if not cur_name:
                        cname = _hex_to_name(color)
                        if cname:
                            updates.append("color_name=?")
                            params.append(cname)
            updates.append("synced_at=?")
            params.append(now_iso())
            db.execute(
                f"UPDATE spools SET {', '.join(updates)}, updated_at=? WHERE id=?",
                (*params, now_iso(), spool["id"]))
            result["updated"] += 1
        elif auto_create and material:
            # 3) катушки нет на складе — заводим сами
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
                # Нельзя считать неизвестную RFID-катушку оплаченной/оценённой:
                # цена и масса уточняются оператором перед производством.
                "price": 0,
                "printer_id": printer_id,
                "ams_slot": slot,
                "tray_uuid": tray_uuid,
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

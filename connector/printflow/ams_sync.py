"""Автосбор данных с принтера и AMS в базу.

Раз в несколько минут менеджер передаёт сюда свежий снапшот принтера:

  * в карточку принтера записываются прошивка, сигнал Wi-Fi, влажность AMS
    и время последней связи (колонки firmware/wifi/ams_humidity/last_seen);
  * катушки, вставленные в AMS, автоматически появляются на складе
    (таблица spools) и обновляют остаток по данным датчиков;
  * каждый слот запоминается отдельно (таблица ams_slots): какая катушка
    в нём стояла, сколько осталось и когда это видели. Память живёт в базе,
    поэтому панель показывает загрузку AMS и после перезапуска, и когда
    принтер выключен — раньше это помнил только процесс в памяти, и снятая
    катушка исчезала вместе с ним;
  * смена катушки в слоте пишется в историю (таблица ams_slot_history) —
    не только когда слот привязали руками, но и когда это сделал автосинк.

Всё внесённое автоматически можно править вручную:

  * новые карточки получают значения по правилу/каталогу; существующие ручные
    материал, бренд, цвет, цену и вес не перезаписывает (пустая цена — исключение);
  * остаток и привязку к слоту автосинк обновляет только у катушек
    с включённой галочкой «Обновлять из AMS» (поле ams_sync = 1);
  * автосоздание и синхронизацию остатка можно выключить целиком
    в настройках (ams_auto_spools, ams_sync_remaining, printer_info_sync).
"""
from __future__ import annotations

import json
from typing import Any

from .accounting import num, uid
from .config import now_iso

def slot_number(tray: dict) -> int:
    """Сквозной номер 0–15: MQTT отдаёт локальный слот 0–3 и номер AMS.

    В тестах и ручных снимках ``slot`` может быть уже сквозным — без ``unit``.
    """
    slot = int(tray["slot"])
    if tray.get("unit") is not None and 0 <= slot < 4:
        return int(tray["unit"]) * 4 + slot
    return slot


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


def remember_slot(db, printer_id: str, slot: Any, *, spool_id: str = "",
                  tray_uuid: str = "", material: str = "", color_name: str = "",
                  color_hex: str = "", label: str = "", remain_pct: float = -1.0,
                  grams_left: float = 0.0, state: str = "live",
                  keep_last: bool = False) -> dict | None:
    """Записать в память, что лежит в слоте AMS (таблица ams_slots).

    ``keep_last`` — для пустого слота: материал, цвет и последнюю катушку
    оставляем, чтобы было видно, что здесь стояло; меняются только состояние,
    остаток и время.
    """
    slot_s = "" if slot is None else str(slot)
    if not printer_id or slot_s == "":
        return None
    at = now_iso()
    row = db.one("SELECT * FROM ams_slots WHERE printer_id=? AND slot=?",
                 (printer_id, slot_s))
    values: dict[str, Any] = {
        "printer_id": printer_id,
        "slot": slot_s,
        "state": state,
        "seen_at": at,
        "updated_at": at,
    }
    if state == "empty":
        values["remain_pct"] = -1.0
        values["grams_left"] = 0.0
        values["emptied_at"] = at if not row or row.get("state") != "empty" else (
            row.get("emptied_at") or at)
        if keep_last and row:
            values["spool_id"] = row.get("spool_id") or ""
            values["material"] = row.get("material") or ""
            values["color_name"] = row.get("color_name") or ""
            values["color_hex"] = row.get("color_hex") or ""
            values["label"] = row.get("label") or ""
            values["tray_uuid"] = ""
        else:
            values.update({"spool_id": "", "tray_uuid": "", "material": "",
                           "color_name": "", "color_hex": "", "label": ""})
    else:
        values.update({
            "spool_id": spool_id or "",
            "tray_uuid": tray_uuid or "",
            "material": material or "",
            "color_name": color_name or "",
            "color_hex": color_hex or "",
            "label": label or "",
            "remain_pct": round(float(remain_pct), 1) if remain_pct is not None else -1.0,
            "grams_left": round(float(grams_left or 0.0), 1),
            "emptied_at": "",
        })
    if row:
        sets = ", ".join(f"{key}=?" for key in values)
        db.execute(f"UPDATE ams_slots SET {sets} WHERE id=?",
                   (*values.values(), row["id"]))
        return db.one("SELECT * FROM ams_slots WHERE id=?", (row["id"],))
    values["id"] = uid("ams")
    db.upsert("ams_slots", values)
    return db.one("SELECT * FROM ams_slots WHERE id=?", (values["id"],))


def slot_event(db, printer_id: str, slot: Any, spool_id: str, action: str,
               note: str = "") -> None:
    """История слотов AMS: кто и когда менял катушку в слоте."""
    db.upsert("ams_slot_history", {
        "id": uid("ash"),
        "at": now_iso(),
        "printer_id": printer_id or "",
        "slot": "" if slot is None else str(slot),
        "spool_id": spool_id or "",
        "action": action,
        "note": (note or "")[:300],
    })


#: Через сколько минут память слота считается устаревшей (принтер молчит).
MEMORY_STALE_MIN = 30


def slot_memory(db, printer_id: str = "", slots_only: bool = False) -> list[dict]:
    """Память AMS из базы: по слоту на строку, свежие сверху раздела.

    ``stale`` — принтер не присылал данные дольше :data:`MEMORY_STALE_MIN`
    минут: панель обязана сказать «данные от такого-то времени», а не
    показывать вчерашнюю загрузку как живую.
    """
    if printer_id:
        rows = db.query("SELECT * FROM ams_slots WHERE printer_id=? ORDER BY slot",
                        (printer_id,))
    else:
        rows = db.query("SELECT * FROM ams_slots ORDER BY printer_id, slot")
    out = []
    for row in rows:
        item = dict(row)
        item["slot_num"] = int(num(item.get("slot"), 0) or 0)
        item["stale"] = _is_stale(item.get("seen_at"))
        item["grams_left"] = round(num(item.get("grams_left")), 1)
        item["remain_pct"] = round(num(item.get("remain_pct"), -1), 1)
        if item["state"] != "empty" and item["grams_left"] <= 0 and item["remain_pct"] >= 0:
            item["grams_left"] = 0.0
        out.append(item)
    return out


def _is_stale(seen_at: Any) -> bool:
    from datetime import datetime, timedelta
    stamp = str(seen_at or "").strip()
    if not stamp:
        return True
    try:
        seen = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return True
    now = datetime.now(seen.tzinfo) if seen.tzinfo else datetime.now()
    return (now - seen) > timedelta(minutes=MEMORY_STALE_MIN)


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


def _compatible(spool: dict, material: str, color: str, threshold: float) -> bool:
    """Без RFID материал и цвет нужны оба: один оттенок — не личность катушки."""
    if not material or not color or str(spool.get("material") or "").upper() != material.upper():
        return False
    other = _normalize_hex(str(spool.get("color_hex") or ""))
    if not other or other in ("#4B5563", "#333333", "#CBD5E1"):
        # Старые карточки с цветом-заглушкой не доказывают совпадение.
        return False
    from .estimate import color_distance
    return color_distance(color, other) <= threshold


def _generic_candidates(db, printer_id: str, slot: str, material: str,
                        color: str, occupied: set[tuple[str, str]]) -> list[dict]:
    """Только живые/недавно виденные катушки. Без истории не угадываем."""
    from datetime import datetime, timedelta, timezone
    if not material or not color or color in ("#CBD5E1", "#4B5563", "#333333"):
        return []  # цвет-заглушка из MQTT — не подтверждение совпадения
    threshold = num(db.setting("ams_delta_e_threshold", 30), 30)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=60)
    candidates = {}
    current = db.one("SELECT * FROM spools WHERE printer_id=? AND ams_slot=? AND archived=0",
                     (printer_id, slot))
    if current:
        candidates[current["id"]] = current
    for history in db.query(
            "SELECT spool_id, at FROM ams_slot_history ORDER BY rowid DESC LIMIT 300"):
        try:
            seen = datetime.fromisoformat(str(history["at"]))
            if seen.tzinfo is None:
                continue  # старое время без зоны не подтверждает 60 минут
            if seen.astimezone(timezone.utc) < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        ident = str(history.get("spool_id") or "")
        if ident and ident not in candidates:
            row = db.one("SELECT * FROM spools WHERE id=? AND archived=0 AND ams_sync=1",
                         (ident,))
            if row:
                candidates[ident] = row
    result = []
    for spool in candidates.values():
        if not int(num(spool.get("ams_sync"), 1)):
            continue
        last_bind = db.one("SELECT action FROM ams_slot_history WHERE spool_id=?"
                           " ORDER BY rowid DESC LIMIT 1", (spool["id"],))
        if last_bind and last_bind["action"] == "manual_unbind":
            continue  # явную отвязку без RFID не переигрываем в следующем опросе
        if _clean_uuid(spool.get("tray_uuid")) or not _compatible(spool, material, color, threshold):
            continue
        position = (str(spool.get("printer_id") or ""), str(spool.get("ams_slot") or ""))
        if position != (printer_id, slot) and position in occupied:
            continue  # катушка всё ещё явно видна в другом занятом слоте
        result.append(spool)
    return result


def sync_ams_spools(db, printer_id: str, snap: dict) -> dict:
    """Автопилот AMS: RFID/история → учёт/память → аудит. Без телеметрии — no-op.

    Синк атомарен для снимка парка: переезд RFID не оставляет две привязки.
    Неизвестную катушку без RFID не создаём «похожей» по всему складу: только
    одна недавняя, совпавшая по материалу и ΔE≤порог, считается узнаваемой.
    """
    from . import ams_actions, ams_defaults

    result = {"created": 0, "updated": 0, "unbound": 0,
              "remembered": 0, "events": 0, "alerts": [], "action_ids": []}
    trays = (snap.get("ams") or {}).get("trays") or []
    if not trays:
        return result
    auto_create = bool(db.setting("ams_auto_spools", True))
    sync_remaining = bool(db.setting("ams_sync_remaining", True))
    printing = str((snap.get("printer") or {}).get("state") or "").upper() in (
        "RUNNING", "PREPARE", "PAUSE", "PAUSED", "SLICING")
    occupied = {(printer_id, str(slot_number(t))) for t in trays
                if t.get("slot") is not None and 0 <= slot_number(t) <= 15
                and _tray_occupied(t)}
    uuids = [(_clean_uuid(t.get("uuid"))).upper() for t in trays if _tray_occupied(t)]
    duplicates = {ident for ident in uuids if ident and uuids.count(ident) > 1}

    def alert(event: str, slot: str, text: str) -> None:
        result["alerts"].append({"event": event, "slot": slot, "detail": text,
                                  "critical": event == "ams_runout"})

    with db.transaction():
        for tray in trays:
            if tray.get("slot") is None:
                continue
            slot = str(slot_number(tray))
            if not 0 <= int(slot) <= 15:
                continue  # внешний вход 254 не является слотом AMS
            label = str(tray.get("label") or f"Слот {slot}")
            uuid = _clean_uuid(tray.get("uuid"))
            material = str(tray.get("type") or "").strip().upper()
            color = _normalize_hex(str(tray.get("color") or ""))
            remain = tray.get("remain")
            by_slot = db.one("SELECT * FROM spools WHERE printer_id=? AND ams_slot=?"
                             " AND archived=0 ORDER BY updated_at DESC LIMIT 1", (printer_id, slot))
            old_memory = db.one("SELECT * FROM ams_slots WHERE printer_id=? AND slot=?",
                                (printer_id, slot))
            spool = None
            action = ""
            detail = ""
            created_id = ""
            conflict = False
            if not _tray_occupied(tray):
                if by_slot and int(num(by_slot.get("ams_sync"), 1)):
                    before = ams_actions.snapshot(db, [by_slot["id"]], [(printer_id, slot)])
                    db.execute("UPDATE spools SET ams_slot='', location='shop', updated_at=? WHERE id=?",
                               (now_iso(), by_slot["id"]))
                    result["unbound"] += 1
                    slot_event(db, printer_id, slot, by_slot["id"], "auto_unbind",
                               f"{label}: слот опустел")
                    result["events"] += 1
                    action, detail = "auto_unbind", f"{label}: катушка вернулась на склад, RFID сохранён"
                    if printing:
                        if not old_memory or old_memory.get("state") != "runout":
                            alert("ams_runout", slot, f"{label}: пластик исчез во время печати")
                    else:
                        alert("ams_unbind", slot, detail)
                else:
                    before = ams_actions.snapshot(db, positions=[(printer_id, slot)])
                if old_memory and old_memory.get("state") != "empty":
                    remember_slot(db, printer_id, slot, state="empty", keep_last=True)
                    result["remembered"] += 1
                    if not action:
                        action, detail = "auto_empty", f"{label}: слот опустел"
                        if old_memory.get("state") != "runout":
                            alert("ams_empty", slot, detail)
                if action:
                    after = ams_actions.snapshot(db, before["spools"], [(printer_id, slot)])
                    row = ams_actions.record(db, printer_id, slot,
                                             (by_slot or {}).get("id") or "",
                                             action, detail, before, after)
                    result["action_ids"].append(row["id"])
                continue

            if uuid and uuid.upper() in duplicates:
                detail = f"{label}: один RFID виден в нескольких слотах — привяжите вручную"
                conflict = True
            elif uuid:
                found = db.query("SELECT * FROM spools WHERE UPPER(tray_uuid)=? AND archived=0",
                                 (uuid.upper(),))
                if len(found) > 1:
                    detail = f"{label}: RFID уже принадлежит нескольким катушкам — разберите вручную"
                    conflict = True
                elif found:
                    spool = found[0]
                elif by_slot and not _clean_uuid(by_slot.get("tray_uuid")) and (
                        str(by_slot.get("material") or "").upper() == material):
                    # Ранее привязанная вручную без метки катушка впервые отдала RFID.
                    spool = by_slot
                elif auto_create and material:
                    created_id = uid("sp")
                else:
                    detail = f"{label}: RFID не узнан — укажите материал или привяжите вручную"
            else:
                # Ручная привязка — явное решение владельца после неоднозначного
                # generic-слота. Доверяем ей, пока тот же материал/цвет и та же
                # запись памяти; без последнего manual_bind не угадываем.
                manual = db.one(
                    "SELECT spool_id,action FROM ams_slot_history WHERE printer_id=? AND slot=?"
                    " ORDER BY rowid DESC LIMIT 1", (printer_id, slot))
                if (by_slot and old_memory
                        and old_memory.get("spool_id") == by_slot["id"] and
                        (manual or {}).get("spool_id") == by_slot["id"] and
                        (manual or {}).get("action") == "manual_bind" and
                        (not material or str(by_slot.get("material") or "").upper() == material)
                        and (not color or color in ("#CBD5E1", "#4B5563", "#333333")
                             or not _normalize_hex(str(by_slot.get("color_hex") or ""))
                             or _compatible(by_slot, material or str(by_slot.get("material") or ""),
                                            color, num(db.setting("ams_delta_e_threshold", 30), 30)))):
                    spool = by_slot
                matches = [] if spool else _generic_candidates(
                    db, printer_id, slot, material, color, occupied)
                if not spool and len(matches) == 1:
                    spool = matches[0]
                elif not spool:
                    detail = (f"{label}: катушка без RFID не узнана — привяжите вручную"
                              if not matches else
                              f"{label}: несколько похожих катушек без RFID — привяжите вручную")
                    conflict = len(matches) > 1 or by_slot is not None
                    if conflict and by_slot and not matches:
                        detail = f"{label}: привязанная катушка не совпадает с катушкой в слоте"

            if spool and not int(num(spool.get("ams_sync"), 1)):
                # Ручной режим катушки: её карточка и слот владельца неизменны.
                remember_slot(db, printer_id, slot, spool_id=spool["id"], tray_uuid=uuid,
                              material=material or spool.get("material") or "",
                              color_name=spool.get("color_name") or "",
                              color_hex=spool.get("color_hex") or "", label=label,
                              remain_pct=num(remain, -1),
                              grams_left=num(spool.get("remaining_grams")))
                result["remembered"] += 1
                continue
            if by_slot and (spool or created_id) and (
                    created_id or by_slot["id"] != spool["id"]) and (
                    not int(num(by_slot.get("ams_sync"), 1))):
                detail = f"{label}: слот занят катушкой на ручном учёте — проверьте привязку"
                conflict = True
                spool = None
                created_id = ""
            affected = {s["id"] for s in (spool, by_slot) if s}
            if created_id:
                affected.add(created_id)
            positions = [(printer_id, slot)]
            if spool and spool.get("ams_slot") not in (None, ""):
                positions.append((str(spool.get("printer_id") or ""), str(spool["ams_slot"])))
            before = ams_actions.snapshot(db, affected, positions)
            if not spool and not created_id:
                # Конфликтный RFID нельзя переназначать, а generic нельзя
                # угадывать: оставляем старую карточку и сохраняем только факт.
                replacement = False
                if by_slot and int(num(by_slot.get("ams_sync"), 1)):
                    saved_uuid = _clean_uuid(by_slot.get("tray_uuid"))
                    if uuid and saved_uuid and uuid.upper() != saved_uuid.upper():
                        replacement = True
                    elif not uuid or not saved_uuid:
                        if (material and by_slot.get("material") and
                                material != str(by_slot["material"]).strip().upper()):
                            replacement = True
                        elif color and color not in ("#CBD5E1", "#4B5563", "#333333"):
                            old_color = _normalize_hex(str(by_slot.get("color_hex") or ""))
                            if old_color and old_color not in ("#CBD5E1", "#4B5563", "#333333"):
                                from .estimate import color_distance
                                replacement = color_distance(color, old_color) > num(
                                    db.setting("ams_delta_e_threshold", 30), 30)
                if replacement:
                    db.execute("UPDATE spools SET ams_slot='', location='shop', updated_at=? WHERE id=?",
                               (now_iso(), by_slot["id"]))
                    slot_event(db, printer_id, slot, by_slot["id"], "auto_swap",
                               f"{label}: старую катушку сняли, новая не опознана")
                    alert("ams_unbind", slot, f"{label}: старая катушка снята, новая не опознана")
                    result["unbound"] += 1
                    conflict = True
                    detail = f"{label}: новая катушка не опознана — привяжите вручную"
                changed = (replacement or not old_memory or old_memory.get("spool_id") or
                           old_memory.get("material") != material or
                           old_memory.get("tray_uuid") != uuid or
                           old_memory.get("color_hex") != color or
                           old_memory.get("state") != ("unrecognized" if conflict else "live"))
                remember_slot(db, printer_id, slot, tray_uuid=uuid,
                              material=material, color_name=_hex_to_name(color),
                              color_hex=color, label=label, remain_pct=num(remain, -1),
                              state="unrecognized" if conflict else "live")
                result["remembered"] += 1
                if detail and changed:
                    if conflict:
                        alert("ams_conflict", slot, detail)
                    after = ams_actions.snapshot(db, affected, positions)
                    row = ams_actions.record(db, printer_id, slot,
                                             by_slot["id"] if replacement else "",
                                             "auto_unbind" if replacement else "unrecognized",
                                             detail, before, after)
                    result["action_ids"].append(row["id"])
                continue

            # Подмена освобождает старое место, но RFID остаётся на старой
            # катушке: следующий опрос другого слота узнает её по метке.
            if by_slot and (created_id or by_slot["id"] != spool["id"]):
                if int(num(by_slot.get("ams_sync"), 1)):
                    db.execute("UPDATE spools SET ams_slot='', location='shop', updated_at=? WHERE id=?",
                               (now_iso(), by_slot["id"]))
                    result["unbound"] += 1
                    slot_event(db, printer_id, slot, by_slot["id"], "auto_swap",
                               f"{label}: вместо неё другая катушка")
                    result["events"] += 1
                    alert("ams_unbind", slot, f"{label}: старая катушка возвращена на склад")
                else:
                    # Не перепривязываем принтер поверх ручной блокировки.
                    continue
            if created_id:
                values = ams_defaults.defaults(db, tray)
                if values is None:
                    continue
                remaining = values["total_grams"]
                if remain is not None and num(remain, -1) >= 0:
                    remaining = round(min(100., max(0., num(remain))) / 100 * values["total_grams"], 1)
                spool = db.upsert("spools", {
                    "id": created_id, "material": values["material"],
                    "brand": values["brand"], "color_name": values["color_name"],
                    "color_hex": values["color_hex"], "price": values["price"],
                    "rec_settings": json.dumps({"temp_nozzle": [values["temp_min"],
                                                          values["temp_max"]]}),
                    "total_grams": values["total_grams"], "remaining_grams": remaining,
                    "printer_id": printer_id, "ams_slot": "" if remain is not None and num(remain, -1) == 0 else slot,
                    "tray_uuid": uuid,
                    "location": "shop" if remain is not None and num(remain, -1) == 0 else "ams",
                    "ams_sync": 1, "verified": 1,
                    "price_source": values["source"],
                    "archived": 0, "synced_at": now_iso(), "created_at": now_iso(),
                    "updated_at": now_iso()})
                result["created"] += 1
                action = "auto_create"
            else:
                previous_pos = (str(spool.get("printer_id") or ""),
                                str(spool.get("ams_slot") or ""))
                if previous_pos[1] and previous_pos != (printer_id, slot):
                    known = db.one("SELECT * FROM ams_slots WHERE printer_id=? AND slot=?",
                                   previous_pos)
                    if known:
                        remember_slot(db, *previous_pos, state="empty", keep_last=True)
                    slot_event(db, *previous_pos, spool["id"], "auto_move",
                               f"{label}: катушка перенесена в другой слот")
                    result["events"] += 1
                    action = "auto_move"
                updates = {}
                exhausted = remain is not None and num(remain, -1) == 0
                if str(spool.get("printer_id") or "") != printer_id:
                    updates["printer_id"] = printer_id
                if not exhausted and str(spool.get("ams_slot") or "") != slot:
                    updates["ams_slot"] = slot
                if uuid and uuid.upper() != _clean_uuid(spool.get("tray_uuid")).upper():
                    updates["tray_uuid"] = uuid
                if not exhausted and spool.get("location") != "ams":
                    updates["location"] = "ams"
                if (spool.get("price_source") and color and
                        str(spool.get("color_hex") or "").upper() in
                        ("#4B5563", "#333333", "#CBD5E1") and
                        color not in ("#4B5563", "#333333", "#CBD5E1")):
                    # Цвет-заглушка автокарточки — не правка владельца: заменяем
                    # его первой достоверной телеметрией, не трогая ручные цвета.
                    updates["color_hex"] = color
                    updates["color_name"] = _hex_to_name(color)
                if not num(spool.get("price")) or not int(num(spool.get("verified"), 1)):
                    values = ams_defaults.defaults(db, tray)
                    if values:
                        if not num(spool.get("price")):
                            updates["price"] = values["price"]
                        if not str(spool.get("brand") or "") and values["brand"]:
                            updates["brand"] = values["brand"]
                        updates["verified"] = 1
                        updates["price_source"] = values["source"]
                if sync_remaining and remain is not None and num(remain, -1) >= 0:
                    total = max(1.0, num(spool.get("total_grams"), 1000))
                    fresh = round(min(100., max(0., num(remain))) / 100 * total, 1)
                    old = num(spool.get("remaining_grams"))
                    drift = fresh - old
                    if abs(drift) > 1:
                        updates["remaining_grams"] = fresh
                        pct_diff = 100 * drift / total
                        if pct_diff >= 15 and drift >= 40:
                            action = "refill"
                        elif pct_diff <= -25 and drift <= -50 and not printing:
                            # После реальной печати снижение — нормальный расход,
                            # даже если принтер уже перешёл в IDLE к этому опросу.
                            # При старых базах spool_id задания часто пуст: проверяем
                            # любую недавнюю печать на этом принтере (лучше пропустить
                            # алерт, чем объявить выполненную печать кражей).
                            since = (old_memory or {}).get("seen_at") or spool.get("synced_at") or ""
                            recent_job = db.one(
                                "SELECT id FROM print_jobs WHERE printer_id=? AND"
                                " (state IN ('running','starting') OR"
                                " (state IN ('done','failed') AND datetime(finished_at)>=datetime(?)))"
                                " LIMIT 1", (printer_id, since)) if since else None
                            if not recent_job:
                                action = "loss"
                                alert("ams_loss", slot, f"{label}: минус {abs(drift):.0f} г без печати")
                if exhausted:
                    if num(spool.get("remaining_grams")) > 0:
                        updates["remaining_grams"] = 0.
                    if previous_pos[1]:
                        updates["ams_slot"] = ""
                    if spool.get("location") != "shop":
                        updates["location"] = "shop"
                    if num(spool.get("remaining_grams")) > 0 or previous_pos[1]:
                        action = "runout"
                        if printing:
                            alert("ams_runout", slot, f"{label}: пластик закончился во время печати")
                if updates:
                    updates["synced_at"] = now_iso()
                    updates["updated_at"] = now_iso()
                    sets = ", ".join(f"{key}=?" for key in updates)
                    db.execute(f"UPDATE spools SET {sets} WHERE id=?",
                               (*updates.values(), spool["id"]))
                    spool = db.one("SELECT * FROM spools WHERE id=?", (spool["id"],))
                    result["updated"] += 1
                    action = action or "auto_update"
            exhausted = remain is not None and num(remain, -1) == 0
            if exhausted and created_id:
                action = "runout"
                if printing:
                    alert("ams_runout", slot, f"{label}: пластик закончился во время печати")
            remember_slot(db, printer_id, slot, spool_id=spool["id"], tray_uuid=uuid,
                          material=material or spool["material"],
                          color_name=spool.get("color_name") or "",
                          color_hex=spool.get("color_hex") or color,
                          label=label, remain_pct=num(remain, -1),
                          grams_left=num(spool.get("remaining_grams")),
                          state="runout" if exhausted else "live")
            result["remembered"] += 1
            if not old_memory or old_memory.get("spool_id") != spool["id"] or (
                    old_memory.get("state") != ("runout" if exhausted else "live")):
                slot_event(db, printer_id, slot, spool["id"],
                           "auto_create" if created_id else "auto_bind",
                           f"{label}: {spool['material']} — катушка со склада")
                result["events"] += 1
                action = action or "auto_bind"
            if action:
                detail = detail or f"{label}: {spool['material']} → {spool.get('remaining_grams')} г ({action})"
                after = ams_actions.snapshot(db, affected, positions)
                row = ams_actions.record(db, printer_id, slot, spool["id"],
                                         action, detail, before, after)
                result["action_ids"].append(row["id"])
    return result


def forget_slots(db, printer_id: str, slot: Any = None) -> int:
    """Забыть память слотов: без `slot` — весь принтер. Возвращает число строк.

    Чистится только память (`ams_slots`). Привязки катушек на складе
    (`spools.ams_slot`) и история смен остаются: очистка памяти не должна
    ломать учёт расхода пластика.
    """
    slot_s = "" if slot is None else str(slot).strip()
    if not printer_id:
        return 0
    if slot_s:
        cur = db.execute("DELETE FROM ams_slots WHERE printer_id=? AND slot=?",
                         (printer_id, slot_s))
    else:
        cur = db.execute("DELETE FROM ams_slots WHERE printer_id=?", (printer_id,))
    return int(getattr(cur, "rowcount", 0) or 0)


def backfill_slots(db, printer_id: str = "") -> int:
    """Дописать память слотов из привязок, которые уже есть в базе.

    Зачем отдельно от синка: у владельца уже стоят катушки в слотах — привязки
    записаны в `spools` (кто-то привязал их вручную, кто-то автосинком до
    обновления), а памяти слотов ещё нет. Ждать, пока принтер пришлёт
    телеметрию, не нужно: раскладка известна из базы, и панель может показать
    её сразу. Заполняются только пустые слоты — живые данные синка не трогаем.
    Возвращает число добавленных слотов.
    """
    sql = ("SELECT * FROM spools WHERE ams_slot IS NOT NULL AND ams_slot<>''"
           " AND archived=0")
    params: tuple = ()
    if printer_id:
        sql += " AND printer_id=?"
        params = (printer_id,)
    added = 0
    for spool in db.query(sql + " ORDER BY updated_at DESC", params):
        printer = str(spool.get("printer_id") or "")
        slot = str(spool.get("ams_slot") or "")
        if not printer or not slot:
            continue
        if db.one("SELECT id FROM ams_slots WHERE printer_id=? AND slot=?",
                  (printer, slot)):
            continue
        remember_slot(db, printer, slot, spool_id=str(spool.get("id") or ""),
                      tray_uuid=_clean_uuid(spool.get("tray_uuid")),
                      material=str(spool.get("material") or ""),
                      color_name=str(spool.get("color_name") or ""),
                      color_hex=str(spool.get("color_hex") or ""),
                      label=f"Слот {slot}",
                      remain_pct=-1.0,
                      grams_left=num(spool.get("remaining_grams")))
        # Время «видели» — когда катушку последний раз синхронизировали:
        # иначе панель сказала бы «данные только что», не видя принтера вовсе.
        seen = str(spool.get("synced_at") or spool.get("updated_at") or now_iso())
        db.execute("UPDATE ams_slots SET seen_at=? WHERE printer_id=? AND slot=?",
                   (seen, printer, slot))
        added += 1
    return added


def sync_one_printer(db, printer_id: str, snap: dict) -> dict:
    """Автосинк одним вызовом: карточка принтера, катушки и память слотов."""
    sync_printer_info(db, printer_id, snap)
    return sync_ams_spools(db, printer_id, snap)

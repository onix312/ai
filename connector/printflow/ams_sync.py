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


def sync_ams_spools(db, printer_id: str, snap: dict) -> dict:
    """Свести катушки в AMS с таблицей spools.

    Возвращает счётчики: created / updated / unbound / remembered / events.
    Фиксы:
      * пустой слот (present=False) отвязывает катушку с ams_sync=1, чистит
        tray_uuid и location=shop, чтобы не плодить 50 AMS-фантомов;
      * unbind всегда чистит tray_uuid + location;
      * проверяем ams_sync старой катушки перед отвязкой;
      * обновляем location=ams при привязке;
      * каждый слот запоминается в ams_slots, а смена катушки попадает в
        ams_slot_history — память остаётся в базе, а не в процессе.
    """
    result = {"created": 0, "updated": 0, "unbound": 0,
              "remembered": 0, "events": 0}
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
                    slot_event(db, printer_id, slot, by_slot["id"], "auto_unbind",
                               f"{label}: слот опустел — катушка возвращена на склад")
                    result["events"] += 1
                    db.add_event(
                        "spool", "Катушка отвязана от слота",
                        f"{label}: слот опустел — катушка возвращена на склад",
                        printer_id, {"spool_id": by_slot["id"], "slot": slot})
                # Память слота не стирается: видно, что здесь стояло.
                # Пустой слот, о котором мы ничего не знали, не запоминаем —
                # иначе база зарастает строками «здесь никогда ничего не было».
                known = db.one("SELECT id FROM ams_slots WHERE printer_id=? AND slot=?",
                               (printer_id, slot))
                if known and remember_slot(db, printer_id, slot, state="empty", keep_last=True):
                    result["remembered"] += 1
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
                        slot_event(db, printer_id, slot, by_slot["id"], "auto_swap",
                                   f"{label}: в слоте другая катушка ({material or 'без типа'})")
                        result["events"] += 1
                        db.add_event(
                            "spool", "Катушка отвязана от слота",
                            f"{label}: в AMS теперь другая катушка",
                            printer_id, {"spool_id": by_slot["id"], "slot": slot})
                    else:
                        spool = by_slot

        if spool:
            previous = db.one("SELECT * FROM ams_slots WHERE printer_id=? AND slot=?",
                              (printer_id, slot))
            was_other = bool(previous) and str(previous.get("spool_id") or "") != str(spool["id"])
            was_empty = bool(previous) and previous.get("state") == "empty"
            if remember_slot(db, printer_id, slot, spool_id=spool["id"],
                             tray_uuid=tray_uuid, material=material,
                             color_name=str(spool.get("color_name") or ""),
                             color_hex=str(spool.get("color_hex") or ""),
                             label=str(label), remain_pct=num(remain, -1),
                             grams_left=num(spool.get("remaining_grams"))):
                result["remembered"] += 1
            if not previous or was_other or was_empty:
                slot_event(db, printer_id, slot, spool["id"], "auto_bind",
                           f"{label}: {material or 'без типа'} — катушка со склада")
                result["events"] += 1
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
        elif not (auto_create and material):
            # Пластик в слоте есть, а катушки на складе нет и заводить её
            # нельзя (автосоздание выключено или тип не сообщён): помним хотя
            # бы то, что принтер рассказал сам, — иначе память слота пуста.
            previous = db.one("SELECT * FROM ams_slots WHERE printer_id=? AND slot=?",
                              (printer_id, slot))
            if remember_slot(db, printer_id, slot, tray_uuid=tray_uuid,
                             material=material, color_name=_hex_to_name(color) if color else "",
                             color_hex=color, label=str(label),
                             remain_pct=num(remain, -1)):
                result["remembered"] += 1
            if previous and previous.get("state") == "empty":
                slot_event(db, printer_id, slot, "", "auto_seen",
                           f"{label}: {material or 'пластик без типа'} — катушки нет на складе")
                result["events"] += 1
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
            remember_slot(db, printer_id, slot, spool_id=str(row.get("id") or ""),
                          tray_uuid=tray_uuid, material=material,
                          color_name=cname, color_hex=hex_norm, label=str(label),
                          remain_pct=num(remain, -1), grams_left=remaining)
            result["remembered"] += 1
            slot_event(db, printer_id, slot, str(row.get("id") or ""), "auto_create",
                       f"{label}: {material}, остаток {round(num(remain, 100))}% — заведена из AMS")
            result["events"] += 1
            db.add_event(
                "spool", "Катушка добавлена из AMS",
                f"{label}: {material}, остаток {round(num(remain, 100))}%."
                " Уточните бренд, цвет и цену в карточке склада.",
                printer_id, {"spool_id": row.get("id"), "slot": slot,
                             "tray_uuid": tray_uuid})
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

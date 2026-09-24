"""Односторонняя запись настроек катушки в AMS, с паузой во время печати.

Используем ровно команду ``ams_filament`` из BambuPrinter: тот же пресет Bambu,
что и при ручной привязке. Повтор того же набора хранится в SQLite, а не только
в памяти процесса. Команда MQTT может не подтвердиться — сохраняем сбой в
журнале, а не говорим владельцу, что принтер принял изменения.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .accounting import num
from .ams_actions import record
from .ams_defaults import hex_color
from .ams_sync import slot_number
from .config import now_iso
from .materials import bambu_filament_preset, get_material


PUSH_MINUTES = 30


def write_allowed(db, snap: dict) -> tuple[bool, str]:
    """Общий предохранитель для автозаписи и ручных команд AMS."""
    if not db.setting("ams_push_settings", True):
        return False, "Запись слотов AMS отключена в настройках"
    state = str((snap.get("printer") or {}).get("state") or "").upper()
    connection = snap.get("connection") or {}
    if state not in ("IDLE", "FINISH") or connection.get("connected") is not True:
        return False, "Принтер занят, не подключён или состояние неизвестно — запись AMS отложена"
    if "last_message" in connection:
        # Состояние IDLE из вчерашнего MQTT-отчёта не доказывает, что станок
        # свободен сейчас. Bambu отдаёт timestamp последнего сообщения.
        try:
            age = datetime.now(timezone.utc).timestamp() - float(connection["last_message"])
        except (TypeError, ValueError):
            age = float("inf")
        if age < -60 or age > 90:
            return False, "Данные принтера устарели — запись AMS отложена"
    return True, ""


def require_write_allowed(db, printer) -> None:
    try:
        allowed, reason = write_allowed(db, printer.snapshot())
    except Exception as exc:
        raise ValueError("Нет живого снимка принтера: запись AMS запрещена") from exc
    if not allowed:
        raise ValueError(reason)


def payload_for(db, spool: dict, slot: int) -> dict:
    """Один набор для MQTT (тип, цвет, бренд, сопло) со склада и каталога."""
    rec = spool.get("rec_settings") or ""
    try:
        rec = json.loads(rec) if isinstance(rec, str) and rec.strip().startswith("{") else {}
    except json.JSONDecodeError:
        rec = {}
    rec_temp = rec.get("temp_nozzle") if isinstance(rec, dict) else None
    catalog_temp = get_material(str(spool.get("material") or ""), db).get("temp_nozzle")
    preset = bambu_filament_preset(str(spool.get("material") or "PLA"),
                                   str(spool.get("brand") or ""), rec_temp, catalog_temp)
    return {"ams_id": slot // 4, "tray_id": slot % 4,
            "type": str(spool.get("material") or "PLA").strip().upper(),
            "color": hex_color(spool.get("color_hex")) or "#FFFFFF",
            "brand": str(spool.get("brand") or ""),
            "temp_min": preset["nozzle_temp_min"],
            "temp_max": preset["nozzle_temp_max"]}


def send_checked(db, printer, payload: dict, *, source: str = "manual",
                 spool_id: str = "") -> dict:
    """Единый шлюз ВСЕХ команд ams_filament (API, профили, очередь, автопилот).

    Сохраняем общий 30-минутный лимит по набору после перезапуска. При отказе
    MQTT пишем подробный журнал; отсутствие аппаратного ACK не приписываем себе.
    """
    require_write_allowed(db, printer)  # перечитываем живое состояние прямо перед MQTT
    if not isinstance(payload, dict):
        raise ValueError("Нужны настройки слота AMS")
    try:
        unit, local = int(payload["ams_id"]), int(payload["tray_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Нужны модуль AMS и слот") from exc
    if not 0 <= unit <= 3 or not 0 <= local <= 3:
        raise ValueError("Модуль AMS: 0–3; слот: 0–3")
    color = hex_color(payload.get("color"))
    if not color or color in ("#4B5563", "#333333", "#CBD5E1"):
        raise ValueError("Цвет катушки неизвестен — запись AMS не выполняется")
    if not str(payload.get("type") or "").strip():
        raise ValueError("Материал катушки неизвестен — запись AMS не выполняется")
    command = dict(payload)
    command.update({"ams_id": unit, "tray_id": local, "color": color,
                    "type": str(payload["type"]).strip().upper()})
    slot = str(unit * 4 + local)
    fingerprint = hashlib.sha256(json.dumps(command, sort_keys=True,
                                             ensure_ascii=False).encode("utf-8")).hexdigest()
    before = db.one("SELECT * FROM ams_push_state WHERE printer_id=? AND slot=?",
                    (printer.id, slot))
    if before and before["fingerprint"] == fingerprint:
        try:
            last = datetime.fromisoformat(before["sent_at"])
            age = (datetime.now(timezone.utc) - last.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            age = 999999
        if age < PUSH_MINUTES * 60:
            return {"ok": True, "sent": False, "skipped": True,
                    "reason": "Тот же набор AMS отправлен менее 30 минут назад"}
    before_state = {"command": command, "push_state": before}
    try:
        response = printer.command("ams_filament", command)
        if isinstance(response, dict) and response.get("ok") is False:
            raise RuntimeError(str(response.get("reason") or "Принтер отказал"))
    except Exception as exc:
        message = f"Слот {slot}: принтер не принял настройки ({exc})"
        db.add_event("ams", "Запись слота AMS не удалась", message, printer.id,
                     {"slot": slot, "spool_id": spool_id, "source": source})
        record(db, printer.id, slot, spool_id, "push_error", message,
               before_state, {"command": command, "error": str(exc)})
        raise
    sent_at = now_iso()
    db.execute("INSERT INTO ams_push_state(printer_id,slot,fingerprint,sent_at) VALUES(?,?,?,?)"
               " ON CONFLICT(printer_id,slot) DO UPDATE SET"
               " fingerprint=excluded.fingerprint, sent_at=excluded.sent_at",
               (printer.id, slot, fingerprint, sent_at))
    record(db, printer.id, slot, spool_id, "push",
           f"Слот {slot}: настройки отправлены ({source})", before_state,
           {"command": command, "push_state": {"printer_id": printer.id, "slot": slot,
                                                   "fingerprint": fingerprint, "sent_at": sent_at}})
    return {"ok": True, "sent": True, "skipped": False, "response": response}


def push(db, printer, snap: dict) -> dict:
    """Привести реальные слоты к складу, только когда станок свободен."""
    result = {"sent": 0, "skipped": 0, "errors": []}
    allowed, reason = write_allowed(db, snap)
    if not allowed:
        result["reason"] = reason
        return result
    for tray in (snap.get("ams") or {}).get("trays") or []:
        if tray.get("present") is False or tray.get("remain") == 0:
            continue
        try:
            slot = slot_number(tray)
        except (TypeError, ValueError):
            continue
        if not 0 <= slot <= 15:  # внешний слот не имеет ams_filament_setting
            continue
        bound = db.query("SELECT * FROM spools WHERE printer_id=? AND ams_slot=? AND archived=0"
                         " AND remaining_grams>0 AND ams_sync=1", (printer.id, str(slot)))
        if len(bound) != 1:  # дубли и ручной режим — никогда не угадываем, что писать
            result["skipped"] += 1
            continue
        spool = bound[0]
        if hex_color(spool.get("color_hex")) in ("#4B5563", "#333333", "#CBD5E1"):
            result["skipped"] += 1  # неизвестный цвет не отправляем как настоящий
            continue
        memory = db.one("SELECT * FROM ams_slots WHERE printer_id=? AND slot=?",
                        (printer.id, str(slot))) or {}
        if (memory.get("spool_id") != spool["id"] or memory.get("state") != "live"
                or (str(tray.get("uuid") or "").strip() and memory.get("tray_uuid")
                    and str(tray["uuid"]).strip().upper() !=
                    str(memory["tray_uuid"]).strip().upper())):
            result["skipped"] += 1
            continue
        payload = payload_for(db, spool, slot)
        known = {
            "type": (str(tray.get("type") or "").upper(), str(payload["type"]).upper()),
            "color": (hex_color(tray.get("color")), hex_color(payload["color"])),
            "brand": (str(tray.get("brand") or "").strip().lower(),
                      str(payload["brand"] or "").strip().lower()),
        }
        different = any(actual and actual != wanted for actual, wanted in known.values())
        for key, desired in (("nozzle_min", "temp_min"), ("nozzle_max", "temp_max")):
            if tray.get(key) not in (None, "") and int(num(tray[key])) != payload[desired]:
                different = True
        complete = all(actual and actual == wanted for actual, wanted in known.values())
        complete = complete and all(tray.get(key) not in (None, "") for key in
                                    ("nozzle_min", "nozzle_max")) and not different
        if complete:
            result["skipped"] += 1
            continue
        label = str(tray.get("label") or f"Слот {slot}")
        try:
            sent = send_checked(db, printer, payload, source="auto", spool_id=spool["id"])
            result["sent" if sent["sent"] else "skipped"] += 1
        except Exception as exc:
            result["errors"].append(f"{label}: {exc}")
    return result

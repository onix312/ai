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

18.13: синк стал автопилотом. Раньше он только «верил датчику» и оставлял
после себя работу человеку: катушка заводилась на 1000 г и цену 0, переезд
катушки в другой слот выглядел как «отвязать и завести заново», сторонний
пластик без RFID не узнавался никогда, а кончившаяся катушка висела в слоте с
нулём грамм. Теперь каждое решение принимается один раз и записывается в
журнал действий (`ams_actions`) со снимком «до» и «после» — панель показывает
ленту, оператор может откатить любую строку:

  * значения новой катушки (масса, цена, бренд, имя цвета) берутся из
    `ams_defaults`: правило владельца → таблица материалов → встроенные;
  * катушка узнаётся по RFID-метке, а без метки — по недавней истории слотов
    и цвету; одна метка = одна катушка на складе;
  * переезд в другой слот — это перенос привязки, а не «снял и завёл заново»;
  * остаток 0 % — катушка помечается пустой и возвращается на склад; рост
    остатка на той же метке — долив; заметное падение без печати — потеря;
  * что не сходится между складом и слотом принтера (тип, цвет, температуры) —
    считается в `ams_push`, отправляет менеджер: синк остаётся без MQTT и
    проверяется тестами.

Всё внесённое автоматически можно править вручную:

  * материал, бренд, цвет, цену и вес катушки автосинк не трогает никогда —
    они принадлежат пользователю (и на них он учится, см. `ams_defaults`);
  * остаток и привязку к слоту автосинк обновляет только у катушек
    с включённой галочкой «Обновлять из AMS» (поле ams_sync = 1);
  * автосоздание и синхронизацию остатка можно выключить целиком
    в настройках (ams_auto_spools, ams_sync_remaining, printer_info_sync),
    а всю работу автопилота — тумблером `ams_autopilot`.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .accounting import num, uid
from .ams_actions import log_action, slot_snapshot, spool_snapshot
from .ams_defaults import color_name_for, normalize_hex, resolve_defaults
from .ams_push import print_in_progress, slot_diff, slot_signature
from .config import now_iso

# Пустой слот AMS отдаёт uuid из одних нулей — считаем его отсутствием метки.
ZERO_UUID = "0" * 32

#: Остаток, при котором катушка считается кончившейся (датчик врёт на ±1 %).
EMPTY_PCT = 0.5

#: Рост остатка на столько процентов и грамм — это долив, а не дрейф датчика.
RISE_STEP_PCT = 15.0
RISE_STEP_GRAMS = 40.0

#: Падение остатка на столько процентов без печати — похоже на потерю пластика.
DROP_STEP_PCT = 25.0

#: Сколько минут работает подсказка «катушку вынули отсюда» при узнавании
#: стороннего пластика без RFID: за это время её успевают переставить.
MOVE_WINDOW_MIN = 60

#: Порог ΔE, при котором катушку без метки ещё считаем той же самой по цвету.
GENERIC_COLOR_TOLERANCE = 30.0


def _hex_to_name(value: str) -> str:
    """Hex #RRGGBB → человеческое имя (полный словарь, см. `ams_defaults`)."""
    return color_name_for(value)


def _normalize_hex(value: str) -> str:
    return normalize_hex(value)


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


def _brand_hint(tray: dict) -> str:
    """Бренд, который сообщил принтер: RFID-катушка Bambu — «Bambu Lab»."""
    brand = str(tray.get("brand") or "").strip()
    if brand:
        return brand
    if tray.get("bambulab") is True:
        return "Bambu Lab"
    return ""


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


def _spool_by_slot(db, printer_id: str, slot: Any) -> dict | None:
    """Катушка склада, привязанная к слоту принтера (не архивная)."""
    if not printer_id or slot in (None, ""):
        return None
    return db.one("SELECT * FROM spools WHERE printer_id=? AND ams_slot=? AND archived=0",
                  (printer_id, str(slot)))


def _adopt_generic(db, printer_id: str, material: str, color_hex: str,
                   exclude_slot: Any = None) -> tuple[dict | None, str]:
    """Узнать катушку без RFID по недавней истории слотов и цвету.

    Сторонний пластик в AMS не отдаёт ни метки, ни бренда: принтер видит
    только тип и цвет. Такую катушку владелец обычно переставляет руками, а
    склад об этом не знает — и на складе остаётся висеть «снятая» катушка,
    пока в слоте живёт вторая, заведённая с нуля.

    Признак: катушка без метки недавно уехала из слота этого же принтера
    (запись ``auto_unbind``/``auto_swap``/``unbind`` в истории), её материал
    совпадает, а цвет близок по ΔE. Кандидат должен быть **один** — если
    подходят две катушки, автопилот молчит: угадывать наугад здесь дороже,
    чем спросить человека.
    """
    since = (datetime.now() - timedelta(minutes=MOVE_WINDOW_MIN)).isoformat()
    rows = db.query(
        "SELECT spool_id, slot, at FROM ams_slot_history"
        " WHERE printer_id=? AND at>=? AND action IN ('auto_unbind','auto_swap','unbind')"
        " ORDER BY at DESC LIMIT 20", (printer_id, since))
    candidates: list[dict] = []
    material_key = str(material or "").strip().upper()
    for row in rows:
        spool = db.one("SELECT * FROM spools WHERE id=? AND archived=0 AND"
                       " COALESCE(ams_slot,'')=''", (row.get("spool_id"),))
        if not spool or _clean_uuid(spool.get("tray_uuid")):
            continue
        if material_key and str(spool.get("material") or "").strip().upper() != material_key:
            continue
        spool_hex = normalize_hex(spool.get("color_hex"))
        if color_hex and spool_hex:
            from .estimate import color_distance
            if color_distance(color_hex, spool_hex) > GENERIC_COLOR_TOLERANCE:
                continue
        if any(item["id"] == spool["id"] for item in candidates):
            continue
        candidates.append(spool)
    if len(candidates) != 1:
        return None, (f"подходящих катушек: {len(candidates)}" if candidates else "")
    return candidates[0], ""


def _mark_empty(db, printer_id: str, slot: Any, spool: dict, tray: dict,
                label: str) -> dict:
    """Катушка кончилась по датчику: пустая, на склад, слот свободен.

    RFID-метку у пустой катушки сохраняем — она по-прежнему опознаёт физическую
    бобину. Если владелец её перемотает и вставит снова, синк узнает катушку по
    метке и не заведёт вторую карточку (инвариант «одна метка = одна катушка»).
    """
    before = {"spool": spool_snapshot(db, spool["id"]),
              "slot": slot_snapshot(db, printer_id, slot)}
    db.execute(
        "UPDATE spools SET remaining_grams=0, ams_state='empty', ams_slot='',"
        " location='shop', synced_at=?, updated_at=? WHERE id=?",
        (now_iso(), now_iso(), spool["id"]))
    remember_slot(db, printer_id, slot, spool_id=str(spool["id"]),
                  material=str(spool.get("material") or ""),
                  color_name=str(spool.get("color_name") or ""),
                  color_hex=str(spool.get("color_hex") or ""),
                  label=str(label), remain_pct=0.0, grams_left=0.0,
                  keep_last=True)
    slot_event(db, printer_id, slot, str(spool["id"]), "auto_empty",
               f"{label}: датчик показал 0 % — катушка пустая")
    title = f"{label}: {spool.get('material') or ''} {spool.get('color_name') or ''}".strip()
    detail = (f"Датчик AMS показал 0 % — катушка «{title}» помечена пустой и"
              " возвращена на склад")
    action = log_action(db, "empty", "Катушка кончилась в слоте",
                        printer_id=printer_id, slot=slot, spool_id=str(spool["id"]),
                        detail=detail, before=before,
                        after={"spool": spool_snapshot(db, spool["id"])})
    db.add_event("spool", "Катушка кончилась в слоте", detail, printer_id,
                 {"spool_id": spool["id"], "slot": str(slot), "action_id": action["id"]})
    return action


def _unbind(db, printer_id: str, slot: Any, spool: dict, reason: str,
            detail: str, action_kind: str = "unbind") -> dict:
    """Отвязать катушку от слота и вернуть её на склад (одним действием)."""
    before = {"spool": spool_snapshot(db, spool["id"]),
              "slot": slot_snapshot(db, printer_id, slot)}
    db.execute(
        "UPDATE spools SET ams_slot='', tray_uuid='', location='shop',"
        " synced_at=?, updated_at=? WHERE id=?",
        (now_iso(), now_iso(), spool["id"]))
    slot_event(db, printer_id, slot, str(spool["id"]), reason, detail)
    action = log_action(db, action_kind, "Катушка отвязана от слота",
                        printer_id=printer_id, slot=slot, spool_id=str(spool["id"]),
                        detail=detail, before=before,
                        after={"spool": spool_snapshot(db, spool["id"])})
    db.add_event("spool", "Катушка отвязана от слота", detail, printer_id,
                 {"spool_id": spool["id"], "slot": str(slot), "action_id": action["id"]})
    return action


def sync_ams_spools(db, printer_id: str, snap: dict) -> dict:
    """Свести катушки в AMS с таблицей spools.

    Возвращает счётчики: created / updated / unbound / moved / adopted / empty /
    remembered / events и список ``pushes`` — что не сходится между складом и
    слотом принтера (отправляет менеджер, см. `ams_push`).
    """
    result: dict[str, Any] = {"created": 0, "updated": 0, "unbound": 0,
                              "remembered": 0, "events": 0, "moved": 0,
                              "adopted": 0, "empty": 0, "pushes": [],
                              "notify": []}
    trays = (snap.get("ams") or {}).get("trays") or []
    if not trays:
        return result
    auto_create = bool(db.setting("ams_auto_spools", True))
    sync_remaining = bool(db.setting("ams_sync_remaining", True))
    autopilot = bool(db.setting("ams_autopilot", True))
    adopt_generic = autopilot and bool(db.setting("ams_adopt_generic", True))
    printing = print_in_progress(snap)

    for tray in trays:
        tray_uuid = _clean_uuid(tray.get("uuid"))
        material = str(tray.get("type") or "").strip()
        slot = "" if tray.get("slot") is None else str(tray.get("slot"))
        label = tray.get("label") or (f"Слот {slot}" if slot else "AMS")
        remain = tray.get("remain")
        color = normalize_hex(str(tray.get("color") or ""))
        generic = _tray_generic(tray)

        if not _tray_occupied(tray):
            if slot != "":
                by_slot = _spool_by_slot(db, printer_id, slot)
                if by_slot and int(num(by_slot.get("ams_sync"), 1)) == 1:
                    if autopilot:
                        _unbind(db, printer_id, slot, by_slot, "auto_unbind",
                                f"{label}: слот опустел — катушка возвращена на склад")
                        result["notify"].append({
                            "kind": "unbind",
                            "title": "Катушка снята со слота",
                            "detail": (f"{label}: слот опустел, "
                                       f"{by_slot.get('material') or ''} "
                                       f"{by_slot.get('color_name') or ''} вернулась на склад"),
                        })
                    else:
                        db.execute(
                            "UPDATE spools SET ams_slot='', tray_uuid='', location='shop',"
                            " updated_at=? WHERE id=?",
                            (now_iso(), by_slot["id"]))
                    result["unbound"] += 1
                    result["events"] += 1
                # Память слота не стирается: видно, что здесь стояло.
                # Пустой слот, о котором мы ничего не знали, не запоминаем —
                # иначе база зарастает строками «здесь никогда ничего не было».
                known = db.one("SELECT id FROM ams_slots WHERE printer_id=? AND slot=?",
                               (printer_id, slot))
                if known and remember_slot(db, printer_id, slot, state="empty",
                                           keep_last=True):
                    result["remembered"] += 1
            continue

        spool = None
        if tray_uuid:
            spool = db.one("SELECT * FROM spools WHERE tray_uuid=? AND archived=0",
                           (tray_uuid,))
        if not spool and slot != "":
            by_slot = _spool_by_slot(db, printer_id, slot)
            if by_slot:
                if int(num(by_slot.get("ams_sync"), 1)) != 1:
                    spool = by_slot
                else:
                    old_uuid = _clean_uuid(by_slot.get("tray_uuid"))
                    swapped = bool(tray_uuid and old_uuid and old_uuid != tray_uuid)
                    replaced_by_generic = bool(old_uuid and not tray_uuid and generic)
                    if swapped or replaced_by_generic:
                        if autopilot:
                            reason = f"{label}: в слоте другая катушка ({material or 'без типа'})"
                            _unbind(db, printer_id, slot, by_slot,
                                    "auto_swap" if swapped else "auto_unbind", reason,
                                    action_kind="unbind")
                            result["notify"].append({
                                "kind": "unbind",
                                "title": "В слот поставили другую катушку",
                                "detail": (f"{label}: {by_slot.get('material') or ''} "
                                           f"{by_slot.get('color_name') or ''} снята со слота,"
                                           " учёт вернулся на склад"),
                            })
                        else:
                            db.execute(
                                "UPDATE spools SET ams_slot='', tray_uuid='', location='shop',"
                                " updated_at=? WHERE id=?",
                                (now_iso(), by_slot["id"]))
                            slot_event(db, printer_id, slot, by_slot["id"], "auto_swap",
                                       f"{label}: в слоте другая катушка")
                        result["unbound"] += 1
                        result["events"] += 1
                    else:
                        spool = by_slot
        adopted_note = ""
        ambiguous_generic = False
        if not spool and adopt_generic and slot != "" and not tray_uuid:
            candidate, why = _adopt_generic(db, printer_id, material, color,
                                            exclude_slot=slot)
            if candidate:
                spool = candidate
                adopted_note = why
                result["adopted"] += 1
            elif why:
                # Подходящих катушек несколько: заводить ещё одну — значит
                # плодить дубли, от которых владелец и хотел избавиться.
                ambiguous_generic = True
                # Кандидатов несколько — молчим, но оставляем след в журнале:
                # иначе оператор не поймёт, почему катушка «не привязалась сама».
                log_action(db, "skip", "Сторонний пластик не узнан",
                           printer_id=printer_id, slot=slot,
                           detail=f"{label}: {material or 'без типа'} — {why}."
                                  " Привяжите катушку вручную в докторе AMS",
                           undoable=False)

        if spool:
            previous = db.one("SELECT * FROM ams_slots WHERE printer_id=? AND slot=?",
                              (printer_id, slot))
            was_other = bool(previous) and str(previous.get("spool_id") or "") != str(spool["id"])
            was_empty = bool(previous) and previous.get("state") == "empty"
            old_printer = str(spool.get("printer_id") or "")
            old_slot = str(spool.get("ams_slot") or "")
            if remember_slot(db, printer_id, slot, spool_id=spool["id"],
                             tray_uuid=tray_uuid or _clean_uuid(spool.get("tray_uuid")),
                             material=material or str(spool.get("material") or ""),
                             color_name=str(spool.get("color_name") or ""),
                             color_hex=str(spool.get("color_hex") or ""),
                             label=str(label), remain_pct=num(remain, -1),
                             grams_left=num(spool.get("remaining_grams"))):
                result["remembered"] += 1
            if not previous or was_other or was_empty:
                slot_event(db, printer_id, slot, spool["id"], "auto_bind",
                           f"{label}: {material or 'без типа'} — катушка со склада")
                result["events"] += 1

            # --- перенос катушки: та же метка, другой слот (или другой принтер)
            moved = bool(old_slot != slot or (old_printer and old_printer != printer_id))
            if autopilot and (moved or was_empty) and spool:
                where_from = (f"из слота {int(num(old_slot, 0)) + 1}"
                              if old_slot not in ("", None) else "со склада")
                if old_printer and old_printer != printer_id:
                    where_from = "с другого принтера"
                kind = "move" if old_slot not in ("", None) else "bind"
                if not tray_uuid and adopted_note == "" and generic:
                    kind = "move" if old_slot not in ("", None) else "bind"
                log_action(db, kind,
                           "Катушка переехала в другой слот" if kind == "move"
                           else "Катушка привязана к слоту",
                           printer_id=printer_id, slot=slot, spool_id=str(spool["id"]),
                           detail=(f"{label}: {spool.get('material') or ''} "
                                   f"{spool.get('color_name') or ''} — "
                                   f"{'перенос ' + where_from if kind == 'move' else 'привязка по RFID'}"),
                           before={"spool": spool_snapshot(db, spool["id"]),
                                   "slot": slot_snapshot(db, printer_id, slot)})
                if kind == "move":
                    result["moved"] += 1

            # --- кончилась по датчику
            already_empty = (num(spool.get("remaining_grams")) <= 0
                             and str(spool.get("ams_state") or "") == "empty")
            if (autopilot and not already_empty and remain is not None
                    and num(remain, -1) >= 0 and num(remain) <= EMPTY_PCT):
                _mark_empty(db, printer_id, slot, spool, tray, str(label))
                result["unbound"] += 1
                result["empty"] += 1
                result["events"] += 1
                result["notify"].append({
                    "kind": "runout" if printing else "empty",
                    "title": ("Пластик кончился во время печати" if printing
                              else "Катушка кончилась в слоте"),
                    "detail": (f"{label}: {spool.get('material') or ''} "
                               f"{spool.get('color_name') or ''} — 0 %, убрана со слота"),
                })
                continue

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
            if str(spool.get("ams_state") or "") != "ok":
                updates.append("ams_state=?")
                params.append("ok")
            fresh = num(spool.get("remaining_grams"))
            if sync_remaining and remain is not None and num(remain, -1) >= 0:
                total = max(1.0, num(spool.get("total_grams"), 1000))
                candidate = round(min(100.0, num(remain)) / 100.0 * total, 1)
                if abs(candidate - num(spool.get("remaining_grams"))) > 1.0:
                    fresh = candidate
                    updates.append("remaining_grams=?")
                    params.append(candidate)

            # --- рост остатка: долив или перемотка
            previous_pct = num((previous or {}).get("remain_pct"), -1)
            now_pct = num(remain, -1)
            if (autopilot and previous_pct >= 0 and now_pct >= 0
                    and now_pct - previous_pct >= RISE_STEP_PCT
                    and fresh - num(spool.get("remaining_grams")) >= RISE_STEP_GRAMS):
                log_action(db, "fill", "Остаток вырос — катушку долили",
                           printer_id=printer_id, slot=slot, spool_id=str(spool["id"]),
                           detail=(f"{label}: было {round(previous_pct)} %, стало"
                                   f" {round(now_pct)} % — остаток поднят, метка и слот не тронуты"),
                           before={"spool": spool_snapshot(db, spool["id"])},
                           after={"spool": {**spool_snapshot(db, spool["id"]),
                                            "remaining_grams": fresh}})
            # --- падение остатка без печати: похоже на потерю пластика
            if (previous_pct >= 0 and now_pct >= 0 and not printing
                    and previous_pct - now_pct >= DROP_STEP_PCT
                    and num(spool.get("remaining_grams")) - fresh >= 50):
                log_action(db, "loss", "Остаток упал без печати",
                           printer_id=printer_id, slot=slot, spool_id=str(spool["id"]),
                           detail=(f"{label}: было {round(previous_pct)} %, стало"
                                   f" {round(now_pct)} % — станок не печатал."
                                   " Проверьте: обрыв, чистка или сняли часть пластика"),
                           before={"spool": spool_snapshot(db, spool["id"])},
                           after={"spool": {**spool_snapshot(db, spool["id"]),
                                            "remaining_grams": fresh}},
                           undoable=False)
                db.add_event("ams", "Потеря пластика в слоте",
                             f"{label}: остаток упал с {round(previous_pct)} % до"
                             f" {round(now_pct)} % без печати", printer_id,
                             {"slot": slot, "spool_id": spool["id"]})
                result["notify"].append({
                    "kind": "loss",
                    "title": "Потеря пластика в слоте",
                    "detail": (f"{label}: остаток упал с {round(previous_pct)} % до"
                               f" {round(now_pct)} % без печати"),
                })
            if color:
                cur_hex = normalize_hex(str(spool.get("color_hex") or ""))
                cur_name = str(spool.get("color_name") or "").strip()
                if not cur_name and color != cur_hex:
                    cname = color_name_for(color)
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
                             material=material,
                             color_name=color_name_for(color) if color else "",
                             color_hex=color, label=str(label),
                             remain_pct=num(remain, -1)):
                result["remembered"] += 1
            if previous and previous.get("state") == "empty":
                slot_event(db, printer_id, slot, "", "auto_seen",
                           f"{label}: {material or 'пластик без типа'} — катушки нет на складе")
                result["events"] += 1
        elif auto_create and material and not ambiguous_generic:
            defaults = resolve_defaults(db, material=material, color_hex=color,
                                        brand_hint=_brand_hint(tray))
            remaining = defaults["total_grams"]
            if remain is not None and num(remain, -1) >= 0:
                remaining = round(min(100.0, max(0.0, num(remain))) / 100.0
                                  * defaults["total_grams"], 1)
            row = db.upsert("spools", {
                "id": uid("sp"),
                "material": defaults["material"],
                "brand": defaults["brand"],
                "color_name": defaults["color_name"],
                "color_hex": defaults["color_hex"],
                "total_grams": defaults["total_grams"],
                "remaining_grams": remaining,
                "price": defaults["price"],
                "printer_id": printer_id,
                "ams_slot": slot,
                "tray_uuid": tray_uuid,
                "location": "ams",
                "ams_sync": 1,
                "ams_auto": 1,
                "ams_state": "ok",
                "synced_at": now_iso(),
                "verified": defaults["verified"],
                "note": defaults["note"] or "Заведено автопилотом AMS",
                "archived": 0,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            })
            result["created"] += 1
            remember_slot(db, printer_id, slot, spool_id=str(row.get("id") or ""),
                          tray_uuid=tray_uuid, material=defaults["material"],
                          color_name=defaults["color_name"],
                          color_hex=defaults["color_hex"], label=str(label),
                          remain_pct=num(remain, -1), grams_left=remaining)
            result["remembered"] += 1
            slot_event(db, printer_id, slot, str(row.get("id") or ""), "auto_create",
                       f"{label}: {material}, остаток {round(num(remain, 100))}% — заведена из AMS")
            result["events"] += 1
            if autopilot:
                log_action(
                    db, "create", "Завёл катушку из AMS", printer_id=printer_id,
                    slot=slot, spool_id=str(row.get("id") or ""),
                    detail=(f"{label}: {defaults['material']} "
                            f"{defaults['color_name']}, {round(num(remain, 100))} %"
                            + (f", {defaults['brand']}" if defaults["brand"] else "")
                            + (f". {defaults['note']}" if defaults["note"] else "")),
                    after={"spool_id": row.get("id"), "slot": slot,
                           "source": defaults["source"]})
            db.add_event(
                "spool", "Катушка добавлена из AMS",
                f"{label}: {material}, остаток {round(num(remain, 100))}%."
                + (f" {defaults['note']}." if defaults["note"] else
                   " Значения подставлены по правилам склада."),
                printer_id, {"spool_id": row.get("id"), "slot": slot,
                             "tray_uuid": tray_uuid, "source": defaults["source"]})

    if autopilot and db.setting("ams_push_settings", True):
        result["pushes"] = _slot_push_plan(db, printer_id, trays, printing)
        for item in result["pushes"]:
            type_diff = next((row for row in item.get("diff") or []
                              if str(row.get("field")) == "type"), None)
            if not type_diff:
                continue
            result["notify"].append({
                "kind": "conflict",
                "title": "В слоте не тот пластик",
                "detail": (f"{item['label']}: в слоте {type_diff.get('actual') or '—'},"
                           f" по складу {type_diff.get('want') or '—'} —"
                           " настройки слота приведены к складу"),
            })
    return result


def _slot_push_plan(db, printer_id: str, trays: list[dict],
                    printing: bool) -> list[dict]:
    """Что не сходится между складом и слотами: план отправки в принтер.

    Автопилот правит слот по складу, но не спорит с печатью: пока станок
    печатает, план пустой. Повторная отправка того же набора не уходит раньше
    ``ams_push_retry_min`` минут — иначе одна и та же команда летела бы в MQTT
    на каждом проходе синка.
    """
    plan: list[dict] = []
    if printing:
        return plan
    retry_min = max(1.0, num(db.setting("ams_push_retry_min", 30.0), 30.0))
    for tray in trays:
        if not _tray_occupied(tray):
            continue
        slot = tray.get("slot")
        if slot in (None, ""):
            continue
        spool = _spool_by_slot(db, printer_id, slot)
        if not spool or str(spool.get("location") or "") != "ams":
            continue
        diff = _slot_diff(tray, spool)
        if not diff:
            continue
        signature = slot_signature(spool) + "#" + ",".join(
            sorted(str(item.get("field")) for item in diff))
        memory = db.one("SELECT pushed_sig, pushed_at FROM ams_slots"
                        " WHERE printer_id=? AND slot=?", (printer_id, str(slot)))
        if memory and str(memory.get("pushed_sig") or "") == signature:
            stamp = str(memory.get("pushed_at") or "").strip()
            if stamp and not _is_stale_minutes(stamp, retry_min):
                continue
        plan.append({
            "slot": int(num(slot, 254)),
            "spool_id": str(spool["id"]),
            "diff": diff,
            "signature": signature,
            "label": str(tray.get("label") or f"Слот {slot}"),
            "previous": {"type": str(tray.get("type") or ""),
                         "color": normalize_hex(tray.get("color")) or "",
                         "temp_min": int(num(tray.get("nozzle_min"))),
                         "temp_max": int(num(tray.get("nozzle_max"))),
                         "brand": _brand_hint(tray)},
        })
    return plan


def _is_stale_minutes(stamp: str, minutes: float) -> bool:
    """Прошло ли с момента отметки времени больше указанных минут."""
    try:
        seen = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return True
    now = datetime.now(seen.tzinfo) if seen.tzinfo else datetime.now()
    return (now - seen) > timedelta(minutes=minutes)


def _slot_diff(tray: dict, spool: dict) -> list[dict]:
    """Обёртка над `ams_push.slot_diff` — вынесена, чтобы её было видно в тестах."""
    return slot_diff(tray, spool)


def mark_slot_pushed(db, printer_id: str, slot: Any, signature: str) -> None:
    """Запомнить, что раскладка слота отправлена в принтер."""
    if not printer_id or slot in (None, ""):
        return
    row = db.one("SELECT id FROM ams_slots WHERE printer_id=? AND slot=?",
                 (printer_id, str(slot)))
    if not row:
        return
    db.execute("UPDATE ams_slots SET pushed_sig=?, pushed_at=? WHERE id=?",
               (str(signature or ""), now_iso(), row["id"]))


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


def spool_by_id(db, spool_id: str) -> dict | None:
    """Катушка по идентификатору: маршрутам нужны её принтер и слот."""
    if not spool_id:
        return None
    row = db.one("SELECT * FROM spools WHERE id=?", (str(spool_id),))
    return dict(row) if row else None


def accept_sensor(db, *, spool_id: str, remain_pct: Any, slot: str = "",
                  printer_id: str = "", tray_label: str = "") -> dict:
    """Принять остаток из AMS: склад подстраивается под датчик.

    Отдельным действием, а не автоматом. При большом расхождении система не
    знает, врёт датчик или в слоте другая катушка; человек смотрит на слот и
    нажимает «принять» — и остаток склада становится таким, как показывает
    принтер. Правка идёт через журнал (`ams_actions`), поэтому откатывается
    одной кнопкой, как и всё остальное у автопилота.
    """
    spool = spool_by_id(db, spool_id)
    if not spool:
        raise ValueError("Катушка не найдена")
    slot = str(slot or spool.get("ams_slot") or "")
    printer_id = str(printer_id or spool.get("printer_id") or "")
    if not printer_id:
        raise ValueError("У катушки не указан принтер — нечего принимать")
    remain = num(remain_pct, -1)
    if remain < 0:
        raise ValueError("Датчик AMS не сообщил остаток — принимать нечего")
    total = max(1.0, num(spool.get("total_grams"), 1000))
    fresh = round(min(100.0, remain) / 100.0 * total, 1)
    label = str(tray_label or (f"Слот {slot}" if slot else "AMS"))
    before = {"spool": spool_snapshot(db, spool_id),
              "slot": slot_snapshot(db, printer_id, slot)}
    action = log_action(
        db, "accept", "Принят остаток из AMS",
        printer_id=printer_id, slot=slot, spool_id=spool_id,
        detail=(f"{label}: датчик {round(remain)} % → {fresh} г вместо "
                f"{round(num(spool.get('remaining_grams')))} г"),
        before=before,
        after={"spool": {**before["spool"], "remaining_grams": fresh}})
    db.execute(
        "UPDATE spools SET remaining_grams=?, ams_state='ok', verified=1,"
        " synced_at=?, updated_at=? WHERE id=?",
        (fresh, now_iso(), now_iso(), spool_id))
    db.add_event("ams", "Остаток принят из AMS",
                 f"Катушка {spool.get('material') or ''} "
                 f"{spool.get('color_name') or ''}: {fresh} г", printer_id,
                 {"spool_id": spool_id, "slot": slot, "action_id": action["id"]})
    return {"spool": spool_by_id(db, spool_id) or {}, "remaining_grams": fresh,
            "action_id": action["id"], "slot": slot}


#: Публичные псевдонимы: их читают доктор (`ams_doctor`) и пульт цеха, чтобы не
#: повторять одну и ту же логику «что в слоте» в двух файлах.
spool_by_slot = _spool_by_slot
tray_occupied = _tray_occupied

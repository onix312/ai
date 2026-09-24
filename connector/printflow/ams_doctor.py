"""Офлайн-диагностика AMS и советующая раскладка (без команд принтеру)."""
from __future__ import annotations

from .accounting import num
from .ams_sync import slot_memory, slot_number
from .materials import is_hygroscopic


def doctor(db, printer_id: str = "", snapshot: dict | None = None) -> dict:
    """Коды проблем пригодны панели; отсутствие телеметрии — не «исправно»."""
    issues: list[dict] = []

    def add(code, level, title, detail, pid="", slot=""):
        issues.append({"code": code, "level": level, "title": title,
                       "detail": detail, "printer_id": pid, "slot": str(slot)})

    memory = slot_memory(db, printer_id)
    for row in memory:
        pid, slot = str(row.get("printer_id") or ""), str(row.get("slot") or "")
        if row.get("state") == "unrecognized":
            add("conflict", "error", "Неоднозначная катушка",
                f"{row.get('label') or slot}: нужна ручная привязка в складе.", pid, slot)
        if row.get("state") == "live" and not row.get("spool_id"):
            add("unbound", "warning", "Слот без карточки",
                f"{row.get('label') or slot}: катушка видна, но не привязана к складу.", pid, slot)
        if row.get("state") == "live" and row.get("stale"):
            add("stale", "info", "Устаревший снимок AMS",
                f"{row.get('label') or slot}: последние данные {row.get('seen_at') or 'неизвестны'}.", pid, slot)
        if row.get("state") == "runout":
            add("runout", "warning", "Слот опустел",
                f"{row.get('label') or slot}: остаток 0%, смените катушку перед печатью.", pid, slot)
    spools = db.query("SELECT * FROM spools WHERE archived=0" +
                      (" AND printer_id=?" if printer_id else ""),
                      (printer_id,) if printer_id else ())
    positions: dict[tuple[str, str], list[str]] = {}
    rfids: dict[str, list[dict]] = {}
    for sp in spools:
        if (sp.get("ams_slot") and str(sp.get("price_source") or "") and
                str(sp.get("color_hex") or "").upper() in ("#4B5563", "#333333", "#CBD5E1")):
            add("color_unknown", "warning", "Цвет катушки неизвестен",
                f"Катушка {sp['id']}: запись цвета в AMS отложена до достоверной телеметрии.",
                str(sp.get("printer_id") or ""), str(sp.get("ams_slot") or ""))
        if sp.get("printer_id") and str(sp.get("ams_slot") or ""):
            key = str(sp["printer_id"]), str(sp["ams_slot"])
            positions.setdefault(key, []).append(sp["id"])
    # RFID — инвариант между ВСЕМИ принтерами, даже если смотрим один AMS.
    for sp in db.query("SELECT id, printer_id, tray_uuid FROM spools WHERE archived=0"
                       " AND tray_uuid IS NOT NULL AND tray_uuid<>''"):
        rfid = str(sp["tray_uuid"]).strip().lower()
        if rfid:
            rfids.setdefault(rfid, []).append(sp)
    for (pid, slot), ids in positions.items():
        if len(ids) > 1:
            add("duplicate_slot", "error", "Две карточки на один слот",
                ", ".join(ids) + ": привяжите вручную без автоархивации.", pid, slot)
    for rfid, cards in rfids.items():
        if len(cards) > 1 and (not printer_id or any(
                str(c.get("printer_id") or "") == printer_id for c in cards)):
            add("duplicate_rfid", "error", "RFID совпадает у нескольких карточек",
                ", ".join(c["id"] for c in cards) +
                ": старые записи не меняются автоматически.", printer_id)
    last_push: set[tuple[str, str]] = set()
    for row in db.query("SELECT * FROM ams_actions WHERE action IN ('push_error','push')"
                        " ORDER BY rowid DESC LIMIT 100"):
        pid, slot = str(row.get("printer_id") or ""), str(row.get("slot") or "")
        key = pid, slot
        if (printer_id and pid != printer_id) or key in last_push:
            continue
        last_push.add(key)
        if row["action"] == "push_error":
            add("push_error", "warning", "Запись настроек AMS не удалась",
                row.get("detail") or "Проверьте принтер", pid, slot)
    if snapshot:
        ams = snapshot.get("ams") or {}
        pid = printer_id or str(snapshot.get("id") or "")
        humidity = ams.get("humidity")
        if humidity is not None:
            try:
                limit = float(db.setting("dry_humidity_threshold", 55))
                if float(humidity) > limit:
                    hygroscopic = [str(t.get("type")) for t in ams.get("trays") or []
                                   if t.get("present") is not False and is_hygroscopic(str(t.get("type") or ""))]
                    if hygroscopic:
                        add("humidity", "warning", "Высокая влажность AMS",
                            f"{humidity}% > {limit:g}%: {', '.join(sorted(set(hygroscopic)))}. Проверьте осушитель.", pid)
            except (TypeError, ValueError):
                add("humidity_unknown", "info", "Нет показаний влажности", "Проверьте датчик AMS.", pid)
        by_position = {(str(s.get("printer_id") or ""), str(s.get("ams_slot") or "")): s
                       for s in spools if s.get("ams_slot")}
        for tray in ams.get("trays") or []:
            try:
                slot = slot_number(tray)
            except (TypeError, ValueError, KeyError):
                continue
            if not 0 <= slot <= 15 or tray.get("present") is False:
                continue
            bound = by_position.get((pid, str(slot)))
            if not bound:
                if not any(i["code"] in ("unbound", "conflict") and i["slot"] == str(slot)
                           for i in issues):
                    add("unbound", "warning", "Слот без карточки",
                        f"Слот {slot + 1} виден принтеру, но не привязан к складу.", pid, slot)
                continue
            uuid = str(tray.get("uuid") or "").strip().upper()
            saved_uuid = str(bound.get("tray_uuid") or "").strip().upper()
            if uuid and saved_uuid and uuid != saved_uuid:
                add("rfid_mismatch", "error", "RFID в слоте не совпадает со складом",
                    f"Слот {slot + 1}: ручная привязка {bound['id']} требует проверки.", pid, slot)
            material = str(tray.get("type") or "").strip().upper()
            if material and material != str(bound.get("material") or "").strip().upper():
                add("material_mismatch", "warning", "Материал слота отличается от склада",
                    f"Слот {slot + 1}: AMS сообщает {material}, склад — {bound.get('material') or 'неизвестно'}.",
                    pid, slot)
            color = str(tray.get("color") or "").strip().upper()
            saved = str(bound.get("color_hex") or "").strip().upper()
            if color and saved and color not in ("#4B5563", "#333333", "#CBD5E1") and saved not in ("#4B5563", "#333333", "#CBD5E1"):
                try:
                    from .estimate import color_distance
                    if color_distance(color, saved) > 30:
                        add("color_mismatch", "warning", "Цвет слота отличается от склада",
                            f"Слот {slot + 1}: AMS {color}, карточка {saved}.", pid, slot)
                except (TypeError, ValueError):
                    pass
        for problem in (snapshot.get("printer") or {}).get("problems") or []:
            text = str(problem.get("title") or problem.get("code") or "")
            code = str(problem.get("code") or "").upper()
            if ("ams" in text.lower() or "мотор" in text.lower() or
                    code.startswith(("0700", "0701", "0702", "07FF", "1200"))):
                add("hardware", "error" if problem.get("severity") in ("error", "fatal")
                    else "warning", "Ошибка механики AMS",
                    text + (" · " + str(problem["advice"]) if problem.get("advice") else ""), pid)
    else:
        add("offline", "info", "Нет живой телеметрии",
            "Диагноз построен по последнему снимку. Состояние мотора и влажность неизвестны.", printer_id)
    return {"ok": not any(i["level"] == "error" for i in issues),
            "printer_id": printer_id, "issues": issues,
            "counts": {level: sum(i["level"] == level for i in issues)
                       for level in ("error", "warning", "info")}}


def plan(db, printer_id: str, needs: list[dict] | None = None) -> dict:
    """Советы из ближайшей очереди: ничего не связывает и не отправляет в AMS."""
    if needs is None:
        needs = []
        jobs = db.query("SELECT j.id,j.order_id,o.material,o.color,o.grams,o.number"
                        " FROM print_jobs j JOIN orders o ON o.id=j.order_id"
                        " WHERE j.printer_id=? AND j.state='queued'"
                        " ORDER BY j.priority DESC, j.queued_at ASC LIMIT 12", (printer_id,))
        for job in jobs:
            if job.get("material"):
                needs.append({"material": str(job["material"]), "color": job.get("color") or "",
                              "grams": num(job.get("grams")),
                              "order": job.get("number") or job.get("order_id")})
    if not isinstance(needs, list):
        raise ValueError("Материалы должны быть списком")
    slots = slot_memory(db, printer_id)
    occupied = {str(s["slot"]): s for s in slots if s.get("state") == "live" and not s.get("stale")}
    empty_slots = sorted((str(s["slot"]) for s in slots
                          if s.get("state") == "empty" and not s.get("stale")), key=int)
    budgets = {s["id"]: num(s.get("remaining_grams")) for s in db.query(
        "SELECT id,remaining_grams FROM spools WHERE archived=0")}
    cards = db.query("SELECT * FROM spools WHERE archived=0 AND remaining_grams>0"
                     " AND (printer_id=? OR COALESCE(ams_slot,'')='')",
                     (printer_id,))
    advised = []
    for request in needs[:20]:
        if not isinstance(request, dict):
            continue
        material = str(request.get("material") or "").strip().upper()
        if not material:
            continue
        color = str(request.get("color") or "").strip().lower()
        grams = num(request.get("grams"))
        options = [s for s in cards if str(s.get("material") or "").upper() == material
                   and (not color or color in (str(s.get("color_name") or "").lower(),
                                              str(s.get("color_hex") or "").lower()))
                   and budgets.get(s["id"], 0) >= grams * 1.15]
        # Уже стоящие подходящие катушки прежде полок; запас по массе — дальше.
        options.sort(key=lambda s: (not (s.get("printer_id") == printer_id and
                                              str(s.get("ams_slot") or "") in occupied),
                                    -num(s.get("remaining_grams"))))
        chosen = options[0] if options else None
        if chosen:
            budgets[chosen["id"]] -= grams
        standing = bool(chosen and chosen.get("printer_id") == printer_id and
                        str(chosen.get("ams_slot") or "") in occupied)
        suggested_slot = (str(chosen.get("ams_slot") or "") if standing else
                          empty_slots.pop(0) if chosen and empty_slots else "")
        advised.append({"material": material, "color": request.get("color") or "",
                        "grams": grams, "order": request.get("order") or "",
                        "spool_id": chosen["id"] if chosen else "",
                        "slot": suggested_slot,
                        "suggestion": ("Уже в AMS" if standing else
                                       "Поставить со склада" if chosen else "Подходящей катушки нет"),
                        "remaining_grams": num(chosen.get("remaining_grams")) if chosen else 0})
    return {"printer_id": printer_id, "advice": advised,
            "note": "Только советы. Раскладка AMS и карточки не изменены."}

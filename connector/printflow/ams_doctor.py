"""AMS-доктор: что не так со слотами и что сделал автопилот (18.13).

Зачем. Автопилот закрывает ручную правку, но у него, как у всякой автоматики,
должен быть экран, где видно его работу и его сомнения: какая катушка не
проверена, где склад расходится с принтером, что он не смог узнать сам. Здесь
собирается один ответ на вопрос «с AMS всё в порядке?» — его показывают панель,
пульт и (при желании) бот.

Модуль читает, но не меняет: починка — это `ams_sync` (привязки и остатки),
`ams_push` (настройки слота) и `ams_actions` (откат).

Второй экран — план раскладки (`plan`): очередь заданий и склад превращаются в
совет «что в какой слот поставить, чтобы меньше дёргать катушки». Это именно
совет: раскладку PrintFlow не меняет, решение остаётся за человеком.
"""
from __future__ import annotations

from typing import Any

from .accounting import num
from .ams_actions import action_count, actions
from .ams_defaults import material_defaults, rules
from .ams_push import slot_diff
from .ams_sync import (MEMORY_STALE_MIN, EMPTY_PCT, spool_by_slot,
                       tray_occupied)
from .config import now_iso

#: Порядок важности проблем — по нему сортируется экран доктора.
SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}


def _issue(code: str, severity: str, title: str, detail: str, **extra: Any) -> dict:
    item = {"code": code, "severity": severity, "title": title, "detail": detail}
    item.update({key: value for key, value in extra.items() if value not in (None, "")})
    return item


def _slot_label(tray: dict) -> str:
    return str(tray.get("label") or f"Слот {num(tray.get('slot'), 0) + 1}")


def issues_for_snapshot(db, printer_id: str, snap: dict | None) -> list[dict]:
    """Проблемы слотов по живой телеметрии и складским привязкам.

    Без снимка (принтер выключен) возвращается пустой список: выдумывать
    проблемы по вчерашней памяти нельзя, об устаревшей памяти скажет отдельная
    проверка `issues_from_base`.
    """
    out: list[dict] = []
    if not snap:
        return out
    trays = ((snap.get("ams") or {}).get("trays")) or []
    printing = str((snap.get("printer") or {}).get("state") or "").upper() in (
        "RUNNING", "PREPARE", "PAUSE", "SLICING")
    for tray in trays:
        occupied = tray_occupied(tray)
        slot = tray.get("slot")
        label = _slot_label(tray)
        spool = spool_by_slot(db, printer_id, slot)
        if not occupied:
            if spool and num(spool.get("remaining_grams")) > 0:
                out.append(_issue(
                    "slot_empty_bound", "warn", "Слот пуст, а катушка привязана",
                    f"{label}: по складу здесь {spool.get('material') or ''} "
                    f"{spool.get('color_name') or ''}, принтер слот пустым не видит. "
                    "Отвяжите катушку или дождитесь следующего синка.",
                    slot=str(slot), spool_id=str(spool.get("id") or "")))
            continue
        if not spool:
            if str(tray.get("uuid") or "").strip():
                out.append(_issue(
                    "slot_unknown_rfid", "warn", "Катушка в слоте не заведена",
                    f"{label}: RFID-метка есть, катушки на складе нет. "
                    "Синхронизация заведёт её сама.",
                    slot=str(slot)))
            else:
                out.append(_issue(
                    "slot_unbound_generic", "info", "Сторонний пластик без привязки",
                    f"{label}: {tray.get('type') or 'тип не задан'} — катушка не "
                    "узнана. Привяжите её к складской или заведите новую.",
                    slot=str(slot)))
            continue

        diff = slot_diff(tray, spool)
        type_diff = next((row for row in diff if row.get("field") == "type"), None)
        if type_diff:
            out.append(_issue(
                "slot_type_mismatch", "error", "В слоте не тот пластик",
                f"{label}: в принтере {type_diff.get('actual') or '—'}, по складу "
                f"{type_diff.get('want') or '—'}. Автопилот приведёт слот к складу.",
                slot=str(slot), spool_id=str(spool.get("id") or "")))
        for row in diff:
            if row.get("field") == "type":
                continue
            out.append(_issue(
                f"slot_{row.get('field')}_mismatch", "warn",
                f"Слот: не сходится {row.get('label')}",
                f"{label}: в принтере {row.get('actual') or '—'}, по складу "
                f"{row.get('want') or '—'}.",
                slot=str(slot), spool_id=str(spool.get("id") or "")))

        remain = num(tray.get("remain"), -1)
        if remain >= 0 and remain <= EMPTY_PCT:
            out.append(_issue(
                "slot_empty_sensor", "error", "Катушка кончилась в слоте",
                f"{label}: датчик 0 % — замена нужна сейчас"
                + (" (печать идёт!)" if printing else ""),
                slot=str(slot), spool_id=str(spool.get("id") or "")))
        elif 0 < remain < 15:
            out.append(_issue(
                "slot_low", "warn", "Пластик кончается",
                f"{label}: осталось {round(remain)} % "
                f"({round(num(spool.get('remaining_grams')))} г)",
                slot=str(slot), spool_id=str(spool.get("id") or "")))
        if not int(num(spool.get("verified"), 1)):
            out.append(_issue(
                "spool_unverified", "warn", "Катушка из AMS не проверена",
                f"{label}: {spool.get('material') or ''} "
                f"{spool.get('color_name') or ''} — уточните массу бобины, цену и "
                "бренд: от них зависит себестоимость",
                slot=str(slot), spool_id=str(spool.get("id") or "")))
    # Влажность: PLA это не мешает, PETG/TPU/PA — уже проблема.
    humidity = num(((snap.get("ams") or {}).get("humidity")), -1)
    if humidity > 0:
        from .materials import is_hygroscopic
        wet = [tray for tray in trays if tray_occupied(tray)
               and is_hygroscopic(str(tray.get("type") or ""))]
        threshold = num(db.setting("dry_humidity_threshold", 55.0), 55.0)
        if wet and humidity >= threshold:
            out.append(_issue(
                "ams_humid", "warn", "В AMS влажно для этих материалов",
                f"Влажность {round(humidity)} % при пороге {round(threshold)} %: "
                + ", ".join(sorted({str(t.get("type")) for t in wet}))
                + " — просушите катушки (склад → катушка → «Просушка»)"))
    return out


def issues_from_base(db, printer_id: str = "") -> list[dict]:
    """Проблемы, которые видно без принтера: несвежая память, «пустые» в слоте."""
    from .ams_sync import slot_memory

    out: list[dict] = []
    for slot in slot_memory(db, printer_id):
        if slot.get("stale") and slot.get("state") != "empty":
            out.append(_issue(
                "memory_stale", "info", "Память слотов не свежая",
                f"{slot.get('label') or 'Слот'} {slot.get('slot')}: данные старше "
                f"{MEMORY_STALE_MIN} минут — принтер давно не выходил на связь",
                slot=str(slot.get("slot")), printer_id=str(slot.get("printer_id") or "")))
    sql = ("SELECT * FROM spools WHERE archived=0 AND COALESCE(ams_slot,'')<>''"
           " AND remaining_grams<=0")
    params: tuple = ()
    if printer_id:
        sql += " AND printer_id=?"
        params = (printer_id,)
    for spool in db.query(sql, params):
        out.append(_issue(
            "empty_in_slot", "error", "Пустая катушка числится в слоте",
            f"{spool.get('material') or ''} {spool.get('color_name') or ''} — "
            "0 г, но слот за ней закреплён. Отвяжите в докторе или дождитесь синка",
            spool_id=str(spool.get("id") or ""), slot=str(spool.get("ams_slot") or "")))
    return out


def unverified_count(db, printer_id: str = "") -> int:
    """Сколько катушек ждут проверки: масса бобины, цена, бренд.

    Это и есть мера ручной работы: ноль здесь означает, что владельцу нечего
    править руками. Запрос живёт в сервисе, а не в маршруте (см. `test_router`).
    """
    sql = "SELECT COUNT(*) AS n FROM spools WHERE archived=0 AND verified=0"
    params: tuple = ()
    if printer_id:
        sql += " AND printer_id=?"
        params = (str(printer_id),)
    row = db.one(sql, params)
    return int(num((row or {}).get("n")))


def preflight_ams(db, printer_id: str, snap: dict | None,
                  estimate: dict | None = None,
                  ams_mapping: list[int] | None = None) -> dict:
    """Проверка AMS перед стартом печати: что помешает, а что стоит знать.

    Смысл отдельного прохода. Доктор смотрит на весь парк «когда угодно», а
    здесь вопрос узкий: файл собирается печатать из этих слотов — можно ли
    стартовать и не окажется ли, что слот занят не тем, чем думает склад.
    Уровни как у всего предполёта: блок — стартовать нельзя, предупреждение —
    можно с подтверждением, заметка — просто факт.

    Тумблеры те же, что у остальных проверок: тип слота и пустой слот —
    `preflight_block_material`, кончившаяся катушка под печатью —
    `preflight_block_filament`. Ничего не меняет: починка — «Привести в порядок».
    """
    blocks: list[dict] = []
    warns: list[dict] = []
    infos: list[dict] = []
    if not snap:
        return {"blocks": blocks, "warns": warns, "infos": infos}

    trays = ((snap.get("ams") or {}).get("trays")) or []
    by_slot = {str(tray.get("slot")): tray for tray in trays
               if tray.get("slot") not in (None, "")}
    active = next((tray for tray in trays if tray.get("active")), None)

    if ams_mapping:
        used = [str(slot) for slot in ams_mapping]
    elif active is not None:
        used = [str(active.get("slot"))]
    else:
        used = []

    for slot in used:
        tray = by_slot.get(slot)
        label = _slot_label(tray) if tray else f"Слот {num(slot, 0) + 1}"
        if tray is None:
            blocks.append(_issue(
                "ams_slot_missing", "error", "Слот для печати не найден",
                f"{label}: файл печатает из этого слота, а принтер его не видит"))
            continue
        spool = spool_by_slot(db, printer_id, slot)
        if not tray_occupied(tray):
            blocks.append(_issue(
                "ams_slot_occupied_empty", "error", "Слот для печати пуст",
                f"{label}: файл печатает из этого слота, а катушки в нём нет"))
            continue
        if not spool:
            warns.append(_issue(
                "ams_no_spool", "warn", "Катушка слота не привязана",
                f"{label}: {tray.get('type') or 'тип не задан'} — печать пойдёт, "
                "но расход не спишется со склада. Привяжите катушку в докторе"))
            continue
        diff = slot_diff(tray, spool)
        type_diff = next((row for row in diff if row.get("field") == "type"), None)
        if type_diff and db.setting("preflight_block_material", True):
            blocks.append(_issue(
                "ams_type_mismatch", "error", "В слоте не тот пластик",
                f"{label}: в принтере {type_diff.get('actual') or '—'}, по складу "
                f"{type_diff.get('want') or '—'} — «Привести в порядок» приведёт "
                "слот к складу, либо поправьте катушку"))
        elif diff:
            infos.append(_issue(
                "ams_slot_diff", "info", "Слот расходится со складом",
                f"{label}: " + ", ".join(
                    f"{row.get('label')}: {row.get('actual') or '—'} → "
                    f"{row.get('want')}" for row in diff)))
        remain = num(tray.get("remain"), -1)
        if remain >= 0 and remain <= EMPTY_PCT and db.setting("preflight_block_filament", True):
            blocks.append(_issue(
                "ams_empty", "error", "Катушка в слоте кончилась",
                f"{label}: датчик 0 % — поставьте другую катушку"))
        if not int(num(spool.get("verified"), 1)):
            warns.append(_issue(
                "ams_unverified", "warn", "Катушка слота не проверена",
                f"{label}: масса бобины, цена и бренд взяты из таблицы — "
                "уточните один раз, и себестоимость станет верной"))

    # Есть материал в AMS, но файл печатает не из него: чаще всего просто забыли
    # выбрать слот при отправке — это заметка, а не блок.
    need = str((estimate or {}).get("material") or "").upper()
    if need and not used:
        matches = [tray for tray in trays if tray_occupied(tray)
                   and str(tray.get("type") or "").upper() == need]
        if matches:
            infos.append(_issue(
                "ams_pick_slot", "info", "Материал есть в AMS",
                "Нужен " + need + ": " + ", ".join(
                    f"{_slot_label(tray)} ({round(num(tray.get('remain')))} %)"
                    for tray in matches) + " — назначьте слот при отправке"))

    # Ошибки доктора, не связанные с выбранным слотом: конфликт «в слоте не то»,
    # пустые датчики по другим слотам. Пусть человек увидит их до старта, а не
    # после трех часов печати — но старт не блокируем: это чужие слоты.
    known = {item.get("code") for item in blocks + warns}
    other = [item for item in issues_for_snapshot(db, printer_id, snap)
             if item.get("severity") == "error"
             and str(item.get("slot") or "") not in used]
    fresh = [item for item in other if item.get("code") not in known]
    if fresh:
        infos.append(_issue(
            "ams_doctor", "info", "AMS-доктор нашёл проблемы",
            "; ".join(str(item.get("title")) for item in fresh[:3])
            + (" и другие" if len(fresh) > 3 else "")
            + " — откройте «Привести в порядок»"))
    return {"blocks": blocks, "warns": warns, "infos": infos}


def report(db, printer_id: str = "", snapshots: dict[str, dict] | None = None,
           actions_limit: int = 12) -> dict:
    """Сводка AMS-доктора: проблемы по принтерам, лента действий и настройки.

    ``snapshots`` — снимки парка (``{printer_id: snap}``); их отдаёт менеджер.
    Без снимков доктор честно показывает только то, что знает база.
    """
    snapshots = snapshots or {}
    printers = db.query("SELECT * FROM printers ORDER BY name")
    if printer_id:
        printers = [row for row in printers if str(row.get("id")) == str(printer_id)]
    items: list[dict] = []
    errors = warns = 0
    for row in printers:
        pid = str(row.get("id") or "")
        snap = snapshots.get(pid)
        issues = issues_for_snapshot(db, pid, snap) + issues_from_base(db, pid)
        issues.sort(key=lambda item: SEVERITY_ORDER.get(str(item.get("severity")), 3))
        errors += sum(1 for item in issues if item.get("severity") == "error")
        warns += sum(1 for item in issues if item.get("severity") == "warn")
        items.append({
            "printer_id": pid,
            "name": row.get("name") or pid,
            "online": bool(snap),
            "state": str(((snap or {}).get("printer") or {}).get("state") or ""),
            "state_label": str(((snap or {}).get("printer") or {}).get("state_label") or ""),
            "issues": issues,
            "issues_count": len(issues),
            "actions_24h": action_count(db, pid, 24),
        })
    feed = actions(db, printer_id, limit=actions_limit)
    return {
        "at": now_iso(),
        "printers": items,
        "issues_total": sum(item["issues_count"] for item in items),
        "errors": errors,
        "warns": warns,
        "actions": feed,
        "actions_24h": action_count(db, printer_id, 24),
        "material_defaults": material_defaults(db),
        "rules": [rule for rule in rules(db)],
        "settings": {
            "autopilot": bool(db.setting("ams_autopilot", True)),
            "push_settings": bool(db.setting("ams_push_settings", True)),
            "adopt_generic": bool(db.setting("ams_adopt_generic", True)),
            "learn": bool(db.setting("ams_learn", True)),
            "retry_min": num(db.setting("ams_push_retry_min", 30.0), 30.0),
            "auto_spools": bool(db.setting("ams_auto_spools", True)),
            "sync_remaining": bool(db.setting("ams_sync_remaining", True)),
        },
    }


def plan(db, printer_id: str = "", snapshots: dict[str, dict] | None = None,
         limit: int = 6) -> dict:
    """План раскладки: что поставить в слоты под очередь заданий.

    Логика простая и объяснимая: катушка, которая уже стоит там, где нужна,
    остаётся (ноль перестановок); недостающую ставим в свободный слот; катушку,
    которая мешает и не нужна очереди, предлагаем снять. Ничего не меняем —
    это совет для человека.
    """
    snapshots = snapshots or {}
    printers = db.query("SELECT * FROM printers ORDER BY name")
    if printer_id:
        printers = [row for row in printers if str(row.get("id")) == str(printer_id)]
    jobs = db.query(
        "SELECT * FROM print_jobs WHERE state IN ('queued','uploading','starting')"
        " ORDER BY priority DESC, datetime(created_at) LIMIT ?",
        (max(1, int(limit or 6)),))
    warehouse = db.query(
        "SELECT * FROM spools WHERE archived=0 AND remaining_grams>0"
        " ORDER BY remaining_grams DESC")
    spools = {str(row.get("id")): row for row in warehouse}
    out: list[dict] = []
    for row in printers:
        pid = str(row.get("id") or "")
        snap = snapshots.get(pid)
        slots = _slot_map(db, pid, snap)
        needed: list[dict] = []
        unknown: list[dict] = []
        for job in jobs:
            if str(job.get("printer_id") or pid) not in ("", pid):
                continue
            spool = _job_spool(db, job, spools)
            if not spool:
                unknown.append({
                    "job": str(job.get("name") or job.get("file") or job.get("id")),
                    "grams": round(num(job.get("grams"))),
                    "reason": ("катушка не выбрана — назначьте материал"
                               if not str(job.get("spool_id") or "") else
                               "катушка задания пустая или снята со склада"),
                })
                continue
            needed.append({
                "job": str(job.get("name") or job.get("file") or job.get("id")),
                "spool_id": str(spool.get("id")),
                "material": str(spool.get("material") or ""),
                "color_name": str(spool.get("color_name") or ""),
                "grams": round(num(job.get("grams"))),
                "remain": round(num(spool.get("remaining_grams"))),
            })
        recommendations: list[dict] = []
        moves: list[dict] = []
        for need in needed:
            spool_id = need["spool_id"]
            current = next((slot for slot, item in slots.items()
                            if str(item.get("spool_id") or "") == spool_id), "")
            if current:
                recommendations.append({
                    "slot": current, "slot_num": num(current, 0) + 1,
                    "spool_id": spool_id, "action": "keep",
                    "why": f"уже стоит здесь — нужна для «{need['job']}»",
                    "material": need["material"], "color_name": need["color_name"],
                })
                continue
            free = next((slot for slot, item in sorted(slots.items(),
                                                       key=lambda pair: num(pair[0], 0))
                         if not str(item.get("spool_id") or "")), "")
            if not free:
                continue
            recommendations.append({
                "slot": free, "slot_num": num(free, 0) + 1,
                "spool_id": spool_id, "action": "put",
                "why": f"нужна для «{need['job']}» — поставить в свободный слот",
                "material": need["material"], "color_name": need["color_name"],
            })
            slots.pop(free, None)
        if recommendations:
            moves = [item for item in recommendations if item["action"] == "put"]
        spare = [
            {"slot": slot, "slot_num": num(slot, 0) + 1,
             "material": str(item.get("material") or ""),
             "color_name": str(item.get("color_name") or ""),
             "remain": round(num(item.get("remain"))),
             "why": "в слоте, но очереди не нужна — можно снять и освободить слот"}
            for slot, item in sorted(slots.items(), key=lambda pair: num(pair[0], 0))
            if str(item.get("spool_id") or "")
            and str(item.get("spool_id")) not in {need["spool_id"] for need in needed}
        ]
        if recommendations or spare or unknown:
            out.append({
                "printer_id": pid,
                "name": str((db.one("SELECT name FROM printers WHERE id=?", (pid,)) or {})
                            .get("name") or pid),
                "online": bool(snap),
                "plan": recommendations,
                "moves": moves,
                "spare": spare[:6],
                "unknown": unknown,
                "steps": len(moves),
            })
    return {"at": now_iso(), "printers": out,
            "queue": [{"job": str(job.get("name") or job.get("file") or job.get("id")),
                       "grams": round(num(job.get("grams")))} for job in jobs]}


def _slot_map(db, printer_id: str, snap: dict | None) -> dict[str, dict]:
    """Что сейчас в каждом слоте: из живой телеметрии, иначе из памяти базы."""
    slots: dict[str, dict] = {}
    trays = ((snap or {}).get("ams") or {}).get("trays") or []
    for tray in trays:
        if tray.get("slot") in (None, ""):
            continue
        slot = str(tray.get("slot"))
        spool = spool_by_slot(db, printer_id, slot)
        slots[slot] = {
            "spool_id": str((spool or {}).get("id") or ""),
            "material": str(tray.get("type") or (spool or {}).get("material") or ""),
            "color_name": str((spool or {}).get("color_name") or ""),
            "remain": num(tray.get("remain")),
        }
    if slots:
        # Слоты, о которых телеметрия молчит (принтер отдаёт только занятые),
        # тоже нужны плану: иначе свободных мест для катушек как будто нет.
        units = int(num(((snap or {}).get("ams") or {}).get("units"), 1)) or 1
        width = max(4 * units,
                    max((num(slot, -1) for slot in slots), default=-1) + 1)
        for index in range(width):
            slots.setdefault(str(index), {"spool_id": "", "material": "",
                                          "color_name": "", "remain": 0})
        return slots
    from .ams_sync import slot_memory

    for item in slot_memory(db, printer_id):
        if item.get("state") == "empty":
            continue
        slots[str(item.get("slot"))] = {
            "spool_id": str(item.get("spool_id") or ""),
            "material": str(item.get("material") or ""),
            "color_name": str(item.get("color_name") or ""),
            "remain": num(item.get("remain_pct")),
        }
    return slots


def _job_spool(db, job: dict, spools: dict[str, dict]) -> dict | None:
    """Катушка задания: прямая ссылка, иначе — катушка из маппинга слотов."""
    spool_id = str(job.get("spool_id") or "")
    if spool_id and spool_id in spools:
        return spools[spool_id]
    mapping = str(job.get("ams_mapping") or "").strip()
    if not mapping:
        return None
    import json

    values: list = []
    try:
        parsed = json.loads(mapping)
        values = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        values = [part.strip() for part in mapping.replace(";", ",").split(",") if part.strip()]
    for value in values:
        slot = str(value).strip().strip('"')
        if not slot or slot == "-1":
            continue
        bound = db.one("SELECT spool_id FROM spools WHERE printer_id=? AND ams_slot=?"
                       " AND archived=0", (str(job.get("printer_id") or ""), slot))
        if bound and str(bound.get("spool_id")) in spools:
            return spools[str(bound["spool_id"])]
    return None

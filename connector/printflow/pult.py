"""Сводка для пульта цеха (18.0.4).

Зачем отдельный маршрут. Пульт — телефон в руке у станка, и на каждом
обновлении ему нужны четыре разных куска: парк с камерой и тревогами, очередь,
память слотов AMS и свободные катушки склада для привязки. Раньше это были
`/api/state`, `/api/ams/memory`, `/api/spools` и ещё несколько — по запросу на
экран, на слабой сети и на телефоне это заметно. Здесь всё то же самое одним
ответом.

Важно: форма снимка парка (`printers`, `queue`, `farm`) совпадает с
`/api/state`. Пульт умеет читать оба ответа одной и той же отрисовкой, а если
коннектор старее 18.0.4 — сам откатывается на `/api/state` (см. `tick()` в
`site/control.html`). Поэтому поля в плитках повторяют вложенность снимка, а не
переписаны «плоско»: страница не должна знать две разные структуры.

Данные только читаются. Ничего не пишем, ничего не двигаем: у пульта для
изменений есть отдельные маршруты (`/api/printer/command`, `/api/jobs/*`,
`/api/spool/bind`).
"""
from __future__ import annotations

from .accounting import num
from .ams_sync import MEMORY_STALE_MIN, backfill_slots, slot_memory
from .config import now_iso

# Сколько свободных катушек отдавать для шторки привязки. Оператору нужно
# выбрать из того, что реально лежит на складе; сотни строк в ответе ни к чему.
SPOOL_LIMIT = 60


def _alerts(snap: dict) -> list[dict]:
    """Тревоги сторожа в коротком виде: severity и текст, без служебных полей."""
    out = []
    for item in (snap.get("guard") or {}).get("alerts") or []:
        out.append({
            "severity": str(item.get("severity") or ""),
            "title": str(item.get("title") or item.get("reason") or "тревога"),
            "reason": str(item.get("reason") or ""),
        })
    return out


def _tray(tray: dict) -> dict:
    """Слот AMS в том же виде, что в снимке парка (пульт рисует их одним кодом)."""
    return {
        "id": tray.get("id"),
        "slot": tray.get("slot"),
        "label": tray.get("label"),
        "type": tray.get("type") or "",
        "color": tray.get("color") or "",
        "remain": tray.get("remain"),
        "uuid": tray.get("uuid") or "",
        "active": bool(tray.get("active")),
        "present": bool(tray.get("present")),
    }


def _camera(snap: dict) -> dict:
    cam = snap.get("camera") or {}
    return {
        "available": bool(cam.get("available")),
        "demo": bool(cam.get("demo")),
        "error": str(cam.get("error") or ""),
        "age": cam.get("age"),
        "shots": int(num(cam.get("shots"))),
    }


def _printer(snap: dict) -> dict:
    """Плитка парка: только то, что видно на телефоне под большим пальцем."""
    info = snap.get("printer") or {}
    job = snap.get("job") or {}
    order = job.get("order") or {}
    conn = snap.get("connection") or {}
    trays = [_tray(t) for t in ((snap.get("ams") or {}).get("trays") or [])]
    return {
        "id": snap.get("id"),
        "name": snap.get("name"),
        "model": snap.get("model"),
        "connection": {
            "connected": bool(conn.get("connected")),
            "configured": bool(conn.get("configured")),
            "mode": conn.get("mode") or "",
            "last_error": str(conn.get("last_error") or ""),
        },
        "printer": {
            "state": info.get("state") or "",
            "state_label": info.get("state_label") or "",
            "task": info.get("task") or "",
            "progress": num(info.get("progress")),
            "remaining_min": num(info.get("remaining_min")),
            "eta": info.get("eta"),
            "layer": info.get("layer"),
            "total_layers": info.get("total_layers"),
            "speed_level": info.get("speed_level"),
            "elapsed_min": info.get("elapsed_min"),
        },
        "temperature": {
            "nozzle": num((snap.get("temperature") or {}).get("nozzle")),
            "bed": num((snap.get("temperature") or {}).get("bed")),
        },
        "light": str(snap.get("light") or ""),
        "ams": {
            "trays": trays,
            "filled": sum(1 for t in trays if t["present"] or t["type"] or t["uuid"]),
            "active_tray": (snap.get("ams") or {}).get("active_tray"),
        },
        "camera": _camera(snap),
        "guard": {"alerts": _alerts(snap)},
        "job": {
            "name": job.get("name") or "",
            "order": {"number": order.get("number"), "product": order.get("product")}
            if order else None,
        },
        "maintenance_due": (snap.get("maintenance") or {}).get("due", 0),
    }


def _queue(job: dict) -> dict:
    order = job.get("order") or {}
    return {
        "id": job.get("id"),
        "name": job.get("name") or "",
        "file": job.get("file") or "",
        "state": job.get("state") or "",
        "plate": job.get("plate"),
        "printer_id": job.get("printer_id") or "",
        "est_minutes": num(job.get("est_minutes")),
        "est_grams": num(job.get("est_grams")),
        "priority": num(job.get("priority")),
        "created_at": job.get("created_at") or "",
        "order": {"number": order.get("number"), "product": order.get("product")}
        if order else None,
    }


def spools_for_binding(db, limit: int = SPOOL_LIMIT) -> list[dict]:
    """Свободные катушки для шторки привязки: с остатком и не в архиве.

    Порядок: сначала те, что уже стоят на принтерах (оператор мог переставить
    катушку руками), затем склад; внутри — по остатку. Список короткий: шторка
    на телефоне не должна листаться минуту.
    """
    rows = db.query(
        "SELECT id, material, color_name, color_hex, remaining_grams, printer_id,"
        " ams_slot, location, total_grams FROM spools"
        " WHERE archived=0 AND remaining_grams>0"
        " ORDER BY CASE WHEN COALESCE(printer_id,'')='' THEN 1 ELSE 0 END,"
        " remaining_grams DESC LIMIT ?", (int(limit),))
    return [{
        "id": row["id"],
        "material": row.get("material") or "",
        "color_name": row.get("color_name") or "",
        "color_hex": row.get("color_hex") or "",
        "remaining_grams": num(row.get("remaining_grams")),
        "total_grams": num(row.get("total_grams")),
        "printer_id": row.get("printer_id") or "",
        "ams_slot": row.get("ams_slot") or "",
        "location": row.get("location") or "",
    } for row in rows]


def _last_done(db, printer_id: str = "") -> dict:
    """Последняя законченная печать: план против факта (18.0.10).

    Пульт у станка показывает эту карточку после финиша, чтобы оператор сразу
    видел, что получилось, и снимал деталь не наугад. Только чтение.
    """
    if printer_id:
        job = db.one("SELECT * FROM print_jobs WHERE state='done' AND printer_id=?"
                     " ORDER BY datetime(COALESCE(finished_at, created_at)) DESC LIMIT 1",
                     (printer_id,))
    else:
        job = db.one("SELECT * FROM print_jobs WHERE state='done'"
                     " ORDER BY datetime(COALESCE(finished_at, created_at)) DESC LIMIT 1")
    if not job:
        return {}
    order = {}
    if job.get("order_id"):
        row = db.one("SELECT number, product FROM orders WHERE id=?", (job["order_id"],))
        if row:
            order = {"number": row.get("number"), "product": row.get("product")}
    # Сколько принтер стоит с момента финиша. Считаем на сервере: часы телефона
    # у станка могут врать, а «простой 0 минут» после ночной печати — обидно.
    idle_min = 0.0
    if job.get("finished_at"):
        try:
            from datetime import datetime
            done_at = datetime.fromisoformat(str(job["finished_at"]))
            idle_min = round(max(0.0, (datetime.now().astimezone() - done_at)
                                .total_seconds() / 60.0), 1)
        except Exception:  # noqa: BLE001 — кривая дата не должна ломать сводку
            idle_min = 0.0
    return {
        "id": job.get("id"),
        "name": job.get("name") or "",
        "printer_id": job.get("printer_id") or "",
        "plate": job.get("plate"),
        "state": job.get("state") or "",
        "finished_at": job.get("finished_at") or "",
        "plan_minutes": num(job.get("est_minutes")),
        "plan_grams": num(job.get("est_grams")),
        "minutes": num(job.get("duration_min")),
        "grams": num(job.get("grams")),
        "progress": num(job.get("progress")),
        "result": str(job.get("result") or ""),
        "idle_min": idle_min,
        "order": order or None,
    }


def summary(db, manager, printer_id: str = "", spool_limit: int = SPOOL_LIMIT) -> dict:
    """Один ответ для пульта: парк, очередь, память AMS, свободные катушки.

    ``printer_id`` выбирает активный принтер (влияет только на память слотов,
    которая в базе разложена по принтерам). Парк и очередь отдаются целиком —
    пульт показывает все принтеры, а не только выбранный.
    """
    snap = manager.snapshot(printer_id)
    raw_printers = snap.get("printers", []) or []
    printers = [_printer(p) for p in raw_printers]
    done = _last_done(db, printer_id)
    # Отчёт автономности считаем по уже готовым снимкам: парк опрошен один раз,
    # а пульт получает ответ на вопрос «почему стоит» тем же запросом (18.0.8).
    autonomy = manager.autonomy_report(
        printer_id, snaps={str(p.get("id")): p for p in raw_printers})
    # Память слотов: привязки, которые уже есть в базе, дописываем сразу —
    # принтер может быть выключен, а раскладка всё равно известна (17.0.25).
    backfill_slots(db, printer_id)
    slots = slot_memory(db, printer_id)
    alerts = [a for p in printers for a in p["guard"]["alerts"]]
    studio = getattr(manager, "studio", None) if manager else None
    pending_studio = studio.pending_list() if studio else []
    return {
        "at": now_iso(),
        "active_id": printer_id or (printers[0]["id"] if printers else ""),
        "autonomy": autonomy,
        "last_done": done,
        "printers": printers,
        "queue": [_queue(j) for j in snap.get("queue", [])],
        "farm": snap.get("farm") or {},
        "quiet": snap.get("quiet"),
        "pending_studio": pending_studio,
        "ams": {
            "printer_id": printer_id,
            "stale_min": MEMORY_STALE_MIN,
            "seen_at": now_iso(),
            "slots": slots,
        },
        "spools": spools_for_binding(db, spool_limit),
        "alerts": {
            "count": len(alerts),
            "worst": "error" if any(a["severity"] == "error" for a in alerts) else (
                "warn" if alerts else ""),
        },
    }

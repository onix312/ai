"""Режим «NOZZA tour» PrintFlow 8.5 (идея 27).

Живое демо системы без реального железа:
    start — страховочная копия базы, сид данных (заказы, задания, стеллаж,
            катушки), включение виртуального принтера (идея 7);
    stop  — запрос отката к копии (стандартный механизм restore + перезапуск).

Сид — честный: номера заказов продолжают сквозную нумерацию, даты — реальные.
Демо-данные помечаются в заметках, чтобы их было легко отличить.
"""
from __future__ import annotations

import logging
import math
import struct
import time
from datetime import datetime, timedelta
from typing import Any

from .accounting import uid
from .config import BACKUP_DIR, now_iso
from .db import Database

log = logging.getLogger("printflow")


def _next_order_number(db: Database) -> str:
    """Номер демо-заказа — из общей нумерации, а не от количества строк.

    Докстринг модуля обещает «номера продолжают сквозную нумерацию», и раньше
    это было неправдой: ``COUNT(*) + 1`` в живой базе выдавал номера вразнобой
    с настоящими заказами и мог наступить на занятый номер (а по номеру заказ
    ищут бот, трекинг и платежи).
    """
    from .repo import Repo
    return Repo(db).next_order_number()

TOUR_MARK = "NOZZA tour (демо-данные)"
TOUR_MODEL = "tour-name-tag.stl"
TOUR_GCODE = "tour-name-tag.gcode"
# Старое имя файла задания (до 18.12): локальной копии не было, и шкала слоёв
# в туре показывала только причину. Оставлено как запасной вариант.
TOUR_JOB_FALLBACK = "tour-name-tag.3mf"


def _rounded_rect(width: float, height: float, radius: float, steps: int = 6) -> list:
    """Скруглённый прямоугольник против часовой стрелки, центр в нуле."""
    pts: list = []
    cx, cy = width / 2.0 - radius, height / 2.0 - radius
    for x, y, start in ((cx, cy, 0), (-cx, cy, 90), (-cx, -cy, 180), (cx, -cy, 270)):
        for i in range(steps + 1):
            a = math.radians(start + 90.0 * i / steps)
            pts.append((x + radius * math.cos(a), y + radius * math.sin(a)))
    return pts


def tag_mesh_stl(width: float = 52.0, depth: float = 24.0, height: float = 3.2,
                 taper: float = 0.86) -> bytes:
    """Бинарный STL демо-адресника: скруглённая пластина с фаской по периметру.

    Выпуклая призма с сужением кверху — замкнутая сетка без самопересечений,
    каждый слой отличается от соседнего, поэтому на шкале слоёв видны и
    стенки, и заполнение, и сплошные слои дна и крыши.
    """
    bottom = _rounded_rect(width, depth, 6.0)
    top = [(x * taper, y * (taper - 0.06)) for x, y in bottom]
    n = len(bottom)
    tris: list = []
    for i in range(n):
        j = (i + 1) % n
        b0, b1 = (*bottom[i], 0.0), (*bottom[j], 0.0)
        t0, t1 = (*top[i], height), (*top[j], height)
        tris.append((b0, b1, t1))
        tris.append((b0, t1, t0))
    for i in range(1, n - 1):
        tris.append(((*bottom[0], 0.0), (*bottom[i + 1], 0.0), (*bottom[i], 0.0)))
        tris.append(((*top[0], height), (*top[i], height), (*top[i + 1], height)))
    out = bytearray(b"PrintFlow NOZZA tour: name tag".ljust(80, b"\0"))
    out += struct.pack("<I", len(tris))
    for a, b, c in tris:
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        out += struct.pack("<3f", nx / length, ny / length, nz / length)
        for px, py, pz in (a, b, c):
            out += struct.pack("<3f", px, py, pz)
        out += b"\0\0"
    return bytes(out)


def build_tour_gcode(db: Database) -> dict[str, Any]:
    """Нарезать демо-адресник своим движком и положить STL + G-code в библиотеку.

    Возвращает ``{"file": имя для задания, "grams", "minutes", "layers"}``.
    Любая ошибка — не повод срывать тур: тогда задание получает старое имя без
    локальной копии, а шкала слоёв честно объяснит, почему пустая.
    """
    from .config import UPLOAD_DIR
    from .library import FileLibrary
    from .slicer_engine import slice_model
    from .slicer_profile import P1S, settings_from

    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        library = FileLibrary(db)
        stl_row = library.put(TOUR_MODEL, tag_mesh_stl(), source="tour", note=TOUR_MARK)
        stl_path = library.resolve(stl_row["id"])
        settings = settings_from({"layer_height": 0.2, "infill_percent": 15.0,
                                  "material": "PLA"})
        text, report = slice_model(stl_path, settings, P1S)
        estimate = {"grams": float(report.get("weight_g") or 0.0),
                    "minutes": float(report.get("minutes") or 0.0),
                    "material": "PLA"}
        row = library.put(TOUR_GCODE, text.encode("utf-8"), source="tour",
                          note=TOUR_MARK, estimate=estimate)
        return {"file": row.get("upload_name") or TOUR_GCODE,
                "grams": estimate["grams"], "minutes": estimate["minutes"],
                "layers": int(report.get("layers") or 0)}
    except Exception as exc:  # noqa: BLE001 — тур важнее демо-файла
        log.warning("NOZZA tour: демо-G-code не собран: %s", exc)
        return {"file": TOUR_JOB_FALLBACK, "grams": 0.0, "minutes": 0.0, "layers": 0}


def start(db: Database) -> dict[str, Any]:
    """Снять копию базы и засеять демо-данные. Возврат — через stop()."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_name = f"tour-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3"
    db.backup_to(BACKUP_DIR / backup_name)
    _seed(db)
    # Настройки демо-режима
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
               ("demo_printer_enabled", "1"))
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
               ("keyframe_interval_min", "2"))
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
               ("tour_backup_file", backup_name))
    db.add_event("system", "NOZZA tour: демо включено",
                 f"Страховая копия: {backup_name}. Откат — кнопка «Завершить тур».",
                 "", {"backup": backup_name})
    return {"ok": True, "backup": backup_name}


def stop_backup_file(db: Database) -> str:
    """Имя копии, с которой начинался тур (для request_restore)."""
    file = str(db.setting("tour_backup_file", "") or "")
    if not file:
        raise ValueError("Демо не включено")
    if not (BACKUP_DIR / file).is_file():
        raise ValueError(f"Копия {file} не найдена — откат невозможен")
    return file


def _day(offset: int, hour: int = 12) -> str:
    dt = (datetime.now() - timedelta(days=offset)).replace(
        hour=hour, minute=17, second=0, microsecond=0)
    return dt.isoformat(timespec="seconds")


def _seed(db: Database) -> None:
    # --- катушки (виртуальный AMS)
    spools = [
        ("PLA", "", "Белый", "#e5e7eb", 750.0, "0"),
        ("PLA", "", "Чёрный", "#1f2937", 600.0, "1"),
        ("PLA", "", "Красный", "#dc2626", 400.0, "2"),
        ("PLA", "", "Голубой", "#38bdf8", 300.0, "3"),
    ]
    for material, brand, color_name, color_hex, grams, slot in spools:
        db.upsert("spools", {
            "id": uid("spl"), "material": material, "brand": brand,
            "color_name": color_name, "color_hex": color_hex,
            "total_grams": 1000.0, "remaining_grams": grams,
            "price": 1500.0, "printer_id": "virtual", "ams_slot": slot,
            "tray_uuid": f"tour-{color_name.lower()}", "verified": 1,
            "created_at": now_iso(), "updated_at": now_iso(),
        })

    # --- клиенты
    anna = db.upsert("customers", {"id": uid("cus"), "name": "Анна (демо)",
                                   "phone": "+7 900 000-00-01",
                                   "notes": TOUR_MARK, "created_at": _day(40)})
    ivan = db.upsert("customers", {"id": uid("cus"), "name": "Иван (демо)",
                                   "phone": "+7 900 000-00-02",
                                   "notes": TOUR_MARK, "created_at": _day(25)})
    cafe = db.upsert("customers", {"id": uid("cus"), "name": "Кофейня «Зерно» (демо)",
                                   "phone": "+7 900 000-00-03",
                                   "company": "ИП Зерно",
                                   "notes": TOUR_MARK, "created_at": _day(18)})

    # --- прошлые задания и заказы (для аналитики: карты, отчёты, достижения)
    past = [
        ("Адресник «Барсик»", ivan["id"], "Иван (демо)", 28.0, 95.0, 350.0, 9, "done", "ready"),
        ("Адресник «Шуша»", anna["id"], "Анна (демо)", 31.0, 110.0, 420.0, 8, "done", "ready"),
        ("Табличка для стола", cafe["id"], "Кофейня «Зерно» (демо)", 120.0, 340.0, 1200.0, 6, "done", "ready"),
        ("Держатель поводка", ivan["id"], "Иван (демо)", 45.0, 150.0, 650.0, 4, "done", "ready"),
        ("Набор бирок ×5", anna["id"], "Анна (демо)", 22.0, 70.0, 500.0, 3, "done", "ready"),
        ("QR-стойка", cafe["id"], "Кофейня «Зерно» (демо)", 210.0, 420.0, 2400.0, 1, "done", "ready"),
    ]
    for product, cus_id, cus_name, grams, minutes, price, days_ago, job_state, status in past:
        job_id = uid("job")
        cost = round(grams * 1.4 + minutes / 60 * 30, 0)
        db.upsert("print_jobs", {
            "id": job_id, "printer_id": "virtual", "name": product, "file": product,
            "state": job_state, "source": "tour",
            "est_minutes": minutes, "est_grams": grams,
            "started_at": _day(days_ago, 9), "finished_at": _day(days_ago, 14),
            "duration_min": minutes, "grams": grams, "cost": cost,
            "energy_kwh": round(minutes / 60 * 0.15, 3),
            "accounted_at": _day(days_ago, 14), "created_at": _day(days_ago, 9),
        })
        order_id = uid("ord")
        db.upsert("orders", {
            "id": order_id, "number": _next_order_number(db),
            "product": product, "customer_id": cus_id, "customer_name": cus_name,
            "channel": "shelf", "status": status, "qty": 1, "material": "PLA",
            "color": "Белый", "grams": grams, "hours": round(minutes / 60, 1),
            "price": price, "cost": cost, "paid": price, "prepaid": price,
            "actual_grams": grams, "actual_hours": round(minutes / 60, 1),
            "actual_cost": cost, "quality": "ok",
            "due": _day(days_ago - 1), "created_at": _day(days_ago + 2),
            "updated_at": _day(days_ago), "closed_at": _day(days_ago),
            "notes": TOUR_MARK,
        })
        db.upsert("transactions", {
            "id": uid("tx"), "kind": "income", "category": "Продажа",
            "amount": price, "at": _day(days_ago), "note": f"{product} ({TOUR_MARK})",
            "account_id": "cash", "taxable": 1,
        })

    # --- стеллаж
    shelf_items = [
        ("Адресник «Пёс»", 6.0, 450.0, 150.0, "Белый"),
        ("Адресник «Кошка»", 4.0, 450.0, 150.0, "Красный"),
        ("Держатель поводка", 5.0, 650.0, 220.0, "Чёрный"),
        ("Табличка «Здесь живёт…»", 3.0, 900.0, 300.0, "Голубой"),
    ]
    for name, qty, price, cost, color in shelf_items:
        item_id = uid("shf")
        db.upsert("shelf_items", {
            "id": item_id, "name": name, "qty": qty, "price": price,
            "cost_per_unit": cost, "min_qty": 2, "photo": "",
            "note": TOUR_MARK, "active": 1,
            "created_at": _day(10), "updated_at": now_iso(),
        })
        db.upsert("shelf_moves", {
            "id": uid("mv"), "at": _day(10), "item_id": item_id,
            "kind": "produce", "qty": qty + 2, "note": TOUR_MARK})
        db.upsert("shelf_moves", {
            "id": uid("mv"), "at": _day(2), "item_id": item_id,
            "kind": "sale", "qty": -2, "price": price, "note": TOUR_MARK})

    # --- активные заказы
    printing = db.upsert("orders", {
        "id": uid("ord"),
        "number": _next_order_number(db),
        "product": "Адресник «Рыжик» (красный)", "customer_id": anna["id"],
        "customer_name": "Анна (демо)", "channel": "online", "status": "queue",
        "qty": 1, "material": "PLA", "color": "Красный", "grams": 30.0,
        "hours": 2.0, "price": 450.0, "cost": 120.0, "paid": 450.0,
        "prepaid": 450.0, "due": _day(-1), "created_at": _day(1),
        "updated_at": now_iso(), "notes": TOUR_MARK,
    })
    # 18.12: файл задания — настоящий G-code, нарезанный своим движком, поэтому
    # шкала слоёв Hero-пульта в туре показывает слои, а не причину пустоты.
    # Смета задания остаётся демонстрационной (2 часа), чтобы печать в туре
    # шла заметное время; вес — из нарезки, если она удалась.
    demo_gcode = build_tour_gcode(db)
    db.upsert("print_jobs", {
        "id": uid("job"), "printer_id": "virtual",
        "name": "Адресник «Рыжик» (красный)", "file": demo_gcode["file"],
        "state": "queued", "source": "tour", "order_id": printing["id"],
        "est_minutes": 120.0, "est_grams": 30.0, "plate": 1,
        "ams_mapping": "[2]", "created_at": now_iso(),
        "notes": TOUR_MARK,
    })
    db.upsert("orders", {
        "id": uid("ord"),
        "number": _next_order_number(db),
        "product": "Держатель поводка ×2", "customer_id": ivan["id"],
        "customer_name": "Иван (демо)", "channel": "shelf", "status": "ready",
        "qty": 2, "material": "PLA", "color": "Чёрный", "grams": 90.0,
        "hours": 4.0, "price": 1300.0, "cost": 400.0, "paid": 1300.0,
        "prepaid": 1300.0, "created_at": _day(3), "updated_at": _day(1),
        "notes": TOUR_MARK,
    })
    db.upsert("orders", {
        "id": uid("ord"),
        "number": _next_order_number(db),
        "product": "QR-стойка «Зерно»", "customer_id": cafe["id"],
        "customer_name": "Кофейня «Зерно» (демо)", "channel": "b2b",
        "status": "new", "qty": 2, "material": "PLA", "color": "Чёрный",
        "grams": 420.0, "hours": 8.0, "price": 4800.0, "cost": 1500.0,
        "paid": 2400.0, "prepaid": 2400.0, "due": _day(-4),
        "created_at": now_iso(), "updated_at": now_iso(), "notes": TOUR_MARK,
    })


def reset_settings(db: Database) -> None:
    """Выключить демо-настройки (вызывается вместе с откатом)."""
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
               ("demo_printer_enabled", "0"))
    db.execute("DELETE FROM settings WHERE key IN ('keyframe_interval_min',"
               " 'tour_backup_file')")

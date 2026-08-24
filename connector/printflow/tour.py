"""Режим «NOZZA tour» PrintFlow 8.5 (идея 27).

Живое демо системы без реального железа:
    start — страховочная копия базы, сид данных (заказы, задания, стеллаж,
            катушки), включение виртуального принтера (идея 7);
    stop  — запрос отката к копии (стандартный механизм restore + перезапуск).

Сид — честный: номера заказов продолжают сквозную нумерацию, даты — реальные.
Демо-данные помечаются в заметках, чтобы их было легко отличить.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from .accounting import uid
from .config import BACKUP_DIR, now_iso
from .db import Database

TOUR_MARK = "NOZZA tour (демо-данные)"


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
            "id": order_id, "number": str(db.one("SELECT COUNT(*) n FROM orders")["n"] + 1),
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
        "number": str(db.one("SELECT COUNT(*) n FROM orders")["n"] + 1),
        "product": "Адресник «Рыжик» (красный)", "customer_id": anna["id"],
        "customer_name": "Анна (демо)", "channel": "online", "status": "queue",
        "qty": 1, "material": "PLA", "color": "Красный", "grams": 30.0,
        "hours": 2.0, "price": 450.0, "cost": 120.0, "paid": 450.0,
        "prepaid": 450.0, "due": _day(-1), "created_at": _day(1),
        "updated_at": now_iso(), "notes": TOUR_MARK,
    })
    db.upsert("print_jobs", {
        "id": uid("job"), "printer_id": "virtual",
        "name": "Адресник «Рыжик» (красный)", "file": "tour-name-tag.3mf",
        "state": "queued", "source": "tour", "order_id": printing["id"],
        "est_minutes": 120.0, "est_grams": 30.0, "plate": 1,
        "ams_mapping": "[2]", "created_at": now_iso(),
        "notes": TOUR_MARK,
    })
    db.upsert("orders", {
        "id": uid("ord"),
        "number": str(db.one("SELECT COUNT(*) n FROM orders")["n"] + 1),
        "product": "Держатель поводка ×2", "customer_id": ivan["id"],
        "customer_name": "Иван (демо)", "channel": "shelf", "status": "ready",
        "qty": 2, "material": "PLA", "color": "Чёрный", "grams": 90.0,
        "hours": 4.0, "price": 1300.0, "cost": 400.0, "paid": 1300.0,
        "prepaid": 1300.0, "created_at": _day(3), "updated_at": _day(1),
        "notes": TOUR_MARK,
    })
    db.upsert("orders", {
        "id": uid("ord"),
        "number": str(db.one("SELECT COUNT(*) n FROM orders")["n"] + 1),
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

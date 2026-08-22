"""Достижения цеха PrintFlow 8.5 (идея 90).

Бейджи считаются из фактов: задания, заказы, стеллаж. Прогресс виден всегда,
чтобы «до 100-й печати ещё 12» было мотивацией, а не загадкой.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .accounting import num
from .db import Database


def _one(db: Database, sql: str, args: tuple = ()) -> float:
    row = db.one(sql, args) or {}
    return num(row.get("v"))


def achievements(db: Database) -> list[dict[str, Any]]:
    jobs_done = int(_one(db, "SELECT COUNT(*) v FROM print_jobs WHERE state='done'"))
    grams = _one(db, "SELECT COALESCE(SUM(grams),0) v FROM print_jobs WHERE state='done'")
    minutes = _one(db, "SELECT COALESCE(SUM(duration_min),0) v FROM print_jobs WHERE state='done'")
    income = _one(db, "SELECT COALESCE(SUM(amount),0) v FROM transactions WHERE kind='income'")
    orders = int(_one(db, "SELECT COUNT(*) v FROM orders"))
    shelf_qty = _one(db, "SELECT COALESCE(SUM(qty),0) v FROM shelf_items WHERE active=1")

    since_w = (datetime.now() - timedelta(days=7)).isoformat()
    since_m = (datetime.now() - timedelta(days=30)).isoformat()
    failed_w = int(_one(db, "SELECT COUNT(*) v FROM print_jobs WHERE state='failed' AND finished_at>=?", (since_w,)))
    done_w = int(_one(db, "SELECT COUNT(*) v FROM print_jobs WHERE state='done' AND finished_at>=?", (since_w,)))
    failed_m = int(_one(db, "SELECT COUNT(*) v FROM print_jobs WHERE state='failed' AND finished_at>=?", (since_m,)))
    done_m = int(_one(db, "SELECT COUNT(*) v FROM print_jobs WHERE state='done' AND finished_at>=?", (since_m,)))

    first_order = db.one("SELECT MIN(created_at) v FROM orders WHERE created_at<>''")
    workshop_age_days = 0
    if first_order and first_order.get("v"):
        try:
            stamp = datetime.fromisoformat(str(first_order["v"]))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            workshop_age_days = (datetime.now(timezone.utc) - stamp).days
        except ValueError:
            workshop_age_days = 0

    def badge(bid: str, title: str, desc: str, progress: float, target: float) -> dict:
        return {"id": bid, "title": title, "desc": desc,
                "progress": round(min(progress, target), 1), "target": target,
                "achieved": progress >= target and target > 0}

    out = [
        badge("first-print", "Первая печать", "Цех напечатал своё первое задание",
              min(jobs_done, 1), 1),
        badge("print-100", "100-я печать", "Сто завершённых заданий", jobs_done, 100),
        badge("print-1000", "1000-я печать", "Тысяча завершённых заданий", jobs_done, 1000),
        badge("grams-10", "10 кг пластика", "Напечатано 10 000 г", grams, 10_000),
        badge("grams-100", "100 кг пластика", "Напечатано 100 000 г", grams, 100_000),
        badge("hours-100", "100 часов печати", "Суммарная наработка станка", minutes / 60, 100),
        badge("no-defect-week", "Неделя без брака", "7 дней: есть печати, нет сбоев",
              1 if (done_w >= 1 and failed_w == 0) else 0, 1),
        badge("no-defect-month", "Месяц без брака", "30 дней: минимум 3 печати, ноль сбоев",
              1 if (done_m >= 3 and failed_m == 0) else 0, 1),
        badge("income-100k", "Первые 100 000 ₽", "Суммарный доход за всё время", income, 100_000),
        badge("orders-100", "100 заказов", "Сто заказов за всё время", orders, 100),
        badge("shelf-20", "Полка полна", "20 и более штук на стеллаже", shelf_qty, 20),
        badge("year-of-work", "Год цеха", "Год с первого заказа", workshop_age_days, 365),
    ]
    return out

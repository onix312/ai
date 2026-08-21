"""Мастер-план производства PrintFlow 4.0.

Первый шаг «диспетчера»: система сама говорит, что печатать следующим, а не
просто показывает очередь и остатки. План собирается из двух источников,
которые уже есть в базе:

    1) открытые заказы с дедлайном, которые ещё не напечатаны;
    2) дефицит на полке по скорости продаж (план пополнения).

Всё сводится к одному вердикту: хватает ли часов парка, и что запускать
сейчас. Расчёт ничего не пишет в базу — это тот же принцип, что у
`/api/batch/plan` и `/api/calc/cost`.
"""
from __future__ import annotations

from typing import Any

from .accounting import num
from .config import now_iso
from .db import Database

# Статусы до этапа производства: заказ в них — это ещё лид, а не работа.
PRE_PRODUCTION_STATUSES = ("new", "estimate", "prepay")

# Порядок приоритетов: чем меньше число, тем раньше печатаем.
PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


class Planner:
    """Сборка «плана на сегодня» из заказов и плана пополнения."""

    def __init__(self, db: Database, batches=None):
        self.db = db
        # Партии нужны только для плана пополнения; создаём сами, если не передали.
        if batches is None:
            from .batches import Batches
            batches = Batches(db)
        self.batches = batches

    # ------------------------------------------------------------- источники
    def _open_orders(self) -> list[dict]:
        """Заказы, требующие производства: не финальные, с нормой часов."""
        done = {row["order_id"] for row in self.db.query(
            "SELECT DISTINCT order_id FROM print_jobs"
            " WHERE state='done' AND order_id IS NOT NULL")}
        rows = self.db.query(
            "SELECT o.*, s.name status_name FROM orders o"
            " LEFT JOIN statuses s ON s.id=o.status"
            " WHERE COALESCE(s.is_final,0)=0"
            " ORDER BY datetime(o.created_at) DESC")
        out = []
        for row in rows:
            if row.get("status") in PRE_PRODUCTION_STATUSES:
                continue
            if num(row.get("hours")) <= 0:
                continue
            if row.get("id") in done:
                continue
            out.append(row)
        return out

    def _in_progress_hours(self) -> float:
        """Часы уже занятых слотов парка: печать, старт и очередь."""
        rows = self.db.query(
            "SELECT j.est_minutes, o.hours, o.qty, o.id order_id FROM print_jobs j"
            " LEFT JOIN orders o ON o.id=j.order_id"
            " WHERE j.state IN ('queued','starting','running')")
        total = 0.0
        for row in rows:
            est = num(row.get("est_minutes"))
            if est > 0:
                total += est / 60.0
            else:
                # Мультизаказ: hours — вся плита, qty — сумма единиц; на qty не умножаем.
                k = 1.0 if self._has_items(row.get("order_id")) \
                    else max(1.0, num(row.get("qty"), 1))
                total += num(row.get("hours")) * k
        return round(total, 2)

    def _has_items(self, order_id: str | None) -> bool:
        if not order_id:
            return False
        row = self.db.one(
            "SELECT 1 FROM order_items WHERE order_id=? LIMIT 1", (order_id,))
        return bool(row)

    def _filament_left(self, material: str) -> float:
        if not material or not str(material).strip():
            return 0.0
        row = self.db.one(
            "SELECT COALESCE(SUM(remaining_grams),0) v FROM spools"
            " WHERE archived=0 AND pylower(material)=?",
            (str(material).strip().lower(),)) or {}
        return round(num(row.get("v")), 1)

    # ----------------------------------------------------------------- задачи
    def _order_task(self, order: dict) -> dict:
        # Мультизаказ: hours/grams — вся плита целиком, qty — сумма единиц
        # по позициям. Умножать на qty нельзя, иначе план дня раздуется.
        k = 1.0 if self._has_items(order.get("id")) \
            else max(1.0, num(order.get("qty"), 1))
        qty = max(1.0, num(order.get("qty"), 1))
        hours = round(num(order.get("hours")) * k, 2)
        grams = round(num(order.get("grams")) * k, 1)
        material = (order.get("material") or "").strip()
        file = (order.get("file") or "").strip()
        issues = self._issues(file, material, grams)
        due = order.get("due") or ""
        return {
            "kind": "order",
            "id": order.get("id"),
            "title": order.get("product") or "Заказ",
            "ref": f"№{order.get('number') or ''}".strip(),
            "customer": order.get("customer_name") or "",
            "status": order.get("status_name") or order.get("status") or "",
            "hours": hours,
            "grams": grams,
            "qty": qty,
            "material": material,
            "due": due,
            "priority": order.get("priority") or "normal",
            "file": file,
            "issues": issues,
            "ready": not issues,
        }

    def _replenish_task(self, row: dict) -> dict:
        grams = round(num(row.get("grams")), 1)
        hours = round(num(row.get("hours")), 2)
        material = (row.get("material") or "").strip()
        file = (row.get("file") or "").strip()
        issues = self._issues(file, material, grams)
        days = num(row.get("days_left"))
        return {
            "kind": "replenish",
            "id": row.get("nom_id"),
            "title": row.get("name") or "Позиция",
            "ref": "полка",
            "customer": "",
            "status": row.get("status") or "",
            "hours": hours,
            "grams": grams,
            "qty": num(row.get("plan_qty")),
            "material": material,
            "due": "",
            "days_left": days if days is not None else None,
            "priority": "normal",
            "file": file,
            "issues": issues,
            "ready": not issues,
        }

    def _issues(self, file: str, material: str, grams: float) -> list[dict]:
        issues: list[dict] = []
        if not file:
            issues.append({"kind": "file", "level": "bad",
                           "text": "Нет файла на принтере — задание не стартует"})
        if material and grams > 0:
            left = self._filament_left(material)
            if left < grams:
                issues.append({"kind": "filament", "level": "warn",
                               "text": f"Пластика {material} осталось {round(left)} г,"
                                       f" нужно ~{round(grams)} г"})
        return issues

    # ------------------------------------------------------------------ план
    def day_plan(self) -> dict[str, Any]:
        capacity = max(1.0, num(self.db.setting("weekly_capacity_hours", 110), 110))

        orders = []
        for order in self._open_orders():
            task = self._order_task(order)
            due = order.get("due") or "9999-12-31"
            prio = PRIORITY_ORDER.get(order.get("priority") or "normal", 2)
            orders.append((due, prio, task))
        orders.sort(key=lambda x: (x[0], x[1]))
        order_tasks = [t for _, _, t in orders]

        replenish_tasks = [self._replenish_task(r)
                           for r in self.batches.plan_replenishment()]
        replenish_tasks.sort(key=lambda t: (t["days_left"] is None,
                                            t["days_left"] if t["days_left"] is not None else 0))

        sequence = order_tasks + replenish_tasks
        in_progress = self._in_progress_hours()
        planned = round(sum(t["hours"] for t in sequence), 2)
        total = round(in_progress + planned, 2)
        load_pct = round(total / capacity * 100, 1)

        verdict, text = "ok", ""
        if total > capacity:
            verdict = "bad"
            text = f"Перегруз: план требует {total:.0f} ч, а парк даёт {capacity:.0f} ч в неделю."
        elif load_pct >= 85:
            verdict = "warn"
            text = f"Загрузка {load_pct:.0f}% — близко к потолку {capacity:.0f} ч в неделю."
        else:
            text = f"Загрузка {load_pct:.0f}% от {capacity:.0f} ч в неделю — запас есть."

        suggested = next((t for t in sequence if t["ready"]), None)

        printers = int(num((self.db.one("SELECT COUNT(*) n FROM printers") or {}).get("n")))
        issues: list[str] = []
        if not printers:
            issues.append("Принтер не настроен — добавьте его в разделе «Принтеры».")

        return {
            "generated_at": now_iso(),
            "capacity_weekly": capacity,
            "in_progress_hours": in_progress,
            "planned_hours": planned,
            "total_hours": total,
            "load_pct": load_pct,
            "overload_hours": round(max(0.0, total - capacity), 2),
            "verdict": verdict,
            "verdict_text": text,
            "orders_to_print": len(order_tasks),
            "replenish_count": len(replenish_tasks),
            "suggested_next": suggested,
            "sequence": sequence,
            "issues": issues,
        }

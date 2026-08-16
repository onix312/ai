"""Автоматический производственный и финансовый учёт.

Правила:
    • себестоимость считается по фактам печати, а не по ощущениям;
    • пластик списывается со склада той катушки, которая реально стояла в AMS;
    • каждая завершённая печать и каждый закрытый заказ оставляют проводку;
    • ручные правки пользователя никогда не перетираются автоматикой
      (флаг auto у заказа и проводки).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any

from .config import now_iso
from .db import Database


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class Accounting:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------ себестоимость
    def cost_breakdown(self, grams: float, hours: float, spool_price: float | None = None,
                       spool_weight: float | None = None, manual_minutes: float = 0.0,
                       qty: float = 1.0) -> dict[str, float]:
        """Полная раскладка себестоимости партии."""
        s = self.db.settings()
        grams = max(0.0, num(grams))
        hours = max(0.0, num(hours))
        qty = max(1.0, num(qty, 1))
        price = num(spool_price if spool_price is not None else s["default_spool_price"], 1600)
        weight = max(1.0, num(spool_weight if spool_weight is not None else s["default_spool_weight"], 1000))

        filament = grams * price / weight
        energy_kwh = hours * num(s["power_kw"], 0.15)
        energy = energy_kwh * num(s["energy_price"], 6)
        amortization = hours * num(s["amortization_per_hour"], 12)
        maintenance = hours * num(s["maintenance_per_hour"], 3)
        labor = num(manual_minutes) / 60.0 * num(s["labor_rate"], 400)
        packaging = num(s["packaging_cost"], 15)
        subtotal = filament + energy + amortization + maintenance + labor + packaging
        failure = subtotal * num(s["failure_rate"], 5) / 100.0
        total = subtotal + failure
        return {
            "filament": round(filament, 2),
            "energy": round(energy, 2),
            "energy_kwh": round(energy_kwh, 3),
            "amortization": round(amortization, 2),
            "maintenance": round(maintenance, 2),
            "labor": round(labor, 2),
            "packaging": round(packaging, 2),
            "failure_reserve": round(failure, 2),
            "total": round(total, 2),
            "per_unit": round(total / qty, 2),
            "per_hour": round(total / hours, 2) if hours else 0.0,
        }

    def order_economics(self, order: dict) -> dict[str, float]:
        """Экономика заказа с учётом налога и фактических данных печати."""
        s = self.db.settings()
        price = num(order.get("price"))
        hours = num(order.get("actual_hours")) or num(order.get("hours"))
        grams = num(order.get("actual_grams")) or num(order.get("grams"))
        cost = num(order.get("actual_cost")) or num(order.get("cost"))
        if not cost:
            cost = self.cost_breakdown(grams, hours,
                                       manual_minutes=num(order.get("manual_minutes")),
                                       qty=num(order.get("qty"), 1))["total"]
        tax = price * num(s["tax_rate"], 0) / 100.0
        profit = price - cost - tax
        return {
            "price": round(price, 2),
            "cost": round(cost, 2),
            "tax": round(tax, 2),
            "profit": round(profit, 2),
            "margin": round(profit / price * 100, 1) if price else 0.0,
            "profit_per_hour": round(profit / hours, 2) if hours else 0.0,
            "grams": round(grams, 1),
            "hours": round(hours, 2),
            "debt": round(max(0.0, price - num(order.get("prepaid"))), 2),
        }

    # ------------------------------------------------------------------ склад
    def pick_spool(self, printer_id: str = "", ams_slot: str = "",
                   material: str = "", tray_uuid: str = "") -> dict | None:
        """Найти катушку: по метке AMS, затем по слоту, затем по материалу."""
        if tray_uuid:
            row = self.db.one(
                "SELECT * FROM spools WHERE tray_uuid=? AND archived=0", (tray_uuid,))
            if row:
                return row
        if printer_id and ams_slot != "":
            row = self.db.one(
                "SELECT * FROM spools WHERE printer_id=? AND ams_slot=? AND archived=0",
                (printer_id, str(ams_slot)))
            if row:
                return row
        if material:
            row = self.db.one(
                "SELECT * FROM spools WHERE UPPER(material)=? AND archived=0"
                " AND remaining_grams>0 ORDER BY remaining_grams DESC LIMIT 1",
                (material.upper(),))
            if row:
                return row
        return None

    def consume_filament(self, grams: float, spool_id: str = "", job_id: str = "",
                         order_id: str = "", note: str = "", auto: bool = True,
                         printer_id: str = "", ams_slot: str = "", material: str = "",
                         tray_uuid: str = "") -> dict:
        """Списать пластик со склада и записать расход."""
        grams = num(grams)
        if grams <= 0:
            return {"ok": False, "reason": "нулевой расход"}
        spool = None
        if spool_id:
            spool = self.db.one("SELECT * FROM spools WHERE id=?", (spool_id,))
        if not spool:
            spool = self.pick_spool(printer_id, ams_slot, material, tray_uuid)
        cost = 0.0
        if spool:
            weight = max(1.0, num(spool["total_grams"], 1000))
            cost = grams * num(spool["price"], 0) / weight
            remaining = max(0.0, num(spool["remaining_grams"]) - grams)
            self.db.execute(
                "UPDATE spools SET remaining_grams=?, updated_at=? WHERE id=?",
                (round(remaining, 1), now_iso(), spool["id"]))
            spool_id = spool["id"]
            threshold = num(self.db.setting("filament_low_threshold", 15))
            if remaining / weight * 100 <= threshold:
                self.db.add_event(
                    "filament_low", "Пластик заканчивается",
                    f"{spool['material']} {spool['color_name']}: осталось {round(remaining)} г",
                    printer_id, {"spool_id": spool_id, "remaining": remaining})
        else:
            s = self.db.settings()
            cost = grams * num(s["default_spool_price"], 1600) / max(1.0, num(s["default_spool_weight"], 1000))
        self.db.execute(
            "INSERT INTO filament_usage(at,spool_id,job_id,order_id,grams,cost,note,auto)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (now_iso(), spool_id or None, job_id or None, order_id or None,
             round(grams, 1), round(cost, 2), note, 1 if auto else 0))
        return {"ok": True, "spool_id": spool_id, "grams": round(grams, 1), "cost": round(cost, 2)}

    def restock_spool(self, spool_id: str, grams: float, price: float = 0.0) -> dict:
        """Приход катушки: пополнение остатка и расход в кассе."""
        spool = self.db.one("SELECT * FROM spools WHERE id=?", (spool_id,))
        if not spool:
            raise ValueError("Катушка не найдена")
        remaining = num(spool["remaining_grams"]) + num(grams)
        self.db.execute("UPDATE spools SET remaining_grams=?, updated_at=? WHERE id=?",
                        (round(remaining, 1), now_iso(), spool_id))
        if price:
            self.add_transaction("expense", "filament", price,
                                 f"Катушка {spool['material']} {spool['color_name']}", auto=False)
        return self.db.one("SELECT * FROM spools WHERE id=?", (spool_id,)) or {}

    # ------------------------------------------------------------------ касса
    def add_transaction(self, kind: str, category: str, amount: float, title: str,
                        note: str = "", order_id: str = "", job_id: str = "",
                        auto: bool = False) -> dict:
        if kind not in ("income", "expense"):
            raise ValueError("Тип проводки: income или expense")
        tx_id = uid("tx")
        self.db.execute(
            "INSERT INTO transactions(id,at,kind,category,amount,title,note,order_id,job_id,auto)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (tx_id, now_iso(), kind, category, round(num(amount), 2), title, note,
             order_id or None, job_id or None, 1 if auto else 0))
        return self.db.one("SELECT * FROM transactions WHERE id=?", (tx_id,)) or {}

    def register_order_income(self, order: dict) -> dict | None:
        """Доход по заказу при переводе в финальный статус (однократно)."""
        if not self.db.setting("auto_income_on_done", True):
            return None
        exists = self.db.one(
            "SELECT id FROM transactions WHERE order_id=? AND kind='income' AND category='order'",
            (order["id"],))
        if exists:
            return None
        price = num(order.get("price"))
        if price <= 0:
            return None
        return self.add_transaction(
            "income", "order", price,
            f"Заказ №{order.get('number','')} · {order.get('product','')}",
            order_id=order["id"], auto=True)

    def register_job_costs(self, job: dict) -> dict:
        """Учёт завершённой печати: пластик, энергия, себестоимость заказа."""
        result: dict[str, Any] = {"job_id": job.get("id")}
        if not self.db.setting("auto_accounting", True):
            return result
        grams = num(job.get("grams"))
        hours = num(job.get("duration_min")) / 60.0
        breakdown = self.cost_breakdown(grams, hours)
        result["breakdown"] = breakdown

        if grams > 0 and self.db.setting("auto_consume_filament", True):
            result["filament"] = self.consume_filament(
                grams, spool_id=job.get("spool_id") or "", job_id=job.get("id", ""),
                order_id=job.get("order_id") or "", note=job.get("name", ""),
                printer_id=job.get("printer_id") or "",
                ams_slot=str(job.get("ams_slot") or ""))

        self.db.execute(
            "UPDATE print_jobs SET cost=?, energy_kwh=? WHERE id=?",
            (breakdown["total"], breakdown["energy_kwh"], job["id"]))

        order_id = job.get("order_id")
        if order_id:
            order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
            if order and order.get("auto_cost", 1):
                self.db.execute(
                    "UPDATE orders SET actual_grams=actual_grams+?, actual_hours=actual_hours+?,"
                    " actual_cost=actual_cost+?, updated_at=? WHERE id=?",
                    (grams, round(hours, 3), breakdown["total"], now_iso(), order_id))
                result["order_updated"] = order_id
        self.bump_stats(job.get("printer_id") or "", hours * 60, grams,
                        done=job.get("state") == "done",
                        failed=job.get("state") == "failed",
                        energy=breakdown["energy_kwh"])
        return result

    def bump_stats(self, printer_id: str, minutes: float, grams: float,
                   done: bool = False, failed: bool = False, energy: float = 0.0) -> None:
        day = date.today().isoformat()
        self.db.execute(
            "INSERT INTO printer_stats(day,printer_id,print_minutes,grams,jobs_done,jobs_failed,energy_kwh)"
            " VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(day,printer_id) DO UPDATE SET"
            " print_minutes=print_minutes+excluded.print_minutes,"
            " grams=grams+excluded.grams, jobs_done=jobs_done+excluded.jobs_done,"
            " jobs_failed=jobs_failed+excluded.jobs_failed,"
            " energy_kwh=energy_kwh+excluded.energy_kwh",
            (day, printer_id or "", round(num(minutes), 1), round(num(grams), 1),
             1 if done else 0, 1 if failed else 0, round(num(energy), 3)))

    # ------------------------------------------------------------------ отчёты
    def summary(self, days: int = 30) -> dict[str, Any]:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        income = num(self.db.one(
            "SELECT COALESCE(SUM(amount),0) v FROM transactions WHERE kind='income' AND at>=?",
            (since,))["v"])
        expense = num(self.db.one(
            "SELECT COALESCE(SUM(amount),0) v FROM transactions WHERE kind='expense' AND at>=?",
            (since,))["v"])
        jobs = self.db.one(
            "SELECT COUNT(*) n, COALESCE(SUM(duration_min),0) m, COALESCE(SUM(grams),0) g,"
            " COALESCE(SUM(cost),0) c, COALESCE(SUM(energy_kwh),0) e"
            " FROM print_jobs WHERE state='done' AND finished_at>=?", (since,)) or {}
        failed = num((self.db.one(
            "SELECT COUNT(*) n FROM print_jobs WHERE state='failed' AND finished_at>=?",
            (since,)) or {}).get("n"))
        orders = self.db.query("SELECT * FROM orders")
        finals = {r["id"] for r in self.db.query("SELECT id FROM statuses WHERE is_final=1")}
        active = [o for o in orders if o["status"] not in finals]
        pipeline = sum(max(0.0, num(o["price"]) - num(o["prepaid"])) for o in active)
        total_minutes = num(jobs.get("m"))
        done_jobs = int(num(jobs.get("n")))
        profit = income - expense
        return {
            "period_days": days,
            "income": round(income, 2),
            "expense": round(expense, 2),
            "profit": round(profit, 2),
            "margin": round(profit / income * 100, 1) if income else 0.0,
            "print_hours": round(total_minutes / 60, 1),
            "profit_per_print_hour": round(profit / (total_minutes / 60), 2) if total_minutes else 0.0,
            "grams": round(num(jobs.get("g")), 1),
            "energy_kwh": round(num(jobs.get("e")), 2),
            "jobs_done": done_jobs,
            "jobs_failed": int(failed),
            "failure_rate": round(failed / (done_jobs + failed) * 100, 1) if (done_jobs + failed) else 0.0,
            "active_orders": len(active),
            "pipeline": round(pipeline, 2),
            "stock_grams": round(num((self.db.one(
                "SELECT COALESCE(SUM(remaining_grams),0) v FROM spools WHERE archived=0") or {}).get("v")), 1),
            "stock_value": round(num((self.db.one(
                "SELECT COALESCE(SUM(remaining_grams/NULLIF(total_grams,0)*price),0) v"
                " FROM spools WHERE archived=0") or {}).get("v")), 2),
        }

    def daily_series(self, days: int = 30) -> list[dict]:
        """Ряды для графиков: деньги и часы печати по дням."""
        today = date.today()
        start = today - timedelta(days=days - 1)
        money: dict[str, dict[str, float]] = {}
        for row in self.db.query(
                "SELECT substr(at,1,10) d, kind, COALESCE(SUM(amount),0) v"
                " FROM transactions WHERE substr(at,1,10)>=? GROUP BY d, kind",
                (start.isoformat(),)):
            money.setdefault(row["d"], {})[row["kind"]] = num(row["v"])
        stats = {r["day"]: r for r in self.db.query(
            "SELECT day, SUM(print_minutes) print_minutes, SUM(grams) grams,"
            " SUM(jobs_done) jobs_done FROM printer_stats WHERE day>=? GROUP BY day",
            (start.isoformat(),))}
        series = []
        for i in range(days):
            day = (start + timedelta(days=i)).isoformat()
            m = money.get(day, {})
            st = stats.get(day, {})
            series.append({
                "day": day,
                "income": round(num(m.get("income")), 2),
                "expense": round(num(m.get("expense")), 2),
                "profit": round(num(m.get("income")) - num(m.get("expense")), 2),
                "hours": round(num(st.get("print_minutes")) / 60, 2),
                "grams": round(num(st.get("grams")), 1),
                "jobs": int(num(st.get("jobs_done"))),
            })
        return series

    def niche_report(self) -> list[dict]:
        rows = []
        finals = {r["id"] for r in self.db.query("SELECT id FROM statuses WHERE is_final=1")}
        for niche in self.db.query("SELECT * FROM niches ORDER BY position, name"):
            orders = self.db.query("SELECT * FROM orders WHERE niche_id=?", (niche["id"],))
            revenue = sum(num(o["price"]) for o in orders)
            profit = sum(self.order_economics(o)["profit"] for o in orders)
            hours = sum(num(o["actual_hours"]) or num(o["hours"]) for o in orders)
            buyers: dict[str, int] = {}
            for o in orders:
                key = o["phone"] or o["customer_name"]
                if key:
                    buyers[key] = buyers.get(key, 0) + 1
            rows.append({
                **niche,
                "orders": len(orders),
                "done": len([o for o in orders if o["status"] in finals]),
                "revenue": round(revenue, 2),
                "profit": round(profit, 2),
                "hours": round(hours, 1),
                "profit_per_hour": round(profit / hours, 2) if hours else 0.0,
                "repeat_buyers": len([k for k, v in buyers.items() if v > 1]),
                "conversion": round(len(orders) / num(niche["leads"]) * 100, 1) if num(niche["leads"]) else 0.0,
            })
        return rows

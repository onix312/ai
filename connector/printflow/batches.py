"""Партии печати PrintFlow 3.0.

Партия — это «напечатать N штук товара»: она раскладывается на запуски
принтера по числу изделий на плите, ставит задания в очередь и по мере
завершения печати сама приходует готовое на склад документом производства.

Если печать сорвалась — штуки уходят в брак, партия становится «частичной»
и предлагает допечатать недостающее.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any

from .accounting import Accounting, num, uid
from .config import now_iso
from .db import Database
from .documents import Documents
from .nomenclature import Nomenclature
from .stock import Stock

MODES = {"full": "Полные плиты", "exact": "Ровно N", "manual": "Вручную",
         "mixed": "Разные товары на плите"}


class Batches:
    """Планирование, запуск и приёмка партий печати."""

    def __init__(self, db: Database, manager=None):
        self.db = db
        self.manager = manager
        self.stock = Stock(db)
        self.acc = Accounting(db)
        self.docs = Documents(db)
        self.nom = Nomenclature(db)

    # ----------------------------------------------------------------- план
    def plan(self, nom_id: str, qty: float, mode: str = "full",
             plates: int = 0, printer_id: str = "", spool_id: str = "",
             price: float = 0.0) -> dict[str, Any]:
        """Расчёт партии без записи в базу: плиты, время, пластик, деньги."""
        item = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
        if not item:
            raise ValueError("Товар не найден")
        qty = max(0.0, num(qty))
        fit = max(1, int(num(item.get("fit_per_plate"), 1) or 1))
        grams_unit = num(item.get("grams"))
        hours_unit = num(item.get("hours"))

        if mode == "manual" and plates:
            plates_n = max(1, int(num(plates)))
        elif mode == "exact":
            plates_n = max(1, int(qty // fit)) if qty >= fit else 1
        else:
            plates_n = max(1, math.ceil(qty / fit)) if qty else 1

        qty_real = plates_n * fit
        qty_extra = round(qty_real - qty, 2)
        grams = round(grams_unit * qty_real, 1)
        hours = round(hours_unit * qty_real, 2)

        breakdown = self.acc.cost_breakdown(
            grams, hours, manual_minutes=num(item.get("post_minutes")) * qty_real,
            qty=max(1.0, qty_real))
        price = num(price) or self.docs.price_of(nom_id)
        revenue = round(price * qty_real, 2)
        profit = round(revenue - num(breakdown["total"]), 2)
        target = num(self.db.setting("target_profit_per_hour", 250), 250)
        per_hour = round(profit / hours, 2) if hours else 0.0

        warnings = self._warnings(item, grams, spool_id, printer_id, qty_real)
        verdict = "ok" if hours and per_hour >= target else (
            "warn" if hours and per_hour >= target * 0.4 else "bad")
        if not hours:
            verdict = "unknown"

        eta = ""
        if hours:
            eta = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="minutes")

        return {
            "nom_id": nom_id, "name": item.get("name"), "mode": mode,
            "fit_per_plate": fit, "plates": plates_n,
            "qty_requested": qty, "qty_real": qty_real, "qty_extra": qty_extra,
            "grams": grams, "hours": hours, "eta": eta,
            "material": item.get("material") or "", "file": item.get("file") or "",
            "cost": breakdown, "cost_per_unit": round(num(breakdown["total"]) / max(1, qty_real), 2),
            "price": price, "revenue": revenue, "profit": profit,
            "profit_per_hour": per_hour, "target_per_hour": target,
            "verdict": verdict, "warnings": warnings,
        }

    def plan_multi(self, items: list, plates: float = 1, printer_id: str = "",
                   spool_id: str = "", file: str = "") -> dict:
        """План смешанной плиты: разные товары на одном столе.

        ``items`` — [{nom_id, qty}] сколько штук каждого товара на ОДНУ плиту.
        Вся плита печатается одним файлом (``file``), приходуется по завершении
        каждой плиты: каждого товара — своё количество.
        """
        if not isinstance(items, list) or not items:
            raise ValueError("Укажите хотя бы один товар")
        rows: list[dict] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            nom = self.db.one("SELECT * FROM nomenclature WHERE id=?", (it.get("nom_id") or "",))
            if not nom:
                raise ValueError(f"Товар не найден: {it.get('nom_id') or '—'}")
            qty = int(num(it.get("qty")))
            if qty <= 0:
                continue
            rows.append({
                "nom_id": nom["id"], "name": nom.get("name") or "",
                "qty_per_plate": qty,
                "grams": round(num(nom.get("grams")) * qty, 1),
                "hours": round(num(nom.get("hours")) * qty, 2),
                "price": self.docs.price_of(nom["id"]),
                "post_minutes": num(nom.get("post_minutes")),
            })
        if not rows:
            raise ValueError("Укажите количество хотя бы одного товара")
        plates_n = max(1, int(num(plates)))
        grams = round(sum(r["grams"] for r in rows) * plates_n, 1)
        hours = round(sum(r["hours"] for r in rows) * plates_n, 2)
        manual = round(sum(r["post_minutes"] * r["qty_per_plate"] for r in rows) * plates_n, 1)
        breakdown = self.acc.cost_breakdown(grams, hours, manual_minutes=manual,
                                            qty=max(1.0, plates_n))
        units_per_plate = sum(r["qty_per_plate"] for r in rows)
        qty_real = units_per_plate * plates_n
        revenue = round(sum(r["price"] * r["qty_per_plate"] for r in rows) * plates_n, 2)
        profit = round(revenue - num(breakdown["total"]), 2)
        target = num(self.db.setting("target_profit_per_hour", 250), 250)
        per_hour = round(profit / hours, 2) if hours else 0.0
        warnings = self._warnings_multi(rows, grams, spool_id, printer_id, file)
        verdict = "ok" if hours and per_hour >= target else (
            "warn" if hours and per_hour >= target * 0.4 else "bad")
        if not hours:
            verdict = "unknown"
        eta = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="minutes") if hours else ""
        return {
            "mixed": True, "items": rows, "plates": plates_n,
            "units_per_plate": units_per_plate, "qty_real": qty_real,
            "grams": grams, "hours": hours, "eta": eta, "file": file or "",
            "cost": breakdown,
            "cost_per_unit": round(num(breakdown["total"]) / max(1, qty_real), 2),
            "revenue": revenue, "profit": profit,
            "profit_per_hour": per_hour, "target_per_hour": target,
            "verdict": verdict, "warnings": warnings,
        }

    def _warnings_multi(self, rows: list[dict], grams: float, spool_id: str,
                        printer_id: str, file: str) -> list[dict]:
        out: list[dict] = []
        if not file:
            out.append({"kind": "file", "level": "bad",
                        "text": "Укажите файл плиты на принтере — без него задание не стартует"})
        for r in rows:
            if not num(r.get("hours")):
                out.append({"kind": "norms", "level": "warn",
                            "text": f"У «{r['name']}» не заполнено время печати — расчёт приблизительный"})
        left = num((self.db.one(
            "SELECT COALESCE(SUM(remaining_grams),0) v FROM spools"
            " WHERE archived=0" + (" AND id=?" if spool_id else ""),
            ((spool_id,) if spool_id else ())) or {}).get("v"))
        if grams and left < grams:
            out.append({"kind": "filament", "level": "warn",
                        "text": f"Пластика на складе {round(left)} г — на партию нужно {round(grams)} г"})
        return out

    def _warnings(self, item: dict, grams: float, spool_id: str,
                  printer_id: str, qty_real: float) -> list[dict]:
        out: list[dict] = []
        if not item.get("file"):
            out.append({"kind": "file", "level": "bad",
                        "text": "У товара не указан файл на принтере — задание не стартует"})
        if not num(item.get("grams")) or not num(item.get("hours")):
            out.append({"kind": "norms", "level": "warn",
                        "text": "Не заполнены нормативы веса и времени — расчёт приблизительный"})

        # хватит ли пластика
        material = (item.get("material") or "").strip()
        sql = "SELECT COALESCE(SUM(remaining_grams),0) v FROM spools WHERE 1=1"
        params: list[Any] = []
        if spool_id:
            sql += " AND id=?"
            params.append(spool_id)
        elif material:
            sql += " AND pylower(material)=?"
            params.append(material.lower())
        row = self.db.one(sql, params) or {}
        left = num(row.get("v"))
        if grams and left < grams:
            unit = num(item.get("grams")) or 1
            fits = int(left // unit) if unit else 0
            out.append({"kind": "filament", "level": "warn",
                        "text": f"Пластика {round(left)} г — хватит на {fits} шт из {int(qty_real)}"})

        # тихие часы
        if self.manager and getattr(self.manager, "quiet_now", None):
            try:
                if self.manager.quiet_now():
                    out.append({"kind": "quiet", "level": "info",
                                "text": "Идут тихие часы — старт будет отложен"})
            except Exception:
                pass

        # затоваривание
        qty_now = self.stock.qty(item["id"])
        stats = self.stock.sales_stats(item["id"])
        rate = num(stats.get("rate_per_day"))
        if qty_now and rate and (qty_now + qty_real) / rate > 60:
            out.append({"kind": "overstock", "level": "warn",
                        "text": f"После партии запас на {round((qty_now + qty_real) / rate)} дней — деньги заморозятся"})
        return out

    # -------------------------------------------------------------- создание
    def create(self, data: dict) -> dict:
        """Создать партию и поставить задания в очередь печати."""
        if isinstance(data.get("items"), list) and data.get("items"):
            return self._create_multi(data)
        nom_id = data.get("nom_id") or ""
        plan = self.plan(nom_id, num(data.get("qty")), data.get("mode", "full"),
                         int(num(data.get("plates"))), data.get("printer_id", ""),
                         data.get("spool_id", ""), num(data.get("price")))
        item = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
        warehouse_id = data.get("warehouse_id") or self._default_warehouse()

        batch = {
            "id": uid("bat"), "number": self.docs.next_number("production"),
            "at": now_iso(), "nom_id": nom_id,
            "variant_id": data.get("variant_id") or None,
            "warehouse_id": warehouse_id,
            "order_id": data.get("order_id") or None,
            "name": item.get("name") or "",
            "qty_planned": plan["qty_real"], "qty_done": 0, "qty_scrap": 0,
            "fit_per_plate": plan["fit_per_plate"], "plates": plan["plates"],
            "mode": plan["mode"], "file": item.get("file") or "",
            "printer_id": data.get("printer_id") or None,
            "spool_id": data.get("spool_id") or None,
            "material": plan["material"],
            "est_grams": plan["grams"], "est_minutes": round(plan["hours"] * 60, 1),
            "cost": 0.0, "state": "planned", "note": data.get("note", ""),
        }
        with self.db.transaction():
            row = self.db.upsert("batches", batch)
            jobs = []
            if self.manager and item.get("file"):
                per_plate_g = round(plan["grams"] / max(1, plan["plates"]), 1)
                per_plate_m = round(plan["hours"] * 60 / max(1, plan["plates"]), 1)
                for index in range(plan["plates"]):
                    job = self.manager.enqueue({
                        "name": f"{item.get('name')} · плита {index + 1}/{plan['plates']}",
                        "file": item.get("file"),
                        "printer_id": data.get("printer_id", ""),
                        "order_id": data.get("order_id", ""),
                        "spool_id": data.get("spool_id", ""),
                        "priority": int(num(data.get("priority"))),
                        "source": "batch",
                    })
                    self.db.execute(
                        "UPDATE print_jobs SET batch_id=?, batch_qty=?,"
                        " est_grams=COALESCE(NULLIF(est_grams,0),?),"
                        " est_minutes=COALESCE(NULLIF(est_minutes,0),?) WHERE id=?",
                        (row["id"], plan["fit_per_plate"], per_plate_g, per_plate_m, job["id"]))
                    jobs.append(job["id"])
        self.db.add_event("batch", "Партия создана",
                          f"{item.get('name')} · {int(plan['qty_real'])} шт · {plan['plates']} запуск(ов)",
                          data={"batch_id": row["id"]})

        if data.get("start_now") and jobs and self.manager:
            try:
                self.manager.start_job(jobs[0], data.get("printer_id", ""))
                self.db.execute("UPDATE batches SET state='printing' WHERE id=?", (row["id"],))
            except Exception as exc:
                self.db.add_event("batch", "Партия создана, но старт не удался",
                                  str(exc), data={"batch_id": row["id"]})
        return self.get(row["id"]) or {}

    def _create_multi(self, data: dict) -> dict:
        """Смешанная партия: несколько разных товаров на одной плите."""
        plan = self.plan_multi(data.get("items") or [], num(data.get("plates"), 1),
                               data.get("printer_id", ""), data.get("spool_id", ""),
                               data.get("file", ""))
        if not plan["file"]:
            raise ValueError("Укажите файл плиты на принтере")
        warehouse_id = data.get("warehouse_id") or self._default_warehouse()
        name = " + ".join(f"{r['name']}×{r['qty_per_plate']}" for r in plan["items"])
        batch = {
            "id": uid("bat"), "number": self.docs.next_number("production"),
            "at": now_iso(), "nom_id": plan["items"][0]["nom_id"],
            "warehouse_id": warehouse_id,
            "order_id": data.get("order_id") or None,
            "name": name,
            "qty_planned": plan["qty_real"], "qty_done": 0, "qty_scrap": 0,
            "fit_per_plate": plan["units_per_plate"], "plates": plan["plates"],
            "mode": "mixed", "file": plan["file"],
            "printer_id": data.get("printer_id") or None,
            "spool_id": data.get("spool_id") or None,
            "material": data.get("material", ""),
            "est_grams": plan["grams"], "est_minutes": round(plan["hours"] * 60, 1),
            "cost": 0.0, "state": "planned", "note": data.get("note", ""),
            "items": json.dumps(plan["items"], ensure_ascii=False),
        }
        with self.db.transaction():
            row = self.db.upsert("batches", batch)
            jobs: list[str] = []
            if self.manager:
                per_plate_g = round(plan["grams"] / max(1, plan["plates"]), 1)
                per_plate_m = round(plan["hours"] * 60 / max(1, plan["plates"]), 1)
                for index in range(plan["plates"]):
                    job = self.manager.enqueue({
                        "name": f"{name} · плита {index + 1}/{plan['plates']}",
                        "file": plan["file"],
                        "printer_id": data.get("printer_id", ""),
                        "order_id": data.get("order_id", ""),
                        "spool_id": data.get("spool_id", ""),
                        "priority": int(num(data.get("priority"))),
                        "source": "batch",
                    })
                    self.db.execute(
                        "UPDATE print_jobs SET batch_id=?, batch_qty=?,"
                        " est_grams=COALESCE(NULLIF(est_grams,0),?),"
                        " est_minutes=COALESCE(NULLIF(est_minutes,0),?) WHERE id=?",
                        (row["id"], plan["units_per_plate"], per_plate_g, per_plate_m, job["id"]))
                    jobs.append(job["id"])
        self.db.add_event("batch", "Смешанная партия создана",
                          f"{name} · {plan['plates']} плита(ит)", data={"batch_id": row["id"]})
        if data.get("start_now") and jobs and self.manager:
            try:
                self.manager.start_job(jobs[0], data.get("printer_id", ""))
                self.db.execute("UPDATE batches SET state='printing' WHERE id=?", (row["id"],))
            except Exception as exc:
                self.db.add_event("batch", "Партия создана, но старт не удался",
                                  str(exc), data={"batch_id": row["id"]})
        return self.get(row["id"]) or {}

    def _default_warehouse(self) -> str:
        row = self.db.one(
            "SELECT id FROM warehouses WHERE archived=0 AND retail=1 ORDER BY position LIMIT 1"
        ) or self.db.one("SELECT id FROM warehouses WHERE archived=0 ORDER BY position LIMIT 1")
        if not row:
            raise ValueError("Не настроен ни один склад")
        return row["id"]

    # ---------------------------------------------------------------- чтение
    def list(self, state: str = "", limit: int = 100) -> list[dict]:
        sql = ("SELECT b.*, n.name nom_name, w.name warehouse_name FROM batches b"
               " LEFT JOIN nomenclature n ON n.id=b.nom_id"
               " LEFT JOIN warehouses w ON w.id=b.warehouse_id WHERE 1=1")
        params: list[Any] = []
        if state:
            sql += " AND b.state=?"
            params.append(state)
        sql += " ORDER BY datetime(b.at) DESC LIMIT ?"
        params.append(int(limit))
        rows = self.db.query(sql, params)
        for row in rows:
            row["mode_label"] = MODES.get(row.get("mode") or "full", "")
            row["items_list"] = self._mixed_rows(row)
            row["progress"] = round(num(row["qty_done"]) / max(1.0, num(row["qty_planned"])) * 100, 1)
        return rows

    def get(self, batch_id: str) -> dict | None:
        row = self.db.one(
            "SELECT b.*, n.name nom_name, w.name warehouse_name FROM batches b"
            " LEFT JOIN nomenclature n ON n.id=b.nom_id"
            " LEFT JOIN warehouses w ON w.id=b.warehouse_id WHERE b.id=?", (batch_id,))
        if not row:
            return None
        row["mode_label"] = MODES.get(row.get("mode") or "full", "")
        row["progress"] = round(num(row["qty_done"]) / max(1.0, num(row["qty_planned"])) * 100, 1)
        row["items_list"] = self._mixed_rows(row)
        row["jobs"] = self.db.query(
            "SELECT * FROM print_jobs WHERE batch_id=? ORDER BY datetime(created_at)",
            (batch_id,))
        row["docs"] = self.db.query(
            "SELECT * FROM documents WHERE batch_id=? ORDER BY datetime(at) DESC", (batch_id,))
        row["remaining"] = max(0.0, round(num(row["qty_planned"]) - num(row["qty_done"]), 1))
        return row

    # ------------------------------------------------------------- приёмка
    def receive(self, batch_id: str, qty: float, scrap: float = 0.0,
                job_id: str = "", cost: float = 0.0, note: str = "",
                items: list | None = None) -> dict:
        """Оприходовать готовое на склад документом производства.

        ``items`` — состав смешанной плиты [{nom_id, qty}]: документ
        приходует каждый товар своим количеством. ``qty`` при этом —
        суммарное число штук.
        """
        batch = self.db.one("SELECT * FROM batches WHERE id=?", (batch_id,))
        if not batch:
            raise ValueError("Партия не найдена")
        qty = num(qty)
        scrap = num(scrap)
        if qty <= 0 and scrap <= 0:
            raise ValueError("Укажите количество")
        items = [it for it in (items or [])
                 if isinstance(it, dict) and it.get("nom_id") and num(it.get("qty")) > 0]

        doc = None
        if qty > 0:
            unit_cost = num(cost)
            if not unit_cost and job_id:
                job = self.db.one("SELECT cost FROM print_jobs WHERE id=?", (job_id,))
                if job and num(job.get("cost")) > 0:
                    unit_cost = num(job["cost"]) / qty
            rows = self._mixed_rows(batch) if items else []
            if not unit_cost:
                if rows:
                    br = self.acc.cost_breakdown(
                        sum(num(r.get("grams")) for r in rows),
                        sum(num(r.get("hours")) for r in rows), qty=max(1.0, qty))
                else:
                    item = self.db.one("SELECT * FROM nomenclature WHERE id=?", (batch["nom_id"],))
                    br = self.acc.cost_breakdown(
                        num((item or {}).get("grams")) * qty,
                        num((item or {}).get("hours")) * qty, qty=qty)
                unit_cost = round(num(br["total"]) / max(1.0, qty), 2)
            if items:
                # Смешанная плита: себестоимость делится по доле граммов
                # товара из состава партии (нет нормативов — поровну).
                total_g = sum(num(r.get("grams")) for r in rows) or float(len(rows))
                share_total = round(unit_cost * qty, 2)
                lines = []
                for it in items:
                    row = next((r for r in rows if r["nom_id"] == it["nom_id"]), {})
                    share_g = num(row.get("grams")) or (total_g / max(1, len(rows)))
                    per_unit = round(share_total * share_g / total_g / max(1.0, num(it["qty"])), 2)
                    lines.append({"nom_id": it["nom_id"], "variant_id": batch.get("variant_id") or "",
                                  "qty": num(it["qty"]), "cost": per_unit, "price": per_unit})
            else:
                lines = [{"nom_id": batch["nom_id"], "variant_id": batch.get("variant_id") or "",
                          "qty": qty, "cost": unit_cost, "price": unit_cost}]
            doc = self.docs.save({
                "kind": "production", "warehouse_id": batch.get("warehouse_id"),
                "batch_id": batch_id, "at": now_iso(),
                "note": note or f"Партия {batch.get('number') or batch_id}",
                "items": lines})
            doc = self.docs.post(doc["id"])

        done = num(batch["qty_done"]) + qty
        scrap_total = num(batch["qty_scrap"]) + scrap
        cost_total = num(batch["cost"]) + num((doc or {}).get("cost_total"))
        state = batch["state"]
        finished = None
        if done >= num(batch["qty_planned"]) - 0.001:
            state, finished = "done", now_iso()
        elif done + scrap_total >= num(batch["qty_planned"]) - 0.001:
            state, finished = "partial", now_iso()
        elif done > 0:
            state = "printing"

        self.db.execute(
            "UPDATE batches SET qty_done=?, qty_scrap=?, cost=?, state=?, finished_at=?"
            " WHERE id=?",
            (round(done, 2), round(scrap_total, 2), round(cost_total, 2),
             state, finished, batch_id))

        if state == "done":
            self.db.add_event("batch", "Партия готова",
                              f"{batch.get('name')} · {int(done)} шт на складе",
                              data={"batch_id": batch_id})
            self._notify(f"✅ Партия готова: {batch.get('name')} — {int(done)} шт на складе")
            # Автообновление себестоимости в номенклатуре
            self._auto_update_cost(batch_id, batch["nom_id"], done, cost_total)
        elif scrap:
            self.db.add_event("batch", "Брак в партии",
                              f"{batch.get('name')} · −{int(scrap)} шт",
                              data={"batch_id": batch_id})
        return self.get(batch_id) or {}

    def _mixed_rows(self, batch: dict) -> list[dict]:
        """Состав смешанной партии из batches.items (JSON)."""
        try:
            rows = json.loads(str(batch.get("items") or "") or "[]")
        except (json.JSONDecodeError, TypeError):
            rows = []
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict) and r.get("nom_id")]

    def _auto_update_cost(self, batch_id: str, nom_id: str, qty: float, cost: float) -> None:
        """Обновить себестоимость в номенклатуре из завершённой партии."""
        if qty <= 0 or cost <= 0:
            return
        batch = self.db.one("SELECT items FROM batches WHERE id=?", (batch_id,)) or {}
        rows = self._mixed_rows(batch)
        if rows:
            # Смешанная партия: себестоимость каждого товара — по доле
            # граммов, чтобы не задирать цену лёгким позициям.
            total_g = sum(num(r.get("grams")) for r in rows) or float(len(rows))
            for r in rows:
                share_g = num(r.get("grams")) or total_g / len(rows)
                per_unit = round(cost * share_g / total_g / max(1.0, num(r.get("qty_per_plate"), 1)), 2)
                if per_unit > 0:
                    self.db.execute("UPDATE nomenclature SET cost=?, updated_at=? WHERE id=?",
                                    (per_unit, now_iso(), r["nom_id"]))
            return
        unit_cost = round(cost / qty, 2)
        self.db.execute(
            "UPDATE nomenclature SET cost=?, updated_at=? WHERE id=?",
            (unit_cost, now_iso(), nom_id))

    def on_job_finished(self, job: dict) -> None:
        """Хук из менеджера печати: задание партии завершилось."""
        batch_id = job.get("batch_id")
        if not batch_id:
            return
        batch = self.db.one("SELECT * FROM batches WHERE id=?", (batch_id,))
        if not batch or batch["state"] in ("done", "cancelled"):
            return
        rows = self._mixed_rows(batch)
        qty = num(job.get("batch_qty")) or num(batch.get("fit_per_plate"), 1)
        state = job.get("state")
        try:
            if state == "done":
                unit_cost = 0.0
                if num(job.get("cost")) > 0 and qty:
                    unit_cost = num(job["cost"]) / qty
                self.receive(batch_id, qty, 0.0, job.get("id", ""), unit_cost,
                             note=f"Печать {job.get('name') or ''}".strip(),
                             items=[{"nom_id": r["nom_id"],
                                     "qty": num(r.get("qty_per_plate"), 1)} for r in rows]
                             if rows else None)
            elif state in ("failed", "cancelled"):
                self.receive(batch_id, 0.0, qty, job.get("id", ""),
                             note=f"Брак: {job.get('name') or ''}".strip())
        except Exception as exc:
            self.db.add_event("error", "Не удалось оприходовать партию", str(exc),
                              data={"batch_id": batch_id, "job_id": job.get("id")})

    def cancel(self, batch_id: str) -> dict:
        batch = self.db.one("SELECT * FROM batches WHERE id=?", (batch_id,))
        if not batch:
            raise ValueError("Партия не найдена")
        for job in self.db.query(
                "SELECT * FROM print_jobs WHERE batch_id=? AND state IN ('queued','starting')",
                (batch_id,)):
            try:
                if self.manager:
                    self.manager.cancel_job(job["id"])
                else:
                    self.db.execute("UPDATE print_jobs SET state='cancelled' WHERE id=?",
                                    (job["id"],))
            except Exception:
                pass
        self.db.execute("UPDATE batches SET state='cancelled', finished_at=? WHERE id=?",
                        (now_iso(), batch_id))
        return self.get(batch_id) or {}

    def repeat(self, batch_id: str, start_now: bool = False) -> dict:
        batch = self.db.one("SELECT * FROM batches WHERE id=?", (batch_id,))
        if not batch:
            raise ValueError("Партия не найдена")
        rows = self._mixed_rows(batch)
        if rows:
            return self.create({
                "items": [{"nom_id": r["nom_id"], "qty": num(r.get("qty_per_plate"), 1)}
                          for r in rows],
                "plates": int(num(batch.get("plates"), 1)),
                "file": batch.get("file") or "",
                "warehouse_id": batch.get("warehouse_id"),
                "printer_id": batch.get("printer_id") or "", "spool_id": batch.get("spool_id") or "",
                "start_now": start_now, "note": "повтор партии " + (batch.get("number") or "")})
        return self.create({
            "nom_id": batch["nom_id"], "qty": num(batch["qty_planned"]),
            "mode": batch.get("mode") or "full", "warehouse_id": batch.get("warehouse_id"),
            "printer_id": batch.get("printer_id") or "", "spool_id": batch.get("spool_id") or "",
            "start_now": start_now, "note": "повтор партии " + (batch.get("number") or "")})

    # ------------------------------------------------- партия по плану
    def plan_replenishment(self, warehouse_id: str = "") -> list[dict]:
        """Что нужно допечатать: план пополнения с расчётом плит и времени."""
        out = []
        for row in self.nom.replenishment(warehouse_id):
            fit = max(1, int(num(row.get("fit_per_plate"), 1) or 1))
            plates = max(1, math.ceil(num(row["plan_qty"]) / fit))
            out.append({**row, "plates": plates, "qty_real": plates * fit,
                        "hours": round(num(row.get("hours")) * plates * fit, 2),
                        "grams": round(num(row.get("grams")) * plates * fit, 1)})
        return out

    def create_from_plan(self, rows: list[dict], warehouse_id: str = "",
                         start_now: bool = False) -> list[dict]:
        """Собрать партии сразу по нескольким дефицитным позициям."""
        created = []
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("nom_id"):
                continue
            qty = num(row.get("qty"))
            if qty <= 0:
                continue
            created.append(self.create({
                "nom_id": row["nom_id"], "qty": qty, "mode": "full",
                "warehouse_id": warehouse_id, "start_now": False,
                "note": "автопартия по плану пополнения"}))
        if start_now and created and self.manager:
            jobs = self.db.query(
                "SELECT id, printer_id FROM print_jobs WHERE state='queued'"
                " ORDER BY priority DESC, datetime(created_at) LIMIT 1")
            if jobs:
                try:
                    self.manager.start_job(jobs[0]["id"], jobs[0].get("printer_id") or "")
                except Exception:
                    pass
        return created

    def _notify(self, text: str) -> None:
        if self.manager and getattr(self.manager, "notify_async", None):
            try:
                self.manager.notify_async(text)
            except Exception:
                pass

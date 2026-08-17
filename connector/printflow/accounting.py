"""Автоматический производственный и финансовый учёт.

Правила:
    • себестоимость считается по фактам печати, а не по ощущениям;
    • пластик списывается со склада той катушки, которая реально стояла в AMS;
    • каждая завершённая печать и каждый закрытый заказ оставляют проводку;
    • ручные правки пользователя никогда не перетираются автоматикой
      (флаг auto у заказа и проводки).
"""
from __future__ import annotations

import json
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


TAX_MODES = {
    "none": "Без налога",
    "npd": "Самозанятый (НПД)",
    "usn6": "УСН «Доходы» 6%",
    "usn15": "УСН «Доходы минус расходы»",
    "patent": "Патент",
    "manual": "Своя ставка",
}


def rub(value: Any) -> str:
    """Сумма для текстовых подсказок: 57 390 ₽ — с неразрывным пробелом."""
    return f"{num(value):,.0f}".replace(",", "\u00a0") + "\u00a0₽"


def month_key(value: str = "") -> str:
    """YYYY-MM из ISO-даты или текущий месяц."""
    if value and len(value) >= 7:
        return value[:7]
    return date.today().isoformat()[:7]


def quarter_key(value: str = "") -> str:
    m = month_key(value)
    year, mon = int(m[:4]), int(m[5:7])
    return f"{year}-Q{(mon - 1) // 3 + 1}"


def month_bounds(key: str) -> tuple[str, str]:
    """Начало месяца включительно и начало следующего (полуинтервал)."""
    year, mon = int(key[:4]), int(key[5:7])
    nxt = f"{year + 1}-01" if mon == 12 else f"{year}-{mon + 1:02d}"
    return f"{key}-01", f"{nxt}-01"


class Accounting:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------ себестоимость
    def overhead_per_hour(self) -> float:
        """Доля постоянных расходов, приходящаяся на час печати.

        Считается от активных постоянных расходов и реальной месячной ёмкости
        парка: 4,33 недели × ёмкость в неделю. Если разнос выключен — 0.
        """
        s = self.db.settings()
        if not s.get("allocate_fixed_costs"):
            return 0.0
        monthly = self.fixed_costs_monthly()
        hours = num(s.get("weekly_capacity_hours"), 110) * 4.33
        return round(monthly / hours, 2) if hours else 0.0

    def fixed_costs_monthly(self) -> float:
        """Сумма постоянных расходов в пересчёте на месяц."""
        divider = {"month": 1.0, "quarter": 3.0, "year": 12.0}
        total = 0.0
        for row in self.db.query("SELECT * FROM fixed_costs WHERE active=1"):
            total += num(row["amount"]) / divider.get(row["period"], 1.0)
        return round(total, 2)

    def cost_breakdown(self, grams: float, hours: float, spool_price: float | None = None,
                       spool_weight: float | None = None, manual_minutes: float = 0.0,
                       qty: float = 1.0, design_minutes: float = 0.0,
                       delivery: float = 0.0, color_swaps: float = 0.0) -> dict[str, float]:
        """Полная раскладка себестоимости партии.

        Своя работа по умолчанию расходом не считается (count_labor_in_cost),
        но всегда показывается отдельной строкой как ориентир по трудозатратам.
        color_swaps — сколько раз принтер сменит цвет/материал: на каждую смену
        Bambu тратит ~10–15 г на продувку сопла, это реальный расход пластика.
        """
        s = self.db.settings()
        grams = max(0.0, num(grams))
        hours = max(0.0, num(hours))
        qty = max(1.0, num(qty, 1))
        price = num(spool_price if spool_price is not None else s["default_spool_price"], 1600)
        weight = max(1.0, num(spool_weight if spool_weight is not None else s["default_spool_weight"], 1000))

        swaps = max(0.0, num(color_swaps))
        if swaps:
            # продувка между цветами — это те же граммы пластика в отходы
            grams += swaps * 12.0
        filament = grams * price / weight
        energy_kwh = hours * num(s["power_kw"], 0.15)
        energy = energy_kwh * num(s["energy_price"], 6)
        amortization = hours * num(s["amortization_per_hour"], 12)
        maintenance = hours * num(s["maintenance_per_hour"], 3)
        labor = num(manual_minutes) / 60.0 * num(s["labor_rate"], 400)
        design = num(design_minutes) / 60.0 * num(s.get("design_rate"), 800)
        packaging = num(s["packaging_cost"], 15)
        delivery = num(delivery) or num(s.get("delivery_cost"))
        overhead = self.overhead_per_hour() * hours
        counted_labor = (labor + design) if s.get("count_labor_in_cost") else 0.0

        subtotal = (filament + energy + amortization + maintenance
                    + packaging + delivery + overhead + counted_labor)
        failure = subtotal * num(s["failure_rate"], 5) / 100.0
        total = subtotal + failure
        cash = total - overhead  # то, что реально уходит из кассы
        return {
            "filament": round(filament, 2),
            "energy": round(energy, 2),
            "energy_kwh": round(energy_kwh, 3),
            "amortization": round(amortization, 2),
            "maintenance": round(maintenance, 2),
            "labor": round(labor, 2),
            "design": round(design, 2),
            "labor_counted": bool(s.get("count_labor_in_cost")),
            "packaging": round(packaging, 2),
            "delivery": round(delivery, 2),
            "overhead": round(overhead, 2),
            "failure_reserve": round(failure, 2),
            "total": round(total, 2),
            "cash_cost": round(cash, 2),
            "per_unit": round(total / qty, 2),
            "per_hour": round(total / hours, 2) if hours else 0.0,
        }

    def suggest_price(self, cost_per_unit: float, qty: float = 1.0, channel: str = "",
                      rush: bool = False) -> dict[str, float]:
        """Рекомендованная цена: наценка, комиссия канала, скидка за объём."""
        s = self.db.settings()
        qty = max(1.0, num(qty, 1))
        base = num(cost_per_unit) * (1 + num(s.get("default_markup"), 150) / 100.0)
        if rush:
            base *= 1 + num(s.get("rush_surcharge")) / 100.0
        discount = 0.0
        if qty >= 50:
            discount = num(s.get("bulk_discount_50"))
        elif qty >= 10:
            discount = num(s.get("bulk_discount_10"))
        base *= 1 - discount / 100.0

        ch = self.channel(channel)
        fee_percent = num(ch.get("fee_percent")) + num(s.get("acquiring_fee")) if ch else 0.0
        fee_fixed = num(ch.get("fee_fixed")) if ch else 0.0
        # цену поднимаем так, чтобы комиссия не съедала наценку
        gross = (base + fee_fixed) / (1 - fee_percent / 100.0) if fee_percent < 100 else base
        step = max(1.0, num(s.get("price_rounding"), 10))
        price = max(num(s.get("min_order_price")), (int(gross / step) + 1) * step)
        return {
            "price": round(price, 2),
            "markup": num(s.get("default_markup"), 150),
            "discount": discount,
            "fee_percent": round(fee_percent, 2),
            "fee_fixed": round(fee_fixed, 2),
            "min_price": num(s.get("min_order_price")),
        }

    def channel(self, channel_id: str) -> dict:
        if not channel_id:
            return {}
        return self.db.one("SELECT * FROM channels WHERE id=?", (channel_id,)) or {}

    # -------------------------------------------------------------- налоги
    def tax_rate_for(self, payer: str = "person") -> float:
        """Действующая ставка налога с дохода, %."""
        s = self.db.settings()
        mode = s.get("tax_mode", "none")
        if mode == "npd":
            return num(s.get("npd_rate_company"), 6) if payer == "company" \
                else num(s.get("npd_rate_person"), 4)
        if mode == "usn6":
            return num(s.get("usn_income_rate"), 6)
        if mode == "usn15":
            return num(s.get("usn_profit_rate"), 15)
        if mode == "manual":
            return num(s.get("tax_rate"), 0)
        return 0.0  # патент и «без налога» считаются отдельно, не с оборота

    def order_tax(self, price: float, profit_base: float, payer: str = "person") -> float:
        """Налог, приходящийся на один заказ, по выбранному режиму."""
        s = self.db.settings()
        mode = s.get("tax_mode", "none")
        if mode == "usn15":
            return max(0.0, num(profit_base)) * num(s.get("usn_profit_rate"), 15) / 100.0
        if mode in ("npd", "usn6", "manual"):
            return num(price) * self.tax_rate_for(payer) / 100.0
        return 0.0

    def order_economics(self, order: dict) -> dict[str, float]:
        """Экономика заказа: комиссии, доставка, налог и реальные деньги."""
        s = self.db.settings()
        price = num(order.get("price")) - num(order.get("discount"))
        hours = num(order.get("actual_hours")) or num(order.get("hours"))
        grams = num(order.get("actual_grams")) or num(order.get("grams"))
        cost = num(order.get("actual_cost")) or num(order.get("cost"))
        if not cost:
            cost = self.cost_breakdown(grams, hours,
                                       manual_minutes=num(order.get("manual_minutes")),
                                       qty=num(order.get("qty"), 1),
                                       design_minutes=num(order.get("design_minutes")),
                                       delivery=num(order.get("delivery")))["total"]

        ch = self.channel(order.get("channel") or "")
        fee = num(order.get("fee"))
        if not fee and ch:
            fee = price * num(ch.get("fee_percent")) / 100.0 + num(ch.get("fee_fixed"))
        ads = num(ch.get("ads_per_order")) if ch else 0.0
        delivery = num(order.get("delivery"))
        payer = order.get("payer") or (ch.get("payer") if ch else "person") or "person"

        gross = price - fee - ads - delivery
        tax = self.order_tax(price, gross - cost, payer)
        profit = gross - cost - tax
        paid = max(num(order.get("paid")), num(order.get("prepaid")))
        return {
            "price": round(price, 2),
            "cost": round(cost, 2),
            "fee": round(fee, 2),
            "ads": round(ads, 2),
            "delivery": round(delivery, 2),
            "tax": round(tax, 2),
            "tax_rate": self.tax_rate_for(payer),
            "revenue_net": round(gross, 2),
            "profit": round(profit, 2),
            "margin": round(profit / price * 100, 1) if price else 0.0,
            "markup": round((price - cost) / cost * 100, 1) if cost else 0.0,
            "profit_per_hour": round(profit / hours, 2) if hours else 0.0,
            "profit_per_unit": round(profit / max(1.0, num(order.get("qty"), 1)), 2),
            "break_even": round(cost + fee + ads + delivery + tax, 2),
            "grams": round(grams, 1),
            "hours": round(hours, 2),
            "paid": round(paid, 2),
            "debt": round(max(0.0, price - paid), 2),
            "low_margin": bool(price) and (profit / price * 100) < num(s.get("low_margin_alert"), 20),
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

    # --------------------------------------------------- многоцветная печать
    def consume_order_colors(self, job: dict) -> list[dict]:
        """Списать расход по цветам заказа (поле orders.colors).

        Формат: JSON-список [{"material":"PLA","color":"Белый","grams":40}].
        Возвращает список результатов consume_filament.
        """
        order = self.db.one("SELECT colors,material FROM orders WHERE id=?",
                            (job.get("order_id") or "",)) if job.get("order_id") else None
        raw = (order or {}).get("colors") or ""
        try:
            colors = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            colors = []
        if not isinstance(colors, list):
            return []
        out = []
        for item in colors:
            if not isinstance(item, dict):
                continue
            grams = num(item.get("grams"))
            if grams <= 0:
                continue
            # Форма хранит материал на уровне заказа, а в элементах — только цвет.
            material = str(item.get("material") or (order or {}).get("material") or "").upper()
            color = str(item.get("color") or "")
            spool = None
            if color:
                sql = ("SELECT * FROM spools WHERE pylower(color_name)=? AND archived=0"
                       " AND remaining_grams>0")
                params: list[Any] = [color.lower()]
                if material:
                    sql += " AND UPPER(material)=?"
                    params.append(material)
                spool = self.db.one(sql + " ORDER BY remaining_grams DESC LIMIT 1", params)
            if not spool and material:
                spool = self.db.one(
                    "SELECT * FROM spools WHERE UPPER(material)=? AND archived=0"
                    " AND remaining_grams>0 ORDER BY remaining_grams DESC LIMIT 1",
                    (material,))
            result = self.consume_filament(
                grams, spool_id=(spool or {}).get("id", ""), job_id=job.get("id", ""),
                order_id=job.get("order_id") or "", auto=True, material=material,
                note=f"цвет: {color or '—'} ({material or 'материал не указан'})")
            out.append({**result, "material": material, "color": color})
        return out

    # ------------------------------------------- статистика расхода пластика
    def filament_stats(self, days: int = 30) -> dict[str, Any]:
        """Расход по материалам и цветам за период — для закупок и анализа."""
        since = (datetime.now() - timedelta(days=max(1, int(days)))).isoformat()
        rows = self.db.query(
            "SELECT f.*, s.material, s.color_name, s.brand FROM filament_usage f"
            " LEFT JOIN spools s ON s.id=f.spool_id WHERE f.at>=? ORDER BY f.at", (since,))
        by_mat: dict[str, dict[str, float]] = {}
        by_color: dict[str, dict[str, float]] = {}
        total_g = total_cost = 0.0
        for r in rows:
            material = str(r.get("material") or "—").upper()
            color = str(r.get("color_name") or "—")
            g, c = num(r.get("grams")), num(r.get("cost"))
            total_g += g
            total_cost += c
            m = by_mat.setdefault(material, {"grams": 0.0, "cost": 0.0, "uses": 0})
            m["grams"] += g
            m["cost"] += c
            m["uses"] += 1
            key = f"{material} · {color}"
            k = by_color.setdefault(key, {"material": material, "color": color,
                                          "grams": 0.0, "cost": 0.0, "uses": 0})
            k["grams"] += g
            k["cost"] += c
            k["uses"] += 1
        for d in (by_mat, by_color):
            for v in d.values():
                v["grams"] = round(v["grams"], 1)
                v["cost"] = round(v["cost"], 2)
        return {
            "days": int(days),
            "total_grams": round(total_g, 1),
            "total_cost": round(total_cost, 2),
            "by_material": sorted(by_mat.values(), key=lambda x: -x["grams"]),
            "by_color": sorted(by_color.values(), key=lambda x: -x["grams"]),
        }

    # ------------------------------------------------------------ история цен
    def record_price_history(self, order: dict) -> None:
        """Записать цену заказа в историю (для замера эластичности)."""
        try:
            price = round(num(order.get("price")), 2)
            if price <= 0:
                return
            self.db.execute(
                "INSERT INTO price_history(at,order_id,product,price,catalog_id)"
                " VALUES(?,?,?,?,?)",
                (now_iso(), order.get("id"), order.get("product") or "",
                 price, order.get("catalog_id") or ""))
        except Exception:
            pass

    def price_history(self, product: str = "", limit: int = 30) -> list[dict]:
        sql = "SELECT * FROM price_history WHERE 1=1"
        params: list[Any] = []
        if product:
            sql += " AND pylower(product)=?"
            params.append(product.lower())
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return self.db.query(sql, params)

    # ------------------------------------------------------------------ касса
    def add_transaction(self, kind: str, category: str, amount: float, title: str,
                        note: str = "", order_id: str = "", job_id: str = "",
                        auto: bool = False, account_id: str = "", channel: str = "",
                        payer: str = "", fee: float = 0.0, taxable: bool = True,
                        deductible: bool = True, customer_id: str = "",
                        fixed_cost_id: str = "", at: str = "") -> dict:
        if kind not in ("income", "expense"):
            raise ValueError("Тип проводки: income или expense")
        # сумма всегда положительная: знак задаёт вид проводки, иначе в списке «−−5 ₽»
        amount = abs(num(amount))
        if amount <= 0:
            raise ValueError("Сумма проводки должна быть больше нуля")
        # без статьи проводка выпадает из отчётов и показывается как «Без статьи»
        category = str(category or "").strip() or ("sale" if kind == "income" else "other")
        tx_id = uid("tx")
        at = at or now_iso()
        account_id = account_id or str(self.db.setting("default_account", "cash") or "")
        self.db.execute(
            "INSERT INTO transactions(id,at,kind,category,amount,title,note,order_id,job_id,auto,"
            "account_id,customer_id,channel,payer,fee,taxable,deductible,fixed_cost_id,period)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tx_id, at, kind, category, round(num(amount), 2), title, note,
             order_id or None, job_id or None, 1 if auto else 0,
             account_id or None, customer_id or None, channel, payer,
             round(num(fee), 2), 1 if taxable else 0, 1 if deductible else 0,
             fixed_cost_id or None, month_key(at)))
        row = self.db.one("SELECT * FROM transactions WHERE id=?", (tx_id,)) or {}
        # Конверты-накопления: отложить проценты с дохода (5.0).
        try:
            from .envelopes import auto_allocate
            auto_allocate(self.db, row)
        except Exception:
            pass
        return row

    def register_order_income(self, order: dict) -> dict | None:
        """При закрытии записать только ещё не полученный остаток как платёж."""
        if not self.db.setting("auto_income_on_done", True):
            return None
        rest = round(max(0.0, num(order.get("price")) -
                         max(num(order.get("paid")), num(order.get("prepaid")))), 2)
        if rest <= 0:
            return None
        payment = self.add_payment(
            order["id"], rest, "payment", order.get("account_id") or "",
            "автоматически", "Оплата при закрытии заказа")
        return self.db.one("SELECT * FROM transactions WHERE id=?",
                           (payment.get("tx_id") or "",))

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
            # Разбивка по цветам — это весь расход, а не добавка к общим граммам.
            # Сначала пробуем её и только при отсутствии разбивки списываем одну катушку.
            try:
                result["colors"] = self.consume_order_colors(job)
            except (TypeError, ValueError):
                result["colors"] = []
            if result["colors"]:
                result["filament"] = {
                    "ok": True, "multi_color": True,
                    "grams": round(sum(num(x.get("grams")) for x in result["colors"]), 1),
                    "cost": round(sum(num(x.get("cost")) for x in result["colors"]), 2),
                }
            else:
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
        # оплачено = paid (реестр платежей), prepaid — устаревшее поле старых заказов
        pipeline = sum(max(0.0, num(o["price"]) - max(num(o["paid"]), num(o["prepaid"])))
                       for o in active)
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

    # ------------------------------------------------------- постоянные расходы
    def run_fixed_costs(self, today: str = "") -> list[dict]:
        """Начисляет постоянные расходы за текущий период (идемпотентно).

        Каждый расход начисляется один раз в месяц/квартал/год: повторный вызов
        ничего не сделает, пока не наступит следующий период.
        """
        if not self.db.setting("fixed_costs_auto", True):
            return []
        stamp = today or date.today().isoformat()
        cur_month = month_key(stamp)
        day_now = int(stamp[8:10])
        created: list[dict] = []
        for row in self.db.query("SELECT * FROM fixed_costs WHERE active=1"):
            amount = num(row["amount"])
            if amount <= 0:
                continue
            if row["started_at"] and stamp < row["started_at"]:
                continue
            if day_now < max(1, int(num(row["day"], 1))):
                continue
            last = row["last_charged"] or ""
            if row["period"] == "month" and last >= cur_month:
                continue
            if row["period"] == "quarter" and last and quarter_key(last + "-01") == quarter_key(stamp):
                continue
            if row["period"] == "year" and last[:4] == cur_month[:4]:
                continue
            tx = self.add_transaction(
                "expense", row["category"] or "other", amount,
                row["name"], note="Постоянный расход (автоначисление)",
                auto=True, account_id=row["account_id"] or "",
                deductible=bool(row["deductible"]), fixed_cost_id=row["id"],
                at=f"{cur_month}-{max(1, int(num(row['day'], 1))):02d}T09:00:00")
            self.db.execute("UPDATE fixed_costs SET last_charged=? WHERE id=?",
                            (cur_month, row["id"]))
            created.append(tx)
        return created

    # -------------------------------------------------------------- P&L
    def pnl_month(self, key: str) -> dict[str, Any]:
        """Отчёт о прибылях и убытках за один месяц."""
        start, end = month_bounds(key)
        rows = self.db.query(
            "SELECT * FROM transactions WHERE at>=? AND at<?", (start, end))
        fixed_ids = {r["id"] for r in self.db.query(
            "SELECT id FROM expense_categories WHERE is_fixed=1")}
        income = sum(num(r["amount"]) for r in rows if r["kind"] == "income")
        fees = sum(num(r["fee"]) for r in rows if r["kind"] == "income")
        by_cat: dict[str, float] = {}
        expense = fixed = variable = taxes = owner = 0.0
        for r in rows:
            if r["kind"] != "expense":
                continue
            amount = num(r["amount"])
            cat = r["category"] or "other"
            by_cat[cat] = round(by_cat.get(cat, 0.0) + amount, 2)
            if cat in ("tax", "insurance"):
                taxes += amount
                continue
            if cat == "withdrawal":
                owner += amount   # вывод себе — не расход бизнеса
                continue
            expense += amount
            if cat in fixed_ids:
                fixed += amount
            else:
                variable += amount
        gross = income - fees - variable
        profit = income - fees - expense - taxes
        return {
            "key": key,
            "income": round(income, 2),
            "fees": round(fees, 2),
            "expense": round(expense, 2),
            "fixed": round(fixed, 2),
            "variable": round(variable, 2),
            "taxes": round(taxes, 2),
            "owner_draw": round(owner, 2),
            "gross_profit": round(gross, 2),
            "profit": round(profit, 2),
            "margin": round(profit / income * 100, 1) if income else 0.0,
            "by_category": by_cat,
        }

    def pnl(self, months: int = 6) -> dict[str, Any]:
        """P&L по месяцам плюс сравнение последнего месяца с предыдущим."""
        today = date.today()
        keys: list[str] = []
        year, mon = today.year, today.month
        for _ in range(max(1, months)):
            keys.append(f"{year}-{mon:02d}")
            mon -= 1
            if mon == 0:
                year, mon = year - 1, 12
        keys.reverse()
        rows = [self.pnl_month(k) for k in keys]
        cur = rows[-1] if rows else {}
        prev = rows[-2] if len(rows) > 1 else {}

        def delta(field: str) -> dict[str, float]:
            a, b = num(cur.get(field)), num(prev.get(field))
            return {"now": a, "was": b, "diff": round(a - b, 2),
                    "percent": round((a - b) / abs(b) * 100, 1) if b else 0.0}

        return {
            "months": rows,
            "current": cur,
            "previous": prev,
            "compare": {f: delta(f) for f in ("income", "expense", "profit", "margin")},
            "average_profit": round(sum(r["profit"] for r in rows) / len(rows), 2) if rows else 0.0,
        }

    # -------------------------------------------------------------- налоги
    def tax_report(self, year: int = 0) -> dict[str, Any]:
        """Налоговая картина года: база, начисления, взносы, резерв, лимиты."""
        s = self.db.settings()
        year = year or date.today().year
        mode = s.get("tax_mode", "none")
        rows = self.db.query(
            "SELECT * FROM transactions WHERE at>=? AND at<?",
            (f"{year}-01-01", f"{year + 1}-01-01"))
        gross_person = sum(num(r["amount"]) for r in rows
                           if r["kind"] == "income" and r["taxable"] and r["payer"] != "company")
        gross_company = sum(num(r["amount"]) for r in rows
                            if r["kind"] == "income" and r["taxable"] and r["payer"] == "company")
        refund_person = sum(num(r["amount"]) for r in rows
                            if r["kind"] == "expense" and r["category"] == "refund"
                            and r["payer"] != "company")
        refund_company = sum(num(r["amount"]) for r in rows
                             if r["kind"] == "expense" and r["category"] == "refund"
                             and r["payer"] == "company")
        income_person = max(0.0, gross_person - refund_person)
        income_company = max(0.0, gross_company - refund_company)
        income = income_person + income_company
        expense = sum(num(r["amount"]) for r in rows
                      if r["kind"] == "expense" and r["deductible"]
                      and r["category"] not in ("tax", "withdrawal", "refund"))
        tax_paid = sum(num(r["amount"]) for r in rows
                       if r["kind"] == "expense" and r["category"] == "tax")
        insurance_paid = sum(num(r["amount"]) for r in rows
                             if r["kind"] == "expense" and r["category"] == "insurance")

        # страховые взносы ИП «за себя»
        fixed_due = num(s.get("insurance_fixed")) if mode in ("usn6", "usn15", "patent") else 0.0
        base = income if mode != "usn15" else max(0.0, income - expense)
        extra_due = 0.0
        if fixed_due:
            over = max(0.0, base - num(s.get("insurance_extra_base"), 300000))
            extra_due = min(over * num(s.get("insurance_extra_rate"), 1) / 100.0,
                            num(s.get("insurance_extra_cap"), 321818))
        insurance_due = round(fixed_due + extra_due, 2)

        tax_due = 0.0
        notes: list[str] = []
        if mode == "npd":
            rate_p = num(s.get("npd_rate_person"), 4)
            rate_c = num(s.get("npd_rate_company"), 6)
            raw = income_person * rate_p / 100.0 + income_company * rate_c / 100.0
            bonus_left = num(s.get("npd_bonus_left"))
            # вычет гасит 1 п.п. со ставки 4% и 2 п.п. со ставки 6%
            bonus_use = min(bonus_left,
                            income_person * 1 / 100.0 + income_company * 2 / 100.0)
            tax_due = max(0.0, raw - bonus_use)
            if bonus_use:
                notes.append(f"Налоговый вычет уменьшил налог на {rub(bonus_use)}")
        elif mode == "usn6":
            raw = income * num(s.get("usn_income_rate"), 6) / 100.0
            if s.get("insurance_reduces_tax"):
                tax_due = max(0.0, raw - insurance_due)
                notes.append("Налог уменьшен на страховые взносы (ИП без сотрудников — до нуля)")
            else:
                tax_due = raw
        elif mode == "usn15":
            profit_base = max(0.0, income - expense - insurance_due)
            raw = profit_base * num(s.get("usn_profit_rate"), 15) / 100.0
            minimal = income * num(s.get("usn_min_tax_rate"), 1) / 100.0
            tax_due = max(raw, minimal)
            if minimal > raw:
                notes.append("Считается минимальный налог 1% с дохода — он выше обычного")
        elif mode == "patent":
            tax_due = num(s.get("patent_cost_year"))
        elif mode == "manual":
            tax_due = income * num(s.get("tax_rate")) / 100.0

        # лимиты режима
        limit = num(s.get("npd_limit"), 2400000) if mode == "npd" else \
            (num(s.get("usn_limit")) if mode in ("usn6", "usn15") else 0.0)
        limit_used = round(income / limit * 100, 1) if limit else 0.0
        if limit and limit_used >= 80:
            notes.append(f"Доход занял {limit_used}% годового лимита режима")
        vat_threshold = num(s.get("vat_threshold"), 20000000)
        if mode in ("usn6", "usn15") and income >= vat_threshold * 0.8:
            notes.append("Доход подходит к порогу НДС 20 млн ₽ — пора считать НДС 5%")

        due_total = round(tax_due + insurance_due - tax_paid - insurance_paid, 2)
        # Резерв — это то, что ещё предстоит заплатить, а не вся годовая сумма.
        reserve = max(0.0, due_total) + num(s.get("tax_reserve_extra")) * income / 100.0
        reserve_rate = reserve / income * 100 if income else 0.0
        if insurance_due and not insurance_paid:
            notes.append(
                f"Страховые взносы за год — {rub(insurance_due)}, срок уплаты 28 декабря")
        return {
            "year": year,
            "mode": mode,
            "mode_name": TAX_MODES.get(mode, mode),
            "income": round(income, 2),
            "income_person": round(income_person, 2),
            "income_company": income_company,
            "expense": round(expense, 2),
            "tax_due": round(tax_due, 2),
            "tax_paid": round(tax_paid, 2),
            "insurance_due": insurance_due,
            "insurance_paid": round(insurance_paid, 2),
            "total_due": max(0.0, due_total),
            "reserve": round(reserve, 2),
            "reserve_rate": round(reserve_rate, 1),
            "limit": limit,
            "limit_used": limit_used,
            "quarters": self._tax_quarters(year, mode),
            "notes": notes,
        }

    def _tax_quarters(self, year: int, mode: str) -> list[dict]:
        out = []
        npd_bonus_left = num(self.db.setting("npd_bonus_left"))
        for q in range(1, 5):
            start_m = (q - 1) * 3 + 1
            start = f"{year}-{start_m:02d}-01"
            end = f"{year + 1}-01-01" if q == 4 else f"{year}-{start_m + 3:02d}-01"
            rows = self.db.query(
                "SELECT kind,amount,taxable,deductible,category,payer FROM transactions"
                " WHERE at>=? AND at<?", (start, end))
            gross_person = sum(num(r["amount"]) for r in rows
                               if r["kind"] == "income" and r["taxable"]
                               and r["payer"] != "company")
            gross_company = sum(num(r["amount"]) for r in rows
                                if r["kind"] == "income" and r["taxable"]
                                and r["payer"] == "company")
            refund_person = sum(num(r["amount"]) for r in rows
                                if r["kind"] == "expense" and r["category"] == "refund"
                                and r["payer"] != "company")
            refund_company = sum(num(r["amount"]) for r in rows
                                 if r["kind"] == "expense" and r["category"] == "refund"
                                 and r["payer"] == "company")
            income_person = max(0.0, gross_person - refund_person)
            income_company = max(0.0, gross_company - refund_company)
            inc = income_person + income_company
            exp = sum(num(r["amount"]) for r in rows if r["kind"] == "expense"
                      and r["deductible"]
                      and r["category"] not in ("tax", "withdrawal", "refund"))
            if mode == "usn15":
                due = max(0.0, inc - exp) * num(self.db.setting("usn_profit_rate", 15)) / 100.0
            elif mode == "usn6":
                due = inc * num(self.db.setting("usn_income_rate", 6)) / 100.0
            elif mode == "npd":
                due = (income_person * num(self.db.setting("npd_rate_person", 4)) / 100.0 +
                       income_company * num(self.db.setting("npd_rate_company", 6)) / 100.0)
                bonus_use = min(npd_bonus_left,
                                income_person / 100.0 + income_company * 2 / 100.0)
                due -= bonus_use
                npd_bonus_left -= bonus_use
            else:
                due = 0.0
            out.append({"key": f"{year}-Q{q}", "income": round(inc, 2),
                        "expense": round(exp, 2), "tax": round(max(0.0, due), 2)})
        return out

    # -------------------------------------------------------------- кассы и долги
    def accounts_state(self) -> dict[str, Any]:
        """Остатки по кассам: старт + доходы − расходы."""
        rows = []
        total = 0.0
        for acc in self.db.query("SELECT * FROM accounts WHERE archived=0 ORDER BY position, name"):
            agg = self.db.one(
                "SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount-COALESCE(fee,0)"
                " ELSE -amount END),0) AS delta, COUNT(*) AS n"
                " FROM transactions WHERE account_id=?", (acc["id"],)) or {}
            balance = num(acc["opening_balance"]) + num(agg.get("delta"))
            total += balance
            rows.append({**acc, "balance": round(balance, 2), "moves": int(num(agg.get("n")))})
        return {"accounts": rows, "total": round(total, 2)}

    def debts(self) -> dict[str, Any]:
        """Долги клиентов и просрочка."""
        finals = {r["id"] for r in self.db.query("SELECT id FROM statuses WHERE is_final=1")}
        alert_days = int(num(self.db.setting("debt_alert_days", 14), 14))
        today = date.today()
        rows = []
        for o in self.db.query("SELECT * FROM orders ORDER BY created_at DESC"):
            eco = self.order_economics(o)
            if eco["debt"] <= 0.5:
                continue
            created = (o.get("created_at") or "")[:10]
            try:
                days = (today - date.fromisoformat(created)).days if created else 0
            except ValueError:
                days = 0
            rows.append({
                "id": o["id"], "number": o.get("number"), "customer": o.get("customer_name"),
                "product": o.get("product"), "price": eco["price"], "paid": eco["paid"],
                "debt": eco["debt"], "days": days, "overdue": days > alert_days,
                "done": o.get("status") in finals, "phone": o.get("phone"),
            })
        rows.sort(key=lambda r: (-r["debt"]))
        return {
            "rows": rows,
            "total": round(sum(r["debt"] for r in rows), 2),
            "overdue": round(sum(r["debt"] for r in rows if r["overdue"]), 2),
            "count": len(rows),
        }

    def add_payment(self, order_id: str, amount: float, kind: str = "payment",
                    account_id: str = "", method: str = "", note: str = "") -> dict:
        """Атомарно записать платёж, кассовую проводку и новый остаток долга."""
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            raise ValueError("Заказ не найден")
        if kind not in ("prepay", "payment", "refund"):
            raise ValueError("Тип платежа: предоплата, оплата или возврат")
        amount = round(num(amount), 2)
        if amount <= 0:
            raise ValueError("Сумма платежа должна быть больше нуля")
        if kind == "refund" and amount > max(num(order.get("paid")), num(order.get("prepaid"))):
            raise ValueError("Возврат не может быть больше полученной суммы")
        acc = self.db.one("SELECT * FROM accounts WHERE id=?",
                          (account_id or self.db.setting("default_account", "cash"),)) or {}
        if not acc:
            raise ValueError("Касса не найдена")
        fee = 0.0 if kind == "refund" else round(amount * num(acc.get("fee_percent")) / 100.0, 2)
        stamp, pay_id = now_iso(), uid("pay")
        title = (("Возврат" if kind == "refund" else
                  "Предоплата" if kind == "prepay" else "Оплата") +
                 f" · заказ №{order.get('number', '')}")
        with self.db.transaction():
            tx = self.add_transaction(
                "expense" if kind == "refund" else "income",
                "refund" if kind == "refund" else ("prepay" if kind == "prepay" else "order"),
                amount, title, note=note, order_id=order_id, auto=(method == "автоматически"),
                account_id=acc["id"], customer_id=order.get("customer_id") or "",
                channel=order.get("channel") or "", payer=order.get("payer") or "person",
                fee=fee, taxable=kind != "refund", deductible=kind == "refund", at=stamp)
            self.db.execute(
                "INSERT INTO payments(id,at,order_id,customer_id,amount,kind,account_id,method,fee,note,tx_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (pay_id, stamp, order_id, order.get("customer_id"), amount, kind,
                 acc["id"], method, fee, note, tx["id"]))
            sign = -1 if kind == "refund" else 1
            self.db.execute(
                "UPDATE orders SET paid=MAX(0,COALESCE(paid,0)+?), updated_at=? WHERE id=?",
                (sign * amount, stamp, order_id))
        return self.db.one("SELECT * FROM payments WHERE id=?", (pay_id,)) or {}

    # -------------------------------------------------------------- безубыточность
    def break_even(self) -> dict[str, Any]:
        """Точка безубыточности в деньгах, заказах и часах печати."""
        s = self.db.settings()
        fixed = self.fixed_costs_monthly()
        insurance_month = 0.0
        if s.get("tax_mode") in ("usn6", "usn15", "patent"):
            insurance_month = num(s.get("insurance_fixed")) / 12.0
        fixed_total = fixed + insurance_month

        stats = self.db.one(
            "SELECT COUNT(*) AS n, COALESCE(AVG(price),0) AS avg_price FROM orders"
            " WHERE price>0") or {}
        orders_n = int(num(stats.get("n")))
        avg_price = num(stats.get("avg_price"))
        margins = [self.order_economics(o)["margin"] for o in
                   self.db.query("SELECT * FROM orders WHERE price>0 LIMIT 200")]
        margin = round(sum(margins) / len(margins), 1) if margins else 0.0
        if margin <= 0:
            markup = num(s.get("default_markup"), 150)
            margin = round(markup / (100 + markup) * 100, 1)

        revenue_needed = round(fixed_total / (margin / 100.0), 2) if margin > 0 else 0.0
        orders_needed = round(revenue_needed / avg_price, 1) if avg_price else 0.0
        goal = num(s.get("goal_profit_month"))
        revenue_goal = round((fixed_total + goal) / (margin / 100.0), 2) if margin > 0 else 0.0
        cur = self.pnl_month(month_key())
        return {
            "fixed_monthly": round(fixed, 2),
            "insurance_monthly": round(insurance_month, 2),
            "fixed_total": round(fixed_total, 2),
            "avg_price": round(avg_price, 2),
            "avg_margin": margin,
            "orders_base": orders_n,
            "revenue_needed": revenue_needed,
            "orders_needed": orders_needed,
            "goal_profit": goal,
            "revenue_goal": revenue_goal,
            "orders_goal": round(revenue_goal / avg_price, 1) if avg_price else 0.0,
            "income_now": cur.get("income", 0.0),
            "progress": round(cur.get("income", 0.0) / revenue_needed * 100, 1) if revenue_needed else 0.0,
            "hours_needed": round(revenue_needed / num(s.get("target_profit_per_hour"), 250), 1)
            if num(s.get("target_profit_per_hour")) else 0.0,
        }

    # ------------------------------------------------------------- ABC-анализ
    def abc_report(self, days: int = 30) -> dict[str, Any]:
        """ABC-анализ изделий: топ по выручке и прибыли за период.

        A — первые ~80% выручки (ядро), B — следующие ~15%, C — хвост ~5%.
        По нему видно, что масштабировать, а что снять с полки.
        """
        since = (datetime.now() - timedelta(days=max(1, int(days)))).isoformat()
        rows: dict[str, dict[str, float]] = {}
        for o in self.db.query(
                "SELECT * FROM orders WHERE created_at>=? AND created_at<?",
                (since, datetime.now().isoformat())):
            eco = self.order_economics(o)
            key = (o.get("product") or "Без названия").strip()
            item = rows.setdefault(key, {"name": key, "qty": 0.0, "revenue": 0.0,
                                         "profit": 0.0, "orders": 0})
            item["qty"] += num(o.get("qty"), 1)
            item["revenue"] += eco["price"]
            item["profit"] += eco["profit"]
            item["orders"] += 1
        items = sorted(rows.values(), key=lambda x: -x["revenue"])
        total = sum(i["revenue"] for i in items) or 1.0
        acc = 0.0
        for index, i in enumerate(items):
            acc += i["revenue"]
            share = acc / total
            i["revenue"] = round(i["revenue"], 2)
            i["profit"] = round(i["profit"], 2)
            i["qty"] = int(i["qty"])
            i["share"] = round(i["revenue"] / total * 100, 1)
            # первый элемент — всегда ядро (A), даже если он один покрыл всё
            if index == 0 or share <= 0.8:
                i["cls"] = "A"
            elif share <= 0.95:
                i["cls"] = "B"
            else:
                i["cls"] = "C"
        return {"days": int(days), "total_revenue": round(total, 2),
                "items": items,
                "a_share": round(sum(i["revenue"] for i in items if i["cls"] == "A") / total * 100, 1),
                "b_share": round(sum(i["revenue"] for i in items if i["cls"] == "B") / total * 100, 1),
                "c_share": round(sum(i["revenue"] for i in items if i["cls"] == "C") / total * 100, 1)}

    # -------------------------------------------------------------- отчёты
    def period_bounds(self, period: str = "month", offset: int = 0) -> tuple[str, str, str]:
        """Границы отчётного периода: (начало, конец, подпись)."""
        today = date.today()
        if period == "year":
            year = today.year - offset
            return f"{year}-01-01", f"{year + 1}-01-01", str(year)
        if period == "quarter":
            q = (today.month - 1) // 3 - offset
            year = today.year + (q // 4)
            q = q % 4
            start_m = q * 3 + 1
            end = f"{year + 1}-01-01" if start_m == 10 else f"{year}-{start_m + 3:02d}-01"
            return f"{year}-{start_m:02d}-01", end, f"{year} · Q{q + 1}"
        year, mon = today.year, today.month - offset
        while mon <= 0:
            year, mon = year - 1, mon + 12
        start, end = month_bounds(f"{year}-{mon:02d}")
        return start, end, f"{year}-{mon:02d}"

    def report(self, period: str = "month", offset: int = 0) -> dict[str, Any]:
        """Сводный отчёт: топ клиентов, товаров, каналов и структура расходов."""
        start, end, label = self.period_bounds(period, offset)
        orders = self.db.query(
            "SELECT * FROM orders WHERE created_at>=? AND created_at<?", (start, end))
        txs = self.db.query(
            "SELECT * FROM transactions WHERE at>=? AND at<?", (start, end))

        customers: dict[str, dict] = {}
        products: dict[str, dict] = {}
        channels: dict[str, dict] = {}
        revenue = profit = hours = grams = 0.0
        for o in orders:
            eco = self.order_economics(o)
            revenue += eco["price"]
            profit += eco["profit"]
            hours += eco["hours"]
            grams += eco["grams"]
            key = o.get("customer_name") or o.get("phone") or "Без имени"
            c = customers.setdefault(key, {"name": key, "orders": 0, "revenue": 0.0, "profit": 0.0})
            c["orders"] += 1
            c["revenue"] = round(c["revenue"] + eco["price"], 2)
            c["profit"] = round(c["profit"] + eco["profit"], 2)
            pkey = (o.get("product") or "Без названия").strip()
            p = products.setdefault(pkey, {"name": pkey, "qty": 0, "revenue": 0.0,
                                           "profit": 0.0, "hours": 0.0})
            p["qty"] += int(num(o.get("qty"), 1))
            p["revenue"] = round(p["revenue"] + eco["price"], 2)
            p["profit"] = round(p["profit"] + eco["profit"], 2)
            p["hours"] = round(p["hours"] + eco["hours"], 2)
            chkey = o.get("channel") or "direct"
            ch = channels.setdefault(chkey, {"id": chkey, "name": chkey, "orders": 0,
                                             "revenue": 0.0, "profit": 0.0, "fee": 0.0})
            ch["orders"] += 1
            ch["revenue"] = round(ch["revenue"] + eco["price"], 2)
            ch["profit"] = round(ch["profit"] + eco["profit"], 2)
            ch["fee"] = round(ch["fee"] + eco["fee"], 2)
        names = {c["id"]: c["name"] for c in self.db.query("SELECT id,name FROM channels")}
        for key, row in channels.items():
            row["name"] = names.get(key, key or "Напрямую")

        cats = {c["id"]: c["name"] for c in self.db.query("SELECT id,name FROM expense_categories")}
        by_cat: dict[str, float] = {}
        for t in txs:
            if t["kind"] == "expense":
                label_cat = cats.get(t["category"], t["category"] or "Прочее")
                by_cat[label_cat] = round(by_cat.get(label_cat, 0.0) + num(t["amount"]), 2)

        income_tx = sum(num(t["amount"]) for t in txs if t["kind"] == "income")
        expense_tx = sum(num(t["amount"]) for t in txs if t["kind"] == "expense")
        top = lambda d, field: sorted(d.values(), key=lambda r: -r[field])[:10]  # noqa: E731
        return {
            "period": period, "label": label, "start": start, "end": end,
            "orders": len(orders),
            "revenue": round(revenue, 2),
            "profit": round(profit, 2),
            "margin": round(profit / revenue * 100, 1) if revenue else 0.0,
            "hours": round(hours, 1),
            "grams": round(grams, 1),
            "avg_check": round(revenue / len(orders), 2) if orders else 0.0,
            "cash_in": round(income_tx, 2),
            "cash_out": round(expense_tx, 2),
            "cash_flow": round(income_tx - expense_tx, 2),
            "customers": top(customers, "revenue"),
            "products": top(products, "profit"),
            "channels": sorted(channels.values(), key=lambda r: -r["revenue"]),
            "expenses": sorted(({"name": k, "amount": v} for k, v in by_cat.items()),
                               key=lambda r: -r["amount"]),
        }

    def report_csv(self, period: str = "month", offset: int = 0) -> str:
        """Отчёт в CSV (разделитель ';' — Excel на русской локали открывает сразу)."""
        rep = self.report(period, offset)
        lines = [f"PrintFlow · отчёт за {rep['label']}", ""]
        lines.append("Показатель;Значение")
        for key, title in (("orders", "Заказов"), ("revenue", "Выручка"),
                           ("profit", "Прибыль"), ("margin", "Маржа, %"),
                           ("avg_check", "Средний чек"), ("hours", "Часы печати"),
                           ("grams", "Пластик, г"), ("cash_in", "Поступления"),
                           ("cash_out", "Списания"), ("cash_flow", "Денежный поток")):
            lines.append(f"{title};{rep[key]}")
        lines += ["", "Клиент;Заказов;Выручка;Прибыль"]
        for c in rep["customers"]:
            lines.append(f"{c['name']};{c['orders']};{c['revenue']};{c['profit']}")
        lines += ["", "Товар;Штук;Выручка;Прибыль;Часы"]
        for p in rep["products"]:
            lines.append(f"{p['name']};{p['qty']};{p['revenue']};{p['profit']};{p['hours']}")
        lines += ["", "Канал;Заказов;Выручка;Комиссия;Прибыль"]
        for c in rep["channels"]:
            lines.append(f"{c['name']};{c['orders']};{c['revenue']};{c['fee']};{c['profit']}")
        lines += ["", "Статья расходов;Сумма"]
        for e in rep["expenses"]:
            lines.append(f"{e['name']};{e['amount']}")
        return "\r\n".join(lines)

    def transactions_csv(self, days: int = 365) -> str:
        since = (datetime.now() - timedelta(days=max(1, days))).isoformat()
        rows = self.db.query(
            "SELECT * FROM transactions WHERE at>=? ORDER BY at DESC", (since,))
        cats = {c["id"]: c["name"] for c in self.db.query("SELECT id,name FROM expense_categories")}
        cats.setdefault("order", "Заказ")
        cats.setdefault("sale", "Продажа")
        out = ["Дата;Тип;Статья;Сумма;Комиссия;Назначение;Касса;Заметка"]
        for r in rows:
            kind = "Доход" if r["kind"] == "income" else "Расход"
            out.append(";".join(str(x).replace(";", ",") for x in (
                (r["at"] or "")[:16].replace("T", " "), kind,
                cats.get(r["category"], r["category"] or ""),
                num(r["amount"]), num(r["fee"]), r["title"] or "",
                r["account_id"] or "", (r["note"] or "").replace("\n", " "))))
        return "\r\n".join(out)

    def money_state(self, months: int = 6) -> dict[str, Any]:
        """Единый пакет для вкладки «Финансы»: P&L, налоги, кассы, долги, ТБУ."""
        self.run_fixed_costs()
        return {
            "pnl": self.pnl(months),
            "tax": self.tax_report(),
            "accounts": self.accounts_state(),
            "debts": self.debts(),
            "break_even": self.break_even(),
            "fixed_costs": self.db.query(
                "SELECT * FROM fixed_costs ORDER BY active DESC, name"),
            "channels": self.db.query("SELECT * FROM channels ORDER BY position, name"),
            "categories": self.db.query(
                "SELECT * FROM expense_categories ORDER BY position, name"),
        }

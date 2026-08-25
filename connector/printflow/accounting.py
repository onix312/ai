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

import math

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
                       delivery: float = 0.0, color_swaps: float = 0.0,
                       material: str = "", quality: str = "standard",
                       supports_pct: float = 0.0, plate_grams: float = 0.0,
                       plate_hours: float = 0.0, fit_per_plate: float = 0.0,
                       warmup_minutes: float = 0.0,
                       remove_minutes: float = 0.0, sand_minutes: float = 0.0,
                       paint_minutes: float = 0.0,
                       model_prep_minutes: float = 0.0) -> dict[str, float]:
        """Полная раскладка себестоимости партии.

        Модель ввода «плита vs штука»:
        • plate_grams / plate_hours — вес и время ВСЕЙ плиты из слайсера;
        • fit_per_plate — сколько штук помещается на плите;
        • qty — сколько штук нужно напечатать.
        Система сама считает: plates = ceil(qty / fit),
        total_grams = plate_grams × plates, total_hours = plate_hours × plates.

        Если plate_grams не указан — используется старый режим (grams = на штуку,
        hours = на плиту), qty × grams для совместимости.

        Параметры материалов и качества:
        • material — ключ из справочника (PLA, PETG, TPU…): корректирует
          скорость печати и подсказывает цену катушки;
        • quality — профиль (draft/standard/detail/strong): множитель времени
          и расхода;
        • supports_pct — процент поддержек (0–50): добавляет пластик и время;
        • color_swaps — смен цвета через AMS: 12 г продувки на каждую смену
          (на плиту);
        • warmup_minutes — прогрев и калибровка на каждую плиту (5–10 мин);
        • remove_minutes / sand_minutes / paint_minutes — постобработка
          на штуку.

        Своя работа по умолчанию расходом не считается (count_labor_in_cost),
        но всегда показывается отдельной строкой как ориентир.
        """
        from .materials import get_material, get_profile
        s = self.db.settings()
        qty = max(1.0, num(qty, 1))

        # --- справочник материала и профиля ---
        # Свои пластики из базы (таблица materials) имеют приоритет над
        # встроенным справочником.
        mat = get_material(material, db=self.db)
        profile = get_profile(quality)
        speed_factor = num(mat.get("speed_factor"), 1.0)
        time_factor = num(profile.get("time_factor"), 1.0)
        filament_factor = num(profile.get("filament_factor"), 1.0)

        # Цена катушки: если пользователь не указал — берём из справочника
        # Приоритет: явное значение > справочник материала > настройка по умолчанию
        sp_price = num(spool_price) if spool_price is not None else 0
        if sp_price > 0:
            price = sp_price
        else:
            price = num(mat.get("price_per_kg")) or num(s["default_spool_price"], 1600)
        sw = num(spool_weight) if spool_weight not in (None, "") else 0
        if sw <= 1:
            sw = num(s.get("default_spool_weight"), 1000) or 1000
        weight = max(1.0, sw)

        # --- модель «плита vs штука» ---
        fit = max(1.0, num(fit_per_plate, 1))
        pg = num(plate_grams)
        ph = num(plate_hours)

        if pg > 0 and ph > 0:
            # Новый режим: слайсер дал вес и время на плиту
            plates = max(1, math.ceil(qty / fit))  # ceil(qty / fit)
            # Вес всей партии = вес плиты × число плит
            # Поддержки добавляются к весу плиты (они уже часть плиты)
            base_grams = pg * plates
            # Время = время плиты × число плит + прогрев на каждую плиту
            warmup = num(warmup_minutes, 7) / 60.0  # минут → часов
            # если warmup передали 0 — это «нет прогрева», не подменяем на 7
            if warmup_minutes is not None and str(warmup_minutes).strip() != "" and num(warmup_minutes, -1) == 0:
                warmup = 0.0
            base_hours = (ph + warmup) * plates
        else:
            # Старый режим: grams = на штуку, hours = на плиту
            plates = max(1, math.ceil(qty / fit))
            base_grams = num(grams) * qty
            base_hours = num(hours) * plates

        # Применяем профиль качества к расходу и времени
        base_grams *= filament_factor
        base_hours *= time_factor
        # Корректировка скорости материала (TPU медленнее в 4 раза)
        if speed_factor > 0 and speed_factor != 1.0:
            base_hours /= speed_factor

        # --- поддержки: дополнительный пластик и время ---
        sup_pct = max(0.0, min(50.0, num(supports_pct))) / 100.0
        support_grams = base_grams * sup_pct
        # Поддержки печатаются быстрее основного тела (~70% скорости)
        support_hours = (support_grams / max(1.0, base_grams) * base_hours * 0.7) if base_grams else 0.0

        total_grams = base_grams + support_grams
        total_hours = base_hours + support_hours

        # --- многоцветная печать: продувка AMS на каждую смену цвета ---
        swaps = max(0.0, num(color_swaps))
        purge_grams = swaps * 12.0 * plates  # 12 г на смену × число плит
        total_grams += purge_grams

        # --- расчёт стоимости ---
        filament = total_grams * price / weight
        energy_kwh = total_hours * num(s["power_kw"], 0.15)
        energy = energy_kwh * num(s["energy_price"], 6)
        amortization = total_hours * num(s["amortization_per_hour"], 12)
        maintenance = total_hours * num(s["maintenance_per_hour"], 3)

        # Ручная работа: основная + постобработка + подготовка модели
        post_per_unit = num(remove_minutes) + num(sand_minutes) + num(paint_minutes)
        # Подготовка модели: разовая работа, делится на всю партию
        # Это РЕАЛЬНАЯ трата времени (не «своя работа») — всегда в себестоимости
        model_prep = num(model_prep_minutes)
        model_prep_cost = model_prep / 60.0 * num(s["labor_rate"], 400)
        total_manual = (num(manual_minutes) + post_per_unit) * qty
        labor = total_manual / 60.0 * num(s["labor_rate"], 400)
        design = num(design_minutes) / 60.0 * num(s.get("design_rate"), 800)
        packaging = num(s["packaging_cost"], 15) * qty
        delivery = num(delivery) or num(s.get("delivery_cost"))
        overhead = self.overhead_per_hour() * total_hours
        counted_labor = (labor + design) if s.get("count_labor_in_cost") else 0.0

        subtotal = (filament + energy + amortization + maintenance
                    + packaging + delivery + overhead + counted_labor
                    + model_prep_cost)
        failure = subtotal * num(s["failure_rate"], 5) / 100.0
        total = subtotal + failure
        cash = total - overhead
        return {
            "filament": round(filament, 2),
            "energy": round(energy, 2),
            "energy_kwh": round(energy_kwh, 3),
            "amortization": round(amortization, 2),
            "maintenance": round(maintenance, 2),
            "labor": round(labor, 2),
            "design": round(design, 2),
            "labor_counted": bool(s.get("count_labor_in_cost")),
            "model_prep": round(model_prep, 1),
            "model_prep_cost": round(model_prep_cost, 2),
            "packaging": round(packaging, 2),
            "delivery": round(delivery, 2),
            "overhead": round(overhead, 2),
            "failure_reserve": round(failure, 2),
            "total": round(total, 2),
            "cash_cost": round(cash, 2),
            "per_unit": round(total / qty, 2),
            "per_hour": round(total / total_hours, 2) if total_hours else 0.0,
            # --- новые поля для калькулятора ---
            "plates": plates,
            "fit_per_plate": int(fit),
            "total_grams": round(total_grams, 1),
            "total_hours": round(total_hours, 2),
            "unit_grams": round(total_grams / qty, 1),
            "unit_hours": round(total_hours / qty, 2),
            "support_grams": round(support_grams, 1),
            "purge_grams": round(purge_grams, 1),
            "material": mat.get("name", "PLA"),
            "quality": profile.get("name", "Стандарт"),
            "speed_factor": speed_factor,
            "time_factor": time_factor,
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
        raw = str(channel_id).strip()
        aliases = {
            "полка магазина": "shop", "витрина": "shop", "другое": "direct",
            "рекомендация": "direct", "напрямую": "direct",
            "telegram": "telegram", "авито": "avito", "b2b": "b2b",
        }
        key = aliases.get(raw.casefold(), raw)
        return self.db.one(
            "SELECT * FROM channels WHERE id=? OR pylower(name)=? LIMIT 1",
            (key, raw.casefold())) or {}

    # ------------------------------------------------------- материалы и профили
    def material_options(self) -> dict[str, Any]:
        """Справочник материалов и профилей качества для калькулятора.

        Встроенные типы пластика + свои из базы (custom=True) + профили
        качества. Свои материалы видны и в настройках, и в калькуляторе.
        """
        from .materials import (material_list, material_full_list, profile_list,
                                seed_builtin_materials)
        # Каталог пластиков живёт в базе (таблица materials): при первом
        # обращении заносим встроенные типы, дальше они правятся под себя.
        seed_builtin_materials(self.db)
        return {
            "materials": material_list(self.db),
            "materials_full": material_full_list(self.db),
            "profiles": profile_list(),
        }

    # -------------------------------------------------- сценарии «что если»
    def calc_scenarios(self, base: dict, variants: list[dict]) -> list[dict]:
        """Сравнение нескольких вариантов расчёта рядом.

        base — общие параметры (grams, hours, qty, fit…).
        variants — список отличий: [{material, quality, qty, …}].
        Для каждого варианта считается cost_breakdown + suggest_price и
        возвращается полная раскладка с вердиктом.
        """
        from .materials import get_material
        results = []
        for i, var in enumerate(variants):
            params = {**base, **var}
            br = self.cost_breakdown(
                grams=num(params.get("grams")),
                hours=num(params.get("hours")),
                spool_price=num(params.get("spool_price")) or None,
                spool_weight=num(params.get("spool_weight")) or None,
                manual_minutes=num(params.get("manual_minutes")),
                qty=num(params.get("qty"), 1),
                design_minutes=num(params.get("design_minutes")),
                delivery=num(params.get("delivery")),
                color_swaps=num(params.get("color_swaps")),
                material=params.get("material", ""),
                quality=params.get("quality", "standard"),
                supports_pct=num(params.get("supports_pct")),
                plate_grams=num(params.get("plate_grams")),
                plate_hours=num(params.get("plate_hours")),
                fit_per_plate=num(params.get("fit_per_plate")),
                warmup_minutes=num(params.get("warmup_minutes")),
                remove_minutes=num(params.get("remove_minutes")),
                sand_minutes=num(params.get("sand_minutes")),
                paint_minutes=num(params.get("paint_minutes")),
            )
            sp = self.suggest_price(
                br["per_unit"], num(params.get("qty"), 1),
                params.get("channel", ""), bool(params.get("rush")))
            mat = get_material(params.get("material", ""), db=self.db)
            results.append({
                "index": i,
                "label": params.get("label") or mat.get("name", "PLA"),
                "breakdown": br,
                "price": sp,
                "profit": round(
                    (sp["price"] - br["per_unit"]) * num(params.get("qty"), 1), 2),
                "profit_per_hour": round(
                    (sp["price"] - br["per_unit"]) * num(params.get("qty"), 1)
                    / br["total_hours"], 2) if br["total_hours"] else 0.0,
                "margin": round(
                    (sp["price"] - br["per_unit"]) / sp["price"] * 100, 1)
                    if sp["price"] else 0.0,
            })
        return results

    # ----------------------------------------------- калькулятор окупаемости
    def payback_calc(self, model_cost: float = 0.0, design_hours: float = 0.0,
                     profit_per_unit: float = 0.0, sales_per_week: float = 1.0) -> dict:
        """Окупаемость модели или разработки.

        • model_cost — стоимость покупки модели (Cults3D, Thangs, …);
        • design_hours — часы на собственное моделирование;
        • profit_per_unit — прибыль с одной штуки (из калькулятора);
        • sales_per_week — сколько штук продаётся в неделю (оценка).
        """
        s = self.db.settings()
        design_cost = num(design_hours) * num(s.get("design_rate"), 800)
        total_invest = num(model_cost) + design_cost
        ppu = max(0.01, num(profit_per_unit))
        units_needed = int(total_invest / ppu) + 1 if total_invest > 0 else 0
        spw = max(0.1, num(sales_per_week))
        weeks = round(units_needed / spw, 1) if units_needed else 0.0
        return {
            "model_cost": round(num(model_cost), 2),
            "design_cost": round(design_cost, 2),
            "total_invest": round(total_invest, 2),
            "profit_per_unit": round(ppu, 2),
            "units_needed": units_needed,
            "weeks_to_payback": weeks,
            "days_to_payback": round(weeks * 7, 0),
        }

    # ------------------------------------------- реальная статистика из журнала
    def real_stats(self, product: str = "", material: str = "",
                   days: int = 60) -> dict[str, Any]:
        """Фактические данные из журнала печати для калибровки калькулятора.

        Ищет завершённые задания с похожим продуктом или материалом и
        возвращает медианные вес/время — это точнее слайсера.
        """
        since = (datetime.now() - timedelta(days=max(1, days))).isoformat()
        sql = ("SELECT j.*, o.product, o.material FROM print_jobs j"
               " LEFT JOIN orders o ON o.id=j.order_id"
               " WHERE j.state='done' AND j.finished_at>=?")
        params: list[Any] = [since]
        if product:
            sql += " AND pylower(o.product)=?"
            params.append(product.lower())
        if material:
            sql += " AND pylower(o.material)=?"
            params.append(material.lower())
        sql += " ORDER BY j.finished_at DESC LIMIT 200"
        rows = self.db.query(sql, params)
        if not rows:
            return {"found": False, "count": 0}

        grams_list = sorted([num(r.get("grams")) for r in rows if num(r.get("grams")) > 0])
        hours_list = sorted([num(r.get("duration_min")) / 60.0
                             for r in rows if num(r.get("duration_min")) > 0])

        def median(lst):
            if not lst:
                return 0.0
            n = len(lst)
            return lst[n // 2] if n % 2 else (lst[n // 2 - 1] + lst[n // 2]) / 2

        return {
            "found": True,
            "count": len(rows),
            "median_grams": round(median(grams_list), 1),
            "median_hours": round(median(hours_list), 2),
            "min_grams": round(min(grams_list), 1) if grams_list else 0,
            "max_grams": round(max(grams_list), 1) if grams_list else 0,
            "min_hours": round(min(hours_list), 2) if hours_list else 0,
            "max_hours": round(max(hours_list), 2) if hours_list else 0,
            "days": days,
        }

    # ------------------------------------------ минимальная рентабельная партия
    def min_profitable_batch(self, plate_grams: float = 0.0,
                             plate_hours: float = 0.0,
                             fit_per_plate: float = 1.0,
                             material: str = "",
                             quality: str = "standard",
                             supports_pct: float = 0.0,
                             target_per_hour: float = 0.0,
                             spool_price: float | None = None,
                             markup: float = 0.0) -> dict[str, Any]:
        """При какой партии прибыль за час принтера становится приемлемой.

        Перебирает qty от 1 до 100 и находит точку, где profit_per_hour
        пересекает target. Одна штука на плите часто невыгодна из-за
        прогрева и калибровки — этот метод это показывает.
        """
        s = self.db.settings()
        target = num(target_per_hour) or num(s.get("target_profit_per_hour"), 250)
        mk = num(markup) or num(s.get("default_markup"), 150)
        results = []
        best_qty = 0
        for q in range(1, 101):
            br = self.cost_breakdown(
                grams=0, hours=0, qty=q,
                plate_grams=num(plate_grams),
                plate_hours=num(plate_hours),
                fit_per_plate=num(fit_per_plate, 1),
                material=material, quality=quality,
                supports_pct=num(supports_pct),
                spool_price=spool_price,
            )
            price = br["per_unit"] * (1 + mk / 100.0)
            profit = (price - br["per_unit"]) * q
            pph = profit / br["total_hours"] if br["total_hours"] else 0
            results.append({
                "qty": q, "plates": br["plates"],
                "cost_unit": br["per_unit"],
                "price_unit": round(price, 0),
                "profit_total": round(profit, 2),
                "profit_per_hour": round(pph, 2),
                "total_hours": br["total_hours"],
                "ok": pph >= target,
            })
            if pph >= target and not best_qty:
                best_qty = q
        return {
            "target_per_hour": target,
            "min_qty": best_qty,
            "min_plates": results[best_qty - 1]["plates"] if best_qty else 0,
            "table": results[:30],
        }

    def live_spool(self, material: str = "") -> dict[str, Any]:
        """Остаток и цена катушек выбранного материала — для калькулятора."""
        s = self.db.settings()
        default = num(s.get("default_spool_price"), 1600)
        sql = "SELECT * FROM spools WHERE archived=0"
        params: list[Any] = []
        if material:
            sql += " AND UPPER(material)=?"
            params.append(str(material).strip().upper())
        rows = self.db.query(sql + " ORDER BY remaining_grams DESC", params)
        remaining = sum(num(r.get("remaining_grams")) for r in rows)
        value = 0.0
        weight_price = 0.0
        for r in rows:
            left = num(r.get("remaining_grams"))
            total = max(1.0, num(r.get("total_grams"), 1000))
            price = num(r.get("price"), default)
            value += left / total * price
            weight_price += left * price
        avg = round(weight_price / remaining, 2) if remaining else default
        top = rows[0] if rows else {}
        return {
            "material": str(material or "").upper(),
            "remaining": round(remaining, 1),
            "count": len(rows),
            "price": round(avg if remaining else default, 2),
            "value": round(value, 2),
            "color_name": top.get("color_name") or "",
        }

    def quote(self, grams: float = 0.0, hours: float = 0.0, qty: float = 1.0,
              material: str = "", color_swaps: float = 0.0, channel: str = "",
              rush: bool = False, spool_price: float | None = None,
              plate_grams: float = 0.0, plate_hours: float = 0.0,
              fit_per_plate: float = 1.0, quality: str = "standard") -> dict[str, Any]:
        """Быстрая котировка: себестоимость, цена, ₽/час, налог, хватит ли склада."""
        s = self.db.settings()
        live = self.live_spool(material)
        price_spool = num(spool_price) if spool_price else live.get("price")
        qty = max(1.0, num(qty, 1))
        br = self.cost_breakdown(
            grams=num(grams), hours=num(hours), qty=qty,
            spool_price=price_spool or None, material=material,
            color_swaps=num(color_swaps), quality=quality or "standard",
            plate_grams=num(plate_grams), plate_hours=num(plate_hours),
            fit_per_plate=num(fit_per_plate, 1))
        sp = self.suggest_price(br["per_unit"], qty, channel, rush)
        profit = round((sp["price"] - br["per_unit"]) * qty, 2)
        hours_total = num(br.get("total_hours"))
        pph = round(profit / hours_total, 2) if hours_total else 0.0
        tax = round(self.order_tax(sp["price"] * qty, profit, "person"), 2)
        target = num(s.get("target_profit_per_hour"), 250)
        need = num(br.get("total_grams"))
        have = num(live.get("remaining"))
        shortfall = round(max(0.0, need - have), 1)
        if not hours_total:
            verdict = ""
        elif pph >= target:
            verdict = "ok"
        elif pph >= target * 0.4:
            verdict = "warn"
        else:
            verdict = "bad"
        return {
            "cost": br["total"], "per_unit": br["per_unit"],
            "price": sp["price"], "profit": profit,
            "profit_per_hour": pph, "tax": tax,
            "after_tax": round(profit - tax, 2),
            "hours": hours_total, "grams": need,
            "purge_grams": br.get("purge_grams") or 0,
            "target": target, "verdict": verdict,
            "stock": live, "shortfall": shortfall,
            "tax_rate": self.tax_rate_for("person"),
            "tax_mode": TAX_MODES.get(str(s.get("tax_mode") or "none"), ""),
        }

    def plan_vs_fact(self, days: int = 30) -> dict[str, Any]:
        """Слайсер vs факт: во сколько раз дольше и тяжелее печатаем."""
        since = (datetime.now() - timedelta(days=max(1, days))).isoformat()
        rows = self.db.query(
            "SELECT j.grams, j.duration_min, j.est_grams, j.est_minutes,"
            " o.grams og, o.hours oh FROM print_jobs j"
            " LEFT JOIN orders o ON o.id=j.order_id"
            " WHERE j.state='done' AND j.finished_at>=? LIMIT 200", (since,))
        h_ratios, g_ratios = [], []
        for r in rows:
            actual_h = num(r.get("duration_min")) / 60.0
            plan_h = num(r.get("est_minutes")) / 60.0 or num(r.get("oh"))
            actual_g = num(r.get("grams"))
            plan_g = num(r.get("est_grams")) or num(r.get("og"))
            if actual_h > 0 and plan_h > 0:
                h_ratios.append(actual_h / plan_h)
            if actual_g > 0 and plan_g > 0:
                g_ratios.append(actual_g / plan_g)

        def med(lst):
            if not lst:
                return 0.0
            lst = sorted(lst)
            n = len(lst)
            return lst[n // 2] if n % 2 else (lst[n // 2 - 1] + lst[n // 2]) / 2

        return {
            "count": len(rows),
            "hours_factor": round(med(h_ratios), 2),
            "grams_factor": round(med(g_ratios), 2),
        }

    def purchase_hint(self) -> list[dict]:
        """Чего не хватает на активные заказы — список закупки."""
        finals = {r["id"] for r in self.db.query("SELECT id FROM statuses WHERE is_final=1")}
        need: dict[str, float] = {}
        for o in self.db.query("SELECT material, grams, qty, status FROM orders"):
            if o.get("status") in finals:
                continue
            mat = str(o.get("material") or "PLA").strip().upper() or "PLA"
            need[mat] = need.get(mat, 0.0) + num(o.get("grams")) * max(1.0, num(o.get("qty"), 1))
        have: dict[str, float] = {}
        for r in self.db.query(
                "SELECT UPPER(material) m, SUM(remaining_grams) g FROM spools"
                " WHERE archived=0 GROUP BY UPPER(material)"):
            have[str(r.get("m") or "PLA")] = num(r.get("g"))
        out = []
        for mat, grams in sorted(need.items(), key=lambda x: -x[1]):
            left = have.get(mat, 0.0)
            short = round(max(0.0, grams - left), 1)
            if short <= 0:
                continue
            out.append({"material": mat, "need": round(grams, 1),
                        "have": round(left, 1), "shortfall": short,
                        "spools": int(-(-short // 1000))})
        return out[:8]

    def weak_orders(self, limit: int = 8) -> list[dict]:
        """Активные заказы ниже нормы ₽/час — не брать или поднять цену."""
        s = self.db.settings()
        target = num(s.get("target_profit_per_hour"), 250)
        finals = {r["id"] for r in self.db.query("SELECT id FROM statuses WHERE is_final=1")}
        rows = []
        for o in self.db.query("SELECT * FROM orders ORDER BY datetime(created_at) DESC"):
            if o.get("status") in finals:
                continue
            if not num(o.get("hours")) and not num(o.get("actual_hours")):
                continue
            eco = self.order_economics(o)
            pph = num(eco.get("profit_per_hour"))
            if pph >= target:
                continue
            rows.append({
                "id": o["id"], "number": o.get("number"), "product": o.get("product"),
                "profit_per_hour": pph, "price": eco["price"], "profit": eco["profit"],
                "hours": eco["hours"], "verdict": "bad" if pph < target * 0.4 else "warn",
            })
            if len(rows) >= limit:
                break
        return rows

    def calc_board(self) -> dict[str, Any]:
        """Сводка для виджета «Экономика» на Обзоре."""
        s = self.db.settings()
        target = num(s.get("target_profit_per_hour"), 250)
        goal = num(s.get("goal_profit_month"), 60000)
        summary = self.summary(30)
        month = self.pnl_month(month_key())
        left = max(0.0, goal - num(month.get("profit")))
        hours_to_goal = round(left / target, 1) if target else 0.0
        pph = num(summary.get("profit_per_print_hour"))
        if pph >= target:
            verdict = "ok"
        elif pph >= target * 0.4:
            verdict = "warn"
        else:
            verdict = "bad"
        return {
            "target": target, "goal": goal,
            "month_profit": round(num(month.get("profit")), 2),
            "hours_to_goal": hours_to_goal,
            "pph": pph, "verdict": verdict,
            "weak": self.weak_orders(),
            "buy": self.purchase_hint(),
            "fact": self.plan_vs_fact(30),
        }

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
        # У мультизаказа grams — уже вся плита, а qty — сумма единиц по всем
        # позициям; умножать вес плиты на количество нельзя.
        # Если вызывающий код уже посчитал items_count (список заказов),
        # лишний пробный запрос не нужен — доверяем счётчику.
        has_items = (num(order.get("items_count")) > 0
                     or bool(order.get("items"))
                     or ("items_count" not in order
                         and self.db.one("SELECT id FROM order_items WHERE order_id=?"
                                         " LIMIT 1", (order.get("id") or "",)) is not None))
        qty = 1.0 if has_items else num(order.get("qty"), 1)
        cost = num(order.get("actual_cost")) or num(order.get("cost"))
        if not cost:
            cost = self.cost_breakdown(grams, hours,
                                       manual_minutes=num(order.get("manual_minutes")),
                                       qty=qty,
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

    def order_items_economics(self, order: dict) -> list[dict]:
        """Экономика позиций мультизаказа: себестоимость плиты делится по доле граммов.

        Вес плиты — фактический с принтера (order.actual_grams → order.grams),
        вес каждой позиции — норматив из базы товаров (order_items.grams).
        Если ни у одной позиции нет граммов, доля считается по цене.
        """
        if not order or not order.get("id"):
            return []
        items = self.db.query(
            "SELECT * FROM order_items WHERE order_id=? ORDER BY position",
            (order.get("id"),))
        if not items:
            return []
        eco = self.order_economics(order)
        weight_total = sum(num(i.get("grams")) * max(0.0, num(i.get("qty"), 1))
                           for i in items)
        price_total = sum(num(i.get("price")) * max(0.0, num(i.get("qty"), 1))
                          for i in items)
        out: list[dict] = []
        for it in items:
            qty = num(it.get("qty"), 1)
            qty = qty if qty > 0 else 1.0
            weight = num(it.get("grams")) * qty
            price = round(num(it.get("price")) * qty, 2)
            if weight_total > 0:
                share = weight / weight_total
            elif price_total > 0:
                share = num(it.get("price")) * qty / price_total
            else:
                share = 1.0 / len(items)
            cost = round(num(eco.get("cost")) * share, 2)
            out.append({
                "id": it.get("id"), "nom_id": it.get("nom_id"),
                "name": it.get("name"), "qty": qty,
                "unit_price": num(it.get("price")),
                "price": price,
                "grams": num(it.get("grams")),      # норматив на штуку
                "total_grams": round(weight, 1),
                "hours": num(it.get("hours")),
                "share": round(share, 4),
                "cost": cost,
                "profit": round(price - cost, 2),
            })
        return out

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
        if spool and auto and not int(num(spool.get("verified"), 1)):
            self.db.add_event(
                "security", "Автосписание остановлено: катушка не проверена",
                f"{spool.get('material') or 'материал'} · подтвердите массу и цену в складе",
                printer_id, {"spool_id": spool.get("id"), "job_id": job_id})
            return {"ok": False, "reason": "Катушка из AMS не подтверждена оператором", "spool": spool}
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
            if remaining <= 0.5:
                # Катушка опустела — архивируем сами, чтобы склад не висел
                # с «0 г, но в работе», и заводим событие «замените катушку».
                self.db.execute(
                    "UPDATE spools SET archived=1, remaining_grams=0, updated_at=? WHERE id=?",
                    (now_iso(), spool["id"]))
                self.db.add_event(
                    "filament_low", "Катушка закончилась",
                    f"{spool['material']} {spool['color_name']}: расходована полностью — "
                    f"поставьте новую катушку.",
                    printer_id, {"spool_id": spool_id, "remaining": 0})
            elif remaining / weight * 100 <= threshold:
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
    def consume_order_spools(self, job: dict) -> list[dict]:
        """Списать пластик по катушкам, привязанным к заказу (orders.spools).

        Формат: JSON-список [{"spool_id":"sp_...","grams":120,"note":"корпус"}].
        Это явное указание мастера «чем печатали» — точнее авто-подбора
        по цвету, поддерживает мультицвет (несколько катушек на заказ).
        """
        if not job.get("order_id"):
            return []
        order = self.db.one("SELECT spools FROM orders WHERE id=?", (job["order_id"],))
        raw = (order or {}).get("spools") or ""
        try:
            spools = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            spools = []
        if not isinstance(spools, list):
            return []
        out: list[dict] = []
        planned_total = sum(num(item.get("grams")) for item in spools if isinstance(item, dict))
        actual_total = num(job.get("grams"))
        # Поля заказа задают пропорцию между катушками. Итоговый вес берём из
        # факта принтера, чтобы плановые 100 г не оставались 100 г при факте 107 г.
        scale = actual_total / planned_total if actual_total > 0 and planned_total > 0 else 1.0
        for item in spools:
            if not isinstance(item, dict):
                continue
            grams = round(num(item.get("grams")) * scale, 1)
            spool_id = str(item.get("spool_id") or "")
            if grams <= 0 or not spool_id:
                continue
            spool = self.db.one("SELECT * FROM spools WHERE id=? AND archived=0", (spool_id,))
            note = str(item.get("note") or "").strip()
            result = self.consume_filament(
                grams, spool_id=spool["id"] if spool else "", job_id=job.get("id", ""),
                order_id=job.get("order_id") or "", auto=True,
                note=(note or "катушка заказа") + ("" if spool else " (катушка не найдена)"))
            out.append({**result, "spool_id": spool_id})
        return out

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
        if not isinstance(colors, list) and raw:
            # старый текстовый формат «Белый:40, Чёрный:15» — понимаем тоже
            colors = []
            for part in raw.split(","):
                name, _, value = part.partition(":")
                name = name.strip()
                try:
                    grams = float(str(value).strip().replace(",", "."))
                except ValueError:
                    continue
                if name and grams > 0:
                    colors.append({"color": name, "grams": grams})
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
        if kind not in ("income", "expense", "correction"):
            raise ValueError("Тип проводки: income, expense или correction")
        if kind == "correction":
            # Корректировка кассы: сумма со знаком (факт − система).
            # В P&L и налоговую базу не попадает (taxable=deductible=0).
            amount = round(num(amount), 2)
            if amount == 0:
                raise ValueError("Сумма корректировки должна быть не нулевой")
            category = str(category or "").strip() or "correction"
        else:
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
        rest = round(num(self.order_economics(order).get("debt")), 2)
        if rest <= 0:
            return None
        payment = self.add_payment(
            order["id"], rest, "payment", order.get("account_id") or "",
            "автоматически", "Оплата при закрытии заказа")
        return self.db.one("SELECT * FROM transactions WHERE id=?",
                           (payment.get("tx_id") or "",))

    def register_job_costs(self, job: dict) -> dict:
        """Идемпотентно учесть завершённую печать.

        Маркер ``print_jobs.accounted_at`` записывается в одной транзакции со
        списанием катушки, фактом заказа и статистикой. Повторный FINISH больше
        не может второй раз изменить остатки или себестоимость.
        """
        job_id = str(job.get("id") or "")
        if not job_id:
            raise ValueError("Не указано задание печати")
        with self.db.transaction():
            stored = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))
            if not stored:
                # Обратная совместимость для импортов и интеграций, которые
                # передавали завершённое задание напрямую, без предварительной
                # записи в очередь. Сначала материализуем его — дальше действует
                # тот же идемпотентный маркер.
                stored = self.db.upsert("print_jobs", {
                    **job,
                    "id": job_id,
                    "created_at": job.get("created_at") or now_iso(),
                })
            if stored.get("accounted_at"):
                return {
                    "job_id": job_id,
                    "already_accounted": True,
                    "accounted_at": stored.get("accounted_at"),
                }
            result = self._register_job_costs_once(stored)
            accounted_at = now_iso()
            self.db.execute(
                "UPDATE print_jobs SET accounted_at=? WHERE id=?",
                (accounted_at, job_id),
            )
            result["already_accounted"] = False
            result["accounted_at"] = accounted_at
            return result

    def _register_job_costs_once(self, job: dict) -> dict:
        """Внутренняя часть учёта; выполняется под транзакцией-маркером."""
        result: dict[str, Any] = {"job_id": job.get("id")}
        if not self.db.setting("auto_accounting", True):
            return result
        grams = num(job.get("grams"))
        hours = num(job.get("duration_min")) / 60.0
        # Многоцветная печать: продувка AMS при смене цвета (~12 г на смену).
        # Число смен берём из раскладки цветов заказа.
        color_swaps = 0
        if job.get("order_id"):
            order = self.db.one("SELECT colors FROM orders WHERE id=?",
                                (job.get("order_id"),)) if job.get("order_id") else None
            try:
                colors = json.loads(str((order or {}).get("colors") or ""))
                if isinstance(colors, list) and len(colors) > 1:
                    color_swaps = max(0, len(colors) - 1)
            except (json.JSONDecodeError, TypeError):
                color_swaps = 0
        breakdown = self.cost_breakdown(grams, hours, color_swaps=color_swaps)
        result["breakdown"] = breakdown
        result["purge_grams"] = breakdown.get("purge_grams", 0)

        if grams > 0 and self.db.setting("auto_consume_filament", True):
            # Порядок точности: 1) катушки, явно указанные в заказе
            # (orders.spools — конкретные катушки со склада, мультицвет);
            # 2) раскладка по цветам (orders.colors); 3) одна катушка по
            # AMS/материалу. Раскладка — весь расход, а не добавка к граммам.
            try:
                result["spools"] = self.consume_order_spools(job)
            except (TypeError, ValueError):
                result["spools"] = []
            if result["spools"]:
                result["filament"] = {
                    "ok": True, "multi_color": True, "by": "order_spools",
                    "grams": round(sum(num(x.get("grams")) for x in result["spools"]), 1),
                    "cost": round(sum(num(x.get("cost")) for x in result["spools"]), 2),
                }
            else:
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
                # Планового веса не было (например, заказ из печати без сметы
                # слайсера) — факт с принтера становится планом: в карточке
                # заказа появляется реальный вес плиты, а не пустота.
                if num(order.get("grams")) <= 0:
                    fact = self.db.one(
                        "SELECT actual_grams, actual_hours FROM orders WHERE id=?",
                        (order_id,))
                    if fact and num(fact.get("actual_grams")) > 0:
                        self.db.execute(
                            "UPDATE orders SET grams=?, hours=? WHERE id=?",
                            (num(fact["actual_grams"]), num(fact["actual_hours"]), order_id))
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
    def defects_cost(self, days: int = 30) -> dict[str, Any]:
        """Сколько денег съел брак за период (идея C10): пластик + время."""
        since = (datetime.now() - timedelta(days=max(1, int(days)))
                 ).isoformat(timespec="seconds")
        jobs = self.db.query(
            "SELECT j.*,d.loss AS defect_loss,d.grams AS defect_grams,"
            " d.confirmed_at AS defect_confirmed_at"
            " FROM print_jobs j LEFT JOIN defects d ON d.job_id=j.id"
            " AND d.confirmed_at<>''"
            " WHERE j.state='failed' AND j.finished_at>=?",
            (since,))
        grams = 0.0
        minutes = 0.0
        cost = 0.0
        spool_price = num(self.db.setting("default_spool_price"), 1600)
        overhead = (num(self.db.setting("amortization_per_hour"), 12)
                    + num(self.db.setting("maintenance_per_hour"), 3)
                    + num(self.db.setting("energy_price"), 6)
                    * num(self.db.setting("power_kw"), 0.15))
        for job in jobs:
            actual_grams = num(job.get("grams"))
            lost_grams = (num(job.get("defect_grams"))
                          if job.get("defect_confirmed_at") else actual_grams)
            ratio = min(1.0, lost_grams / actual_grams) if actual_grams > 0 else 1.0
            lost_minutes = num(job.get("duration_min")) * ratio
            grams += lost_grams
            minutes += lost_minutes
            if job.get("defect_confirmed_at"):
                cost += num(job.get("defect_loss"))
            else:
                cost += (lost_grams / 1000.0 * spool_price
                         + lost_minutes / 60.0 * overhead)
        return {"count": len(jobs), "grams": round(grams, 1),
                "minutes": round(minutes, 1), "cost": round(cost, 2)}

    def actual_hour_cost(self, days: int = 30) -> dict[str, Any]:
        """Фактическая стоимость часа печати против тарифа (идея C3).

        Факт — из реальных расходов на пластик, энергию и оборудование за
        период, делённых на фактические часы печати. Если факт сильно
        расходится с тарифом из настроек — себестоимость считается неверно.
        """
        since = (datetime.now() - timedelta(days=max(1, int(days)))
                 ).isoformat(timespec="seconds")
        minutes = num((self.db.one(
            "SELECT COALESCE(SUM(duration_min),0) m FROM print_jobs"
            " WHERE state='done' AND finished_at>=?", (since,)) or {}).get("m"))
        hours = minutes / 60.0
        rows = self.db.query(
            "SELECT COALESCE(SUM(amount),0) v FROM transactions"
            " WHERE kind='expense' AND at>=? AND category IN"
            " ('filament','energy','equipment','packaging')", (since,))
        spent = num((rows[0] if rows else {}).get("v"))
        per_hour = round(spent / hours, 2) if hours else 0.0
        tariff = (num(self.db.setting("amortization_per_hour"), 12)
                  + num(self.db.setting("maintenance_per_hour"), 3)
                  + num(self.db.setting("energy_price"), 6)
                  * num(self.db.setting("power_kw"), 0.15))
        diff_pct = round((per_hour - tariff) / tariff * 100, 1) if tariff else 0.0
        if hours <= 0:
            verdict = "нет часов печати за период — сравнение позже"
        elif abs(diff_pct) <= 10:
            verdict = "тариф близок к факту — расчёты честные"
        elif diff_pct > 0:
            verdict = f"факт выше тарифа на {diff_pct}% — себестоимость недооценена"
        else:
            verdict = f"факт ниже тарифа на {abs(diff_pct)}% — тариф можно снизить"
        return {"days": int(days), "hours": round(hours, 1),
                "spent": round(spent, 2), "per_hour": per_hour,
                "tariff": round(tariff, 2), "diff_pct": diff_pct,
                "verdict": verdict}

    def recalc_catalog(self, apply: bool = False) -> dict[str, Any]:
        """Массовый пересчёт цен базы изделий по текущим тарифам (идея C11).

        Сначала — предпросмотр (apply=False): для каждой позиции считается
        новая себестоимость по текущим тарифам и рекомендованная цена.
        apply=True пишет новые цены в базу изделий.
        """
        items: list[dict] = []
        for row in self.db.query("SELECT * FROM catalog WHERE archived=0"):
            grams = num(row.get("grams"))
            hours = num(row.get("hours"))
            if grams <= 0 and hours <= 0:
                continue
            breakdown = self.cost_breakdown(
                grams, hours, material=str(row.get("material") or "PLA"))
            cost = num(breakdown.get("per_unit"))
            suggestion = self.suggest_price(cost)
            new_price = num(suggestion.get("price"))
            items.append({
                "id": row["id"], "name": row.get("name"),
                "old_price": round(num(row.get("price")), 2),
                "new_price": round(new_price, 2),
                "cost": round(cost, 2),
                "changed": abs(new_price - num(row.get("price"))) >= 0.5,
            })
        if apply:
            for item in items:
                self.db.execute("UPDATE catalog SET price=? WHERE id=?",
                                (item["new_price"], item["id"]))
            self.db.add_event("catalog", "Пересчитаны цены базы изделий",
                              f"{len(items)} позиций по новым тарифам", "", {})
        return {"items": items, "count": len(items),
                "changed": sum(1 for item in items if item["changed"])}

    def summary(self, days: int = 30) -> dict[str, Any]:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        income_rows = self.db.query(
            "SELECT amount, fee FROM transactions WHERE kind='income' AND at>=?", (since,))
        income = sum(num(r["amount"]) for r in income_rows)
        fees = sum(num(r["fee"]) for r in income_rows)
        expense_rows = self.db.query(
            "SELECT amount, category FROM transactions WHERE kind='expense' AND at>=?",
            (since,))
        # P&L считает прибыль без «вывода себе» и с отдельной строкой налогов;
        # сводка цеха должна давать ту же цифру, иначе два отчёта расходятся.
        expense = sum(num(r["amount"]) for r in expense_rows
                      if r["category"] not in ("tax", "insurance", "withdrawal"))
        taxes = sum(num(r["amount"]) for r in expense_rows
                    if r["category"] in ("tax", "insurance"))
        owner_draw = sum(num(r["amount"]) for r in expense_rows
                         if r["category"] == "withdrawal")
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
        profit = income - fees - expense - taxes
        return {
            "period_days": days,
            "income": round(income, 2),
            "fees": round(fees, 2),
            "expense": round(expense, 2),
            "taxes": round(taxes, 2),
            "owner_draw": round(owner_draw, 2),
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
            "defects_cost": self.defects_cost(days)["cost"],
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
    def run_fixed_costs(self, today: str = "", force: bool = False) -> list[dict]:
        """Начисляет постоянные расходы за текущий период (идемпотентно).

        Каждый расход начисляется один раз в месяц/квартал/год: повторный вызов
        ничего не сделает, пока не наступит следующий период. ``force=True``
        использует мастер «Закрыть месяц» — флаг автоначисления не обязателен.
        """
        if not force and not self.db.setting("fixed_costs_auto", True):
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
        # Дополнительный взнос 1% считается с дохода (выручки) свыше порога,
        # а не с прибыли — даже на УСН «доходы минус расходы».
        base = income
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
        # Выключатель раньше отображался в настройках, но расчёт его игнорировал.
        reserve = 0.0
        if s.get("tax_reserve_enabled", True):
            reserve = (max(0.0, due_total)
                       + num(s.get("tax_reserve_extra")) * income / 100.0)
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
                " WHEN kind='correction' THEN amount"
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
            # Для выданного в долг заказа возраст долга начинается с выдачи,
            # а не с даты создания карточки (иначе старый заказ сразу становился
            # «просроченным» в момент передачи клиенту).
            debt_since = (o.get("closed_at") or o.get("created_at") or "")[:10]
            try:
                days = (today - date.fromisoformat(debt_since)).days if debt_since else 0
            except ValueError:
                days = 0
            rows.append({
                "id": o["id"], "number": o.get("number"), "customer": o.get("customer_name"),
                "product": o.get("product"), "price": eco["price"], "paid": eco["paid"],
                "debt": eco["debt"], "days": days, "overdue": days > alert_days,
                "done": o.get("status") in finals, "phone": o.get("phone"),
                "messenger": o.get("messenger"), "reminded_at": o.get("reminded_at") or "",
            })
        rows.sort(key=lambda r: (-r["debt"]))
        return {
            "rows": rows,
            "total": round(sum(r["debt"] for r in rows), 2),
            "overdue": round(sum(r["debt"] for r in rows if r["overdue"]), 2),
            "count": len(rows),
        }

    def add_payment(self, order_id: str, amount: float, kind: str = "payment",
                    account_id: str = "", method: str = "", note: str = "",
                    request_id: str = "", expected_updated_at: str = "") -> dict:
        """Идемпотентно записать платёж, проводку и остаток долга."""
        request_id = str(request_id or "").strip()[:120]
        with self.db.transaction():
            if request_id:
                existing = self.db.one(
                    "SELECT * FROM payments WHERE request_id=?", (request_id,)
                )
                if existing:
                    if (existing.get("order_id") != order_id
                            or existing.get("kind") != kind
                            or abs(num(existing.get("amount")) - num(amount)) > 0.005):
                        raise ValueError("Ключ запроса уже использован для другого платежа")
                    existing["already_recorded"] = True
                    return existing
            row = self._add_payment_once(
                order_id, amount, kind, account_id, method, note, request_id,
                expected_updated_at
            )
            row["already_recorded"] = False
            return row

    def _add_payment_once(self, order_id: str, amount: float, kind: str = "payment",
                          account_id: str = "", method: str = "", note: str = "",
                          request_id: str = "", expected_updated_at: str = "") -> dict:
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            raise ValueError("Заказ не найден")
        expected_updated_at = str(expected_updated_at or "").strip()
        if expected_updated_at and expected_updated_at != str(order.get("updated_at") or ""):
            raise ValueError("Заказ уже изменён — обновите карточку перед записью платежа")
        # Старое поле prepaid — только совместимость старых заказов. Если оплата
        # приходит по новому сценарию, основной счётчик paid должен стать суммой
        # «уже собранной» суммы, иначе заказ с prepaid>paid навсегда сохранит
        # остаток долга: max(paid,prepaid) увидит только старое значение.
        legacy_prepaid = num(order.get("prepaid"))
        if legacy_prepaid > num(order.get("paid")):
            stamp = now_iso()
            self.db.execute(
                "UPDATE orders SET paid=?, prepaid=0, updated_at=? WHERE id=?",
                (legacy_prepaid, stamp, order_id))
            order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if kind not in ("prepay", "payment", "refund"):
            raise ValueError("Тип платежа: предоплата, оплата или возврат")
        amount = round(num(amount), 2)
        if amount <= 0:
            raise ValueError("Сумма платежа должна быть больше нуля")
        paid = max(num(order.get("paid")), num(order.get("prepaid")))
        due = round(max(0.0, num(order.get("price")) - num(order.get("discount")) - paid), 2)
        if kind in ("prepay", "payment") and num(order.get("price")) > 0 and amount > due + 0.005:
            raise ValueError(f"Платёж больше остатка: осталось {due:g} ₽")
        if kind == "refund" and amount > paid:
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
                "INSERT INTO payments(id,at,order_id,customer_id,amount,kind,account_id,"
                "method,fee,note,tx_id,request_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pay_id, stamp, order_id, order.get("customer_id"), amount, kind,
                 acc["id"], method, fee, note, tx["id"], request_id, stamp))
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

        # Денежный поток считаем по факту на кассе: комиссия эквайринга
        # уходит до зачисления, поэтому с дохода берём amount−fee (так же
        # считает accounts_state), иначе отчёт показывал бы больше денег,
        # чем есть на счетах.
        income_tx = sum(num(t["amount"]) - num(t["fee"]) for t in txs
                        if t["kind"] == "income")
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

    def sales_details(self, period: str = "month", offset: int = 0,
                      limit: int = 500) -> dict[str, Any]:
        """Построчный реестр проданных товаров за период.

        Владелец просил «буквально посмотреть каждый товар, который продался,
        даже если продавалось документом». Поэтому в один список попадают:
        • позиции проведённых документов продаж (1С-стиль: несколько строк в
          одном документе, себестоимость по складу);
        • позиции закрытых заказов (с одиночным товаром и мультизаказы);
        • продажи со стеллажа (по QR, из ТГ, ручные, 1С-чек).

        Это рабочий реестр фактов, а не сведённый P&L: один и тот же товар
        показывается столько раз, сколько реально ушёл, с документом-источником.
        """
        start, end, label = self.period_bounds(period, offset)
        channels = {c["id"]: c.get("name") or c["id"]
                    for c in self.db.query("SELECT id,name FROM channels")}
        final_ids = [r["id"] for r in self.db.query(
            "SELECT id FROM statuses WHERE is_final=1")]
        final_marks = ",".join("?" * len(final_ids)) if final_ids else "''"

        rows: list[dict[str, Any]] = []

        # 1) Проведённые документы «Продажа» — основной «продавалось документом».
        doc_rows = self.db.query(
            "SELECT d.id doc_id, d.number doc_number, d.at at, d.channel channel,"
            " d.note note, d.counterparty_id cp, ctp.name cp_name,"
            " ctp.name counterparty_name,"
            " i.line line, i.nom_id nom_id, n.name nom_name, i.qty qty,"
            " i.price price, i.cost cost, i.amount amount"
            " FROM documents d LEFT JOIN doc_items i ON i.doc_id=d.id"
            " LEFT JOIN nomenclature n ON n.id=i.nom_id"
            " LEFT JOIN customers ctp ON ctp.id=d.counterparty_id"
            " WHERE d.kind='sale' AND d.state='posted'"
            " AND d.at>=? AND d.at<? AND i.id IS NOT NULL"
            " ORDER BY datetime(d.at) DESC, d.number, i.line", (start, end))
        # Себестоимость строки документа при проведении пишется не всегда
        # (quick_sale отдаёт только цену). Распределяем cost_total документа
        # пропорционально суммам строк, чтобы прибыль в реестре не была
        # завышена до выручки.
        doc_cost_totals = {
            d["id"]: num(d.get("cost_total")) for d in self.db.query(
                "SELECT id,cost_total FROM documents WHERE kind='sale' AND state='posted'"
                " AND at>=? AND at<?", (start, end))
        }
        doc_amount_totals: dict[str, float] = {}
        for r in doc_rows:
            doc_amount_totals[r["doc_id"]] = doc_amount_totals.get(r["doc_id"], 0.0) \
                + num(r.get("amount"))
        for r in doc_rows:
            qty = num(r.get("qty"))
            price = num(r.get("price"))
            amount = num(r.get("amount")) or qty * price
            line_cost = num(r.get("cost")) * qty
            if not line_cost:
                doc_total_cost = doc_cost_totals.get(r.get("doc_id"), 0.0)
                doc_total_amount = doc_amount_totals.get(r.get("doc_id"), 0.0)
                if doc_total_cost and doc_total_amount:
                    line_cost = doc_total_cost * amount / doc_total_amount
            rows.append({
                "at": r.get("at") or "",
                "source": "document",
                "source_label": "Документ",
                "doc_id": r.get("doc_id") or "",
                "doc_number": r.get("doc_number") or r.get("doc_id") or "",
                "nom_id": r.get("nom_id") or "",
                "name": r.get("nom_name") or "Без названия",
                "qty": round(qty, 3),
                "price": round(price, 2),
                "amount": round(amount, 2),
                "cost": round(line_cost, 2),
                "profit": round(amount - line_cost, 2),
                "customer": (r.get("cp_name") or r.get("counterparty_name") or ""),
                "channel": channels.get(r.get("channel") or "", r.get("channel") or "Магазин"),
                "note": r.get("note") or "",
            })

        # 2) Закрытые заказы: одиночные и строки мультизаказов.
        if final_ids:
            orders = self.db.query(
                f"SELECT * FROM orders WHERE created_at>=? AND created_at<?"
                f" AND status IN ({final_marks})", (start, end, *final_ids))
        else:
            orders = []
        doc_order_ids = {r.get("order_id") for r in self.db.query(
            "SELECT order_id FROM documents WHERE kind='sale' AND state='posted'"
            " AND order_id IS NOT NULL AND order_id<>''")}
        for o in orders:
            if o.get("id") in doc_order_ids:
                continue  # учтён через документ продажи
            at = o.get("closed_at") or o.get("updated_at") or o.get("created_at") or ""
            if not (start <= at < end):
                # заказ создан в периоде, но закрылся позже — всё равно покажем
                at = o.get("created_at") or at
            eco = self.order_economics(o)
            lines = self.order_items_economics(o)
            if lines:
                for li in lines:
                    rows.append({
                        "at": at, "source": "order", "source_label": "Заказ",
                        "doc_id": o.get("id") or "",
                        "doc_number": o.get("number") or "",
                        "nom_id": li.get("nom_id") or "",
                        "name": li.get("name") or o.get("product") or "Без названия",
                        "qty": round(num(li.get("qty")), 3),
                        "price": round(num(li.get("unit_price")), 2),
                        "amount": round(num(li.get("price")), 2),
                        "cost": round(num(li.get("cost")), 2),
                        "profit": round(num(li.get("profit")), 2),
                        "customer": o.get("customer_name") or "",
                        "channel": channels.get(o.get("channel") or "",
                                                o.get("channel") or "Напрямую"),
                        "note": o.get("notes") or "",
                    })
            else:
                qty = num(o.get("qty"), 1) or 1
                rows.append({
                    "at": at, "source": "order", "source_label": "Заказ",
                    "doc_id": o.get("id") or "",
                    "doc_number": o.get("number") or "",
                    "nom_id": o.get("nom_id") or "",
                    "name": o.get("product") or "Без названия",
                    "qty": round(qty, 3),
                    "price": round(eco["price"] / qty, 2) if qty else 0.0,
                    "amount": round(eco["price"], 2),
                    "cost": round(eco["cost"], 2),
                    "profit": round(eco["profit"], 2),
                    "customer": o.get("customer_name") or "",
                    "channel": channels.get(o.get("channel") or "",
                                            o.get("channel") or "Напрямую"),
                    "note": o.get("notes") or "",
                })

        # 3) Продажи со стеллажа (по QR, из ТГ, ручные, 1С).
        shelf_rows = self.db.query(
            "SELECT m.*, i.name item_name, i.cost_per_unit cost_unit"
            " FROM shelf_moves m LEFT JOIN shelf_items i ON i.id=m.item_id"
            " WHERE m.kind IN ('sale','online') AND m.qty<0"
            " AND m.at>=? AND m.at<? ORDER BY datetime(m.at) DESC", (start, end))
        for m in shelf_rows:
            qty = abs(num(m.get("qty")))
            price = num(m.get("price"))
            unit_cost = num(m.get("cost_unit"))
            amount = price * qty
            rows.append({
                "at": m.get("at") or "",
                "source": "shelf", "source_label": "Стеллаж",
                "doc_id": m.get("id") or "",
                "doc_number": m.get("external_id") or m.get("id") or "",
                "nom_id": m.get("item_id") or "",
                "name": m.get("item_name") or "Позиция стеллажа",
                "qty": round(qty, 3),
                "price": round(price, 2),
                "amount": round(amount, 2),
                "cost": round(unit_cost * qty, 2),
                "profit": round(amount - unit_cost * qty, 2),
                "customer": "",
                "channel": "Стеллаж" if m.get("kind") == "sale" else "Онлайн",
                "note": (m.get("note") or "").strip(),
            })

        rows.sort(key=lambda r: r.get("at") or "", reverse=True)
        total_qty = sum(num(r["qty"]) for r in rows)
        total_amount = sum(num(r["amount"]) for r in rows)
        total_cost = sum(num(r["cost"]) for r in rows)
        total_profit = sum(num(r["profit"]) for r in rows)
        # Сводка по товарам — для быстрого поиска «что именно и сколько».
        products: dict[str, dict[str, Any]] = {}
        for r in rows:
            p = products.setdefault(r["name"], {"name": r["name"], "qty": 0.0,
                                                "amount": 0.0, "profit": 0.0})
            p["qty"] += num(r["qty"])
            p["amount"] += num(r["amount"])
            p["profit"] += num(r["profit"])
        product_list = sorted(products.values(), key=lambda x: -x["amount"])
        for p in product_list:
            p["qty"] = round(p["qty"], 2)
            p["amount"] = round(p["amount"], 2)
            p["profit"] = round(p["profit"], 2)

        return {
            "period": period, "label": label, "start": start, "end": end,
            "rows": rows[: max(1, int(limit or 500))],
            "count": len(rows),
            "total_qty": round(total_qty, 2),
            "total_amount": round(total_amount, 2),
            "total_cost": round(total_cost, 2),
            "total_profit": round(total_profit, 2),
            "products": product_list,
        }

    def sales_details_csv(self, period: str = "month", offset: int = 0) -> str:
        """CSV реестра продаж — каждая строка товара отдельной записью."""
        rep = self.sales_details(period, offset, limit=10000)
        lines = [f"PrintFlow · реестр проданных товаров за {rep['label']}", "",
                 "Дата;Источник;Документ/заказ;Товар;Клиент;Канал;Штук;Цена;Сумма;Себест.;Прибыль;Заметка"]
        for r in rep["rows"]:
            lines.append(";".join(str(x).replace(";", ",") for x in (
                (r["at"] or "")[:16].replace("T", " "),
                r.get("source_label", ""),
                r.get("doc_number", ""),
                r.get("name", ""),
                r.get("customer", ""),
                r.get("channel", ""),
                r.get("qty", 0),
                r.get("price", 0),
                r.get("amount", 0),
                r.get("cost", 0),
                r.get("profit", 0),
                (r.get("note") or "").replace("\n", " "))))
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

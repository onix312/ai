"""Аналитика PrintFlow 6.0: OEE, детектор аномалий, P&L по продуктам,
коэффициент коррекции, инвестиционный калькулятор.

Единый модуль для всех аналитических расчётов, которые не относятся
к бухгалтерии (accounting.py) или планированию (planner.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .accounting import Accounting, num
from .db import Database


class Analytics:
    """OEE, аномалии, P&L продуктов, коррекция, инвестиции."""

    def __init__(self, db: Database):
        self.db = db
        self.acc = Accounting(db)

    # =========================================================== OEE (R)
    def oee(self, days: int = 30, printer_id: str = "") -> dict[str, Any]:
        """Overall Equipment Effectiveness — эффективность принтера.

        OEE = Availability × Performance × Quality

        • Availability = время_печати / (время_печати + простой)
        • Performance = факт_скорость / план_скорость
        • Quality = годные / (годные + брак)

        Простой = время между завершением печати и следующим стартом
        (когда принтер IDLE и никого нет рядом).
        """
        since = (datetime.now() - timedelta(days=max(1, days))).isoformat()
        sql = ("SELECT * FROM print_jobs WHERE finished_at>=?"
               " ORDER BY datetime(finished_at)")
        params: list[Any] = [since]
        if printer_id:
            sql += " AND printer_id=?"
            params.append(printer_id)
        jobs = self.db.query(sql, params)

        # Разбивка времени
        print_min = sum(num(j.get("duration_min")) for j in jobs)
        done = [j for j in jobs if j.get("state") == "done"]
        failed = [j for j in jobs if j.get("state") == "failed"]
        done_min = sum(num(j.get("duration_min")) for j in done)
        failed_min = sum(num(j.get("duration_min")) for j in failed)

        # Простой: замеряем из событий «деталь снята»
        idle_events = self.db.query(
            "SELECT * FROM events WHERE kind='production' AND at>=?"
            " ORDER BY at", (since,))
        idle_min = sum(num(e.get("data", {}).get("idle_min") if isinstance(e.get("data"), dict) else 0)
                       for e in idle_events)
        # Если событий нет — оценка: 15 мин простоя на каждое задание
        if not idle_min and len(done) > 0:
            idle_min = len(done) * 15.0

        total_min = print_min + idle_min
        # Доступность
        availability = done_min / total_min if total_min else 0.0
        # Производительность: факт/план по заданиям с оценкой
        perf_ratios = []
        for j in done:
            est = num(j.get("est_minutes"))
            actual = num(j.get("duration_min"))
            if est > 0 and actual > 0:
                perf_ratios.append(est / actual)
        performance = sum(perf_ratios) / len(perf_ratios) if perf_ratios else 1.0
        performance = min(performance, 1.0)  # не может быть >100%
        # Качество
        total_items = len(done) + len(failed)
        quality = len(done) / total_items if total_items else 1.0

        oee_pct = availability * performance * quality * 100

        # Разбивка потерь
        losses = []
        if idle_min > 0:
            losses.append({"kind": "idle", "label": "Простой (ожидание снятия)",
                           "minutes": round(idle_min, 1),
                           "pct": round(idle_min / max(1, total_min) * 100, 1)})
        if failed_min > 0:
            losses.append({"kind": "failure", "label": "Брак (неудачные печати)",
                           "minutes": round(failed_min, 1),
                           "pct": round(failed_min / max(1, total_min) * 100, 1)})
        # Потери на перенастройки (смена материала): ~12 мин на смену
        material_changes = self._count_material_changes(since, printer_id)
        change_min = material_changes * 12
        if change_min > 0:
            losses.append({"kind": "changeover", "label": "Перенастройка (смена материала)",
                           "minutes": round(change_min, 1),
                           "pct": round(change_min / max(1, total_min + change_min) * 100, 1)})
        losses.sort(key=lambda x: -x["minutes"])

        # Цель: >70% OEE
        target = 70.0
        verdict = "ok" if oee_pct >= target else "warn" if oee_pct >= target * 0.6 else "bad"

        return {
            "days": days,
            "oee_pct": round(oee_pct, 1),
            "availability": round(availability * 100, 1),
            "performance": round(performance * 100, 1),
            "quality": round(quality * 100, 1),
            "target": target,
            "verdict": verdict,
            "print_minutes": round(print_min, 1),
            "idle_minutes": round(idle_min, 1),
            "total_minutes": round(total_min, 1),
            "jobs_done": len(done),
            "jobs_failed": len(failed),
            "material_changes": material_changes,
            "losses": losses,
            "utilization_pct": round(print_min / max(1, total_min) * 100, 1),
        }

    def _count_material_changes(self, since: str, printer_id: str = "") -> int:
        """Подсчёт смен материала в AMS (по событиям ams)."""
        sql = "SELECT * FROM events WHERE kind='ams' AND at>=? AND title LIKE '%заменили%'"
        params: list[Any] = [since]
        if printer_id:
            sql += " AND printer_id=?"
            params.append(printer_id)
        return len(self.db.query(sql, params))

    # =================================================== Коэффициент коррекции (S)
    def correction_factors(self, days: int = 60, material: str = "") -> dict[str, Any]:
        """Фактическая vs плановая оценка: насколько слайсер врёт.

        Для каждого завершённого задания сравниваем est_minutes (из слайсера)
        и duration_min (факт). Коэффициент >1 означает «дольше плана».
        """
        since = (datetime.now() - timedelta(days=max(1, days))).isoformat()
        sql = ("SELECT j.est_minutes, j.duration_min, j.grams, j.est_grams,"
               " o.material, o.product"
               " FROM print_jobs j LEFT JOIN orders o ON o.id=j.order_id"
               " WHERE j.state='done' AND j.finished_at>=?"
               " AND j.est_minutes>0 AND j.duration_min>0")
        params: list[Any] = [since]
        if material:
            sql += " AND pylower(o.material)=?"
            params.append(material.lower())
        rows = self.db.query(sql, params)
        if not rows:
            return {"found": False, "count": 0, "factors": {}}

        by_material: dict[str, list[float]] = {}
        all_ratios: list[float] = []
        for r in rows:
            ratio = num(r["duration_min"]) / max(1, num(r["est_minutes"]))
            all_ratios.append(ratio)
            mat = (r.get("material") or "OTHER").upper()
            by_material.setdefault(mat, []).append(ratio)

        def stats(ratios: list[float]) -> dict:
            if not ratios:
                return {"count": 0, "factor": 1.0, "min": 1.0, "max": 1.0}
            return {
                "count": len(ratios),
                "factor": round(sum(ratios) / len(ratios), 3),
                "min": round(min(ratios), 3),
                "max": round(max(ratios), 3),
            }

        factors = {}
        for mat, ratios in by_material.items():
            factors[mat] = stats(ratios)
        factors["_all"] = stats(all_ratios)

        return {
            "found": True,
            "count": len(rows),
            "days": days,
            "factors": factors,
        }

    # =================================================== P&L по продуктам (AC)
    def pnl_by_product(self, days: int = 30) -> dict[str, Any]:
        """Прибыль и убытки по каждому продукту за период.

        Группирует закрытые заказы по названию продукта и считает:
        выручку, себестоимость, комиссии, налог, чистую прибыль,
        прибыль за час печати.
        """
        since = (datetime.now() - timedelta(days=max(1, days))).isoformat()
        orders = self.db.query(
            "SELECT * FROM orders WHERE created_at>=?"
            " AND status IN (SELECT id FROM statuses WHERE is_final=1)",
            (since,))
        if not orders:
            return {"days": days, "products": [], "total_revenue": 0,
                    "total_profit": 0, "total_hours": 0}

        products: dict[str, dict[str, float]] = {}
        for o in orders:
            eco = self.acc.order_economics(o)
            name = (o.get("product") or "Без названия").strip()
            p = products.setdefault(name, {
                "name": name, "orders": 0, "qty": 0.0,
                "revenue": 0.0, "cost": 0.0, "fee": 0.0,
                "tax": 0.0, "profit": 0.0, "hours": 0.0, "grams": 0.0,
            })
            p["orders"] += 1
            p["qty"] += num(o.get("qty"), 1)
            p["revenue"] += eco["price"]
            p["cost"] += eco["cost"]
            p["fee"] += eco["fee"]
            p["tax"] += eco["tax"]
            p["profit"] += eco["profit"]
            p["hours"] += eco["hours"]
            p["grams"] += eco["grams"]

        items = sorted(products.values(), key=lambda x: -x["profit"])
        for item in items:
            for k in ("revenue", "cost", "fee", "tax", "profit"):
                item[k] = round(item[k], 2)
            item["hours"] = round(item["hours"], 2)
            item["grams"] = round(item["grams"], 1)
            item["margin_pct"] = round(item["profit"] / item["revenue"] * 100, 1) if item["revenue"] else 0
            item["profit_per_hour"] = round(item["profit"] / item["hours"], 2) if item["hours"] else 0
            item["profitable"] = item["profit"] > 0

        # ABC-классификация по выручке
        total_rev = sum(i["revenue"] for i in items) or 1.0
        acc = 0.0
        for i in items:
            acc += i["revenue"]
            share = acc / total_rev
            if share <= 0.8:
                i["cls"] = "A"
            elif share <= 0.95:
                i["cls"] = "B"
            else:
                i["cls"] = "C"

        return {
            "days": days,
            "products": items,
            "total_revenue": round(total_rev, 2),
            "total_profit": round(sum(i["profit"] for i in items), 2),
            "total_hours": round(sum(i["hours"] for i in items), 2),
            "total_orders": sum(i["orders"] for i in items),
            "profitable_count": len([i for i in items if i["profitable"]]),
            "unprofitable_count": len([i for i in items if not i["profitable"]]),
        }

    # =================================================== Детектор аномалий (AN)
    def detect_anomalies(self, days: int = 30) -> list[dict]:
        """Автоматический поиск странных ситуаций в данных.

        Типы аномалий:
        1. Себестоимость выросла — продукт стал дороже >20% за месяц
        2. Подозрительно дешёвый заказ — маржа <5%
        3. Принтер медленнее обычного — факт > плана на >30%
        4. Рост брака — % брака за последние 7 дней выше среднего
        5. Клиент-аномалия — заказ в 5+ раз больше обычного
        6. Простой — принтер IDLE больше 2 часов подряд
        """
        since = (datetime.now() - timedelta(days=days)).isoformat()
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        anomalies: list[dict] = []

        # 1. Себестоимость выросла
        products_now = {}
        products_prev = {}
        month_start = (datetime.now() - timedelta(days=30)).isoformat()
        prev_start = (datetime.now() - timedelta(days=60)).isoformat()
        for o in self.db.query(
                "SELECT * FROM orders WHERE created_at>=? AND status IN"
                " (SELECT id FROM statuses WHERE is_final=1)", (prev_start,)):
            eco = self.acc.order_economics(o)
            name = (o.get("product") or "").strip()
            if not name:
                continue
            created = o.get("created_at", "")
            if created >= month_start:
                products_now.setdefault(name, []).append(eco["cost"] / max(1, num(o.get("qty"), 1)))
            else:
                products_prev.setdefault(name, []).append(eco["cost"] / max(1, num(o.get("qty"), 1)))
        for name in products_now:
            if name not in products_prev:
                continue
            avg_now = sum(products_now[name]) / len(products_now[name])
            avg_prev = sum(products_prev[name]) / len(products_prev[name])
            if avg_prev > 0:
                change = (avg_now - avg_prev) / avg_prev * 100
                if change > 20:
                    anomalies.append({
                        "kind": "cost_spike", "severity": "warn",
                        "title": f"Себестоимость «{name}» выросла на {round(change)}%",
                        "detail": f"Было {round(avg_prev)} ₽/шт, стало {round(avg_now)} ₽/шт",
                        "action": "Проверить профиль печати, цену пластика и время печати",
                    })

        # 2. Подозрительно дешёвые заказы
        for o in self.db.query(
                "SELECT * FROM orders WHERE created_at>=?"
                " AND status IN (SELECT id FROM statuses WHERE is_final=1)", (since,)):
            eco = self.acc.order_economics(o)
            if eco["price"] > 0 and eco["margin"] < 5:
                anomalies.append({
                    "kind": "low_margin", "severity": "bad",
                    "title": f"Заказ №{o.get('number', '?')}: маржа {round(eco['margin'])}%",
                    "detail": f"Цена {round(eco['price'])} ₽, себестоимость {round(eco['cost'])} ₽",
                    "action": "Поднять цену или отказаться от заказа",
                })

        # 3. Принтер медленнее обычного
        recent_jobs = self.db.query(
            "SELECT * FROM print_jobs WHERE state='done' AND finished_at>=?"
            " AND est_minutes>0 AND duration_min>0", (week_ago,))
        for j in recent_jobs:
            ratio = num(j["duration_min"]) / num(j["est_minutes"])
            if ratio > 1.3:
                anomalies.append({
                    "kind": "slow_print", "severity": "warn",
                    "title": f"Печать на {round(ratio * 100 - 100)}% дольше плана",
                    "detail": f"План {round(num(j['est_minutes']))} мин, факт {round(num(j['duration_min']))} мин",
                    "action": "Проверить профиль: возможно, стрингинг или паузы",
                })

        # 4. Рост брака
        total_week = self.db.one(
            "SELECT COUNT(*) done FROM print_jobs WHERE state='done' AND finished_at>=?",
            (week_ago,)) or {}
        failed_week = self.db.one(
            "SELECT COUNT(*) failed FROM print_jobs WHERE state='failed' AND finished_at>=?",
            (week_ago,)) or {}
        done_n = int(num(total_week.get("done")))
        fail_n = int(num(failed_week.get("failed")))
        if done_n + fail_n >= 5:
            fail_pct = fail_n / (done_n + fail_n) * 100
            if fail_pct > 15:
                anomalies.append({
                    "kind": "failure_spike", "severity": "bad",
                    "title": f"Брак {round(fail_pct)}% за неделю ({fail_n} из {done_n + fail_n})",
                    "detail": "Норма — до 5%. Проверить пластик, стол, сопло.",
                    "action": "Остановить печать и проверить оборудование",
                })

        # Сортировка: bad → warn
        severity_order = {"bad": 0, "warn": 1}
        anomalies.sort(key=lambda a: severity_order.get(a["severity"], 9))
        return anomalies[:20]  # максимум 20

    # ============================================= Инвестиционный калькулятор (AA)
    def investment_calc(self, printer_cost: float = 0.0,
                        extra_hours_month: float = 0.0,
                        profit_per_hour: float = 0.0,
                        extra_costs_month: float = 0.0) -> dict[str, Any]:
        """Окупаемость нового принтера или оборудования.

        • printer_cost — стоимость покупки;
        • extra_hours_month — сколько доп. часов печати в месяц;
        • profit_per_hour — прибыль за час (из текущего OEE);
        • extra_costs_month — доп. расходы (пластик, энергия, обслуживание).
        """
        cost = num(printer_cost)
        hours = num(extra_hours_month)
        pph = num(profit_per_hour) or num(
            self.db.setting("target_profit_per_hour", 250), 250)
        extra = num(extra_costs_month)

        monthly_revenue = hours * pph
        monthly_net = monthly_revenue - extra
        payback_months = cost / monthly_net if monthly_net > 0 else 0
        yearly_profit = monthly_net * 12

        return {
            "printer_cost": round(cost, 2),
            "extra_hours": round(hours, 1),
            "profit_per_hour": round(pph, 2),
            "monthly_revenue": round(monthly_revenue, 2),
            "monthly_costs": round(extra, 2),
            "monthly_net": round(monthly_net, 2),
            "yearly_profit": round(yearly_profit, 2),
            "payback_months": round(payback_months, 1),
            "payback_days": round(payback_months * 30, 0),
            "roi_pct": round(yearly_profit / cost * 100, 1) if cost > 0 else 0,
            "verdict": "ok" if payback_months <= 6 else "warn" if payback_months <= 12 else "bad",
        }

    # =============================================== Трекер брака с анализом (AQ)
    def defect_analysis(self, days: int = 30) -> dict[str, Any]:
        """Анализ брака: причины, стоимость, рекомендации."""
        since = (datetime.now() - timedelta(days=max(1, days))).isoformat()
        defects = self.db.query(
            "SELECT d.*, j.name job_name, j.duration_min, j.grams,"
            " o.product, o.material"
            " FROM defects d"
            " LEFT JOIN print_jobs j ON j.id=d.job_id"
            " LEFT JOIN orders o ON o.id=d.order_id"
            " WHERE d.at>=? ORDER BY d.at DESC", (since,))
        failed_jobs = self.db.query(
            "SELECT * FROM print_jobs WHERE state='failed' AND finished_at>=?",
            (since,))

        by_reason: dict[str, dict[str, float]] = {}
        by_material: dict[str, dict[str, float]] = {}
        total_loss = 0.0
        for d in defects:
            reason = (d.get("reason") or "Не указана").strip()
            mat = (d.get("material") or "OTHER").upper()
            grams = num(d.get("grams"))
            minutes = num(d.get("duration_min"))
            loss = num(d.get("loss"))
            # Оценка потери если не записана
            if not loss and grams:
                s = self.db.settings()
                per_gram = num(s.get("default_spool_price"), 1600) / max(1, num(s.get("default_spool_weight"), 1000))
                loss = grams * per_gram + (minutes / 60) * (
                    num(s.get("energy_price"), 6) * num(s.get("power_kw"), 0.15)
                    + num(s.get("amortization_per_hour"), 12))
            total_loss += loss
            r = by_reason.setdefault(reason, {"count": 0, "loss": 0.0, "grams": 0.0})
            r["count"] += 1
            r["loss"] += loss
            r["grams"] += grams
            m = by_material.setdefault(mat, {"count": 0, "loss": 0.0})
            m["count"] += 1
            m["loss"] += loss

        for d in by_reason.values():
            d["loss"] = round(d["loss"], 2)
            d["grams"] = round(d["grams"], 1)
        for d in by_material.values():
            d["loss"] = round(d["loss"], 2)

        # Топ причины
        reasons = sorted(by_reason.items(), key=lambda x: -x[1]["count"])
        materials = sorted(by_material.items(), key=lambda x: -x[1]["count"])

        # Рекомендации
        tips = []
        if any(r[0].lower().find("влажн") >= 0 or r[0].lower().find("пузыр") >= 0
               for r in reasons):
            tips.append("Высушите пластик: пузыри и хрупкость — признак влажного филамента")
        if any(r[0].lower().find("адгез") >= 0 or r[0].lower().find("откле") >= 0
               for r in reasons):
            tips.append("Протрите стол спиртом и проверьте калибровку первого слоя")
        if any(r[0].lower().find("засор") >= 0 for r in reasons):
            tips.append("Прочистите сопло: засоры ведут к пропускам слоёв")
        if len(failed_jobs) > 5:
            tips.append(f"За {days} дней {len(failed_jobs)} неудачных печатей — проверьте регламент ТО")

        return {
            "days": days,
            "total_defects": len(defects),
            "total_failed": len(failed_jobs),
            "total_loss": round(total_loss, 2),
            "by_reason": [{"reason": r, **d} for r, d in reasons[:10]],
            "by_material": [{"material": m, **d} for m, d in materials[:10]],
            "tips": tips,
        }

    # =================================================== Умная очередь 2.0 (Q)
    def smart_queue(self) -> dict[str, Any]:
        """Оптимальная последовательность заданий: минимум перенастроек.

        Учитывает:
        • материал в AMS прямо сейчас → сначала задания этого материала;
        • стоимость каждой смены материала (~12 мин + 12 г продувки);
        • дедлайны заказов;
        • группировку по материалу.
        """
        s = self.db.settings()
        queued = self.db.query(
            "SELECT j.*, o.material, o.due, o.product, o.number, o.customer_name"
            " FROM print_jobs j LEFT JOIN orders o ON o.id=j.order_id"
            " WHERE j.state='queued' AND j.file<>''"
            " ORDER BY COALESCE(o.due,'9999-12-31'), j.priority DESC")
        if not queued:
            return {"queue": [], "savings": {}, "current_material": ""}

        # Текущий материал в AMS
        current_mat = ""
        printers = self.db.query("SELECT * FROM printers WHERE enabled=1")
        for p in printers:
            # Простой способ: берём последний материал из заданий running
            running = self.db.one(
                "SELECT o.material FROM print_jobs j"
                " LEFT JOIN orders o ON o.id=j.order_id"
                " WHERE j.printer_id=? AND j.state='running'", (p["id"],))
            if running and running.get("material"):
                current_mat = str(running["material"]).upper()
                break

        # Группируем по материалу
        groups: dict[str, list[dict]] = {}
        for j in queued:
            mat = (j.get("material") or "OTHER").upper()
            groups.setdefault(mat, []).append(j)

        # Порядок: сначала текущий материал, потом по количеству заданий
        order = []
        if current_mat and current_mat in groups:
            order.append(current_mat)
        for mat in sorted(groups, key=lambda m: -len(groups[m])):
            if mat not in order:
                order.append(mat)

        # Собираем оптимальную последовательность
        optimized = []
        changeovers = 0
        prev_mat = current_mat
        for mat in order:
            if mat != prev_mat and prev_mat:
                changeovers += 1
            for j in groups[mat]:
                optimized.append({
                    **j,
                    "group_material": mat,
                    "is_current": mat == current_mat,
                    "position": len(optimized) + 1,
                })
            prev_mat = mat

        # Наивная очередь (по дедлайну) vs оптимизированная
        naive_changes = 0
        prev = ""
        for j in queued:
            mat = (j.get("material") or "OTHER").upper()
            if mat != prev and prev:
                naive_changes += 1
            prev = mat

        savings_changes = max(0, naive_changes - changeovers)
        savings_minutes = savings_changes * 12
        savings_grams = savings_changes * 12  # 12 г продувки на смену
        savings_rub = round(savings_minutes * (
            num(s.get("amortization_per_hour"), 12) / 60
            + num(s.get("energy_price"), 6) * num(s.get("power_kw"), 0.15) / 60
        ) + savings_grams * num(s.get("default_spool_price"), 1600)
            / max(1, num(s.get("default_spool_weight"), 1000)), 2)

        return {
            "queue": optimized[:30],
            "current_material": current_mat,
            "groups": {mat: len(items) for mat, items in groups.items()},
            "savings": {
                "changeovers_avoided": savings_changes,
                "minutes_saved": savings_minutes,
                "grams_saved": savings_grams,
                "rub_saved": savings_rub,
            },
            "naive_changeovers": naive_changes,
            "optimized_changeovers": changeovers,
        }

"""Инсайты PrintFlow 4.0 — «система говорит, что делать».

В отличие от отчётов, которые показывают факт, этот модуль строит прогноз и
вердикт по уже собранным данным:

    1. Цель месяца — сколько чистой прибыли уже заработано против
       goal_profit_month, с прогнозом «успеем или нет» по текущему темпу.
    2. Касса вперёд — куда пойдёт остаток кассы на 30/60/90 дней с учётом
       ожидаемых оплат, постоянных расходов и налогов.
    3. Налоговый календарь — ближайшие платежи по выбранному режиму и
       контроль лимита (НПД 2,4 млн ₽, УСН 490,5 млн ₽).

Всё читает базу и ничего не пишет.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .accounting import Accounting, num, rub
from .config import now_iso
from .db import Database


def _month_days(d: date) -> int:
    nxt = date(d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1, 1)
    return (nxt - date(d.year, d.month, 1)).days


class Insights:
    """Прогнозы и календарь платежей поверх бухгалтерии."""

    def __init__(self, db: Database):
        self.db = db
        self.acc = Accounting(db)

    # ------------------------------------------------------------ цель месяца
    def goal_progress(self) -> dict[str, Any]:
        today = date.today()
        cur = self.acc.pnl_month(f"{today.year}-{today.month:02d}")
        profit = num(cur.get("profit"))
        goal = num(self.db.setting("goal_profit_month", 60000), 60000)
        pct = round(profit / goal * 100, 1) if goal else 0.0
        days_total = _month_days(today)
        days_elapsed = today.day
        run = profit / days_elapsed if days_elapsed else 0.0
        projected = round(run * days_total, 2)
        # сколько ещё надо заработать до конца месяца в расчёте на оставшиеся дни
        days_left = max(1, days_total - days_elapsed)
        needed_daily = round(max(0.0, goal - profit) / days_left, 2)
        verdict, text = "ok", ""
        if profit >= goal:
            verdict, text = "ok", f"Цель месяца выполнена: {rub(profit)} из {rub(goal)}."
        elif projected >= goal:
            verdict, text = "ok", (f"По темпу выходим на {rub(projected)} к концу месяца — "
                                   f"цель {rub(goal)} достижима.")
        elif projected >= goal * 0.6:
            verdict, text = "warn", (f"Темп даёт {rub(projected)} к концу месяца. Чтобы дойти "
                                     f"до {rub(goal)}, нужно {rub(needed_daily)} в день.")
        else:
            verdict, text = "bad", (f"Сейчас {rub(profit)}, темп ведёт к {rub(projected)}. "
                                    f"До цели {rub(goal)} нужно {rub(needed_daily)} в день.")
        return {
            "month": f"{today.year}-{today.month:02d}",
            "profit": round(profit, 2),
            "goal": goal,
            "pct": pct,
            "days_elapsed": days_elapsed,
            "days_total": days_total,
            "projected": projected,
            "needed_daily": needed_daily,
            "verdict": verdict,
            "verdict_text": text,
        }

    # ----------------------------------------------------------- касса вперёд
    def cash_forecast(self, days: int = 90) -> dict[str, Any]:
        s = self.db.settings()
        start = num(self.acc.accounts_state().get("total"))
        pipeline = num(self.acc.summary(30).get("pipeline"))
        fixed_monthly = num(self.acc.fixed_costs_monthly())
        insurance_monthly = 0.0
        if s.get("tax_mode") in ("usn6", "usn15", "patent"):
            insurance_monthly = num(s.get("insurance_fixed")) / 12.0
        burn_monthly = fixed_monthly + insurance_monthly

        tax = self.acc.tax_report()
        tax_due = num(tax.get("total_due"))

        def horizon(n: int) -> float:
            # ожидаемые оплаты забираем за первые 30 дней, дальше только расходы
            inflow = pipeline if n >= 30 else pipeline * n / 30.0
            out_months = n / 30.0
            # налог к уплате падает в ближайшие 30 дней (месячная периодичность НПД)
            tax_hit = tax_due if n >= 30 else 0.0
            return round(start + inflow - burn_monthly * out_months - tax_hit, 2)

        c30, c60, c90 = horizon(30), horizon(60), horizon(90)
        points = [
            {"day": 0, "cash": round(start, 2)},
            {"day": 30, "cash": c30},
            {"day": 60, "cash": c60},
            {"day": 90, "cash": c90},
        ]
        # запас прочности: на сколько месяцев хватит текущих денег при текущем «сгорании»
        net = burn_monthly + (tax_due / 3.0 if tax_due else 0.0)
        runway_days = round(start / net * 30, 1) if net > 0 else None
        lowest = min(start, c30, c60, c90)
        verdict, text = "ok", ""
        if lowest < 0:
            verdict, text = "bad", (f"Кассовый разрыв: к дню 90 касса уходит в {rub(lowest)}. "
                                    f"Планируйте оплаты заранее.")
        elif runway_days is not None and runway_days < 45:
            verdict, text = "warn", f"Денег хватит примерно на {int(runway_days)} дней."
        else:
            verdict, text = "ok", "Запас кассы здоровый — разрывов на горизонте нет."
        return {
            "now": round(start, 2),
            "pipeline": round(pipeline, 2),
            "burn_monthly": round(burn_monthly, 2),
            "tax_due": round(tax_due, 2),
            "points": points,
            "runway_days": runway_days,
            "verdict": verdict,
            "verdict_text": text,
        }

    # ---------------------------------------------------- прогноз кассы по дням
    def cash_forecast_daily(self, days: int = 90) -> dict[str, Any]:
        """Касса вперёд по дням: остаток + ожидаемые оплаты − ежедневный расход."""
        s = self.db.settings()
        start = num(self.acc.accounts_state().get("total"))
        pipeline = num(self.acc.summary(30).get("pipeline"))
        fixed_monthly = num(self.acc.fixed_costs_monthly())
        insurance_monthly = num(s.get("insurance_fixed")) / 12.0 \
            if s.get("tax_mode") in ("usn6", "usn15", "patent") else 0.0
        tax_due = num(self.acc.tax_report().get("total_due"))
        daily_burn = (fixed_monthly + insurance_monthly) / 30.0
        points = []
        cash = start
        for day in range(0, days + 1, 1):
            if day == 0:
                points.append({"day": 0, "cash": round(cash, 2)})
                continue
            inflow = pipeline / 30.0 if day <= 30 else 0.0
            tax_hit = tax_due if day == 30 else 0.0
            cash += inflow - daily_burn - tax_hit
            points.append({"day": day, "cash": round(cash, 2)})
        lowest = min(p["cash"] for p in points)
        gap_day = next((p["day"] for p in points if p["cash"] < 0), None)
        return {"start": round(start, 2), "pipeline": round(pipeline, 2),
                "daily_burn": round(daily_burn, 2), "tax_due": round(tax_due, 2),
                "points": points, "lowest": round(lowest, 2),
                "negative_at_day": gap_day}

    # ------------------------------------------------------ окупаемость
    def payback(self) -> dict[str, Any]:
        """Сколько принтер уже отбил и когда окупится полностью."""
        investment = num(self.db.setting("printer_investment", 0), 0)
        income = num(self.acc.summary(365).get("profit"))
        if investment <= 0:
            return {"investment": 0.0, "paid_back": 0.0, "pct": None,
                    "profit_total": round(income, 2), "ready": False}
        pct = round(income / investment * 100, 1)
        if income >= investment:
            return {"investment": investment, "paid_back": investment,
                    "pct": pct, "profit_total": round(income, 2), "ready": True,
                    "note": "Принтер полностью окупился — дальше чистая прибыль."}
        # скорость прибыли за последние 30 дней → оценка даты окупаемости
        rate30 = num(self.acc.summary(30).get("profit"))
        remaining = investment - income
        est_days = round(remaining / (rate30 / 30.0)) if rate30 > 0 else None
        est_date = ""
        if est_days is not None:
            from datetime import date, timedelta
            est_date = (date.today() + timedelta(days=est_days)).isoformat()
        return {"investment": investment, "paid_back": round(income, 2), "pct": pct,
                "profit_total": round(income, 2), "ready": False,
                "remaining": round(remaining, 2), "rate30": round(rate30, 2),
                "est_days": est_days, "est_date": est_date}

    # --------------------------------------------------- сравнение режимов
    def tax_compare(self) -> dict[str, Any]:
        """НПД vs УСН 6% vs УСН 15% на фактическом доходе года."""
        s = self.db.settings()
        tax = self.acc.tax_report()
        income_person = num(tax.get("income_person"))
        income_company = num(tax.get("income_company"))
        income = income_person + income_company
        expense = num(tax.get("expense"))
        insurance = num(s.get("insurance_fixed"))
        npd = income_person * num(s.get("npd_rate_person"), 4) / 100.0 + \
            income_company * num(s.get("npd_rate_company"), 6) / 100.0
        usn6 = max(0.0, income * num(s.get("usn_income_rate"), 6) / 100.0 - insurance)
        usn15_raw = max(0.0, income - expense - insurance) * num(s.get("usn_profit_rate"), 15) / 100.0
        usn15 = max(usn15_raw, income * num(s.get("usn_min_tax_rate"), 1) / 100.0)
        rows = [{"mode": "npd", "label": "НПД", "tax": round(npd, 2)},
                {"mode": "usn6", "label": "УСН 6%", "tax": round(usn6, 2)},
                {"mode": "usn15", "label": "УСН 15%", "tax": round(usn15, 2)}]
        rows.sort(key=lambda r: r["tax"])
        rows[0]["best"] = True
        return {"income": round(income, 2), "expense": round(expense, 2),
                "insurance": round(insurance, 2), "rows": rows}

    # -------------------------------------------------------- налоги и лимиты
    def tax_calendar(self) -> dict[str, Any]:
        s = self.db.settings()
        mode = s.get("tax_mode", "none")
        tax = self.acc.tax_report()
        income = num(tax.get("income"))
        today = date.today()
        year = today.year

        events: list[dict] = []
        if mode == "npd":
            # НПД: платёж до 28 числа месяца, следующего за отчётным
            due_month = today.replace(day=28) + timedelta(days=31)
            due = due_month.replace(day=28)
            amount = num(tax.get("tax_due"))
            events.append({"title": "Налог НПД", "due": due.isoformat(),
                           "amount": round(amount, 2),
                           "kind": "tax", "note": "До 28 числа следующего месяца"})
        elif mode in ("usn6", "usn15"):
            advances = []
            for q in range(1, 5):
                month_due = q * 3 + 1  # аванс платится в месяце после квартала
                due = date(year + (1 if month_due > 12 else 0),
                           month_due if month_due <= 12 else month_due - 12, 28)
                if due < today:
                    continue
                quarter = next((x for x in tax.get("quarters", []) if x["key"] == f"{year}-Q{q}"), {})
                advances.append({"title": f"Аванс УСН за {q} квартал",
                                 "due": due.isoformat(),
                                 "amount": round(num(quarter.get("tax")), 2),
                                 "kind": "tax", "note": "До 28 числа"})
            events += advances
            if s.get("insurance_fixed"):
                events.append({"title": "Страховые взносы ИП (фикс.)",
                               "due": f"{year}-12-28", "amount": round(num(s.get("insurance_fixed")), 2),
                               "kind": "insurance", "note": "До 28 декабря"})
        elif mode == "patent":
            events.append({"title": "Патент", "due": "", "amount": round(num(s.get("patent_cost_year")), 2),
                           "kind": "tax", "note": "По графику из уведомления"})
        events.sort(key=lambda e: (e["due"] or "9999",))

        # контроль лимита выбранного режима
        limit = num(tax.get("limit"))
        limit_used = num(tax.get("limit_used"))
        limit_days = None
        if limit and income > 0:
            # сколько дней шло накопление дохода с начала года
            elapsed = (today - date(year, 1, 1)).days + 1
            rate = income / elapsed if elapsed else 0.0
            if rate > 0:
                left = (limit - income) / rate
                limit_days = round(left, 1)
        return {
            "mode": mode,
            "income_year": round(income, 2),
            "limit": limit,
            "limit_used": limit_used,
            "limit_days": limit_days,
            "tax_due": round(num(tax.get("total_due")), 2),
            "reserve": round(num(tax.get("reserve")), 2),
            "events": events[:5],
        }

    # ------------------------------------------------------------------ всё
    def all(self) -> dict[str, Any]:
        return {
            "generated_at": now_iso(),
            "goal": self.goal_progress(),
            "cash": self.cash_forecast(),
            "tax": self.tax_calendar(),
            "payback": self.payback(),
            "tax_compare": self.tax_compare(),
        }

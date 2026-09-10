"""НПД-контур: годовой лимит как живой счётчик и контроль «чеки выбиты».

Зачем. Два риска, которые не видны ни в кассе, ни в «Налогах» по отдельности:

* чек. Самозанятый обязан выдать чек на каждый расчёт с покупателем
  (422-ФЗ, ст. 14, п. 12): штраф за работу без чека — 20% от суммы, а
  повторно в течение полугода — 100% суммы расчёта. «Потом соберусь и
  выбью пачкой» — это и есть «потом», за которое платят;
* лимит 2,4 млн ₽ в год. Пересечение означает потерю режима НПД задним
  числом по всем расчётам года, поэтому «узнал в декабре» слишком поздно.

Как. Второго журнала сумм не заводим: доходы и так лежат в ``transactions``
(локальная база — единственный источник правды). Здесь считаются производные
(остаток лимита, темп, прогноз, «сколько можно в день»), а от владельца
нужна только короткая отметка по дню: «чеки выбиты, N штук». Отметка
сверяется с проводками дня и не даёт подтвердить меньше, чем взяли, —
иначе это самообман с зелёной галкой.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .accounting import TAX_MODES, num

LEVEL_NAMES = {"ok": "в норме", "watch": "следите", "warn": "лимит близко",
               "over": "лимит выбран"}


def _day(value: Any = "") -> date:
    """Дата из строки/датETIME; пустое и непонятное — сегодня."""
    text = str(value or "").strip()[:10]
    if not text:
        return date.today()
    try:
        return date.fromisoformat(text)
    except ValueError:
        return date.today()


def _rub(value: Any) -> str:
    """Сумма словами: «2 395 700 ₽», а не «2.3957e+06 ₽» — до `:g` договор не дошёл."""
    n = round(num(value), 2)
    if abs(n) >= 1000:
        return f"{n:,.0f}".replace(",", " ")
    return f"{n:g}"

def _int(value: Any) -> int:
    try:
        return max(0, int(num(value)))
    except (TypeError, ValueError):
        return 0


class Npd:
    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------- состояние
    def status(self, day: date | None = None) -> dict:
        """Лимит налогового режима: остаток, темп, прогноз и дневной бюджет.

        Считается по тем же проводкам, что и «Налоги» (доход года за вычетом
        возвратов), поэтому разойтись с налоговым отчётом не может по
        построению. Прогноз даты — ориентир по фактическому среднему темпу
        (все дни года, включая простые), а не обещание.
        """
        s = self.db.settings()
        mode = str(s.get("tax_mode") or "none")
        today = day or date.today()
        year = today.year
        limit = num(s.get("npd_limit"), 2400000) if mode == "npd" \
            else (num(s.get("usn_limit")) if mode in ("usn6", "usn15") else 0.0)
        row = self.db.one(
            "SELECT COALESCE(SUM(CASE WHEN kind='income' AND taxable=1 THEN amount END),0) AS income,"
            " COALESCE(SUM(CASE WHEN kind='expense' AND category='refund' THEN amount END),0) AS refunds"
            " FROM transactions WHERE at>=? AND at<?",
            (f"{year}-01-01", f"{year + 1}-01-01")) or {}
        income = max(0.0, round(num(row.get("income")) - num(row.get("refunds")), 2))
        out: dict[str, Any] = {"mode": mode, "year": year, "income": income,
                               "limit": round(limit, 2),
                               "npd": mode == "npd" and bool(limit)}
        out["mode_name"] = TAX_MODES.get(mode, mode)
        if not limit:
            out.update({"left": 0.0, "used_pct": 0.0, "level": "ok",
                        "level_name": LEVEL_NAMES["ok"], "days_left": 0,
                        "day_budget": 0.0, "run_rate": 0.0, "exhausted_on": ""})
            return out
        left = round(max(0.0, limit - income), 2)
        used_pct = round(min(999.0, income / limit * 100), 1)
        days_left = max(0, (date(year, 12, 31) - today).days)
        days_passed = max(1, (today - date(year, 1, 1)).days + 1)
        run_rate = round(income / days_passed, 2)
        warn_at = round(num(s.get("npd_limit_warn_pct"), 90), 1)
        level = ("over" if used_pct >= 100 else
                 "warn" if used_pct >= warn_at else
                 "watch" if used_pct >= max(0.0, warn_at - 15) else "ok")
        exhausted_on = ""
        if run_rate > 0 and left > 0:
            eta = today + timedelta(days=int(left // run_rate))
            if eta.year == year:
                exhausted_on = eta.isoformat()
        out.update({"left": left, "used_pct": used_pct, "level": level,
                    "level_name": LEVEL_NAMES[level], "days_left": days_left,
                    "day_budget": round(left / max(1, days_left), 2),
                    "run_rate": run_rate, "exhausted_on": exhausted_on,
                    "warn_at": warn_at,
                    "alerts": bool(s.get("npd_alerts_enabled", True))})
        return out

    # ------------------------------------------------------- деньги по дням
    def day_income(self, day: Any = "") -> dict:
        """Что взято с людей за день: сумма, из них от юрлиц, число проводок."""
        d = _day(day)
        row = self.db.one(
            "SELECT COALESCE(SUM(amount),0) AS total,"
            " COALESCE(SUM(CASE WHEN payer='company' THEN amount END),0) AS company,"
            " COUNT(*) AS docs FROM transactions"
            " WHERE kind='income' AND taxable=1 AND date(at)=date(?)",
            (d.isoformat(),)) or {}
        return {"day": d.isoformat(), "income": round(num(row.get("total")), 2),
                "company": round(num(row.get("company")), 2),
                "docs": _int(row.get("docs"))}

    def days(self, back: int = 14) -> list[dict]:
        """Последние дни: сколько взяли и подтверждены ли чеки."""
        today = date.today()
        out: list[dict] = []
        for offset in range(max(1, _int(back) or 14)):
            day = today - timedelta(days=offset)
            data = self.day_income(day)
            mark = self.db.one("SELECT * FROM npd_days WHERE day=?", (day.isoformat(),))
            data["checks"] = _int((mark or {}).get("checks"))
            data["marked_amount"] = round(num((mark or {}).get("amount")), 2)
            data["marked_at"] = str((mark or {}).get("marked_at") or "")
            data["marked_by"] = str((mark or {}).get("marked_by") or "")
            data["note"] = str((mark or {}).get("note") or "")
            data["marked"] = bool(mark)
            data["gap"] = round(data["income"] - data["marked_amount"], 2) if mark \
                else data["income"]
            # сегодняшний день ещё не просрочен: чек по расчётам дня выдаётся
            # до его конца, требовать его с утра — шуметь понапрасну
            data["overdue"] = bool(data["income"]) and not mark \
                and day < today
            out.append(data)
        return out

    def pending(self, lookback: int = 31) -> dict:
        """Сколько дней с деньгами остались без подтверждения чеков."""
        need = [r for r in self.days(lookback) if r["overdue"]]
        return {"days": len(need),
                "amount": round(sum(num(r["income"]) for r in need), 2),
                "list": need[:10],
                "fine_min": round(sum(num(r["income"]) for r in need) * 0.2, 2),
                "fine_max": round(sum(num(r["income"]) for r in need), 2)}

    # ------------------------------------------------------------ отметки
    def mark_day(self, day: Any, checks: int = 0, amount: float = 0.0,
                 note: str = "", actor: str = "panel") -> dict:
        """Подтвердить, что чеки по дню выбиты.

        Пустая сумма = «на всю сумму дня». Меньше, чем взяли, подтвердить
        нельзя без причины: иначе галка есть, а чеков нет.
        """
        d = _day(day)
        if d > date.today():
            raise ValueError("Отметить можно прошедший или сегодняшний день")
        got = self.day_income(d)
        want = round(num(amount) if num(amount) else got["income"], 2)
        if want <= 0 and got["income"] <= 0:
            raise ValueError("За этот день доходов нет — подтверждать нечего")
        gap = round(got["income"] - want, 2)
        note = str(note or "").strip()[:400]
        if gap > 0.005 and not note:
            raise ValueError(f"Чеками подтверждено {want:g} ₽, а за день взято "
                             f"{got['income']:g} ₽ — не хватает {gap:g} ₽. "
                             "Укажите причину в комментарии")
        stamp = date.today().isoformat()
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO npd_days(day,checks,amount,note,marked_at,marked_by)"
                " VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(day) DO UPDATE SET checks=excluded.checks,"
                " amount=excluded.amount,note=excluded.note,"
                " marked_at=excluded.marked_at,marked_by=excluded.marked_by",
                (d.isoformat(), _int(checks), want, note, stamp,
                 str(actor or "panel")[:120]))
        self.db.add_event(
            "money", "НПД: чеки за день подтверждены",
            f"{d.isoformat()} · {_int(checks)} чек(ов) на {want:g} ₽"
            + (f" · {note}" if note else ""),
            data={"day": d.isoformat(), "checks": _int(checks), "amount": want,
                  "gap": gap, "actor": actor})
        return {"ok": True, "day": d.isoformat(), "checks": _int(checks),
                "amount": want, "income": got["income"], "gap": gap,
                "pending": self.pending()}

    def unmark_day(self, day: Any, actor: str = "panel") -> dict:
        """Снять отметку — нажали не туда или чек аннулирован в приложении."""
        d = _day(day)
        with self.db.transaction():
            self.db.execute("DELETE FROM npd_days WHERE day=?", (d.isoformat(),))
        self.db.add_event("money", "НПД: отметка о чеках снята", d.isoformat(),
                          data={"day": d.isoformat(), "actor": actor})
        return {"ok": True, "day": d.isoformat(), "pending": self.pending()}

    # ------------------------------------------------- подпись для кассира
    def cashier_note(self, extra: float = 0.0) -> dict:
        """Одна строка про лимит для экрана кассы и ответа на продажу.

        Кассир не налоговый инспектор, но именно он принимает деньги: когда
        следующая продажа выбирает лимит режима, это нужно увидеть в момент
        расчёта, а не в отчёте в декабре. Ничего не блокируем — право
        решать, что делать с режимом, у владельца.
        """
        st = self.status()
        if not st["limit"]:
            return {}
        left = round(num(st["left"]) - max(0.0, num(extra)), 2)
        if left <= 0:
            return {"level": "over",
                    "text": f"Лимит {st.get('mode_name') or st['mode']} выбран: "
                            f"{_rub(st['income'])} из {_rub(st['limit'])} ₽ — продолжать "
                            "приём денег только с разрешения владельца, режим слетает"}
        if st["level"] in ("warn", "over"):
            return {"level": "warn",
                    "text": f"До годового лимита осталось {_rub(left)} ₽ "
                            f"({st['used_pct']}% выбрано) — предупредите владельца"}
        if st["level"] == "watch":
            return {"level": "watch", "text": f"Годовой лимит: осталось {_rub(left)} ₽"}
        return {"level": "ok", "text": f"Годовой лимит: осталось {_rub(left)} ₽"}

    # ------------------------------------------------ сводка для «Налогов»
    def summary(self) -> dict:
        """Всё, что нужно панели: лимит + состояние чеков (один запрос)."""
        return {"status": self.status(), "pending": self.pending(31),
                "today": self.day_income(), "level_names": LEVEL_NAMES}

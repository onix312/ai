"""Мастер «Закрыть месяц» (идея H4 из списка идей).

Пять шагов, которые владелец делает раз в месяц и каждый раз вспоминает
порядок заново. Мастер собирает их в один диалог с понятным состоянием:

1. **fixed**  — начислить постоянные расходы месяца (идемпотентно);
2. **cash**   — сверить кассы: факт наличных против остатка системы,
               расхождение пишется проводкой «корректировка кассы»;
3. **tax**    — посчитать налог месяца и отложить его в конверт «Налог»;
4. **report** — сводка месяца (P&L, кассы, долги, налог);
5. **backup** — копия базы файлом + JSON-экспорт в каталог данных.

Каждый шаг фиксируется в настройке ``month_close``, повторный запуск шага
запрещён до следующего месяца — мастер не задвоит проводки.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .accounting import Accounting, month_bounds, month_key, num
from .config import DATA_DIR, now_iso

STEP_ORDER = ["fixed", "cash", "tax", "report", "backup"]

STEP_TITLES = {
    "fixed": "Постоянные расходы",
    "cash": "Сверка касс",
    "tax": "Налог месяца",
    "report": "Сводка месяца",
    "backup": "Копия данных",
}

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _valid_key(key: str) -> str:
    key = (key or "").strip()[:7]
    if not _MONTH_RE.match(key):
        raise ValueError("Месяц в формате ГГГГ-ММ")
    return key


class MonthClose:
    """Пять шагов закрытия месяца, состояние — в настройке month_close."""

    def __init__(self, db):
        self.db = db
        self.acc = Accounting(db)

    # -------------------------------------------------------------- состояние
    def _log(self) -> dict[str, Any]:
        raw = self.db.setting("month_close", {})
        return raw if isinstance(raw, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        self.db.set_settings({"month_close": data})

    def mark(self, key: str, step: str, **extra) -> None:
        log = self._log()
        month = log.setdefault(key, {})
        month[step] = {"at": now_iso(), **extra}
        self._save(log)

    def state(self, key: str = "") -> dict[str, Any]:
        key = _valid_key(key) if key else month_key(now_iso()[:10])
        log = self._log().get(key, {})
        accounts = self.acc.accounts_state()
        return {
            "key": key,
            "titles": STEP_TITLES,
            "order": STEP_ORDER,
            "done": {step: bool(log.get(step)) for step in STEP_ORDER},
            "log": log,
            "accounts": accounts["accounts"],
            "next": next((s for s in STEP_ORDER if not log.get(s)), ""),
        }

    # ---------------------------------------------------------------- шаг 1
    def step_fixed(self, key: str) -> dict[str, Any]:
        key = _valid_key(key)
        if self._log().get(key, {}).get("fixed"):
            return {"ok": False, "done": True, "error": "Постоянные расходы уже начислены"}
        stamp = f"{key}-28T09:00:00"  # день, покрывающий типовые даты начисления
        created = self.acc.run_fixed_costs(today=stamp, force=True)
        self.mark(key, "fixed", count=len(created))
        return {"ok": True, "created": created,
                "message": (f"Начислено расходов: {len(created)}" if created
                            else "Постоянных расходов к начислению не было")}

    # ---------------------------------------------------------------- шаг 2
    def step_cash(self, key: str, facts: list[dict] | None = None) -> dict[str, Any]:
        key = _valid_key(key)
        if self._log().get(key, {}).get("cash"):
            return {"ok": False, "done": True, "error": "Кассы уже сверены"}
        facts = facts or []
        wanted = {str(item.get("id") or ""): num(item.get("fact")) for item in facts}
        current = {str(acc["id"]): num(acc["balance"])
                   for acc in self.acc.accounts_state()["accounts"]}
        fixed: list[dict] = []
        for acc_id, fact in wanted.items():
            if acc_id not in current:
                continue
            diff = round(fact - current[acc_id], 2)
            if abs(diff) < 0.01:
                continue
            name = next((a["name"] for a in self.acc.accounts_state()["accounts"]
                         if a["id"] == acc_id), acc_id)
            row = self.acc.add_transaction(
                "correction", "correction", diff,
                f"Сверка кассы: {name}",
                note=f"Закрытие месяца {key}: было {current[acc_id]:.2f}, факт {fact:.2f}",
                account_id=acc_id, taxable=False, deductible=False,
                at=f"{key}-28T23:59:00")
            fixed.append({"account": name, "diff": diff, "tx": row.get("id")})
        self.mark(key, "cash", adjustments=len(fixed))
        return {"ok": True, "adjustments": fixed,
                "message": (f"Корректировок кассы: {len(fixed)}" if fixed
                            else "Кассы сошлись — корректировки не нужны")}

    # ---------------------------------------------------------------- шаг 3
    def month_tax(self, key: str) -> dict[str, Any]:
        """Оценка налога месяца по фактическим проводкам.

        Точный годовой расчёт с вычетами и лимитами — Финансы → Налоги;
        здесь — рабочая оценка, чтобы отложить деньги в конверт.
        """
        start, end = month_bounds(key)
        rows = self.db.query("SELECT * FROM transactions WHERE at>=? AND at<?",
                             (start, end))
        settings = self.db.settings()
        mode = str(settings.get("tax_mode") or "none")
        gross_person = sum(num(r["amount"]) for r in rows
                           if r["kind"] == "income" and r["taxable"]
                           and r["payer"] != "company")
        gross_company = sum(num(r["amount"]) for r in rows
                            if r["kind"] == "income" and r["taxable"]
                            and r["payer"] == "company")
        gross = gross_person + gross_company
        expense = sum(num(r["amount"]) for r in rows
                      if r["kind"] == "expense" and r["deductible"]
                      and r["category"] not in ("tax", "insurance", "refund"))
        tax = 0.0
        note = "налог по фактическим доходам месяца"
        if mode == "npd":
            tax = (gross_person * num(settings.get("npd_rate_person"), 4)
                   + gross_company * num(settings.get("npd_rate_company"), 6)) / 100.0
        elif mode == "usn6":
            tax = gross * num(settings.get("usn_income_rate"), 6) / 100.0
            note = "аванс УСН «доходы» (взносы уменьшат налог в годовом расчёте)"
        elif mode == "usn15":
            tax = max(0.0, gross - expense) * num(settings.get("usn_profit_rate"), 15) / 100.0
            note = "аванс УСН «доходы − расходы» (предварительно)"
        elif mode == "patent":
            tax = num(settings.get("patent_cost_year")) / 12.0
            note = "1/12 стоимости патента"
        elif mode == "manual":
            tax = gross * num(settings.get("tax_rate")) / 100.0
        return {"mode": mode, "tax": round(tax, 2), "income": round(gross, 2),
                "note": note}

    def step_tax(self, key: str) -> dict[str, Any]:
        key = _valid_key(key)
        if self._log().get(key, {}).get("tax"):
            return {"ok": False, "done": True, "error": "Налог уже отложен"}
        estimate = self.month_tax(key)
        if estimate["tax"] <= 0:
            self.mark(key, "tax", amount=0.0)
            return {"ok": True, "estimate": estimate, "deposited": 0.0,
                    "message": "Налога к уплате за месяц нет (нет облагаемого дохода)"}
        from .envelopes import Envelopes
        envelopes = Envelopes(self.db)
        target = next((e for e in envelopes.list()
                       if "налог" in str(e.get("name") or "").lower()), None)
        if target is None:
            target = envelopes.save({"name": "Налог", "pct": 0})
        envelopes.add_move(target["id"], estimate["tax"],
                           f"Резерв налога за {key} (мастер закрытия месяца)")
        self.mark(key, "tax", amount=estimate["tax"], envelope=target["id"])
        return {"ok": True, "estimate": estimate, "deposited": estimate["tax"],
                "envelope": target["name"],
                "message": f"В конверт «{target['name']}» отложено {estimate['tax']:.2f} ₽"}

    # ---------------------------------------------------------------- шаг 4
    def step_report(self, key: str) -> dict[str, Any]:
        key = _valid_key(key)
        report = {
            "pnl": self.acc.pnl_month(key),
            "cash": self.acc.accounts_state(),
            "debts": self.acc.debts(),
            "tax": self.month_tax(key),
        }
        self.mark(key, "report")
        return {"ok": True, "report": report}

    # ---------------------------------------------------------------- шаг 5
    def step_backup(self, key: str) -> dict[str, Any]:
        key = _valid_key(key)
        if self._log().get(key, {}).get("backup"):
            return {"ok": False, "done": True, "error": "Копия уже сделана"}
        from .db import make_backup
        file_copy = make_backup(f"month-close-{key.replace('-', '')}")
        export_name = ""
        try:
            from .repo import Repo
            export_dir = DATA_DIR / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            export_name = f"printflow-месяц-{key}.json"
            (export_dir / export_name).write_text(
                json.dumps(Repo(self.db).export_all(), ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception:
            export_name = ""
        self.mark(key, "backup", file=file_copy.get("file", ""),
                  export=export_name)
        return {"ok": True, "file": file_copy.get("file", ""),
                "export": export_name,
                "message": "Копия базы и JSON-экспорт сохранены в каталоге данных"}

    # ------------------------------------------------------------- запуск шага
    def run(self, key: str, step: str, payload: dict | None = None) -> dict[str, Any]:
        key = _valid_key(key) if key else month_key(now_iso()[:10])
        if step not in STEP_ORDER:
            raise ValueError("Неизвестный шаг")
        payload = payload or {}
        if isinstance(payload, list):  # удобство: run(key, "cash", [{id, fact}])
            payload = {"accounts": payload}
        if step == "fixed":
            return self.step_fixed(key)
        if step == "cash":
            return self.step_cash(key, payload.get("accounts") or [])
        if step == "tax":
            return self.step_tax(key)
        if step == "report":
            return self.step_report(key)
        return self.step_backup(key)

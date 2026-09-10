"""НПД-контур (17.0.5): годовой лимит как счётчик и контроль «чеки выбиты».

Закрепляем то, за что платят деньгами: чек на каждый расчёт (422-ФЗ, ст. 14)
и лимит 2,4 млн ₽, после которого слетает режим. Суммы намеренно не дублируются
в второй журнал — они берутся из `transactions`, а отметка владельца только
подтверждает, что чеки по дню выданы.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import date, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.npd import Npd  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "npd.sqlite3")


def income(db: Database, amount: float, day: str = "", payer: str = "person",
           kind: str = "income", category: str = "sale", taxable: int = 1) -> str:
    """Проводка дня прямо в журнал: НПД-контур читает только `transactions`."""
    from connector.printflow.accounting import uid
    tx_id = uid("tx")
    db.execute(
        "INSERT INTO transactions(id,at,kind,category,amount,title,payer,taxable,"
        "deductible,auto) VALUES(?,?,?,?,?,?,?,?,?,0)",
        (tx_id, f"{day or date.today().isoformat()}T12:00:00+03:00", kind, category,
         amount, f"Продажа {amount:g}", payer, taxable, 1))
    return tx_id


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.npd = Npd(self.db)

    def tearDown(self):
        self.db.close()

    def test_no_mode_no_counter(self):
        st = self.npd.status()
        self.assertFalse(st["npd"])
        self.assertEqual(st["limit"], 0.0)
        self.assertEqual(st["level"], "ok")

    def test_limit_left_and_budget(self):
        self.db.set_settings({"tax_mode": "npd", "npd_limit": 2400000})
        income(self.db, 2300000, day=date.today().isoformat())
        st = self.npd.status()
        self.assertEqual(st["income"], 2300000.0)
        self.assertEqual(st["left"], 100000.0)
        self.assertGreater(st["used_pct"], 95)
        self.assertEqual(st["level"], "warn")          # порог 90% по умолчанию
        self.assertGreater(st["day_budget"], 0)
        self.assertTrue(st["exhausted_on"])            # прогноз даты при этом темпе

    def test_over_limit_is_loud(self):
        self.db.set_settings({"tax_mode": "npd", "npd_limit": 100000})
        income(self.db, 120000, day=date.today().isoformat())
        st = self.npd.status()
        self.assertEqual(st["level"], "over")
        self.assertEqual(st["left"], 0.0)

    def test_refunds_leave_the_base(self):
        """Возврат покупателю уменьшает доход года — иначе налог бы завысился."""
        self.db.set_settings({"tax_mode": "npd", "npd_limit": 2400000})
        income(self.db, 10000, day=date.today().isoformat())
        income(self.db, 4000, day=date.today().isoformat(), kind="expense",
               category="refund")
        self.assertEqual(self.npd.status()["income"], 6000.0)


class DayLedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.npd = Npd(self.db)
        self.db.set_settings({"tax_mode": "npd"})
        self.yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.today = date.today().isoformat()

    def tearDown(self):
        self.db.close()

    def _mark(self, day: str, checks: int = 2, **kw):
        return self.npd.mark_day(day, checks=checks, **kw)

    def test_day_income_counts_only_taxable_incomes(self):
        income(self.db, 1500, day=self.today)
        income(self.db, 900, day=self.today, payer="company")
        income(self.db, 500, day=self.today, kind="expense", category="materials",
               taxable=0)
        data = self.npd.day_income(self.today)
        self.assertEqual(data["income"], 2400.0)
        self.assertEqual(data["company"], 900.0)
        self.assertEqual(data["docs"], 2)

    def test_unmarked_day_with_money_is_overdue_from_tomorrow(self):
        income(self.db, 1000, day=self.yesterday)
        income(self.db, 700, day=self.today)
        pend = self.npd.pending()
        self.assertEqual(pend["days"], 1)             # сегодняшний день ещё не просрочен
        self.assertEqual(pend["amount"], 1000.0)
        self.assertEqual(pend["fine_min"], 200.0)     # 20% от суммы расчёта
        self.assertEqual(pend["fine_max"], 1000.0)    # повторно — 100%

    def test_mark_defaults_to_the_day_income(self):
        income(self.db, 1000, day=self.yesterday)
        out = self._mark(self.yesterday)
        self.assertEqual(out["amount"], 1000.0)
        self.assertEqual(out["gap"], 0.0)
        self.assertEqual(self.npd.pending()["days"], 0)

    def test_undermark_needs_a_reason(self):
        income(self.db, 1000, day=self.yesterday)
        with self.assertRaisesRegex(ValueError, "не хватает"):
            self._mark(self.yesterday, amount=400)
        # причина есть — отметка проходит, расхождение остаётся видно
        out = self._mark(self.yesterday, amount=400, note="600 ₽ вернули наличными")
        self.assertEqual(out["gap"], 600.0)

    def test_future_day_rejected(self):
        with self.assertRaisesRegex(ValueError, "прошедший или сегодняшний"):
            self._mark((date.today() + timedelta(days=3)).isoformat())

    def test_unmark_returns_the_day_to_the_queue(self):
        income(self.db, 800, day=self.yesterday)
        self._mark(self.yesterday)
        self.npd.unmark_day(self.yesterday)
        self.assertEqual(self.npd.pending()["days"], 1)

    def test_remark_overwrites_instead_of_doubling(self):
        income(self.db, 800, day=self.yesterday)
        self._mark(self.yesterday, checks=1)
        self._mark(self.yesterday, checks=3)
        rows = self.db.query("SELECT * FROM npd_days")
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["checks"]), 3)

    def test_days_list_flags_marks_and_gaps(self):
        income(self.db, 500, day=self.yesterday)
        days = {d["day"]: d for d in self.npd.days(7)}
        self.assertTrue(days[self.yesterday]["overdue"])
        self._mark(self.yesterday, checks=1)
        days = {d["day"]: d for d in self.npd.days(7)}
        row = days[self.yesterday]
        self.assertTrue(row["marked"])
        self.assertEqual(row["gap"], 0.0)
        self.assertFalse(row["overdue"])


class CashierNoteTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.npd = Npd(self.db)
        self.db.set_settings({"tax_mode": "npd", "npd_limit": 900})

    def tearDown(self):
        self.db.close()

    def test_quiet_while_far_from_limit(self):
        income(self.db, 100, day=date.today().isoformat())
        note = self.npd.cashier_note()
        self.assertEqual(note["level"], "ok")
        self.assertIn("осталось 800", note["text"])

    def test_warn_before_the_sale_and_over_after(self):
        income(self.db, 850, day=date.today().isoformat())   # 94% из 900 ₽
        self.assertEqual(self.npd.cashier_note()["level"], "warn")
        # «что будет, если пробить ещё 100» — кассир видит исход заранее
        after = self.npd.cashier_note(extra=100)
        self.assertEqual(after["level"], "over")
        self.assertIn("только с разрешения", after["text"])

    def test_big_numbers_stay_readable(self):
        """«2 395 000 ₽», а не «2.3957e+06 ₽»: строку читает кассир за прилавком."""
        self.db.set_settings({"tax_mode": "npd", "npd_limit": 2400000})
        income(self.db, 5000, day=date.today().isoformat())
        text = self.npd.cashier_note()["text"]
        self.assertIn("2 395 000", text)
        self.assertNotIn("e+", text)

    def test_mode_name_is_human(self):
        """В подсказке — «Самозанятый (НПД)», а не технический ключ `npd`."""
        self.assertIn("НПД", self.npd.status()["mode_name"])
        self.db.set_settings({"npd_limit": 100})
        income(self.db, 100, day=date.today().isoformat())
        note = self.npd.cashier_note()
        self.assertEqual(note["level"], "over")
        self.assertIn("Самозанятый (НПД)", note["text"])

    def test_no_mode_no_nag(self):
        self.db.set_settings({"tax_mode": "none"})
        self.assertEqual(self.npd.cashier_note(), {})


class WiringTests(unittest.TestCase):
    """Контур должен быть виден там, где на него смотрят: касса и «Налоги»."""

    def test_routes_registered(self):
        import connector.printflow.routes_npd  # noqa: F401  — регистрация
        from connector.printflow.router import router
        paths = {getattr(r, "path", "") for r in router.routes()}
        for path in ("/api/npd/status", "/api/npd/days", "/api/npd/day/mark",
                     "/api/npd/day/unmark"):
            self.assertIn(path, paths)

    def test_tax_report_carries_the_npd_block(self):
        db = make_db()
        try:
            db.set_settings({"tax_mode": "npd", "npd_limit": 2400000})
            income(db, 3000, day=date.today().isoformat())
            rep = Accounting(db).tax_report()
            self.assertIn("npd", rep)
            self.assertEqual(rep["npd"]["status"]["income"], 3000.0)
            self.assertIn("pending", rep["npd"])
        finally:
            db.close()

    def test_cashier_sees_limit_in_catalog_and_after_sale(self):
        from connector.printflow.cashier import Cashier
        from connector.printflow.shelf import Shelf
        db = make_db()
        try:
            db.set_settings({"tax_mode": "npd", "npd_limit": 900, "cashier_code": "1234"})
            db.upsert("shelf_items", {"id": "s1", "name": "Адресник", "qty": 10,
                                       "price": 900, "cost_per_unit": 100, "active": 1})
            acc = Accounting(db)
            cashier = Cashier(db, acc, Shelf(db))
            token = cashier.login("1234")["token"]
            self.assertEqual(cashier.catalog()["npd"]["level"], "ok")
            # продажа на 900 ₽ выбирает лимит 900 ₽ до нуля — это «over»
            out = cashier.sell([{"item_id": "s1", "qty": 1}], "cash", token)
            self.assertEqual(out["npd"]["level"], "over")
            self.assertIn("Лимит", out["npd"]["text"])
        finally:
            db.close()

    def test_panel_markup_has_the_npd_card(self):
        page = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
        for needle in ('id="npd_card"', 'id="npd_box"', 'id="npd_mark_today"',
                       "Чеки НПД и лимит года", "422-ФЗ"):
            self.assertIn(needle, page, needle)
        script = (ROOT / "site" / "assets" / "finance.js").read_text(encoding="utf-8")
        for needle in ("renderNpd", "npdBoxHtml", "npdDaysHtml", "/api/npd/day/mark",
                       "/api/npd/days", "state-badge bad"):
            self.assertIn(needle, script, needle)
        # страница кассы: строка о лимите и реакция на продажу
        cash = (ROOT / "site" / "cashier.html").read_text(encoding="utf-8")
        for needle in ("state.npd", "npdLine", "r.npd&&r.npd.text"):
            self.assertIn(needle, cash, needle)


if __name__ == "__main__":
    unittest.main()

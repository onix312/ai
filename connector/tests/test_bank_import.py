"""Импорт банковской выписки (M1): разбор CSV, правила, дубли, проведение."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import bank_import  # noqa: E402
from connector.printflow.db import Database  # noqa: E402

CSV_SAMPLE = """Дата;Сумма;Назначение
2026-08-01;1500,00;Перевод от Иванова за адресник
2026-08-02;-2000,00;Оплата пластика PETG в магазине
2026-08-03;-59,00;Комиссия за обслуживание счета
2026-08-04;300,00;Что-то непонятное
"""


class ParseTests(unittest.TestCase):
    def test_parse_detects_columns_and_amounts(self):
        rows = bank_import.parse_csv(CSV_SAMPLE)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["amount"], 1500.0)
        self.assertEqual(rows[1]["amount"], -2000.0)
        self.assertEqual(rows[1]["date"], "2026-08-02")
        self.assertIn("PETG", rows[1]["description"])

    def test_parse_english_columns(self):
        text = "Date,Amount,Description\n2026-08-05,-100,Internet\n"
        rows = bank_import.parse_csv(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], -100)

    def test_parse_empty_and_garbage(self):
        self.assertEqual(bank_import.parse_csv(""), [])
        self.assertEqual(bank_import.parse_csv("столбец1;столбец2\n1;2\n"), [])


class ClassifyTests(unittest.TestCase):
    def test_default_rules(self):
        self.assertEqual(bank_import.classify("пластик PETG")["category"],
                         "filament")
        self.assertEqual(bank_import.classify("продажа ozon")["kind"], "income")
        self.assertEqual(bank_import.classify("налог уплачен")["category"], "tax")
        self.assertIsNone(bank_import.classify("совершенно неизвестная строка"))

    def test_custom_rules(self):
        rules = [{"match": "зоомагазин", "kind": "expense",
                  "category": "filament", "title": "Пластик"}]
        found = bank_import.classify("покупка в зоомагазине", rules)
        self.assertEqual(found["category"], "filament")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_preview_counts_and_duplicates(self):
        result = bank_import.preview(self.db, CSV_SAMPLE)
        self.assertEqual(result["matched"], 3)
        self.assertEqual(result["unmatched"], 1)
        self.assertEqual(result["duplicates"], 0)
        # После импорта те же строки — дубли, повторно не проведутся.
        bank_import.apply_rows(self.db, result["rows"])
        again = bank_import.preview(self.db, CSV_SAMPLE)
        self.assertEqual(again["duplicates"], 3)

    def test_apply_rows_imports_and_skips(self):
        preview = bank_import.preview(self.db, CSV_SAMPLE)
        applied = bank_import.apply_rows(self.db, preview["rows"])
        self.assertEqual(applied["imported"], 3)
        self.assertEqual(applied["skipped"], 1)
        txs = self.db.query("SELECT * FROM transactions")
        kinds = {row["kind"] for row in txs}
        self.assertIn("income", kinds)
        self.assertIn("expense", kinds)
        # повторный импорт не задвоит: дубли пропускаются
        again = bank_import.apply_rows(self.db, preview["rows"])
        self.assertEqual(again["imported"], 0)


class SbpDoubleCountTests(unittest.TestCase):
    """Импорт выписки не должен проводить СБП-доход второй раз.

    Один и тот же приход можно занести двумя дорожками: «Поступления из банка»
    (ведёт деньги через ядро СБП) и «Импорт выписки» (разносит строки по
    статьям напрямую). Смоук 2026-09-10: после обеих дорожек в журнале лежало
    3000 вместо 1500 — то есть двойная выручка и двойная налоговая база.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "sbp-double.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _confirmed_sbp_income(self, amount: float = 1500.0) -> str:
        from datetime import datetime
        from connector.printflow.accounting import Accounting
        from connector.printflow.sbp import Sbp
        sbp = Sbp(self.db, Accounting(self.db))
        payment = sbp.create(amount=amount, request_id="dbl")
        sbp.confirm(payment["id"], note="СБП")
        return datetime.now().strftime("%Y-%m-%d")

    def test_income_already_booked_as_sbp_is_skipped(self):
        date = self._confirmed_sbp_income()
        before = self.db.one(
            "SELECT COALESCE(SUM(amount),0) s FROM transactions WHERE kind='income'")["s"]
        csv_text = (f"Дата;Сумма;Назначение\n{date};1500,00;Перевод от клиента Иван\n")
        prev = bank_import.preview(self.db, csv_text)
        self.assertEqual(prev["sbp_taken"], 1)
        self.assertTrue(prev["rows"][0]["sbp_taken"])
        applied = bank_import.apply_rows(self.db, prev["rows"])
        self.assertEqual(applied["imported"], 0)
        self.assertEqual(applied["sbp_skipped"], 1)
        after = self.db.one(
            "SELECT COALESCE(SUM(amount),0) s FROM transactions WHERE kind='income'")["s"]
        self.assertEqual(before, after)

    def test_other_rows_still_import(self):
        date = self._confirmed_sbp_income()
        csv_text = ("Дата;Сумма;Назначение\n"
                    f"{date};1500,00;Перевод от клиента Иван\n"
                    f"{date};-2000,00;Оплата пластика PETG\n")
        prev = bank_import.preview(self.db, csv_text)
        applied = bank_import.apply_rows(self.db, prev["rows"])
        self.assertEqual(applied["sbp_skipped"], 1)
        self.assertEqual(applied["imported"], 1)   # расход plastic прошёл как обычно
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) n FROM transactions WHERE kind='expense'")["n"], 1)


if __name__ == "__main__":
    unittest.main()

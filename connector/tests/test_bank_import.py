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
        from connector.printflow.accounting import Accounting
        txs = self.db.query("SELECT * FROM transactions")
        kinds = {row["kind"] for row in txs}
        self.assertIn("income", kinds)
        self.assertIn("expense", kinds)
        # повторный импорт не задвоит: дубли пропускаются
        again = bank_import.apply_rows(self.db, preview["rows"])
        self.assertEqual(again["imported"], 0)


if __name__ == "__main__":
    unittest.main()

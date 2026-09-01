"""Единый поиск по цеху (идея 70).

Раньше поиск был в четырёх местах панели и каждый искал своё; теперь один
`/api/search?q=` отдаёт сгруппированный результат. Проверяем, что группы
находятся по русскому тексту без регистра и что короткий запрос не роняет
базу десятками LIKE-запросов.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow.search import GROUPS, Search  # noqa: E402


class SearchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.search = Search(self.db)
        self.db.upsert("customers", {"id": "c1", "name": "Мария Иванова",
                                     "phone": "+79001234567"})
        self.db.upsert("orders", {"id": "o1", "number": "1001",
                                  "product": "Адресник дверной", "price": 350,
                                  "status": "new", "customer_id": "c1",
                                  "created_at": "2026-08-30T10:00:00"})
        self.db.upsert("nomenclature", {"id": "n1", "name": "Держатель телефона",
                                        "sku": "DRJ-1", "archived": 0})
        self.db.upsert("spools", {"id": "s1", "material": "PLA",
                                  "color_name": "Красный", "color_hex": "#f00",
                                  "remaining_grams": 800, "total_grams": 1000,
                                  "location": "A1", "archived": 0})
        self.db.upsert("print_jobs", {"id": "j1", "name": "держатель_красный.3mf",
                                      "state": "RUNNING", "printer_id": "P1",
                                      "created_at": "2026-08-30T11:00:00"})
        self.db.upsert("workshop_docs", {"id": "d1", "kind": "приход",
                                         "number": "П-1", "title": "Пруток PLA",
                                         "supplier": "Филамент-Про",
                                         "total_amount": 4200,
                                         "at": "2026-08-29T09:00:00"})
        self.db.upsert("shelf_items", {"id": "sh1", "name": "Адресник",
                                       "qty": 4, "price": 500, "cell": "B2",
                                       "nom_id": "n1",
                                       "updated_at": "2026-08-30T12:00:00"})

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _kinds(self, result: dict) -> list[str]:
        return [group["kind"] for group in result["groups"]]

    def test_order_found_by_number_and_by_product(self):
        by_number = self.search.run("1001")
        self.assertIn("orders", self._kinds(by_number))
        by_product = self.search.run("адресник дверной")
        titles = [item["title"] for group in by_product["groups"]
                  for item in group["items"] if group["kind"] == "orders"]
        self.assertTrue(titles and "1001" in titles[0])

    def test_russian_search_is_case_insensitive(self):
        upper = self.search.run("ДЕРЖАТЕЛЬ")
        lower = self.search.run("держатель")
        self.assertEqual(upper["total"], lower["total"])
        self.assertGreater(lower["total"], 0)

    def test_customer_found_by_phone(self):
        result = self.search.run("9001234567")
        self.assertIn("customers", self._kinds(result))

    def test_all_groups_are_searchable(self):
        """Каждая объявленная группа умеет искать (нет мёртвых веток)."""
        found = set()
        for term in ("адресник", "держатель", "PLA", "1001", "Пруток", "Иванова"):
            found.update(self._kinds(self.search.run(term)))
        self.assertEqual(set(GROUPS) - found, set(),
                         f"группы не ищут ничего: {sorted(set(GROUPS) - found)}")

    def test_group_filter_limits_scope(self):
        result = self.search.run("адресник", groups=("orders",))
        self.assertEqual(self._kinds(result), ["orders"])

    def test_limit_is_applied_per_group(self):
        for i in range(5):
            self.db.upsert("nomenclature", {"id": f"n{i + 10}",
                                            "name": f"Держатель {i}",
                                            "archived": 0})
        result = self.search.run("держатель", limit=2, groups=("products",))
        self.assertEqual(result["groups"][0]["count"], 2)

    def test_short_query_returns_hint_not_sql_fanout(self):
        result = self.search.run("а")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["groups"], [])
        self.assertIn("Минимум", result["hint"])

    def test_empty_query_is_safe(self):
        result = self.search.run("")
        self.assertEqual(result["total"], 0)

    def test_results_carry_deep_links(self):
        """Идея 57: из поиска можно открыть карточку, а не только список."""
        result = self.search.run("1001")
        routes = [item["route"] for group in result["groups"]
                  for item in group["items"]]
        self.assertTrue(any(route.startswith("#orders/") for route in routes))

    def test_archived_rows_are_hidden(self):
        self.db.upsert("nomenclature", {"id": "n-old", "name": "Держатель старый",
                                        "archived": 1})
        result = self.search.run("держатель старый", groups=("products",))
        self.assertEqual(result["groups"], [])

    def test_missing_table_does_not_break_search(self):
        """Старая база без части таблиц: группа сообщает об ошибке, поиск жив."""
        self.db.execute("DROP TABLE workshop_docs")
        broken = self.search.run("пруток")
        docs = [g for g in broken["groups"] if g["kind"] == "documents"]
        self.assertTrue(docs and "error" in docs[0]["items"][0])
        # остальные сущности ищутся как ни в чём не бывало
        alive = self.search.run("адресник")
        self.assertIn("orders", [g["kind"] for g in alive["groups"]])


if __name__ == "__main__":
    unittest.main()

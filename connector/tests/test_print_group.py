"""PrintFlow 9.1: печатные группы мелких товаров и пакетная оптимизация.

Свёртка строк документа (b2b) — визуально одинаковые мелкие товары
(«игрушки мелкие» и т.п.) с одинаковой печатной группой показываются одной
строкой, повторы позиций сливаются, сумма документа не меняется. Складские
движения при этом остаются построчными.

Оптимизация: список товаров и карточка больше не делают запросы на каждую
строку (статистика продаж, резервы, базовый тип цен — массово), settings()
кэшируется, правило «последней цены» согласовано между списком и документами.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import APP_VERSION  # noqa: E402
from connector.printflow.accounting import num  # noqa: E402
from connector.printflow.b2b import B2B, fold_lines  # noqa: E402
from connector.printflow.db import Database, SCHEMA_VERSION  # noqa: E402
from connector.printflow.documents import Documents  # noqa: E402
from connector.printflow.nomenclature import Nomenclature  # noqa: E402
from connector.printflow.repo import Repo, uid  # noqa: E402
from connector.printflow.stock import Stock  # noqa: E402


class PrintGroupBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "t.sqlite3")
        self.repo = Repo(self.db)
        self.stock = Stock(self.db)
        self.nom = Nomenclature(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _nom(self, name, price, print_group=""):
        row = self.db.upsert("nomenclature", {
            "id": uid("nom"), "name": name, "kind": "product",
            "print_group": print_group,
            "created_at": "2026-01-01T00:00:00+00:00"})
        if price:
            self.db.execute(
                "INSERT INTO prices(id,nom_id,price_type_id,price,at)"
                " VALUES(?,?,?,?,?)",
                (uid("pr"), row["id"], "retail", price,
                 "2026-01-01T00:00:00+00:00"))
        return row

    def _order(self, items):
        return self.repo.save_order({"items": items, "status": "new"})


class VersionTests(unittest.TestCase):
    def test_version_and_schema(self):
        self.assertEqual(APP_VERSION, "9.3.0")
        self.assertEqual(SCHEMA_VERSION, 13)


class FoldLinesTests(unittest.TestCase):
    def test_duplicates_merge_into_one_line(self):
        lines = [
            {"name": "Дракон", "qty": 2, "price": 300, "nom_id": "n1"},
            {"name": "Табличка", "qty": 1, "price": 500, "nom_id": "n2"},
            {"name": "Дракон", "qty": 3, "price": 300, "nom_id": "n1"},
        ]
        rows, info = fold_lines(lines)
        self.assertEqual(len(rows), 2)
        dragon = next(r for r in rows if r["name"] == "Дракон")
        self.assertEqual(dragon["qty"], 5)
        self.assertEqual(dragon["amount"], 1500.0)
        self.assertFalse(dragon["averaged"])
        self.assertTrue(info["folded"])
        self.assertEqual((info["before"], info["after"]), (3, 2))

    def test_print_group_collapses_small_goods(self):
        lines = [
            {"name": "Флексикот", "qty": 2, "price": 150,
             "nom_id": "n1", "print_group": "Игрушки мелкие"},
            {"name": "Дракончик", "qty": 3, "price": 200,
             "nom_id": "n2", "print_group": "Игрушки мелкие"},
            {"name": "Вешалка", "qty": 1, "price": 400, "nom_id": "n3"},
        ]
        rows, info = fold_lines(lines)
        self.assertEqual(len(rows), 2)
        group = next(r for r in rows if r["name"] == "Игрушки мелкие")
        # 150×2 + 200×3 = 900, количество 5, средняя цена 180
        self.assertEqual(group["qty"], 5)
        self.assertEqual(group["amount"], 900.0)
        self.assertTrue(group["averaged"])
        self.assertEqual(group["price"], 180.0)
        self.assertEqual(info["groups"], ["Игрушки мелкие"])

    def test_collapse_disabled_keeps_lines_but_merges_duplicates(self):
        lines = [
            {"name": "Кот", "qty": 1, "price": 100,
             "nom_id": "n1", "print_group": "Игрушки мелкие"},
            {"name": "Пёс", "qty": 1, "price": 100,
             "nom_id": "n2", "print_group": "Игрушки мелкие"},
            {"name": "Кот", "qty": 2, "price": 100,
             "nom_id": "n1", "print_group": "Игрушки мелкие"},
        ]
        rows, info = fold_lines(lines, collapse_groups=False)
        names = sorted(r["name"] for r in rows)
        self.assertEqual(names, ["Кот", "Пёс"])  # дубль слился, группа — нет
        self.assertEqual(next(r for r in rows if r["name"] == "Кот")["qty"], 3)
        self.assertEqual(info["groups"], [])

    def test_total_is_preserved_and_zero_qty_skipped(self):
        lines = [
            {"name": "A", "qty": 2, "price": 125.5, "nom_id": "n1"},
            {"name": "B", "qty": 0, "price": 999, "nom_id": "n2"},
            {"name": "A", "qty": 1, "price": 125.5, "nom_id": "n1"},
        ]
        rows, _info = fold_lines(lines)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qty"], 3)
        self.assertEqual(rows[0]["amount"], round(125.5 * 3, 2))

    def test_custom_lines_merge_by_name_and_price(self):
        lines = [
            {"name": "Услуга печати", "qty": 1, "price": 300},
            {"name": "Услуга печати", "qty": 2, "price": 300},
            {"name": "Услуга печати", "qty": 1, "price": 350},  # другая цена — отдельно
        ]
        rows, info = fold_lines(lines)
        self.assertEqual(len(rows), 2)
        merged = next(r for r in rows if r["qty"] == 3)
        self.assertEqual(merged["amount"], 900.0)
        self.assertTrue(info["folded"])


class B2BDocumentTests(PrintGroupBase):
    def _fixture(self):
        toy1 = self._nom("Флексикот", 150, print_group="Игрушки мелкие")
        toy2 = self._nom("Дракончик", 200, print_group="Игрушки мелкие")
        big = self._nom("Адресник дома", 900)
        order = self._order([
            {"nom_id": toy1["id"], "qty": 2},
            {"nom_id": big["id"], "qty": 1},
            {"nom_id": toy2["id"], "qty": 3},
            {"nom_id": toy1["id"], "qty": 1},
        ])
        return order

    def test_waybill_folds_small_goods_by_default(self):
        order = self._fixture()
        html = B2B(self.db).document(order["id"], "waybill")
        # три игрушки (2+1 и 3) и один адресник → 2 строки вместо четырёх
        self.assertIn("Игрушки мелкие", html)
        self.assertEqual(html.count("Флексикот"), 0)
        self.assertEqual(html.count("Адресник дома"), 1)
        self.assertIn("Показано позиций", html)
        # сумма не изменилась: 150×3 + 900 + 200×3 = 1950
        self.assertIn("1 950", html)

    def test_group_zero_disables_folding(self):
        order = self._fixture()
        html = B2B(self.db).document(order["id"], "waybill", group=False)
        self.assertIn("Флексикот", html)
        self.assertIn("Дракончик", html)
        # печатные группы не сработали, в строке нет «средней» цены
        self.assertNotIn("Игрушки мелкие", html)
        self.assertNotIn("ср. ", html)
        # а повтор одного товара всё равно слился: 4 строки состава, 3 в печати
        self.assertIn("Показано позиций", html)
        self.assertEqual(html.count("Флексикот"), 1)
        self.assertIn("1 950", html)

    def test_duplicate_item_appears_once(self):
        big = self._nom("Адресник", 900)
        order = self._order([
            {"nom_id": big["id"], "qty": 1},
            {"nom_id": big["id"], "qty": 1},
        ])
        html = B2B(self.db).document(order["id"], "waybill", group=False)
        self.assertEqual(html.count("Адресник"), 1)
        self.assertIn(">2</td>", html)  # количество сложилось

    def test_fractional_qty_not_truncated(self):
        bulk = self._nom("Крепёж набор", 100)
        order = self._order([{"nom_id": bulk["id"], "qty": 2.5}])
        html = B2B(self.db).document(order["id"], "waybill", group=False)
        self.assertIn("2.5", html)  # раньше int() резал количество до 2

    def test_single_order_price_is_total_not_per_unit(self):
        """Итог одиночного заказа не умножается на количество второй раз."""
        order = self.repo.save_order(
            {"product": "Набор из 3 табличек", "price": 1200, "qty": 3, "status": "new"})
        html = B2B(self.db).document(order["id"], "waybill")
        self.assertIn("1 200", html)       # итог — цена заказа
        self.assertNotIn("3 600", html)    # а не price × qty
        self.assertIn(">400</td>", html)   # цена штуки — итог/количество


class ForOrderFoldTests(PrintGroupBase):
    def test_for_order_reports_fold_hint(self):
        toy = self._nom("Котик", 100, print_group="Игрушки мелкие")
        toy2 = self._nom("Пёсик", 100, print_group="Игрушки мелкие")
        order = self._order([
            {"nom_id": toy["id"], "qty": 1},
            {"nom_id": toy2["id"], "qty": 1},
        ])
        payload = Documents(self.db).for_order(order["id"])
        self.assertTrue(payload["fold"]["enabled"])
        self.assertEqual(payload["fold"]["before"], 2)
        self.assertEqual(payload["fold"]["after"], 1)
        self.assertEqual(payload["fold"]["groups"], ["Игрушки мелкие"])

    def test_for_order_without_groups_folds_nothing(self):
        a = self._nom("Раз", 100)
        b = self._nom("Два", 200)
        order = self._order([
            {"nom_id": a["id"], "qty": 1},
            {"nom_id": b["id"], "qty": 1},
        ])
        payload = Documents(self.db).for_order(order["id"])
        self.assertFalse(payload["fold"]["enabled"])
        self.assertEqual(payload["fold"]["before"], payload["fold"]["after"])

    def test_waybill_still_moves_stock_per_item(self):
        """Складская накладная не сворачивается: группы — только в печати."""
        toy = self._nom("Котик", 100, print_group="Игрушки мелкие")
        toy2 = self._nom("Пёсик", 150, print_group="Игрушки мелкие")
        order = self._order([
            {"nom_id": toy["id"], "qty": 2},
            {"nom_id": toy2["id"], "qty": 3},
        ])
        doc = Documents(self.db).waybill_from_order(order["id"])
        noms = sorted(it["nom_id"] for it in doc["items"])
        self.assertEqual(noms, sorted([toy["id"], toy2["id"]]))


class PrintGroupsApiTests(PrintGroupBase):
    def test_save_and_list_print_groups(self):
        self._nom("Котик", 100, print_group="Игрушки мелкие")
        self._nom("Пёсик", 100, print_group="Игрушки мелкие")
        self._nom("Табличка", 500, print_group="Таблички")
        self._nom("Крючок", 50)
        self.assertEqual(self.nom.print_groups(), ["Игрушки мелкие", "Таблички"])

    def test_save_via_nomenclature_service(self):
        saved = self.nom.save({"name": "Шестерёнка", "print_group": "Игрушки мелкие"})
        item = self.nom.item(saved["id"])
        self.assertEqual(item["print_group"], "Игрушки мелкие")

    def test_nomenclature_payload_has_print_groups(self):
        from connector.printflow.api import Api
        self._nom("Котик", 100, print_group="Игрушки мелкие")
        api = Api.__new__(Api)
        api.db = self.db
        api.nom = self.nom
        code, payload = api.get("/api/nomenclature", {})
        self.assertEqual(code, 200)
        self.assertEqual(payload["print_groups"], ["Игрушки мелкие"])

    def test_summary_stays_global_when_filtered(self):
        from connector.printflow.api import Api
        self._nom("Котик", 100, print_group="Игрушки мелкие")
        self._nom("Табличка", 500)
        api = Api.__new__(Api)
        api.db = self.db
        api.nom = self.nom
        code, payload = api.get("/api/nomenclature", {"search": ["кот"]})
        self.assertEqual(code, 200)
        self.assertEqual(len(payload["items"]), 1)          # фильтр по списку
        self.assertEqual(payload["summary"]["items"], 2)    # сводка глобальная


class StockBatchOptimizationTests(PrintGroupBase):
    def test_stats_all_matches_per_item(self):
        a = self._nom("A", 0)
        b = self._nom("B", 0)
        self.stock.add_move(a["id"], "", 10, 100)
        self.stock.add_move(a["id"], "", -3, 0, doc_kind="sale",
                            at="2026-08-20T10:00:00")
        self.stock.add_move(a["id"], "", -2, 0, doc_kind="sale",
                            at="2026-08-05T10:00:00")
        self.stock.add_move(b["id"], "", -4, 0, doc_kind="sale",
                            at="2026-08-22T10:00:00")
        packed = self.stock.sales_stats_all()
        for nom_id in (a["id"], b["id"]):
            self.assertEqual(packed[nom_id], self.stock.sales_stats(nom_id))

    def test_reserved_all_matches_per_item(self):
        a = self._nom("A", 0)
        b = self._nom("B", 0)
        self.stock.add_move(a["id"], "", 10, 100)
        self.stock.add_move(b["id"], "", 5, 100)
        self.stock.reserve(a["id"], 3)
        self.stock.reserve(b["id"], 2)
        packed = self.stock.reserved_all()
        self.assertEqual(packed[a["id"]], self.stock.reserved(a["id"]))
        self.assertEqual(packed[b["id"]], self.stock.reserved(b["id"]))

    def test_items_list_uses_constant_query_count(self):
        """Главный регресс оптимизации: список товаров — не N+1."""
        for i in range(5):
            row = self._nom(f"Товар {i}", 100 + i)
            self.stock.add_move(row["id"], "", 10, 100)
            self.stock.add_move(row["id"], "", -2, 0, doc_kind="sale")
        calls = {"n": 0}
        real_query = self.db.query

        def counting(sql, params=()):
            calls["n"] += 1
            return real_query(sql, params)

        self.db.query = counting  # type: ignore[assignment]
        try:
            items = self.nom.items()
        finally:
            self.db.query = real_query  # type: ignore[assignment]
        self.assertEqual(len(items), 5)
        self.assertLessEqual(calls["n"], 10)  # было ~4 запроса на каждую строку

    def test_decorated_values_survive_batching(self):
        row = self._nom("Дракон", 300)
        self.stock.add_move(row["id"], "", 20, 100)
        self.stock.add_move(row["id"], "", -7, 0, doc_kind="sale")
        self.stock.reserve(row["id"], 3)
        item = next(i for i in self.nom.items() if i["id"] == row["id"])
        stats = self.stock.sales_stats(row["id"])
        self.assertEqual(item["sold_7"], stats["sold_7"])
        self.assertEqual(item["sold_30"], stats["sold_30"])
        self.assertEqual(item["last_sale"], stats["last_sale"])
        self.assertEqual(item["rate_per_day"], stats["rate_per_day"])
        self.assertEqual(item["reserved"], 3.0)
        self.assertEqual(item["qty"], 13.0)
        self.assertEqual(item["free"], 10.0)
        self.assertEqual(item["price"], 300.0)


class PricesConsistencyTests(PrintGroupBase):
    def test_all_prices_agrees_with_price_of_on_backdate(self):
        """Запись задним числом не перебивает актуальную цену в списках."""
        row = self._nom("A", 0)
        self.db.execute(
            "INSERT INTO prices(id,nom_id,price_type_id,price,at) VALUES(?,?,?,?,?)",
            (uid("pr"), row["id"], "retail", 250, "2026-01-10T00:00:00"))
        # позже внесли историческую цену — у неё больше rowid, но старее дата
        self.db.execute(
            "INSERT INTO prices(id,nom_id,price_type_id,price,at) VALUES(?,?,?,?,?)",
            (uid("pr"), row["id"], "retail", 199, "2026-01-05T00:00:00"))
        docs_price = Documents(self.db).price_of(row["id"])
        all_prices = self.nom._all_prices()
        self.assertEqual(docs_price, 250.0)
        self.assertEqual(all_prices[row["id"]]["retail"], 250.0)


class SettingsCacheTests(PrintGroupBase):
    def test_settings_cache_invalidated_on_write(self):
        self.db.settings()  # заполнили кэш
        self.db.set_settings({"target_profit_per_hour": 321.0})
        self.assertAlmostEqual(num(self.db.settings()["target_profit_per_hour"]), 321.0)
        # и прямые запросы тоже сбрасывают кэш (tour.py пишет так)
        self.db.execute("DELETE FROM settings WHERE key='target_profit_per_hour'")
        self.assertAlmostEqual(num(self.db.settings()["target_profit_per_hour"]), 250.0)

    def test_orders_list_has_no_per_order_item_probe(self):
        self._order([{"nom_id": self._nom("A", 100)["id"], "qty": 1}])
        statements: list[str] = []
        real_query = self.db.query

        def counting(sql, params=()):
            statements.append(sql)
            return real_query(sql, params)

        self.db.query = counting  # type: ignore[assignment]
        try:
            orders = self.repo.orders()
        finally:
            self.db.query = real_query  # type: ignore[assignment]
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["items_count"], 1)
        self.assertIn("economics", orders[0])
        # экономика заказа больше не спрашивает order_items отдельно на заказ
        probes = [s for s in statements
                  if "FROM order_items" in s.upper() and "GROUP BY" not in s.upper()]
        self.assertEqual(probes, [])


if __name__ == "__main__":
    unittest.main()

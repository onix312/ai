"""Ручные корректировки склада «−1» / «+1» (проект «Склад 15.5»).

Корректировка — это движение регистра `stock_moves` с типом `manual`,
а не продажа: деньги, касса, выручка, долги и статистика продаж не
затрагиваются. Любое действие откатывается кнопкой «Вернуть» (удаление
движения, как при распроведении документа).
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database
from connector.printflow.stock import Stock, MANUAL_KIND


class StockAdjustTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "adjust.sqlite3")
        self.stock = Stock(self.db)
        self.db.upsert("warehouses", {
            "id": "wh-1", "name": "Магазин", "kind": "shelf",
            "retail": 1, "archived": 0, "position": 0,
        })
        self.db.upsert("warehouses", {
            "id": "wh-2", "name": "Дом", "kind": "home",
            "retail": 0, "archived": 0, "position": 1,
        })
        self.db.upsert("nomenclature", {
            "id": "nom-1", "code": "000001", "name": "Адресник",
            "kind": "product", "unit": "шт", "archived": 0,
        })
        # приход 5 шт по 100 ₽
        self.stock.add_move("nom-1", "wh-1", 5, 500.0, doc_kind="receipt",
                            note="приход")
        # продажа 1 шт — денежный эталон
        self.stock.add_move("nom-1", "wh-1", -1, -100.0, doc_id="doc-sale",
                            doc_kind="sale", note="продажа")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------ «−1»
    def test_minus_one_writes_manual_move(self):
        before_qty = self.stock.qty("nom-1", "wh-1")
        move = self.stock.manual_adjust("nom-1", "wh-1", -1)
        self.assertEqual(move["doc_kind"], MANUAL_KIND)
        self.assertEqual(move["qty"], -1.0)
        self.assertEqual(move["cost"], -100.0)  # расход по средней
        self.assertEqual(self.stock.qty("nom-1", "wh-1"), before_qty - 1)
        # средняя себестоимость не изменилась
        self.assertEqual(self.stock.avg_cost("nom-1", "wh-1"), 100.0)

    def test_minus_one_is_not_a_sale(self):
        """Корректировка не видна продажам: статистика и денежные потоки
        не двигаются."""
        stats_before = self.stock.sales_stats("nom-1")
        cash_before = self.db.one(
            "SELECT COALESCE(SUM(CASE WHEN qty<0 AND doc_kind='sale' THEN -cost END),0) v"
            " FROM stock_moves WHERE nom_id=?", ("nom-1",))["v"]
        self.stock.manual_adjust("nom-1", "wh-1", -1)
        stats_after = self.stock.sales_stats("nom-1")
        cash_after = self.db.one(
            "SELECT COALESCE(SUM(CASE WHEN qty<0 AND doc_kind='sale' THEN -cost END),0) v"
            " FROM stock_moves WHERE nom_id=?", ("nom-1",))["v"]
        self.assertEqual(stats_before["sold_7"], stats_after["sold_7"])
        self.assertEqual(stats_before["sold_30"], stats_after["sold_30"])
        self.assertEqual(cash_before, cash_after)  # «выручка» не выросла

    def test_minus_one_blocked_at_zero(self):
        # На wh-2 позиция не приходовалась — свободного остатка нет.
        with self.assertRaises(ValueError):
            self.stock.manual_adjust("nom-1", "wh-2", -1)

    def test_minus_one_respects_reserve(self):
        """Зарезервированное под заказ списать нельзя."""
        # остаток 4 — резервируем все 4
        self.stock.reserve("nom-1", 4, "order-1", "wh-1", "под заказ")
        with self.assertRaises(ValueError):
            self.stock.manual_adjust("nom-1", "wh-1", -1)
        # освободим резерв — списание снова доступно
        self.stock.release(order_id="order-1")
        move = self.stock.manual_adjust("nom-1", "wh-1", -1)
        self.assertEqual(self.stock.qty("nom-1", "wh-1"), 3.0)
        self.assertEqual(move["doc_kind"], "manual")

    def test_zero_delta_rejected(self):
        with self.assertRaises(ValueError):
            self.stock.manual_adjust("nom-1", "wh-1", 0)

    def test_minus_n_amount(self):
        # идея 1: корректировка количества (3 шт), стоимость по средней
        move = self.stock.manual_adjust("nom-1", "wh-1", -3, reason="брак")
        self.assertEqual(move["qty"], -3.0)
        self.assertEqual(move["cost"], -300.0)
        self.assertEqual(self.stock.qty("nom-1", "wh-1"), 1.0)
        self.assertIn("[Брак]", move["note"])

    def test_minus_n_blocked_over_free(self):
        with self.assertRaises(ValueError):
            self.stock.manual_adjust("nom-1", "wh-1", -99, reason="брак")

    def test_plus_n_and_stats(self):
        self.stock.manual_adjust("nom-1", "wh-1", 2, reason="найдено")
        self.assertEqual(self.stock.qty("nom-1", "wh-1"), 6.0)
        stats = self.stock.manual_stats(days=7)
        self.assertEqual(stats["total_qty"], 0.0)  # оприходование не списание
        self.stock.manual_adjust("nom-1", "wh-1", -1, reason="брак")
        stats = self.stock.manual_stats(days=7)
        self.assertEqual(stats["total_qty"], 1.0)
        recent = self.stock.manual_recent(days=7)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["nom_name"], "Адресник")

    def test_unknown_nom_or_warehouse(self):
        with self.assertRaises(ValueError):
            self.stock.manual_adjust("nope", "wh-1", -1)
        with self.assertRaises(ValueError):
            self.stock.manual_adjust("nom-1", "nope", -1)

    # ------------------------------------------------------------ «+1»
    def test_plus_one(self):
        before = self.stock.qty("nom-1", "wh-1")
        move = self.stock.manual_adjust("nom-1", "wh-1", 1)
        self.assertEqual(move["qty"], 1.0)
        self.assertEqual(move["cost"], 100.0)  # приход по средней
        self.assertEqual(self.stock.qty("nom-1", "wh-1"), before + 1)
        # продажам оприходование тоже не видно
        stats = self.stock.sales_stats("nom-1")
        self.assertEqual(stats["sold_7"], 1.0)

    def test_plus_one_on_empty_uses_zero_cost(self):
        # Новая позиция без прихода: оприходование возможно, денег нет.
        self.db.upsert("nomenclature", {
            "id": "nom-2", "code": "000002", "name": "Потеряшка",
            "kind": "product", "unit": "шт", "archived": 0})
        move = self.stock.manual_adjust("nom-2", "wh-1", 1)
        self.assertEqual(move["cost"], 0.0)
        self.assertEqual(self.stock.qty("nom-2", "wh-1"), 1.0)

    # ---------------------------------------------------------- возврат
    def test_revert_minus_restores_balance(self):
        before = self.stock.qty("nom-1", "wh-1")
        move = self.stock.manual_adjust("nom-1", "wh-1", -1)
        self.assertEqual(self.stock.qty("nom-1", "wh-1"), before - 1)
        self.stock.revert_manual(move["id"])
        self.assertEqual(self.stock.qty("nom-1", "wh-1"), before)
        # движение удалено — повторный откат невозможен
        with self.assertRaises(ValueError):
            self.stock.revert_manual(move["id"])

    def test_revert_plus_restores_balance(self):
        before = self.stock.qty("nom-1", "wh-1")
        move = self.stock.manual_adjust("nom-1", "wh-1", 1)
        self.assertEqual(self.stock.qty("nom-1", "wh-1"), before + 1)
        self.stock.revert_manual(move["id"])
        self.assertEqual(self.stock.qty("nom-1", "wh-1"), before)

    def test_revert_only_manual(self):
        """Документ продажи откатывается распроведением, не кнопкой."""
        with self.assertRaises(ValueError):
            self.stock.revert_manual("doc-sale")  # id документа != id движения
        sale_move = self.db.one(
            "SELECT id FROM stock_moves WHERE doc_kind='sale' LIMIT 1")
        with self.assertRaises(ValueError):
            self.stock.revert_manual(sale_move["id"])

    def test_revert_minus_allowed_even_when_sold_out(self):
        """Откат списания только добавляет штуку обратно и не может создать
        минус: он разрешён даже если после списания остаток распродан —
        это как раз случай «списали по ошибке, штука нашлась»."""
        move = self.stock.manual_adjust("nom-1", "wh-1", -1)  # остаток 3
        # продаём оставшиеся 3
        self.stock.add_move("nom-1", "wh-1", -1, -100.0, doc_kind="sale")
        self.stock.add_move("nom-1", "wh-1", -1, -100.0, doc_kind="sale")
        self.stock.add_move("nom-1", "wh-1", -1, -100.0, doc_kind="sale")
        self.assertEqual(self.stock.qty("nom-1", "wh-1"), 0.0)
        self.stock.revert_manual(move["id"])
        self.assertEqual(self.stock.qty("nom-1", "wh-1"), 1.0)
        # деньги продаж не изменились
        stats = self.stock.sales_stats("nom-1")
        self.assertEqual(stats["sold_7"], 4.0)  # 1 в setUp + 3 после

    def test_revert_plus_blocked_when_reserved(self):
        """Откат оприходования не должен трогать зарезервированное."""
        move = self.stock.manual_adjust("nom-1", "wh-1", 1)  # остаток 5
        self.stock.reserve("nom-1", 5, "order-2", "wh-1", "под заказ")
        with self.assertRaises(ValueError):
            self.stock.revert_manual(move["id"])

    # ------------------------------------------------------------- аудит
    def test_audit_events(self):
        move = self.stock.manual_adjust("nom-1", "wh-1", -1)
        events = self.db.events(limit=10)
        kinds = [(e["kind"], e["title"]) for e in events]
        self.assertTrue(any(k == "stock" and "списание" in t for k, t in kinds))
        self.stock.revert_manual(move["id"])
        events = self.db.events(limit=10)
        self.assertTrue(
            any(e["kind"] == "stock" and "возвращена" in e["title"] for e in events))

    # ------------------------------------------------- позиции склада
    def test_warehouse_positions(self):
        self.stock.manual_adjust("nom-1", "wh-1", -1)  # остаток 3
        self.stock.reserve("nom-1", 2, "order-9", "wh-1", "резерв")
        rows = {r["nom_id"]: r for r in self.stock.warehouse_positions("wh-1")}
        self.assertIn("nom-1", rows)
        row = rows["nom-1"]
        self.assertEqual(row["qty"], 3.0)
        self.assertEqual(row["reserved"], 2.0)
        self.assertEqual(row["free"], 1.0)
        self.assertEqual(row["cost"], 100.0)
        # архивная позиция не показывается
        self.db.execute("UPDATE nomenclature SET archived=1 WHERE id=?", ("nom-1",))
        self.assertEqual(self.stock.warehouse_positions("wh-1"), [])


if __name__ == "__main__":
    unittest.main()

"""И2 «единый регистр»: витрина-зона, холды СБП, запрет минусов.

Проверяем:
• миграция сверяет зону «Полка магазина» с физическим остатком полки;
• операции полки пишут движения регистра и держат qty синхронно;
• документы по зоне не проводятся; расход защищён свободным остатком;
• СБП-продажа откладывает товар и ставит холд; подтверждение/отклонение/
  таймаут снимают холд; повторные вызовы не дублируют списания и деньги.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.cashier import Cashier  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.documents import Documents  # noqa: E402
from connector.printflow.shelf import Shelf  # noqa: E402
from connector.printflow.stock import Stock  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "zone.sqlite3")


def add_nom(db: Database, nom_id: str = "nom1", name: str = "Органайзер",
            **extra) -> dict:
    data = {"id": nom_id, "name": name, "kind": "product", "unit": "шт",
            "archived": 0}
    data.update(extra)
    return db.upsert("nomenclature", data)


class MigrationTests(unittest.TestCase):
    """Сверка витрины с регистром при открытии старой базы."""

    def tearDown(self):
        self.db.close()

    def _reopen(self) -> Database:
        path = self.db.path if hasattr(self.db, "path") else None
        assert path is not None
        self.db.close()
        self.db = Database(path)
        return self.db

    def test_migration_aligns_zone_to_shelf(self):
        self.db = make_db()
        add_nom(self.db)
        self.db.upsert("shelf_items", {"id": "s1", "name": "Органайзер",
                                      "nom_id": "nom1", "qty": 5,
                                      "cost_per_unit": 100, "active": 1})
        # Застывший слепок v3-миграции: в регистре 8, на полке 5.
        Stock(self.db).add_move("nom1", "shelf", 8, 800, doc_kind="receipt")
        self.db.execute("DELETE FROM settings WHERE key='migrated_shelf_zone'")
        self._reopen()
        stock = Stock(self.db)
        self.assertEqual(stock.qty("nom1", "shelf"), 5)
        moves = self.db.query(
            "SELECT * FROM stock_moves WHERE note LIKE 'начальный остаток витрины%'")
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["qty"], -3)
        self.assertEqual(moves[0]["doc_kind"], "inventory")
        self.assertIn("было 8", moves[0]["note"])
        self.assertIn("стало 5", moves[0]["note"])
        flag = self.db.one("SELECT value FROM settings WHERE key='migrated_shelf_zone'")
        self.assertEqual(flag["value"], "true")

    def test_migration_is_idempotent(self):
        self.db = make_db()
        add_nom(self.db)
        self.db.upsert("shelf_items", {"id": "s1", "name": "Органайзер",
                                      "nom_id": "nom1", "qty": 5,
                                      "cost_per_unit": 100, "active": 1})
        self.db.execute("DELETE FROM settings WHERE key='migrated_shelf_zone'")
        self._reopen()
        first = self.db.query(
            "SELECT id FROM stock_moves WHERE note LIKE 'начальный остаток витрины%'")
        self.assertEqual(len(first), 1)
        # Флаг потерян, но маркер в движениях не даёт задвоить.
        self.db.execute("DELETE FROM settings WHERE key='migrated_shelf_zone'")
        self._reopen()
        again = self.db.query(
            "SELECT id FROM stock_moves WHERE note LIKE 'начальный остаток витрины%'")
        self.assertEqual(len(again), 1)

    def test_migration_skips_unlinked_zero_and_inactive(self):
        self.db = make_db()
        add_nom(self.db)
        self.db.upsert("shelf_items", {"id": "s1", "name": "Без связки",
                                      "qty": 3, "active": 1})
        self.db.upsert("shelf_items", {"id": "s2", "name": "Органайзер",
                                      "nom_id": "nom1", "qty": 0, "active": 1})
        self.db.upsert("shelf_items", {"id": "s3", "name": "Органайзер",
                                      "nom_id": "nom1", "qty": 4, "active": 0})
        self.db.execute("DELETE FROM settings WHERE key='migrated_shelf_zone'")
        self._reopen()
        moves = self.db.query(
            "SELECT id FROM stock_moves WHERE note LIKE 'начальный остаток витрины%'")
        self.assertEqual(moves, [])

    def test_migration_resolves_legacy_shelf_link(self):
        self.db = make_db()
        add_nom(self.db, legacy_shelf_id="s1")
        self.db.upsert("shelf_items", {"id": "s1", "name": "Органайзер",
                                      "qty": 2, "cost_per_unit": 50, "active": 1})
        self.db.execute("DELETE FROM settings WHERE key='migrated_shelf_zone'")
        self._reopen()
        self.assertEqual(Stock(self.db).qty("nom1", "shelf"), 2)


class ZoneLegTests(unittest.TestCase):
    """Операции полки пишут зону регистра."""

    def setUp(self):
        self.db = make_db()
        self.stock = Stock(self.db)
        self.shelf = Shelf(self.db)
        add_nom(self.db)
        self.stock.add_move("nom1", "home", 10, 1000, doc_kind="receipt")
        moved = self.shelf.transfer_from_stock("nom1", "home", 5)
        self.item_id = moved["item"]["id"]

    def tearDown(self):
        self.db.close()

    def shelf_qty(self) -> float:
        return float(self.db.one("SELECT qty FROM shelf_items WHERE id=?",
                                 (self.item_id,))["qty"])

    def test_transfer_writes_arrival_to_zone(self):
        self.assertEqual(self.stock.qty("nom1", "home"), 5)
        self.assertEqual(self.stock.qty("nom1", "shelf"), 5)
        self.assertEqual(self.stock.qty("nom1"), 10)
        self.assertEqual(self.shelf_qty(), 5)

    def test_transfer_adopts_name_matched_item(self):
        self.db.upsert("nomenclature", {"id": "nom2", "name": "Крючок",
                                       "kind": "product", "unit": "шт",
                                       "archived": 0})
        self.stock.add_move("nom2", "home", 4, 400, doc_kind="receipt")
        item = self.shelf.save_item({"name": "Крючок", "price": 300})
        self.assertEqual(item.get("nom_id") or "", "")
        self.shelf.transfer_from_stock("nom2", "home", 2)
        adopted = self.db.one("SELECT nom_id FROM shelf_items WHERE id=?",
                              (item["id"],))
        self.assertEqual(adopted["nom_id"], "nom2")
        self.assertEqual(self.stock.qty("nom2", "shelf"), 2)

    def test_sale_writes_zone_move_and_money(self):
        result = self.shelf.sale(self.item_id, 2, 500, channel="shelf")
        self.assertTrue(result["ok"])
        self.assertEqual(self.shelf_qty(), 3)
        self.assertEqual(self.stock.qty("nom1", "shelf"), 3)
        legs = self.db.query(
            "SELECT * FROM stock_moves WHERE warehouse_id='shelf' AND doc_kind='sale'")
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]["qty"], -2)
        tx = self.db.one("SELECT * FROM transactions WHERE kind='income'")
        self.assertEqual(tx["amount"], 1000)

    def test_unlinked_sale_touches_no_register(self):
        item = self.shelf.save_item({"name": "Ручная позиция", "price": 100,
                                     "qty": 5})
        self.shelf.sale(item["id"], 2, 100, channel="shelf")
        self.assertEqual(
            self.db.one("SELECT COUNT(*) n FROM stock_moves")["n"], 3)  # receipt + пара move
        self.assertEqual(self.stock.qty("nom1", "shelf"), 5)

    def test_sale_blocked_by_hold(self):
        self.stock.hold("nom1", "shelf", 5, "cs_test")
        with self.assertRaisesRegex(ValueError, "свободно"):
            self.shelf.sale(self.item_id, 1, 500, channel="shelf")
        self.assertEqual(self.shelf_qty(), 5)
        self.assertEqual(self.stock.qty("nom1", "shelf"), 5)
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))

    def test_writeoff_writes_zone_move(self):
        self.shelf.writeoff(self.item_id, 1, "брак")
        self.assertEqual(self.shelf_qty(), 4)
        self.assertEqual(self.stock.qty("nom1", "shelf"), 4)

    def test_writeoff_blocked_by_reserve(self):
        self.stock.reserve("nom1", 5, order_id="o1", warehouse_id="shelf")
        with self.assertRaisesRegex(ValueError, "резерве"):
            self.shelf.writeoff(self.item_id, 1, "брак")
        self.assertEqual(self.shelf_qty(), 5)

    def test_inventory_realigns_both_to_fact(self):
        # Расхождение из прошлого: полка правилась мимо регистра.
        self.db.execute("UPDATE shelf_items SET qty=7 WHERE id=?", (self.item_id,))
        result = self.shelf.inventory(self.item_id, 7)
        self.assertEqual(result["diff"], 0)
        self.assertEqual(self.stock.qty("nom1", "shelf"), 7)

    def test_undo_sale_restores_zone(self):
        sold = self.shelf.sale(self.item_id, 2, 500, channel="shelf")
        self.shelf.undo_sale(sold["move"]["id"])
        self.assertEqual(self.shelf_qty(), 5)
        self.assertEqual(self.stock.qty("nom1", "shelf"), 5)
        self.assertIsNone(self.db.one("SELECT * FROM transactions"))
        compensating = self.db.query(
            "SELECT * FROM stock_moves WHERE warehouse_id='shelf' AND doc_kind='sale'"
            " AND qty>0")
        self.assertEqual(len(compensating), 1)

    def test_produce_writes_receipt_leg(self):
        self.shelf.produce(self.item_id, 3, cost_per_unit=120)
        self.assertEqual(self.shelf_qty(), 8)
        self.assertEqual(self.stock.qty("nom1", "shelf"), 8)

    def test_sale_without_zone_warehouse_still_works(self):
        self.db.execute("UPDATE warehouses SET archived=1 WHERE id='shelf'")
        self.shelf.sale(self.item_id, 2, 500, channel="shelf")
        self.assertEqual(self.shelf_qty(), 3)
        # Зоны нет — регистр не тронут, продажа живёт только на полке.
        self.assertEqual(
            self.db.one("SELECT COUNT(*) n FROM stock_moves")["n"], 3)


class ManualMirrorTests(unittest.TestCase):
    """Корректировка «−1/+1» на зоне зеркалится на полку."""

    def setUp(self):
        self.db = make_db()
        self.stock = Stock(self.db)
        self.shelf = Shelf(self.db)
        add_nom(self.db)
        self.stock.add_move("nom1", "shelf", 5, 500, doc_kind="receipt")
        self.item = self.shelf.save_item({"name": "Органайзер", "nom_id": "nom1",
                                          "price": 500, "qty": 5})

    def tearDown(self):
        self.db.close()

    def shelf_qty(self) -> float:
        return float(self.db.one("SELECT qty FROM shelf_items WHERE id=?",
                                 (self.item["id"],))["qty"])

    def test_minus_one_mirrors_to_shelf(self):
        move = self.stock.manual_adjust("nom1", "shelf", -1, reason="брак")
        self.assertEqual(self.stock.qty("nom1", "shelf"), 4)
        self.assertEqual(self.shelf_qty(), 4)
        mirror = self.db.one("SELECT * FROM shelf_moves WHERE note LIKE ?",
                             (f"%витрина-корр:{move['id']}%",))
        self.assertIsNotNone(mirror)
        self.assertEqual(mirror["kind"], "writeoff")

    def test_plus_one_mirrors_to_shelf(self):
        self.stock.manual_adjust("nom1", "shelf", 2, reason="найдено")
        self.assertEqual(self.stock.qty("nom1", "shelf"), 7)
        self.assertEqual(self.shelf_qty(), 7)

    def test_revert_removes_mirror(self):
        move = self.stock.manual_adjust("nom1", "shelf", -1, reason="брак")
        result = self.stock.revert_manual(move["id"])
        self.assertEqual(result["mirrored"], 1)
        self.assertEqual(self.stock.qty("nom1", "shelf"), 5)
        self.assertEqual(self.shelf_qty(), 5)
        leftovers = self.db.query("SELECT id FROM shelf_moves WHERE note LIKE ?",
                                  (f"%витрина-корр:{move['id']}%",))
        self.assertEqual(leftovers, [])

    def test_adjust_without_item_is_register_only(self):
        self.db.execute("DELETE FROM shelf_items WHERE id=?", (self.item["id"],))
        self.stock.manual_adjust("nom1", "shelf", -1, reason="потеря")
        self.assertEqual(self.stock.qty("nom1", "shelf"), 4)

    def test_mirror_refuses_shelf_minus(self):
        self.db.execute("UPDATE shelf_items SET qty=0 WHERE id=?", (self.item["id"],))
        with self.assertRaisesRegex(ValueError, "инвентаризацией"):
            self.stock.manual_adjust("nom1", "shelf", -1, reason="брак")
        self.assertEqual(self.stock.qty("nom1", "shelf"), 5)


class DocumentsBoundaryTests(unittest.TestCase):
    """Документы не лезут в зону; расход — только из свободного."""

    def setUp(self):
        self.db = make_db()
        self.stock = Stock(self.db)
        self.docs = Documents(self.db)
        add_nom(self.db)
        self.stock.add_move("nom1", "home", 5, 500, doc_kind="receipt")

    def tearDown(self):
        self.db.close()

    def test_post_to_shelf_zone_forbidden(self):
        doc = self.docs.save({"kind": "receipt", "warehouse_id": "shelf",
                              "items": [{"nom_id": "nom1", "qty": 1, "cost": 100}]})
        with self.assertRaisesRegex(ValueError, "витрина"):
            self.docs.post(doc["id"])
        doc = self.docs.save({"kind": "sale", "warehouse_id": "home",
                              "warehouse_to_id": "shelf",
                              "items": [{"nom_id": "nom1", "qty": 1, "price": 500}]})
        with self.assertRaisesRegex(ValueError, "витрина"):
            self.docs.post(doc["id"])

    def test_default_warehouse_skips_shelf(self):
        self.assertNotEqual(self.docs._default_warehouse(), "shelf")

    def test_sale_over_foreign_reserve_blocked(self):
        self.stock.reserve("nom1", 5, order_id="o1", warehouse_id="home")
        doc = self.docs.save({"kind": "sale", "warehouse_id": "home",
                              "items": [{"nom_id": "nom1", "qty": 5, "price": 500}]})
        with self.assertRaisesRegex(ValueError, "продаём"):
            self.docs.post(doc["id"])
        self.assertEqual(self.stock.qty("nom1", "home"), 5)

    def test_sale_with_own_order_reserve_allowed(self):
        self.stock.reserve("nom1", 5, order_id="o1", warehouse_id="home")
        doc = self.docs.save({"kind": "sale", "warehouse_id": "home", "order_id": "o1",
                              "items": [{"nom_id": "nom1", "qty": 5, "price": 500}]})
        posted = self.docs.post(doc["id"])
        self.assertEqual(posted["state"], "posted")
        self.assertEqual(self.stock.qty("nom1", "home"), 0)
        self.assertEqual(self.stock.reserved("nom1", "home"), 0)

    def test_move_over_reserve_blocked(self):
        self.stock.reserve("nom1", 5, order_id="o1", warehouse_id="home")
        doc = self.docs.save({"kind": "move", "warehouse_id": "home",
                              "warehouse_to_id": "defect",
                              "items": [{"nom_id": "nom1", "qty": 1}]})
        with self.assertRaisesRegex(ValueError, "источнике"):
            self.docs.post(doc["id"])

    def test_writeoff_over_reserve_blocked(self):
        self.stock.reserve("nom1", 4, order_id="o1", warehouse_id="home")
        doc = self.docs.save({"kind": "writeoff", "warehouse_id": "home",
                              "items": [{"nom_id": "nom1", "qty": 2}]})
        with self.assertRaisesRegex(ValueError, "а есть"):
            self.docs.post(doc["id"])

    def test_transfer_over_reserve_blocked(self):
        self.stock.reserve("nom1", 5, order_id="o1", warehouse_id="home")
        with self.assertRaises(ValueError):
            Shelf(self.db).transfer_from_stock("nom1", "home", 1)


class HoldTests(unittest.TestCase):
    """Холды СБП-продаж кассы."""

    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc)
        self.stock = Stock(self.db)
        self.shelf = Shelf(self.db)
        self.db.set_settings({"cashier_code": "1234"})
        add_nom(self.db)
        self.stock.add_move("nom1", "home", 10, 1000, doc_kind="receipt")
        moved = self.shelf.transfer_from_stock("nom1", "home", 5)
        self.item_id = moved["item"]["id"]
        self.db.execute("UPDATE shelf_items SET price=500 WHERE id=?", (self.item_id,))
        self.token = self.cashier.login("1234")["token"]

    def tearDown(self):
        self.db.close()

    def shelf_qty(self) -> float:
        return float(self.db.one("SELECT qty FROM shelf_items WHERE id=?",
                                 (self.item_id,))["qty"])

    def active_holds(self, sale_id: str) -> list:
        return self.db.query(
            "SELECT * FROM reserves WHERE doc_id=? AND state='active'", (sale_id,))

    def test_sbp_sell_holds_and_confirm_consumes(self):
        r = self.cashier.sell([{"item_id": self.item_id, "qty": 2}],
                              "sbp", self.token)
        self.assertFalse(r["paid"])
        self.assertEqual(self.shelf_qty(), 5)  # товар отложен, не списан
        holds = self.active_holds(r["sale_id"])
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0]["kind"], "hold")
        self.assertEqual(holds[0]["qty"], 2)
        self.assertEqual(holds[0]["warehouse_id"], "shelf")
        tile = next(i for i in self.cashier.catalog()["items"]
                    if i["id"] == self.item_id)
        # Свободное полки (3) + свободное домашнего склада (5): холд занял 2.
        self.assertEqual(tile["qty"], 8)
        self.assertEqual(tile["held"], 2)
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))
        confirmed = self.cashier.confirm_sbp(r["payment_id"], self.token)
        self.assertTrue(confirmed["confirmed"])
        self.assertEqual(self.shelf_qty(), 3)
        self.assertEqual(self.stock.qty("nom1", "shelf"), 3)
        self.assertEqual(self.active_holds(r["sale_id"]), [])
        tx = self.db.one("SELECT * FROM transactions WHERE kind='income'")
        self.assertEqual(tx["account_id"], "sbp")
        self.assertEqual(tx["amount"], 1000)

    def test_second_sale_cannot_take_held(self):
        # Всё на витрине и всё в холде: второй продаже брать нечего.
        self.shelf.transfer_from_stock("nom1", "home", 5)
        self.cashier.sell([{"item_id": self.item_id, "qty": 10}],
                          "sbp", self.token)
        with self.assertRaisesRegex(ValueError, "нельзя"):
            self.cashier.sell([{"item_id": self.item_id, "qty": 1}],
                              "cash", self.token)
        with self.assertRaisesRegex(ValueError, "нельзя"):
            self.cashier.sell([{"item_id": self.item_id, "qty": 1}],
                              "sbp", self.token)
        self.assertEqual(self.shelf_qty(), 10)

    def test_reject_releases_hold_goods_stay(self):
        r = self.cashier.sell([{"item_id": self.item_id, "qty": 2}],
                              "sbp", self.token)
        self.cashier.reject_sbp(r["payment_id"], self.token, reason="не пришло")
        self.assertEqual(self.active_holds(r["sale_id"]), [])
        self.assertEqual(self.shelf_qty(), 5)
        self.assertEqual(self.stock.qty("nom1", "shelf"), 5)
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))
        tile = next(i for i in self.cashier.catalog()["items"]
                    if i["id"] == self.item_id)
        self.assertEqual(tile["qty"], 10)

    def test_expired_hold_released_lazily(self):
        self.db.set_settings({"sbp_hold_hours": 1})
        r = self.cashier.sell([{"item_id": self.item_id, "qty": 2}],
                              "sbp", self.token)
        self.assertEqual(len(self.active_holds(r["sale_id"])), 1)
        self.db.execute("UPDATE reserves SET at='2020-01-01T00:00:00'"
                        " WHERE doc_id=?", (r["sale_id"],))
        incoming = self.cashier.incoming()
        self.assertEqual(self.active_holds(r["sale_id"]), [])
        row = next(p for p in incoming["payments"] if p["id"] == r["sale_id"])
        self.assertEqual(row["holds"], [])
        # Подтвердить после ухода товара уже нельзя — проверка честная:
        # полка пуста, домашний склад целиком в чужом резерве.
        self.stock.reserve("nom1", 5, order_id="o1", warehouse_id="home")
        self.db.execute("UPDATE shelf_items SET qty=0 WHERE id=?", (self.item_id,))
        with self.assertRaisesRegex(ValueError, "нельзя"):
            self.cashier.confirm_sbp(r["payment_id"], self.token)
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))
        payment = self.db.one("SELECT status FROM sbp_payments WHERE id=?",
                              (r["payment_id"],))
        self.assertIn(payment["status"], ("new", "pending"))

    def test_incoming_shows_holds(self):
        r = self.cashier.sell([{"item_id": self.item_id, "qty": 2}],
                              "sbp", self.token)
        row = next(p for p in self.cashier.incoming()["payments"]
                   if p["id"] == r["sale_id"])
        self.assertEqual(len(row["holds"]), 1)
        self.assertEqual(row["holds"][0]["qty"], 2)
        self.assertEqual(row["holds"][0]["nom_name"], "Органайзер")

    def test_double_confirm_writes_once(self):
        r = self.cashier.sell([{"item_id": self.item_id, "qty": 2}],
                              "sbp", self.token)
        self.cashier.confirm_sbp(r["payment_id"], self.token)
        again = self.cashier.confirm_sbp(r["payment_id"], self.token)
        self.assertTrue(again.get("already_recorded"))
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM transactions")["n"], 1)
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) n FROM stock_moves"
            " WHERE warehouse_id='shelf' AND doc_kind='sale' AND qty<0")["n"], 1)
        self.assertEqual(self.shelf_qty(), 3)

    def test_sbp_retry_creates_single_hold(self):
        first = self.cashier.sell([{"item_id": self.item_id, "qty": 2}],
                                  "sbp", self.token, request_id="dup-1")
        again = self.cashier.sell([{"item_id": self.item_id, "qty": 2}],
                                  "sbp", self.token, request_id="dup-1")
        self.assertTrue(again.get("already_recorded"))
        self.assertEqual(len(self.active_holds(first["sale_id"])), 1)

    def test_unlinked_sbp_sale_has_no_hold(self):
        item = self.shelf.save_item({"name": "Ручная позиция", "price": 100,
                                     "qty": 5})
        r = self.cashier.sell([{"item_id": item["id"], "qty": 2}],
                              "sbp", self.token)
        self.assertEqual(self.active_holds(r["sale_id"]), [])
        self.cashier.confirm_sbp(r["payment_id"], self.token)
        self.assertEqual(float(self.db.one("SELECT qty FROM shelf_items WHERE id=?",
                                           (item["id"],))["qty"]), 3)
        tx = self.db.one("SELECT * FROM transactions WHERE kind='income'")
        self.assertEqual(tx["amount"], 200)


if __name__ == "__main__":
    unittest.main()

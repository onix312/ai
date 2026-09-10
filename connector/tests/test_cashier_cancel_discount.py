"""И4 «Всё кассовое»: отмена наличной продажи, скидка старшего, задел ящиков.

Проверяем: отмена — только наличные в окне открытой смены (любой кассир),
СБП — только из панели; скидка — процентом со скидкой нетто, PIN старшего
(или режим владельца без PIN); одна открытая смена на ящик.
"""
from __future__ import annotations

import json
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
from connector.printflow.staff import Staff  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "i4.sqlite3")


def add_item(db: Database, item_id: str = "s1", qty: float = 10,
             price: float = 500) -> None:
    db.upsert("shelf_items", {
        "id": item_id, "name": "Адресник", "qty": qty, "price": price,
        "cost_per_unit": 120, "active": 1})


class CancelBase(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc)
        self.db.set_settings({"cashier_code": "1234"})
        add_item(self.db)
        self.token = self.cashier.login("1234")["token"]

    def tearDown(self):
        self.db.close()

    def qty(self) -> float:
        return float(self.db.one(
            "SELECT qty FROM shelf_items WHERE id='s1'")["qty"])

    def tx_count(self) -> int:
        return int(self.db.one(
            "SELECT COUNT(*) n FROM transactions")["n"])


class CancelTests(CancelBase):
    def test_cash_sale_persists_move_id(self):
        r = self.cashier.sell([{"item_id": "s1", "qty": 2}], "cash",
                              self.token)
        row = self.db.one("SELECT items FROM cashier_sales WHERE id=?",
                          (r["sale_id"],))
        moves = [it.get("move_id") for it in json.loads(row["items"])]
        self.assertEqual(len(moves), 1)
        self.assertTrue(moves[0])
        self.assertIsNotNone(self.db.one(
            "SELECT id FROM shelf_moves WHERE id=?", (moves[0],)))

    def test_cancel_happy_path(self):
        self.cashier.open_shift(self.token, 0)
        r = self.cashier.sell([{"item_id": "s1", "qty": 2}], "cash",
                              self.token)
        self.assertEqual(self.qty(), 8)
        self.assertEqual(self.tx_count(), 1)
        out = self.cashier.cancel_sale(r["sale_id"], self.token)
        self.assertTrue(out["ok"])
        self.assertFalse(out["already"])
        self.assertTrue(out["cancelled"])
        self.assertEqual(self.qty(), 10)  # штуки вернулись
        self.assertEqual(self.tx_count(), 0)  # проводка удалена
        move = self.db.one(
            "SELECT undone FROM shelf_moves WHERE qty<0 ORDER BY rowid DESC")
        self.assertEqual(int(move["undone"]), 1)
        audit = self.db.one(
            "SELECT * FROM audit_log WHERE action='cancel_sale'"
            " ORDER BY rowid DESC")
        self.assertIn("1000", audit["detail"])
        event = self.db.one(
            "SELECT * FROM events WHERE title='Отмена продажи в кассе'"
            " ORDER BY rowid DESC")
        self.assertIsNotNone(event)

    def test_cancel_requires_open_shift(self):
        # «отмена только в окне смены» — про ручную смену (режим manual).
        # В auto смена открывается сама на первой продаже, и отмена работает
        # без участия человека: test_cashier_shift_mode.AutoShiftTests.
        self.db.set_settings({"cashier_shift_mode": "manual"})
        r = self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash",
                              self.token)
        with self.assertRaisesRegex(ValueError, "не из текущей смены"):
            self.cashier.cancel_sale(r["sale_id"], self.token)
        self.assertEqual(self.qty(), 9)  # ничего не тронуто

    def test_cancel_old_sale_blocked(self):
        # «не из текущей смены» — про смену, которую открывает человек
        self.db.set_settings({"cashier_shift_mode": "manual"})
        r = self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash",
                              self.token)
        self.db.execute("UPDATE cashier_sales SET created_at='2020-01-01T10:00:00'"
                        " WHERE id=?", (r["sale_id"],))
        self.cashier.open_shift(self.token, 0)
        with self.assertRaisesRegex(ValueError, "не из текущей смены"):
            self.cashier.cancel_sale(r["sale_id"], self.token)

    def test_cancel_sbp_blocked(self):
        self.cashier.open_shift(self.token, 0)
        pending = self.cashier.sell([{"item_id": "s1", "qty": 1}], "sbp",
                                    self.token)
        with self.assertRaisesRegex(ValueError, "из панели"):
            self.cashier.cancel_sale(pending["sale_id"], self.token)
        self.cashier.confirm_sbp(pending["payment_id"], self.token)
        with self.assertRaisesRegex(ValueError, "из панели"):
            self.cashier.cancel_sale(pending["sale_id"], self.token)

    def test_cancel_unknown_sale(self):
        self.cashier.open_shift(self.token, 0)
        with self.assertRaisesRegex(ValueError, "не найдена"):
            self.cashier.cancel_sale("нет-такой", self.token)

    def test_cancel_idempotent(self):
        self.cashier.open_shift(self.token, 0)
        r = self.cashier.sell([{"item_id": "s1", "qty": 2}], "cash",
                              self.token)
        self.cashier.cancel_sale(r["sale_id"], self.token)
        again = self.cashier.cancel_sale(r["sale_id"], self.token)
        self.assertTrue(again["already"])
        self.assertEqual(self.qty(), 10)  # второй раз штуки не вернулись
        self.assertEqual(self.tx_count(), 0)

    def test_cancel_by_employee(self):
        staff = Staff(self.db)
        ivan = staff.add("Иван", "employee", "")
        staff.set_pin(ivan["id"], "1111")
        emp = self.cashier.login("1111")["token"]
        self.cashier.open_shift(emp, 0)
        r = self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash", emp)
        out = self.cashier.cancel_sale(r["sale_id"], emp)
        self.assertTrue(out["ok"])  # любой кассир — решение И4
        audit = self.db.one(
            "SELECT data FROM audit_log WHERE action='cancel_sale'"
            " ORDER BY rowid DESC")
        self.assertIn("Иван", audit["data"])

    def test_cancel_legacy_rows_without_move(self):
        self.cashier.open_shift(self.token, 0)
        r = self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash",
                              self.token)
        row = self.db.one("SELECT items FROM cashier_sales WHERE id=?",
                          (r["sale_id"],))
        items = json.loads(row["items"])
        for it in items:
            it.pop("move_id", None)
        self.db.execute("UPDATE cashier_sales SET items=? WHERE id=?",
                        (json.dumps(items), r["sale_id"]))
        with self.assertRaisesRegex(ValueError, "до обновления"):
            self.cashier.cancel_sale(r["sale_id"], self.token)
        self.assertEqual(self.qty(), 9)


class ShiftSalesTests(CancelBase):
    def test_no_shift_no_sales(self):
        # в ручном режиме смены нет, пока кассир её не открыл (режим manual)
        self.db.set_settings({"cashier_shift_mode": "manual"})
        self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash", self.token)
        out = self.cashier.shift_sales(self.token)
        self.assertFalse(out["open"])
        self.assertEqual(out["sales"], [])

    def test_window_new_first_with_flags(self):
        self.cashier.open_shift(self.token, 0)
        first = self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash",
                                  self.token)
        # Штамп первой продажи — ровно открытие смены (тот же формат ISO):
        # в окно попадает, а сортировка новых сверху детерминирована.
        self.db.execute(
            "UPDATE cashier_sales SET created_at=("
            "SELECT opened_at FROM cashier_shifts"
            " WHERE COALESCE(closed_at,'')='')"
            " WHERE id=?", (first["sale_id"],))
        second = self.cashier.sell([{"item_id": "s1", "qty": 1}], "sbp",
                                   self.token)
        self.cashier.cancel_sale(first["sale_id"], self.token)
        out = self.cashier.shift_sales(self.token)
        self.assertTrue(out["open"])
        self.assertEqual([s["id"] for s in out["sales"]],
                         [second["sale_id"], first["sale_id"]])
        by_id = {s["id"]: s for s in out["sales"]}
        self.assertTrue(by_id[first["sale_id"]]["cancelled"])
        self.assertEqual(by_id[second["sale_id"]]["method"], "sbp")
        self.assertFalse(by_id[second["sale_id"]]["confirmed"])
        self.assertTrue(by_id[second["sale_id"]]["items"])

    def test_sales_scoped_by_box(self):
        self.cashier.open_shift(self.token, 0)
        self.cashier.open_shift(self.token, 0, "box2")
        plain = self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash",
                                  self.token)
        boxed = self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash",
                                  self.token, box_id="box2")
        main = self.cashier.shift_sales(self.token)
        extra = self.cashier.shift_sales(self.token, "box2")
        self.assertEqual([s["id"] for s in main["sales"]], [plain["sale_id"]])
        self.assertEqual([s["id"] for s in extra["sales"]], [boxed["sale_id"]])


class DiscountBase(CancelBase):
    def setUp(self):
        super().setUp()
        self.staff = Staff(self.db)

    def with_pins(self):
        ivan = self.staff.add("Иван", "employee", "")
        self.staff.set_pin(ivan["id"], "1111")
        boss = self.staff.add("Ольга", "manager", "")
        self.staff.set_pin(boss["id"], "2222")
        emp = self.cashier.login("1111")["token"]
        mgr = self.cashier.login("2222")["token"]
        return emp, mgr


class DiscountTests(DiscountBase):
    def test_zero_discount_as_before(self):
        r = self.cashier.sell([{"item_id": "s1", "qty": 2}], "cash",
                              self.token)
        self.assertEqual(r["amount"], 1000)
        self.assertEqual(r["discount_pct"], 0)
        self.assertEqual(r["discount_amount"], 0)
        self.assertEqual(r["list_amount"], 1000)
        row = self.db.one("SELECT discount_pct, discount_amount, box_id"
                          " FROM cashier_sales WHERE id=?", (r["sale_id"],))
        self.assertEqual(float(row["discount_pct"]), 0)
        self.assertEqual(row["box_id"], "")

    def test_no_pins_owner_discount_without_approval(self):
        r = self.cashier.sell([{"item_id": "s1", "qty": 2}], "cash",
                              self.token, discount_pct=10)
        self.assertEqual(r["amount"], 900)
        self.assertEqual(r["discount_pct"], 10)
        self.assertEqual(r["discount_amount"], 100)
        self.assertEqual(r["list_amount"], 1000)
        items = r["items"]
        self.assertEqual(items[0]["price"], 450)  # цена продажи
        self.assertEqual(items[0]["list_price"], 500)  # каталог
        tx = self.db.one("SELECT amount FROM transactions")
        self.assertEqual(tx["amount"], 900)  # выручка нетто
        audit = self.db.one(
            "SELECT detail FROM audit_log WHERE action='sell'"
            " ORDER BY rowid DESC")
        self.assertIn("скидка 10%", audit["detail"])

    def test_pins_require_manager_pin(self):
        emp, _mgr = self.with_pins()
        with self.assertRaisesRegex(ValueError, "PIN старшего"):
            self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash", emp,
                              discount_pct=10)
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) n FROM cashier_sales")["n"], 0)
        self.assertEqual(self.qty(), 10)  # склад не тронут
        with self.assertRaisesRegex(ValueError, "PIN старшего"):
            self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash", emp,
                              discount_pct=10, manager_pin="1111")  # свой, employee
        with self.assertRaisesRegex(ValueError, "PIN старшего"):
            self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash", emp,
                              discount_pct=10, manager_pin="9999")

    def test_manager_pin_approves(self):
        emp, _mgr = self.with_pins()
        r = self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash", emp,
                              discount_pct=20, manager_pin="2222")
        self.assertEqual(r["amount"], 400)
        self.assertEqual(r["cashier"], "Иван")
        audit = self.db.one(
            "SELECT detail FROM audit_log WHERE action='sell'"
            " ORDER BY rowid DESC")
        self.assertIn("скидка 20% (Ольга)", audit["detail"])
        event = self.db.one(
            "SELECT * FROM events WHERE title='Скидка на кассе'"
            " ORDER BY rowid DESC")
        self.assertIn("Ольга", event["detail"])
        self.assertNotIn("2222", event["detail"])  # PIN не светим

    def test_discount_bounds(self):
        with self.assertRaisesRegex(ValueError, "0–100"):
            self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash",
                              self.token, discount_pct=101)
        with self.assertRaisesRegex(ValueError, "0–100"):
            self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash",
                              self.token, discount_pct=-5)

    def test_full_gift_cash_only(self):
        r = self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash",
                              self.token, discount_pct=100)
        self.assertEqual(r["amount"], 0)
        self.assertEqual(self.qty(), 9)  # штуки ушли
        self.assertEqual(self.tx_count(), 0)  # денег нет
        audit = self.db.one(
            "SELECT detail FROM audit_log WHERE action='sell'"
            " ORDER BY rowid DESC")
        self.assertIn("дарение", audit["detail"])
        with self.assertRaisesRegex(ValueError, "наличные"):
            self.cashier.sell([{"item_id": "s1", "qty": 1}], "sbp",
                              self.token, discount_pct=100)

    def test_sbp_discounted_net(self):
        r = self.cashier.sell([{"item_id": "s1", "qty": 2}], "sbp",
                              self.token, discount_pct=10)
        self.assertEqual(r["amount"], 900)
        payment = self.db.one("SELECT * FROM sbp_payments WHERE id=?",
                              (r["payment_id"],))
        self.assertEqual(float(payment["amount"]), 900)
        composition = json.loads(payment["items"])
        self.assertEqual(composition[0]["price"], 450)
        self.cashier.confirm_sbp(r["payment_id"], self.token)
        tx = self.db.one("SELECT amount FROM transactions")
        self.assertEqual(tx["amount"], 900)

    def test_discounted_sale_cancels(self):
        self.cashier.open_shift(self.token, 0)
        r = self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash",
                              self.token, discount_pct=10)
        out = self.cashier.cancel_sale(r["sale_id"], self.token)
        self.assertTrue(out["ok"])
        self.assertEqual(self.qty(), 10)
        self.assertEqual(self.tx_count(), 0)


class BoxTests(CancelBase):
    def test_one_shift_per_box(self):
        first = self.cashier.open_shift(self.token, 0)
        self.assertTrue(first["open"])
        with self.assertRaisesRegex(ValueError, "уже открыта"):
            self.cashier.open_shift(self.token, 0)
        second = self.cashier.open_shift(self.token, 500, "box2")
        self.assertTrue(second["open"])
        self.assertEqual(second["shift"]["box_id"], "box2")
        with self.assertRaisesRegex(ValueError, "ящик box2"):
            self.cashier.open_shift(self.token, 0, "box2")
        cur = self.cashier.current_shift(self.token, "box2")
        self.assertEqual(cur["live"]["expected"], 500)
        main = self.cashier.current_shift(self.token)
        self.assertEqual(main["live"]["expected"], 0)

    def test_close_and_collect_ect_scoped(self):
        self.cashier.open_shift(self.token, 0)
        self.cashier.open_shift(self.token, 0, "box2")
        self.cashier.sell([{"item_id": "s1", "qty": 4}], "cash", self.token)
        out = self.cashier.collect(self.token, 300, box_id="box2")
        shift2 = self.cashier.current_shift(self.token, "box2")["shift"]["id"]
        self.assertEqual(out["shift_id"], shift2)
        self.cashier.close_shift(self.token, 0, box_id="box2")
        self.assertFalse(self.cashier.current_shift(self.token, "box2")["open"])
        self.assertTrue(self.cashier.current_shift(self.token)["open"])


class I4PageTests(unittest.TestCase):
    def setUp(self):
        self.page = (ROOT / "site" / "cashier.html").read_text(
            encoding="utf-8")

    def test_cancel_ui_wired(self):
        for needle in ("shiftSales", "loadShiftSales", "data-cancel",
                       "/api/cashier/sale/cancel", "/api/cashier/shift/sales",
                       "Продажи смены", "Отменить продажу?"):
            self.assertIn(needle, self.page)

    def test_discount_ui_wired(self):
        for needle in ("discPct", "discPin", "discount_pct", "manager_pin",
                       "refreshPayTitle", "PIN старшего", "qb disc"):
            self.assertIn(needle, self.page)

    def test_inline_script_parses(self):
        import re
        import shutil
        import subprocess
        import tempfile
        if not shutil.which("node"):
            self.skipTest("node не установлен — проверка пропущена")
        scripts = re.findall(r"<script>(.*?)</script>", self.page, re.S)
        inline = max(scripts, key=len)
        with tempfile.NamedTemporaryFile("w", suffix=".js",
                                         delete=False) as tmp:
            tmp.write(inline)
            name = tmp.name
        try:
            result = subprocess.run(["node", "--check", name],
                                    capture_output=True, text=True, timeout=60)
        finally:
            pathlib.Path(name).unlink(missing_ok=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class I4RouteTests(unittest.TestCase):
    def test_new_routes_registered(self):
        import connector.printflow.routes_cashier  # noqa: F401 — регистрация
        from connector.printflow.router import router
        paths = {getattr(r, "path", "") for r in router.routes()}
        self.assertIn("/api/cashier/sale/cancel", paths)
        self.assertIn("/api/cashier/shift/sales", paths)


if __name__ == "__main__":
    unittest.main()

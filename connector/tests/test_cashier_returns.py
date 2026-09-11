"""Возвраты на кассе (17.0.6), звонок по настройке и закрытие заявок клиента (R5).

Возврат принципиально отличается от «Отменить»: продажа остаётся в журнале и
в своей смене, а деньги уходят сегодняшней проводкой ``expense/refund``. Если
бы этого не было, возврат в ноябре переписывал бы выручку и налоговую базу
сентября, а сверка смены показывала бы недостачу там, где кассир просто выдал
деньги покупателю.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.cashier import Cashier  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.shelf import Shelf  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "returns.sqlite3")


def add_item(db: Database, item_id: str = "s1", qty: float = 10, price: float = 500) -> None:
    db.upsert("shelf_items", {"id": item_id, "name": "Адресник", "qty": qty,
                               "price": price, "cost_per_unit": 120, "active": 1})


def insert(db: Database, table: str, data: dict) -> None:
    names = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    db.execute(f"INSERT INTO {table}({names}) VALUES({marks})", list(data.values()))


class ShelfReturnTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.shelf = Shelf(self.db)
        add_item(self.db)

    def tearDown(self):
        self.db.close()

    def test_return_restores_stock_and_keeps_history(self):
        sale = self.shelf.sale("s1", 2, 500, channel="shelf")
        move_id = str(sale["move"]["id"])
        out = self.shelf.return_stock(move_id, 1, "не подошёл размер")
        self.assertEqual(out["amount"], 500.0)
        self.assertEqual(round(float(self.shelf.item("s1")["qty"]), 2), 9.0)
        # Продажа НЕ удалена — был возврат, а не «не было продажи».
        self.assertIsNotNone(self.db.one("SELECT id FROM transactions WHERE id=?",
                                         (str(sale["tx"]["id"]),)))
        tx = out["tx"]
        self.assertEqual((tx["kind"], tx["category"]), ("expense", "refund"))
        self.assertEqual(tx["channel"], "shelf")

    def test_over_return_is_refused(self):
        sale = self.shelf.sale("s1", 2, 500, channel="shelf")
        move_id = str(sale["move"]["id"])
        self.shelf.return_stock(move_id, 1, "")
        with self.assertRaisesRegex(ValueError, "не больше проданного"):
            self.shelf.return_stock(move_id, 2, "")
        self.assertEqual(round(float(self.shelf.item("s1")["qty"]), 2), 9.0)

    def test_cancelled_sale_has_nothing_to_return(self):
        sale = self.shelf.sale("s1", 1, 500, channel="shelf")
        self.shelf.undo_sale(str(sale["move"]["id"]))
        with self.assertRaisesRegex(ValueError, "отменена"):
            self.shelf.return_stock(str(sale["move"]["id"]), 1, "")

    def test_till_balance_nets_refunds(self):
        """Сколько должно лежать в магазине — за вычетом выданных возвратов."""
        sale = self.shelf.sale("s1", 2, 500, channel="shelf")
        self.assertEqual(self.shelf.shop_cash()["in_shop"], 1000.0)
        self.shelf.return_stock(str(sale["move"]["id"]), 1, "брак")
        self.assertEqual(self.shelf.shop_cash()["in_shop"], 500.0)


class CashierReturnTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.db.set_settings({"cashier_code": "1234", "tax_mode": "npd",
                              "npd_limit": 100000})
        add_item(self.db)
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc, Shelf(self.db))
        self.token = self.cashier.login("1234")["token"]

    def tearDown(self):
        self.db.close()

    def shelf_qty(self) -> float:
        return float(self.db.one("SELECT qty FROM shelf_items WHERE id='s1'")["qty"])

    def _sale(self, qty: float = 2) -> dict:
        return self.cashier.sell([{"item_id": "s1", "qty": qty}], "cash", self.token)

    def test_full_return_leaves_till_balanced(self):
        sale = self._sale()
        self.assertEqual(self.cashier.current_shift(self.token)["live"]["income_cash"], 1000.0)
        out = self.cashier.return_sale(sale["sale_id"], self.token, note="брак")
        self.assertEqual(out["returned"], 1000.0)
        live = self.cashier.current_shift(self.token)["live"]
        self.assertEqual(live["refunds"], 1000.0)
        self.assertEqual(live["income_cash"], 0.0)
        # Сервер ждёт в ящике ровно 0 ₽: недостачи из-за возврата быть не должно.
        self.assertEqual(live["expected"], 0.0)
        self.assertEqual(round(float(self.shelf_qty()), 2), 10.0)

    def shelf_qty(self) -> float:
        return float(self.db.one("SELECT qty FROM shelf_items WHERE id='s1'")["qty"])

    def test_partial_return_then_the_rest(self):
        sale = self._sale(3)
        move_id = str((sale["items"][0] or {}).get("move_id") or "")
        first = self.cashier.return_sale(sale["sale_id"], self.token,
                                         lines=[{"move_id": move_id, "qty": 1}])
        self.assertEqual(first["returned"], 500.0)
        self.assertEqual(round(float(self.shelf_qty()), 2), 8.0)   # 10 − 3 + 1
        rest = self.cashier.return_sale(sale["sale_id"], self.token)
        self.assertEqual(rest["returned"], 1000.0)     # remaining 2 шт
        self.assertEqual(round(float(self.shelf_qty()), 2), 10.0)
        with self.assertRaisesRegex(ValueError, "уже возвращено"):
            self.cashier.return_sale(sale["sale_id"], self.token)

    def test_second_full_return_is_blocked(self):
        sale = self._sale(2)
        self.cashier.return_sale(sale["sale_id"], self.token)
        again = self.db.one("SELECT refunded_amount FROM cashier_sales WHERE id=?",
                            (sale["sale_id"],))
        self.assertEqual(float(again["refunded_amount"]), 1000.0)

    def test_employee_may_not_return(self):
        from connector.printflow.staff import Staff
        staff = Staff(self.db)
        ira = staff.add("Ира", "employee", "")
        staff.set_pin(ira["id"], "1111")
        sale = self._sale()
        emp_token = self.cashier.login("1111")["token"]
        with self.assertRaisesRegex(ValueError, "руководителя"):
            self.cashier.return_sale(sale["sale_id"], emp_token)
        self.assertEqual(round(float(self.shelf_qty()), 2), 8.0)

    def test_sbp_sale_is_returned_via_bank_flow(self):
        sale_id = "cs-sbp-1"
        insert(self.db, "cashier_sales", {"id": sale_id, "method": "sbp", "amount": 700.0,
                                          "items": "[]", "cashier": "касса",
                                          "created_at": "2026-09-10T10:00:00+03:00"})
        with self.assertRaisesRegex(ValueError, "СБП"):
            self.cashier.return_sale(sale_id, self.token)

    def test_cancelled_sale_cannot_be_returned(self):
        sale = self._sale()
        self.cashier.cancel_sale(sale["sale_id"], self.token)
        with self.assertRaisesRegex(ValueError, "отменена"):
            self.cashier.return_sale(sale["sale_id"], self.token)

    def test_request_id_is_idempotent(self):
        sale = self._sale()
        first = self.cashier.return_sale(sale["sale_id"], self.token, request_id="ret-1")
        self.assertEqual(first["returned"], 1000.0)
        again = self.cashier.return_sale(sale["sale_id"], self.token, request_id="ret-1")
        self.assertTrue(again.get("already_recorded"))
        self.assertTrue(again.get("replayed"))
        self.assertEqual(again["returned"], 1000.0)   # форма как у свежего ответа
        self.assertEqual(round(float(self.shelf_qty()), 2), 10.0)
        self.assertEqual(float(self.db.one(
            "SELECT refunded_amount FROM cashier_sales WHERE id=?",
            (sale["sale_id"],))["refunded_amount"]), 1000.0)

    def test_unknown_line_is_refused(self):
        sale = self._sale()
        with self.assertRaisesRegex(ValueError, "нет таких строк"):
            self.cashier.return_sale(sale["sale_id"], self.token,
                                     lines=[{"move_id": "mv-nope", "qty": 1}])

    def test_sale_list_shows_refunds_and_cashier_is_notified(self):
        sale = self._sale()
        out = self.cashier.return_sale(sale["sale_id"], self.token, note="передумал")
        self.assertIn("npd", out)                      # годовая база тоже падает
        row = [r for r in self.cashier.shift_sales(self.token)["sales"]
               if r["id"] == sale["sale_id"]][0]
        self.assertTrue(row["refunded"])
        self.assertEqual(row["refunded_amount"], 1000.0)
        self.assertEqual(row["refund_left"], 0.0)
        event = self.db.one("SELECT * FROM events WHERE title='Возврат денег из кассы'"
                            " ORDER BY id DESC LIMIT 1")
        self.assertIsNotNone(event)
        import json as _json
        data = _json.loads(event["data"] or "{}")
        self.assertEqual(data["signal"], "money_out")
        self.assertEqual(data["amount"], -1000.0)

    def test_returned_day_still_needs_a_receipt_mark(self):
        """День, когда были только возвраты, тоже требует подтверждения чека."""
        npd = self.cashier.npd
        today = date.today().isoformat()
        sale = self._sale(1)
        self.cashier.return_sale(sale["sale_id"], self.token, note="возврат в тот же день")
        day = npd.day_income(today)
        self.assertEqual(day["refunds"], 500.0)
        # оборот = оба расчёта дня: выдали 500 ₽ и взяли 500 ₽ — два чека,
        # а не «ноль денег, делать нечего»
        self.assertEqual(day["turnover"], 1000.0)
        row = [d for d in npd.days(3) if d["day"] == today][0]
        self.assertEqual(row["refunds"], 500.0)


class RingSettingTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.db.set_settings({"cashier_code": "1234"})
        add_item(self.db)
        self.cashier = Cashier(self.db, Accounting(self.db), Shelf(self.db))

    def tearDown(self):
        self.db.close()

    def test_ring_on_by_default(self):
        self.assertTrue(self.cashier.catalog()["ring"])

    def test_owner_can_silence_the_cashier(self):
        self.db.set_settings({"cashier_ring": False})
        self.assertFalse(self.cashier.catalog()["ring"])

    def test_setting_is_exposed_in_the_panel_schema(self):
        from connector.printflow.settings_schema import get_schema
        item = get_schema()["cashier_ring"]
        self.assertEqual(item["type"], "bool")
        self.assertEqual(item["group"], "cashier")
        self.assertIn("Баннер", item["hint"] or "")

    def test_route_registered(self):
        import connector.printflow.routes_cashier  # noqa: F401  — регистрация
        from connector.printflow.router import router
        paths = {getattr(r, "path", "") for r in router.routes()}
        self.assertIn("/api/cashier/return", paths)


class ClientIntentCloseTests(unittest.TestCase):
    """R5: у заявки клиента появляется тот же итог, что у платежа."""

    def setUp(self):
        from connector.printflow.sbp import Sbp
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.sbp = Sbp(self.db, self.acc)
        self.db.set_settings({"sbp_enabled": True})

    def tearDown(self):
        self.db.close()

    def _intent(self, **data) -> str:
        row = {"id": data.pop("id", "cpi-1"), "order_id": "", "chat_id": "1",
               "amount": 800.0, "status": "pending", "created_at": "2026-09-10T10:00:00+03:00",
               "updated_at": ""}
        row.update(data)
        insert(self.db, "client_payment_intents", row)
        return row["id"]

    def test_confirm_closes_linked_intent(self):
        pay = self.sbp.create(amount=800, purpose="Адресник × 2")
        intent = self._intent(sbp_id=pay["id"])
        self.sbp.confirm(pay["id"], authorized="банк")
        row = self.db.one("SELECT * FROM client_payment_intents WHERE id=?", (intent,))
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(row["confirmed_by"], "банк")
        self.assertFalse(self.db.one(
            "SELECT id FROM client_payment_intents WHERE status='pending'"))

    def test_reject_closes_with_reason(self):
        pay = self.sbp.create(amount=800, purpose="Адресник × 2")
        intent = self._intent(sbp_id=pay["id"])
        self.sbp.reject(pay["id"], reason="поступления нет", authorized="кассир")
        row = self.db.one("SELECT * FROM client_payment_intents WHERE id=?", (intent,))
        self.assertEqual(row["status"], "rejected")
        self.assertIn("поступления нет", row["reject_reason"])

    def test_unlinked_intent_matches_by_order_and_amount(self):
        """Клиент платил по статичному QR: связи нет — ищем по заказу и сумме."""
        insert(self.db, "orders", {"id": "o1", "number": "1001", "product": "Адресник",
                                   "status": "new", "qty": 1, "price": 800.0,
                                   "paid": 0.0, "created_at": "2026-09-09T10:00:00+03:00"})
        pay = self.sbp.create(amount=800, order_id="o1", purpose="заказ 1001")
        hit = self._intent(order_id="o1", amount=800.0, sbp_id="")
        other = self._intent(id="cpi-2", order_id="o1", chat_id="2", amount=300.0,
                              sbp_id="")
        self.sbp.confirm(pay["id"], authorized="сверка банка")
        self.assertEqual(self.db.one("SELECT status FROM client_payment_intents WHERE id=?",
                                     (hit,))["status"], "confirmed")
        # другую сумму не трогаем: угадывать чужие заявки опасно
        self.assertEqual(self.db.one("SELECT status FROM client_payment_intents WHERE id=?",
                                     (other,))["status"], "pending")

    def test_event_carries_the_count(self):
        import json as _json
        pay = self.sbp.create(amount=800, purpose="Адресник × 2")
        self._intent(sbp_id=pay["id"])
        self.sbp.confirm(pay["id"], authorized="банк")
        row = self.db.one("SELECT data FROM events WHERE title='СБП-оплата подтверждена'"
                          " ORDER BY id DESC LIMIT 1")
        self.assertEqual(_json.loads(row["data"] or "{}")["intents_closed"], 1)


class PageWiringTests(unittest.TestCase):
    """Экран кассы: кнопка возврата у старшего и тумблер звонка — на месте."""

    @classmethod
    def setUpClass(cls):
        cls.page = (ROOT / "site" / "cashier.html").read_text(encoding="utf-8")

    def test_return_controls(self):
        for needle in ('id="retModal"', "function openReturn", 'data-return=',
                       '/api/cashier/return', "refund_left", 'state.role==="manager"',
                       "retLines", "request_id:\"ret-"):
            self.assertIn(needle, self.page, needle)
        # строки возвращаем по остатку, а не «всё или ничего»
        self.assertIn("уже вернули", self.page)
        self.assertIn('data-max="', self.page)

    def test_ring_toggle(self):
        for needle in ('id="bRing"', "function ringOn", "cashier_ring_muted",
                       "state.ring!==false", "function applyRingBtn"):
            self.assertIn(needle, self.page, needle)
        # звук и вибрация gated, баннер — нет: молчаливая вкладка снова
        # означала бы потерянные платежи
        head = self.page[self.page.index("function ring(loud,text){"):]
        gate = head[:head.index("var bar=")]
        self.assertIn("if(ringOn())", gate)
        self.assertNotIn("bar.hidden=false", gate)

    def test_shift_shows_refunds(self):
        self.assertIn('kv("Возвраты из кассы"', self.page)
        self.assertIn("за вычетом возвратов", self.page)

    def test_npd_days_table_shows_returns(self):
        script = (ROOT / "site" / "assets" / "finance.js").read_text(encoding="utf-8")
        self.assertIn("возврат ${money(d.refunds)}", script)
        page = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
        self.assertIn("finance.js?v=17.0.6", page)

    def test_shell_cache_bumped(self):
        source = (ROOT / "site" / "sw.js").read_text(encoding="utf-8")
        import re
        match = re.search(r"const CACHE = 'printflow-shell-v(\d+)';", source)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(int(match.group(1)), 33)


if __name__ == "__main__":
    unittest.main()

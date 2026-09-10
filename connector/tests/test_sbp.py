"""СБП-ядро (Касса 16.0): деньги не ломаются.

Проверяем контракт платежа: создание (new) не трогает деньги, выручка и долг
меняются только при подтверждении, двойное нажатие не дублирует проводку,
отмена/возврат — явные операции с аудитом, счёт СБП настраиваемый.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.sbp import Sbp, STATUS_CONFIRMED, STATUS_NEW  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "sbp.sqlite3")


def order(db: Database, **overrides) -> dict:
    data = {
        "id": "o1", "number": "1001", "product": "Адресник",
        "customer_name": "Мария", "status": "done",
        "price": 1000, "paid": 0, "created_at": "2026-09-06T10:00:00+03:00",
        "updated_at": "2026-09-06T10:00:00+03:00",
    }
    data.update(overrides)
    return db.upsert("orders", data)


class SbpServiceTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.sbp = Sbp(self.db, self.acc)

    def tearDown(self):
        self.db.close()

    # ------------------------------------------------------------- создание
    def test_create_does_not_touch_money_or_debt(self):
        """Создание платежа (new) не создаёт выручку и не меняет долг."""
        order(self.db)
        p = self.sbp.create(amount=1000, order_id="o1", request_id="r1")
        self.assertEqual(p["status"], STATUS_NEW)
        self.assertEqual(self.db.one("SELECT paid FROM orders WHERE id='o1'")["paid"], 0)
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))
        self.assertIsNone(self.db.one("SELECT * FROM payments WHERE order_id='o1'"))

    def test_create_requires_positive_amount(self):
        order(self.db)
        with self.assertRaisesRegex(ValueError, "больше нуля"):
            self.sbp.create(amount=0, order_id="o1")
        with self.assertRaisesRegex(ValueError, "больше нуля"):
            self.sbp.create(amount=-5, order_id="o1")

    def test_create_rejects_amount_over_debt(self):
        order(self.db, paid=900)  # долг 100
        with self.assertRaisesRegex(ValueError, "больше долга"):
            self.sbp.create(amount=500, order_id="o1")

    def test_create_idempotent_by_request_id(self):
        order(self.db)
        first = self.sbp.create(amount=1000, order_id="o1", request_id="req-1")
        second = self.sbp.create(amount=1000, order_id="o1", request_id="req-1")
        self.assertTrue(second.get("already_recorded"))
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(
            self.db.one("SELECT COUNT(*) n FROM sbp_payments")["n"], 1)

    def test_create_request_id_conflict_raises(self):
        order(self.db)
        self.sbp.create(amount=1000, order_id="o1", request_id="req-1")
        with self.assertRaisesRegex(ValueError, "другого платежа"):
            self.sbp.create(amount=500, order_id="o1", request_id="req-1")

    def test_create_disabled_sbp_raises(self):
        self.db.set_settings({"sbp_enabled": False})
        with self.assertRaisesRegex(ValueError, "отключена"):
            self.sbp.create(amount=100, request_id="r1")

    # --------------------------------------------------------- подтверждение
    def test_confirm_writes_income_on_sbp_account_and_closes_debt(self):
        order(self.db)
        p = self.sbp.create(amount=1000, order_id="o1")
        c = self.sbp.confirm(p["id"], actor="manager")
        self.assertEqual(c["status"], STATUS_CONFIRMED)
        self.assertEqual(c["confirmed_by"], "manager")
        self.assertEqual(self.db.one("SELECT paid FROM orders WHERE id='o1'")["paid"], 1000)
        tx = self.db.one("SELECT * FROM transactions WHERE id=?", (c["tx_id"],))
        self.assertEqual(tx["kind"], "income")
        self.assertEqual(tx["account_id"], "sbp")
        self.assertEqual(tx["amount"], 1000)
        pay = self.db.one("SELECT * FROM payments WHERE id=?", (c["payment_id"],))
        self.assertEqual(pay["method"], "СБП")
        self.assertEqual(pay["account_id"], "sbp")

    def test_confirm_is_idempotent(self):
        """Двойное нажатие «Подтвердить» не пишет вторую проводку."""
        order(self.db)
        p = self.sbp.create(amount=1000, order_id="o1")
        self.sbp.confirm(p["id"])
        again = self.sbp.confirm(p["id"])
        self.assertTrue(again.get("already_recorded"))
        self.assertEqual(
            self.db.one("SELECT COUNT(*) n FROM transactions WHERE account_id='sbp'")["n"], 1)
        self.assertEqual(
            self.db.one("SELECT COUNT(*) n FROM payments WHERE order_id='o1'")["n"], 1)

    def test_reject_never_creates_revenue(self):
        order(self.db)
        p = self.sbp.create(amount=1000, order_id="o1")
        self.sbp.reject(p["id"], reason="не пришло")
        self.assertEqual(self.sbp._get(p["id"])["status"], "rejected")
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))
        self.assertEqual(self.db.one("SELECT paid FROM orders WHERE id='o1'")["paid"], 0)

    def test_cannot_confirm_terminal_payment(self):
        order(self.db)
        p = self.sbp.create(amount=1000, order_id="o1")
        self.sbp.reject(p["id"], reason="не пришло")
        with self.assertRaisesRegex(ValueError, "нельзя"):
            self.sbp.confirm(p["id"])
        p2 = self.sbp.create(amount=1000, order_id="o1", request_id="r2")
        self.sbp.confirm(p2["id"])
        self.sbp.refund(p2["id"], bank_done=True)
        with self.assertRaisesRegex(ValueError, "нельзя"):
            self.sbp.confirm(p2["id"])

    # --------------------------------------------------------------- возврат
    def test_refund_requires_bank_done(self):
        order(self.db)
        p = self.sbp.create(amount=1000, order_id="o1")
        self.sbp.confirm(p["id"])
        with self.assertRaisesRegex(ValueError, "выполнен в банке"):
            self.sbp.refund(p["id"])
        self.assertEqual(self.sbp._get(p["id"])["status"], STATUS_CONFIRMED)

    def test_refund_reverses_income_and_debt(self):
        order(self.db)
        p = self.sbp.create(amount=1000, order_id="o1")
        self.sbp.confirm(p["id"])
        r = self.sbp.refund(p["id"], bank_done=True, actor="owner", note="клиент передумал")
        self.assertEqual(r["status"], "refunded")
        self.assertEqual(r["refunded_by"], "owner")
        self.assertEqual(self.db.one("SELECT paid FROM orders WHERE id='o1'")["paid"], 0)
        expense = self.db.one("SELECT * FROM transactions WHERE kind='expense'")
        self.assertIsNotNone(expense)
        self.assertEqual(expense["account_id"], "sbp")
        self.assertEqual(expense["amount"], 1000)

    def test_refund_is_idempotent(self):
        order(self.db)
        p = self.sbp.create(amount=1000, order_id="o1")
        self.sbp.confirm(p["id"])
        self.sbp.refund(p["id"], bank_done=True)
        again = self.sbp.refund(p["id"], bank_done=True)
        self.assertTrue(again.get("already_recorded"))
        self.assertEqual(
            self.db.one("SELECT COUNT(*) n FROM transactions WHERE kind='expense'")["n"], 1)

    def test_refund_only_for_confirmed(self):
        order(self.db)
        p = self.sbp.create(amount=1000, order_id="o1")
        with self.assertRaisesRegex(ValueError, "только для подтверждённого"):
            self.sbp.refund(p["id"], bank_done=True)

    # ------------------------------------------- продажа без заказа (с полки)
    def test_ad_hoc_payment_writes_income_without_order(self):
        p = self.sbp.create(amount=350, purpose="Адресник × 1", request_id="r-adhoc")
        c = self.sbp.confirm(p["id"])
        self.assertEqual(c["payment_id"], "")
        tx = self.db.one("SELECT * FROM transactions WHERE id=?", (c["tx_id"],))
        self.assertEqual(tx["kind"], "income")
        self.assertEqual(tx["category"], "sale")
        self.assertEqual(tx["account_id"], "sbp")
        self.assertEqual(tx["channel"], "sbp")

    # -------------------------------------------------------- счёт и шаблоны
    def test_account_is_configurable(self):
        order(self.db)
        self.db.set_settings({"sbp_account_id": "bank"})
        p = self.sbp.create(amount=1000, order_id="o1")
        c = self.sbp.confirm(p["id"])
        tx = self.db.one("SELECT * FROM transactions WHERE id=?", (c["tx_id"],))
        self.assertEqual(tx["account_id"], "bank")

    def test_purpose_template(self):
        order(self.db, number="2042")
        self.db.set_settings({"sbp_payment_note": "Заказ {number} · NOZZA"})
        p = self.sbp.create(amount=1000, order_id="o1")
        self.assertEqual(p["purpose"], "Заказ 2042 · NOZZA")

    # ---------------------------------------------------------------- аудит
    def test_audit_covers_every_action(self):
        order(self.db)
        p = self.sbp.create(amount=1000, order_id="o1", request_id="r1")
        self.sbp.confirm(p["id"], actor="manager")
        order(self.db, id="o2", number="1002")
        p2 = self.sbp.create(amount=1000, order_id="o2", request_id="r2")
        self.sbp.reject(p2["id"], actor="manager")
        order(self.db, id="o3", number="1003")
        p3 = self.sbp.create(amount=1000, order_id="o3", request_id="r3")
        self.sbp.confirm(p3["id"])
        self.sbp.refund(p3["id"], bank_done=True, actor="owner")
        actions = [r["action"] for r in self.db.query(
            "SELECT action FROM audit_log WHERE entity='sbp_payment' ORDER BY id")]
        self.assertEqual(actions, ["create", "confirm", "create", "reject",
                                   "create", "confirm", "refund"])


class SbpRouteTests(unittest.TestCase):
    """Маршруты СБП объявлены в реестре и диспетчеризуются через Ctx."""

    def setUp(self):
        from connector.printflow.api import register_routes
        register_routes()

    def test_routes_registered(self):
        from connector.printflow.router import router
        paths = router.paths()
        for path in ("/api/sbp/settings", "/api/sbp/state", "/api/sbp/payments",
                     "/api/sbp/create", "/api/sbp/confirm", "/api/sbp/reject",
                     "/api/sbp/refund"):
            self.assertIn(path, paths, path)

    def test_create_route_through_ctx(self):
        from connector.printflow.router import router
        db = make_db()
        try:
            acc = Accounting(db)
            order(db)
            api = SimpleNamespace(db=db, acc=acc, sbp=Sbp(db, acc))
            status, body = router.dispatch(
                api, "POST", "/api/sbp/create",
                body={"amount": 1000, "order_id": "o1", "request_id": "http-1",
                      "actor": "panel"})
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], STATUS_NEW)
            # 18.0: назначение по заказу — из состава, а не «Оплата заказа №…»
            self.assertEqual(body["purpose"], "NOZZA №1001: Адресник × 1")
        finally:
            db.close()


class SbpNumberTests(unittest.TestCase):
    """Номер СБП-платежа: только вперёд и без дублей.

    Номер — не украшение: он на экране «Входящие», в привязке поступлений из
    банка (``/api/bank/link`` ищет платёж по id ИЛИ номеру) и в назначении
    перевода. ``MAX(CAST(number))+1`` давал один номер двум платежам на
    параллельных созданиях и переиспользовал номер удалённого.
    """

    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.sbp = Sbp(self.db, self.acc)

    def tearDown(self):
        self.db.close()

    def test_numbers_grow_and_never_repeat(self):
        made = [self.sbp.create(amount=100 + i, request_id=f"n{i}")["number"]
                for i in range(5)]
        self.assertEqual(made, ["1", "2", "3", "4", "5"])
        # удаление не возвращает номер: следующий не наступит на историю
        self.db.delete("sbp_payments", made[-1])
        self.assertEqual(self.sbp.create(amount=900, request_id="n-after")["number"], "6")

    def test_parallel_create_gives_unique_numbers(self):
        import threading

        numbers: list[str] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            payment = self.sbp.create(amount=50 + index, request_id=f"p{index}")
            with lock:
                numbers.append(payment["number"])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(numbers), 8)
        self.assertEqual(len(set(numbers)), 8, f"дубли номеров платежей: {sorted(numbers)}")


class SbpRightsTests(unittest.TestCase):
    """Деньги подтверждает человек, а не анонимный запрос из LAN.

    Живой смоук 2026-09-10: `POST /api/sbp/refund` без всяких полномочий
    проводил возврат. Решение №9 ТЗ «Касса 16.0» («возвраты — только
    руководитель») существовало только как кнопка в интерфейсе. Теперь право
    проверяет сервер: как только в «Команде» заведён хотя бы один PIN,
    панели нужен PIN сотрудника, а возврату — PIN старшего.
    """

    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.sbp = Sbp(self.db, self.acc)
        from connector.printflow.staff import Staff
        staff = Staff(self.db)
        emp = staff.add("Ира", "employee", "")
        staff.set_pin(emp["id"], "1111")
        boss = staff.add("Оля", "manager", "")
        staff.set_pin(boss["id"], "2222")

    def tearDown(self):
        self.db.close()

    def _payment(self, amount: float = 500, key: str = "p") -> str:
        return self.sbp.create(amount=amount, request_id=key)["id"]

    def test_settings_advertise_pin_mode(self):
        self.assertTrue(self.sbp.settings()["pins_required"])

    def test_anonymous_confirm_is_blocked_and_writes_nothing(self):
        pid = self._payment()
        with self.assertRaisesRegex(ValueError, "PIN"):
            self.sbp.confirm(pid, actor="panel")
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))
        self.assertEqual(self.db.one("SELECT status FROM sbp_payments WHERE id=?", (pid,))["status"],
                         STATUS_NEW)

    def test_employee_pin_confirms_and_is_named_in_journal(self):
        pid = self._payment()
        out = self.sbp.confirm(pid, pin="1111")
        self.assertEqual(out["status"], STATUS_CONFIRMED)
        self.assertEqual(out["confirmed_by"], "Ира")

    def test_refund_requires_manager_pin(self):
        pid = self._payment()
        self.sbp.confirm(pid, pin="2222")
        with self.assertRaisesRegex(ValueError, "старшего"):
            self.sbp.refund(pid, bank_done=True, note="возврат", pin="1111")
        out = self.sbp.refund(pid, bank_done=True, note="возврат", pin="2222")
        self.assertEqual(out["status"], "refunded")
        self.assertEqual(out["refunded_by"], "Оля")

    def test_reject_requires_pin_too(self):
        pid = self._payment(300, key="rej")
        with self.assertRaisesRegex(ValueError, "PIN"):
            self.sbp.reject(pid, reason="не пришло")
        self.assertEqual(self.sbp.reject(pid, reason="не пришло", pin="1111")["status"], "rejected")

    def test_single_owner_install_still_works_without_pin(self):
        """Пока PIN нет ни у кого (одиночная установка) — как раньше, без запроса."""
        db = make_db()
        try:
            acc = Accounting(db)
            sbp = Sbp(db, acc)
            self.assertFalse(sbp.pins_required())
            pid = sbp.create(amount=300, request_id="solo")["id"]
            self.assertEqual(sbp.confirm(pid)["status"], STATUS_CONFIRMED)
            self.assertEqual(sbp.refund(pid, bank_done=True)["status"], "refunded")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()

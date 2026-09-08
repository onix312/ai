"""И3: PIN кассиров, роли, смены и выемка.

Проверяем: PIN хранится хешем и уникален, вход по PIN даёт имя и роль из
команды, общий код после появления PIN становится «кассиром», выемка и чужая
смена — только старшему (проверка на сервере), смена считает наличные
по окну, СБП и 1С в расчёт ящика не попадают.
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
from connector.printflow.crypto import hash_pin, verify_pin  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.staff import Staff  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "pin-shift.sqlite3")


def add_item(db: Database, item_id: str = "s1", qty: float = 10,
             price: float = 500) -> None:
    db.upsert("shelf_items", {
        "id": item_id, "name": "Адресник", "qty": qty, "price": price,
        "cost_per_unit": 120, "active": 1})


class PinHashTests(unittest.TestCase):
    def test_roundtrip_and_salt(self):
        first, second = hash_pin("1234"), hash_pin("1234")
        self.assertTrue(first.startswith("pin:v1:"))
        self.assertNotEqual(first, second)  # соль разная
        self.assertTrue(verify_pin("1234", first))
        self.assertTrue(verify_pin("1234", second))
        self.assertFalse(verify_pin("4321", first))

    def test_foreign_format_never_matches(self):
        self.assertFalse(verify_pin("1234", ""))
        self.assertFalse(verify_pin("1234", "1234"))
        self.assertFalse(verify_pin("1234", "enc:v1:abc"))
        self.assertFalse(verify_pin("", hash_pin("1234")))


class StaffPinTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.staff = Staff(self.db)
        self.ivan = self.staff.add("Иван", "employee", "")
        self.boss = self.staff.add("Ольга", "manager", "")

    def tearDown(self):
        self.db.close()

    def test_cashier_without_telegram(self):
        """Кассиру без Telegram строка создаётся, chat пустой."""
        self.assertEqual(self.ivan["chat_id"], "")
        again = self.staff.add("Пётр", "employee", "")
        self.assertNotEqual(again["id"], self.ivan["id"])  # не затёр Ивана

    def test_bad_chat_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "chat_id"):
            self.staff.add("Хакер", "employee", "не-число")

    def test_set_pin_rules(self):
        with self.assertRaisesRegex(ValueError, "4–8 цифр"):
            self.staff.set_pin(self.ivan["id"], "12")
        with self.assertRaisesRegex(ValueError, "4–8 цифр"):
            self.staff.set_pin(self.ivan["id"], "123456789")
        with self.assertRaisesRegex(ValueError, "4–8 цифр"):
            self.staff.set_pin(self.ivan["id"], "12ab")
        with self.assertRaisesRegex(ValueError, "не найден"):
            self.staff.set_pin("нет-такого", "1234")

    def test_pin_unique_among_active(self):
        self.staff.set_pin(self.ivan["id"], "1234")
        with self.assertRaisesRegex(ValueError, "уже занят"):
            self.staff.set_pin(self.boss["id"], "1234")
        self.staff.set_pin(self.boss["id"], "4321")
        self.assertEqual(self.staff.pins_count(), 2)

    def test_pin_clear_and_reuse(self):
        self.staff.set_pin(self.ivan["id"], "1234")
        cleared = self.staff.set_pin(self.ivan["id"], "")
        self.assertFalse(cleared["has_pin"])
        self.assertIsNone(self.staff.find_by_pin("1234"))
        self.staff.set_pin(self.boss["id"], "1234")  # освободился
        self.assertEqual(self.staff.pins_count(), 1)

    def test_find_by_pin_skips_inactive(self):
        self.staff.set_pin(self.ivan["id"], "1234")
        self.staff.remove(self.ivan["id"])
        self.assertIsNone(self.staff.find_by_pin("1234"))
        self.assertEqual(self.staff.pins_count(), 0)

    def test_hash_never_leaks(self):
        self.staff.set_pin(self.ivan["id"], "1234")
        for row in self.staff.all():
            self.assertNotIn("pin_hash", row)
        ivan = next(r for r in self.staff.all()
                    if r["id"] == self.ivan["id"])
        boss = next(r for r in self.staff.all() if r["id"] == self.boss["id"])
        self.assertTrue(ivan["has_pin"])
        self.assertFalse(boss["has_pin"])
        raw = self.db.one("SELECT pin_hash FROM staff WHERE id=?",
                          (self.ivan["id"],))
        self.assertTrue(str(raw["pin_hash"]).startswith("pin:v1:"))

    def test_staff_pin_endpoint(self):
        from connector.printflow.api import Api
        api = Api.__new__(Api)
        api.db = self.db
        api.bus = type("Bus", (), {"publish": lambda self, *a: None})()
        code, payload = Api.post(api, "/api/staff/pin",
                                 {"id": self.ivan["id"], "pin": "7777"}, {})
        self.assertEqual(code, 200)
        self.assertTrue(payload["member"]["has_pin"])
        self.assertNotIn("pin_hash", payload["member"])
        self.assertEqual(
            self.staff.find_by_pin("7777")["id"], self.ivan["id"])


class CashierLoginTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc)
        self.staff = Staff(self.db)
        self.db.set_settings({"cashier_code": "1234"})
        add_item(self.db)

    def tearDown(self):
        self.db.close()

    def test_pin_login_gives_name_and_role(self):
        ivan = self.staff.add("Иван", "employee", "")
        self.staff.set_pin(ivan["id"], "1111")
        boss = self.staff.add("Ольга", "manager", "")
        self.staff.set_pin(boss["id"], "2222")
        r = self.cashier.login("1111")
        self.assertEqual(r["role"], "employee")
        self.assertEqual(r["name"], "Иван")
        session = self.cashier.require(r["token"])
        self.assertEqual(session["staff_id"], ivan["id"])
        r2 = self.cashier.login("2222")
        self.assertEqual(r2["role"], "manager")
        self.assertEqual(r2["name"], "Ольга")

    def test_shared_code_manager_until_first_pin(self):
        first = self.cashier.login("1234")
        self.assertEqual(first["role"], "manager")  # старый режим
        ivan = self.staff.add("Иван", "employee", "")
        self.staff.set_pin(ivan["id"], "1111")
        second = Cashier(self.db, self.acc).login("1234")
        self.assertEqual(second["role"], "employee")  # режим ролей

    def test_wrong_pin_and_code(self):
        self.staff.set_pin(self.staff.add("Иван", "employee", "")["id"],
                           "1111")
        with self.assertRaisesRegex(ValueError, "Неверный"):
            self.cashier.login("9999")
        with self.assertRaisesRegex(ValueError, "Неверный"):
            self.cashier.login("0000")

    def test_no_code_no_pins(self):
        self.db.set_settings({"cashier_code": ""})
        with self.assertRaisesRegex(ValueError, "не задан"):
            self.cashier.login("1234")

    def test_pin_only_setup(self):
        """PIN есть, общий код стёрт — вход только по PIN."""
        self.db.set_settings({"cashier_code": ""})
        ivan = self.staff.add("Иван", "employee", "")
        self.staff.set_pin(ivan["id"], "1111")
        r = self.cashier.login("1111")
        self.assertEqual(r["name"], "Иван")
        with self.assertRaisesRegex(ValueError, "Неверный"):
            self.cashier.login("1234")

    def test_inactive_pin_rejected(self):
        ivan = self.staff.add("Иван", "employee", "")
        self.staff.set_pin(ivan["id"], "1111")
        self.staff.remove(ivan["id"])
        with self.assertRaisesRegex(ValueError, "Неверный"):
            self.cashier.login("1111")

    def test_role_gate(self):
        emp = self.cashier.login("1234")["token"]  # manager без PIN
        self.cashier.require_role(emp, "manager")  # ок
        ivan = self.staff.add("Иван", "employee", "")
        self.staff.set_pin(ivan["id"], "1111")
        pin_token = self.cashier.login("1111")["token"]
        with self.assertRaisesRegex(ValueError, "руководителя"):
            self.cashier.require_role(pin_token, "manager")


class SessionNameTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc)
        self.staff = Staff(self.db)
        self.db.set_settings({"cashier_code": "1234"})
        add_item(self.db)
        ivan = self.staff.add("Иван", "employee", "")
        self.staff.set_pin(ivan["id"], "1111")
        self.pin_token = self.cashier.login("1111")["token"]
        self.code_token = self.cashier.login("1234")["token"]

    def tearDown(self):
        self.db.close()

    def test_sale_cashier_from_session_not_client(self):
        r = self.cashier.sell(
            [{"item_id": "s1", "qty": 1}], "cash", self.pin_token,
            cashier_name="Чужак")
        self.assertEqual(r["cashier"], "Иван")  # сессия главнее запроса

    def test_legacy_code_keeps_client_name(self):
        r = self.cashier.sell(
            [{"item_id": "s1", "qty": 1}], "cash", self.code_token,
            cashier_name="Продавец А")
        self.assertEqual(r["cashier"], "Продавец А")
        r2 = self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash",
                               self.code_token)
        self.assertEqual(r2["cashier"], "кассир")

    def test_confirm_actor_from_session(self):
        r = self.cashier.sell(
            [{"item_id": "s1", "qty": 1}], "sbp", self.code_token,
            cashier_name="Продавец А")
        c = self.cashier.confirm_sbp(r["payment_id"], self.pin_token)
        self.assertEqual(c["cashier"], "Продавец А")  # автор продажи — в строке
        audit = self.db.one(
            "SELECT * FROM audit_log WHERE action='confirm_sbp'"
            " ORDER BY rowid DESC")
        self.assertIn("Иван", audit["data"])  # подтвердил — Иван


class ShiftTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc)
        self.staff = Staff(self.db)
        self.db.set_settings({"cashier_code": "1234"})
        add_item(self.db, qty=50)
        ivan = self.staff.add("Иван", "employee", "")
        self.staff.set_pin(ivan["id"], "1111")
        boss = self.staff.add("Ольга", "manager", "")
        self.staff.set_pin(boss["id"], "2222")
        self.emp = self.cashier.login("1111")["token"]
        self.mgr = self.cashier.login("2222")["token"]

    def tearDown(self):
        self.db.close()

    def test_open_current_close(self):
        cur = self.cashier.current_shift(self.emp)
        self.assertFalse(cur["open"])
        opened = self.cashier.open_shift(self.emp, 1000)
        self.assertTrue(opened["open"])
        self.assertEqual(opened["shift"]["cashier"], "Иван")
        self.assertEqual(opened["live"]["expected"], 1000)
        with self.assertRaisesRegex(ValueError, "уже открыта"):
            self.cashier.open_shift(self.mgr, 0)
        done = self.cashier.close_shift(self.emp, 1000)
        self.assertEqual(done["shift"]["diff"], 0)
        self.assertFalse(self.cashier.current_shift(self.emp)["open"])

    def test_open_cash_cannot_be_negative(self):
        with self.assertRaisesRegex(ValueError, "меньше нуля"):
            self.cashier.open_shift(self.emp, -5)

    def test_cash_sale_grows_expected_sbp_does_not(self):
        self.cashier.open_shift(self.emp, 1000)
        self.cashier.sell([{"item_id": "s1", "qty": 2}], "cash", self.emp)
        live = self.cashier.current_shift(self.emp)["live"]
        self.assertEqual(live["income_cash"], 1000)
        self.assertEqual(live["expected"], 2000)
        sbp = self.cashier.sell([{"item_id": "s1", "qty": 1}], "sbp", self.emp)
        self.cashier.confirm_sbp(sbp["payment_id"], self.emp)
        live = self.cashier.current_shift(self.emp)["live"]
        self.assertEqual(live["income_cash"], 1000)  # СБП — не наличные
        done = self.cashier.close_shift(self.emp, 2000)
        self.assertEqual(done["shift"]["diff"], 0)

    def test_diff_recorded_not_blocking(self):
        self.cashier.open_shift(self.emp, 0)
        self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash", self.emp)
        done = self.cashier.close_shift(self.emp, 400, note="недосдача?")
        self.assertEqual(done["shift"]["expected"], 500)
        self.assertEqual(done["shift"]["diff"], -100)
        audit = self.db.one(
            "SELECT * FROM audit_log WHERE entity='cashier_shift'"
            " AND action='close_shift' ORDER BY rowid DESC")
        self.assertIn("-100", audit["detail"])

    def test_foreign_shift_manager_only(self):
        self.cashier.open_shift(self.emp, 0)
        # второй кассир — тоже employee: закрыть чужую не может
        petr = self.staff.add("Пётр", "employee", "")
        self.staff.set_pin(petr["id"], "3333")
        petr_token = self.cashier.login("3333")["token"]
        with self.assertRaisesRegex(ValueError, "чужая смена"):
            self.cashier.close_shift(petr_token, 0)
        done = self.cashier.close_shift(self.mgr, 0)  # старший — может
        self.assertEqual(done["shift"]["diff"], 0)

    def test_old_income_outside_window(self):
        self.acc.add_transaction("income", "sale", 5000, "Старая выручка",
                                 channel="shelf", at="2020-01-01T10:00:00")
        self.cashier.open_shift(self.emp, 0)
        live = self.cashier.current_shift(self.emp)["live"]
        self.assertEqual(live["income_cash"], 0)

    def test_1c_money_shown_aside_not_in_expected(self):
        self.cashier.open_shift(self.emp, 0)
        from connector.printflow.shelf import Shelf
        Shelf(self.db).sale("s1", 2, 500, record_income=False, source="1c",
                            external_id="kkm-1")
        live = self.cashier.current_shift(self.emp)["live"]
        self.assertEqual(live["income_1c"], 1000)
        self.assertEqual(live["expected"], 0)


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc)
        self.staff = Staff(self.db)
        self.db.set_settings({"cashier_code": "1234"})
        add_item(self.db, qty=50)
        ivan = self.staff.add("Иван", "employee", "")
        self.staff.set_pin(ivan["id"], "1111")
        boss = self.staff.add("Ольга", "manager", "")
        self.staff.set_pin(boss["id"], "2222")
        self.emp = self.cashier.login("1111")["token"]
        self.mgr = self.cashier.login("2222")["token"]

    def tearDown(self):
        self.db.close()

    def earn(self, qty: int = 2):
        self.cashier.sell([{"item_id": "s1", "qty": qty}], "cash", self.emp)

    def test_employee_collect_blocked(self):
        self.earn()
        with self.assertRaisesRegex(ValueError, "руководителя"):
            self.cashier.collect(self.emp, 100)

    def test_manager_collect_links_shift(self):
        self.cashier.open_shift(self.emp, 0)
        self.earn()  # 1000 ₽ наличными
        r = self.cashier.collect(self.mgr, 400, note="в сейф")
        self.assertTrue(r["ok"])
        shift_id = self.cashier.current_shift(self.emp)["shift"]["id"]
        self.assertEqual(r["shift_id"], shift_id)
        col = self.db.one("SELECT * FROM shelf_collections WHERE id=?",
                          (r["collection"]["id"],))
        self.assertEqual(col["shift_id"], shift_id)
        live = self.cashier.current_shift(self.emp)["live"]
        self.assertEqual(live["collected"], 400)
        self.assertEqual(live["expected"], 600)

    def test_collect_outside_shift(self):
        self.earn()
        r = self.cashier.collect(self.mgr, 100)
        self.assertEqual(r["shift_id"], "")

    def test_collect_over_limit_blocked(self):
        self.earn()  # 1000 ₽
        with self.assertRaisesRegex(ValueError, "забрать"):
            self.cashier.collect(self.mgr, 5000)

    def test_legacy_manager_collects_without_pins(self):
        db = make_db()
        try:
            acc = Accounting(db)
            cashier = Cashier(db, acc)
            db.set_settings({"cashier_code": "1234"})
            add_item(db, qty=50)
            token = cashier.login("1234")["token"]
            cashier.sell([{"item_id": "s1", "qty": 2}], "cash", token)
            r = cashier.collect(token, 100)
            self.assertTrue(r["ok"])  # режим без PIN — всё можно
        finally:
            db.close()


class ShiftRouteTests(unittest.TestCase):
    def test_shift_routes_registered(self):
        from connector.printflow.router import router
        paths = {getattr(r, "path", "") for r in router.routes()}
        for path in ("/api/cashier/shift/current", "/api/cashier/shift/open",
                     "/api/cashier/shift/close", "/api/cashier/collect"):
            self.assertIn(path, paths)


class PanelReservesVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        from connector.printflow.stock import Stock
        self.stock = Stock(self.db)
        self.db.upsert("warehouses", {
            "id": "wh-zone", "name": "Витрина", "kind": "shelf",
            "archived": 0, "position": 0})
        self.db.upsert("nomenclature", {
            "id": "nom-1", "name": "Адресник", "unit": "шт"})
        self.stock.add_move("nom-1", "wh-zone", 10, 0, doc_kind="receipt",
                            note="старт")

    def tearDown(self):
        self.db.close()

    def api(self):
        from connector.printflow.api import Api
        api = Api.__new__(Api)
        api.db = self.db
        api.stock = self.stock
        return api

    def test_warehouses_lists_hold_with_kind(self):
        self.stock.hold("nom-1", "wh-zone", 2, "cs-sale-1")
        code, payload = self.api().get("/api/warehouses", {})
        self.assertEqual(code, 200)
        holds = [r for r in payload["reserves"] if r.get("kind") == "hold"]
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0]["doc_id"], "cs-sale-1")
        self.assertEqual(holds[0]["nom_name"], "Адресник")

    def test_warehouses_releases_expired_hold_lazily(self):
        self.stock.hold("nom-1", "wh-zone", 2, "cs-old")
        self.db.execute("UPDATE reserves SET at='2020-01-01T00:00:00'"
                        " WHERE doc_id='cs-old'")
        code, payload = self.api().get("/api/warehouses", {})
        self.assertEqual(code, 200)
        self.assertEqual(
            [r for r in payload["reserves"] if r.get("doc_id") == "cs-old"],
            [])
        row = self.db.one("SELECT state FROM reserves WHERE doc_id='cs-old'")
        self.assertEqual(row["state"], "released")

    def test_reserves_endpoint_releases_expired_too(self):
        self.stock.hold("nom-1", "wh-zone", 1, "cs-old-2")
        self.db.execute("UPDATE reserves SET at='2020-01-01T00:00:00'"
                        " WHERE doc_id='cs-old-2'")
        code, payload = self.api().get("/api/reserves", {})
        self.assertEqual(code, 200)
        self.assertEqual(payload["reserves"], [])


class CashierShiftPageTests(unittest.TestCase):
    def setUp(self):
        self.page = (ROOT / "site" / "cashier.html").read_text(
            encoding="utf-8")

    def test_shift_tab_and_endpoints_wired(self):
        for needle in ("tabShift", "viewShift", "shiftBox", "loadShift",
                       "/api/cashier/shift/current", "/api/cashier/shift/open",
                       "/api/cashier/shift/close", "/api/cashier/collect",
                       "bShiftOpen", "bShiftClose", "bShiftCollect"):
            self.assertIn(needle, self.page)

    def test_cashier_identity_and_holds_shown(self):
        for needle in ("whoPill", "cashier_name", "cashier_role",
                       "cashier_legacy", "function renderWho", "holdBadge",
                       "p.holds", "отложено"):
            self.assertIn(needle, self.page)

    def test_single_document_tail_and_theme_toggle(self):
        self.assertEqual(self.page.count("</body>"), 1)
        self.assertEqual(self.page.count("</html>"), 1)
        self.assertEqual(self.page.count("/assets/theme-toggle.js?v=17.0.1"), 1)
        self.assertEqual(self.page.split("</html>", 1)[1].strip(), "")

    def test_shift_tab_really_switches_views(self):
        for needle in ('$("tabShift").classList.toggle("on",state.tab==="shift")',
                       '$("viewShift").hidden=state.tab!=="shift"',
                       'if(state.tab==="shift") loadShift();'):
            self.assertIn(needle, self.page)

    def test_money_actions_have_confirm_and_busy_guards(self):
        for needle in ('Очистить корзину? Товары из текущей продажи уберутся.',
                       'withBusy(btn,function(){',
                       '/api/cashier/confirm-sbp',
                       '/api/cashier/collect',
                       'Забрать из ящика',
                       'Выемка запишется в учёт.',
                       'Продажи смены недоступны',
                       'Укажите причину отклонения'):
            self.assertIn(needle, self.page)

    def test_mobile_cart_and_theme_safe_logo_are_wired(self):
        for needle in ('function shouldOpenCartModal()',
                       'window.matchMedia("(max-width: 520px)")',
                       'openCartModal();return;',
                       '/assets/brand/nozza-mark.svg'):
            self.assertIn(needle, self.page)
        self.assertNotIn('/assets/brand/nozza-mark-white.svg', self.page)

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


class PanelVisibilityPageTests(unittest.TestCase):
    def test_products_renders_holds(self):
        text = (ROOT / "site" / "assets" / "products.js").read_text(
            encoding="utf-8")
        for needle in ("холд СБП", "data-reserve-release", "holdCount",
                       "r.kind === 'hold'"):
            self.assertIn(needle, text)

    def test_staff_renders_pin(self):
        text = (ROOT / "site" / "assets" / "clientbot.js").read_text(
            encoding="utf-8")
        for needle in ("/api/staff/pin", "data-pin-set", "data-pin-clear",
                       "data-pin-input", "has_pin"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()

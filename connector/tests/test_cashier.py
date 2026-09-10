"""Мобильная касса в LAN (Касса 16.0): продажа и СБП без дублей и расхождений.

Проверяем: вход по коду, наличная продажа сразу в журнал (in_shop), СБП-продажа
не трогает склад и выручку до подтверждения, подтверждение в одной транзакции
списывает склад и пишет деньги на счёт СБП, повторные вызовы идемпотентны.
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

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "cashier.sqlite3")


def add_item(db: Database, item_id: str = "s1", qty: float = 10, price: float = 500) -> None:
    db.upsert("shelf_items", {
        "id": item_id, "name": "Адресник", "qty": qty, "price": price,
        "cost_per_unit": 120, "active": 1})


class CashierSchemaTests(unittest.TestCase):
    def test_old_nom_groups_gets_color_column(self):
        """База до 17.0 не должна ронять каталог ошибкой no such column."""
        folder = tempfile.TemporaryDirectory()
        _held.append(folder)
        path = pathlib.Path(folder.name) / "old-cashier.sqlite3"
        db = Database(path)
        db.close()
        import sqlite3
        connection = sqlite3.connect(path)
        connection.execute("ALTER TABLE nom_groups DROP COLUMN color")
        connection.commit()
        connection.close()

        migrated = Database(path)
        try:
            columns = {row["name"] for row in migrated.query("PRAGMA table_info(nom_groups)")}
            self.assertIn("color", columns)
            self.assertEqual(migrated.one("SELECT color FROM nom_groups LIMIT 1")["color"],
                             "#6366f1")
        finally:
            migrated.close()


class CashierTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc)
        self.db.set_settings({"cashier_code": "1234"})
        add_item(self.db)
        self.token = self.cashier.login("1234")["token"]

    def tearDown(self):
        self.db.close()

    def qty(self, item_id: str = "s1") -> float:
        return float(self.db.one("SELECT qty FROM shelf_items WHERE id=?", (item_id,))["qty"])

    # ---------------------------------------------------------------- вход
    def test_login_requires_code_and_rejects_wrong(self):
        self.db.set_settings({"cashier_code": ""})
        with self.assertRaisesRegex(ValueError, "не задан"):
            Cashier(self.db, self.acc).login("1234")
        self.db.set_settings({"cashier_code": "5678"})
        with self.assertRaisesRegex(ValueError, "Неверный"):
            self.cashier.login("1111")

    def test_session_expires(self):
        self.cashier.require(self.token)  # ок
        self.cashier.logout(self.token)
        with self.assertRaisesRegex(ValueError, "истекла"):
            self.cashier.require(self.token)

    # ------------------------------------------------------- наличная продажа
    def test_cash_sale_writes_income_and_deducts_stock(self):
        r = self.cashier.sell([{"item_id": "s1", "qty": 2}], "cash", self.token)
        self.assertEqual(r["amount"], 1000)
        self.assertTrue(r["paid"])
        self.assertEqual(self.qty(), 8)
        tx = self.db.one("SELECT * FROM transactions WHERE kind='income'")
        self.assertIsNotNone(tx)
        self.assertEqual(tx["channel"], "shelf")
        self.assertEqual(tx["amount"], 1000)
        self.assertEqual(tx["account_id"], "cash")  # наличные — касса по умолчанию

    def test_cash_sale_idempotent_by_request_id(self):
        self.cashier.sell([{"item_id": "s1", "qty": 2}], "cash", self.token, request_id="r1")
        again = self.cashier.sell([{"item_id": "s1", "qty": 2}], "cash", self.token, request_id="r1")
        self.assertTrue(again.get("already_recorded"))
        self.assertEqual(self.qty(), 8)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM transactions")["n"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM cashier_sales")["n"], 1)

    def test_sell_rejects_oversell(self):
        with self.assertRaisesRegex(ValueError, "нельзя"):
            self.cashier.sell([{"item_id": "s1", "qty": 11}], "cash", self.token)

    # -------------------------------------------------------- СБП-продажа
    def test_sbp_sale_does_not_touch_stock_or_money_until_confirm(self):
        r = self.cashier.sell([{"item_id": "s1", "qty": 2}], "sbp", self.token)
        self.assertFalse(r["paid"])
        self.assertTrue(r["payment_id"])
        self.assertEqual(self.qty(), 10)  # склад не тронут
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))
        p = self.db.one("SELECT * FROM sbp_payments WHERE id=?", (r["payment_id"],))
        self.assertIn(p["status"], ("new", "pending"))
        self.assertEqual(p["amount"], 1000)

    def test_sbp_retry_returns_same_payment_and_qr_payload(self):
        first = self.cashier.sell(
            [{"item_id": "s1", "qty": 2}], "sbp", self.token, request_id="mobile-1")
        again = self.cashier.sell(
            [{"item_id": "s1", "qty": 2}], "sbp", self.token, request_id="mobile-1")
        self.assertTrue(again["already_recorded"])
        self.assertEqual(again["sale_id"], first["sale_id"])
        self.assertEqual(again["payment_id"], first["payment_id"])
        self.assertIn("qr", again)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM cashier_sales")["n"], 1)

    def test_sbp_confirm_deducts_stock_and_writes_income_on_sbp_account(self):
        r = self.cashier.sell([{"item_id": "s1", "qty": 2}], "sbp", self.token)
        c = self.cashier.confirm_sbp(r["payment_id"], self.token)
        self.assertTrue(c["confirmed"])
        self.assertEqual(self.qty(), 8)
        tx = self.db.one("SELECT * FROM transactions WHERE kind='income'")
        self.assertEqual(tx["account_id"], "sbp")
        self.assertEqual(tx["amount"], 1000)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM transactions")["n"], 1)

    def test_sbp_confirm_is_idempotent(self):
        r = self.cashier.sell([{"item_id": "s1", "qty": 2}], "sbp", self.token)
        self.cashier.confirm_sbp(r["payment_id"], self.token)
        again = self.cashier.confirm_sbp(r["payment_id"], self.token)
        self.assertTrue(again.get("already_recorded"))
        self.assertEqual(self.qty(), 8)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM transactions")["n"], 1)
        self.assertEqual(
            self.db.one("SELECT COUNT(*) n FROM shelf_moves WHERE kind='sale'")["n"], 1)

    def test_sbp_reject_leaves_stock_and_money_untouched(self):
        r = self.cashier.sell([{"item_id": "s1", "qty": 2}], "sbp", self.token)
        self.cashier.reject_sbp(r["payment_id"], self.token, reason="не пришло")
        self.assertEqual(self.qty(), 10)
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))
        self.assertEqual(
            self.db.one("SELECT status FROM sbp_payments WHERE id=?", (r["payment_id"],))["status"],
            "rejected")
        self.assertEqual(self.cashier.incoming()["payments"], [],
                         "отклонённая оплата не должна оставаться во входящих")

    def test_sbp_confirm_fails_cleanly_on_shortfall(self):
        """Полку продали во время сверки — подтверждение не должно ничего сломать."""
        r = self.cashier.sell([{"item_id": "s1", "qty": 5}], "sbp", self.token)
        self.db.execute("UPDATE shelf_items SET qty=3 WHERE id='s1'")  # конкурент продал
        with self.assertRaisesRegex(ValueError, "нельзя"):
            self.cashier.confirm_sbp(r["payment_id"], self.token)
        self.assertEqual(self.qty(), 3)
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))
        p = self.db.one("SELECT * FROM sbp_payments WHERE id=?", (r["payment_id"],))
        self.assertIn(p["status"], ("new", "pending"))  # деньги не подтверждены

    # ---------------------------------------------------------------- аудит
    def test_audit_covers_sell_confirm_reject(self):
        r = self.cashier.sell([{"item_id": "s1", "qty": 1}], "sbp", self.token)
        self.cashier.confirm_sbp(r["payment_id"], self.token)
        self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash", self.token)
        actions = [x["action"] for x in self.db.query(
            "SELECT action FROM audit_log WHERE entity='cashier_sale' ORDER BY id")]
        self.assertEqual(actions, ["sell", "confirm_sbp", "sell"])

    # -------------------------------------------------------------- каталог
    def test_catalog_shows_shelf_items(self):
        items = self.cashier.catalog()["items"]
        self.assertEqual([i["id"] for i in items], ["s1"])
        self.assertEqual(items[0]["price"], 500)
        self.assertEqual(items[0]["qty"], 10)
        self.assertEqual(items[0]["shelf_qty"], 10)
        self.assertEqual(items[0]["stock_qty"], 0)
        self.assertFalse(items[0]["price_missing"])

    def test_catalog_hides_inactive_items(self):
        self.db.execute("UPDATE shelf_items SET active=0 WHERE id='s1'")
        self.assertEqual(self.cashier.catalog()["items"], [])


class CashierStockCatalogTests(unittest.TestCase):
    """Единый каталог: товар со склада виден кассе без дублей на витрине."""

    def setUp(self):
        from connector.printflow.stock import Stock
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc)
        self.db.set_settings({"cashier_code": "1234"})
        self.stock = Stock(self.db)
        self.db.upsert("nomenclature", {
            "id": "nom1", "name": "Органайзер", "kind": "product",
            "unit": "шт", "archived": 0})
        self.db.upsert("prices", {
            "id": "pr1", "nom_id": "nom1", "price_type_id": "retail",
            "price": 700, "at": "2026-01-01T00:00:00"})
        self.stock.add_move("nom1", "home", 5, 500, doc_kind="receipt")
        self.token = self.cashier.login("1234")["token"]

    def tearDown(self):
        self.db.close()

    def one_item(self) -> dict:
        items = self.cashier.catalog()["items"]
        self.assertEqual(len(items), 1, items)
        return items[0]

    def test_stock_goods_appear_without_shelf_record(self):
        """Товар заведён в номенклатуре и оприходован — касса его видит."""
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM shelf_items")["n"], 0)
        item = self.one_item()
        self.assertEqual(item["id"], "stock:nom1")
        self.assertEqual(item["source"], "stock")
        self.assertEqual(item["name"], "Органайзер")
        self.assertEqual(item["price"], 700)
        self.assertEqual(item["qty"], 5)
        self.assertEqual(item["status"], "ok")

    def test_reserved_stock_is_not_offered_to_cashier(self):
        """Зарезервированное под заказ кассе не показываем."""
        self.stock.reserve("nom1", 3, order_id="o1", warehouse_id="home")
        self.assertEqual(self.one_item()["qty"], 2)

    def test_cash_sale_pulls_goods_from_stock_to_shelf(self):
        r = self.cashier.sell([{"item_id": "stock:nom1", "qty": 2}], "cash", self.token)
        self.assertEqual(r["amount"], 1400)
        self.assertTrue(r["paid"])
        # склад списан движением регистра, а не правкой остатка
        self.assertEqual(self.stock.qty("nom1", "home"), 3)
        moves = self.db.query("SELECT qty, doc_kind FROM stock_moves ORDER BY rowid")
        # И2: приход → расход со склада → приход на витрину → продажа с витрины
        self.assertEqual([m["doc_kind"] for m in moves],
                         ["receipt", "move", "move", "sale"])
        self.assertEqual(moves[-1]["qty"], -2)
        self.assertEqual(self.stock.qty("nom1", "shelf"), 0)  # привезли и продали
        # позиция витрины создалась сама и сразу продана
        item = self.db.one("SELECT * FROM shelf_items")
        self.assertEqual(item["nom_id"], "nom1")
        self.assertEqual(item["qty"], 0)
        tx = self.db.one("SELECT * FROM transactions WHERE kind='income'")
        self.assertEqual(tx["amount"], 1400)

    def test_catalog_merges_shelf_and_stock_into_one_tile(self):
        self.cashier.sell([{"item_id": "stock:nom1", "qty": 1}], "cash", self.token)
        self.db.execute("UPDATE shelf_items SET qty=2")  # витрина пополнена
        item = self.one_item()
        self.assertEqual(item["source"], "shelf")
        self.assertEqual(item["shelf_qty"], 2)
        self.assertEqual(item["stock_qty"], 4)
        self.assertEqual(item["qty"], 6)

    def test_sale_tops_up_shelf_from_stock_when_short(self):
        """Продаём больше, чем на полке: недостающее приезжает со склада."""
        from connector.printflow.shelf import Shelf
        self.cashier.sell([{"item_id": "stock:nom1", "qty": 1}], "cash", self.token)
        item_id = self.db.one("SELECT id FROM shelf_items")["id"]
        # И2: витрину пополняем приходом (полка + регистр разом), а не прямым
        # UPDATE — прямое число в обход регистра единый учёт не признаёт.
        Shelf(self.db).produce(item_id, 1)
        self.cashier.sell([{"item_id": item_id, "qty": 3}], "cash", self.token)
        self.assertEqual(self.db.one("SELECT qty FROM shelf_items")["qty"], 0)
        self.assertEqual(self.stock.qty("nom1", "home"), 2)  # 5 − 1 − 2

    def test_sell_rejects_more_than_shelf_plus_stock(self):
        with self.assertRaisesRegex(ValueError, "нельзя"):
            self.cashier.sell([{"item_id": "stock:nom1", "qty": 6}], "cash", self.token)
        self.assertEqual(self.stock.qty("nom1", "home"), 5)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM shelf_items")["n"], 0)

    def test_sell_requires_price(self):
        self.db.execute("DELETE FROM prices")
        item = self.one_item()
        self.assertTrue(item["price_missing"])
        with self.assertRaisesRegex(ValueError, "цена не задана"):
            self.cashier.sell([{"item_id": "stock:nom1", "qty": 1}], "cash", self.token)
        self.assertEqual(self.stock.qty("nom1", "home"), 5)

    def test_sbp_sale_from_stock_pulls_and_holds_until_confirm(self):
        """И2: СБП-продажа сразу откладывает товар на полку и ставит холд."""
        r = self.cashier.sell([{"item_id": "stock:nom1", "qty": 2}], "sbp", self.token)
        self.assertFalse(r["paid"])
        # товар переехал на витрину и занят холдом, денег пока нет
        self.assertEqual(self.stock.qty("nom1", "home"), 3)
        self.assertEqual(self.db.one("SELECT qty FROM shelf_items")["qty"], 2)
        hold = self.db.one("SELECT * FROM reserves WHERE doc_id=? AND state='active'",
                           (r["sale_id"],))
        self.assertIsNotNone(hold)
        self.assertEqual(hold["kind"], "hold")
        self.assertEqual(hold["qty"], 2)
        self.assertIsNone(self.db.one("SELECT * FROM transactions WHERE kind='income'"))
        self.cashier.confirm_sbp(r["payment_id"], self.token)
        self.assertEqual(self.stock.qty("nom1", "home"), 3)
        self.assertEqual(self.db.one("SELECT qty FROM shelf_items")["qty"], 0)
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) n FROM reserves WHERE doc_id=? AND state='active'",
            (r["sale_id"],))["n"], 0)
        tx = self.db.one("SELECT * FROM transactions WHERE kind='income'")
        self.assertEqual(tx["account_id"], "sbp")
        self.assertEqual(tx["amount"], 1400)

    def test_sbp_confirm_from_stock_is_idempotent(self):
        r = self.cashier.sell([{"item_id": "stock:nom1", "qty": 2}], "sbp", self.token)
        self.cashier.confirm_sbp(r["payment_id"], self.token)
        again = self.cashier.confirm_sbp(r["payment_id"], self.token)
        self.assertTrue(again.get("already_recorded"))
        self.assertEqual(self.stock.qty("nom1", "home"), 3)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM transactions")["n"], 1)

    def test_showcase_and_archived_goods_stay_out_of_cashier(self):
        self.db.upsert("nomenclature", {
            "id": "nom2", "name": "Витрина", "kind": "showcase",
            "unit": "шт", "archived": 0})
        self.stock.add_move("nom2", "home", 3, 0, doc_kind="receipt")
        self.db.upsert("nomenclature", {
            "id": "nom3", "name": "Архивный", "kind": "product",
            "unit": "шт", "archived": 1})
        self.stock.add_move("nom3", "home", 3, 300, doc_kind="receipt")
        self.assertEqual([i["nom_id"] for i in self.cashier.catalog()["items"]], ["nom1"])


class CashierRouteTests(unittest.TestCase):
    def setUp(self):
        from connector.printflow.api import register_routes
        register_routes()

    def test_routes_registered(self):
        from connector.printflow.router import router
        paths = router.paths()
        for path in ("/api/cashier/login", "/api/cashier/logout",
                     "/api/cashier/catalog", "/api/cashier/incoming",
                     "/api/cashier/sell", "/api/cashier/confirm-sbp",
                     "/api/cashier/reject-sbp", "/api/cashier/reconcile"):
            self.assertIn(path, paths, path)


class CashierPageTests(unittest.TestCase):
    """Регрессия экрана кассы: токен добавляется к пути, а не заменяет его."""

    page = (ROOT / "site" / "cashier.html").read_text(encoding="utf-8")

    def test_with_token_keeps_request_path(self):
        line = [ln for ln in self.page.splitlines() if "function withToken" in ln]
        self.assertEqual(len(line), 1, "функция withToken должна быть одна")
        self.assertIn("return p +", line[0],
                      "URL должен начинаться с пути запроса, иначе каталог не грузится")
        self.assertNotIn("return state.token +", line[0])

    def test_catalog_and_incoming_go_through_with_token(self):
        for path in ("/api/cashier/catalog", "/api/cashier/incoming"):
            self.assertIn(f'withToken("{path}")', self.page)

    def test_payment_qr_is_rendered_by_bundled_generator(self):
        """QR рисуется своим генератором qr.js — без внешних сервисов."""
        self.assertIn("/assets/qr.js", self.page)
        self.assertIn("window.QR.svg(qr.text", self.page)
        self.assertIn('id="qrModal"', self.page)
        for external in ("api.qrserver.com", "chart.googleapis.com", "qrcode.js"):
            self.assertNotIn(external, self.page)


class CashierSbpQrTests(unittest.TestCase):
    """Свой QR для оплаты: строка банка → картинка на экране кассы."""

    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc)
        self.db.set_settings({"cashier_code": "1234"})
        add_item(self.db)
        self.token = self.cashier.login("1234")["token"]

    def tearDown(self):
        self.db.close()

    def test_catalog_reports_missing_shop_qr(self):
        qr = self.cashier.catalog()["sbp"]
        self.assertEqual(qr["text"], "")
        self.assertEqual(qr["kind"], "")
        # Ни реквизитов, ни QR банка — подсказка говорит, что заполнить
        self.assertIn("реквизиты", qr["hint"].lower())

    def test_static_shop_qr_from_settings(self):
        self.db.set_settings({"sbp_shop_qr": "https://qr.nspk.ru/AS100012345",
                              "sbp_bank_name": "Т-Банк"})
        qr = self.cashier.catalog()["sbp"]
        self.assertEqual(qr["text"], "https://qr.nspk.ru/AS100012345")
        self.assertEqual(qr["kind"], "static")
        self.assertEqual(qr["bank_name"], "Т-Банк")
        self.assertEqual(qr["hint"], "")

    def test_sbp_sale_returns_qr_for_customer(self):
        self.db.set_settings({"sbp_shop_qr": "https://qr.nspk.ru/AS100012345"})
        sale = self.cashier.sell([{"item_id": "s1", "qty": 2}], "sbp", self.token)
        self.assertEqual(sale["qr"]["text"], "https://qr.nspk.ru/AS100012345")
        self.assertEqual(sale["qr"]["kind"], "static")
        self.assertEqual(sale["qr"]["amount"], 1000)
        # 18.0: назначение — из товаров, а не «Продажа на кассе · N поз.»
        self.assertEqual(sale["qr"]["purpose"], "NOZZA: Адресник × 2")

    def test_dynamic_payment_qr_wins_over_shop_qr(self):
        """Если банк выдал динамический QR с суммой — показываем его."""
        self.db.set_settings({"sbp_shop_qr": "https://qr.nspk.ru/static"})
        payment = {"qr_payload": "https://qr.nspk.ru/AD200099?amount=1000",
                   "purpose": "Продажа"}
        qr = self.cashier.payment_qr(payment, 1000)
        self.assertEqual(qr["text"], "https://qr.nspk.ru/AD200099?amount=1000")
        self.assertEqual(qr["kind"], "dynamic")

    def test_qr_string_is_encodable_by_own_generator(self):
        """Строка банка помещается в QR, который рисует PrintFlow сам."""
        from connector.printflow.qrgen import svg
        picture = svg("https://qr.nspk.ru/AS100012345", scale=4)
        self.assertTrue(picture.startswith("<svg"))


class PersistentSessionTests(unittest.TestCase):
    """Сессия кассира переживает рестарт коннектора (надёжность из отчётов 16.1).

    Обещание в документации было такое: «сессия живёт до перезагрузки сервера»,
    а по факту токен держался только в памяти процесса — обновление или падение
    коннектора выставляло кассира посреди смены с «введите код снова». Теперь
    токен (его sha256, не сам токен) лежит в `cashier_tokens` с абсолютным
    сроком в 12 часов.
    """

    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc)
        self.db.set_settings({"cashier_code": "1234"})
        add_item(self.db)

    def tearDown(self):
        self.db.close()

    def test_session_survives_new_process(self):
        token = self.cashier.login("1234")["token"]
        # «процесс упал»: новый Cashier на той же базе
        revived = Cashier(self.db, self.acc)
        self.assertEqual(revived.require(token)["role"], "manager")

    def test_database_holds_only_a_hash(self):
        token = self.cashier.login("1234")["token"]
        row = self.db.one("SELECT * FROM cashier_tokens")
        self.assertNotIn(token, str(row["token_hash"]))
        self.assertNotIn(token, str(dict(row)))

    def test_logout_kills_the_stored_session(self):
        token = self.cashier.login("1234")["token"]
        self.cashier.logout(token)
        self.assertIsNone(self.db.one("SELECT * FROM cashier_tokens"))
        with self.assertRaisesRegex(ValueError, "истекла"):
            Cashier(self.db, self.acc).require(token)

    def test_expiry_is_absolute(self):
        token = self.cashier.login("1234")["token"]
        self.db.execute("UPDATE cashier_tokens SET expires_at='2020-01-01T00:00:00'")
        revived = Cashier(self.db, self.acc)
        revived._sessions.clear()
        with self.assertRaisesRegex(ValueError, "истекла"):
            revived.require(token)
        # просроченный токен заодно вычищается из базы, реестр не растёт
        self.assertIsNone(self.db.one("SELECT * FROM cashier_tokens"))

    def test_pin_session_keeps_name_and_role(self):
        from connector.printflow.staff import Staff
        staff = Staff(self.db)
        ira = staff.add("Ира", "employee", "")
        staff.set_pin(ira["id"], "1111")
        token = self.cashier.login("1111")["token"]
        revived = Cashier(self.db, self.acc)
        session = revived.require(token)
        self.assertEqual(session["role"], "employee")
        self.assertEqual(session["name"], "Ира")
        with self.assertRaisesRegex(ValueError, "руководителя"):
            revived.require_role(token, "manager")


if __name__ == "__main__":
    unittest.main()

"""Офлайн-контур кассы (17.0.8): продажа без связи и её выгрузка.

Касса живёт в LAN, но Wi-Fi и выключение сервера никто не отменял. Правила,
которые здесь защищаются:

* наличная корзина без сети не пропадает — она доходит до сервера позже с тем
  же ``request_id``, поэтому вторая отправка не создаёт вторую продажу;
* время продажи — серверное (офлайн-штамп только показывает, что продажа была
  локальной), часы «вперёд» и «три дня назад» отклоняются;
* расхождение с остатком при выгрузке не блокирует прилавок, если политикой
  разрешён минус-остаток: продажа проводится, минус виден событием и флагом;
* при строгой политике (флажок снят) выгрузка отбивается с внятными словами;
* СБП офлайн не существует, возвраты офлайн не проводятся (это решение фронта,
  сервер же не даёт провести офлайн-продажу по СБП);
* снятие очереди требует причины и оставляет запись в событиях и аудите.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.cashier import Cashier  # noqa: E402
from connector.printflow.db import Database  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "offline.sqlite3")


def add_item(db: Database, item_id: str = "s1", qty: float = 10,
             price: float = 500, name: str = "Адресник") -> None:
    db.upsert("shelf_items", {
        "id": item_id, "name": name, "qty": qty, "price": price,
        "cost_per_unit": 120, "active": 1})


def local_time(minutes_ahead: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes_ahead)).isoformat()


class OfflineSellTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc)
        self.db.set_settings({"cashier_code": "1234"})
        add_item(self.db, qty=2)
        self.token = self.cashier.login("1234")["token"]

    def tearDown(self):
        self.db.close()

    def sell(self, qty: float = 5, **kw):
        kw.setdefault("request_id", "of-1")
        kw.setdefault("offline_at", local_time())
        return self.cashier.sell([{"item_id": "s1", "qty": qty}], "cash",
                                 self.token, **kw)

    def row(self, sale_id: str = "latest") -> dict:
        if sale_id == "latest":
            return self.db.one("SELECT * FROM cashier_sales ORDER BY rowid DESC LIMIT 1") or {}
        return self.db.one("SELECT * FROM cashier_sales WHERE id=?", (sale_id,)) or {}

    # ------------------------------------------------------- деньги не теряются
    def test_shortage_is_replayed_with_negative_stock(self):
        """Была 2 шт, продали 5 офлайн: проводим, остаток −3, флаг на месте."""
        result = self.sell(qty=5)
        self.assertTrue(result.get("paid"))
        # Две строки расхождения — и про остаток, и про то, что везти было
        # нечего: владельцу важно, что именно произошло, а не «что-то не так».
        self.assertEqual([c["kind"] for c in result.get("negative_stock") or []],
                         ["short", "no-stock"])
        shelf = self.db.one("SELECT qty FROM shelf_items WHERE id='s1'") or {}
        self.assertAlmostEqual(float(shelf["qty"]), -3.0, places=3)
        row = self.row()
        self.assertTrue(str(row.get("offline_at") or ""))
        flags = json.loads(str(row.get("offline_flags") or "{}"))
        self.assertEqual(flags["policy"], "negative")
        self.assertEqual(flags["negative_stock"][0]["kind"], "short")
        self.assertAlmostEqual(flags["negative_stock"][0]["left"], 2.0, places=2)

    def test_negative_stock_is_announced_in_the_feed(self):
        self.sell(qty=5)
        events = [e for e in self.db.events(limit=50)
                  if "минус" in str(e.get("title") or "").lower()]
        self.assertEqual(len(events), 1)
        self.assertIn("Адресник", str(events[0].get("detail") or ""))

    def test_replay_of_the_same_entry_does_not_double(self):
        """Повторная отправка очереди (тот же request_id) — одна продажа."""
        first = self.sell(qty=1)
        again = self.sell(qty=1)
        self.assertTrue(again.get("already_recorded"))
        self.assertEqual(again.get("id"), first.get("id"))
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM cashier_sales")["c"], 1)
        shelf = self.db.one("SELECT qty FROM shelf_items WHERE id='s1'") or {}
        self.assertAlmostEqual(float(shelf["qty"]), 1.0, places=3)

    def test_clean_replay_has_no_flags(self):
        """Остатка хватило — офлайн-флагов нет, штамп есть."""
        self.sell(qty=1)
        row = self.row()
        self.assertTrue(str(row.get("offline_at") or ""))
        self.assertEqual(str(row.get("offline_flags") or ""), "")

    def test_ordinary_sale_is_not_marked(self):
        result = self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash", self.token,
                                   request_id="ui-1")
        self.assertFalse(result.get("offline_at"))
        row = self.row()
        self.assertEqual(str(row.get("offline_at") or ""), "")
        self.assertEqual(str(row.get("offline_flags") or ""), "")

    # --------------------------------------------------------- строгая политика
    def test_block_policy_refuses_the_sale(self):
        self.db.set_settings({"cashier_offline_negative": False})
        with self.assertRaises(ValueError) as ctx:
            self.sell(qty=5)
        self.assertIn("нельзя", str(ctx.exception))
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM cashier_sales")["c"], 0)
        shelf = self.db.one("SELECT qty FROM shelf_items WHERE id='s1'") or {}
        self.assertAlmostEqual(float(shelf["qty"]), 2.0, places=3)

    def test_money_is_not_written_when_blocked(self):
        self.db.set_settings({"cashier_offline_negative": False})
        try:
            self.sell(qty=5)
        except ValueError:
            pass
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM transactions")["c"], 0)

    # ------------------------------------------------------------- часы кассы
    def test_clock_ahead_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.sell(qty=1, offline_at=local_time(10))
        self.assertIn("спешит", str(ctx.exception))

    def test_too_old_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.sell(qty=1, offline_at=local_time(-60 * 24 * 5))
        self.assertIn("трёх суток", str(ctx.exception))

    def test_garbage_stamp_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.sell(qty=1, offline_at="вчера")
        self.assertIn("ISO", str(ctx.exception))

    def test_offline_sbp_needs_shop_qr(self):
        """Без QR магазина офлайн-СБП закрыт: обещать оплату, которую нельзя
        провести, хуже чем честно сказать «подождите сеть»."""
        with self.assertRaises(ValueError) as ctx:
            self.cashier.sell([{"item_id": "s1", "qty": 1}], "sbp", self.token,
                              request_id="of-sbp", offline_at=local_time())
        self.assertIn("QR магазина", str(ctx.exception))
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM cashier_sales")["c"], 0)

    # ------------------------------------------------------------- склад и тупики
    def test_unlinked_shelf_row_is_solved_without_losing_money(self):
        """Со склада везти нечего: проводим в минус по той же позиции, дубль не заводим."""
        self.db.execute("UPDATE shelf_items SET qty=0 WHERE id='s1'")
        result = self.sell(qty=2)
        self.assertTrue(result.get("paid"))
        kinds = [c["kind"] for c in result.get("negative_stock") or []]
        self.assertIn("no-stock", kinds)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM shelf_items")["c"], 1)
        shelf = self.db.one("SELECT qty FROM shelf_items WHERE id='s1'") or {}
        self.assertAlmostEqual(float(shelf["qty"]), -2.0, places=3)

    # --------------------------------------------------------------- видимость
    def test_shift_list_exposes_offline_info(self):
        self.sell(qty=5)
        sales = self.cashier.shift_sales(self.token)["sales"]
        self.assertEqual(len(sales), 1)
        sale = sales[0]
        self.assertTrue(sale["offline_at"])
        self.assertEqual(sale["offline"]["negative_stock"][0]["kind"], "short")

    def test_badge_survives_without_flags(self):
        self.sell(qty=1)
        sale = self.cashier.shift_sales(self.token)["sales"][0]
        self.assertTrue(sale["offline_at"])
        self.assertIsNone(sale["offline"])

    def test_journal_events_of_the_shift_are_not_duplicated(self):
        self.sell(qty=1)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM cashier_sales")["c"], 1)
        incomes = self.db.query("SELECT * FROM transactions WHERE kind='income'")
        self.assertEqual(len(incomes), 1)


class OfflineAbandonTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc)
        self.db.set_settings({"cashier_code": "1234"})
        add_item(self.db, qty=5)
        self.token = self.cashier.login("1234")["token"]

    def tearDown(self):
        self.db.close()

    def test_reason_is_required(self):
        with self.assertRaises(ValueError):
            self.cashier.abandon_offline(["of-1"], self.token, "")

    def test_ids_are_required(self):
        with self.assertRaises(ValueError):
            self.cashier.abandon_offline([], self.token, "дубль")

    def test_abandon_writes_event_and_audit(self):
        self.cashier.sell([{"item_id": "s1", "qty": 1}], "cash", self.token,
                          request_id="of-keep", offline_at=local_time())
        out = self.cashier.abandon_offline(["of-keep", "of-ghost"], self.token, "дубль")
        self.assertTrue(out["ok"])
        self.assertEqual(out["removed"], 2)
        self.assertEqual(len(out["found"]), 1)
        event = [e for e in self.db.events(limit=20)
                 if "офлайн-очередь" in str(e.get("title") or "").lower()]
        self.assertEqual(len(event), 1)
        self.assertIn("дубль", str(event[0].get("detail") or ""))
        audit = self.db.query("SELECT * FROM audit_log WHERE action='abandon'")
        self.assertEqual(len(audit), 1)
        self.assertIn("actor", str(audit[0].get("data") or ""))

    def test_abandon_does_not_touch_stock_or_money(self):
        self.cashier.sell([{"item_id": "s1", "qty": 2}], "cash", self.token,
                          request_id="of-2", offline_at=local_time())
        before = self.db.one("SELECT qty FROM shelf_items WHERE id='s1'")["qty"]
        self.cashier.abandon_offline(["of-2"], self.token, "передумали")
        self.assertAlmostEqual(float(self.db.one(
            "SELECT qty FROM shelf_items WHERE id='s1'")["qty"]), float(before), places=3)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM cashier_sales")["c"], 1)



class FrontendContractTests(unittest.TestCase):
    """Контракт «страница + оболочка»: то, без чего очередь молча перестанет работать.

    JS мы не исполняем, но договор держим: ключ localStorage, поле `offline_at`
    в запросе и мостик оболочки. Если кто-то переименует ключ — очередь
    «исчезнет» на глазах у кассира, и это должно упасть здесь, а не в смене.
    """

    site = pathlib.Path(__file__).resolve().parents[2] / "site" / "cashier.html"
    android = pathlib.Path(__file__).resolve().parents[2] / "android" / "app" / "src" / "main"

    def setUp(self):
        self.js = self.site.read_text(encoding="utf-8")

    def test_queue_key_and_fields(self):
        self.assertIn('var OFF_KEY="cashier_offline_q"', self.js)
        self.assertIn("request_id:e.id,offline_at:e.at", self.js)
        # метод очереди: наличная продажа и заявка СБП выгружаются по-разному
        self.assertIn('method:(e.m==="sbp"?"sbp":"cash")', self.js)
        self.assertIn('m:method||"cash"', self.js)

    def test_offline_sbp_requires_cached_qr(self):
        # Страница не предлагает СБП офлайн, если QR магазина не закеширован:
        # «оплата не прошла» из-за вчерашнего кэша = испорченная смена.
        self.assertIn("var OFF_QR_KEY=\"cashier_sbp_qr\"", self.js)
        self.assertIn("/api/cashier/offline/qr", self.js)
        self.assertIn("СБП офлайн невозможен", self.js)
        self.assertIn("showOfflineQr(sum)", self.js)

    def test_pending_count_reaches_the_shell(self):
        self.assertIn("app.offline", self.js)

    def test_shell_service_is_declared(self):
        manifest = (self.android / "AndroidManifest.xml").read_text(encoding="utf-8")
        self.assertIn('android:name=".RingService"', manifest)
        self.assertIn("FOREGROUND_SERVICE_SPECIAL_USE", manifest)
        self.assertIn('android:foregroundServiceType="specialUse"', manifest)
        service = (self.android / "java/ai/printflow/kassa/RingService.kt").read_text(encoding="utf-8")
        self.assertIn("/api/stream", service)
        self.assertIn("money_in", service)          # деньги в журнале — мягкий сигнал
        self.assertIn("client_claim", service)      # «я оплатил» — громкий
        self.assertIn("PARTIAL_WAKE_LOCK", service)

if __name__ == "__main__":
    unittest.main()


class OfflineSbpClaimTests(unittest.TestCase):
    """Офлайн-СБП (17.0.9): заявка об оплате, а не оплата.

    Ключевой инвариант: пока банк не показал поступление, дохода нет ни при
    каких обстоятельствах — ни в момент записи заявки, ни при «подтверди, потому
    что клиент показал чек перевода». Склад при этом не должен врать: товар либо
    в холде, либо списан с флагом, либо списан в минус при отклонении.
    """

    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.cashier = Cashier(self.db, self.acc)
        self.db.set_settings({"cashier_code": "1234",
                              "sbp_shop_qr": "https://qr.nspk.ru/ASTEST123"})
        add_item(self.db, qty=4, price=300, name="Брелок")
        self.token = self.cashier.login("1234")["token"]

    def tearDown(self):
        self.db.close()

    def claim(self, qty: float = 2, **kw):
        kw.setdefault("request_id", "of-sbp-1")
        kw.setdefault("offline_at", local_time())
        return self.cashier.sell([{"item_id": "s1", "qty": qty}], "sbp", self.token, **kw)

    def test_claim_is_not_income(self):
        result = self.claim(qty=2)
        self.assertFalse(result.get("paid"))
        self.assertTrue(result.get("claim"))
        self.assertIn("выписк", str(result.get("claim_note") or ""))
        # доход: 0. склад: не списан (только холд)
        self.assertEqual(self.db.query("SELECT * FROM transactions WHERE kind='income'"), [])
        self.assertAlmostEqual(float(self.db.one(
            "SELECT qty FROM shelf_items WHERE id='s1'")["qty"]), 4.0, places=3)
        flags = json.loads(str(self.db.one(
            "SELECT offline_flags FROM cashier_sales WHERE request_id='of-sbp-1'")["offline_flags"]))
        self.assertIn("sbp_claim", flags)

    def test_claim_needs_shop_qr(self):
        self.db.set_settings({"sbp_shop_qr": "", "pay_qr_mode": "static"})
        with self.assertRaises(ValueError) as ctx:
            self.claim(qty=1)
        self.assertIn("QR магазина", str(ctx.exception))
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM cashier_sales")["c"], 0)

    def test_replay_is_idempotent(self):
        first = self.claim(qty=1)
        again = self.cashier.sell([{"item_id": "s1", "qty": 1}], "sbp", self.token,
                                  request_id="of-sbp-1", offline_at=local_time())
        self.assertTrue(again.get("already_recorded"))
        self.assertEqual(again.get("sale_id"), first.get("sale_id"))
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM cashier_sales")["c"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM sbp_payments")["c"], 1)

    def test_confirm_writes_income_and_stock_once(self):
        """Подтверждение = деньги в журнале; для офлайна минус не блокировка."""
        result = self.claim(qty=2)
        payment_id = str(result.get("payment_id") or "") or str(
            self.db.one("SELECT payment_id FROM cashier_sales WHERE request_id='of-sbp-1'")["payment_id"])
        done = self.cashier.confirm_sbp(payment_id, self.token)
        self.assertTrue(done.get("confirmed") or done.get("paid"))
        self.assertEqual(len(self.db.query("SELECT * FROM transactions WHERE kind='income'")), 1)
        self.assertAlmostEqual(float(self.db.one(
            "SELECT qty FROM shelf_items WHERE id='s1'")["qty"]), 2.0, places=3)
        again = self.cashier.confirm_sbp(payment_id, self.token)
        self.assertTrue(again.get("already_recorded"))
        self.assertEqual(len(self.db.query("SELECT * FROM transactions WHERE kind='income'")), 1)

    def test_confirm_with_negative_stock_is_not_blocked(self):
        """Полка ушла в минус до подтверждения: проводим и флагуем, деньги не висячие."""
        self.db.set_settings({"cashier_offline_negative": True})
        result = self.claim(qty=2)
        self.db.execute("UPDATE shelf_items SET qty=0 WHERE id='s1'")
        payment_id = str(result.get("payment_id") or "") or str(
            self.db.one("SELECT payment_id FROM cashier_sales WHERE request_id='of-sbp-1'")["payment_id"])
        done = self.cashier.confirm_sbp(payment_id, self.token)
        self.assertFalse(done.get("already_recorded"))
        self.assertAlmostEqual(float(self.db.one(
            "SELECT qty FROM shelf_items WHERE id='s1'")["qty"]), -2.0, places=3)
        sale = self.db.one("SELECT offline_flags FROM cashier_sales WHERE request_id='of-sbp-1'")
        flags = json.loads(str(sale["offline_flags"]))
        self.assertIn("sbp_claim", flags)   # заявка не потерялась при пересчёте
        self.assertIn("negative_stock", flags)

    def test_reject_without_answer_about_goods_is_refused(self):
        result = self.claim(qty=2)
        payment_id = str(result.get("payment_id") or "")
        with self.assertRaises(ValueError) as ctx:
            self.cashier.reject_sbp(payment_id, self.token, reason="не дошли")
        self.assertIn("товар", str(ctx.exception))

    def test_reject_with_goods_written_off(self):
        result = self.claim(qty=2)
        payment_id = str(result.get("payment_id") or "")
        self.cashier.reject_sbp(payment_id, self.token, reason="не дошли",
                                goods_taken=True)
        self.assertAlmostEqual(float(self.db.one(
            "SELECT qty FROM shelf_items WHERE id='s1'")["qty"]), 2.0, places=3)
        self.assertEqual(self.db.query("SELECT * FROM transactions WHERE kind='income'"), [])
        events = [e for e in self.db.events(limit=20)
                  if "отклонена" in str(e.get("title") or "").lower()]
        self.assertEqual(len(events), 1)

    def test_reject_goods_returned_keeps_stock(self):
        result = self.claim(qty=2)
        self.cashier.reject_sbp(str(result.get("payment_id") or ""), self.token,
                                reason="передумали", goods_taken=False)
        self.assertAlmostEqual(float(self.db.one(
            "SELECT qty FROM shelf_items WHERE id='s1'")["qty"]), 4.0, places=3)

    def test_offline_qr_endpoint_shape(self):
        qr = self.cashier.offline_qr()
        self.assertEqual(qr.get("text"), "https://qr.nspk.ru/ASTEST123")
        self.assertEqual(qr.get("kind"), "static")
        self.assertTrue(qr.get("svg"))

    def test_offline_qr_empty_when_not_configured(self):
        self.db.set_settings({"sbp_shop_qr": "", "pay_qr_mode": "off"})
        self.assertEqual(self.cashier.offline_qr(), {})
        self.assertFalse(self.cashier._static_qr_ready())

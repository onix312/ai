"""Тесты Фазы 11 PrintFlow 8.5: контент, аналитика, виртуальный принтер, tour."""
from __future__ import annotations

import io
import pathlib
import sys
import tempfile
import time
import types
import unittest
import zipfile
from datetime import date
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402


def make_db() -> Database:
    # Держим ссылку, чтобы каталог не удалили до закрытия БД.
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "test.sqlite3")


_held: list = []


def make_api(db: Database):
    from connector.printflow.api import Api
    from connector.printflow.shelf import Shelf
    api = Api.__new__(Api)
    api.db = db
    api.shelf = Shelf(db)
    api.manager = types.SimpleNamespace(printers={}, bot=None)
    api.acc = mock.Mock()
    api.bus = types.SimpleNamespace(publish=lambda *a, **k: None)
    api.started_at = time.time()
    api.last_host = "test"
    return api


def seed_basic(db: Database) -> None:
    """Минимальные данные: клиент, заказ с ценой, задание, доход."""
    from connector.printflow.config import now_iso
    db.upsert("customers", {"id": "c1", "name": "Анна", "phone": "1",
                            "created_at": now_iso()})
    db.upsert("orders", {"id": "o1", "number": "1", "product": "Адресник",
                         "customer_id": "c1", "customer_name": "Анна",
                         "status": "done", "price": 500.0, "paid": 500.0,
                         "created_at": now_iso(), "updated_at": now_iso()})
    db.upsert("print_jobs", {"id": "j1", "printer_id": "p1", "order_id": "o1",
                             "name": "Адресник", "state": "done",
                             "grams": 30.0, "duration_min": 90.0,
                             "cost": 60.0, "energy_kwh": 0.2,
                             "finished_at": now_iso(),
                             "started_at": now_iso()})
    db.upsert("transactions", {"id": "t1", "kind": "income", "order_id": "o1",
                               "category": "Продажа", "amount": 500.0,
                               "at": now_iso(), "note": "тест"})


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        seed_basic(self.db)

    def tearDown(self):
        self.db.close()

    def test_week_post(self):
        from connector.printflow.content import week_post
        out = week_post(self.db, 7)
        self.assertIn("цих NOZZA" if "цих NOZZA" in out["text"] else "NOZZA",
                      out["text"])
        self.assertGreaterEqual(out["numbers"]["income"], 500)
        self.assertEqual(out["top"], "Адресник")

    def test_holiday_nearest_is_future(self):
        from connector.printflow.content import holiday_cards
        out = holiday_cards(date(2026, 8, 22))
        self.assertIsNotNone(out["nearest"])
        self.assertGreaterEqual(out["nearest"]["days_left"], 0)
        self.assertEqual(len(out["all"]), 7)

    def test_holiday_rollover_year(self):
        from connector.printflow.content import holiday_cards
        out = holiday_cards(date(2026, 12, 28))
        # ближайший — Новый год 2027-го
        self.assertEqual(out["nearest"]["name"], "Новый год")
        self.assertEqual(out["nearest"]["date"], "2027-01-01")

    def test_avito_card(self):
        from connector.printflow.content import avito_card
        from connector.printflow.config import now_iso
        db = self.db
        db.upsert("shelf_items", {"id": "s1", "name": "Адресник «Пёс»",
                                  "qty": 0.0, "price": 450.0,
                                  "created_at": now_iso()})
        card = avito_card(db, "s1")
        self.assertIn("3D", card["title"].upper() + "3D".upper())
        self.assertIn("закончилось", card["description"].lower())
        with self.assertRaises(ValueError):
            avito_card(db, "nope")

    def test_seasonality_and_report(self):
        from connector.printflow.content import seasonality, workshop_report
        season = seasonality(self.db)
        self.assertEqual(len(season["months"]), 12)
        report = workshop_report(self.db, 30)
        self.assertGreaterEqual(report["income"], 500)
        self.assertEqual(report["jobs_done"], 1)
        self.assertEqual(report["top"][0]["product"], "Адресник")

    def test_social_pack(self):
        from connector.printflow.content import social_pack
        pack = social_pack(self.db, 30)
        self.assertIn("NOZZA", pack["header"])
        self.assertIn("30 дней", pack["period"])


class AchievementTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        seed_basic(self.db)

    def tearDown(self):
        self.db.close()

    def test_first_badges(self):
        from connector.printflow.achievements import achievements
        badges = {b["id"]: b for b in achievements(self.db)}
        self.assertTrue(badges["first-print"]["achieved"])
        self.assertFalse(badges["print-100"]["achieved"])
        self.assertTrue(badges["income-100k"]["progress"] >= 500)
        self.assertTrue(badges["no-defect-week"]["achieved"])
        self.assertFalse(badges["year-of-work"]["achieved"])


class ShelfForecastTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        from connector.printflow.shelf import Shelf
        self.shelf = Shelf(self.db)

    def tearDown(self):
        self.db.close()

    def _item(self, item_id: str, qty: float, sales7: float, created: str = ""):
        from connector.printflow.config import now_iso
        self.shelf.save_item({"id": item_id, "name": item_id, "qty": qty,
                              "price": 100.0, "created_at": created or now_iso()})
        for _ in range(int(sales7)):
            self.shelf._db_execute_sale = None  # noqa: SLF001 — прямой учёт
            self.db.upsert("shelf_moves", {"id": f"m-{item_id}-{now_iso()}-{_}",
                                           "at": now_iso(), "item_id": item_id,
                                           "kind": "sale", "qty": -1.0,
                                           "price": 100.0})

    def test_forecast_gap_and_projected(self):
        from connector.printflow.config import now_iso
        # 4 продажи за 7 дней → скорость ~0.57/день; через 7 дней ≈ 0
        self._item("fast", 2.0, 4, created=(now_iso()[:8] + "01"))
        # без продаж — нулевая скорость
        self._item("slow", 5.0, 0)
        out = {r["id"]: r for r in self.shelf.forecast(7)}
        self.assertAlmostEqual(out["fast"]["rate_per_day"], 4 / 7, places=2)
        self.assertLessEqual(out["fast"]["projected"], 1.0)
        self.assertGreater(out["fast"]["gap"], 0.0)
        self.assertTrue(out["fast"]["empty"] or out["fast"]["projected"] <= 1)
        self.assertEqual(out["slow"]["rate_per_day"], 0.0)
        self.assertEqual(out["slow"]["projected"], 5.0)

    def test_live_tags(self):
        from connector.printflow.config import now_iso
        self._item("hit", 3.0, 6)
        self.shelf.save_item({"id": "new", "name": "new",
                                       "qty": 4.0, "price": 50.0,
                                       "created_at": now_iso()})
        self._item("last", 1.0, 0)
        tags = self.shelf.live_tags()
        self.assertIn("hit", {t["id"] for t in tags["hit"]})
        self.assertIn("new", {t["id"] for t in tags["new"]})
        self.assertIn("last", {t["id"] for t in tags["last"]})


class PlateMapTests(unittest.TestCase):
    def test_gcode_audit(self):
        from connector.printflow.plate_map import audit_gcode
        tmp = tempfile.TemporaryDirectory()
        try:
            path = pathlib.Path(tmp.name) / "sample.gcode"
            path.write_text(
                "; comment line\n"
                "G28\n"
                "G1 X0 Y0 Z0.2 F3000\n"
                "G1 X10 Y0 Z0.2 E0.5 F1200\n"
                "G1 X10 Y5 Z0.24 E1.0 F1200\n"
                "G1 X5 Y5 Z0.24 E-0.95 F1200\n"
                "G1 X5 Y5 Z0.26 E0.05 F1200\n", encoding="utf-8")
            out = audit_gcode(path)
            self.assertEqual(out["layers"], 3)
            self.assertAlmostEqual(out["height_mm"], 0.26, places=1)
            self.assertEqual(out["retracts"], 1)
            self.assertGreater(out["max_speed_mm_min"], 300)
            self.assertTrue(out["warnings"])
        finally:
            tmp.cleanup()

    def test_gcode_audit_non_gcode(self):
        from connector.printflow.plate_map import audit_gcode
        tmp = tempfile.TemporaryDirectory()
        try:
            path = pathlib.Path(tmp.name) / "x.txt"
            path.write_text("G1 Z1", encoding="utf-8")
            self.assertEqual(audit_gcode(path), {})
        finally:
            tmp.cleanup()

    def test_3mf_plate_map(self):
        from connector.printflow.plate_map import plate_map_3mf
        model = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<model unit="millimeter" xml:lang="en-US"'
            ' xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            '<resources>'
            '<object id="1" name="Part1" minx="0" miny="0" maxx="40" maxy="20">'
            '<mesh/></object>'
            '<object id="2" name="Part2" minx="0" miny="0" maxx="30" maxy="30">'
            '<mesh/></object>'
            '</resources>'
            '<object id="3"><nodes>'
            '<node id="1" objectid="1" position="10 10 0" scale="1 1 1"/>'
            '<node id="2" objectid="2" position="60 60 0" scale="1 1 1"/>'
            '</nodes></object></model>'
        )
        tmp = tempfile.TemporaryDirectory()
        try:
            path = pathlib.Path(tmp.name) / "sample.3mf"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("3D/3dmodel.model", model)
            out = plate_map_3mf(path)
            self.assertEqual(out["plate"]["w"], 256.0)
            self.assertEqual(len(out["objects"]), 2)
            self.assertEqual(out["objects"][0]["name"], "Part1")
            self.assertAlmostEqual(out["objects"][0]["x"], 10.0)
            self.assertAlmostEqual(out["objects"][0]["w"], 40.0)
            self.assertGreater(out["fill_pct"], 0)
        finally:
            tmp.cleanup()

    def test_3mf_bad_file(self):
        from connector.printflow.plate_map import plate_map_3mf
        tmp = tempfile.TemporaryDirectory()
        try:
            path = pathlib.Path(tmp.name) / "broken.3mf"
            path.write_bytes(b"not a zip")
            self.assertEqual(plate_map_3mf(path), {})
        finally:
            tmp.cleanup()


class VirtualPrinterTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.db.upsert("spools", {"id": "sp1", "material": "PLA",
                                  "color_name": "Красный", "color_hex": "#dc2626",
                                  "remaining_grams": 400.0,
                                  "printer_id": "virtual", "ams_slot": "2",
                                  "tray_uuid": "u1"})
        self.db.upsert("print_jobs", {"id": "job1", "printer_id": "virtual",
                                      "name": "тест", "state": "starting",
                                      "est_minutes": 60.0, "est_grams": 30.0})

    def tearDown(self):
        self.db.close()

    def _vp(self, events: list):
        from connector.printflow.virtual import VirtualPrinter
        self.db.set_settings({"demo_speed": 1000.0})
        vp = VirtualPrinter(self.db, {"id": "virtual", "name": "Вирт",
                                      "model": "P1S"},
                            on_event=lambda kind, title, name, data:
                            events.append((kind, title, name, data)))
        return vp

    def test_print_lifecycle(self):
        events: list = []
        vp = self._vp(events)
        self.assertEqual(vp._state, "IDLE")  # noqa: SLF001
        vp.start_print("test.3mf", subtask_name="Тест")
        self.assertEqual(vp._state, "PRINTING")  # noqa: SLF001
        self.assertEqual(events[-1][0], "start")
        # симулируем 70 минут: 1000 мин/с * 0.07 с > плана в 60 минут
        vp._started_ts = time.time() - 0.07  # noqa: SLF001
        vp._tick()
        self.assertEqual(vp._state, "FINISH")  # noqa: SLF001
        self.assertIn("complete", [e[0] for e in events])
        snap = vp.snapshot()
        self.assertEqual(snap["printer"]["progress"], 100.0)
        self.assertEqual(snap["connection"]["mode"], "virtual")
        vp._finish_at = time.time() - 10  # noqa: SLF001
        vp._tick()
        self.assertEqual(vp._state, "IDLE")  # noqa: SLF001
        vp.shutdown()

    def test_pause_accumulates_time(self):
        events: list = []
        vp = self._vp(events)
        self.db.set_settings({"demo_speed": 1.0})  # 1 мин/с: 0.2 с = 0.2 мин
        vp.start_print("test.3mf")
        time.sleep(0.2)
        vp.command("pause")
        self.assertEqual(vp._state, "PAUSE")  # noqa: SLF001
        self.assertGreater(vp._accumulated, 0.1)  # noqa: SLF001
        time.sleep(0.2)
        # во время паузы время не копится
        self.assertAlmostEqual(vp._accumulated, vp._elapsed_min(), places=1)
        vp.command("resume")
        self.assertEqual(vp._state, "PRINTING")  # noqa: SLF001
        snap = vp.snapshot()
        self.assertGreater(snap["printer"]["elapsed_min"], 0)
        vp.shutdown()

    def test_ams_trays_from_spools(self):
        events: list = []
        vp = self._vp(events)
        snap = vp.snapshot()
        trays = snap["ams"]["trays"]
        self.assertEqual(len(trays), 1)
        self.assertEqual(trays[0]["type"], "PLA")
        self.assertEqual(trays[0]["slot"], 2)
        vp.shutdown()


class SpaghettiExtTests(unittest.TestCase):
    def test_first_layer_decision(self):
        from connector.printflow.spaghetti import first_layer_decision
        self.assertTrue(first_layer_decision(20.0, 5.0))
        self.assertFalse(first_layer_decision(20.0, 18.0))
        self.assertFalse(first_layer_decision(0.0, 0.0))

    def test_frame_diff_ratio(self):
        from connector.printflow.spaghetti import frame_diff_ratio
        if not __import__("connector.printflow.spaghetti", fromlist=["HAS_PIL"]).HAS_PIL:
            self.skipTest("Pillow не установлен")
        from PIL import Image
        a = Image.new("L", (40, 30), 10)
        b = Image.new("L", (40, 30), 90)
        ba, bb = io.BytesIO(), io.BytesIO()
        a.save(ba, format="JPEG")
        b.save(bb, format="JPEG")
        ratio = frame_diff_ratio(ba.getvalue(), bb.getvalue())
        self.assertIsNotNone(ratio)
        self.assertGreater(ratio, 50)
        self.assertEqual(frame_diff_ratio(b"junk", bb.getvalue()), None)

    def test_inspect_bed_clear(self):
        from connector.printflow.spaghetti import inspect_bed_clear
        empty = inspect_bed_clear(None, None)
        self.assertTrue(empty["ok"])
        self.assertEqual(empty["code"], "bed_no_ref")
        no_cam = inspect_bed_clear(None, b"ref")
        self.assertTrue(no_cam["ok"])
        self.assertEqual(no_cam["code"], "bed_no_camera")
        with mock.patch("connector.printflow.spaghetti.frame_diff_ratio", return_value=40.0):
            dirty = inspect_bed_clear(b"frame", b"ref", threshold=6.0)
        self.assertFalse(dirty["ok"])
        self.assertEqual(dirty["code"], "bed_dirty")
        with mock.patch("connector.printflow.spaghetti.frame_diff_ratio", return_value=2.0):
            clean = inspect_bed_clear(b"frame", b"ref", threshold=6.0)
        self.assertTrue(clean["ok"])
        self.assertEqual(clean["code"], "bed_clear")


class PhotosSearchTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        if not __import__("connector.printflow.photos", fromlist=["HAS_PIL"]).HAS_PIL:
            raise unittest.SkipTest("Pillow не установлен")
        self.db.upsert("order_photos", {"id": "ph1", "order_id": "o1",
                                        "file": "a.jpg", "at": "2026-08-01"})
        self.db.upsert("order_photos", {"id": "ph2", "order_id": "o2",
                                        "file": "b.jpg", "at": "2026-08-02"})

    def tearDown(self):
        self.db.close()

    def test_similar_requires_files(self):
        from connector.printflow.photos import similar
        # файлов нет на диске — честно пустой список
        out = similar(self.db, "ph1")
        self.assertTrue(out["ok"])
        self.assertEqual(out["photos"], [])


class ApiPhase11Tests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        seed_basic(self.db)
        self.api = make_api(self.db)

    def tearDown(self):
        self.db.close()

    def test_content_routes(self):
        for path in ("/api/content/week", "/api/content/social",
                     "/api/content/holiday", "/api/content/season",
                     "/api/content/report"):
            code, _ = self.api.get(path, {})
            self.assertEqual(code, 200, path)

    def test_queue_add_route_maps_order_fast_add(self):
        """«В очередь» с карточки заказа не должно уходить в несуществующий URL.

        Раньше фронтенд вызывал /api/queue/add, которого не было на сервере, и
        кнопка всегда заканчивалась ошибкой. Маршрут теперь стандартизирует
        поля, которые очередь читает (est_grams/est_minutes).
        """
        calls: dict = {}
        self.api.manager = types.SimpleNamespace(enqueue=lambda p: (calls.update(p), {"id": "jq"})[-1])
        code, payload = self.api.post("/api/queue/add", {
            "order_id": "o1", "title": "Адресник (№1)", "file": "/model/a.3mf",
            "grams": 30, "hours": 1.5,
        }, {})
        self.assertEqual(code, 200)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(calls["name"], "Адресник (№1)")
        self.assertEqual(calls["est_grams"], 30.0)
        self.assertEqual(calls["est_minutes"], 90.0)
        self.assertNotIn("grams", calls)
        self.assertNotIn("hours", calls)
        self.assertNotIn("title", calls)

    def test_shelf_and_achieve_routes(self):
        code, payload = self.api.get("/api/shelf/forecast", {"days": ["7"]})
        self.assertEqual(code, 200)
        self.assertEqual(payload["days"], 7)
        code, payload = self.api.get("/api/shelf/tags", {})
        self.assertEqual(code, 200)
        self.assertIn("hit", payload)
        code, payload = self.api.get("/api/achievements", {})
        self.assertEqual(code, 200)
        self.assertTrue(payload["badges"])

    def test_heartbeat(self):
        code, payload = self.api.get("/api/system/heartbeat", {})
        self.assertEqual(code, 200)
        self.assertIn("telegram", payload)
        self.assertIn("printers", payload)
        self.assertEqual(payload["telegram"]["enabled"], False)

    def test_ams_suggestion_empty(self):
        code, payload = self.api.get("/api/ams/suggestion", {})
        self.assertEqual(code, 200)
        self.assertEqual(payload["suggestion"], [])

    def test_wish_lifecycle(self):
        code, payload = self.api.post(
            "/api/wish/save", {"customer_id": "c1", "text": "Красный адресник"}, {})
        self.assertEqual(code, 200)
        wish_id = payload["wish"]["id"]
        code, _ = self.api.post("/api/wish/resolve",
                                {"id": wish_id, "status": "done"}, {})
        self.assertEqual(code, 200)
        row = self.db.one("SELECT * FROM wishes WHERE id=?", (wish_id,))
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["resolved_at"])

    def test_wish_requires_text(self):
        code, _ = self.api.post("/api/wish/save",
                                {"customer_id": "c1", "text": ""}, {})
        self.assertEqual(code, 400)

    def test_portal_code_and_my_nozza(self):
        code, payload = self.api.post("/api/portal/code",
                                      {"customer_id": "c1"}, {})
        self.assertEqual(code, 200)
        code_ = payload["code"]
        self.assertEqual(len(code_), 8)
        code, payload = self.api.get("/api/public/my", {"code": [code_.upper()]})
        self.assertEqual(code, 200)
        self.assertEqual(payload["name"], "Анна")
        self.assertEqual(payload["orders_count"], 1)
        code, payload = self.api.get("/api/public/my", {"code": ["00000000"]})
        self.assertEqual(code, 200)
        self.assertIn("error", payload)

    def test_pack_data(self):
        code, payload = self.api.get("/api/order/pack-data", {"id": ["o1"]})
        self.assertEqual(code, 200)
        self.assertEqual(payload["order"]["product"], "Адресник")
        # отсутствующий заказ — ValueError → 500 в общем обработчике
        with self.assertRaises(ValueError):
            self.api._pack_data("nope")  # noqa: SLF001


class FulfillmentGiftTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

    def tearDown(self):
        self.db.close()

    def test_gift_hides_price(self):
        from connector.printflow.fulfillment import OrderFulfillment
        msg = OrderFulfillment._message({"customer_name": "Анна", "number": "7",
                                         "gift": 1}, 500.0)
        self.assertNotIn("Осталось к оплате", msg)
        self.assertIn("пожеланиями", msg)
        msg2 = OrderFulfillment._message({"customer_name": "", "number": "7",
                                          "gift": 0}, 500.0)
        self.assertIn("Осталось к оплате: 500", msg2)


class PassportEcoTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.db.upsert("print_jobs", {"id": "j1", "printer_id": "p1",
                                      "name": "x", "state": "done",
                                      "grams": 40.0, "energy_kwh": 0.3,
                                      "started_at": "2026-08-01T00:00:00",
                                      "finished_at": "2026-08-01T05:00:00"})

    def tearDown(self):
        self.db.close()

    def test_eco_block(self):
        from connector.printflow.passport import job_passport
        out = job_passport(self.db, "j1")
        self.assertAlmostEqual(out["eco"]["grams"], 40.0)
        self.assertAlmostEqual(out["eco"]["energy_kwh"], 0.3)
        self.assertAlmostEqual(out["eco"]["co2_kg"], 0.04, places=2)


class BarcodeTests(unittest.TestCase):
    def test_code128_check_vector(self):
        from connector.printflow.barcode import encode
        # «A» в Code 128 B: start 104, 'A'=33, check (104+33*1)%103=34, stop 106
        self.assertEqual(encode("A"), [104, 33, 34, 106])

    def test_code128_modes(self):
        from connector.printflow.barcode import modules, validate
        info_c = validate("20260822")
        self.assertEqual(info_c["mode"], "C")
        self.assertTrue(info_c["check_ok"])
        info_b = validate("199.90")
        self.assertEqual(info_b["mode"], "B")
        self.assertTrue(info_b["check_ok"])
        # все паттерны дают нечётное число модулей (штрих начинается и заканчивается штрихом)
        self.assertGreater(len(modules("NOZZA")), 40)

    def test_code128_rejects_empty_and_cyrillic(self):
        from connector.printflow.barcode import encode, svg
        with self.assertRaises(ValueError):
            encode("")
        with self.assertRaises(ValueError):
            encode("НОZZА")
        self.assertIn("<svg", svg("199.90"))


class ContentGenTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        seed_basic(self.db)

    def tearDown(self):
        self.db.close()

    def test_shelf_header_and_promo(self):
        from connector.printflow.content import promo_pack, shelf_header
        from connector.printflow.config import now_iso
        self.db.upsert("shelf_items", {"id": "s1", "name": "Адресник", "qty": 3.0,
                                       "price": 400.0, "created_at": now_iso()})
        self.db.upsert("shelf_moves", {"id": "m1", "item_id": "s1", "kind": "sale",
                                       "qty": -2.0, "price": 400.0, "at": now_iso()})
        head = shelf_header(self.db, 7)
        self.assertIn("Адресник", head["text"])
        self.assertEqual(head["sold_total"], 2)
        promo = promo_pack(self.db)
        self.assertTrue(promo["nearest"])
        self.assertGreaterEqual(len(promo["cards"]), 1)

    def test_print_map_grid(self):
        from connector.printflow.content import print_map
        out = print_map(self.db)
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["max_day"], 1)
        self.assertEqual(len(out["cells"]), 1)

    def test_order_thread(self):
        from connector.printflow.content import order_thread
        out = order_thread(self.db, "o1")
        self.assertEqual(out["order"]["product"], "Адресник")
        self.assertEqual(out["print"][0]["state"], "done")
        self.assertEqual(out["income"][0]["amount"], 500)
        self.assertIsNone(out["feedback"])
        with self.assertRaises(ValueError):
            order_thread(self.db, "nope")

    def test_week_video_without_frames(self):
        from connector.printflow.content import week_video
        out = week_video(self.db, 7)
        self.assertEqual(out["jobs"], 1)
        self.assertEqual(out["jobs_with_frames"], 0)
        self.assertEqual(out["frames"], [])

    def test_stickers_and_business_card(self):
        from connector.printflow.content import business_card_html, stickers
        html = stickers("pla")
        self.assertIn("100% PLA", html)
        self.assertIn("@page", html)
        card = business_card_html(self.db, "c1")
        self.assertIn("NOZZA", card)
        self.assertIn("Мой NOZZA", card)


class TourApiTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.api = make_api(self.db)

    def tearDown(self):
        self.db.close()

    def test_tour_state_inactive(self):
        code, payload = self.api.get("/api/tour/state", {})
        self.assertEqual(code, 200)
        self.assertFalse(payload["active"])


class WishListApiTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        seed_basic(self.db)
        self.api = make_api(self.db)

    def tearDown(self):
        self.db.close()

    def test_wish_list_route(self):
        code, _ = self.api.post("/api/wish/save",
                                {"customer_id": "c1", "text": "Тестовое"}, {})
        self.assertEqual(code, 200)
        code, payload = self.api.get("/api/wish/list", {"customer_id": ["c1"]})
        self.assertEqual(code, 200)
        self.assertEqual(len(payload["wishes"]), 1)


class BarcodeApiTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.api = make_api(self.db)

    def tearDown(self):
        self.db.close()

    def test_code128_route(self):
        code, payload = self.api.get("/api/labels/code128", {"text": ["199.90"]})
        self.assertEqual(code, 200)
        self.assertIn("<svg", payload["svg"])
        self.assertTrue(payload["check_ok"])
        code, _ = self.api.get("/api/labels/code128", {"text": [""]})
        self.assertEqual(code, 400)


class TourFullTests(unittest.TestCase):
    """Tour-маршрут с мок-менеджером: старт/стоп и откат."""

    def setUp(self):
        self.db = make_db()
        self.api = make_api(self.db)
        self.api.manager = types.SimpleNamespace(
            printers={}, bot=None,
            reload=lambda: None,
            start_job=lambda job_id, printer_id: {"ok": True})
        self.api.restart_process = mock.Mock()

    def tearDown(self):
        self.db.close()

    def test_tour_start_stop(self):
        code, payload = self.api.post("/api/tour/start", {}, {})
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["job_started"])
        code, payload = self.api.get("/api/tour/state", {})
        self.assertTrue(payload["active"])
        code, payload = self.api.post("/api/tour/stop", {}, {})
        self.assertEqual(code, 200)
        self.assertTrue(payload["restarting"])
        # перезапуск идёт через threading.Timer(1.5, …) — не ждём его в тесте


class TourTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

    def tearDown(self):
        self.db.close()

    def test_tour_start_and_backup(self):
        from connector.printflow import tour
        result = tour.start(self.db)
        self.assertTrue(result["ok"])
        from connector.printflow.config import BACKUP_DIR
        self.assertTrue((BACKUP_DIR / result["backup"]).is_file())
        file = tour.stop_backup_file(self.db)
        self.assertEqual(file, result["backup"])
        # демо-данные появились
        orders = self.db.query("SELECT * FROM orders WHERE notes LIKE '%NOZZA tour%'")
        self.assertGreaterEqual(len(orders), 5)
        job = self.db.one("SELECT * FROM print_jobs WHERE state='queued'"
                          " AND printer_id='virtual'")
        self.assertIsNotNone(job)
        self.assertEqual(int(self.db.setting("demo_printer_enabled", 0)), 1)
        tour.reset_settings(self.db)
        # сброс возвращает демо-принтер в выключенное состояние
        self.assertEqual(int(self.db.setting("demo_printer_enabled", 1)), 0)

    def test_tour_stop_without_start(self):
        from connector.printflow import tour
        with self.assertRaises(ValueError):
            tour.stop_backup_file(self.db)


if __name__ == "__main__":
    unittest.main()

"""Редактор вариации как полноценного товара (18.6).

Владелец недоволен строкой вариации: у каждой вариации должна быть такая же
полная настройка, как у товара — фото, поля карточки, AMS-система. Правила:

* новые поля (бренд, описание, галерея) догоняют старые базы миграцией;
* галерея: первый кадр — обложка, старые раздачи (касса, бот) видят её же;
* удалённая обложка не оставляет дыру — обложкой становится следующий кадр;
* карточка редактора несёт строку, галерею, экономику и живой слот AMS;
* привязка к AMS — только через катушку/состав, прямой привязки нет;
* без состава поведение прежнее: экономика считается по одной катушке.
"""
from __future__ import annotations

import base64
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow.nomenclature import (  # noqa: E402
    VARIANT_GALLERY_MAX,
    Nomenclature,
)

_held: list = []


def make_db(**settings) -> Database:
    _held.append(tempfile.TemporaryDirectory())
    db = Database(pathlib.Path(_held[-1].name) / "variant_editor.sqlite3")
    if settings:
        db.set_settings(settings)
    return db


def _img(tag: str) -> str:
    raw = b"\xff\xd8fake-" + tag.encode()
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode()


class VariantEditorSchemaTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)

    def test_new_columns_exist_on_fresh_db(self):
        cols = {r["name"] for r in self.db.query("PRAGMA table_info(nom_variants)")}
        for name in ("brand", "description", "photos_json"):
            self.assertIn(name, cols, f"нет колонки nom_variants.{name}")

    def test_old_db_is_caught_up_with_defaults(self):
        import sqlite3
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = pathlib.Path(folder.name) / "old.sqlite3"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE nom_variants (id TEXT PRIMARY KEY, nom_id TEXT, name TEXT)")
        con.execute("INSERT INTO nom_variants VALUES ('v-old', 'n1', 'Красный')")
        con.commit()
        con.close()
        db = Database(path)
        self.addCleanup(db.close)
        cols = {r["name"] for r in db.query("PRAGMA table_info(nom_variants)")}
        for name in ("brand", "description", "photos_json", "photo", "spool_id"):
            self.assertIn(name, cols, f"миграция не догнала {name}")
        row = db.one("SELECT * FROM nom_variants WHERE id='v-old'")
        self.assertEqual("", row["brand"])
        self.assertEqual("", row["description"])
        self.assertEqual("", row["photos_json"])


class VariantGalleryTests(unittest.TestCase):
    def setUp(self):
        import connector.printflow.config as pf_config
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.nom = Nomenclature(self.db)
        item = self.nom.save({"name": "Брелок", "kind": "product", "unit": "шт",
                              "material": "PLA", "grams": 8, "hours": 0.5,
                              "fit_per_plate": 6})
        self.nom.save_variant({"id": "vg1", "nom_id": item["id"], "name": "Красный"})
        self.photos = pathlib.Path(tempfile.mkdtemp())
        old = pf_config.PHOTO_DIR
        pf_config.PHOTO_DIR = self.photos
        self.addCleanup(setattr, pf_config, "PHOTO_DIR", old)

    def test_first_frame_becomes_cover(self):
        resp = self.nom.add_variant_photo("vg1", _img("one"))
        self.assertEqual(["var_vg1.jpg"], resp["gallery"])
        row = self.db.one("SELECT photo FROM nom_variants WHERE id='vg1'")
        self.assertEqual("var_vg1.jpg", row["photo"])
        self.assertTrue((self.photos / "var_vg1.jpg").is_file())

    def test_next_frames_do_not_move_cover(self):
        self.nom.add_variant_photo("vg1", _img("one"))
        resp = self.nom.add_variant_photo("vg1", _img("two"))
        self.assertEqual("var_vg1.jpg", resp["gallery"][0])
        self.assertEqual(2, len(resp["gallery"]))
        row = self.db.one("SELECT photo FROM nom_variants WHERE id='vg1'")
        self.assertEqual("var_vg1.jpg", row["photo"])

    def test_cover_replace_keeps_gallery(self):
        self.nom.add_variant_photo("vg1", _img("one"))
        self.nom.add_variant_photo("vg1", _img("two"))
        self.nom.set_variant_photo("vg1", _img("new-cover"))
        gallery = self.nom.variant_gallery("vg1")
        self.assertEqual(2, len(gallery))
        self.assertEqual("var_vg1.jpg", gallery[0])
        self.assertEqual(b"\xff\xd8fake-new-cover",
                         (self.photos / "var_vg1.jpg").read_bytes())

    def test_gallery_is_capped(self):
        for i in range(VARIANT_GALLERY_MAX):
            self.nom.add_variant_photo("vg1", _img(f"f{i}"))
        with self.assertRaises(ValueError):
            self.nom.add_variant_photo("vg1", _img("extra"))

    def test_deleted_cover_promotes_next_frame(self):
        self.nom.add_variant_photo("vg1", _img("one"))
        second = self.nom.add_variant_photo("vg1", _img("two"))["photo"]
        resp = self.nom.delete_variant_photo("vg1", "var_vg1.jpg")
        self.assertEqual(second, resp["photo"])
        self.assertEqual([second], resp["gallery"])
        self.assertFalse((self.photos / "var_vg1.jpg").exists())

    def test_make_cover_moves_old_one_to_gallery(self):
        self.nom.add_variant_photo("vg1", _img("one"))
        second = self.nom.add_variant_photo("vg1", _img("two"))["photo"]
        resp = self.nom.set_variant_cover("vg1", second)
        self.assertEqual(second, resp["photo"])
        self.assertEqual([second, "var_vg1.jpg"], resp["gallery"])

    def test_unknown_frame_is_refused(self):
        self.nom.add_variant_photo("vg1", _img("one"))
        with self.assertRaises(ValueError):
            self.nom.delete_variant_photo("vg1", "var_vg1_9.jpg")
        with self.assertRaises(ValueError):
            self.nom.set_variant_cover("vg1", "var_vg1_9.jpg")

    def test_unknown_variant_raises_lookup(self):
        with self.assertRaises(LookupError):
            self.nom.add_variant_photo("nope", _img("x"))
        with self.assertRaises(LookupError):
            self.nom.variant_gallery("nope")

    def test_bad_data_rejected_like_cover(self):
        with self.assertRaises(ValueError):
            self.nom.add_variant_photo("vg1", "not-a-data-url")

    def test_legacy_cover_is_gallery_of_one(self):
        self.nom.set_variant_photo("vg1", _img("old"))
        self.assertEqual(["var_vg1.jpg"], self.nom.variant_gallery("vg1"))


class VariantCardTests(unittest.TestCase):
    """Карточка редактора: строка, галерея, экономика, слот AMS."""

    def setUp(self):
        import connector.printflow.config as pf_config
        self.db = make_db(min_order_price=0, price_rounding=1)
        self.addCleanup(self.db.close)
        self.nom = Nomenclature(self.db)
        item = self.nom.save({"name": "Адресник", "kind": "product", "unit": "шт",
                              "material": "PLA", "grams": 12, "hours": 0.75,
                              "fit_per_plate": 4})
        self.item_id = item["id"]
        self.db.upsert("printers", {"id": "p1", "name": "P1S", "enabled": 1})
        self.db.upsert("spools", {
            "id": "sp-ams", "material": "PLA", "brand": "Sunlu",
            "color_name": "Красный", "color_hex": "#e11d48",
            "price": 1600, "total_grams": 1000, "remaining_grams": 640,
            "printer_id": "p1", "ams_slot": "1"})
        self.nom.save_variant({"id": "vc1", "nom_id": self.item_id,
                               "name": "Красный", "brand": "Sunlu",
                               "description": "матовый",
                               "spool_id": "sp-ams"})
        photos = pathlib.Path(tempfile.mkdtemp())
        old = pf_config.PHOTO_DIR
        pf_config.PHOTO_DIR = photos
        self.addCleanup(setattr, pf_config, "PHOTO_DIR", old)

    def test_full_card_fields_round_trip(self):
        row = self.nom.save_variant({
            "id": "vc1", "nom_id": self.item_id, "name": "Красный",
            "color_name": "Красный", "color_hex": "#e11d48",
            "material": "PLA", "brand": "Bambu", "size": "L",
            "grams": 14, "hours": 0.8, "sku": "ADR-R-L",
            "barcode": "2001", "description": "матовый", "price": 349})
        for key, want in (("brand", "Bambu"), ("description", "матовый"),
                          ("color_hex", "#e11d48"), ("size", "L"),
                          ("sku", "ADR-R-L"), ("barcode", "2001")):
            self.assertEqual(want, row[key])
        self.assertTrue(row["updated_at"], "сохранение не штампует updated_at")

    def test_card_carries_gallery_economics_and_slot(self):
        self.nom.add_variant_photo("vc1", _img("one"))
        card = self.nom.variant_card("vc1")
        self.assertEqual("vc1", card["variant"]["id"])
        self.assertEqual(["var_vc1.jpg"], card["gallery"])
        self.assertGreater(card["economics"]["cost"], 0)
        slot = card["ams"]["spool"]
        self.assertTrue(slot["in_ams"])
        self.assertEqual("P1S", slot["printer_name"])
        self.assertEqual("1", slot["ams_slot"])
        self.assertEqual(640, slot["remaining_grams"])

    def test_spool_outside_ams_is_reported_honestly(self):
        self.db.upsert("spools", {"id": "sp-ams", "printer_id": None, "ams_slot": ""})
        slot = self.nom.variant_card("vc1")["ams"]["spool"]
        self.assertFalse(slot["in_ams"])
        self.assertEqual("", slot["ams_slot"])

    def test_missing_spool_is_flagged_not_crashed(self):
        self.db.execute("DELETE FROM spools WHERE id='sp-ams'")
        slot = self.nom.variant_card("vc1")["ams"]["spool"]
        self.assertTrue(slot["missing"])

    def test_unknown_variant_card_404(self):
        with self.assertRaises(LookupError):
            self.nom.variant_card("nope")

    def test_no_direct_slot_binding_on_variant(self):
        cols = {r["name"] for r in self.db.query("PRAGMA table_info(nom_variants)")}
        self.assertNotIn("ams_slot", cols)
        self.assertNotIn("printer_id", cols)


class VariantEditorRoutesTests(unittest.TestCase):
    def setUp(self):
        import connector.printflow.config as pf_config
        self.db = make_db(min_order_price=0, price_rounding=1)
        self.nom = Nomenclature(self.db)
        item = self.nom.save({"name": "Брелок", "kind": "product", "unit": "шт",
                              "material": "PLA", "grams": 8, "hours": 0.5,
                              "fit_per_plate": 6})
        self.nom.save_variant({"id": "vr1", "nom_id": item["id"], "name": "Синий"})
        from connector.printflow.api import Api
        self.api = Api.__new__(Api)
        self.api.db = self.db
        self.api.nom = self.nom
        self.api.catalog_changed = lambda *a, **k: None
        photos = pathlib.Path(tempfile.mkdtemp())
        old = pf_config.PHOTO_DIR
        pf_config.PHOTO_DIR = photos
        self.addCleanup(setattr, pf_config, "PHOTO_DIR", old)

    def test_routes_are_registered(self):
        from connector.printflow.api import router
        paths = {r["path"] for r in router.reference()}
        for path in ("/api/nomenclature/variant/card",
                     "/api/nomenclature/variant/photos",
                     "/api/nomenclature/variant/photos/delete",
                     "/api/nomenclature/variant/cover"):
            self.assertIn(path, paths, f"нет маршрута {path}")

    def test_card_route_reads_variant(self):
        code, body = self.api.get("/api/nomenclature/variant/card", {"id": ["vr1"]})
        self.assertEqual(200, code)
        self.assertEqual("vr1", body["variant"]["id"])
        self.assertIn("gallery", body)
        self.assertIn("economics", body)
        self.assertIn("ams", body)

    def test_card_route_404(self):
        code, body = self.api.get("/api/nomenclature/variant/card", {"id": ["nope"]})
        self.assertEqual(404, code)

    def test_photos_route_appends_frame(self):
        code, body = self.api.post("/api/nomenclature/variant/photos",
                                   {"id": "vr1", "data": _img("one")}, {})
        self.assertEqual(200, code)
        self.assertEqual(1, len(body["gallery"]))
        code, body = self.api.post("/api/nomenclature/variant/photos",
                                   {"id": "vr1", "data": _img("two")}, {})
        self.assertEqual(200, code)
        self.assertEqual(2, len(body["gallery"]))

    def test_cover_and_delete_routes(self):
        self.api.post("/api/nomenclature/variant/photos",
                      {"id": "vr1", "data": _img("one")}, {})
        second = self.api.post(
            "/api/nomenclature/variant/photos",
            {"id": "vr1", "data": _img("two")}, {})[1]["photo"]
        code, body = self.api.post("/api/nomenclature/variant/cover",
                                   {"id": "vr1", "name": second}, {})
        self.assertEqual(200, code)
        self.assertEqual(second, body["photo"])
        code, body = self.api.post("/api/nomenclature/variant/photos/delete",
                                   {"id": "vr1", "name": second}, {})
        self.assertEqual(200, code)
        self.assertEqual(1, len(body["gallery"]))

    def test_photo_routes_refuse_unknown_variant(self):
        code, _ = self.api.post("/api/nomenclature/variant/photos",
                                {"id": "nope", "data": _img("x")}, {})
        self.assertEqual(404, code)

    def test_editor_markup_exists(self):
        html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
        for token in ("id=\"variant_modal\"", "id=\"vf_gallery\"", "id=\"vf_save\"",
                      "id=\"vf_ams\"", "id=\"vf_economy\"", "id=\"vf_description\"",
                      "id=\"vf_brand\""):
            self.assertIn(token, html, f"в index.html нет {token}")
        js = (ROOT / "site" / "assets" / "products.js").read_text(encoding="utf-8")
        for token in ("openVariantEditor", "saveVariantEditor", "data-var-edit",
                      "/api/nomenclature/variant/card",
                      "/api/nomenclature/variant/photos"):
            self.assertIn(token, js, f"в products.js нет {token}")


if __name__ == "__main__":
    unittest.main()

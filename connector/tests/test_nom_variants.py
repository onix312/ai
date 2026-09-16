"""Вариации товара и цена из себестоимости по цене катушки (18.0).

Один товар легко живёт в сотнях сочетаний: адресник — 12 цветов × 5 размеров ×
3 пластика = 180 вариаций, которые невозможно ввести руками. Правила, которые
здесь защищаются:

* оси перемножаются, а не складываются: три цвета и два размера дают шесть
  вариаций, а не пять;
* повторный запуск не плодит дубли — сочетание, которое уже есть, пропускается;
* цена грамма берётся из катушки, привязанной к вариации: PLA за 1600 и PETG
  за 3200 за килограмм не могут давать одну себестоимость;
* катушку не привязали — берём подходящую по пластику и цвету и честно
  говорим, откуда цифра;
* своя цена владельца пересчётом не затирается: машина считает, человек решает.
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
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.nomenclature import Nomenclature  # noqa: E402

_held: list = []


def make_db(**settings) -> Database:
    _held.append(tempfile.TemporaryDirectory())
    db = Database(pathlib.Path(_held[-1].name) / "variants.sqlite3")
    if settings:
        db.set_settings(settings)
    return db


def add_nom(nom: Nomenclature, **fields) -> dict:
    data = {"name": "Адресник «Кость»", "kind": "product", "unit": "шт",
            "material": "PLA", "grams": 12, "hours": 0.75, "fit_per_plate": 4,
            "post_minutes": 2, "sku": "ADR-1"}
    data.update(fields)
    return nom.save(data)


def add_spool(db: Database, material: str, price: float, color: str = "",
              total: float = 1000) -> str:
    import uuid
    spool_id = "sp-" + uuid.uuid4().hex[:10]
    db.upsert("spools", {"id": spool_id, "material": material, "price": price,
                         "total_grams": total, "remaining_grams": total,
                         "color_name": color, "brand": "Test"})
    return spool_id


class VariantGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.nom = Nomenclature(self.db)
        self.item = add_nom(self.nom)

    def test_axes_multiply(self):
        result = self.nom.generate_variants(self.item["id"], [
            {"name": "Цвет", "values": ["Чёрный", "Белый", "Красный"]},
            {"name": "Размер", "values": ["S", "L"]}])
        self.assertEqual(6, result["created"])
        names = {r["name"] for r in self.db.query(
            "SELECT name FROM nom_variants WHERE nom_id=?", (self.item["id"],))}
        self.assertIn("Чёрный / S", names)
        self.assertIn("Красный / L", names)

    def test_color_size_and_material_axes_fill_their_fields(self):
        self.nom.generate_variants(self.item["id"], [
            {"name": "Цвет", "values": [{"name": "Чёрный", "hex": "#111111"}]},
            {"name": "Размер", "values": ["L"]},
            {"name": "Пластик", "values": ["PETG"]}])
        row = self.db.one("SELECT * FROM nom_variants WHERE nom_id=?",
                          (self.item["id"],))
        self.assertEqual("Чёрный", row["color_name"])
        self.assertEqual("#111111", row["color_hex"])
        self.assertEqual("L", row["size"])
        self.assertEqual("PETG", row["material"])

    def test_repeat_call_does_not_duplicate(self):
        axes = [{"name": "Цвет", "values": ["Чёрный", "Белый"]}]
        self.nom.generate_variants(self.item["id"], axes)
        second = self.nom.generate_variants(self.item["id"], axes)
        self.assertEqual(0, second["created"])
        self.assertEqual(2, second["skipped"])
        self.assertEqual(2, second["total"])

    def test_preview_writes_nothing(self):
        axes = [{"name": "Цвет", "values": ["Чёрный", "Белый", "Синий"]}]
        preview = self.nom.generate_variants(self.item["id"], axes, preview=True)
        self.assertEqual(3, preview["would_create"])
        self.assertEqual(0, preview["created"])
        self.assertEqual([], self.db.query(
            "SELECT id FROM nom_variants WHERE nom_id=?", (self.item["id"],)))

    def test_sku_is_human_readable(self):
        self.nom.generate_variants(self.item["id"],
                                   [{"name": "Цвет", "values": ["Чёрный матовый"]}])
        row = self.db.one("SELECT sku FROM nom_variants WHERE nom_id=?",
                          (self.item["id"],))
        self.assertEqual("ADR-1-chernyy-matovyy", row["sku"])

    def test_too_many_combinations_is_refused_with_words(self):
        axes = [{"name": "Цвет", "values": [f"цвет{i}" for i in range(60)]},
                {"name": "Размер", "values": [f"размер{i}" for i in range(60)]}]
        with self.assertRaises(ValueError) as ctx:
            self.nom.generate_variants(self.item["id"], axes)
        text = str(ctx.exception)
        self.assertIn("3600", text, "отказ обязан называть число, которое вышло")
        self.assertIn("2000", text, "и предел, который не даёт его превысить")

    def test_empty_axes_are_refused(self):
        with self.assertRaises(ValueError):
            self.nom.generate_variants(self.item["id"], [])

    def test_unknown_product_is_refused(self):
        with self.assertRaises(ValueError):
            self.nom.generate_variants("нет-такого", [{"name": "Цвет",
                                                       "values": ["Чёрный"]}])


class VariantCostTests(unittest.TestCase):
    """Себестоимость считается по цене катушки варианта, а не «в среднем»."""

    def setUp(self):
        # Округление и «минимальный заказ» убраны: в этой группе проверяется
        # связь «цена катушки → себестоимость → цена», а не правила округления.
        self.db = make_db(min_order_price=0, price_rounding=1)
        self.acc = Accounting(self.db)
        self.nom = Nomenclature(self.db)
        self.item = add_nom(self.nom)
        self.cheap = add_spool(self.db, "PLA", 1200, color="Чёрный")
        self.pricey = add_spool(self.db, "PETG", 3600, color="Белый")

    def _variant(self, **fields) -> str:
        data = {"nom_id": self.item["id"], "name": "Чёрный / S",
                "color_name": "Чёрный"}
        data.update(fields)
        return self.nom.save_variant(data)["id"]

    def test_spool_price_drives_the_cost(self):
        cheap_id = self._variant(spool_id=self.cheap)
        pricey_id = self._variant(spool_id=self.pricey, name="Белый / S")
        cheap = self.nom.variant_economics(cheap_id)
        pricey = self.nom.variant_economics(pricey_id)
        self.assertEqual("variant", cheap["spool_source"])
        self.assertGreater(pricey["cost"], cheap["cost"],
                           "дорогой пластик не может давать ту же себестоимость")
        self.assertGreater(pricey["price"], cheap["price"],
                           "цена считается из себестоимости, а не из головы")

    def test_own_spool_beats_auto_choice(self):
        variant_id = self._variant(spool_id=self.pricey, color_name="Красный")
        eco = self.nom.variant_economics(variant_id)
        self.assertEqual(self.pricey, eco["spool"]["id"],
                         "цвет катушки не совпадает с вариантом, но выбор "
                         "сделал человек — он и главнее")

    def test_without_spool_the_material_one_is_taken_and_told(self):
        variant_id = self._variant()
        eco = self.nom.variant_economics(variant_id)
        self.assertEqual(self.cheap, eco["spool"]["id"])
        self.assertEqual("auto", eco["spool_source"])
        self.assertIn("PLA", eco["material"])

    def test_norm_overrides_of_the_variant_are_used(self):
        light = self._variant(spool_id=self.cheap, grams=2)
        heavy = self._variant(spool_id=self.cheap, grams=200, name="Тяжёлый")
        self.assertGreater(self.nom.variant_economics(heavy)["cost"],
                           self.nom.variant_economics(light)["cost"])

    def test_variant_without_spool_at_all_is_honest(self):
        self.db.execute("DELETE FROM spools")
        eco = self.nom.variant_economics(self._variant())
        self.assertEqual("none", eco["spool_source"])
        self.assertEqual({}, eco["spool"])
        self.assertGreater(eco["cost"], 0,
                           "без катушки считаем по справочной цене пластика")

    def test_own_price_is_not_overwritten(self):
        variant_id = self._variant(spool_id=self.cheap, price=999)
        result = self.nom.recalc_variant_prices(self.item["id"])
        row = self.db.one("SELECT * FROM nom_variants WHERE id=?", (variant_id,))
        self.assertEqual(999, row["price"],
                         "своя цена — решение владельца, машина её не отменяет")
        self.assertEqual(1, result["kept_manual_price"])
        self.assertGreater(row["cost"], 0, "себестоимость обновляется всегда")

    def test_recalc_writes_prices_for_all_auto_variants(self):
        first = self._variant(spool_id=self.cheap, name="Чёрный / S")
        second = self._variant(spool_id=self.pricey, name="Белый / S")
        result = self.nom.recalc_variant_prices(self.item["id"])
        self.assertEqual(2, result["updated"])
        rows = {r["id"]: r for r in self.db.query(
            "SELECT id, cost, price FROM nom_variants WHERE nom_id=?",
            (self.item["id"],))}
        self.assertGreater(rows[second]["price"], rows[first]["price"])
        self.assertTrue(all(num > 0 for num in
                            (rows[first]["cost"], rows[second]["cost"])))

    def test_spool_options_show_price_per_gram(self):
        options = self.nom.spool_options()
        self.assertEqual(2, len(options))
        per_gram = {o["id"]: o["per_gram"] for o in options}
        self.assertAlmostEqual(1.2, per_gram[self.cheap], places=4)
        self.assertAlmostEqual(3.6, per_gram[self.pricey], places=4)


class VariantSurfaceTests(unittest.TestCase):
    """Маршруты и карточка товара: контракт со страницей."""

    def setUp(self):
        self.db = make_db(min_order_price=0, price_rounding=1)
        self.nom = Nomenclature(self.db)
        self.item = add_nom(self.nom)
        from connector.printflow.api import Api
        self.api = Api.__new__(Api)
        self.api.db = self.db
        self.api.nom = self.nom
        self.api.catalog_changed = lambda *a, **k: None

    def test_read_routes_are_registered(self):
        from connector.printflow.api import router
        paths = {r["path"] for r in router.reference()}
        for path in ("/api/nomenclature/variant/economics",
                     "/api/nomenclature/spools"):
            self.assertIn(path, paths, f"нет маршрута {path}")

    def test_generate_route_creates_variations(self):
        code, body = self.api.post("/api/nomenclature/variants/generate", {
            "nom_id": self.item["id"],
            "axes": [{"name": "Цвет", "values": ["Чёрный", "Белый"]},
                     {"name": "Размер", "values": ["S", "L"]}]}, {})
        self.assertEqual(200, code)
        self.assertEqual(4, body["created"])

    def test_generate_route_previews_without_writing(self):
        code, body = self.api.post("/api/nomenclature/variants/generate", {
            "nom_id": self.item["id"], "preview": True,
            "axes": [{"name": "Цвет", "values": ["Чёрный", "Белый"]}]}, {})
        self.assertEqual(200, code)
        self.assertEqual(2, body["would_create"])
        self.assertEqual(0, len(self.nom.item(self.item["id"])["variants"]))

    def test_bad_axes_are_answered_with_words_not_a_crash(self):
        code, body = self.api.post("/api/nomenclature/variants/generate",
                                   {"nom_id": self.item["id"], "axes": []}, {})
        self.assertEqual(400, code)
        self.assertIn("ось", body["error"].lower())

    def test_recalc_route_writes_prices(self):
        spool = add_spool(self.db, "PLA", 1200)
        variant_id = self.nom.save_variant(
            {"nom_id": self.item["id"], "name": "Чёрный / S",
             "spool_id": spool})["id"]
        code, body = self.api.post("/api/nomenclature/variants/recalc",
                                   {"nom_id": self.item["id"]}, {})
        self.assertEqual(200, code)
        self.assertEqual(1, body["updated"])
        row = self.db.one("SELECT * FROM nom_variants WHERE id=?", (variant_id,))
        self.assertGreater(row["price"], 0)
        self.assertGreater(row["cost"], 0)

    def test_economics_route_reads_the_variant(self):
        spool = add_spool(self.db, "PETG", 3600)
        variant_id = self.nom.save_variant(
            {"nom_id": self.item["id"], "name": "Белый / S", "spool_id": spool})["id"]
        # Значения запроса приходят списком — так их читает Ctx.one().
        code, body = self.api.get("/api/nomenclature/variant/economics",
                                  {"id": [variant_id]})
        self.assertEqual(200, code)
        self.assertEqual(spool, body["spool"]["id"])
        self.assertGreater(body["price"], body["cost"])

    def test_product_card_has_a_variants_pane(self):
        page = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "site" / "assets" / "products.js").read_text(encoding="utf-8")
        self.assertIn('data-pane="variants"', page)
        self.assertIn('id="nompane-variants"', page)
        for call in ("/api/nomenclature/variants/generate",
                     "/api/nomenclature/variants/recalc",
                     "/api/nomenclature/variant/spool",
                     "/api/nomenclature/variant/save",
                     "/api/nomenclature/spools"):
            self.assertIn(call, script, f"карточка не зовёт {call}")
        self.assertIn("вариант", page.lower())


class SpoolAutoBindTests(unittest.TestCase):
    """Катушка привязывается к новой вариации сама, когда выбор однозначен.

    Создание 60 вариаций и потом назначение каждой катушки руками —
    лишний час работы. Правило: ровно одна живая катушка под цвет/пластик —
    привязываем; ноль или несколько — человек решает в карточке, ошибочная
    привязка дороже её отсутствия.
    """

    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.nom = Nomenclature(self.db)
        self.item = add_nom(self.nom)

    def _variants(self):
        return self.db.query(
            "SELECT * FROM nom_variants WHERE nom_id=?", (self.item["id"],))

    def test_single_matching_spool_binds(self):
        spool = add_spool(self.db, "PLA", 1600, "Чёрный")
        result = self.nom.generate_variants(self.item["id"], [
            {"name": "Цвет", "values": [{"name": "Чёрный", "hex": "#111111",
                                         "material": "PLA"}, "Белый"]}])
        self.assertEqual(1, result["spools_bound"])
        rows = {r["name"]: r for r in self._variants()}
        self.assertEqual(spool, rows["Чёрный"]["spool_id"])
        self.assertEqual("", rows["Белый"]["spool_id"])

    def test_ambiguous_spool_stays_unbound(self):
        add_spool(self.db, "PLA", 1600, "Чёрный")
        add_spool(self.db, "PLA", 1800, "Чёрный")
        result = self.nom.generate_variants(self.item["id"], [
            {"name": "Цвет", "values": ["Чёрный"]}])
        self.assertEqual(0, result["spools_bound"])
        self.assertEqual("", self._variants()[0]["spool_id"])

    def test_material_axis_binds_when_only_spool_of_material(self):
        pla = add_spool(self.db, "PLA", 1600, "Серый")
        petg = add_spool(self.db, "PETG", 3200, "Серый")
        result = self.nom.generate_variants(self.item["id"], [
            {"name": "Пластик", "values": ["PLA", "PETG"]}])
        self.assertEqual(2, result["spools_bound"])
        rows = {r["name"]: r for r in self._variants()}
        self.assertEqual(pla, rows["PLA"]["spool_id"])
        self.assertEqual(petg, rows["PETG"]["spool_id"])

    def test_color_only_matches_across_materials_only_when_unique(self):
        add_spool(self.db, "PETG", 3200, "Синий")
        result = self.nom.generate_variants(self.item["id"], [
            {"name": "Цвет", "values": ["Синий"]}])
        self.assertEqual(1, result["spools_bound"])
        add_spool(self.db, "PLA", 1600, "Красный")
        add_spool(self.db, "PETG", 3200, "Красный")
        result = self.nom.generate_variants(self.item["id"], [
            {"name": "Цвет", "values": ["Красный"]}])
        self.assertEqual(0, result["spools_bound"],
                         "две катушки одного цвета — человек выбирает сам")

    def test_archived_spool_is_not_a_candidate(self):
        spool = add_spool(self.db, "PLA", 1600, "Белый")
        self.db.upsert("spools", {"id": spool, "archived": 1})
        result = self.nom.generate_variants(self.item["id"], [
            {"name": "Цвет", "values": ["Белый"]}])
        self.assertEqual(0, result["spools_bound"])
        self.assertEqual("", self._variants()[0]["spool_id"])


if __name__ == "__main__":
    unittest.main()


class VariantStructureTests(unittest.TestCase):
    """Состав вариации из нескольких катушек (18.5, М2).

    Классика: брелок тело красное (PLA 20 г) + хвост белый (PETG 5 г) —
    себестоимость складывается по каждой катушке, а не берётся от одной.
    Без состава вариация живёт старым путём: одна катушка или автоподбор.
    """

    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.nom = Nomenclature(self.db)
        self.item = add_nom(self.nom)
        self.nom.save_variant({
            "id": "v1", "nom_id": self.item["id"], "name": "Двухцветный",
            "grams": 0, "spool_id": ""})
        self.red = add_spool(self.db, "PLA", 1600, "Красный")
        self.white = add_spool(self.db, "PETG", 3200, "Белый")

    def tearDown(self):
        self.db.close()

    def test_structure_cost_is_sum_by_spools(self):
        self.nom.save_variant_structures("v1", [
            {"spool_id": self.red, "grams": 20},
            {"spool_id": self.white, "grams": 5}])
        rows = self.nom.variant_structures("v1")
        assert len(rows) == 2
        eco = self.nom.variant_economics("v1")
        assert eco["spool_source"] == "structures"
        assert eco["grams"] == 25  # 20 + 5
        assert sum(r["filament_cost"] for r in rows) > 0
        # пластик дороже одной катушки: цена грамма по каждой соответственно
        # 20 г по 1.6 ₽/г + 5 г по 3.2 ₽/г = 48 ₽ чистый пластик
        # цена не должна быть как у одной PLA-катушки (там было бы 40 ₽)
        mat_cost = sum(r["filament_cost"] for r in rows)
        assert abs(mat_cost - (20 * 1.6 + 5 * 3.2)) < 0.01

    def test_more_than_eight_rejected(self):
        with self.assertRaises(ValueError):
            self.nom.save_variant_structures("v1", [
                {"spool_id": self.red, "grams": 10}] * 9)

    def test_duplicate_spool_rejected(self):
        with self.assertRaises(ValueError):
            self.nom.save_variant_structures("v1", [
                {"spool_id": self.red, "grams": 5},
                {"spool_id": self.red, "grams": 5}])

    def test_zero_grams_rejected(self):
        with self.assertRaises(ValueError):
            self.nom.save_variant_structures("v1", [
                {"spool_id": self.red, "grams": 0}])

    def test_missing_spool_rejected(self):
        with self.assertRaises(ValueError):
            self.nom.save_variant_structures("v1", [
                {"spool_id": "sp-nothere", "grams": 10}])

    def test_single_spool_is_plain_binding_not_structure(self):
        self.nom.save_variant_structures("v1", [{"spool_id": self.red, "grams": 30}])
        assert self.nom.variant_structures("v1") == []
        row = self.db.one("SELECT spool_id FROM nom_variants WHERE id=?", ("v1",))
        assert row["spool_id"] == self.red

    def test_clearing_returns_old_behaviour(self):
        self.nom.save_variant_structures("v1", [
            {"spool_id": self.red, "grams": 20},
            {"spool_id": self.white, "grams": 5}])
        self.nom.save_variant_structures("v1", [])
        assert self.nom.variant_structures("v1") == []
        eco = self.nom.variant_economics("v1")
        assert eco["spool_source"] != "structures"

    def test_recalc_updates_variant_cost(self):
        self.nom.save_variant_structures("v1", [
            {"spool_id": self.red, "grams": 20},
            {"spool_id": self.white, "grams": 5}])
        row = self.db.one("SELECT cost FROM nom_variants WHERE id=?", ("v1",))
        eco = self.nom.variant_economics("v1")
        assert abs(row["cost"] - eco["cost"]) < 0.01


class VariantPhotoServiceTests(unittest.TestCase):
    """Фото вариации (18.5, М4): загрузка через сервис, ошибки словами."""

    def setUp(self):
        self.db = make_db()
        self.acc = Accounting(self.db)
        self.nom = Nomenclature(self.db)
        self.item = add_nom(self.nom)
        self.nom.save_variant({
            "id": "vp1", "nom_id": self.item["id"], "name": "Чёрный / L"})

    def tearDown(self):
        self.db.close()

    def test_photo_saved_and_column_set(self):
        import base64
        raw = b"\x89PNG\r\n\x1a\nfakeimg"
        data_url = "data:image/png;base64," + base64.b64encode(raw).decode()
        name = self.nom.set_variant_photo("vp1", data_url)
        row = self.db.one("SELECT photo FROM nom_variants WHERE id=?", ("vp1",))
        assert row["photo"] == name == "var_vp1.png"

    def test_unknown_variant_404_wording(self):
        with self.assertRaises(LookupError):
            self.nom.set_variant_photo("nope", "data:image/png;base64,AAA=")

    def test_bad_data_url_rejected(self):
        with self.assertRaises(ValueError):
            self.nom.set_variant_photo("vp1", "not-a-data-url")

    def test_oversized_rejected(self):
        import base64
        huge = base64.b64encode(b"0" * (8 * 1024 * 1024 + 10)).decode()
        with self.assertRaises(ValueError):
            self.nom.set_variant_photo("vp1", "data:image/jpeg;base64," + huge)

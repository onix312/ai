"""Умные значения катушек из AMS: цвет, таблица материалов, выученные правила.

Контракт 18.13: катушка, которую завёл автопилот, должна быть похожа на
правду, а не на «1000 г, цена 0, цвет Синий» для любой бобины. Проверяем три
источника (`ams_defaults`): правило владельца → таблица материалов → встроенные
значения, и то, что разовая правка правилом не становится.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import ams_defaults  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "test.sqlite3")


class ColorNameTests(unittest.TestCase):
    def test_known_colors_are_exact(self):
        self.assertEqual("Чёрный", ams_defaults.color_name_for("#000000"))
        self.assertEqual("Белый", ams_defaults.color_name_for("#FFFFFF"))
        self.assertEqual("Зелёный", ams_defaults.color_name_for("#00AE42"))

    def test_hue_gives_a_real_name(self):
        """Шесть слов старой палитры не различали фиолетовый и бирюзовый."""
        self.assertEqual("Фиолетовый", ams_defaults.color_name_for("#8A2BE2"))
        self.assertEqual("Бирюзовый", ams_defaults.color_name_for("#30C8C8"))
        self.assertEqual("Салатовый", ams_defaults.color_name_for("#9ACD32"))

    def test_dark_and_light_are_qualified(self):
        self.assertEqual("Тёмно-синий", ams_defaults.color_name_for("#000040"))
        self.assertEqual("Светло-розовый", ams_defaults.color_name_for("#FFD6E0"))

    def test_grey_scale_by_lightness(self):
        self.assertEqual("Тёмно-серый", ams_defaults.color_name_for("#3C3C3C"))
        self.assertEqual("Серый", ams_defaults.color_name_for("#8A8A8A"))
        self.assertEqual("Светло-серый", ams_defaults.color_name_for("#D8D8D8"))

    def test_garbage_is_not_a_color(self):
        self.assertEqual("", ams_defaults.color_name_for(""))
        self.assertEqual("", ams_defaults.color_name_for("#12"))
        self.assertEqual("", ams_defaults.color_name_for("не цвет"))
        self.assertEqual("", ams_defaults.normalize_hex("00000000"))

    def test_bambu_eight_digit_hex_is_understood(self):
        """Принтер отдаёт RRGGBBAA — цвет это первые шесть знаков."""
        self.assertEqual("#1A66E0", ams_defaults.normalize_hex("1A66E0FF"))


class MaterialTableTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

    def tearDown(self):
        self.db.close()

    def test_table_roundtrip_and_normalisation(self):
        ams_defaults.update_material_default(self.db, "petg", total_grams=750,
                                             price=2100, brand="eSUN")
        table = ams_defaults.material_defaults(self.db)
        self.assertEqual({"PETG"}, set(table))
        self.assertEqual(750.0, table["PETG"]["total_grams"])
        self.assertEqual(2100.0, table["PETG"]["price"])
        self.assertEqual("eSUN", table["PETG"]["brand"])

    def test_zero_clears_the_row(self):
        ams_defaults.update_material_default(self.db, "PLA", total_grams=1000)
        ams_defaults.update_material_default(self.db, "PLA", total_grams=0)
        self.assertEqual({}, ams_defaults.material_defaults(self.db))


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

    def tearDown(self):
        self.db.close()

    def rule(self, ident: str) -> dict:
        return self.db.one("SELECT * FROM ams_rules WHERE id=?", (ident,)) or {}

    def test_single_edit_is_only_a_candidate(self):
        """Одна правка — ещё не правило: опечатка не должна разъезжаться."""
        learned = ams_defaults.learn_observation(
            self.db, "spool_defaults", "PLA|bambu", {"price": 1900})
        self.assertFalse(learned["applied"])
        self.assertEqual(1, learned["seen"])
        self.assertEqual({}, ams_defaults.applied_rules(self.db))
        again = ams_defaults.learn_observation(
            self.db, "spool_defaults", "PLA|bambu", {"price": 1900})
        self.assertTrue(again["applied"], "две одинаковые правки не стали правилом")

    def test_different_values_do_not_confirm_each_other(self):
        ams_defaults.learn_observation(self.db, "spool_defaults", "PLA|", {"price": 1900})
        ams_defaults.learn_observation(self.db, "spool_defaults", "PLA|", {"price": 2400})
        applied = ams_defaults.applied_rules(self.db)
        self.assertEqual({}, applied, "разные цифры не должны подтверждать правило")

    def test_learn_from_edit_ignores_empty_values(self):
        learned = ams_defaults.learn_from_edit(
            self.db,
            {"material": "PLA", "brand": "Bambu", "total_grams": 1000, "price": 1900,
             "color_name": "Зелёный", "color_hex": "#00AE42"},
            {"material": "PLA", "brand": "", "total_grams": 1000, "price": 0,
             "color_name": "", "color_hex": "#00AE42"})
        self.assertEqual([], learned, "пустой бренд и нулевая цена — это «не знаю»")

    def test_learn_from_edit_records_both_kinds(self):
        learned = ams_defaults.learn_from_edit(
            self.db,
            {"material": "PLA", "brand": "Bambu", "total_grams": 1000, "price": 1900,
             "color_name": "Зелёный", "color_hex": "#00AE42"},
            {"material": "PLA", "brand": "Bambu", "total_grams": 750, "price": 2200,
             "color_name": "Изумрудный", "color_hex": "#00AE42"})
        kinds = {item["kind"] for item in learned}
        self.assertEqual({"spool_defaults", "color_name"}, kinds)
        self.assertIn("PLA|bambu", {item["key"] for item in learned})

    def test_manual_rule_applies_at_once(self):
        ams_defaults.set_rule(self.db, "spool_defaults", "PETG|esun",
                              {"total_grams": 750, "price": 2400, "brand": "eSUN"})
        values = ams_defaults.resolve_defaults(self.db, material="PETG",
                                               brand_hint="eSUN")
        self.assertEqual(750.0, values["total_grams"])
        self.assertEqual(2400.0, values["price"])
        self.assertEqual(1, values["verified"])

    def test_forget_rule(self):
        rule = ams_defaults.set_rule(self.db, "spool_defaults", "PLA|", {"price": 500})
        self.assertEqual(1, len(ams_defaults.rules(self.db)))
        self.assertEqual(1, ams_defaults.forget_rule(self.db, rule["id"]))
        self.assertEqual({}, ams_defaults.applied_rules(self.db))


class ResolveDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

    def tearDown(self):
        self.db.close()

    def test_builtin_is_honest_about_being_unknown(self):
        values = ams_defaults.resolve_defaults(self.db, material="PLA",
                                               color_hex="#00AE42")
        self.assertEqual(1000.0, values["total_grams"])
        self.assertEqual(1600.0, values["price"], "цена по умолчанию из настроек")
        self.assertEqual(0, values["verified"], "неизвестное выдано за проверенное")
        self.assertIn("бренд", values["missing"])
        self.assertTrue(values["note"])

    def test_table_wins_over_builtin(self):
        ams_defaults.update_material_default(self.db, "PLA", total_grams=750,
                                             price=1900, brand="Bambu Lab")
        values = ams_defaults.resolve_defaults(self.db, material="PLA")
        self.assertEqual("table", values["source"])
        self.assertEqual(750.0, values["total_grams"])
        self.assertEqual(1, values["verified"])

    def test_rule_wins_over_table(self):
        ams_defaults.update_material_default(self.db, "PLA", total_grams=1000,
                                             price=1900, brand="Generic")
        ams_defaults.set_rule(self.db, "spool_defaults", "PLA|bambu lab",
                              {"total_grams": 750, "price": 2200})
        values = ams_defaults.resolve_defaults(self.db, material="PLA",
                                               brand_hint="Bambu Lab")
        self.assertEqual(750.0, values["total_grams"])
        self.assertEqual(2200.0, values["price"])
        self.assertEqual("rule", values["source"])

    def test_printer_brand_is_more_trusted_than_rule(self):
        """RFID-метка говорит про конкретную бобину, правило — про привычку."""
        ams_defaults.set_rule(self.db, "spool_defaults", "PLA|",
                              {"brand": "Generic", "price": 1000})
        values = ams_defaults.resolve_defaults(self.db, material="PLA",
                                               brand_hint="Bambu Lab")
        self.assertEqual("Bambu Lab", values["brand"])
        self.assertEqual("printer", values["brand_source"])

    def test_tpu_defaults_to_half_kilo(self):
        values = ams_defaults.resolve_defaults(self.db, material="TPU")
        self.assertEqual(500.0, values["total_grams"])

    def test_learned_color_name_is_used_for_the_same_hex(self):
        ams_defaults.set_rule(self.db, "color_name", "PLA|#00AE42",
                              {"color_name": "Изумрудный Bambu"})
        values = ams_defaults.resolve_defaults(self.db, material="PLA",
                                               color_hex="#00AE42")
        self.assertEqual("Изумрудный Bambu", values["color_name"])

    def test_color_falls_back_to_own_dictionary(self):
        values = ams_defaults.resolve_defaults(self.db, material="PLA",
                                               color_hex="#30C8C8")
        self.assertEqual("Бирюзовый", values["color_name"])


class SaveSpoolLearningTests(unittest.TestCase):
    """Сквозной путь владельца: поправил карточку дважды — подставляется само."""

    def setUp(self):
        self.db = make_db()
        self.repo = Repo(self.db)

    def tearDown(self):
        self.db.close()

    def card(self, **over: object) -> dict:
        data = {"id": "sp1", "material": "PLA", "brand": "", "color_name": "Зелёный",
                "color_hex": "#00AE42", "total_grams": 1000, "remaining_grams": 900,
                "price": 0, "verified": 0, "ams_auto": 1, "archived": 0}
        data.update(over)
        return data

    def test_two_edits_create_a_rule_and_it_is_applied(self):
        """Две одинаковые правки (на двух катушках) — и значение подставляется."""
        self.db.upsert("spools", {**self.card(id="sp1"), "material": "PLA"})
        self.db.upsert("spools", {**self.card(id="sp2"), "material": "PLA"})
        self.repo.save_spool(self.card(id="sp1", brand="Bambu Lab",
                                       total_grams=750, price=2200))
        values = ams_defaults.resolve_defaults(self.db, material="PLA",
                                               brand_hint="Bambu Lab")
        self.assertEqual(1000.0, values["total_grams"],
                         "одна правка уже стала правилом")
        self.repo.save_spool(self.card(id="sp2", brand="Bambu Lab",
                                       total_grams=750, price=2200))
        values = ams_defaults.resolve_defaults(self.db, material="PLA",
                                               brand_hint="Bambu Lab")
        self.assertEqual(750.0, values["total_grams"])
        self.assertEqual(2200.0, values["price"])
        self.assertEqual("rule", values["source"])

    def test_learning_is_visible_in_the_actions_feed(self):
        self.db.upsert("spools", {**self.card(), "material": "PLA"})
        self.repo.save_spool(self.card(brand="Bambu Lab", total_grams=750))
        rows = self.db.query("SELECT * FROM ams_actions WHERE kind='learn'")
        self.assertTrue(rows, "правка карточки не попала в ленту автопилота")

    def test_learning_toggle_off(self):
        self.db.set_settings({"ams_learn": False})
        self.db.upsert("spools", {**self.card(), "material": "PLA"})
        self.repo.save_spool(self.card(total_grams=750))
        self.assertEqual([], ams_defaults.rules(self.db))

    def test_new_spool_is_not_learning_material(self):
        """Создание карточки — не правка: учиться можно только на изменении."""
        created = self.repo.save_spool(self.card(id="", material="PETG",
                                                 total_grams=800, price=1900))
        self.assertTrue(created.get("id"))
        self.assertEqual([], ams_defaults.rules(self.db))


if __name__ == "__main__":
    unittest.main()

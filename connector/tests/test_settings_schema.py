"""Схема настроек: одна правда для формы, валидации и диагностики (идея 10).

До схемы опечатка в имени ключа создавала настройку-призрак, а граница
«сколько бэкапов хранить» проверялась в трёх местах по-разному. Теперь
тип, группа, подпись и ограничения описаны один раз.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.config import DEFAULT_SETTINGS  # noqa: E402
from connector.printflow.settings_schema import (  # noqa: E402
    describe, diff_defaults, get_schema, kind_of, unknown_keys, validate)


class SchemaShapeTests(unittest.TestCase):
    def setUp(self):
        self.spec = get_schema()

    def test_every_default_setting_is_described(self):
        missing = sorted(set(DEFAULT_SETTINGS) - set(self.spec))
        self.assertEqual([], missing,
                         f"настройки без описания в схеме: {missing[:10]}")

    def test_each_item_carries_contract(self):
        for key, item in self.spec.items():
            self.assertEqual(item["key"], key)
            self.assertIn(item["type"], ("bool", "int", "float", "str", "json"))
            self.assertTrue(item["label"], f"{key}: нет подписи")
            self.assertTrue(item["group"], f"{key}: нет группы")

    def test_secrets_are_marked(self):
        for key in ("telegram_token", "client_bot_token", "cloud_token"):
            self.assertTrue(self.spec[key]["secret"], key)

    def test_kind_of_detects_types(self):
        self.assertEqual(kind_of(True), "bool")
        self.assertEqual(kind_of(3), "int")
        self.assertEqual(kind_of(2.5), "float")
        self.assertEqual(kind_of([1, 2]), "json")
        self.assertEqual(kind_of("текст"), "str")

    def test_describe_groups_fields_and_hides_secrets(self):
        payload = describe()
        self.assertEqual(payload["count"], len(self.spec))
        self.assertTrue(payload["groups"])
        for rows in payload["fields"].values():
            for row in rows:
                self.assertNotIn("default", row, row["key"])
                if row.get("hidden"):
                    self.assertTrue(row["key"] in
                                    ("telegram_token", "client_bot_token", "cloud_token",
                                     "cloud_uid", "studio_gateway_access_code"))


class ValidateTests(unittest.TestCase):
    def test_unknown_key_is_dropped_not_stored(self):
        clean, _warnings, unknown = validate({"telegram_tokn": "опечатка"})
        self.assertEqual(clean, {})
        self.assertEqual(unknown, ["telegram_tokn"])

    def test_unknown_keys_helper(self):
        self.assertEqual(unknown_keys({"backup_keep": 5, "несуществует": 1}),
                         ["несуществует"])

    def test_int_is_coerced_and_clamped(self):
        clean, warnings, _ = validate({"backup_keep": "30"})
        self.assertEqual(clean["backup_keep"], 30)
        self.assertEqual(warnings, [])
        clean, warnings, _ = validate({"backup_keep": 9999})
        self.assertEqual(clean["backup_keep"], 200)
        self.assertTrue(any("выше максимума" in w for w in warnings))

    def test_non_numeric_falls_back_to_default_with_warning(self):
        clean, warnings, _ = validate({"backup_keep": "много"})
        self.assertEqual(clean["backup_keep"], DEFAULT_SETTINGS["backup_keep"])
        self.assertTrue(any("ожидалось число" in w for w in warnings))

    def test_bool_accepts_human_words(self):
        for truthy in ("1", "true", "on", "да", True, 1):
            clean, warnings, _ = validate({"telegram_bot": truthy})
            self.assertIs(clean["telegram_bot"], True, repr(truthy))
            self.assertEqual(warnings, [])
        clean, _, _ = validate({"telegram_bot": "нет"})
        self.assertIs(clean["telegram_bot"], False)

    def test_max_len_is_enforced(self):
        clean, warnings, _ = validate({"public_url": "https://x/" + "a" * 500})
        self.assertLessEqual(len(clean["public_url"]), 300)
        self.assertTrue(warnings)

    def test_non_dict_patch_is_rejected(self):
        clean, warnings, unknown = validate("не объект")
        self.assertEqual((clean, unknown), ({}, []))
        self.assertTrue(warnings)

    def test_empty_patch_is_clean(self):
        self.assertEqual(validate({}), ({}, [], []))


class DiffDefaultsTests(unittest.TestCase):
    def test_changed_values_are_listed(self):
        current = dict(DEFAULT_SETTINGS)
        current["backup_keep"] = DEFAULT_SETTINGS["backup_keep"] + 1
        changed = diff_defaults(current)
        self.assertIn("backup_keep", [row["key"] for row in changed])

    def test_secrets_never_leak_into_diff(self):
        current = dict(DEFAULT_SETTINGS)
        current["telegram_token"] = "123456:секрет"
        self.assertNotIn("telegram_token", [row["key"] for row in diff_defaults(current)])

    def test_unchanged_settings_are_not_listed(self):
        self.assertEqual(diff_defaults(dict(DEFAULT_SETTINGS)), [])


if __name__ == "__main__":
    unittest.main()

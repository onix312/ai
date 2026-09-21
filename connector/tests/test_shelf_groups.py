"""Группы витрины: средний ценник, медиана, выравнивание цен.

Контракт v1 (grill-me):
• ручная группа ≥ 2 позиций, позиция ≤ 1 группа;
• медиана по price>0, округление до рубля;
• выравнивание только по кнопке, пишет только ненулевым, tag_old_price не трогает;
• печать (printable) по факту равенства ненулевых цен, ≥2 с price>0;
• выход/распуск цены не откатывает;
• members по умолчанию не в labels.shelf для печати (флаг group_id).
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database
from connector.printflow.shelf import (
    GROUP_STATUS_INCOMPLETE,
    GROUP_STATUS_NEEDS_ALIGN,
    GROUP_STATUS_READY,
    Shelf,
    group_median,
)


_held: list[tempfile.TemporaryDirectory] = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "test.sqlite3")


class GroupMedianTests(unittest.TestCase):
    def test_odd_and_even_round_to_ruble(self):
        self.assertEqual(490.0, group_median([390, 490, 790]))
        # (390+790)/2 = 590
        self.assertEqual(590.0, group_median([390, 790]))
        # (400+500)/2 = 450
        self.assertEqual(450.0, group_median([400, 500]))
        # (401+502)/2 = 451.5 → 452
        self.assertEqual(452.0, group_median([401, 502]))

    def test_zeros_are_ignored(self):
        self.assertEqual(500.0, group_median([0, 500, 0]))
        self.assertEqual(0.0, group_median([0, 0]))
        self.assertEqual(0.0, group_median([]))


class ShelfGroupCoreTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.shelf = Shelf(self.db)
        self.a = self.shelf.save_item({"name": "Крючок A", "qty": 3, "price": 390})
        self.b = self.shelf.save_item({"name": "Крючок B", "qty": 2, "price": 490})
        self.c = self.shelf.save_item({"name": "Крючок C", "qty": 1, "price": 790})
        self.d = self.shelf.save_item({"name": "Без цены", "qty": 5, "price": 0})

    def tearDown(self):
        self.db.close()

    def test_save_requires_name_and_two_members(self):
        with self.assertRaisesRegex(ValueError, "название"):
            self.shelf.save_group({"name": "  ", "member_ids": [self.a["id"], self.b["id"]]})
        with self.assertRaisesRegex(ValueError, "минимум 2"):
            self.shelf.save_group({"name": "Один", "member_ids": [self.a["id"]]})

    def test_group_membership_is_exclusive(self):
        g1 = self.shelf.save_group({
            "name": "Крючки", "member_ids": [self.a["id"], self.b["id"]],
        })
        with self.assertRaisesRegex(ValueError, "уже в группе"):
            self.shelf.save_group({
                "name": "Другие", "member_ids": [self.a["id"], self.c["id"]],
            })
        # Тот же состав в своей группе — ок (обновление).
        again = self.shelf.save_group({
            "id": g1["id"], "name": "Крючки 2",
            "member_ids": [self.a["id"], self.b["id"], self.c["id"]],
        })
        self.assertEqual("Крючки 2", again["name"])
        self.assertEqual(3, again["member_count"])

    def test_status_needs_align_then_ready_after_align(self):
        g = self.shelf.save_group({
            "name": "Ряд", "member_ids": [self.a["id"], self.b["id"], self.c["id"]],
        })
        self.assertEqual(GROUP_STATUS_NEEDS_ALIGN, g["status"])
        self.assertFalse(g["printable"])
        # медиана 390,490,790 → 490
        self.assertEqual(490.0, g["median"])
        preview = self.shelf.align_group_preview(g["id"])
        # B уже стоит 490 — меняется только 390 и 790.
        self.assertEqual(2, len(preview["changes"]))
        result = self.shelf.align_group(g["id"])
        self.assertEqual(490.0, result["median"])
        self.assertEqual(2, result["changed"])
        fresh = self.shelf.group(g["id"])
        self.assertEqual(GROUP_STATUS_READY, fresh["status"])
        self.assertTrue(fresh["printable"])
        for item_id in (self.a["id"], self.b["id"], self.c["id"]):
            self.assertEqual(490.0, self.shelf.item(item_id)["price"])

    def test_align_skips_zero_price_and_keeps_tag_old_price(self):
        self.db.execute(
            "UPDATE shelf_items SET tag_old_price=? WHERE id=?",
            (990.0, self.a["id"]))
        g = self.shelf.save_group({
            "name": "С нулём",
            "member_ids": [self.a["id"], self.b["id"], self.d["id"]],
        })
        # d с price=0 не в медиане; медиана (390+490)/2 = 440
        self.assertEqual(440.0, g["median"])
        self.shelf.align_group(g["id"])
        self.assertEqual(440.0, self.shelf.item(self.a["id"])["price"])
        self.assertEqual(440.0, self.shelf.item(self.b["id"])["price"])
        self.assertEqual(0.0, self.shelf.item(self.d["id"])["price"])
        self.assertEqual(990.0, self.shelf.item(self.a["id"])["tag_old_price"])

    def test_manual_equal_prices_are_printable_without_button(self):
        self.shelf.save_item({"id": self.a["id"], "name": "A", "price": 500})
        self.shelf.save_item({"id": self.b["id"], "name": "B", "price": 500})
        g = self.shelf.save_group({
            "name": "Уже ровно", "member_ids": [self.a["id"], self.b["id"]],
        })
        self.assertEqual(GROUP_STATUS_READY, g["status"])
        self.assertTrue(g["printable"])
        self.assertEqual(500.0, g["price"])

    def test_price_drift_blocks_print_again(self):
        g = self.shelf.save_group({
            "name": "Ряд", "member_ids": [self.a["id"], self.b["id"]],
        })
        self.shelf.align_group(g["id"])
        self.assertTrue(self.shelf.group(g["id"])["printable"])
        self.shelf.save_item({"id": self.a["id"], "name": "A", "price": 999})
        fresh = self.shelf.group(g["id"])
        self.assertEqual(GROUP_STATUS_NEEDS_ALIGN, fresh["status"])
        self.assertFalse(fresh["printable"])

    def test_incomplete_when_fewer_than_two_priced(self):
        g = self.shelf.save_group({
            "name": "Почти", "member_ids": [self.a["id"], self.d["id"]],
        })
        self.assertEqual(GROUP_STATUS_INCOMPLETE, g["status"])
        self.assertFalse(g["printable"])
        with self.assertRaisesRegex(ValueError, "минимум двум"):
            self.shelf.align_group(g["id"])

    def test_leave_group_keeps_aligned_price(self):
        g = self.shelf.save_group({
            "name": "Ряд", "member_ids": [self.a["id"], self.b["id"], self.c["id"]],
        })
        self.shelf.align_group(g["id"])
        # Убрали C — группа остаётся (черновик неполная? нет, ещё 2).
        self.shelf.save_group({
            "id": g["id"], "name": "Ряд", "member_ids": [self.a["id"], self.b["id"]],
        })
        self.assertEqual(490.0, self.shelf.item(self.c["id"])["price"])
        self.assertEqual("", self.shelf.item(self.c["id"]).get("group_id") or "")

    def test_dissolve_keeps_prices(self):
        g = self.shelf.save_group({
            "name": "Ряд", "member_ids": [self.a["id"], self.b["id"]],
        })
        self.shelf.align_group(g["id"])
        median = self.shelf.item(self.a["id"])["price"]
        self.shelf.delete_group(g["id"])
        self.assertIsNone(self.shelf.group(g["id"]))
        self.assertEqual(median, self.shelf.item(self.a["id"])["price"])
        self.assertEqual("", self.shelf.item(self.a["id"]).get("group_id") or "")

    def test_items_expose_group_fields(self):
        g = self.shelf.save_group({
            "name": "Крючки", "member_ids": [self.a["id"], self.b["id"]],
        })
        item = next(i for i in self.shelf.items() if i["id"] == self.a["id"])
        self.assertEqual(g["id"], item["group_id"])
        self.assertEqual("Крючки", item["group_name"])

    def test_shrinking_below_two_via_update_is_blocked(self):
        g = self.shelf.save_group({
            "name": "Ряд", "member_ids": [self.a["id"], self.b["id"]],
        })
        with self.assertRaisesRegex(ValueError, "минимум 2"):
            self.shelf.save_group({
                "id": g["id"], "name": "Ряд", "member_ids": [self.a["id"]],
            })


class ShelfGroupLabelsTests(unittest.TestCase):
    def setUp(self):
        from connector.printflow.api import Api
        self.db = make_db()
        self.api = Api.__new__(Api)
        self.api.db = self.db
        self.api.shelf = Shelf(self.db)
        self.api.last_host = "192.168.1.20:8080"
        self.api.listen_port = 8080
        self.api.manager = types.SimpleNamespace(printers={}, bot=None)
        self.api.repo = types.SimpleNamespace(spools=lambda: [])
        self.a = self.api.shelf.save_item({
            "name": "A", "qty": 1, "price": 400, "barcode": "4601234567890",
        })
        self.b = self.api.shelf.save_item({
            "name": "B", "qty": 1, "price": 600,
        })
        self.lonely = self.api.shelf.save_item({
            "name": "Одиночка", "qty": 2, "price": 300,
        })

    def tearDown(self):
        self.db.close()

    def test_labels_hide_members_flag_and_expose_ready_groups(self):
        g = self.api.shelf.save_group({
            "name": "Пара", "member_ids": [self.a["id"], self.b["id"]],
        })
        # Пока не выровнено — группы в labels нет.
        payload = self.api.labels("shelf")
        self.assertEqual([], payload.get("shelf_groups") or [])
        member = next(x for x in payload["shelf"] if x["id"] == self.a["id"])
        self.assertEqual(g["id"], member["group_id"])
        self.api.shelf.align_group(g["id"])
        payload = self.api.labels("shelf")
        groups = payload.get("shelf_groups") or []
        self.assertEqual(1, len(groups))
        self.assertEqual("Пара", groups[0]["name"])
        self.assertTrue(groups[0]["is_group"])
        self.assertEqual("promo", groups[0]["tag_template"])
        self.assertEqual(500.0, groups[0]["price"])  # медиана 400/600

    def test_api_group_routes(self):
        code, body = self.api.post("/api/shelf/group/save", {
            "name": "Тест", "member_ids": [self.a["id"], self.b["id"]],
        }, {})
        self.assertEqual(200, code)
        gid = body["group"]["id"]
        code, listed = self.api.get("/api/shelf/groups", {})
        # GET идёт через router — в unit-тесте Api.get может не быть.
        # Проверяем post-цепочку align.
        code, prev = self.api.post("/api/shelf/group/align/preview", {"id": gid}, {})
        self.assertEqual(200, code)
        self.assertIn("changes", prev)
        code, aligned = self.api.post("/api/shelf/group/align", {"id": gid}, {})
        self.assertEqual(200, code)
        self.assertTrue(aligned["group"]["printable"])
        code, deleted = self.api.post("/api/shelf/group/delete", {"id": gid}, {})
        self.assertEqual(200, code)


class ShelfGroupFrontendContractTests(unittest.TestCase):
    """Строки UI: группа на стеллаже и в конструкторе ценников."""

    def test_shelf_ui_has_group_controls(self):
        index = (ROOT / "site/index.html").read_text(encoding="utf-8")
        script = (ROOT / "site/assets/shelf.js").read_text(encoding="utf-8")
        self.assertIn('id="shelf_group_modal"', index)
        self.assertIn('id="shelf_groups_list"', index)
        self.assertIn('id="sgf_align"', index)
        self.assertIn("openShelfGroup", script)
        self.assertIn("/api/shelf/group/align", script)
        self.assertIn("/api/shelf/group/save", script)

    def test_price_tags_hide_members_by_default(self):
        page = (ROOT / "site/price-tags.html").read_text(encoding="utf-8")
        script = (ROOT / "site/assets/shelf.js").read_text(encoding="utf-8")
        self.assertIn("showGroupMembers", page)
        self.assertIn("shelf_groups", page)
        self.assertIn("selectGroups", page)
        self.assertIn("listableItems", page)
        self.assertIn("is_group", page)
        # Глубокая ссылка на средний ценник строится на вкладке стеллажа.
        self.assertIn("price-tags.html?group=", script)
        self.assertIn("get('group')", page)


if __name__ == "__main__":
    unittest.main()

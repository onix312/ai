"""18.5 (М1/М5): многоцветные катушки и рекомендации производителя.

Что гарантируем:
* катушка хранит список цветов любой длины (colors_json) + вид (color_kind);
* однотонная катушка ведёт себя ровно как до 18.5: colors_json пуст;
* серверная раздача (spool_options) несёт цвета/вид для всех экранов;
* рекомендации производителя с бобины сохраняются и режутся разумно;
* в AMS вместе с типом/цветом уходят температуры сопла — и только явно
  записанные словом «сопло», а не случайный диапазон из текста про стол;
* контракт: на каждом экране, где цвет катушки — рабочий сигнал, свотч
  рисуется общим градиентом (assets/colors.js подключён и использован).
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.api import _nozzle_range_from_rec  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.nomenclature import Nomenclature  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402

_held: list = []


def _repo() -> Repo:
    _held.append(tempfile.TemporaryDirectory())
    return Repo(Database(pathlib.Path(_held[-1].name) / "spools.sqlite3"))


class TestMulticolorSpool(unittest.TestCase):
    def test_multicolor_saved_and_listed(self):
        store = _repo()
        s = store.save_spool({
            "material": "PLA", "brand": "Sunlu", "color_name": "Рассвет",
            "color_hex": "#ff0000", "color_kind": "gradient",
            "colors": ["#ff0000", "#ffaa00", "#00aa55"],
        })
        row = store.spool(s["id"])
        assert json.loads(row["colors_json"]) == ["#ff0000", "#ffaa00", "#00aa55"]
        assert row["color_kind"] == "gradient"
        assert row["color_hex"] == "#ff0000"  # первый цвет = основной
        opts = Nomenclature(store.db).spool_options()
        mine = next(o for o in opts if o["id"] == s["id"])
        assert mine["color_kind"] == "gradient"
        assert json.loads(mine["colors_json"]) == ["#ff0000", "#ffaa00", "#00aa55"]

    def test_more_than_three_colors_ok(self):
        store = _repo()
        s = store.save_spool({
            "material": "PLA", "color_name": "Радуга-6",
            "color_kind": "rainbow",
            "colors": ["#ff0000", "#ff8800", "#ffff00", "#00ff00", "#0088ff", "#8800ff"],
        })
        row = store.spool(s["id"])
        assert len(json.loads(row["colors_json"])) == 6

    def test_single_color_is_old_plain_spool(self):
        # «ничего не меняем» — однотонная катушка не получает новых полей
        store = _repo()
        s = store.save_spool({
            "material": "PETG", "color_name": "Чёрный", "color_hex": "#111111",
        })
        row = store.spool(s["id"])
        assert row["colors_json"] == ""
        assert row["color_kind"] == ""
        assert row["rec_settings"] == ""
        assert row["color_hex"] == "#111111"

    def test_one_color_with_kind_falls_back_to_plain(self):
        store = _repo()
        s = store.save_spool({
            "material": "PLA", "color_name": "Один",
            "color_kind": "gradient", "colors": ["#123456"],
        })
        row = store.spool(s["id"])
        assert row["colors_json"] == ""

    def test_colors_without_kind_become_gradient(self):
        store = _repo()
        s = store.save_spool({
            "material": "PLA", "color_name": "Град",
            "colors": ["#000000", "#ffffff"],
        })
        row = store.spool(s["id"])
        assert row["color_kind"] == "gradient"
        assert json.loads(row["colors_json"]) == ["#000000", "#ffffff"]

    def test_unknown_kind_and_dups_cleaned(self):
        store = _repo()
        s = store.save_spool({
            "material": "PLA", "color_name": "Шедевр",
            "color_kind": "Sparkle!", "colors": ["#aa0000", "#aa0000", "#00aa00"],
        })
        row = store.spool(s["id"])
        assert row["color_kind"] == "gradient"  # неведомый вид → список без дублей → градиент
        assert json.loads(row["colors_json"]) == ["#aa0000", "#00aa00"]

    def test_recommendations_saved_and_bounded(self):
        store = _repo()
        s = store.save_spool({
            "material": "PAHT-CF", "color_name": "Инженерка",
            "rec_settings": "сопло 260–300°, стол 70–100°, сушить 8 ч" + " x" * 3000,
        })
        row = store.spool(s["id"])
        assert row["rec_settings"].startswith("сопло 260–300°")
        assert len(row["rec_settings"]) <= 2000


class TestNozzleRangeForAms(unittest.TestCase):
    def test_russian_and_en_ranges(self):
        assert _nozzle_range_from_rec("сопло 210–230°, стол 60–70°") == (210, 230)
        assert _nozzle_range_from_rec("Nozzle temp: 190-220C") == (190, 220)
        assert _nozzle_range_from_rec("экструдер 250 — 280") == (250, 280)

    def test_other_ranges_do_not_leak_into_nozzle(self):
        # Только стол/термокамера/скорость — в AMS ничего не уезжает
        assert _nozzle_range_from_rec("стол 60–70°, сушить 80–100°") is None
        assert _nozzle_range_from_rec("печатать 40–80 мм/с") is None
        assert _nozzle_range_from_rec("") is None

    def test_out_of_realistic_bounds_rejected(self):
        assert _nozzle_range_from_rec("сопло 500–600°") is None
        assert _nozzle_range_from_rec("сопло 90-95") is None


class TestSwatchEverywhereContract(unittest.TestCase):
    """Казнь-контракт М1: каждый экран, где цвет катушки решает «ту ли беру»,
    рисует свотч общим кодом градиента. Добавил новое место показа —
    подключи colors.js или сними выпул М1."""

    FILES = [
        "site/assets/money.js", "site/assets/printer.js",
        "site/assets/products.js", "site/control.html", "site/labels.html",
        "site/spool.html",
    ]

    def _read(self, rel):
        with open(rel, encoding="utf-8") as f:
            return f.read()

    def test_helper_present_and_loaded(self):
        helper = self._read("site/assets/colors.js")
        assert "PFSpoolColor" in helper and "gradient" in helper
        for page in ["site/index.html", "site/control.html",
                     "site/labels.html", "site/spool.html"]:
            assert "assets/colors.js" in self._read(page), page

    def test_every_screen_uses_the_helper(self):
        for rel in self.FILES:
            assert "PFSpoolColor" in self._read(rel), f"{rel}: нет единого свотча"

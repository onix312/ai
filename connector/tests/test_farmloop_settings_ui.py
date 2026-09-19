"""FarmLoop в отдельной вкладке «Конвейер»: контракт на полноту.

История: в 18.6.3 карточка конвейера тихо исчезла из интерфейса при
пересборке настроек (ключи, описания и маршруты остались, а в app.js — ни
одного «farmloop»). Контракт держал полноту группы. В 18.8 группа переехала
из свалки «Настройки» в отдельную вкладку «Конвейер» (решение владельца):
конвейер — это цех, и настраивается он там, где работает, а не среди
тарифов и облака. Тест переехал вместе с группой и держит обратное:

- каждый ключ FarmLoop из DEFAULT_SETTINGS есть в JS-группе (теперь в
  conveyor.js, не в app.js) — иначе снова исчезнет тихо;
- группа рисуется в контейнер cv_settings внутри view-conveyor;
- из панели «Принтеры и Bambu» настроек карточка снята;
- раздел «Печать» держит компактную ссылку на вкладку (живые id статуса);
- схема и маршрут /api/farmloop/settings знают все ключи и вычисленные
  гейты — по ним вкладка объясняет, чего не хватает допусков.

Проверка DOM без браузера невозможна (docs/ТЕСТЫ.md, п. 4), поэтому часть
про разметку — строковый контракт с этой пометкой. Часть про маршрут —
поведенческая: /api/farmloop/settings запускается по-настоящему.
"""
from __future__ import annotations

import pathlib
import re
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys_path_fix = str(ROOT)
if sys_path_fix not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path_fix)
    __import__("sys").path.insert(0, str(ROOT / "connector"))

from connector.printflow.config import DEFAULT_SETTINGS  # noqa: E402
from connector.printflow.settings_schema import get_schema  # noqa: E402

APP_JS = (ROOT / "site" / "assets" / "app.js").read_text(encoding="utf-8")
CONVEYOR_JS = (ROOT / "site" / "assets" / "conveyor.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
PRINT_JS = (ROOT / "site" / "assets" / "print.js").read_text(encoding="utf-8")

FARMLOOP_KEYS = sorted(key for key in DEFAULT_SETTINGS if key.startswith("farmloop_"))


def farmloop_group_body() -> str:
    """Тело JS-группы FARMLOOP из conveyor.js (от объявления до закрывающей ];)."""
    match = re.search(r"const FARMLOOP = \[(.*?)\n\];", CONVEYOR_JS, re.S)
    if not match:
        return ""
    return match.group(1)


class FarmLoopConveyorTabTests(unittest.TestCase):
    """Группа настроек живёт во вкладке «Конвейер», а не в «Настройках»."""

    def test_thirteen_farmloop_keys_exist(self):
        """Ключей конвейера в конфиге по-прежнему 13 — не меньше и не больше."""
        self.assertEqual(len(FARMLOOP_KEYS), 13, FARMLOOP_KEYS)

    def test_every_farmloop_key_is_in_the_settings_group(self):
        """Каждый ключ из конфига есть в группе FARMLOOP (иначе снова исчезнет тихо)."""
        body = farmloop_group_body()
        self.assertTrue(body, "группа FARMLOOP пропала из conveyor.js")
        missing = [key for key in FARMLOOP_KEYS if f"'{key}'" not in body]
        self.assertEqual([], missing, f"ключи без строки в карточке настроек: {missing}")

    def test_group_moved_out_of_app_js(self):
        """Группа не осталась в app.js — «Настройки»FarmLoop не рисуют."""
        self.assertNotIn("const FARMLOOP", APP_JS,
                         "группа FARMLOOP должна жить в conveyor.js, а не в app.js")
        self.assertNotIn("put('set_farmloop'", APP_JS,
                         "старый контейнер настроек не должен рендериться")

    def test_group_is_rendered_into_conveyor_view(self):
        """Группа рисуется в cv_settings внутри view-conveyor."""
        self.assertIn("put('cv_settings', settingGroup(FARMLOOP))", CONVEYOR_JS)
        view = INDEX_HTML.split('id="view-conveyor"', 1)[1].split("</section>\n</section>", 1)[0]
        self.assertIn('id="cv_settings"', view,
                      "контейнер допусков уехал из вкладки «Конвейер»")
        self.assertIn('id="cv_settings"', INDEX_HTML)

    def test_settings_pane_no_longer_has_farmloop_card(self):
        """Из панели «Принтеры и Bambu» настроек карточка снята."""
        printers_pane = INDEX_HTML.split('id="setpane-printers"', 1)[1].split('id="setpane-production"', 1)[0]
        self.assertNotIn('id="set_farmloop"', printers_pane,
                         "карточка FarmLoop должна жить во вкладке «Конвейер»")

    def test_nav_has_conveyor_link(self):
        """В навигации «Цех» есть ссылка на вкладку «Конвейер»."""
        self.assertIn('data-view="conveyor"', INDEX_HTML)
        self.assertIn("Конвейер", INDEX_HTML)

    def test_print_section_keeps_farmloop_status_elements(self):
        """Раздел «Печать» по-прежнему ссылается на живые элементы статуса —
        и это ссылка на вкладку, а не дубль настроек."""
        for element_id in re.findall(r"\$\\?'(pr_farmloop_[a-z]+)'", PRINT_JS):
            self.assertIn(f'id="{element_id}"', INDEX_HTML,
                          f"print.js ждёт #{element_id}, а в разметке его нет")
        # В «Печати» осталась компактная ссылка, а не полная карточка.
        self.assertIn("pr-farmloop-link", INDEX_HTML)
        self.assertIn('href="#conveyor"', INDEX_HTML)

    def test_group_mentions_unattended_clearance(self):
        """Safety-gate подписан как допуск на работу без человека."""
        body = farmloop_group_body()
        self.assertIn("допуск", body)
        self.assertIn("без человека", body)


class FarmLoopSettingsSchemaAndRouteTests(unittest.TestCase):
    """Схема и маршрут знают каждый ключ и вычисленные гейты."""

    def test_schema_describes_every_farmloop_key(self):
        """Схема настроек знает каждый ключ: подпись и описание на месте."""
        spec = get_schema()
        for key in FARMLOOP_KEYS:
            self.assertIn(key, spec, key)
            self.assertTrue(spec[key]["label"], key)


class FarmLoopSettingsRouteTests(unittest.TestCase):
    """Маршрут /api/farmloop/settings отвечает полным набором ключей."""

    def setUp(self):
        from connector.tests.test_phase11 import make_db
        self.db = make_db()
        self.addCleanup(self.db.close)

    def test_settings_route_returns_all_keys(self):
        from connector.printflow.router import register_all, router
        from connector.printflow.api import Api
        register_all()
        api = Api.__new__(Api)
        api.db = self.db
        api.repo = types.SimpleNamespace()
        api.bus = types.SimpleNamespace(publish=lambda *a, **k: None)
        code, body = router.dispatch(api, "GET", "/api/farmloop/settings", query={})
        self.assertEqual(code, 200)
        settings = body["settings"]
        missing = [key for key in FARMLOOP_KEYS if key not in settings]
        self.assertEqual([], missing, f"маршрут не отдаёт ключи: {missing}")
        # вычисленные гейты на месте: по ним вкладка объясняет, чего не хватает
        for key in ("can_auto_next", "can_unattended_series", "blocked_reason",
                    "template_installed", "can_prepare"):
            self.assertIn(key, settings)


if __name__ == "__main__":
    unittest.main()

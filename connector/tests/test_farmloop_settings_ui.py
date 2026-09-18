"""FarmLoop вернулся в настройки: контракт на полноту карточки.

В 18.6.3 карточка конвейера исчезла из интерфейса при пересборке настроек:
ключи, описания и маршруты остались, а в app.js не осталось ни одного
«farmloop» — включить конвейер и выставить паузы стало негде. Тест держит
обратное: каждый ключ FarmLoop из DEFAULT_SETTINGS обязан присутствовать в
JS-группе настроек и в контейнере карточки.

Проверка DOM без браузера невозможна (docs/ТЕСТЫ.md, п. 4 — заглушка стенда
panel-check отвечает цепочкой на любой querySelector), поэтому часть про
разметку — строковый контракт с этой пометкой. Часть про маршрут —
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
INDEX_HTML = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
PRINT_JS = (ROOT / "site" / "assets" / "print.js").read_text(encoding="utf-8")

FARMLOOP_KEYS = sorted(key for key in DEFAULT_SETTINGS if key.startswith("farmloop_"))


def farmloop_group_body() -> str:
    """Тело JS-группы FARMLOOP из app.js (от объявления до закрывающей ];)."""
    match = re.search(r"const FARMLOOP = \[(.*?)\n\];", APP_JS, re.S)
    if not match:
        return ""
    return match.group(1)


class FarmLoopSettingsUiTests(unittest.TestCase):
    def test_thirteen_farmloop_keys_exist(self):
        """Ключей конвейера в конфиге по-прежнему 13 — не меньше и не больше."""
        self.assertEqual(len(FARMLOOP_KEYS), 13, FARMLOOP_KEYS)

    def test_every_farmloop_key_is_in_the_settings_group(self):
        """Каждый ключ из конфига есть в группе FARMLOOP (иначе снова исчезнет тихо)."""
        body = farmloop_group_body()
        self.assertTrue(body, "группа FARMLOOP пропала из app.js")
        missing = [key for key in FARMLOOP_KEYS if f"'{key}'" not in body]
        self.assertEqual([], missing, f"ключи без строки в карточке настроек: {missing}")

    def test_group_is_rendered_into_card_container(self):
        """Группа рисуется в контейнер set_farmloop, контейнер есть в разметке."""
        self.assertIn("put('set_farmloop', settingGroup(FARMLOOP))", APP_JS)
        self.assertIn('id="set_farmloop"', INDEX_HTML)
        # Карточка стоит во вкладке «Принтеры и Bambu»
        printers_pane = INDEX_HTML.split('id="setpane-printers"', 1)[1].split('id="setpane-production"', 1)[0]
        self.assertIn('id="set_farmloop"', printers_pane,
                      "карточка FarmLoop уехала из вкладки «Принтеры и Bambu»")

    def test_group_mentions_unattended_clearance(self):
        """Safety-gate подписан как допуск на работу без человека."""
        body = farmloop_group_body()
        self.assertIn("допуск", body)
        self.assertIn("без человека", body)

    def test_schema_describes_every_farmloop_key(self):
        """Схема настроек знает каждый ключ: подпись и описание на месте."""
        spec = get_schema()
        for key in FARMLOOP_KEYS:
            self.assertIn(key, spec, key)
            self.assertTrue(spec[key]["label"], key)

    def test_print_section_keeps_farmloop_status_elements(self):
        """Раздел «Печать» по-прежнему ссылается на живые элементы статуса."""
        for element_id in re.findall(r"\$\\?'(pr_farmloop_[a-z]+)'", PRINT_JS):
            self.assertIn(f'id="{element_id}"', INDEX_HTML,
                          f"print.js ждёт #{element_id}, а в разметке его нет")


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
        # вычисленные гейты на месте: по ним карточка объясняет, чего не хватает
        for key in ("can_auto_next", "can_unattended_series", "blocked_reason",
                    "template_installed", "can_prepare"):
            self.assertIn(key, settings)


if __name__ == "__main__":
    unittest.main()

"""Оболочка пульта цеха (`ai.printflow.pult`): контракты сборки и окна (18.0.5).

Kotlin тут не компилируется — ни JDK, ни Android SDK в репозитории нет, и
«собирается ли APK» проверяется на ПК владельца (`./scripts/android-build.sh
--pult`). Но ошибки, которые видно только на телефоне, ловятся без компилятора:

* `applicationId`/`versionCode` — часть контракта: по ним APK ставится поверх
  себя, а телефон по versionCode сравнивает сборки. Перепутать пульт с кассой
  значит выпустить «обновление», которое поставится поверх кассы;
* `R.…` ссылки на несуществующий ресурс — сборка упадёт на другой машине, а в
  репозитории пролежит до следующего раза;
* цвета оболочки берутся с тёмной темы страницы (`site/assets/tokens.css`):
  расхождение видно как «две разные программы» на стыке экрана связи и пульта;
* разрешения и компоненты: пульту нужен LAN и экран, а не уведомления кассы.
"""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
ANDROID = ROOT / "android"
PULT = ANDROID / "pult"
KOTLIN = PULT / "src" / "main" / "java" / "ai" / "printflow" / "pult"
RES = PULT / "src" / "main" / "res"
MANIFEST = PULT / "src" / "main" / "AndroidManifest.xml"
GRADLE = PULT / "build.gradle"
SETTINGS = ANDROID / "settings.gradle"
SCRIPT = ROOT / "scripts" / "android-build.sh"
TOKENS = ROOT / "site" / "assets" / "tokens.css"


def gradle_config() -> dict[str, str]:
    text = GRADLE.read_text(encoding="utf-8")
    block = text.split("defaultConfig", 1)[1].split("}", 1)[0]
    block = "\n".join(line.split("//", 1)[0] for line in block.splitlines())
    out: dict[str, str] = {}
    for key in ("versionCode", "versionName", "minSdk", "targetSdk", "applicationId"):
        match = re.search(rf"{key}\s+('?[^'\n]+'?)", block)
        if match:
            out[key] = match.group(1).strip("'")
    return out


def kotlin_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8")
                     for path in sorted(KOTLIN.glob("*.kt")))


def resource_refs() -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {}
    for kind, name in re.findall(r"\bR\.([a-z]+)\.([A-Za-z0-9_]+)", kotlin_text()):
        refs.setdefault(kind, set()).add(name)
    return refs


def declared(kind: str) -> set[str]:
    names: set[str] = set()
    if kind in ("drawable", "layout", "xml"):
        for folder in RES.glob(kind + "*"):
            for file in folder.iterdir():
                names.add(file.stem)
        return names
    for file in (RES / "values").glob("*.xml"):
        text = file.read_text(encoding="utf-8")
        tag = {"string": "string", "color": "color", "dimen": "dimen",
               "style": "style", "integer": "integer", "bool": "bool"}.get(kind)
        if not tag:
            continue
        names |= set(re.findall(rf'<{tag}\s+name="([^"]+)"', text))
    return names


def dark_tokens() -> dict[str, str]:
    """Токены тёмной темы страницы: --имя: #цвет.

    Ищем именно правило (`html[data-theme="dark"] {` в начале строки), а не
    первое упоминание в тексте: в шапке файла оно встречается в комментарии, и
    разбор по нему отдавал палитру светлой темы.
    """
    text = TOKENS.read_text(encoding="utf-8")
    match = re.search(r'^html\[data-theme="dark"\]\s*\{', text, re.M)
    if not match:
        return {}
    block = text[match.end():].split("\n}", 1)[0]
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", block))


class PultBuildMetadataTests(unittest.TestCase):
    def test_version_matches_app_version(self):
        from connector.printflow import APP_VERSION
        self.assertEqual(APP_VERSION, gradle_config().get("versionName"),
                         "versionName оболочки пульта разошёлся с APP_VERSION")

    def test_version_code_is_derived_from_version(self):
        from connector.printflow import APP_VERSION
        major, minor, patch = (int(x) for x in APP_VERSION.split("."))
        expected = major * 10000 + minor * 100 + patch
        self.assertEqual(str(expected), gradle_config().get("versionCode"),
                         "versionCode должен считаться из версии: "
                         "major*10000 + minor*100 + patch")

    def test_package_id_is_the_install_contract(self):
        """Пульт и касса — два разных приложения: перепутать нельзя, иначе
        «обновление пульта» встанет поверх кассы и наоборот."""
        config = gradle_config()
        self.assertEqual("ai.printflow.pult", config.get("applicationId"))
        kassa = (ANDROID / "app" / "build.gradle").read_text(encoding="utf-8")
        self.assertIn("applicationId 'ai.printflow.kassa'", kassa)
        self.assertNotIn(config.get("applicationId"), kassa)

    def test_module_is_part_of_the_gradle_project(self):
        text = SETTINGS.read_text(encoding="utf-8")
        self.assertIn("include ':pult'", text, "модуль пульта не подключён к сборке")
        self.assertIn("include ':app'", text, "касса из сборки пропала")

    def test_min_sdk_matches_kassa(self):
        """Разные minSdk в одном проекте значат, что на старом телефоне встанет
        одно приложение и не встанет другое — без причины."""
        pult = gradle_config()
        kassa_text = (ANDROID / "app" / "build.gradle").read_text(encoding="utf-8")
        block = kassa_text.split("defaultConfig", 1)[1].split("}", 1)[0]
        for key in ("minSdk", "targetSdk"):
            match = re.search(rf"{key}\s+(\d+)", block)
            self.assertIsNotNone(match, f"в build.gradle кассы нет {key}")
            self.assertEqual(match.group(1), pult.get(key),
                             f"{key} пульта и кассы разошлись")


class PultResourceTests(unittest.TestCase):
    def test_kotlin_only_references_existing_resources(self):
        missing: list[str] = []
        for kind, names in sorted(resource_refs().items()):
            have = declared(kind)
            for name in sorted(names):
                if name not in have:
                    missing.append(f"R.{kind}.{name}")
        self.assertEqual([], missing,
                         "в res/ нет этих ресурсов — сборка упадёт на другой машине")

    def test_manifest_references_exist(self):
        text = MANIFEST.read_text(encoding="utf-8")
        for kind, name in re.findall(r"@(drawable|string|xml|style|color)/([A-Za-z0-9_.]+)", text):
            self.assertIn(name, declared(kind), f"в манифесте {kind}/{name} не объявлен")

    def test_colours_match_the_dark_theme_of_the_page(self):
        """Экран связи и страница пульта не должны выглядеть как две программы."""
        tokens = dark_tokens()
        kotlin = kotlin_text()
        expected = {"bg": "BG", "panel": "PANEL", "line": "LINE",
                    "accent": "ACCENT", "text": "TEXT", "muted": "MUTED"}
        colours = (RES / "values" / "colors.xml").read_text(encoding="utf-8")
        for token, const in expected.items():
            value = tokens.get(token)
            self.assertIsNotNone(value, f"в tokens.css нет токена --{token}")
            with self.subTest(token=token):
                self.assertIn(f'const val {const} = "{value}"', kotlin,
                              f"{const} разошёлся с тёмной темой страницы (--{token})")
        for name, token in (("pult_bg", "bg"), ("pult_panel", "panel"),
                            ("pult_line", "line"), ("pult_accent", "accent")):
            with self.subTest(resource=name):
                self.assertIn(f'<color name="{name}">{tokens[token]}</color>', colours)


class PultWindowTests(unittest.TestCase):
    def test_window_opens_the_pult_page_only(self):
        """Окно пульта не должно уметь открывать кассу: там деньги и смены."""
        text = kotlin_text()
        self.assertIn('const val PULT_PATH = "/pult"', text)
        self.assertIn('view.loadUrl("$base$PULT_PATH")', text)
        for forbidden in ("/cashier", "/api/cashier", "/api/sbp", "/api/money",
                          "/api/shift", "/api/payment", "printflow/index.html"):
            self.assertNotIn(forbidden, text, f"пульт не должен трогать {forbidden}")

    def test_no_external_hosts(self):
        """Никаких облаков: единственный адрес — сервер владельца в его сети.

        Схема пространства имён в манифесте (`schemas.android.com`) — это
        разметка XML, а не адрес, по которому приложение ходит, поэтому
        проверяем сам код, а не манифест.
        """
        hosts = set(re.findall(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})", kotlin_text()))
        self.assertEqual(set(), hosts, f"в оболочке появились внешние адреса: {hosts}")
        manifest = MANIFEST.read_text(encoding="utf-8")
        self.assertNotIn("android:host=", manifest, "манифест не должен ходить на свой домен")

    def test_kiosk_and_awake_flags_are_used(self):
        text = kotlin_text()
        self.assertIn("FLAG_KEEP_SCREEN_ON", text, "нет флага «не гасить экран»")
        self.assertIn("SYSTEM_UI_FLAG_IMMERSIVE_STICKY", text, "нет киоск-режима")
        strings = (RES / "values" / "strings.xml").read_text(encoding="utf-8")
        self.assertIn('name="cb_kiosk"', strings)
        self.assertIn('name="cb_awake"', strings)

    def test_permissions_are_lan_only(self):
        """Пульту не нужны уведомления, автозапуск и foreground-сервис кассы."""
        text = MANIFEST.read_text(encoding="utf-8")
        for needed in ("android.permission.INTERNET", "android.permission.WAKE_LOCK"):
            self.assertIn(needed, text, f"в манифесте нет {needed}")
        for forbidden in ("POST_NOTIFICATIONS", "RECEIVE_BOOT_COMPLETED",
                          "FOREGROUND_SERVICE", "CAMERA", "RECORD_AUDIO"):
            self.assertNotIn(forbidden, text, f"пульту не нужно разрешение {forbidden}")
        self.assertIn('android:usesCleartextTraffic="true"', text,
                      "LAN-сервер по http не откроется без cleartext")
        self.assertIn('android:networkSecurityConfig="@xml/network_security_config"', text)


class PultBuildScriptTests(unittest.TestCase):
    def test_script_builds_the_pult_module(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('--pult) TARGET="pult"', text, "у сборки нет режима пульта")
        self.assertIn('"$GRADLE" --console=plain ":$MODULE:assemble${MODE_CAP}"', text)
        self.assertIn('NOZZA-pult', text, "имя файла пульта потерялось")
        self.assertIn('MANIFEST="$OUT/pult.json"', text,
                      "пульт и касса должны писать разные манифесты сборки")
        self.assertIn('"package": package', text, "package обязан идти из выбора модуля")

    def test_environment_check_reports_failure(self):
        """Найденный баг: при отсутствии JDK/SDK функция возвращала успех, и
        скрипт печатал «Окружение готово», падая позже на пустом GRADLE."""
        text = SCRIPT.read_text(encoding="utf-8")
        block = text.split("check_env() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("local ok=0", block,
                      "0 должно значить «готово»: иначе `! check_env` не сработает")
        self.assertNotIn("local ok=1", block)
        self.assertIn('exit 1', text.split("check_env() {", 1)[1])


class PultDocsTests(unittest.TestCase):
    DOC = ROOT / "docs" / "ПУЛЬТ-НА-ТЕЛЕФОНЕ.md"

    def test_phone_guide_exists(self):
        self.assertTrue(self.DOC.exists(), "нет инструкции «пульт на телефоне»")
        text = self.DOC.read_text(encoding="utf-8")
        for phrase in ("Добавить на экран", "./scripts/android-build.sh --pult",
                       "ai.printflow.pult", "Приёмка", "офлайн"):
            self.assertIn(phrase, text, f"в инструкции нет «{phrase}»")

    def test_acceptance_covers_every_stage(self):
        """Сценарии всех этапов: семь этапа 1, два этапа 2 и автономность.

        Порядок важен: сценарии этапа 1 не переписываются, новые добавляются
        в конец — владелец проходит список сверху вниз и видит, что добавилось.
        Номера идут подряд, без пропусков: список читают сверху вниз, и дырка
        в нумерации означает потерянный сценарий.
        """
        text = self.DOC.read_text(encoding="utf-8")
        section = text.split("## Приёмка", 1)[1].split("\n## ", 1)[0]
        numbers = re.findall(r"^(\d{1,2})\.\s", section, re.M)
        self.assertEqual(["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"], numbers,
                         "семь сценариев этапа 1, «Файл на печать» (18.0.6), "
                         "«Слоты AMS и запуск» (18.0.7) и «Автономность» (18.0.8)")
        self.assertIn("**Файл на печать", section)
        self.assertIn("**Слоты AMS и запуск с пульта", section)
        self.assertIn("**Автономность", section)
        self.assertIn("десять проверок", section,
                      "скрипт приёмки идёт по десяти сценам — это должно быть видно")


if __name__ == "__main__":
    unittest.main()

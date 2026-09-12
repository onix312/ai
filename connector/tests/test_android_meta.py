"""Оболочка кассы: метаданные сборки и ссылки на ресурсы (волна 3, 17.0.16).

Kotlin здесь не компилируется — ни Gradle, ни Android SDK в репозитории нет,
и проверка «собирается ли APK» остаётся за `СОБРАТЬ-APP.command`. Но две
группы ошибок ловятся без компилятора и стоят дороже всего именно потому, что
видны только на телефоне:

* `versionCode`/`versionName` разошлись с `APP_VERSION` — оболочка сравнивает
  свой versionCode с `version_code` из `site/app/version.json` и предлагает
  обновиться; при расхождении телефон либо зовёт обновляться вечно, либо
  молча остаётся на старой сборке;
* `R.drawable.…`, `R.string.…`, `R.id.…` указывают на ресурс, которого нет —
  компилятор это поймает, но сборка делается на другой машине, а в репозитории
  ошибка лежит до следующего раза.
"""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
ANDROID = ROOT / "android" / "app"
KOTLIN = ANDROID / "src" / "main" / "java" / "ai" / "printflow" / "kassa"
RES = ANDROID / "src" / "main" / "res"
MANIFEST = ANDROID / "src" / "main" / "AndroidManifest.xml"
GRADLE = ANDROID / "build.gradle"


def gradle_config() -> dict[str, str]:
    text = GRADLE.read_text(encoding="utf-8")
    block = text.split("defaultConfig", 1)[1].split("}", 1)[0]
    # Комментарии в блоке не значения: в пояснении про versionCode стоят те же
    # цифры, и без их обрезки разбор читал строку комментария.
    block = "\n".join(line.split("//", 1)[0] for line in block.splitlines())
    out: dict[str, str] = {}
    for key in ("versionCode", "versionName", "minSdk", "targetSdk", "applicationId"):
        match = re.search(rf"{key}\s+('?[^'\n]+'?)", block)
        if match:
            out[key] = match.group(1).strip("'")
    return out


def resource_refs() -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {}
    for file in sorted(KOTLIN.glob("*.kt")):
        text = file.read_text(encoding="utf-8")
        for kind, name in re.findall(r"\bR\.([a-z]+)\.([A-Za-z0-9_]+)", text):
            refs.setdefault(kind, set()).add(name)
    return refs


def declared(kind: str) -> set[str]:
    """Имена ресурсов этого типа в res/ (файлы для drawable/layout, ключи для values)."""
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
    if kind == "id":
        for file in (RES / "layout").glob("*.xml"):
            text = file.read_text(encoding="utf-8")
            names |= set(re.findall(r'android:id="@\+id/([A-Za-z0-9_]+)"', text))
    return names


class AndroidBuildMetadataTests(unittest.TestCase):
    def test_version_matches_app_version(self):
        from connector.printflow import APP_VERSION
        config = gradle_config()
        self.assertEqual(APP_VERSION, config.get("versionName"),
                         "versionName оболочки разошёлся с APP_VERSION")

    def test_version_code_is_derived_from_version(self):
        from connector.printflow import APP_VERSION
        major, minor, patch = (int(x) for x in APP_VERSION.split("."))
        expected = major * 10000 + minor * 100 + patch
        self.assertEqual(str(expected), gradle_config().get("versionCode"),
                         "versionCode должен считаться из версии: "
                         "major*10000 + minor*100 + patch")

    def test_package_id_is_the_install_contract(self):
        """Менять нельзя: по нему APK ставится поверх себя, а localStorage
        WebView (код кассы, корзина, отметки «не звенеть») переживает обновление."""
        self.assertEqual("ai.printflow.kassa", gradle_config().get("applicationId"))


class AndroidResourceTests(unittest.TestCase):
    def test_kotlin_only_references_existing_resources(self):
        missing: list[str] = []
        for kind, names in sorted(resource_refs().items()):
            have = declared(kind)
            for name in sorted(names):
                if name not in have:
                    missing.append(f"R.{kind}.{name}")
        self.assertEqual([], missing,
                         "в res/ нет этих ресурсов — сборка упадёт на другой машине")

    def test_manifest_icon_and_label_exist(self):
        text = MANIFEST.read_text(encoding="utf-8")
        for ref in re.findall(r'@(drawable|string|xml|style)/([A-Za-z0-9_.]+)', text):
            kind, name = ref
            self.assertIn(name, declared(kind),
                          f"манифест ссылается на @{kind}/{name}, которого нет")

    def test_notification_icon_is_the_app_icon(self):
        """Постоянное уведомление и звонок о платеже — одно приложение."""
        icons = set()
        for file in sorted(KOTLIN.glob("*.kt")):
            icons |= set(re.findall(r"setSmallIcon\(([^)]+)\)", file.read_text(encoding="utf-8")))
        self.assertEqual({"R.drawable.ic_notify"}, icons,
                         f"иконки уведомлений разошлись: {sorted(icons)}")


if __name__ == "__main__":
    unittest.main()

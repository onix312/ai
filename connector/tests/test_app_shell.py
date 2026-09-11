"""Оболочка-приложение (вариант «C», Android): раздача APK и контракт окна.

Тестируется без Android SDK: здесь важны три вещи — что сервер честно говорит
«сборки нет / сборка есть, вот версия», что `.apk` раздаётся с правильным
типом и без кэша (иначе «обновление не приехало» выглядит багом сборки) и что
в проекте оболочки остались те свойства, ради которых она затевалась:
localStorage вместо повторного кода, экран без затемнения, звонок уведомлением,
обновление по versionCode.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
import sys  # noqa: E402  — после ROOT, чтобы пакет импортировался из репозитория
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import app_shell  # noqa: E402
from connector.printflow import static_serve  # noqa: E402


def fake_build(root: pathlib.Path, version: str = "17.0.7", code: int = 170007,
               name: str = "NOZZA-kassa-17.0.7.apk", size: int = 1_800_000,
               manifest: dict | None = "default") -> pathlib.Path:
    root.mkdir(parents=True, exist_ok=True)
    apk = root / name
    apk.write_bytes(b"PK\x03\x04" + b"\0" * max(0, min(size, 4096)))
    os.truncate(apk, size)
    if manifest != "default":
        if manifest is not None:
            (root / "version.json").write_text(json.dumps(manifest), encoding="utf-8")
        return apk
    (root / "version.json").write_text(json.dumps({
        "version": version, "version_code": code, "file": name,
        "package": "ai.printflow.kassa", "built_at": "2026-09-10T15:00:00",
    }), encoding="utf-8")
    return apk


class StatusTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name) / "app"

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_build_is_not_an_error(self):
        out = app_shell.status(self.dir)
        self.assertFalse(out["available"])
        self.assertEqual(out["url"], "")
        self.assertIn("android-build.sh", out["hint"])

    def test_empty_dir(self):
        self.dir.mkdir(parents=True)
        self.assertFalse(app_shell.status(self.dir)["available"])

    def test_build_is_reported(self):
        fake_build(self.dir)
        out = app_shell.status(self.dir)
        self.assertTrue(out["available"])
        self.assertEqual(out["url"], "/app/NOZZA-kassa-17.0.7.apk")
        self.assertEqual(out["version"], "17.0.7")
        self.assertEqual(out["version_code"], 170007)
        self.assertGreater(out["size_mb"], 1.0)

    def test_newest_apk_wins(self):
        fake_build(self.dir, name="NOZZA-kassa-17.0.6.apk")
        old = self.dir / "NOZZA-kassa-17.0.6.apk"
        os.utime(old, (time.time() - 500, time.time() - 500))
        fake_build(self.dir, version="17.0.7", code=170008, name="NOZZA-kassa-17.0.7.apk")
        self.assertEqual(app_shell.status(self.dir)["file"], "NOZZA-kassa-17.0.7.apk")

    def test_broken_manifest_does_not_hide_the_build(self):
        """Версии может не быть — APK всё равно отдаём: «нет version.json» ≠ «нет сборки»."""
        fake_build(self.dir, manifest=None)
        out = app_shell.status(self.dir)
        self.assertTrue(out["available"])
        self.assertEqual(out["version"], "")
        self.assertEqual(out["version_code"], 0)

    def test_garbage_manifest_ignored(self):
        fake_build(self.dir)
        (self.dir / "version.json").write_text("{не json", encoding="utf-8")
        out = app_shell.status(self.dir)
        self.assertTrue(out["available"])
        self.assertEqual(out["version"], "")


class UpdateCheckTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name) / "app"

    def tearDown(self):
        self.tmp.cleanup()

    def test_higher_version_offers_update(self):
        fake_build(self.dir, code=170008)
        out = app_shell.check_version("170007", self.dir)
        self.assertTrue(out["update_available"])
        self.assertEqual(out["url"], "/app/NOZZA-kassa-17.0.7.apk")

    def test_equal_or_lower_never_downgrades(self):
        """Иначе авто-обновление маятником гоняет телефон туда-сюда."""
        fake_build(self.dir, code=170007)
        self.assertFalse(app_shell.check_version("170007", self.dir)["update_available"])
        self.assertFalse(app_shell.check_version(170008, self.dir)["update_available"])

    def test_no_build_no_update(self):
        out = app_shell.check_version("170007", self.dir)
        self.assertFalse(out["available"])
        self.assertFalse(out["update_available"])

    def test_garbage_input_is_zero(self):
        fake_build(self.dir, code=170008)
        out = app_shell.check_version("не число", self.dir)
        self.assertEqual(out["installed"], 0)
        self.assertTrue(out["update_available"])


class StaticTests(unittest.TestCase):
    def test_apk_content_type(self):
        self.assertEqual(static_serve.content_type(pathlib.Path("kassa.apk")),
                         "application/vnd.android.package-archive")

    def test_apk_is_never_cached(self):
        policy = static_serve.cache_policy(pathlib.Path("kassa.apk"), "/app/kassa.apk")
        self.assertEqual(policy, "no-store")

    def test_pinned_assets_stay_immutable(self):
        # регрессия на «случайно выключили кэш всем»
        self.assertIn("immutable", static_serve.cache_policy(
            pathlib.Path("assets/app.js"), "/assets/app.js?v=17.0.7"))


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # маршруты регистрируются импортом модуля — иначе порядок алфавита
        # методов решает, «есть» маршрут или нет
        import connector.printflow.routes_app  # noqa: F401  — регистрация

    def test_route_registered_and_answers(self):
        from connector.printflow.router import router
        self.assertIn("/api/app/android", router.paths())
        code, payload = router.dispatch(None, "GET", "/api/app/android", query={}) or (None, None)
        self.assertEqual(code, 200)
        self.assertIn("available", payload)
        self.assertIn("package", payload)

    def test_installed_param_adds_update_info(self):
        from connector.printflow.router import router
        code, payload = router.dispatch(None, "GET", "/api/app/android",
                                        query={"installed": "1"}) or (None, None)
        self.assertEqual(code, 200)
        self.assertIn("update_available", payload)


class ShellProjectTests(unittest.TestCase):
    """Контракт самого проекта оболочки: то, ради чего она и затевалась."""

    @classmethod
    def setUpClass(cls):
        cls.base = ROOT / "android"
        cls.manifest = (cls.base / "app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")
        cls.gradle = (cls.base / "app/build.gradle").read_text(encoding="utf-8")
        cls.activity = (cls.base / "app/src/main/java/ai/printflow/kassa/MainActivity.kt"
                        ).read_text(encoding="utf-8")
        cls.net = (cls.base / "app/src/main/java/ai/printflow/kassa/Net.kt").read_text(encoding="utf-8")
        cls.ring = (cls.base / "app/src/main/java/ai/printflow/kassa/Ring.kt").read_text(encoding="utf-8")
        cls.page = (ROOT / "site" / "cashier.html").read_text(encoding="utf-8")

    def test_no_third_party_dependencies(self):
        """Принцип «минимум внешних зависимостей» действует и здесь: только плагины."""
        self.assertNotIn("implementation", self.gradle)
        self.assertIn("android.useAndroidX=false",
                      (self.base / "gradle.properties").read_text(encoding="utf-8"))

    def test_package_contract_is_stable(self):
        # смена applicationId = потерянные настройки WebView на всех телефонах
        self.assertIn("applicationId 'ai.printflow.kassa'", self.gradle)
        self.assertIn("versionCode", self.gradle)
        self.assertIn("minSdk 24", self.gradle)

    def test_manifest_declares_what_we_use(self):
        for perm in ("android.permission.INTERNET", "android.permission.VIBRATE",
                     "android.permission.POST_NOTIFICATIONS",
                     "android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS"):
            self.assertIn(perm, self.manifest, perm)
        self.assertIn('android:usesCleartextTraffic="true"', self.manifest)
        self.assertIn("network_security_config", self.manifest)
        self.assertIn('android:configChanges="orientation|screenSize', self.manifest)

    def test_cashier_state_survives_restart(self):
        # localStorage — то, за счёт чего код кассы не спрашивается каждое утро
        self.assertIn("domStorageEnabled = true", self.activity)
        self.assertIn("mediaPlaybackRequiresUserGesture = false", self.activity)
        self.assertIn("FLAG_KEEP_SCREEN_ON", self.activity)
        self.assertIn('addJavascriptInterface(Bridge(), "PfApp")', self.activity)

    def test_page_uses_the_bridge_without_breaking_the_browser(self):
        self.assertIn("window.PfApp", self.page)
        self.assertIn('$("installHint")', self.page)
        self.assertIn("/api/app/android", self.page)
        # внутри приложения плашка не нужна
        self.assertIn("if(window.PfApp)return;", self.page)

    def test_scan_probes_only_its_own_subnet(self):
        self.assertIn("localV4()", self.net)
        self.assertIn("/api/health", self.net)
        self.assertIn("invokeAll", self.net)          # ждём все ответы, а не первый

    def test_ring_is_a_notification_and_vibration(self):
        self.assertIn("createNotificationChannel", self.ring)
        self.assertIn("VibrationEffect.createWaveform", self.ring)
        # вибрация не должна ронять кассу никогда
        self.assertIn("catch (_: Exception)", self.ring)

    def test_kotlin_files_are_structurally_sane(self):
        """Баланс скобок — единственная проверка без Gradle; ловит обрезанный патч."""
        for name, source in (("MainActivity.kt", self.activity), ("Net.kt", self.net),
                             ("Ring.kt", self.ring)):
            self.assertEqual(source.count("{"), source.count("}"), f"{name}: разное число {{ и }}")
            self.assertEqual(source.count("("), source.count(")"), f"{name}: скобки ( )")
            # (проверка ловит и лишнюю ) в однострочном .apply {} — именно так
            # в Ring.kt уже нашлась несбалансированная скобка)
            self.assertTrue(source.rstrip().endswith("}"), f"{name}: файл обрывается")

    def test_build_script_does_not_install_silently(self):
        script = (ROOT / "scripts" / "android-build.sh").read_text(encoding="utf-8")
        for needle in ("--quiet-check", "sdkmanager", "keystore.properties",
                       "unsigned.apk"):
            self.assertIn(needle, script, needle)

    def test_docs_and_gitignore_cover_the_shell(self):
        docs = (ROOT / "docs" / "ANDROID.md").read_text(encoding="utf-8")
        for needle in ("keytool", "Не даёт", "site/app", "--quiet-check",
                       "versionCode", "## Откат"):
            self.assertIn(needle, docs, needle)
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for line in ("android/app/build/", "site/app/*.apk", "android/keystore.properties"):
            self.assertIn(line, ignore, line)
        self.assertTrue((ROOT / "site" / "app" / "README.md").is_file())


class KotlinSourceTests(unittest.TestCase):
    """Статический контракт правок оболочки «адаптация под экраны» (без SDK).

    Первая `:app:assembleDebug` на ПК владельца — приёмка, поэтому здесь ловим
    только то, что можно увидеть без компилятора: панель переехала из пикселей
    в dimens/XML, ротация не пересоздаёт WebView (и не теряет корзину), отладка
    WebView доступна только в DEBUG, versionCode поднят под раздачу.
    """

    @classmethod
    def setUpClass(cls):
        cls.base = ROOT / "android"
        cls.manifest = (cls.base / "app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")
        cls.activity = (cls.base / "app/src/main/java/ai/printflow/kassa/MainActivity.kt"
                        ).read_text(encoding="utf-8")
        cls.gradle = (cls.base / "app/build.gradle").read_text(encoding="utf-8")
        cls.layout = (cls.base / "app/src/main/res/layout/panel_server.xml").read_text(encoding="utf-8")
        cls.strings = (cls.base / "app/src/main/res/values/strings.xml").read_text(encoding="utf-8")
        cls.net = (cls.base / "app/src/main/java/ai/printflow/kassa/Net.kt").read_text(encoding="utf-8")
        cls.ring = (cls.base / "app/src/main/java/ai/printflow/kassa/Ring.kt").read_text(encoding="utf-8")

    def test_panel_layout_is_xml_not_pixels(self):
        # пиксельных setPadding в коде больше нет — отступы живут в dimens (dp)
        self.assertNotIn("setPadding(", self.activity)
        self.assertIn("R.layout.panel_server", self.activity)

    def test_dimens_directories_exist(self):
        for rel in ("values/dimens.xml", "values-land/dimens.xml", "values-sw600dp/dimens.xml"):
            self.assertTrue((self.base / "app/src/main/res" / rel).is_file(), rel)

    def test_control_min_height_is_48dp(self):
        dims = (self.base / "app/src/main/res/values/dimens.xml").read_text(encoding="utf-8")
        self.assertIn('name="panel_control_min_height">48dp', dims)
        layout = (self.base / "app/src/main/res/layout/panel_server.xml").read_text(encoding="utf-8")
        self.assertIn("@dimen/panel_control_min_height", layout)
        self.assertIn("ScrollView", layout)

    def test_webview_debugging_is_debug_only(self):
        self.assertIn("if (BuildConfig.DEBUG) WebView.setWebContentsDebuggingEnabled(true)", self.activity)

    def test_portrait_lock_is_removed(self):
        self.assertNotIn('android:screenOrientation="portrait"', self.manifest)

    def test_rotation_rebuilds_panel_without_recreating_webview(self):
        # configChanges остаётся (WebView не пересоздаётся), панель пересобирается вручную
        self.assertIn('android:configChanges="orientation|screenSize', self.manifest)
        self.assertIn("onConfigurationChanged", self.activity)

    def test_version_was_bumped_for_this_distribution(self):
        m = re.search(r"versionCode (\d+)", self.gradle)
        self.assertIsNotNone(m, "versionCode не найден")
        self.assertGreaterEqual(int(m.group(1)), 170011,
                                "versionCode должен расти при каждой раздаче APK")

    # --- 17.0.12: визуал панели выбора сервера -----------------------------

    def test_accent_button_and_mono_field(self):
        self.assertIn("@drawable/bg_btn_accent", self.layout)
        self.assertIn('android:fontFamily="monospace"', self.layout)

    def test_panel_shows_version_for_support(self):
        self.assertIn("panel_version_fmt", self.strings)
        self.assertIn("BuildConfig.VERSION_NAME", self.activity)

    def test_scan_progress_is_live(self):
        self.assertIn("scan_found", self.strings)
        self.assertIn("onFound", self.net)
        self.assertIn("scanNote", self.activity)

    def test_notification_icon_is_branded(self):
        self.assertTrue((self.base / "app/src/main/res/drawable/ic_notify.xml").is_file())
        self.assertIn("R.drawable.ic_notify", self.ring)

    def test_version_was_bumped_again_for_visual_round(self):
        m = re.search(r"versionCode (\d+)", self.gradle)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 170012,
                                "визуальный раунд — своя раздача APK (17.0.12)")


if __name__ == "__main__":
    unittest.main()

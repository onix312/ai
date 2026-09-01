"""Слои HTTP после разборки `api.py` (Н18).

`api.py` вырос до 4800 строк, потому что в нём смешались транспорт, раздача
файлов и логика маршрутов. Здесь проверяются вынесенные слои: утилиты без
состояния, политика кэша и то, что примесь загрузок действительно подключена
к обработчику.

Отдельно зафиксирован каталог загрузок: `save_upload` обязан читать
`config.UPLOAD_DIR` при вызове, иначе подмена каталога в тестах не доходит
до функции и файлы уезжают в боевую папку.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import static_serve  # noqa: E402
from connector.printflow.api import Handler  # noqa: E402
from connector.printflow.http_helpers import (  # noqa: E402
    CLIENT_DISCONNECT_ERRORS, MAX_JSON, MAX_UPLOAD, _form_bool, _upload_filename,
    parse_multipart, rate_bucket, request_length, request_origin_allowed,
    safe_file, save_upload, uploads_root)
from connector.printflow.uploads import UploadMixin  # noqa: E402


class SafeFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        (self.root / "inner").mkdir()
        (self.root / "inner" / "a.txt").write_text("x", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_path_inside_root_is_allowed(self):
        self.assertEqual(self.root / "inner" / "a.txt",
                         safe_file(self.root, "inner/a.txt"))

    def test_parent_directory_is_blocked(self):
        self.assertIsNone(safe_file(self.root, "../outside.txt"))

    def test_absolute_path_outside_is_blocked(self):
        self.assertIsNone(safe_file(self.root, "/etc/passwd"))


class RequestLengthTests(unittest.TestCase):
    def test_returns_size_and_flag(self):
        self.assertEqual((100, False), request_length("100", 1000))
        self.assertEqual((2000, True), request_length("2000", 1000))

    def test_empty_header_means_zero(self):
        self.assertEqual((0, False), request_length(None, 1000))

    def test_garbage_header_is_rejected(self):
        with self.assertRaises(ValueError):
            request_length("abc", 1000)

    def test_negative_header_is_rejected(self):
        with self.assertRaises(ValueError):
            request_length("-5", 1000)

    def test_limits_are_sane(self):
        self.assertGreater(MAX_UPLOAD, MAX_JSON,
                           "модели крупнее JSON-тела")


class OriginTests(unittest.TestCase):
    def test_api_client_without_origin_is_allowed(self):
        self.assertTrue(request_origin_allowed(None, "host"))

    def test_same_host_is_allowed(self):
        self.assertTrue(request_origin_allowed("http://h:8080/a", "h:8080"))

    def test_foreign_page_is_rejected(self):
        """Иначе чужая страница могла бы управлять принтером."""
        self.assertFalse(request_origin_allowed("http://evil.test/a", "h:8080"))

    def test_subdomain_is_rejected(self):
        self.assertFalse(request_origin_allowed("http://a.h:8080/", "h:8080"))

    def test_missing_host_is_rejected(self):
        self.assertFalse(request_origin_allowed("http://h:8080/", ""))


class RateBucketTests(unittest.TestCase):
    def test_public_buckets(self):
        self.assertEqual("public_order", rate_bucket("/api/public/order"))
        self.assertEqual("public_catalog", rate_bucket("/api/public/catalog"))
        self.assertEqual("public_track", rate_bucket("/api/track"))
        self.assertEqual("login", rate_bucket("/api/cloud/login"))
        self.assertEqual("upload", rate_bucket("/api/upload"))

    def test_internal_routes_are_not_limited(self):
        self.assertEqual("", rate_bucket("/api/orders"))


class MultipartTests(unittest.TestCase):
    BODY = (b"--BND\r\nContent-Disposition: form-data; name=\"priority\"\r\n\r\n7\r\n"
            b"--BND\r\nContent-Disposition: form-data; name=\"file\";"
            b" filename=\"a.3mf\"\r\nContent-Type: application/octet-stream\r\n"
            b"\r\nDATA\r\n--BND--\r\n")

    def test_fields_and_file_are_split(self):
        fields, upload = parse_multipart(self.BODY, "BND")
        self.assertEqual({"priority": "7"}, fields)
        self.assertEqual(("a.3mf", b"DATA"), upload)

    def test_body_without_file(self):
        fields, upload = parse_multipart(
            b"--B\r\nContent-Disposition: form-data; name=\"x\"\r\n\r\n1\r\n--B--\r\n",
            "B")
        self.assertEqual({"x": "1"}, fields)
        self.assertIsNone(upload)

    def test_windows_filename_is_normalized(self):
        self.assertEqual("model.3mf", _upload_filename("C:\\temp\\model.3mf"))

    def test_bad_filenames_are_rejected(self):
        for bad in ("", ".", "..", "a\x00b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                _upload_filename(bad)


class FormBoolTests(unittest.TestCase):
    def test_truthy_words(self):
        for value in ("1", "true", "YES", "on", "да"):
            self.assertTrue(_form_bool(value), value)

    def test_empty_uses_default(self):
        self.assertFalse(_form_bool(""))
        self.assertTrue(_form_bool(None, True))

    def test_other_values_are_false(self):
        self.assertFalse(_form_bool("нет"))


class SaveUploadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name) / "uploads"

    def tearDown(self):
        self._tmp.cleanup()

    def test_saves_and_reports_creation(self):
        with patch("connector.printflow.config.UPLOAD_DIR", self.dir):
            name, path, created = save_upload("part.3mf", b"first")
        self.assertEqual("part.3mf", name)
        self.assertTrue(created)
        self.assertEqual(b"first", path.read_bytes())

    def test_same_name_different_data_gets_suffix(self):
        """Задание в очереди не должно печатать содержимое чужой загрузки."""
        with patch("connector.printflow.config.UPLOAD_DIR", self.dir):
            first, _, _ = save_upload("part.3mf", b"first")
            second, _, created = save_upload("part.3mf", b"second")
        self.assertNotEqual(first, second)
        self.assertTrue(created)
        self.assertEqual(b"first", (self.dir / first).read_bytes())
        self.assertEqual(b"second", (self.dir / second).read_bytes())

    def test_same_name_same_data_is_not_rewritten(self):
        with patch("connector.printflow.config.UPLOAD_DIR", self.dir):
            first, _, _ = save_upload("part.3mf", b"first")
            second, _, created = save_upload("part.3mf", b"first")
        self.assertEqual(first, second)
        self.assertFalse(created)

    def test_directory_is_read_at_call_time(self):
        """Подмена каталога обязана доходить до функции."""
        with patch("connector.printflow.config.UPLOAD_DIR", self.dir):
            self.assertEqual(self.dir, uploads_root())
        self.assertNotEqual(self.dir, uploads_root())


class CachePolicyTests(unittest.TestCase):
    def test_html_is_never_cached(self):
        self.assertEqual("no-store", static_serve.cache_policy(pathlib.Path("index.html")))

    def test_service_worker_is_never_cached(self):
        """Иначе обновление оболочки не приедет."""
        self.assertEqual("no-store",
                         static_serve.cache_policy(pathlib.Path("sw.js"), "/sw.js?v=1"))

    def test_pinned_asset_is_immutable(self):
        self.assertEqual("public, max-age=31536000, immutable",
                         static_serve.cache_policy(pathlib.Path("app.js"),
                                                   "/assets/app.js?v=15.0.0"))

    def test_unpinned_asset_keeps_short_cache(self):
        self.assertEqual("public, max-age=3600",
                         static_serve.cache_policy(pathlib.Path("app.js"),
                                                   "/assets/app.js"))


class ContentTypeTests(unittest.TestCase):
    def test_text_carries_charset(self):
        self.assertIn("charset=utf-8", static_serve.content_type(pathlib.Path("a.html")))
        self.assertIn("charset=utf-8", static_serve.content_type(pathlib.Path("a.js")))

    def test_webmanifest_is_known(self):
        """Без правильного типа браузер не зарегистрирует PWA."""
        self.assertIn("application/manifest+json",
                      static_serve.content_type(pathlib.Path("m.webmanifest")))

    def test_binary_has_no_charset(self):
        self.assertNotIn("charset", static_serve.content_type(pathlib.Path("a.png")))


class ResolveTargetTests(unittest.TestCase):
    def test_root_serves_panel(self):
        self.assertEqual("index.html", static_serve.resolve_target("/").name)

    def test_short_url_finds_html(self):
        self.assertEqual("m.html", static_serve.resolve_target("/m").name)

    def test_directory_escape_is_blocked(self):
        self.assertIsNone(static_serve.resolve_target("/../etc/passwd"))

    def test_missing_file_is_none(self):
        self.assertIsNone(static_serve.resolve_target("/nope.html"))

    def test_etag_is_stable(self):
        target = static_serve.resolve_target("/")
        self.assertEqual(static_serve.etag_for(target), static_serve.etag_for(target))


class HandlerWiringTests(unittest.TestCase):
    """Разборка не должна потерять ни одного метода обработчика."""

    def test_upload_mixin_is_attached(self):
        self.assertTrue(issubclass(Handler, UploadMixin))

    def test_upload_methods_survived_the_split(self):
        for name in ("serve_upload", "handle_job_upload", "handle_estimate_upload",
                     "handle_upload", "_multipart_upload"):
            self.assertTrue(hasattr(Handler, name), f"потерян {name}")

    def test_transport_methods_survived_the_split(self):
        for name in ("do_GET", "do_POST", "send_json", "serve_static", "check_origin"):
            self.assertTrue(hasattr(Handler, name), f"потерян {name}")

    def test_client_disconnect_errors_cover_browser_closes(self):
        self.assertIn(ConnectionResetError, CLIENT_DISCONNECT_ERRORS)
        self.assertIn(ConnectionAbortedError, CLIENT_DISCONNECT_ERRORS)


if __name__ == "__main__":
    unittest.main()

"""Тесты раздачи сайта: короткие адреса, типы файлов, защита каталога.

Проверяются без сети и без базы — только логика Handler.serve_static.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.api import Handler  # noqa: E402


class Recorder:
    """Подставной сокет: запоминает всё, что сервер собрался отправить."""

    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    def flush(self) -> None:
        pass

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)


def serve(path: str) -> tuple[int, dict, bytes]:
    """Выполнить serve_static и вернуть (код, заголовки, тело)."""
    handler = Handler.__new__(Handler)
    handler.request_version = "HTTP/1.1"
    handler.command = "GET"
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.server = SimpleNamespace(flags=[])
    handler.close_connection = False
    handler.wfile = Recorder()
    handler.api = SimpleNamespace(last_host="")

    state = {"code": 0, "headers": {}}
    handler.send_response = lambda code, *_: state.update(code=code)
    handler.send_header = lambda key, value: state["headers"].__setitem__(key, value)
    handler.end_headers = lambda: None
    handler.send_error = lambda code, *_a, **_kw: state.update(code=code)

    handler.serve_static(path)
    return state["code"], state["headers"], handler.wfile.body


class TestShortUrls(unittest.TestCase):
    """Адрес без .html удобно диктовать вслух и печатать на ценнике."""

    def test_root_serves_panel(self):
        code, _, body = serve("/")
        self.assertEqual(code, 200)
        self.assertIn(b"PrintFlow", body)

    def test_short_url_finds_html(self):
        for short, marker in (("/m", b"<"), ("/order", b"<"), ("/track", b"<")):
            with self.subTest(short=short):
                code, _, body = serve(short)
                self.assertEqual(code, 200, f"{short} должен открываться")
                self.assertIn(marker, body)

    def test_short_and_long_url_are_the_same_page(self):
        self.assertEqual(serve("/m")[2], serve("/m.html")[2])

    def test_unknown_path_is_404(self):
        code, _, _ = serve("/такой-страницы-нет")
        self.assertEqual(code, 404)

    def test_short_url_does_not_expose_other_files(self):
        # /connector — это каталог вне site/, .html к нему не подставляется
        code, _, _ = serve("/connector")
        self.assertEqual(code, 404)


class TestContentTypes(unittest.TestCase):
    def test_manifest_has_its_own_type(self):
        code, headers, body = serve("/manifest.webmanifest")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/manifest+json; charset=utf-8")
        self.assertIn(b"PrintFlow", body)

    def test_service_worker_is_javascript_and_not_cached(self):
        code, headers, body = serve("/sw.js")
        self.assertEqual(code, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b"printflow-shell", body)

    def test_service_worker_never_caches_api(self):
        _, _, body = serve("/sw.js")
        self.assertIn(b"/api/", body)
        self.assertIn(b"startsWith('/api/')", body)

    def test_pages_are_not_cached_but_images_are(self):
        self.assertEqual(serve("/index.html")[1]["Cache-Control"], "no-store")
        self.assertEqual(serve("/assets/brand/favicon.svg")[1]["Cache-Control"], "max-age=3600")


class TestDirectoryEscape(unittest.TestCase):
    def test_parent_directory_is_blocked(self):
        for path in ("/../connector/printflow/config.py",
                     "/../../etc/passwd",
                     "/assets/../../connector/requirements.txt"):
            with self.subTest(path=path):
                code, _, _ = serve(path)
                self.assertIn(code, (403, 404), "выход за пределы site/ должен блокироваться")


if __name__ == "__main__":
    unittest.main()

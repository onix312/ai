"""Загрузка модели в библиотеку: POST /api/library/upload (18.8).

Слайсер Stage 1 режет только STL, а до этого маршрута у панели не было
способа привести модель с компьютера — загрузка 3MF/G-code в очередь её
отклоняла. Здесь держим контракт байтового маршрута: что принимаем, куда
кладем, как дедуплицируем по SHA и что отдаём карточке слайсера.
"""
from __future__ import annotations

import io
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.api import Handler  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.library import FileLibrary  # noqa: E402


FAKE_STL = b"solid part\nfacet normal 0 0 1\nendsolid part\n"
FAKE_STL_B = b"solid other\nfacet normal 1 0 0\nendsolid other\n"


def _multipart(boundary: str, parts: list[tuple[str, bytes]]) -> bytes:
    """Части (имя, байты) одним multipart-телом."""
    body = b""
    for name, data in parts:
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        body += data
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    return body


def _field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode("utf-8")


def _handler(body: bytes, db) -> tuple:
    """Обработчик как в доке тестов очередей: только шапка, тело и ответ."""
    boundary = "----printflow-library-test"
    handler = Handler.__new__(Handler)
    handler.headers = {
        "Content-Length": str(len(body)),
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    handler.rfile = io.BytesIO(body)
    handler.api = SimpleNamespace(db=db, manager=None)
    sent = {}
    handler.send_json = lambda code, payload: sent.update(code=code, payload=payload)
    return handler, boundary, sent


def _transport(path: str, body: bytes, headers: dict | None = None,
               client_address: tuple = ("192.168.1.66", 51234)):
    """Обработчик для транспортных веток do_POST (18.12.1).

    Ранние ответы (403 по Origin, 429 по лимиту, 400 от multipart) живут до
    бизнес-логики, поэтому `api` им не нужен — нужны путь, заголовки, тело и
    адрес сокета: по нему считается ключ ограничения частоты.
    """
    handler = Handler.__new__(Handler)
    handler.headers = {"Content-Length": str(len(body)), **(headers or {})}
    handler.rfile = io.BytesIO(body)
    handler.path = path
    handler.client_address = client_address
    handler.close_connection = False
    sent = {}
    handler.send_json = lambda code, payload: sent.update(code=code, payload=payload)
    return handler, sent


class LibraryUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.uploads = self.root / "uploads"
        self.library_dir = self.root / "library"
        self.db = Database(self.root / "printflow.sqlite3")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _run(self, body: bytes) -> tuple:
        handler, _boundary, sent = _handler(body, self.db)
        with patch("connector.printflow.library.UPLOAD_DIR", self.uploads), \
             patch("connector.printflow.library.LIBRARY_DIR", self.library_dir):
            handler.handle_library_upload()
        return sent

    def test_stl_goes_to_library_and_uploads_copy(self):
        boundary = "----printflow-library-test"
        body = _multipart(boundary, [("part.stl", FAKE_STL)])
        sent = self._run(body)
        self.assertEqual(sent["code"], 200)
        self.assertTrue(sent["payload"]["ok"])
        rec = sent["payload"]["library"]
        self.assertEqual(rec["name"], "part.stl")
        self.assertEqual(rec["kind"], "stl")
        self.assertEqual(rec["source"], "upload")
        self.assertEqual(rec["size"], len(FAKE_STL))
        # SHA-файл в хранилище библиотеки.
        sha_file = pathlib.Path(rec["path"])
        self.assertTrue(sha_file.is_file())
        self.assertEqual(sha_file.read_bytes(), FAKE_STL)
        # Копия с исходным именем в uploads — по ней нарезает слайсер.
        self.assertTrue((self.uploads / "part.stl").is_file())
        # Разрешение по id работает (тот же путь, что в _resolve_input).
        self.assertEqual(FileLibrary(self.db).resolve(rec["id"]).read_bytes(), FAKE_STL)

    def test_obj_accepted_with_stl_kind(self):
        boundary = "----printflow-library-test"
        body = _multipart(boundary, [("body.obj", b"v 0 0 0\n")])
        sent = self._run(body)
        self.assertEqual(sent["code"], 200)
        self.assertEqual(sent["payload"]["library"]["kind"], "stl")

    def test_wrong_extension_rejected(self):
        boundary = "----printflow-library-test"
        body = _multipart(boundary, [("print.gcode", b";TIME:60\n")])
        sent = self._run(body)
        self.assertEqual(sent["code"], 400)
        self.assertIn("STL или OBJ", sent["payload"]["error"])
        self.assertEqual(FileLibrary(self.db).list(), [])

    def test_missing_file_rejected(self):
        boundary = "----printflow-library-test"
        body = _field(boundary, "note", "без файла")
        sent = self._run(body)
        self.assertEqual(sent["code"], 400)
        self.assertEqual(sent["payload"]["error"], "Файл не передан")

    def test_empty_file_rejected(self):
        boundary = "----printflow-library-test"
        body = _multipart(boundary, [("empty.stl", b"")])
        sent = self._run(body)
        self.assertEqual(sent["code"], 400)
        self.assertEqual(sent["payload"]["error"], "Файл пустой")

    def test_same_sha_is_not_duplicated(self):
        boundary = "----printflow-library-test"
        first = self._run(_multipart(boundary, [("part.stl", FAKE_STL)]))
        # Та же модель, другое имя — не вторая строка, а обновление записи.
        second = self._run(_multipart(boundary, [("part-копия.stl", FAKE_STL)]))
        self.assertEqual(first["code"], 200)
        self.assertEqual(second["code"], 200)
        self.assertEqual(second["payload"]["library"]["id"],
                         first["payload"]["library"]["id"])
        self.assertEqual(len(FileLibrary(self.db).list()), 1)
        self.assertEqual(FileLibrary(self.db).list()[0]["name"], "part-копия.stl")

    def test_note_and_source_fields_are_stored(self):
        boundary = "----printflow-library-test"
        body = _field(boundary, "note", "модель клиента")
        body += _field(boundary, "source", "print-panel")
        body += (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="note.stl"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + FAKE_STL_B + b"\r\n"
        body += f"--{boundary}--\r\n".encode("utf-8")
        sent = self._run(body)
        self.assertEqual(sent["code"], 200)
        rec = sent["payload"]["library"]
        self.assertEqual(rec["note"], "модель клиента")
        self.assertEqual(rec["source"], "print-panel")


class EarlyAnswerDrainTests(unittest.TestCase):
    """18.12.1: ранний ответ обязан дочитать тело запроса.

    Баг владельца: загрузка модели в конвейере показывала «Нет связи с
    коннектором PrintFlow» вместо причины. Ответ, отправленный НЕ дочитав
    тело, рвёт соединение на середине загрузки — браузер ещё льёт байты,
    сервер уже закрыл канал, `fetch` падает сетевой ошибкой.
    """

    BOUNDARY = "----printflow-library-test"
    BODY = _multipart("----printflow-library-test", [("part.stl", FAKE_STL)])

    def test_foreign_origin_answer_is_sent_after_draining_the_body(self):
        handler, sent = _transport(
            "/api/library/upload", self.BODY,
            {"Content-Type": f"multipart/form-data; boundary={self.BOUNDARY}",
             "Origin": "http://evil.example", "Host": "192.168.1.50:8765"})
        handler.do_POST()
        self.assertEqual(403, sent["code"])
        self.assertIn("посторонний источник", sent["payload"]["error"])
        self.assertEqual(b"", handler.rfile.read(),
                         "тело не дочитано: браузер получит обрыв соединения вместо 403")
        self.assertTrue(handler.close_connection)

    def test_rate_limit_answer_is_sent_after_draining_the_body(self):
        handler, sent = _transport(
            "/api/library/upload", self.BODY,
            {"Content-Type": f"multipart/form-data; boundary={self.BOUNDARY}"})
        denied = (False, {"bucket": "upload", "limit": 30, "window": 600,
                          "retry_after": 42,
                          "error": "Слишком много запросов. Повторите через 42 с."})
        with patch("connector.printflow.http_handler.limiter.check", return_value=denied) as check:
            handler.do_POST()
        self.assertEqual(429, sent["code"])
        self.assertEqual(42, sent["payload"]["retry_after"])
        self.assertEqual(b"", handler.rfile.read(),
                         "429 без дочитывания тела рвёт загрузку модели")
        # Ключ клиента — адрес сокета, а не общий "unknown" на всю панель.
        self.assertEqual(("upload", "192.168.1.66"), check.call_args[0])

    def test_own_origin_is_not_rejected(self):
        """Контракт рядом: свой Origin проходит дальше по веткам загрузки."""
        handler, _sent = _transport(
            "/api/library/upload", self.BODY,
            {"Content-Type": f"multipart/form-data; boundary={self.BOUNDARY}",
             "Origin": "http://192.168.1.50:8765", "Host": "192.168.1.50:8765"})
        self.assertTrue(handler.check_origin())

    def test_multipart_value_error_drains_body_and_closes_connection(self):
        """«Файл слишком большой» / «нет boundary» — 400 доходит до браузера."""
        handler, sent = _transport(
            "/api/library/upload", self.BODY,
            {"Content-Type": "multipart/form-data"})   # boundary отсутствует
        handler.api = SimpleNamespace(db=None, manager=None)
        handler.do_POST()
        self.assertEqual(400, sent["code"])
        self.assertIn("multipart/form-data", sent["payload"]["error"])
        self.assertEqual(b"", handler.rfile.read())
        self.assertTrue(handler.close_connection,
                        "после 400 от multipart тело может быть прочитано наполовину —"
                        " на keep-alive следующий запрос разобрать уже нельзя")

    def test_oversized_body_is_not_swallowed(self):
        """Тело больше лимита не глотаем: только честное закрытие соединения.

        Предел передаётся явно: значение по умолчанию в сигнатуре
        ``_drain_body(limit=MAX_UPLOAD)`` вычисляется один раз при импорте,
        поэтому подмена константы модуля на него не действует.
        """
        body = b"x" * 4096
        handler, sent = _transport(
            "/api/library/upload", body,
            {"Content-Type": f"multipart/form-data; boundary={self.BOUNDARY}",
             "Origin": "http://evil.example", "Host": "192.168.1.50:8765"})
        handler._drain_body(limit=1024)
        handler.send_json(403, {"error": "Запрос отклонён: посторонний источник"})
        self.assertEqual(403, sent["code"])
        self.assertTrue(handler.close_connection)
        self.assertEqual(body, handler.rfile.read(),
                         "сотни мегабайт мусора не должны читаться в память")

    def test_oversized_upload_reaches_the_browser_as_400(self):
        """«Файл слишком большой» — 400 с причиной, а не сетевая ошибка."""
        body = b"x" * 4096
        handler, sent = _transport(
            "/api/library/upload", body,
            {"Content-Type": f"multipart/form-data; boundary={self.BOUNDARY}"})
        handler.api = SimpleNamespace(db=None, manager=None)
        with patch("connector.printflow.uploads.MAX_UPLOAD", 1024):
            handler.do_POST()
        self.assertEqual(400, sent["code"])
        self.assertIn("слишком большой", sent["payload"]["error"].lower())
        self.assertTrue(handler.close_connection)


if __name__ == "__main__":
    unittest.main()

"""Входящий заказ с файлом: POST /api/order/intake/upload (18.13).

Боль, которую закрывает маршрут: сообщение клиента приходит вместе с моделью,
а разобрать их вместе было нечем — текст принимал `/api/order/intake/preview`,
файлы принимали `/api/jobs/upload` и `/api/estimate/upload`, и файл из
переписки мастер переносил в заказ руками.

Здесь держится контракт байтового маршрута: что принимаем, куда кладём, чем
дополняем черновик и чего не делаем никогда (не сохраняем заказ, не зовём
модель, если помощник выключен, не перетираем разобранные из текста поля).
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

TEXT = ("Нужно 20 шт адресник PETG чёрный для Марии, "
        "телефон +7 999 123-45-67, бюджет 3600 руб")
FAKE_3MF = b"PK\x03\x04fake-3mf-body"


def _multipart(boundary: str, fields: dict[str, str],
               file: tuple[str, bytes] | None) -> bytes:
    """Текстовые поля и один файл одним multipart-телом."""
    body = b""
    for name, value in fields.items():
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                 f"{value}\r\n").encode("utf-8")
    if file:
        name, data = file
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    return body


class IntakeUploadTests(unittest.TestCase):
    BOUNDARY = "----printflow-intake-test"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.uploads = self.root / "uploads"
        self.db = Database(self.root / "printflow.sqlite3")
        self.db.upsert("customers", {
            "id": "cus-maria", "name": "Мария", "phone": "+79991234567",
            "created_at": "2026-09-01T10:00:00"})
        self.db.upsert("nomenclature", {
            "id": "nom-tag", "code": "000123", "sku": "TAG-PETG",
            "name": "Адресник", "kind": "product", "niche_id": "pets",
            "material": "PETG", "grams": 18, "hours": 0.6,
            "file": "tag-old.3mf", "archived": 0})

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _run(self, fields: dict[str, str], file: tuple[str, bytes] | None,
             assistant_enabled: bool = False):
        body = _multipart(self.BOUNDARY, fields, file)
        handler = Handler.__new__(Handler)
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": f"multipart/form-data; boundary={self.BOUNDARY}",
        }
        handler.rfile = io.BytesIO(body)
        handler.api = SimpleNamespace(db=self.db, manager=None)
        sent: dict = {}
        handler.send_json = lambda code, payload: sent.update(code=code, payload=payload)
        self.db.set_settings({"assistant_enabled": assistant_enabled})
        with patch("connector.printflow.config.UPLOAD_DIR", self.uploads), \
             patch("connector.printflow.assistant._get_json",
                   return_value=(False, None, "рантайм недоступен")):
            handler.handle_intake_upload()
        return sent

    def test_text_and_file_give_one_draft(self):
        sent = self._run({"text": TEXT, "channel": "telegram"}, ("adresnik.3mf", FAKE_3MF))
        self.assertEqual(200, sent["code"])
        payload = sent["payload"]
        self.assertTrue(payload["ok"])
        draft = payload["draft"]
        # текст разобран детерминированно, как и в preview
        self.assertEqual("Адресник", draft["product"])
        self.assertEqual(20, draft["qty"])
        self.assertEqual(3600, draft["price"])
        self.assertEqual("PETG", draft["material"])
        self.assertEqual("+79991234567", draft["phone"])
        self.assertEqual("cus-maria", draft["customer_id"])
        # файл подставлен в тот же черновик
        self.assertEqual("adresnik.3mf", draft["file"])
        self.assertEqual("adresnik.3mf", payload["file"]["name"])
        self.assertEqual(len(FAKE_3MF), payload["file"]["size"])
        self.assertEqual("3mf", payload["file"]["extension"])

    def test_file_does_not_overwrite_parsed_text(self):
        """Оценка файла дополняет пустые поля, а не перетирает разобранные."""
        estimate = {"total_grams": 999, "total_minutes": 999,
                    "material": "PLA", "color": "Белый"}
        with patch("connector.printflow.estimate.estimate_file", return_value=estimate):
            sent = self._run({"text": TEXT, "channel": "telegram"},
                             ("adresnik.3mf", FAKE_3MF))
        draft = sent["payload"]["draft"]
        self.assertEqual("PETG", draft["material"], "материал из текста перезаписан файлом")
        self.assertEqual(18, draft["grams"], "норматив товара перезаписан файлом")
        self.assertEqual(999, estimate["total_grams"])

    def test_empty_fields_are_filled_from_file_estimate(self):
        estimate = {"total_grams": 42, "total_minutes": 90,
                    "material": "PLA", "color": "Белый"}
        with patch("connector.printflow.estimate.estimate_file", return_value=estimate):
            sent = self._run({"text": "нужен один брелок", "channel": "avito"},
                             ("brelok.gcode", b"; gcode"))
        draft = sent["payload"]["draft"]
        self.assertEqual(42, draft["grams"])
        self.assertEqual(1.5, draft["hours"])

    def test_file_without_text_still_gives_draft(self):
        sent = self._run({"text": "", "channel": "telegram"}, ("model.3mf", FAKE_3MF))
        self.assertEqual(200, sent["code"])
        draft = sent["payload"]["draft"]
        self.assertEqual("Изделие из файла", draft["product"])
        self.assertEqual("model.3mf", draft["file"])
        self.assertEqual("telegram", draft["channel"])

    def test_text_without_file_behaves_like_preview(self):
        sent = self._run({"text": TEXT, "channel": "telegram"}, None)
        self.assertEqual(200, sent["code"])
        self.assertIsNone(sent["payload"]["file"])
        self.assertEqual("Адресник", sent["payload"]["draft"]["product"])

    def test_photo_is_refused_with_reason(self):
        sent = self._run({"text": TEXT}, ("photo.jpg", b"\xff\xd8\xff"))
        self.assertEqual(400, sent["code"])
        self.assertIn("3MF", sent["payload"]["error"])

    def test_empty_request_is_refused(self):
        sent = self._run({"text": ""}, None)
        self.assertEqual(400, sent["code"])

    def test_empty_file_is_refused(self):
        sent = self._run({"text": TEXT}, ("empty.3mf", b""))
        self.assertEqual(400, sent["code"])
        self.assertIn("пустой", sent["payload"]["error"])

    def test_order_is_not_saved_by_route(self):
        before = self.db.one("SELECT COUNT(*) n FROM orders")["n"]
        self._run({"text": TEXT, "channel": "telegram"}, ("adresnik.3mf", FAKE_3MF))
        self.assertEqual(before, self.db.one("SELECT COUNT(*) n FROM orders")["n"])

    def test_disabled_assistant_is_not_called(self):
        with patch("connector.printflow.assistant._post_json") as post:
            with patch("connector.printflow.config.UPLOAD_DIR", self.uploads):
                body = _multipart(self.BOUNDARY, {"text": TEXT}, None)
                handler = Handler.__new__(Handler)
                handler.headers = {
                    "Content-Length": str(len(body)),
                    "Content-Type": f"multipart/form-data; boundary={self.BOUNDARY}"}
                handler.rfile = io.BytesIO(body)
                handler.api = SimpleNamespace(db=self.db, manager=None)
                handler.send_json = lambda code, payload: None
                self.db.set_settings({"assistant_enabled": False})
                handler.handle_intake_upload()
        post.assert_not_called()

    def test_assistant_failure_does_not_break_intake(self):
        """Рантайм умер — черновик всё равно собран парсером."""
        sent = self._run({"text": TEXT, "channel": "telegram"},
                         ("adresnik.3mf", FAKE_3MF), assistant_enabled=True)
        self.assertEqual(200, sent["code"])
        self.assertEqual("Адресник", sent["payload"]["draft"]["product"])
        self.assertFalse(sent["payload"]["assistant"]["ok"])

    def test_same_content_is_not_duplicated_in_uploads(self):
        self._run({"text": TEXT}, ("adresnik.3mf", FAKE_3MF))
        sent = self._run({"text": TEXT}, ("adresnik.3mf", FAKE_3MF))
        self.assertEqual("adresnik.3mf", sent["payload"]["draft"]["file"])
        saved = list(self.uploads.glob("adresnik*.3mf"))
        self.assertEqual(1, len(saved), f"в uploads появилось несколько копий: {saved}")

    def test_warnings_mention_attachment(self):
        sent = self._run({"text": TEXT}, ("adresnik.3mf", FAKE_3MF))
        self.assertTrue(any("Файл из сообщения" in warning
                            for warning in sent["payload"]["warnings"]))


if __name__ == "__main__":
    unittest.main()

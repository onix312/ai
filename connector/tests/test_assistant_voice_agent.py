"""Голос и агент компьютера: два внешних рантайма помощника (18.13, срезы 2–3).

Оба устроены как текстовая модель: отдельная программа, loopback, stdlib-клиент,
честное выключение. Поэтому здесь держатся те же предохранители и ничего больше:

  * адрес проверяется на loopback ДО запроса — звук цеха и снимки экрана не
    должны уехать на чужую машину;
  * выключенный рантайм не пингуется вовсе: панель не обязана ждать;
  * расшифровка ничего не выполняет и не сохраняется — это текст, а не действие;
  * PrintFlow не хранит ни зависимостей агента, ни его снимков: только адрес,
    статус и имя активного окна.

Живую речь, стоп-слово и действия в чужих окнах в CI проверить нельзя — для
владельца есть чеки-лист в `docs/`.
"""
from __future__ import annotations

import io
import json
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

import pf  # noqa: E402
from connector.printflow import assistant, config, http_handler  # noqa: E402
from connector.printflow.api import Handler, register_routes, router  # noqa: E402
from connector.printflow.db import Database  # noqa: E402

AUDIO = b"RIFF....WAVEfmt " + b"\x00" * 64


def _multipart(boundary: str, fields: dict[str, str], file_bytes: bytes) -> bytes:
    body = b""
    for name, value in fields.items():
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                 f"{value}\r\n").encode("utf-8")
    body += (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="audio"; filename="phrase.webm"\r\n'
             "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
    return body + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "runtime.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()


class SpeechConfigTests(DatabaseTestCase):
    def test_defaults_match_connector(self):
        cfg = assistant.speech_config(self.db)
        self.assertFalse(cfg["enabled"])
        self.assertEqual(assistant.DEFAULT_SPEECH_URL, cfg["url"])
        self.assertEqual(config.DEFAULT_SETTINGS["assistant_speech_url"], cfg["url"])

    def test_url_keeps_no_trailing_slash(self):
        self.db.set_settings({"assistant_speech_url": "http://127.0.0.1:8791/"})
        self.assertEqual("http://127.0.0.1:8791",
                         assistant.speech_config(self.db)["url"])

    def test_disabled_runtime_is_not_probed(self):
        """Выключенный голос не должен тормозить открытие панели ожиданием."""
        with patch.object(assistant, "_post_json") as post:
            state = assistant.speech_status(self.db)
        post.assert_not_called()
        self.assertFalse(state["available"])
        self.assertIn("выключен", state["reason"].lower())

    def test_foreign_address_is_refused_before_request(self):
        self.db.set_settings({"assistant_speech_enabled": True,
                              "assistant_speech_url": "http://10.0.0.9:8791"})
        with patch.object(assistant, "_post_json") as post:
            state = assistant.speech_status(self.db)
        post.assert_not_called()
        self.assertFalse(state["loopback"])
        self.assertIn("этим компьютером", state["reason"])

    def test_dead_runtime_gives_reason(self):
        self.db.set_settings({"assistant_speech_enabled": True})
        with patch.object(assistant, "_post_json",
                          return_value=(False, None, "connection refused")):
            state = assistant.speech_status(self.db)
        self.assertFalse(state["available"])
        self.assertIn("connection refused", state["reason"])

    def test_alive_runtime_reports_model(self):
        self.db.set_settings({"assistant_speech_enabled": True})
        with patch.object(assistant, "_post_json",
                          return_value=(True, {"ok": True, "model": "vosk-ru-0.42"}, "")):
            state = assistant.speech_status(self.db)
        self.assertTrue(state["available"])
        self.assertEqual("vosk-ru-0.42", state["model"])

    def test_status_carries_speech_for_panel(self):
        """Панель рисует микрофон по состоянию речи, а не по текстовой модели."""
        with patch.object(assistant, "_post_json",
                          return_value=(False, None, "рантайм недоступен")):
            state = assistant.status(self.db)
        self.assertIn("speech", state)
        self.assertFalse(state["speech"]["available"])


class TranscribeTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.db.set_settings({"assistant_speech_enabled": True,
                              "assistant_speech_url": "http://127.0.0.1:8791"})

    def _call(self, audio: bytes, reply: dict | None = None, error: Exception | None = None):
        calls: dict = {}

        class FakeResponse:
            def __init__(self, raw):
                self._raw = raw

            def read(self, limit=-1):
                return self._raw

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=None):
            calls["url"] = request.full_url
            calls["body"] = request.data
            calls["timeout"] = timeout
            if error:
                raise error
            payload = reply if reply is not None else {"text": "поставь паузу на первом"}
            return FakeResponse(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        with patch.object(assistant, "_post_json", return_value=(True, {"ok": True}, "")), \
             patch.object(assistant, "_local_open", fake_urlopen):
            result = assistant.transcribe(self.db, audio)
        return result, calls

    def test_audio_goes_only_to_loopback(self):
        result, calls = self._call(AUDIO)
        self.assertTrue(result["ok"])
        self.assertEqual("поставь паузу на первом", result["text"])
        self.assertEqual("http://127.0.0.1:8791/transcribe", calls["url"])
        self.assertIn(AUDIO, calls["body"])

    def test_empty_recording_is_refused(self):
        result, _calls = self._call(b"")
        self.assertFalse(result["ok"])
        self.assertIn("Пустая", result["reason"])

    def test_too_long_recording_is_refused(self):
        result, _calls = self._call(b"\x00" * (assistant.MAX_AUDIO_BYTES + 1))
        self.assertFalse(result["ok"])
        self.assertIn("длиннее", result["reason"])

    def test_unavailable_runtime_is_not_called(self):
        self.db.set_settings({"assistant_speech_enabled": False})
        with patch.object(assistant, "_local_open") as opened:
            result = assistant.transcribe(self.db, AUDIO)
        opened.assert_not_called()
        self.assertFalse(result["ok"])

    def test_silence_is_a_reason_not_an_empty_answer(self):
        result, _calls = self._call(AUDIO, reply={"text": "   "})
        self.assertFalse(result["ok"])
        self.assertIn("не распознана", result["reason"])

    def test_runtime_error_is_a_reason(self):
        result, _calls = self._call(AUDIO, error=OSError("нет ответа"))
        self.assertFalse(result["ok"])
        self.assertIn("нет ответа", result["reason"])

    def test_text_is_trimmed_like_any_phrase(self):
        result, _calls = self._call(AUDIO, reply={"text": "а" * 9000})
        self.assertEqual(assistant.MAX_INPUT_CHARS, len(result["text"]))

    def test_transcribe_does_not_execute_anything(self):
        """Расшифровка — текст. Действие делает человек в панели."""
        class FakeResponse:
            def read(self, limit=-1):
                return json.dumps({"text": "запусти печать"},
                                  ensure_ascii=False).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch.object(assistant, "_post_json", return_value=(True, {"ok": True}, "")), \
             patch.object(assistant, "parse_intent") as intent, \
             patch.object(assistant.urllib.request, "urlopen",
                          return_value=FakeResponse()):
            assistant.transcribe(self.db, AUDIO)
        intent.assert_not_called()

class SpeechRouteTests(DatabaseTestCase):
    BOUNDARY = "----printflow-speech-test"

    def _post(self, audio: bytes):
        body = _multipart(self.BOUNDARY, {"language": "ru"}, audio)
        handler = Handler.__new__(Handler)
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": f"multipart/form-data; boundary={self.BOUNDARY}",
        }
        handler.rfile = io.BytesIO(body)
        handler.api = SimpleNamespace(db=self.db, manager=None)
        sent: dict = {}
        handler.send_json = lambda code, payload: sent.update(code=code, payload=payload)
        handler.handle_speech_upload()
        return sent

    def test_route_is_wired_into_post_chain(self):
        """Байтовый маршрут живёт в цепочке до реестра: реестр принимает только JSON."""
        source = (ROOT / "connector" / "printflow"
                  / "http_handler.py").read_text(encoding="utf-8")
        self.assertIn('if path == "/api/assistant/speech":', source)
        self.assertIn("handle_speech_upload()", source)
        self.assertTrue(hasattr(http_handler.Handler, "handle_speech_upload"))

    def test_dead_runtime_answers_with_reason(self):
        with patch.object(assistant, "_post_json",
                          return_value=(False, None, "рантайм недоступен")):
            sent = self._post(AUDIO)
        self.assertEqual(200, sent["code"])
        self.assertFalse(sent["payload"]["ok"])

    def test_live_runtime_returns_text(self):
        self.db.set_settings({"assistant_speech_enabled": True})

        class FakeResponse:
            def read(self, limit=-1):
                return json.dumps({"text": "что сейчас печатается"},
                                  ensure_ascii=False).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch.object(assistant, "_post_json", return_value=(True, {"ok": True}, "")), \
             patch.object(assistant, "_local_open",
                          return_value=FakeResponse()):
            sent = self._post(AUDIO)
        self.assertEqual(200, sent["code"])
        self.assertEqual("что сейчас печатается", sent["payload"]["text"])

    def test_missing_audio_is_400(self):
        handler = Handler.__new__(Handler)
        handler.headers = {"Content-Length": "0",
                           "Content-Type": f"multipart/form-data; boundary={self.BOUNDARY}"}
        handler.rfile = io.BytesIO(b"")
        handler.api = SimpleNamespace(db=self.db, manager=None)
        sent: dict = {}
        handler.send_json = lambda code, payload: sent.update(code=code, payload=payload)
        handler.handle_speech_upload()
        self.assertEqual(400, sent["code"])


class AgentStatusTests(DatabaseTestCase):
    def test_disabled_agent_is_not_probed(self):
        with patch.object(assistant, "_post_json") as post:
            state = assistant.agent_status(self.db)
        post.assert_not_called()
        self.assertFalse(state["available"])
        self.assertIn("выключен", state["reason"].lower())

    def test_foreign_agent_address_is_refused(self):
        self.db.set_settings({"assistant_agent_enabled": True,
                              "assistant_agent_url": "http://192.168.1.50:8799"})
        with patch.object(assistant, "_post_json") as post:
            state = assistant.agent_status(self.db)
        post.assert_not_called()
        self.assertFalse(state["loopback"])

    def test_alive_agent_reports_window_and_wake_word(self):
        self.db.set_settings({"assistant_agent_enabled": True})
        with patch.object(assistant, "_tcp_up", return_value=(True, "")), \
             patch.object(assistant, "_post_json",
                          return_value=(True, {"ok": True, "window": "Telegram",
                                               "wake_word": True}, "")):
            state = assistant.agent_status(self.db)
        self.assertTrue(state["available"])
        self.assertEqual("Telegram", state["window"])
        self.assertTrue(state["wake_word"])

    def test_printflow_stores_only_address(self):
        """Ни снимков, ни нажатий: база знает только адрес и флаг."""
        keys = sorted(key for key in config.DEFAULT_SETTINGS if "agent" in key)
        self.assertEqual(["assistant_agent_enabled", "assistant_agent_url"], keys)

    def test_agent_route_is_registered(self):
        register_routes()
        found = [r for r in router.reference() if r["path"] == "/api/assistant/agent"]
        self.assertEqual(1, len(found))
        self.assertEqual("GET", found[0]["method"])

    def test_route_answers_without_agent(self):
        register_routes()
        api = SimpleNamespace(db=self.db, manager=None)
        response = router.dispatch(api, "GET", "/api/assistant/agent")
        self.assertEqual(200, response[0])
        self.assertFalse(response[1]["available"])


class PanelWiringTests(unittest.TestCase):
    """Панель: микрофон живёт, адреса рантаймов в странице не зашиты."""

    @classmethod
    def setUpClass(cls):
        cls.page = (ROOT / "site" / "assistant.html").read_text(encoding="utf-8")

    def test_mic_records_and_sends_to_own_route(self):
        self.assertIn("MediaRecorder", self.page)
        self.assertIn("getUserMedia", self.page)
        self.assertIn("/api/assistant/speech", self.page)

    def test_runtime_addresses_are_not_hardcoded(self):
        self.assertNotIn("8791", self.page)
        self.assertNotIn("8799", self.page)
        self.assertIn("/api/assistant/agent", self.page)

    def test_mic_is_disabled_until_speech_is_alive(self):
        self.assertIn('id="as_mic" type="button" disabled', self.page)
        self.assertIn("mic.disabled = false", self.page)

    def test_settings_page_documents_voice_and_agent(self):
        """Поля рисуются списком в app.js и проверяются схемой на сервере."""
        page = (ROOT / "site" / "assets" / "app.js").read_text(encoding="utf-8")
        schema = (ROOT / "connector" / "printflow"
                  / "settings_schema.py").read_text(encoding="utf-8")
        defaults = (ROOT / "connector" / "printflow"
                    / "config.py").read_text(encoding="utf-8")
        for key in ("assistant_speech_enabled", "assistant_speech_url",
                    "assistant_speech_model", "assistant_speech_timeout_sec",
                    "assistant_agent_enabled", "assistant_agent_url"):
            with self.subTest(setting=key):
                self.assertIn(key, page)
                self.assertIn(key, schema)
                self.assertIn(key, defaults)


class DoctorRuntimeTests(unittest.TestCase):
    def test_defaults_include_voice_and_agent(self):
        settings = pf.read_assistant_settings()
        self.assertFalse(settings["assistant_speech_enabled"])
        self.assertFalse(settings["assistant_agent_enabled"])
        self.assertEqual("http://127.0.0.1:8791", settings["assistant_speech_url"])
        self.assertEqual("http://127.0.0.1:8799", settings["assistant_agent_url"])
        self.assertEqual(config.DEFAULT_SETTINGS["assistant_speech_url"],
                         settings["assistant_speech_url"])
        self.assertEqual(config.DEFAULT_SETTINGS["assistant_agent_url"],
                         settings["assistant_agent_url"])

    def test_probe_refuses_foreign_address(self):
        state, reason = pf.assistant_runtime_report("http://10.1.2.3:8791", "/health")
        self.assertEqual({}, state)
        self.assertIn("этим компьютером", reason)

    def test_probe_reports_dead_runtime(self):
        with patch.object(assistant, "_post_json",
                          return_value=(False, None, "connection refused")):
            state, reason = pf.assistant_runtime_report("http://127.0.0.1:8791", "/health")
        self.assertEqual({}, state)
        self.assertEqual("connection refused", reason)

    def test_all_three_blocks_survive_dead_runtimes(self):
        import contextlib
        buffer = io.StringIO()
        with patch.object(pf, "read_assistant_settings",
                          return_value={"assistant_enabled": True,
                                        "assistant_url": assistant.DEFAULT_URL,
                                        "assistant_model": "qwen2.5:3b",
                                        "assistant_speech_enabled": True,
                                        "assistant_speech_url": "http://127.0.0.1:8791",
                                        "assistant_speech_model": "",
                                        "assistant_agent_enabled": True,
                                        "assistant_agent_url": "http://127.0.0.1:8799"}), \
             patch.object(assistant, "_post_json",
                          return_value=(False, None, "нет ответа")), \
             contextlib.redirect_stdout(buffer):
            pf.assistant_reports()
        text = buffer.getvalue()
        self.assertIn("Голос", text)
        self.assertIn("Агент компьютера", text)
        self.assertNotIn("Traceback", text)


if __name__ == "__main__":
    unittest.main()

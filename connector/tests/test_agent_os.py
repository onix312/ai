"""Агент компьютера: речь, стоп-слово, окна и подтверждение (18.13, срез 3).

Агент — внешняя программа со своими зависимостями, поэтому здесь проверяется то,
что можно проверить без Windows, без микрофона и без модели речи:

  * он слушает только loopback — из сети до него не достать;
  * он жив и честен без единой зависимости: отдаёт причины, а не падает;
  * любое действие в чужом окне сначала становится ожидающим и выполняется
    только после подтверждения человека;
  * неподтверждённое действие умирает само и не выстреливает позже;
  * стоп-слово слышится с одной ошибкой распознавания и не срабатывает на
    похожих бытовых фразах;
  * фраза агента попадает в журнал панели, а не выполняется агентом.

Живой голос, снимок экрана и клик в чужом приложении в CI не проверяются —
для владельца есть чеки-лист в `docs/ПОМОЩНИК.md`.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from agent import capabilities, config, server, speech, winapi  # noqa: E402
from connector.printflow.api import register_routes, router  # noqa: E402
from connector.printflow.db import Database  # noqa: E402


def _url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


class ServerTestCase(unittest.TestCase):
    """Один живой агент на класс: порты поднимаются и гасятся вокруг тестов."""

    @classmethod
    def setUpClass(cls):
        cls.agent = server.Agent()
        # У другого запущенного помощника могут быть заняты фиксированные
        # порты. Тестовые серверы всегда берут свободные loopback-порты.
        with patch.object(config, "SPEECH_PORT", 0), patch.object(config, "AGENT_PORT", 0):
            cls.speech_server, cls.agent_server = server.serve(cls.agent)
        cls.speech_port = cls.speech_server.server_address[1]
        cls.agent_port = cls.agent_server.server_address[1]
        threading.Thread(target=cls.speech_server.serve_forever, daemon=True).start()
        threading.Thread(target=cls.agent_server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.speech_server.shutdown()
        cls.agent_server.shutdown()
        cls.speech_server.server_close()
        cls.agent_server.server_close()

    def get(self, port: int, path: str):
        with urllib.request.urlopen(_url(port, path), timeout=5) as response:
            raw = response.read(2 * 1024 * 1024)
            kind = response.headers.get("Content-Type", "")
        if kind.startswith("application/json"):
            return json.loads(raw.decode("utf-8"))
        return raw

    def post(self, port: int, path: str, payload: dict | None = None):
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            _url(port, path), data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read(1024 * 1024).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return json.loads(exc.read(1024 * 1024).decode("utf-8"))


class BindingTests(ServerTestCase):
    def test_both_ports_listen_on_loopback_only(self):
        for sock in (self.speech_server.socket, self.agent_server.socket):
            host, _port = sock.getsockname()[:2]
            self.assertEqual("127.0.0.1", host, "агент не должен быть виден из сети")

    def test_speech_health_answers_without_dependencies(self):
        payload = self.get(self.speech_port, "/health")
        self.assertTrue(payload["ok"])
        self.assertIn("capabilities", payload)
        self.assertIn("wake_word", payload)

    def test_agent_status_answers_without_dependencies(self):
        payload = self.get(self.agent_port, "/status")
        self.assertTrue(payload["ok"])
        self.assertEqual([], payload["pending"])

    def test_capabilities_report_every_missing_reason(self):
        payload = self.get(self.agent_port, "/capabilities")
        self.assertTrue(payload["capabilities"])
        for line in payload["missing"]:
            self.assertTrue(line, "причина отсутствия возможности не может быть пустой")

    def test_speech_routes_do_not_leak_into_agent_port(self):
        payload = self.get(self.agent_port, "/health")
        self.assertTrue(payload["ok"])
        with self.assertRaises(urllib.error.HTTPError):
            self.get(self.agent_port, "/screen.png")

    def test_unknown_route_answers_with_reason(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get(self.speech_port, "/whatever")
        self.assertEqual(404, caught.exception.code)


class ActionConfirmationTests(ServerTestCase):
    """Действие в чужом окне: очередь, подтверждение человека, срок жизни."""

    def test_action_is_refused_without_windows(self):
        payload = self.post(self.agent_port, "/click", {"x": 10, "y": 10})
        self.assertFalse(payload["ok"])
        self.assertIn("Windows", payload["reason"])

    def test_unknown_action_is_refused(self):
        self.agent.capabilities["windows"] = True
        try:
            payload = self.agent.queue_action("format_disk", {})
        finally:
            self.agent.capabilities["windows"] = winapi.IS_WINDOWS
        self.assertFalse(payload["ok"])
        self.assertIn("не известно", payload["reason"])

    def test_action_waits_for_human(self):
        self.agent.capabilities["windows"] = True
        try:
            with patch.object(server, "_execute", return_value=(True, "")) as execute, \
                 patch.object(server.Agent, "_ask"):
                result = self.agent.queue_action("type", {"text": "привет"})
                self.assertTrue(result["queued"])
                self.assertTrue(result["requires_confirmation"])
                execute.assert_not_called()
                self.assertEqual(1, len(self.agent.pending()))
                done = self.agent.confirm_action(result["id"], True)
            self.assertTrue(done["done"])
            execute.assert_called_once()
        finally:
            self.agent.capabilities["windows"] = winapi.IS_WINDOWS
            self.agent._pending.clear()

    def test_cancelled_action_is_not_executed(self):
        self.agent.capabilities["windows"] = True
        try:
            with patch.object(server, "_execute", return_value=(True, "")) as execute, \
                 patch.object(server.Agent, "_ask"):
                result = self.agent.queue_action("click", {"x": 1, "y": 2})
                answer = self.agent.confirm_action(result["id"], False)
            self.assertFalse(answer["done"])
            self.assertIn("Отменено", answer["reason"])
            execute.assert_not_called()
            self.assertEqual([], self.agent.pending())
        finally:
            self.agent.capabilities["windows"] = winapi.IS_WINDOWS

    def test_expired_action_dies_by_itself(self):
        self.agent.capabilities["windows"] = True
        try:
            with patch.object(server.Agent, "_ask"):
                result = self.agent.queue_action("key", {"key": "enter"})
                self.agent._pending[result["id"]]["created_at"] -= server.PENDING_TTL_SEC + 1
                self.assertEqual([], self.agent.pending())
                answer = self.agent.confirm_action(result["id"], True)
            self.assertFalse(answer["ok"])
            self.assertIn("истекло", answer["reason"])
        finally:
            self.agent.capabilities["windows"] = winapi.IS_WINDOWS

    def test_missing_confirmation_window_refuses_action(self):
        """Нет окна подтверждения — нет и действия, даже по истечении срока."""
        self.agent.capabilities["windows"] = True
        try:
            with patch.object(server.Agent, "_ask"):
                result = self.agent.queue_action("click", {"x": 5, "y": 5})
            # Реальный `_ask` ловит и отсутствие экрана, и отсутствие tkinter:
            # в обоих случаях действие не выполняется.
            with patch.object(server, "_execute", return_value=(True, "")) as execute:
                self.agent._ask(result["id"])
            execute.assert_not_called()
            self.assertEqual([], self.agent.pending())
        finally:
            self.agent.capabilities["windows"] = winapi.IS_WINDOWS
            self.agent._pending.clear()

    def test_action_text_says_what_will_happen(self):
        self.assertEqual("Нажать клавишу enter в окне «Блокнот»",
                         server._describe("key", {"key": "enter"}, "Блокнот"))
        self.assertIn("Ввести текст", server._describe("type", {"text": "Мария"}, ""))


class ScreenAndWindowTests(ServerTestCase):
    def test_screen_answers_with_reason_when_unavailable(self):
        with patch.object(winapi, "grab_screen", return_value=(b"", "нет Pillow")):
            payload = self.get(self.agent_port, "/screen")
        self.assertFalse(payload["ok"])
        self.assertEqual("нет Pillow", payload["reason"])

    def test_screen_returns_bytes_when_available(self):
        with patch.object(winapi, "grab_screen", return_value=(b"\x89PNG-test", "")):
            payload = self.get(self.agent_port, "/screen")
        self.assertEqual(b"\x89PNG-test", payload)

    def test_windows_list_answers_with_reason(self):
        with patch.object(winapi, "list_windows", return_value=([], "не Windows")):
            payload = self.get(self.agent_port, "/windows")
        self.assertFalse(payload["ok"])
        self.assertEqual([], payload["windows"])

    def test_screenshot_is_not_saved_on_disk(self):
        """Снимок уходит байтами и не остаётся ни в базе, ни в папке."""
        source = (ROOT / "agent" / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("write_bytes", source)
        self.assertNotIn("open(", source)


class SpeechRuntimeTests(ServerTestCase):
    def test_transcribe_refuses_without_model(self):
        payload = self.agent.transcribe(b"RIFF....WAVE")
        self.assertFalse(payload["ok"])
        self.assertIn("vosk", payload["reason"])

    def test_transcribe_refuses_empty_audio(self):
        payload = self.agent.transcribe(b"")
        self.assertFalse(payload["ok"])
        self.assertIn("Пустая", payload["reason"])

    def test_route_returns_reason_not_crash(self):
        boundary = "----agent-test"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="audio"; filename="p.wav"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n").encode() + b"RIFF" \
            + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            _url(self.speech_port, "/transcribe"), data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["reason"])

    def test_multipart_reader_splits_fields_and_audio(self):
        handler = server.AgentHandler.__new__(server.AgentHandler)
        boundary = "----unit"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\nru\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; "
                f"filename=\"a.wav\"\r\nContent-Type: application/octet-stream\r\n\r\n"
                ).encode() + b"\x01\x02\x03" + f"\r\n--{boundary}--\r\n".encode()
        handler.headers = {"Content-Length": str(len(body)),
                           "Content-Type": f"multipart/form-data; boundary={boundary}"}
        handler.rfile = io.BytesIO(body)
        fields, audio = handler._read_multipart()
        self.assertEqual({"language": "ru"}, fields)
        self.assertEqual(b"\x01\x02\x03", audio)

    def test_microphone_refuses_without_sounddevice(self):
        ok, reason = self.agent.microphone.arm(5)
        self.assertFalse(ok)
        self.assertTrue(reason)
        self.assertFalse(self.agent.microphone.armed)

    def test_transcribe_with_model_returns_phrase(self):
        with patch.object(speech.Recognizer, "transcribe_wav",
                          return_value=("ноза что сейчас печатается", "")):
            payload = self.agent.transcribe(b"RIFF")
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["wake_word"])
        self.assertIn("печатается", payload["phrase"])


class WakeWordTests(unittest.TestCase):
    def test_exact_word_is_heard(self):
        self.assertTrue(speech.is_wake_phrase("Ноза, покажи долги"))

    def test_one_recognition_mistake_is_tolerated(self):
        self.assertTrue(speech.is_wake_phrase("нозза включи свет"))
        self.assertTrue(speech.is_wake_phrase("Но за, что печатается"))

    def test_everyday_phrase_is_not_a_wake_word(self):
        for phrase in ("нужно включить станок", "заказ для Марии", "поставь паузу"):
            with self.subTest(phrase=phrase):
                self.assertFalse(speech.is_wake_phrase(phrase))

    def test_command_is_cut_from_phrase(self):
        self.assertEqual("покажи долги",
                         speech.phrase_after_wake_word("Ноза, покажи долги"))

    def test_empty_wake_word_never_matches(self):
        self.assertFalse(speech.is_wake_phrase("что угодно", wake_word=""))


class PhraseRouteTests(unittest.TestCase):
    """Фраза агента в панели: след в журнале, действия — нет."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "phrase.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_route_is_registered(self):
        register_routes()
        found = [r for r in router.reference() if r["path"] == "/api/assistant/phrase"]
        self.assertEqual(1, len(found))
        self.assertEqual("POST", found[0]["method"])

    def test_phrase_is_journaled_and_not_executed(self):
        register_routes()
        api = SimpleNamespace(db=self.db, manager=None)
        with patch("connector.printflow.assistant.parse_intent") as intent:
            code, payload = router.dispatch(
                api, "POST", "/api/assistant/phrase",
                {"text": "поставь паузу на первом", "source": "agent-wake-word"})
        intent.assert_not_called()
        self.assertEqual(200, code)
        self.assertTrue(payload["ok"])
        self.assertEqual("agent-wake-word", payload["source"])
        rows = [row for row in self.db.events(limit=20)
                if (row.get("data") or {}).get("action") == "phrase"]
        self.assertEqual(1, len(rows))

    def test_empty_phrase_is_refused(self):
        register_routes()
        api = SimpleNamespace(db=self.db, manager=None)
        code, payload = router.dispatch(api, "POST", "/api/assistant/phrase", {"text": "  "})
        self.assertEqual(400, code)
        self.assertFalse(payload["ok"])


class IsolationTests(unittest.TestCase):
    """Агент и коннектор не знают друг о друге: иначе зависимости протечут."""

    def test_agent_does_not_import_connector(self):
        for path in sorted((ROOT / "agent").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                self.assertNotIn("from connector", source)
                self.assertNotIn("import connector", source)
                self.assertNotIn("printflow.db", source)

    def test_agent_dependencies_are_not_in_connector_requirements(self):
        for name in ("requirements.txt", "connector/requirements.txt"):
            path = ROOT / name
            if not path.exists():
                continue
            source = path.read_text(encoding="utf-8").lower()
            with self.subTest(file=name):
                for package in ("vosk", "sounddevice", "mss", "pywin32"):
                    self.assertNotIn(package, source)

    def test_printflow_stores_only_agent_address(self):
        from connector.printflow import config as connector_config
        keys = sorted(key for key in connector_config.DEFAULT_SETTINGS
                      if "agent" in key)
        self.assertEqual(["assistant_agent_enabled", "assistant_agent_url"], keys)

    def test_agent_requirements_are_documented(self):
        text = (ROOT / "agent" / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("vosk", text)
        self.assertIn("процессор", text.lower())

    def test_agent_folder_is_not_imported_by_launcher(self):
        source = (ROOT / "pf.py").read_text(encoding="utf-8")
        self.assertNotIn("from agent", source)
        self.assertNotIn("import agent", source)

    def test_capabilities_are_honest_on_this_machine(self):
        state = capabilities.detect()
        for key in ("windows", "screen", "ocr", "speech", "microphone", "wake_word"):
            self.assertIsInstance(state[key], bool, key)
        if not state["windows"]:
            self.assertTrue(state["windows_reason"])


if __name__ == "__main__":
    unittest.main()

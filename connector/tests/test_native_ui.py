"""Контракт нативного NOZZA UI без требования графической сессии."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from agent.native_ui.backend import BackendClient, BackendError
from agent.native_ui.state import UiState


class _Response:
    def __init__(self, payload: dict):
        self.data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.data


class BackendClientTests(unittest.TestCase):
    def test_chat_uses_native_session_contract(self):
        client = BackendClient()
        seen = {}

        def fake(req, timeout=0):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode("utf-8"))
            seen["timeout"] = timeout
            return _Response({"ok": True, "reply": "готово"})

        with mock.patch("urllib.request.urlopen", fake):
            payload = client.chat("открой телеграм", "native")
        self.assertEqual(payload["reply"], "готово")
        self.assertEqual(seen["url"], "http://127.0.0.1:8799/chat")
        self.assertEqual(seen["body"]["session"], "native")
        self.assertEqual(seen["body"]["contract_version"], 1)

    def test_microphone_goes_to_speech_port(self):
        client = BackendClient()
        urls = []

        def fake(req, timeout=0):
            urls.append(req.full_url)
            return _Response({"ok": True, "armed": True})

        with mock.patch("urllib.request.urlopen", fake):
            client.arm_mic(25)
        self.assertEqual(urls, ["http://127.0.0.1:8791/mic/arm"])

    def test_network_failure_is_friendly(self):
        client = BackendClient()
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            with self.assertRaises(BackendError) as error:
                client.status()
        self.assertIn("Агент недоступен", str(error.exception))


class UiStateTests(unittest.TestCase):
    def test_status_and_recovery(self):
        state = UiState()
        state.set_error("нет связи")
        self.assertFalse(state.connected)
        self.assertEqual(state.assistant_state, "error")
        state.apply_status({"ok": True, "armed": True, "pending": [{"id": "1"}]})
        self.assertTrue(state.connected)
        self.assertTrue(state.armed)
        self.assertEqual(state.pending[0]["id"], "1")
        self.assertEqual(state.last_error, "")


if __name__ == "__main__":
    unittest.main()

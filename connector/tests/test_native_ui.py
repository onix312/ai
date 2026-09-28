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

    def test_persistent_voice_controls_use_speech_port(self):
        client = BackendClient()
        calls = []

        def fake(req, timeout=0):
            calls.append((req.get_method(), req.full_url))
            return _Response({"ok": True, "enabled": True, "state": "idle"})

        with mock.patch("urllib.request.urlopen", fake):
            client.voice_status()
            client.enable_voice()
            client.stop_voice()
            client.disable_voice()
        self.assertEqual([
            ("GET", "http://127.0.0.1:8791/voice/status"),
            ("POST", "http://127.0.0.1:8791/voice/enable"),
            ("POST", "http://127.0.0.1:8791/voice/stop"),
            ("POST", "http://127.0.0.1:8791/voice/disable"),
        ], calls)

    def test_stop_all_uses_agent_safety_routes(self):
        seen = []
        client = BackendClient()

        def fake(req, timeout=0):
            seen.append((req.get_method(), req.full_url))
            return _Response({"ok": True, "stopped": True, "latched": True})

        with patch("urllib.request.urlopen", side_effect=fake):
            self.assertTrue(client.safety_stop()["stopped"])
            client.safety_status()
            client.safety_resume()
        self.assertEqual(seen, [
            ("POST", "http://127.0.0.1:8799/safety/stop"),
            ("GET", "http://127.0.0.1:8799/safety/status"),
            ("POST", "http://127.0.0.1:8799/safety/resume"),
        ])

    def test_task_api_uses_agent_port(self):
        client = BackendClient()
        calls = []

        def fake(req, timeout=0):
            body = json.loads(req.data.decode("utf-8")) if req.data else None
            calls.append((req.get_method(), req.full_url, body))
            return _Response({"ok": True, "tasks": []})

        with mock.patch("urllib.request.urlopen", fake):
            client.tasks(12)
            client.task_op("pause", 7)
        self.assertEqual([
            ("GET", "http://127.0.0.1:8799/tasks?limit=12", None),
            ("POST", "http://127.0.0.1:8799/tasks", {"op": "pause", "id": 7}),
        ], calls)

    def test_planner_api_uses_agent_port(self):
        client = BackendClient()
        calls = []

        def fake(req, timeout=0):
            body = json.loads(req.data.decode("utf-8")) if req.data else None
            calls.append((req.get_method(), req.full_url, body))
            return _Response({"ok": True, "plans": []})

        with mock.patch("urllib.request.urlopen", fake):
            client.plans()
            client.plan_op("preview", goal="подготовить компьютер")
            client.plan_op("approve", "abc")
        self.assertEqual([
            ("GET", "http://127.0.0.1:8799/plans", None),
            ("POST", "http://127.0.0.1:8799/plans",
             {"op": "preview", "id": "", "goal": "подготовить компьютер"}),
            ("POST", "http://127.0.0.1:8799/plans",
             {"op": "approve", "id": "abc"}),
        ], calls)

    def test_provider_catalog_uses_agent_port(self):
        client = BackendClient()
        urls = []

        def fake(req, timeout=0):
            urls.append(req.full_url)
            return _Response({"ok": True, "providers": []})

        with mock.patch("urllib.request.urlopen", fake):
            payload = client.providers()
        self.assertTrue(payload["ok"])
        self.assertEqual(["http://127.0.0.1:8799/providers"], urls)

    def test_persona_api_uses_agent_port_and_whitelisted_profile_body(self):
        client = BackendClient()
        calls = []

        def fake(req, timeout=0):
            body = json.loads(req.data.decode("utf-8")) if req.data else None
            calls.append((req.get_method(), req.full_url, body))
            return _Response({"ok": True, "profile": {"address": "informal"}})

        with mock.patch("urllib.request.urlopen", fake):
            client.persona()
            client.persona_update({"address": "informal", "verbosity": "normal"})
            client.persona_reset()
        self.assertEqual([
            ("GET", "http://127.0.0.1:8799/persona", None),
            ("POST", "http://127.0.0.1:8799/persona", {
                "op": "update",
                "profile": {"address": "informal", "verbosity": "normal"},
            }),
            ("POST", "http://127.0.0.1:8799/persona", {"op": "reset"}),
        ], calls)

    def test_autonomy_api_uses_agent_port(self):
        client = BackendClient()
        calls = []

        def fake(req, timeout=0):
            body = json.loads(req.data.decode("utf-8")) if req.data else None
            calls.append((req.get_method(), req.full_url, body))
            return _Response({"ok": True, "level": "agent", "providers": {}})

        with mock.patch("urllib.request.urlopen", fake):
            client.autonomy()
            client.autonomy_update("operator", {"printflow": "assistant"})
            client.autonomy_reset()
        self.assertEqual([
            ("GET", "http://127.0.0.1:8799/autonomy", None),
            ("POST", "http://127.0.0.1:8799/autonomy", {
                "op": "update", "level": "operator",
                "providers": {"printflow": "assistant"},
            }),
            ("POST", "http://127.0.0.1:8799/autonomy", {"op": "reset"}),
        ], calls)

    def test_proactivity_api_uses_agent_port(self):
        client = BackendClient()
        calls = []

        def fake(req, timeout=0):
            body = json.loads(req.data.decode("utf-8")) if req.data else None
            calls.append((req.get_method(), req.full_url, body))
            return _Response({"ok": True, "settings": {"mode": "balanced"}, "recent": []})

        with mock.patch("urllib.request.urlopen", fake):
            client.proactivity()
            client.proactivity_update({
                "mode": "active",
                "max_nonurgent_per_hour": 3,
                "cooldown_minutes": 15,
                "quiet_start": "23:00",
                "quiet_end": "07:00",
            })
            client.events(12, "suppressed")
            client.proactivity_reset()
        self.assertEqual([
            ("GET", "http://127.0.0.1:8799/proactivity", None),
            ("POST", "http://127.0.0.1:8799/proactivity", {
                "op": "update",
                "settings": {
                    "mode": "active",
                    "max_nonurgent_per_hour": 3,
                    "cooldown_minutes": 15,
                    "quiet_start": "23:00",
                    "quiet_end": "07:00",
                },
            }),
            ("GET", "http://127.0.0.1:8799/events?limit=12&state=suppressed", None),
            ("POST", "http://127.0.0.1:8799/proactivity", {"op": "reset"}),
        ], calls)

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

    def test_status_keeps_stop_all_latch(self):
        state = UiState()
        state.apply_status({
            "ok": True,
            "voice": {"enabled": False, "state": "idle"},
            "safety": {"stopped": True, "latched": True},
        })
        self.assertTrue(state.safety_stopped)

    def test_voice_runtime_state_is_authoritative(self):
        state = UiState()
        state.apply_status({
            "ok": True,
            "armed": False,
            "voice": {
                "enabled": True,
                "armed": True,
                "state": "speaking",
                "last_error": "",
            },
        })
        self.assertTrue(state.voice_enabled)
        self.assertTrue(state.armed)
        self.assertEqual("speaking", state.assistant_state)

    def test_voice_streaming_activity_is_kept_for_orb(self):
        state = UiState()
        state.apply_status({
            "ok": True,
            "voice": {
                "enabled": True,
                "armed": True,
                "state": "listening",
                "partial_phrase": "ноза открой телеграм",
                "audio_level": 1450,
                "streaming_asr": True,
            },
        })
        self.assertEqual("ноза открой телеграм", state.voice_partial)
        self.assertEqual(1450, state.audio_level)
        self.assertTrue(state.streaming_asr)


if __name__ == "__main__":
    unittest.main()

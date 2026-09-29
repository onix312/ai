"""Контракт нативного NOZZA UI без требования графической сессии."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch
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
            client.tune_voice(420, 1.8, 220, 0.3)
            client.reset_voice_diagnostics()
            client.tts_settings()
            client.tts_update("piper.exe", "C:/voices/luma.onnx", "1")
            client.tts_test()
            client.pronunciation_items()
            client.pronunciation_set("Bambu", "бэмбу")
            client.pronunciation_delete("Bambu")
            client.tts_reset()
            client.disable_voice()
        self.assertEqual([
            ("GET", "http://127.0.0.1:8791/voice/status"),
            ("POST", "http://127.0.0.1:8791/voice/enable"),
            ("POST", "http://127.0.0.1:8791/voice/stop"),
            ("POST", "http://127.0.0.1:8791/voice/tune"),
            ("POST", "http://127.0.0.1:8791/voice/diagnostics/reset"),
            ("GET", "http://127.0.0.1:8791/voice/tts"),
            ("POST", "http://127.0.0.1:8791/voice/tts"),
            ("POST", "http://127.0.0.1:8791/voice/tts"),
            ("POST", "http://127.0.0.1:8791/voice/tts"),
            ("POST", "http://127.0.0.1:8791/voice/tts"),
            ("POST", "http://127.0.0.1:8791/voice/tts"),
            ("POST", "http://127.0.0.1:8791/voice/tts"),
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

    def test_memory_api_uses_exact_agent_record_operations(self):
        client = BackendClient()
        calls = []

        def fake(req, timeout=0):
            body = json.loads(req.data.decode("utf-8")) if req.data else None
            calls.append((req.get_method(), req.full_url, body))
            return _Response({"ok": True})

        with mock.patch("urllib.request.urlopen", fake):
            client.memory("Мария")
            client.memory_op("pin", 11, pinned=True)
            client.memory_op("forget", 11)
        self.assertEqual([
            ("GET", "http://127.0.0.1:8799/memory?q=%D0%9C%D0%B0%D1%80%D0%B8%D1%8F&session=native", None),
            ("POST", "http://127.0.0.1:8799/memory", {"op": "pin", "id": 11, "pinned": True}),
            ("POST", "http://127.0.0.1:8799/memory", {"op": "forget", "id": 11}),
        ], calls)

    def test_learning_api_preserves_exact_operation_payloads(self):
        client = BackendClient()
        calls = []

        def fake(req, timeout=0):
            body = json.loads(req.data.decode("utf-8")) if req.data else None
            calls.append((req.get_method(), req.full_url, body))
            return _Response({"ok": True})

        with mock.patch("urllib.request.urlopen", fake):
            client.learning()
            client.learning_op("teach", phrase="вруби рабочку", meaning="открой Telegram")
            client.learning_op("forget", id=17)
            client.learning_op("alias_forget", word="телега")
            client.learning_op("dismiss", id=31)
        self.assertEqual([
            ("GET", "http://127.0.0.1:8799/learning", None),
            ("POST", "http://127.0.0.1:8799/learning",
             {"op": "teach", "phrase": "вруби рабочку", "meaning": "открой Telegram"}),
            ("POST", "http://127.0.0.1:8799/learning", {"op": "forget", "id": 17}),
            ("POST", "http://127.0.0.1:8799/learning", {"op": "alias_forget", "word": "телега"}),
            ("POST", "http://127.0.0.1:8799/learning", {"op": "dismiss", "id": 31}),
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

    def test_camera_image_fetch_accepts_only_loopback_printer_routes(self):
        client = BackendClient()
        for url in (
            "https://example.com/camera.jpg",
            "http://127.0.0.1:8765/api/uploads?file=x.jpg",
        ):
            with self.assertRaises(BackendError, msg=url):
                client.fetch_local_image(url)

        class ImageResponse:
            headers = {"Content-Type": "image/jpeg"}
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self, _limit=-1):
                return b"\xff\xd8\xffjpeg\xff\xd9"

        with mock.patch("urllib.request.urlopen", return_value=ImageResponse()):
            data = client.fetch_local_image(
                "http://127.0.0.1:8765/api/printer/camera.jpg?printer_id=p1"
            )
        self.assertTrue(data.startswith(b"\xff\xd8\xff"))


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

    def test_live_activity_is_kept_for_orb_and_chat_surface(self):
        state = UiState()
        state.apply_status({
            "ok": True,
            "voice": {"state": "idle"},
            "activity": {
                "current": {
                    "phase": "executing",
                    "heard": "открой телеграм",
                    "reply": "",
                    "skill": "app.open",
                    "task_id": 7,
                    "detail": "Открыть Telegram",
                    "active": True,
                },
                "recent": [{"phase": "done", "reply": "Готово."}],
            },
        })
        self.assertTrue(state.activity_active)
        self.assertEqual("executing", state.activity_phase)
        self.assertEqual("открой телеграм", state.activity_heard)
        self.assertEqual("app.open", state.activity_skill)
        self.assertEqual(7, state.activity_task_id)
        self.assertEqual("Готово.", state.activity_recent[0]["reply"])

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

    def test_hq_tts_status_is_kept_for_settings_surface(self):
        state = UiState()
        state.apply_status({
            "ok": True,
            "tts": {
                "engine": "silero",
                "hq_local": True,
                "model": "silero_v5_5_ru.pt",
                "model_path": "C:/voices/silero_v5_5_ru.pt",
                "piper_path": "C:/tools/piper.exe",
                "piper_model_path": "C:/voices/luma.onnx",
                "piper_speaker": "2",
                "speaker": "baya",
                "sample_rate": 48000,
                "model_ready": True,
                "last_synth_ms": 184,
                "last_chars": 42,
            },
        })
        self.assertEqual("silero", state.tts_engine)
        self.assertTrue(state.tts_hq_local)
        self.assertEqual("silero_v5_5_ru.pt", state.tts_model)
        self.assertEqual("C:/voices/silero_v5_5_ru.pt", state.tts_model_path)
        self.assertEqual("C:/tools/piper.exe", state.tts_piper_path)
        self.assertEqual("C:/voices/luma.onnx", state.tts_piper_model_path)
        self.assertEqual("2", state.tts_piper_speaker)
        self.assertEqual("baya", state.tts_speaker)
        self.assertEqual(48000, state.tts_sample_rate)
        self.assertTrue(state.tts_model_ready)
        self.assertEqual(184, state.tts_last_synth_ms)
        self.assertEqual(42, state.tts_last_chars)

    def test_voice_diagnostics_are_kept_for_settings_surface(self):
        state = UiState()
        state.apply_status({
            "ok": True,
            "voice": {
                "audio_level": 910,
                "echo_floor": 240,
                "echo_threshold": 620,
                "echo_suppressed": 17,
                "echo_gate_multiplier": 1.8,
                "echo_gate_margin": 220,
                "echo_floor_alpha": 0.3,
                "vad_threshold": 320,
                "asr_engine": "vosk",
                "vocabulary_count": 42,
            },
        })
        self.assertEqual(910, state.audio_level)
        self.assertEqual(240, state.echo_floor)
        self.assertEqual(620, state.echo_threshold)
        self.assertEqual(17, state.echo_suppressed)
        self.assertAlmostEqual(1.8, state.echo_gate_multiplier)
        self.assertEqual(220, state.echo_gate_margin)
        self.assertAlmostEqual(0.3, state.echo_floor_alpha)
        self.assertEqual("vosk", state.asr_engine)
        self.assertEqual(42, state.vocabulary_count)

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

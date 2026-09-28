"""Voice Engine 2.0: state machine without a real microphone."""
from __future__ import annotations

import queue
import struct
import threading
import time
import unittest
from unittest.mock import patch

from agent import pc, voice_runtime


class _Recognizer:
    reason = ""
    def load(self):
        return True


class VoiceHelpersTests(unittest.TestCase):
    def test_peak_level_reads_int16(self):
        data = struct.pack("<4h", 0, 100, -900, 20)
        self.assertEqual(900, voice_runtime.peak_level(data))

    def test_stop_and_end_phrases(self):
        self.assertTrue(voice_runtime.is_stop_phrase("стоп, Ноза"))
        self.assertTrue(voice_runtime.is_stop_phrase("хватит говорить"))
        self.assertFalse(voice_runtime.is_stop_phrase("останови печать"))
        self.assertTrue(voice_runtime.is_end_phrase("всё, спасибо"))
        self.assertFalse(voice_runtime.is_end_phrase("спасибо и открой телеграм"))


class _FakeStream:
    def __init__(self):
        self.chunks = []

    def feed(self, data):
        self.chunks.append(bytes(data))
        return "ноза открой" if len(self.chunks) >= 3 else "ноза"

    def finish(self):
        return "ноза открой телеграм"


class _StreamingRecognizer(_Recognizer):
    name = "vosk"

    def __init__(self):
        self.stream = _FakeStream()

    def start_stream(self, rate=16000):
        self.rate = rate
        return self.stream


class VoiceStreamingTests(unittest.TestCase):
    def test_vad_keeps_preroll_and_finalizes_incremental_asr(self):
        recognizer = _StreamingRecognizer()
        runtime = voice_runtime.VoiceRuntime(recognizer)
        frames = queue.Queue()
        silent = struct.pack("<4h", 0, 10, -10, 0)
        loud = struct.pack("<4h", 0, 900, -700, 0)
        frames.put(silent)
        frames.put(silent)
        frames.put(loud)
        frames.put(loud)
        for _ in range(8):
            frames.put(silent)

        with patch.object(runtime, "_should_run", return_value=True), \
             patch.object(pc, "is_speaking", return_value=False):
            chunks, during_output, text = runtime._next_phrase(frames)

        self.assertFalse(during_output)
        self.assertEqual("ноза открой телеграм", text)
        self.assertTrue(runtime.streaming_asr)
        self.assertEqual("ноза открой телеграм", runtime.partial_phrase)
        self.assertEqual(16000, recognizer.rate)
        self.assertGreaterEqual(len(chunks), 4)
        self.assertEqual(silent, chunks[0], "pre-roll должен сохранить звук до VAD")
        self.assertEqual(silent, recognizer.stream.chunks[0])

    def test_status_exposes_partial_and_engine(self):
        recognizer = _StreamingRecognizer()
        runtime = voice_runtime.VoiceRuntime(recognizer)
        runtime.partial_phrase = "ноза открой"
        runtime.audio_level = 1234
        runtime.streaming_asr = True
        status = runtime.status()
        self.assertEqual("ноза открой", status["partial_phrase"])
        self.assertEqual(1234, status["audio_level"])
        self.assertTrue(status["streaming_asr"])
        self.assertEqual("vosk", status["asr_engine"])


class VoiceRuntimeStateTests(unittest.TestCase):
    def setUp(self):
        self.runtime = voice_runtime.VoiceRuntime(_Recognizer())

    def _handler(self, calls, event):
        def handle(text):
            calls.append(text)
            event.set()
            return {"reply": "ok"}
        return handle

    def test_idle_ignores_phrase_without_wake_word(self):
        calls = []
        event = threading.Event()
        self.runtime.handler = self._handler(calls, event)
        with patch.object(pc, "is_speaking", return_value=False):
            self.runtime._handle_text("открой телеграм")
        self.assertFalse(event.wait(0.1))
        self.assertEqual([], calls)

    def test_wake_word_dispatches_command_and_opens_followup(self):
        calls = []
        event = threading.Event()
        self.runtime.handler = self._handler(calls, event)
        with patch.object(pc, "is_speaking", return_value=False):
            self.runtime._handle_text("Ноза, открой телеграм")
            self.assertTrue(event.wait(1))
        self.assertEqual(["открой телеграм"], calls)
        deadline = time.time() + 1
        while self.runtime._handler_busy and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.runtime.conversation_active)

    def test_followup_does_not_need_second_wake_word(self):
        calls = []
        first = threading.Event()
        self.runtime.handler = self._handler(calls, first)
        with patch.object(pc, "is_speaking", return_value=False):
            self.runtime._handle_text("ноза который час")
            self.assertTrue(first.wait(1))
            deadline = time.time() + 1
            while self.runtime._handler_busy and time.time() < deadline:
                time.sleep(0.01)
            second = threading.Event()
            self.runtime.handler = self._handler(calls, second)
            self.runtime._handle_text("а завтра")
            self.assertTrue(second.wait(1))
        self.assertEqual(["который час", "а завтра"], calls)

    def test_output_echo_is_never_dispatched(self):
        calls = []
        event = threading.Event()
        self.runtime.handler = self._handler(calls, event)
        self.runtime.conversation_until = time.time() + 10
        with patch.object(pc, "is_speaking", return_value=False):
            self.runtime._handle_text("это голос самой нозы", during_output=True)
        self.assertFalse(event.wait(0.1))
        self.assertEqual([], calls)

    def test_barge_in_stops_tts(self):
        self.runtime.conversation_until = time.time() + 10
        with patch.object(pc, "is_speaking", return_value=True), \
             patch.object(pc, "stop_speaking", return_value=True) as stop:
            self.runtime._handle_text("стоп", during_output=True)
        stop.assert_called_once()
        self.assertEqual("listening", self.runtime.state)

    def test_stop_phrase_cancels_busy_model_turn(self):
        calls = []
        self.runtime._handler_busy = True
        self.runtime.conversation_until = time.time() + 10
        self.runtime.cancel_handler = lambda: calls.append("cancel") or True
        with patch.object(pc, "is_speaking", return_value=False):
            self.runtime._handle_text("стоп")
        self.assertEqual(["cancel"], calls)
        self.assertEqual("listening", self.runtime.state)

    def test_non_stop_phrase_is_ignored_while_handler_busy(self):
        calls = []
        self.runtime._handler_busy = True
        self.runtime.cancel_handler = lambda: calls.append("cancel") or True
        with patch.object(pc, "is_speaking", return_value=False):
            self.runtime._handle_text("открой телеграм")
        self.assertEqual([], calls)

    def test_end_phrase_closes_conversation(self):
        self.runtime.conversation_until = time.time() + 10
        with patch.object(pc, "is_speaking", return_value=False):
            self.runtime._handle_text("всё спасибо")
        self.assertFalse(self.runtime.conversation_active)
        self.assertEqual("idle", self.runtime.state)

    def test_followup_window_restarts_after_tts_finishes(self):
        self.runtime.conversation_until = time.time() - 1
        with patch.object(pc, "is_speaking", side_effect=[True, False]), \
             patch.object(voice_runtime.time, "sleep", return_value=None):
            self.runtime._watch_output()
        self.assertTrue(self.runtime.conversation_active)
        self.assertEqual("listening", self.runtime.state)


    def test_error_state_is_not_erased_by_status(self):
        self.runtime.state = "error"
        self.runtime.last_error = "нет устройства"
        status = self.runtime.status()
        self.assertEqual("error", status["state"])
        self.assertEqual("нет устройства", status["last_error"])

    def test_stop_when_voice_disabled_does_not_arm_microphone(self):
        self.runtime.cancel_handler = lambda: False
        with patch.object(pc, "stop_speaking", return_value=False):
            payload = self.runtime.stop_output()
        self.assertFalse(payload["generation_cancelled"])
        self.assertFalse(payload["armed"])
        self.assertFalse(payload["enabled"])
        self.assertEqual("idle", payload["state"])


class _FakeStdin:
    def close(self):
        pass


class _FakeProcess:
    def __init__(self):
        self.pid = 123
        self.stdin = _FakeStdin()
        self.alive = True
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True
        self.alive = False

    def wait(self, timeout=None):
        self.alive = False
        return 0

    def kill(self):
        self.killed = True
        self.alive = False


class TtsInterruptionTests(unittest.TestCase):
    def tearDown(self):
        pc.stop_speaking()

    def test_speak_tracks_process_and_stop_terminates_it(self):
        process = _FakeProcess()
        with patch.object(pc, "speech_engine", return_value="sapi"), \
             patch.object(pc.subprocess, "Popen", return_value=process):
            result, reason = pc.speak("привет")
        self.assertEqual("", reason)
        self.assertEqual(123, result["pid"])
        self.assertTrue(pc.is_speaking())
        self.assertTrue(pc.stop_speaking())
        self.assertTrue(process.terminated)
        self.assertFalse(pc.is_speaking())


if __name__ == "__main__":
    unittest.main()

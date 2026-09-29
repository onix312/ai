"""Speech Quality Layer 1.0 tests."""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from agent import pc, tts_quality


class SpeechQualityTests(unittest.TestCase):
    def test_builtin_pronunciations_are_applied(self):
        text = tts_quality.prepare_tts_text("CPU 40%, GPU 65%, P1S печатает PLA.")
        self.assertIn("си-пи-ю", text)
        self.assertIn("джи-пи-ю", text)
        self.assertIn("пи один эс", text)
        self.assertIn("пи-эл-эй", text)
        self.assertIn("процентов", text)

    def test_time_decimal_and_small_numbers_are_spoken(self):
        text = tts_quality.prepare_tts_text("В 8:30 температура 21.5 °C, осталось 3 минуты.", overrides={})
        self.assertIn("восемь тридцать", text)
        self.assertIn("двадцать один целых пять", text)
        self.assertIn("градусов Цельсия", text)
        self.assertIn("три минуты", text)

    def test_url_is_made_pronounceable(self):
        text = tts_quality.prepare_tts_text("Открой https://example.com/docs/test-page", overrides={})
        self.assertNotIn("https://", text)
        self.assertIn("точка", text)
        self.assertIn("слэш", text)
        self.assertIn("дефис", text)

    def test_pause_punctuation_is_normalized(self):
        text = tts_quality.prepare_tts_text("Готово — можно печатать; следующий шаг: Проверка.", overrides={})
        self.assertNotIn("—", text)
        self.assertNotIn(";", text)
        self.assertIn(",", text)

    def test_user_dictionary_overrides_builtin(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "pron.json"
            path.write_text(json.dumps({"Bambu": "бэмбу", "MIKHAIL": "михаил"}, ensure_ascii=False),
                            encoding="utf-8")
            with patch.dict(os.environ, {"LUMA_TTS_PRONUNCIATIONS": str(path)}, clear=False):
                text = tts_quality.prepare_tts_text("Bambu для MIKHAIL")
        self.assertEqual("бэмбу для михаил", text.casefold())

    def test_dictionary_matches_whole_terms_only(self):
        text = tts_quality.prepare_tts_text("API и APITest", overrides={"API": "апи"})
        self.assertEqual("апи и APITest", text)


class PiperQualityIntegrationTests(unittest.TestCase):
    def test_piper_receives_prepared_text(self):
        with patch.object(pc, "speech_engine", return_value="piper"), \
             patch.object(pc.tts_quality, "prepare_tts_text", return_value="готовый текст") as prepare, \
             patch.object(pc, "_piper_speak", return_value=({"engine": "piper"}, "")) as piper:
            state, reason = pc.speak("CPU 40%")
        self.assertEqual("", reason)
        self.assertEqual("piper", state["engine"])
        prepare.assert_called_once_with("CPU 40%")
        piper.assert_called_once_with("готовый текст", 0, 100)

    def test_system_fallback_receives_original_text(self):
        with patch.object(pc, "speech_engine", return_value="piper"), \
             patch.object(pc.tts_quality, "prepare_tts_text", return_value="си-пи-ю сорок процентов"), \
             patch.object(pc, "_piper_speak", return_value=({}, "model failed")), \
             patch.object(pc, "_system_speech_engine", return_value="sapi"), \
             patch.object(pc, "_system_speak", return_value=({"engine": "sapi"}, "")) as system:
            state, reason = pc.speak("CPU 40%")
        self.assertEqual("", reason)
        self.assertEqual("sapi", state["engine"])
        system.assert_called_once_with("CPU 40%", 0, 100, engine="sapi")


if __name__ == "__main__":
    unittest.main()

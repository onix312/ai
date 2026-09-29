"""Unit tests for the extracted Luma TTS settings service."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from agent import tts_service


class _Store:
    def __init__(self):
        self.values = {}
        self.deleted = []

    def get_preference(self, key):
        value = self.values.get(key)
        return {"value": value} if value is not None else None

    def set_preference(self, key, value):
        self.values[key] = value

    def delete_preference(self, key):
        self.deleted.append(key)
        self.values.pop(key, None)


class TtsServiceTests(unittest.TestCase):
    def test_restore_reads_saved_preferences_once(self):
        store = _Store()
        store.values.update({
            "tts.piper": "piper.exe",
            "tts.model": "luma.onnx",
            "tts.speaker": "2",
        })
        service = tts_service.TtsService(store)
        with patch.object(tts_service.pc, "configure_tts", return_value={"ok": True}) as configure:
            service.restore()
            service.restore()
        configure.assert_called_once_with(
            piper="piper.exe", model="luma.onnx", speaker="2"
        )

    def test_update_persists_validated_values(self):
        store = _Store()
        service = tts_service.TtsService(store)
        with patch.object(tts_service.pc, "configure_tts", return_value={"ok": True, "engine": "piper"}):
            result = service.update({
                "piper": "piper.exe",
                "model": "luma.onnx",
                "speaker": "",
            })
        self.assertTrue(result["ok"])
        self.assertEqual("piper.exe", store.values["tts.piper"])
        self.assertEqual("luma.onnx", store.values["tts.model"])
        self.assertIn("tts.speaker", store.deleted)

    def test_invalid_update_is_not_persisted(self):
        store = _Store()
        service = tts_service.TtsService(store)
        with patch.object(
            tts_service.pc,
            "configure_tts",
            side_effect=[{"ok": True}, {"ok": False, "reason": "bad model"}],
        ):
            result = service.update({"model": "bad.onnx"})
        self.assertFalse(result["ok"])
        self.assertEqual({}, store.values)

    def test_reset_clears_all_persisted_fields(self):
        store = _Store()
        store.values.update({
            "tts.piper": "piper.exe",
            "tts.model": "luma.onnx",
            "tts.speaker": "3",
        })
        service = tts_service.TtsService(store)
        with patch.object(tts_service.pc, "configure_tts", return_value={"ok": True}), \
             patch.object(tts_service.pc, "reset_tts_config", return_value={"ok": True}):
            result = service.update({"op": "reset"})
        self.assertTrue(result["ok"])
        self.assertEqual({}, store.values)
        self.assertEqual(
            ["tts.piper", "tts.model", "tts.speaker"],
            store.deleted,
        )

    def test_preview_keeps_existing_response_contract(self):
        store = _Store()
        service = tts_service.TtsService(store)
        with patch.object(tts_service.pc, "configure_tts", return_value={"ok": True}), \
             patch.object(tts_service.pc, "speak", return_value=({"engine": "piper"}, "")), \
             patch.object(tts_service.pc, "tts_status", return_value={"engine": "piper"}):
            result = service.update({"op": "test", "text": "Привет"})
        self.assertTrue(result["ok"])
        self.assertEqual("piper", result["speech"]["engine"])
        self.assertEqual("piper", result["engine"])


if __name__ == "__main__":
    unittest.main()

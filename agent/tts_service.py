"""TTS settings service for Luma.

Keeps persistence, pronunciation dictionary operations and preview dispatch out
of the HTTP server. The HTTP contract and pc.speak() runtime stay unchanged.
"""
from __future__ import annotations

from typing import Any

from . import pc, tts_quality


class TtsService:
    FIELDS = ("piper", "model", "speaker")

    def __init__(self, store: Any) -> None:
        self.store = store
        self._restored = False

    def restore(self) -> None:
        if self._restored:
            return
        self._restored = True
        try:
            values = {
                field: str(((self.store.get_preference(f"tts.{field}") or {}).get("value")) or "")
                for field in self.FIELDS
            }
            pc.configure_tts(**values)
        except Exception:
            # TTS preferences must never prevent the local agent from starting.
            pass

    def settings(self) -> dict[str, Any]:
        self.restore()
        return {"ok": True, **pc.tts_status()}

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.restore()
        op = str(payload.get("op") or "update").strip().casefold()

        if op == "pronunciations":
            return tts_quality.pronunciation_payload()
        if op == "pronunciation_set":
            return tts_quality.set_pronunciation(
                str(payload.get("source") or ""),
                str(payload.get("target") or ""),
            )
        if op == "pronunciation_delete":
            return tts_quality.delete_pronunciation(str(payload.get("source") or ""))
        if op == "test":
            phrase = str(
                payload.get("text") or "Привет. Я Люма. Проверяю локальный голос."
            ).strip()
            state, reason = pc.speak(phrase[:240])
            return {"ok": bool(state), "reason": reason, "speech": state, **pc.tts_status()}
        if op == "reset":
            result = pc.reset_tts_config()
            for field in self.FIELDS:
                try:
                    self.store.delete_preference(f"tts.{field}")
                except Exception:
                    pass
            return result

        values = {
            "piper": str(payload.get("piper") or "").strip(),
            "model": str(payload.get("model") or "").strip(),
            "speaker": str(payload.get("speaker") or "").strip(),
        }
        result = pc.configure_tts(**values)
        if not result.get("ok"):
            return result
        for field, value in values.items():
            if value:
                self.store.set_preference(f"tts.{field}", value)
            else:
                self.store.delete_preference(f"tts.{field}")
        return result

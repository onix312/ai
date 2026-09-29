"""Состояния нативного интерфейса."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class UiState:
    connected: bool = False
    assistant_state: str = "idle"
    armed: bool = False
    voice_enabled: bool = False
    model_ok: bool = False
    panel_ok: bool = False
    last_error: str = ""
    voice_partial: str = ""
    audio_level: int = 0
    streaming_asr: bool = False
    safety_stopped: bool = False
    activity_phase: str = "idle"
    activity_heard: str = ""
    activity_reply: str = ""
    activity_skill: str = ""
    activity_detail: str = ""
    activity_task_id: int = 0
    activity_active: bool = False
    activity_recent: list[dict[str, Any]] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)

    def apply_status(self, payload: dict[str, Any]) -> None:
        self.connected = bool(payload.get("ok"))
        voice = payload.get("voice") if isinstance(payload.get("voice"), dict) else {}
        self.armed = bool(voice.get("armed", payload.get("armed") or payload.get("wake_word")))
        self.voice_enabled = bool(voice.get("enabled", payload.get("voice_enabled", False)))
        self.voice_partial = str(voice.get("partial_phrase") or "")
        self.audio_level = max(0, int(voice.get("audio_level") or 0))
        self.streaming_asr = bool(voice.get("streaming_asr"))
        safety = payload.get("safety") if isinstance(payload.get("safety"), dict) else {}
        self.safety_stopped = bool(safety.get("stopped") or safety.get("latched"))
        activity = payload.get("activity") if isinstance(payload.get("activity"), dict) else {}
        current = activity.get("current") if isinstance(activity.get("current"), dict) else {}
        self.activity_phase = str(current.get("phase") or "idle")
        self.activity_heard = str(current.get("heard") or "")
        self.activity_reply = str(current.get("reply") or "")
        self.activity_skill = str(current.get("skill") or "")
        self.activity_detail = str(current.get("detail") or "")
        self.activity_task_id = int(current.get("task_id") or 0)
        self.activity_active = bool(current.get("active"))
        self.activity_recent = list(activity.get("recent") or [])[:4]
        voice_state = str(voice.get("state") or payload.get("voice_state") or "idle")
        if voice_state in ("idle", "listening", "thinking", "speaking", "error"):
            self.assistant_state = voice_state
        self.pending = list(payload.get("pending") or [])
        voice_error = str(voice.get("last_error") or "")
        if voice_state == "error" and voice_error:
            self.last_error = voice_error
        elif self.last_error and self.connected:
            self.last_error = ""

    def set_error(self, message: str) -> None:
        self.connected = False
        self.assistant_state = "error"
        self.last_error = str(message or "Неизвестная ошибка")

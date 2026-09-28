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
    pending: list[dict[str, Any]] = field(default_factory=list)

    def apply_status(self, payload: dict[str, Any]) -> None:
        self.connected = bool(payload.get("ok"))
        voice = payload.get("voice") if isinstance(payload.get("voice"), dict) else {}
        self.armed = bool(voice.get("armed", payload.get("armed") or payload.get("wake_word")))
        self.voice_enabled = bool(voice.get("enabled", payload.get("voice_enabled", False)))
        voice_state = str(voice.get("state") or payload.get("voice_state") or "idle")
        if voice_state in ("idle", "listening", "thinking", "speaking", "error"):
            self.assistant_state = voice_state
        self.pending = list(payload.get("pending") or [])
        if self.last_error and self.connected:
            self.last_error = ""

    def set_error(self, message: str) -> None:
        self.connected = False
        self.assistant_state = "error"
        self.last_error = str(message or "Неизвестная ошибка")

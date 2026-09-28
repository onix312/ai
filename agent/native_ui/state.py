"""Состояния нативного интерфейса."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class UiState:
    connected: bool = False
    assistant_state: str = "idle"
    armed: bool = False
    model_ok: bool = False
    panel_ok: bool = False
    last_error: str = ""
    pending: list[dict[str, Any]] = field(default_factory=list)

    def apply_status(self, payload: dict[str, Any]) -> None:
        self.connected = bool(payload.get("ok"))
        self.armed = bool(payload.get("armed") or payload.get("wake_word"))
        self.pending = list(payload.get("pending") or [])
        if self.last_error and self.connected:
            self.last_error = ""

    def set_error(self, message: str) -> None:
        self.connected = False
        self.assistant_state = "error"
        self.last_error = str(message or "Неизвестная ошибка")

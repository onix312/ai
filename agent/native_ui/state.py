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
    voice_error: str = ""
    voice_partial: str = ""
    audio_level: int = 0
    streaming_asr: bool = False
    echo_floor: int = 0
    echo_threshold: int = 0
    echo_suppressed: int = 0
    echo_gate_multiplier: float = 1.65
    echo_gate_margin: int = 180
    echo_floor_alpha: float = 0.22
    vad_threshold: int = 320
    asr_engine: str = ""
    vocabulary_count: int = 0
    tts_engine: str = ""
    tts_hq_local: bool = False
    tts_model: str = ""
    tts_model_path: str = ""
    tts_piper_path: str = ""
    tts_piper_model_path: str = ""
    tts_piper_speaker: str = ""
    tts_speaker: str = ""
    tts_sample_rate: int = 0
    tts_model_ready: bool = False
    tts_last_synth_ms: int = 0
    tts_last_chars: int = 0
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
        self.echo_floor = max(0, int(voice.get("echo_floor") or 0))
        self.echo_threshold = max(0, int(voice.get("echo_threshold") or 0))
        self.echo_suppressed = max(0, int(voice.get("echo_suppressed") or 0))
        self.echo_gate_multiplier = float(voice.get("echo_gate_multiplier") or 1.65)
        self.echo_gate_margin = max(0, int(voice.get("echo_gate_margin") or 0))
        self.echo_floor_alpha = float(voice.get("echo_floor_alpha") or 0.22)
        self.vad_threshold = max(0, int(voice.get("vad_threshold") or 0))
        self.asr_engine = str(voice.get("asr_engine") or "")
        self.vocabulary_count = max(0, int(voice.get("vocabulary_count") or 0))
        tts = payload.get("tts") if isinstance(payload.get("tts"), dict) else {}
        self.tts_engine = str(tts.get("engine") or "")
        self.tts_hq_local = bool(tts.get("hq_local"))
        self.tts_model = str(tts.get("model") or "")
        self.tts_model_path = str(tts.get("model_path") or "")
        self.tts_piper_path = str(tts.get("piper_path") or "")
        self.tts_piper_model_path = str(tts.get("piper_model_path") or "")
        self.tts_piper_speaker = str(tts.get("piper_speaker") or "")
        self.tts_speaker = str(tts.get("speaker") or "")
        self.tts_sample_rate = max(0, int(tts.get("sample_rate") or 0))
        self.tts_model_ready = bool(tts.get("model_ready"))
        self.tts_last_synth_ms = max(0, int(tts.get("last_synth_ms") or 0))
        self.tts_last_chars = max(0, int(tts.get("last_chars") or 0))
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
            self.assistant_state = "idle" if voice_state == "error" and not self.voice_enabled else voice_state
        self.pending = list(payload.get("pending") or [])
        self.voice_error = str(voice.get("last_error") or "")
        if voice_state == "error" and self.voice_enabled and self.voice_error:
            self.last_error = self.voice_error
        elif self.last_error and self.connected:
            self.last_error = ""

    def set_error(self, message: str) -> None:
        self.connected = False
        self.assistant_state = "error"
        self.last_error = str(message or "Неизвестная ошибка")

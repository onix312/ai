"""Voice Engine 2.0: локальный wake word, VAD, разговорное окно и barge-in.

Аудио не сохраняется. В idle поток нужен только для локального обнаружения речи
и стоп-слова. Полный текст попадает в Brain лишь после wake word или пока открыта
короткая разговорная сессия.
"""
from __future__ import annotations

import queue
import struct
from collections import deque
import threading
import time
from typing import Any

from . import config, speech


STOP_WORDS = frozenset(("стоп", "хватит", "тихо", "замолчи", "остановись"))
END_WORDS = frozenset(("всё", "все", "отбой"))


def normalized_words(text: str) -> tuple[str, ...]:
    return tuple(
        word.strip(".,!?;:«»\"'()—-").casefold()
        for word in str(text or "").split()
        if word.strip(".,!?;:«»\"'()—-")
    )


def is_stop_phrase(text: str) -> bool:
    words = normalized_words(text)
    return bool(words) and any(word in STOP_WORDS for word in words[:3])


def is_end_phrase(text: str) -> bool:
    words = normalized_words(text)
    return bool(words) and len(words) <= 3 and any(word in END_WORDS for word in words)


def peak_level(data: bytes) -> int:
    count = len(data) // 2
    if not count:
        return 0
    samples = struct.unpack(f"<{count}h", data[:count * 2])
    return max((abs(value) for value in samples), default=0)


class VoiceRuntime:
    """Один владелец микрофона для постоянного и ручного голосового режима."""

    def __init__(self, recognizer: speech.Recognizer) -> None:
        self.recognizer = recognizer
        self.handler: Any = None
        self.cancel_handler: Any = None
        self.vocabulary_provider: Any = None
        self.vocabulary_terms: list[str] = []
        self.persistent_enabled = False
        self.manual_until = 0.0
        self.conversation_until = 0.0
        self.listening = False
        self.last_phrase = ""
        self.partial_phrase = ""
        self.audio_level = 0
        self.echo_floor = 0.0
        self.echo_threshold = int(config.VOICE_VAD_THRESHOLD)
        self.echo_suppressed = 0
        self.echo_gate_multiplier = float(config.VOICE_ECHO_GATE_MULTIPLIER)
        self.echo_gate_margin = int(config.VOICE_ECHO_GATE_MARGIN)
        self.echo_floor_alpha = float(config.VOICE_ECHO_FLOOR_ALPHA)
        self.streaming_asr = False
        self.last_error = ""
        self.state = "idle"
        self._thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._lock = threading.RLock()
        self._handler_busy = False

    @property
    def armed(self) -> bool:
        return bool(
            self.persistent_enabled
            or time.time() < self.manual_until
            or time.time() < self.conversation_until
        )

    @property
    def armed_until(self) -> float:
        """Совместимость со старым /mic/arm."""
        return max(self.manual_until, self.conversation_until)

    @property
    def conversation_active(self) -> bool:
        return time.time() < self.conversation_until

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.persistent_enabled),
            "armed": self.armed,
            "listening": bool(self.listening),
            "state": self._current_state(),
            "conversation_active": self.conversation_active,
            "conversation_until": self.conversation_until,
            "wake_word": config.WAKE_WORD,
            "last_phrase": self.last_phrase,
            "partial_phrase": self.partial_phrase,
            "audio_level": int(self.audio_level),
            "echo_floor": int(self.echo_floor),
            "echo_threshold": int(self.echo_threshold),
            "echo_suppressed": int(self.echo_suppressed),
            "echo_gate_multiplier": float(self.echo_gate_multiplier),
            "echo_gate_margin": int(self.echo_gate_margin),
            "echo_floor_alpha": float(self.echo_floor_alpha),
            "vad_threshold": int(config.VOICE_VAD_THRESHOLD),
            "streaming_asr": bool(self.streaming_asr),
            "asr_engine": str(getattr(self.recognizer, "name", "") or ""),
            "vocabulary_count": len(self.vocabulary_terms),
            "last_error": self.last_error,
        }

    def enable(self) -> tuple[bool, str]:
        """Включить постоянный локальный wake-word режим."""
        ok, reason = self._prepare()
        if not ok:
            return False, reason
        with self._lock:
            self.persistent_enabled = True
            self._shutdown.clear()
            self.state = "idle"
            self.last_error = ""
            self._ensure_thread()
        return True, ""

    def disable(self) -> None:
        """Полностью закрыть постоянный микрофон и разговорную сессию."""
        with self._lock:
            self.persistent_enabled = False
            self.manual_until = 0.0
            self.conversation_until = 0.0
            self.state = "idle"
            self.last_error = ""
            self._shutdown.set()

    def arm(self, seconds: float = config.MIC_ARM_SECONDS) -> tuple[bool, str]:
        """Совместимый ручной режим: слушать wake word ограниченное время."""
        ok, reason = self._prepare()
        if not ok:
            return False, reason
        with self._lock:
            self.manual_until = time.time() + max(3.0, float(seconds))
            self._shutdown.clear()
            self.state = "listening"
            self.last_error = ""
            self._ensure_thread()
        return True, ""

    def disarm(self) -> None:
        """Закрыть ручное окно; persistent режим, если включён, остаётся."""
        with self._lock:
            self.manual_until = 0.0
            self.conversation_until = 0.0
            self.state = "idle"
            if not self.persistent_enabled:
                self._shutdown.set()

    def shutdown(self) -> None:
        self.disable()
        try:
            from . import pc
            pc.stop_speaking()
        except Exception:
            pass

    def tune(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Adjust lightweight echo-gate tuning at runtime. RAM-only."""
        data = payload or {}
        try:
            if "multiplier" in data:
                self.echo_gate_multiplier = min(4.0, max(1.0, float(data["multiplier"])))
            if "margin" in data:
                self.echo_gate_margin = min(4000, max(0, int(data["margin"])))
            if "alpha" in data:
                self.echo_floor_alpha = min(0.95, max(0.05, float(data["alpha"])))
        except (TypeError, ValueError):
            return {"ok": False, "reason": "Некорректные параметры Voice Diagnostics", **self.status()}
        return {"ok": True, **self.status()}

    def reset_diagnostics(self) -> dict[str, Any]:
        """Reset learned acoustic floor/counters and restore default tuning."""
        self.echo_floor = 0.0
        self.echo_threshold = int(config.VOICE_VAD_THRESHOLD)
        self.echo_suppressed = 0
        self.echo_gate_multiplier = float(config.VOICE_ECHO_GATE_MULTIPLIER)
        self.echo_gate_margin = int(config.VOICE_ECHO_GATE_MARGIN)
        self.echo_floor_alpha = float(config.VOICE_ECHO_FLOOR_ALPHA)
        return {"ok": True, **self.status()}

    def stop_output(self) -> dict[str, Any]:
        from . import pc
        stopped = pc.stop_speaking()
        generation_cancelled = False
        if callable(self.cancel_handler):
            try:
                generation_cancelled = bool(self.cancel_handler())
            except Exception:
                generation_cancelled = False
        if self.persistent_enabled or time.time() < self.manual_until or self.conversation_active:
            self.conversation_until = time.time() + config.VOICE_FOLLOWUP_SECONDS
            self.state = "listening"
        else:
            self.conversation_until = 0.0
            self.state = "idle"
        return {"ok": True, "stopped": stopped,
                "generation_cancelled": generation_cancelled, **self.status()}

    def _prepare(self) -> tuple[bool, str]:
        from . import capabilities
        state = capabilities.detect()
        if not state.get("microphone"):
            reason = str(state.get("microphone_reason") or "Микрофон недоступен")
            self.last_error = reason
            self.state = "error"
            return False, reason
        if not self.recognizer.load():
            reason = self.recognizer.reason or "Модель речи не загрузилась"
            self.last_error = reason
            self.state = "error"
            return False, reason
        return True, ""

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._listen, daemon=True, name="nozza-voice-runtime"
            )
            self._thread.start()

    def _should_run(self) -> bool:
        return self.armed and not self._shutdown.is_set()

    def _listen(self) -> None:
        try:
            import sounddevice as sd  # type: ignore
        except ImportError:
            self.last_error = "Нет sounddevice — микрофон недоступен"
            self.state = "error"
            return

        frames: queue.Queue[bytes] = queue.Queue()
        self.listening = True
        try:
            with sd.RawInputStream(
                samplerate=16000,
                channels=1,
                dtype="int16",
                blocksize=1600,
                callback=lambda data, *_a: frames.put(bytes(data)),
            ):
                self.last_error = ""
                if self.state == "error":
                    self.state = "idle"
                while self._should_run():
                    audio, during_output, streamed_text = self._next_phrase(frames)
                    self._expire_conversation()
                    if not audio:
                        continue
                    if streamed_text:
                        text, reason = streamed_text, ""
                    else:
                        text, reason = self.recognizer.transcribe_wav(
                            speech.pack_wav(audio), config.LANGUAGE
                        )
                    try:
                        terms = self.vocabulary_provider() if callable(self.vocabulary_provider) else []
                        self.vocabulary_terms = speech.normalize_vocabulary(terms)
                        text = speech.apply_dynamic_vocabulary(text, self.vocabulary_terms)
                    except Exception:
                        self.vocabulary_terms = []
                    self.partial_phrase = ""
                    if reason:
                        self.last_error = reason
                        continue
                    self.last_error = ""
                    self.last_phrase = text
                    self._handle_text(text, during_output=during_output)
        except OSError as exc:
            self.last_error = f"Микрофон не открылся: {exc}"
            self.state = "error"
        finally:
            self.listening = False
            self.audio_level = 0
            self.partial_phrase = ""
            self.streaming_asr = False
            self._thread = None
            if self.persistent_enabled and self.state == "error":
                self._schedule_retry()
            elif not self.persistent_enabled:
                self.state = "idle"

    def _schedule_retry(self) -> None:
        """Переподключить микрофон после временного отказа устройства."""
        def retry() -> None:
            if not self.persistent_enabled:
                return
            self._shutdown.clear()
            self.state = "idle"
            self._ensure_thread()

        timer = threading.Timer(2.0, retry)
        timer.daemon = True
        timer.start()

    def _next_phrase(self, frames: "queue.Queue[bytes]") -> tuple[list[bytes], bool, str]:
        """VAD + RAM pre-roll + incremental Vosk when available.

        Partial hypotheses are exposed through status for UI only. Brain still
        receives exactly one finalized phrase, so streaming cannot execute a
        half-heard command.
        """
        from . import pc

        chunks: list[bytes] = []
        pre_roll: deque[bytes] = deque(maxlen=max(0, int(config.VOICE_PREROLL_CHUNKS)))
        started = False
        silence = 0.0
        started_at = 0.0
        during_output = False
        stream: Any = None
        self.partial_phrase = ""
        self.streaming_asr = False

        while self._should_run():
            try:
                data = frames.get(timeout=0.2)
            except queue.Empty:
                self.audio_level = 0
                self._expire_conversation()
                continue

            level = peak_level(data)
            self.audio_level = level
            try:
                speaking_now = pc.is_speaking()
            except Exception:
                speaking_now = False
            threshold = int(config.VOICE_VAD_THRESHOLD)
            if speaking_now and not started and self.echo_floor > 0:
                threshold = max(
                    threshold,
                    int(self.echo_floor * float(self.echo_gate_multiplier)),
                    int(self.echo_floor + int(self.echo_gate_margin)),
                )
            self.echo_threshold = threshold
            loud = level >= threshold
            if speaking_now and not started:
                if not loud:
                    alpha = float(self.echo_floor_alpha)
                    if self.echo_floor <= 0:
                        self.echo_floor = float(level)
                    else:
                        self.echo_floor = (1.0 - alpha) * self.echo_floor + alpha * float(level)
                    if level >= config.VOICE_VAD_THRESHOLD:
                        self.echo_suppressed += 1
            elif not started:
                self.echo_floor *= 0.92

            if not started and not loud:
                if speaking_now:
                    # Suppressed speaker leakage must not ride into ASR as
                    # pre-roll when the owner starts speaking over TTS.
                    pre_roll.clear()
                else:
                    pre_roll.append(data)
                continue

            if loud and not started:
                started = True
                started_at = time.time()
                try:
                    during_output = pc.is_speaking()
                except Exception:
                    during_output = False

                if pre_roll:
                    chunks.extend(pre_roll)
                chunks.append(data)
                try:
                    stream = self.recognizer.start_stream(16000)
                except (AttributeError, TypeError):
                    stream = None
                self.streaming_asr = stream is not None
                if stream is not None:
                    for chunk in chunks:
                        partial = str(stream.feed(chunk) or "").strip()
                    if not during_output and len(partial) >= config.VOICE_PARTIAL_MIN_CHARS:
                        self.partial_phrase = partial
                silence = 0.0
                continue

            chunks.append(data)
            if stream is not None:
                partial = str(stream.feed(data) or "").strip()
                if not during_output and len(partial) >= config.VOICE_PARTIAL_MIN_CHARS:
                    self.partial_phrase = partial

            if loud:
                silence = 0.0
            else:
                silence += 0.1
                # Не зависеть от двоичного представления 0.1: восемь 100-ms
                # чанков должны ровно закрывать стандартные 0.8 s паузы.
                if silence + 1e-9 >= config.VOICE_SILENCE_SECONDS:
                    break
            if started and time.time() - started_at >= config.VOICE_MAX_PHRASE_SECONDS:
                break

        self.audio_level = 0
        self.echo_threshold = int(config.VOICE_VAD_THRESHOLD)
        final = ""
        if stream is not None:
            try:
                final = str(stream.finish() or "").strip()
            except (RuntimeError, ValueError):
                final = ""
        self.partial_phrase = final or self.partial_phrase
        return chunks, during_output, final

    def _handle_text(self, text: str, during_output: bool = False) -> None:
        clean = " ".join(str(text or "").split())
        if not clean:
            return
        from . import pc

        # Во время собственного TTS принимаем stop и явное обращение к Люме,
        # но отбрасываем фразу, если она похожа на недавно произнесённый TTS.
        if during_output or pc.is_speaking():
            if is_stop_phrase(clean):
                pc.stop_speaking()
                self.conversation_until = time.time() + config.VOICE_FOLLOWUP_SECONDS
                self.state = "listening"
                return
            if speech.is_wake_phrase(clean) and not pc.recent_tts_echo(clean):
                pc.stop_speaking()
                self.conversation_until = time.time() + config.VOICE_FOLLOWUP_SECONDS
                phrase = speech.phrase_after_wake_word(clean)
                if phrase:
                    self._dispatch(phrase)
                else:
                    self.state = "listening"
            return

        if self._handler_busy:
            if is_stop_phrase(clean):
                cancelled = False
                if callable(self.cancel_handler):
                    try:
                        cancelled = bool(self.cancel_handler())
                    except Exception:
                        cancelled = False
                if cancelled:
                    self.conversation_until = time.time() + config.VOICE_FOLLOWUP_SECONDS
                    self.state = "listening"
            return

        wake = speech.is_wake_phrase(clean)
        if wake:
            self.conversation_until = time.time() + config.VOICE_FOLLOWUP_SECONDS
            phrase = speech.phrase_after_wake_word(clean)
            if phrase:
                self._dispatch(phrase)
            else:
                self.state = "listening"
            return

        if self.conversation_active:
            if is_end_phrase(clean):
                self.conversation_until = 0.0
                self.state = "idle"
                return
            self._dispatch(clean)

    def _dispatch(self, phrase: str) -> None:
        if not callable(self.handler) or self._handler_busy:
            return
        self._handler_busy = True
        self.state = "thinking"

        def work() -> None:
            try:
                self.handler(phrase)
            except Exception as exc:
                self.last_error = f"Фраза не обработана: {exc.__class__.__name__}"
                self.state = "error"
            finally:
                self._handler_busy = False
                self.conversation_until = time.time() + config.VOICE_FOLLOWUP_SECONDS
                try:
                    from . import pc
                    speaking = pc.is_speaking()
                except Exception:
                    speaking = False
                self.state = "speaking" if speaking else "listening"
                if speaking:
                    threading.Thread(
                        target=self._watch_output,
                        daemon=True,
                        name="nozza-voice-output-watch",
                    ).start()

        threading.Thread(target=work, daemon=True, name="nozza-voice-dispatch").start()

    def _watch_output(self) -> None:
        from . import pc
        while pc.is_speaking():
            time.sleep(0.08)
        # Follow-up отсчитывается от конца ответа, а не от запуска TTS.
        self.conversation_until = time.time() + config.VOICE_FOLLOWUP_SECONDS
        self.state = "listening"

    def _expire_conversation(self, force_refresh: bool = False) -> None:
        if self._handler_busy:
            return
        if self.state == "error" and self.last_error:
            return
        if self.conversation_active:
            if force_refresh or self.state not in ("listening", "speaking"):
                self.state = "listening"
            return
        if self.persistent_enabled:
            self.state = "idle"
        elif time.time() < self.manual_until:
            self.state = "listening"
        else:
            self.state = "idle"

    def _current_state(self) -> str:
        self._expire_conversation()
        return self.state

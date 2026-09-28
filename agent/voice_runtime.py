"""Voice Engine 2.0: локальный wake word, VAD, разговорное окно и barge-in.

Аудио не сохраняется. В idle поток нужен только для локального обнаружения речи
и стоп-слова. Полный текст попадает в Brain лишь после wake word или пока открыта
короткая разговорная сессия.
"""
from __future__ import annotations

import queue
import struct
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
        self.persistent_enabled = False
        self.manual_until = 0.0
        self.conversation_until = 0.0
        self.listening = False
        self.last_phrase = ""
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

    def stop_output(self) -> dict[str, Any]:
        from . import pc
        stopped = pc.stop_speaking()
        if self.persistent_enabled or time.time() < self.manual_until:
            self.conversation_until = time.time() + config.VOICE_FOLLOWUP_SECONDS
            self.state = "listening"
        else:
            self.conversation_until = 0.0
            self.state = "idle"
        return {"ok": True, "stopped": stopped, **self.status()}

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
                    audio, during_output = self._next_phrase(frames)
                    self._expire_conversation()
                    if not audio:
                        continue
                    text, reason = self.recognizer.transcribe_wav(
                        speech.pack_wav(audio), config.LANGUAGE
                    )
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

    def _next_phrase(self, frames: "queue.Queue[bytes]") -> tuple[list[bytes], bool]:
        """Простой VAD; помечает звук, начавшийся во время TTS, против эха."""
        chunks: list[bytes] = []
        started = False
        silence = 0.0
        started_at = 0.0
        during_output = False
        while self._should_run():
            try:
                data = frames.get(timeout=0.2)
            except queue.Empty:
                self._expire_conversation()
                continue
            loud = peak_level(data) >= config.VOICE_VAD_THRESHOLD
            if loud:
                if not started:
                    started = True
                    started_at = time.time()
                    try:
                        from . import pc
                        during_output = pc.is_speaking()
                    except Exception:
                        during_output = False
                silence = 0.0
                chunks.append(data)
            elif started:
                chunks.append(data)
                silence += 0.1
                if silence >= config.VOICE_SILENCE_SECONDS:
                    break
            if started and time.time() - started_at >= config.VOICE_MAX_PHRASE_SECONDS:
                break
        return chunks, during_output

    def _handle_text(self, text: str, during_output: bool = False) -> None:
        clean = " ".join(str(text or "").split())
        if not clean:
            return
        from . import pc

        # Пока NOZZA говорит, собственный голос не считается новой командой.
        # Слушается только явный barge-in.
        if during_output or pc.is_speaking():
            if is_stop_phrase(clean):
                pc.stop_speaking()
                self.conversation_until = time.time() + config.VOICE_FOLLOWUP_SECONDS
                self.state = "listening"
            return

        if self._handler_busy:
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
        self._expire_conversation(force_refresh=True)

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

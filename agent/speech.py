"""Речь на процессоре: запись → текст, стоп-слово → фраза.

Внешние библиотеки (vosk, faster-whisper, sounddevice) импортируются лениво:
без них агент запускается и честно отвечает «распознавание недоступно», а не
падает при старте. Видеопамять не трогается вовсе — маленькая модель речи
считается на процессоре, пока модель помощника и WebGPU-нарезка делят 8 ГБ.

Стоп-слово устроено как включение, а не как прослушка: микрофон открывается
только после явного действия человека (горячая клавиша, кнопка агента или
`POST /mic/arm`) и закрывается через `MIC_ARM_SECONDS`. Постоянно открытый
микрофон исключён решением владельца.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.request
from typing import Any

from . import config

# Порог похожести стоп-слова: распознавание на процессоре ошибается в одной
# букве routinely, а требование точного совпадения превратило бы голос в лотерею.
WAKE_WORD_TOLERANCE = 1


def _levenshtein(left: str, right: str) -> int:
    """Расстояние между словами — чтобы «нозза» слышалось как «ноза»."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for index, char in enumerate(left, start=1):
        current = [index]
        for position, other in enumerate(right, start=1):
            current.append(min(previous[position] + 1, current[position - 1] + 1,
                               previous[position - 1] + (char != other)))
        previous = current
    return previous[-1]


def is_wake_phrase(text: str, wake_word: str = config.WAKE_WORD,
                   tolerance: int = WAKE_WORD_TOLERANCE) -> bool:
    """Стоп-слово в фразе: отдельным словом, с одной ошибкой распознавания."""
    target = str(wake_word or "").strip().lower()
    if not target:
        return False
    if any(_levenshtein(word, target) <= tolerance for word in _words(text) if word):
        return True
    # Воспроизведение вслух двух коротких слов («но за») распознаватель
    # нередко склеивает в одно — такое совпадение тоже считаем стоп-словом.
    joined = "".join(_words(text))
    return bool(target) and target in joined


def phrase_after_wake_word(text: str, wake_word: str = config.WAKE_WORD,
                           tolerance: int = WAKE_WORD_TOLERANCE) -> str:
    """Команда без стоп-слова: «ноза, что печатается» → «что печатается»."""
    target = str(wake_word or "").strip().lower()
    words = str(text or "").split()
    for index, word in enumerate(words):
        cleaned = _clean(word)
        if cleaned and _levenshtein(cleaned, target) <= tolerance:
            return " ".join(words[index + 1:]).strip()
        if target and target in cleaned:
            rest = cleaned.split(target, 1)[1]
            tail = " ".join(words[index + 1:])
            return (rest + " " + tail).strip()
    return str(text or "").strip()


_PUNCTUATION = ".,!?;:«»\"'()—"


def _clean(word: str) -> str:
    return str(word or "").strip(_PUNCTUATION).lower()


def _words(text: str) -> list[str]:
    """Слова фразы без знаков препинания: «но за,» — это «но за»."""
    return [_clean(word) for word in str(text or "").split()]


class Recognizer:
    """Один рантайм речи на процессоре. Модель грузится при первом обращении."""

    def __init__(self, model_path: str = "") -> None:
        self.model_path = model_path
        self._model: Any = None
        self._engine = ""
        self._lock = threading.Lock()
        self.reason = ""

    @property
    def name(self) -> str:
        return self._engine or "нет"

    @property
    def loaded(self) -> bool:
        """Модель уже в памяти: для статуса, где грузить модель нельзя."""
        return self._model is not None

    def load(self) -> bool:
        """Загрузить модель. Отказ — с причиной, а не исключением наружу."""
        with self._lock:
            if self._model is not None:
                return True
            try:
                from vosk import Model, SetLogLevel  # type: ignore

                SetLogLevel(-1)
                if not self.model_path:
                    self.reason = "Не указан путь к модели vosk"
                    return False
                self._model = Model(self.model_path)
                self._engine = "vosk"
                self.reason = ""
                return True
            except ImportError:
                pass
            except OSError as exc:
                self.reason = f"Модель речи не загрузилась: {exc}"
                return False
            try:
                from faster_whisper import WhisperModel  # type: ignore

                self._model = WhisperModel(self.model_path or "tiny", device="cpu",
                                           compute_type="int8")
                self._engine = "faster-whisper"
                self.reason = ""
                return True
            except ImportError:
                self.reason = ("Нет vosk и нет faster-whisper: установите зависимости "
                               "агента (agent/README.md)")
                return False
            except OSError as exc:
                self.reason = f"Модель речи не загрузилась: {exc}"
                return False

    def transcribe_wav(self, audio: bytes, language: str = config.LANGUAGE) -> tuple[str, str]:
        """WAV 16 кГц моно → текст. Пустой текст возвращается с причиной."""
        if not audio:
            return "", "Пустая запись"
        if not self.load():
            return "", self.reason
        try:
            if self._engine == "vosk":
                return self._vosk(audio), ""
            return self._whisper(audio, language)
        except (OSError, ValueError, RuntimeError) as exc:
            return "", f"Распознавание не удалось: {exc}"

    def _vosk(self, audio: bytes) -> str:
        import wave
        import io

        from vosk import KaldiRecognizer  # type: ignore

        with wave.open(io.BytesIO(audio)) as stream:
            recognizer = KaldiRecognizer(self._model, stream.getframerate())
            recognizer.SetWords(False)
            text: list[str] = []
            while True:
                chunk = stream.readframes(4000)
                if not chunk:
                    break
                if recognizer.AcceptWaveform(chunk):
                    text.append(json.loads(recognizer.Result() or "{}").get("text", ""))
            text.append(json.loads(recognizer.FinalResult() or "{}").get("text", ""))
        return " ".join(part for part in text if part).strip()

    def _whisper(self, audio: bytes, language: str) -> tuple[str, str]:
        import tempfile
        import pathlib

        # faster-whisper читает файл, а не байты: кладём запись во временный
        # каталог и сразу удаляем — голос не остаётся на диске.
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "phrase.wav"
            path.write_bytes(audio)
            segments, _info = self._model.transcribe(str(path), language=language or None,
                                                     beam_size=1)
            text = " ".join(str(segment.text) for segment in segments).strip()
        return text, ""


class Microphone:
    """Микрофон, который открывается только на время фразы."""

    def __init__(self, recognizer: Recognizer) -> None:
        self.recognizer = recognizer
        self.armed_until = 0.0
        self.listening = False
        self.last_phrase = ""
        self.last_error = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # 18.21: фраза после стоп-слова сначала идёт мозгу агента (команды
        # компьютеру исполняются сразу и ответ звучит вслух), а вопросы цеха —
        # в панель. Без обработчика — прежний путь: только в журнал панели.
        self.handler: Any = None

    def arm(self, seconds: float = config.MIC_ARM_SECONDS) -> tuple[bool, str]:
        """Включить прослушивание на короткое окно времени."""
        from . import capabilities

        state = capabilities.detect()
        if not state["microphone"]:
            return False, state["microphone_reason"]
        if not self.recognizer.load():
            return False, self.recognizer.reason
        self.armed_until = time.time() + max(3.0, float(seconds))
        self._stop.clear()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._listen, daemon=True,
                                            name="agent-microphone")
            self._thread.start()
        return True, ""

    def disarm(self) -> None:
        """Закрыть микрофон немедленно."""
        self.armed_until = 0.0
        self._stop.set()

    @property
    def armed(self) -> bool:
        return time.time() < self.armed_until and not self._stop.is_set()

    def _listen(self) -> None:
        """Поток микрофона: одна фраза, одна расшифровка, микрофон закрыт."""
        try:
            import sounddevice as sd  # type: ignore
        except ImportError:
            self.last_error = "Нет sounddevice — микрофон недоступен"
            self.disarm()
            return
        frames: queue.Queue = queue.Queue()
        self.listening = True
        try:
            with sd.RawInputStream(samplerate=16000, channels=1, dtype="int16",
                                   blocksize=4000, callback=lambda data, *_a: frames.put(data)):
                while self.armed and not self._stop.is_set():
                    try:
                        frames.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    # Фраза заканчивается паузой: собираем звук, пока человек
                    # говорит, и отдаём на распознавание после тишины.
                    audio = self._collect(frames, sd)
                    if not audio:
                        continue
                    text, reason = self.recognizer.transcribe_wav(audio)
                    if reason:
                        self.last_error = reason
                        continue
                    self.last_phrase = text
                    self.last_error = ""
                    if is_wake_phrase(text):
                        self.on_phrase(phrase_after_wake_word(text))
                    self.disarm()
                    break
        except OSError as exc:
            self.last_error = f"Микрофон не открылся: {exc}"
        finally:
            self.listening = False
            self.disarm()

    def _collect(self, frames: "queue.Queue", sd: Any) -> bytes:
        """Собрать запись до паузы и упаковать в WAV 16 кГц моно."""
        import io
        import wave

        chunks: list[bytes] = []
        silence = 0.0
        started = False
        deadline = time.time() + min(20.0, max(3.0, self.armed_until - time.time()))
        while time.time() < deadline and self.armed:
            try:
                chunk = frames.get(timeout=0.2)
            except queue.Empty:
                if started:
                    silence += 0.2
                    if silence > 0.9:
                        break
                continue
            data = bytes(chunk)
            peak = max((abs(sample) for sample in _int16_samples(data)), default=0)
            if peak > 320:
                started = True
                silence = 0.0
                chunks.append(data)
            elif started:
                silence += 0.2
                chunks.append(data)
                if silence > 0.9:
                    break
        if not chunks:
            return b""
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b"".join(chunks))
        return buffer.getvalue()

    def on_phrase(self, phrase: str) -> None:
        """Команда после стоп-слова уходит в панель помощника по loopback.

        Агент не выполняет её сам: решение и подтверждение остаются в PrintFlow,
        где деньги и печать требуют кнопки «Подтвердить».
        """
        if not phrase:
            return
        if callable(self.handler):
            try:
                self.handler(phrase)
                return
            except Exception as exc:  # голос не должен ронять поток микрофона
                self.last_error = f"Фраза не обработана: {exc.__class__.__name__}"
        payload = json.dumps({"text": phrase, "source": "agent-wake-word"},
                             ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{config.PRINTFLOW_URL}/api/assistant/phrase", data=payload, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "PrintFlow-agent/1"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read(64 * 1024)
        except (OSError, ValueError):
            # Панель может быть выключена: агент из-за этого не падает и не
            # повторяет команду в очередь — голос не должен жить дольше окна.
            self.last_error = "Панель PrintFlow не ответила на фразу"


def _int16_samples(data: bytes):
    import struct

    count = len(data) // 2
    return struct.unpack(f"<{count}h", data[:count * 2]) if count else ()


def pack_wav(chunks: list[bytes], rate: int = 16000) -> bytes:
    """Сырые кадры int16 моно → WAV в памяти (голос на диск не пишется)."""
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"".join(chunks))
    return buffer.getvalue()


def record_and_transcribe(recognizer: Recognizer, seconds: float) -> tuple[str, str]:
    """Открыть микрофон на `seconds`, записать и распознать. Микрофон закрывается сам.

    Нужен навыкам `voice.listen` и `voice.dictate` (18.21): раньше они
    отвечали «требует зависимостей» даже там, где vosk и sounddevice стоят.
    """
    try:
        import sounddevice as sd  # type: ignore
    except ImportError:
        return "", "Нет sounddevice — микрофон недоступен (python -m agent.install --install)"
    if not recognizer.load():
        return "", recognizer.reason or "Модель речи не загружена"
    frames: queue.Queue = queue.Queue()
    chunks: list[bytes] = []
    deadline = time.time() + max(1.0, min(30.0, float(seconds)))
    try:
        with sd.RawInputStream(samplerate=16000, channels=1, dtype="int16", blocksize=4000,
                               callback=lambda data, *_a: frames.put(bytes(data))):
            while time.time() < deadline:
                try:
                    chunks.append(frames.get(timeout=0.2))
                except queue.Empty:
                    continue
    except OSError as exc:
        return "", f"Микрофон не открылся: {exc}"
    if not chunks:
        return "", "Микрофон ничего не записал"
    text, reason = recognizer.transcribe_wav(pack_wav(chunks))
    if reason:
        return "", reason
    return text, "" if text else "Речь не распознана"

"""Что агент умеет прямо сейчас — честно, без обещаний.

Панель помощника и `pf.py doctor` показывают этот список, поэтому отсутствующая
зависимость видна владельцу сразу: «нет OCR» вместо кнопки, которая молча ничего
не делает. Проверки идут один раз при старте и по запросу `/capabilities`.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys

WINDOWS = sys.platform.startswith("win")


def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def detect() -> dict:
    """Карта возможностей: что включено и почему."""
    capabilities = {
        "windows": WINDOWS,
        "windows_reason": "" if WINDOWS else "Управление окнами работает только в Windows",
        "active_window": WINDOWS,
        "click": WINDOWS,
        "type": WINDOWS,
        "screen": bool(WINDOWS and (_has("mss") or _has("PIL"))),
        "screen_reason": "" if WINDOWS and (_has("mss") or _has("PIL"))
        else ("Нет mss или Pillow — снимок экрана недоступен" if WINDOWS
              else "Снимок экрана работает только в Windows"),
        "ocr": bool(_has("rapidocr_onnxruntime") or _has("pytesseract")),
        "ocr_reason": "" if (_has("rapidocr_onnxruntime") or _has("pytesseract"))
        else "Нет OCR-библиотеки: агент показывает снимок человеку и не угадывает текст",
        "tesseract": bool(shutil.which("tesseract")),
        "speech": bool(_has("vosk") or _has("faster_whisper")),
        "speech_reason": "" if (_has("vosk") or _has("faster_whisper"))
        else "Нет vosk или faster-whisper — распознавание речи недоступно",
        "microphone": bool(_has("sounddevice") or _has("pyaudio")),
        "microphone_reason": "" if (_has("sounddevice") or _has("pyaudio"))
        else "Нет sounddevice или pyaudio — микрофон недоступен",
        "speech_model": "",
    }
    if capabilities["speech"]:
        capabilities["speech_model"] = _speech_model()
    capabilities["wake_word"] = bool(capabilities["speech"] and capabilities["microphone"])
    return capabilities


def _speech_model() -> str:
    """Какая модель речи найдена на диске. Ищем по обычным местам."""
    import pathlib

    candidates = [pathlib.Path("models"), pathlib.Path.home() / ".printflow" / "models",
                  pathlib.Path(__file__).resolve().parent / "models"]
    for folder in candidates:
        try:
            for child in sorted(folder.glob("*")):
                if child.is_dir() and (child / "am").is_dir():
                    return f"vosk:{child.name}"
                if child.suffix in (".bin", ".onnx") and "whisper" in child.name.lower():
                    return f"whisper:{child.name}"
        except OSError:
            continue
    return ""


def missing(capabilities: dict) -> list[str]:
    """Причины, по которым агент не умеет того, что обещает панель."""
    return [str(capabilities[key]) for key in ("windows_reason", "screen_reason",
                                               "ocr_reason", "speech_reason",
                                               "microphone_reason")
            if capabilities.get(key)]

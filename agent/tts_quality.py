"""Text preparation for Luma HQ TTS.

This module is deliberately pure: no audio, no subprocesses, no UI.
It turns assistant text into a pronunciation-friendly Russian string before
Piper sees it. System-TTS fallback keeps the original text.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from functools import lru_cache
from typing import Mapping


_BUILTIN: dict[str, str] = {
    "AI": "эй-ай",
    "API": "эй-пи-ай",
    "CPU": "си-пи-ю",
    "GPU": "джи-пи-ю",
    "RAM": "рэм",
    "SSD": "эс-эс-ди",
    "HDD": "эйч-ди-ди",
    "USB": "ю-эс-би",
    "UI": "ю-ай",
    "UX": "ю-экс",
    "HTTP": "эйч-ти-ти-пи",
    "HTTPS": "эйч-ти-ти-пи-эс",
    "JSON": "джейсон",
    "SQL": "эс-кью-эл",
    "Wi-Fi": "вай-фай",
    "WiFi": "вай-фай",
    "GitHub": "гитхаб",
    "PySide6": "пай-сайд шесть",
    "Piper": "пайпер",
    "Bambu": "бамбу",
    "Orca": "орка",
    "PLA": "пи-эл-эй",
    "PETG": "пи-и-ти-джи",
    "TPU": "ти-пи-ю",
    "ABS": "эй-би-эс",
    "P1S": "пи один эс",
}

_ONES = (
    "ноль", "один", "два", "три", "четыре", "пять",
    "шесть", "семь", "восемь", "девять", "десять",
    "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
    "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать",
    "девятнадцать",
)
_TENS = ("", "", "двадцать", "тридцать", "сорок", "пятьдесят",
         "шестьдесят", "семьдесят", "восемьдесят", "девяносто")
_HUNDREDS = ("", "сто", "двести", "триста", "четыреста", "пятьсот",
             "шестьсот", "семьсот", "восемьсот", "девятьсот")


def _number_ru(value: int) -> str:
    if value < 0:
        return "минус " + _number_ru(-value)
    if value < 20:
        return _ONES[value]
    if value < 100:
        tens, ones = divmod(value, 10)
        return _TENS[tens] + ((" " + _ONES[ones]) if ones else "")
    if value < 1000:
        hundreds, rest = divmod(value, 100)
        return _HUNDREDS[hundreds] + ((" " + _number_ru(rest)) if rest else "")
    if value < 10000:
        thousands, rest = divmod(value, 1000)
        if thousands == 1:
            head = "одна тысяча"
        elif thousands == 2:
            head = "две тысячи"
        elif thousands in (3, 4):
            head = _ONES[thousands] + " тысячи"
        else:
            head = _ONES[thousands] + " тысяч"
        return head + ((" " + _number_ru(rest)) if rest else "")
    return str(value)


def _default_dictionary_path() -> pathlib.Path:
    configured = os.environ.get("LUMA_TTS_PRONUNCIATIONS", "").strip()
    if configured:
        return pathlib.Path(configured).expanduser()
    return pathlib.Path.home() / ".printflow" / "tts-pronunciations.json"


@lru_cache(maxsize=8)
def _load_dictionary(path_text: str, mtime_ns: int) -> dict[str, str]:
    del mtime_ns
    path = pathlib.Path(path_text)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in payload.items():
        source = str(key or "").strip()
        target = str(value or "").strip()
        if source and target and len(source) <= 80 and len(target) <= 160:
            result[source] = target
    return result


def pronunciation_dictionary() -> dict[str, str]:
    result = dict(_BUILTIN)
    path = _default_dictionary_path()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = 0
    result.update(_load_dictionary(str(path), stamp))
    return result


def _apply_dictionary(text: str, words: Mapping[str, str]) -> str:
    if not words:
        return text
    result = text
    # Longest first prevents API from touching HTTPS-like larger entries.
    for source in sorted(words, key=len, reverse=True):
        target = words[source]
        pattern = re.compile(rf"(?<![\wА-Яа-яЁё]){re.escape(source)}(?![\wА-Яа-яЁё])", re.IGNORECASE)
        result = pattern.sub(target, result)
    return result


def _normalize_urls(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        value = match.group(0)
        value = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
        value = value.rstrip("/")
        value = value.replace(".", " точка ").replace("/", " слэш ")
        value = value.replace("-", " дефис ").replace("_", " подчёркивание ")
        return " ".join(value.split())
    return re.sub(r"https?://[^\s<>()]+", repl, text, flags=re.IGNORECASE)


def _normalize_symbols(text: str) -> str:
    text = re.sub(r"(?<=\d)\s*%", " процентов", text)
    text = text.replace("°C", " градусов Цельсия")
    text = text.replace("°", " градусов")
    text = re.sub(r"(?<=\d)\s*GB\b", " гигабайт", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*MB\b", " мегабайт", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*ms\b", " миллисекунд", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*sec\b", " секунд", text, flags=re.IGNORECASE)
    return text


def _normalize_numbers(text: str) -> str:
    # Decimal separator gets a spoken pause instead of a raw punctuation guess.
    text = re.sub(
        r"(?<!\w)(-?\d{1,4})[.,](\d{1,3})(?!\w)",
        lambda m: f"{_number_ru(int(m.group(1)))} целых {' '.join(_ONES[int(ch)] for ch in m.group(2))}",
        text,
    )

    # Clock notation is common in reminders and should not sound like division.
    def clock(match: re.Match[str]) -> str:
        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour > 23 or minute > 59:
            return match.group(0)
        return f"{_number_ru(hour)} {(_number_ru(minute) if minute else 'ровно')}"

    text = re.sub(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", clock, text)

    # Conservative cardinal conversion: small standalone numbers only.
    text = re.sub(
        r"(?<![\w.-])-?\d{1,4}(?![\w.-])",
        lambda m: _number_ru(int(m.group(0))),
        text,
    )
    return text


def _normalize_pause_punctuation(text: str) -> str:
    text = text.replace("—", ", ").replace("–", ", ")
    text = re.sub(r"\s*;\s*", ", ", text)
    text = re.sub(r"\.{3,}", ". ", text)
    text = re.sub(r"\s*:\s+(?=[А-ЯA-Z0-9])", ". ", text)
    text = re.sub(r"([!?]){2,}", r"\1", text)
    text = re.sub(r",\s*,+", ", ", text)
    return text


def quality_status() -> dict[str, object]:
    path = _default_dictionary_path()
    custom = {}
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = 0
    if stamp:
        custom = _load_dictionary(str(path), stamp)
    return {
        "enabled": True,
        "builtin_terms": len(_BUILTIN),
        "custom_terms": len(custom),
        "dictionary_path": str(path),
    }


def prepare_tts_text(text: str, overrides: Mapping[str, str] | None = None) -> str:
    """Return a pronunciation-friendly string for local Piper TTS."""
    clean = " ".join(str(text or "").split())
    if not clean:
        return ""
    clean = _normalize_urls(clean)
    clean = _normalize_symbols(clean)
    clean = _apply_dictionary(clean, overrides if overrides is not None else pronunciation_dictionary())
    clean = _normalize_numbers(clean)
    clean = _normalize_pause_punctuation(clean)
    clean = re.sub(r"\s+([,.!?])", r"\1", clean)
    clean = re.sub(r"\s{2,}", " ", clean).strip()
    return clean[:1800]

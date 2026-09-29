"""Настройки агента: адрес, стоп-слово, подтверждение, папки ассистента.

Никакой базы и никакого импорта коннектора. Агент — самостоятельная программа:
если PrintFlow не запущен, агент всё равно работает и честно говорит «панель
недоступна». Значения читаются из переменных окружения, чтобы владелец мог
поменять стоп-слово или порт, не правя код.

В 18.14 агент стал личным ассистентом компьютера, поэтому к адресам добавились
папки: что индексировать (`PRINTFLOW_ASSISTANT_FOLDERS`), что считать знаниями
цеха (`PRINTFLOW_KNOWLEDGE_FOLDERS`), где своя база (`PRINTFLOW_ASSISTANT_DB`)
и какой рантайм модели звать (`PRINTFLOW_MODEL_URL`). Папки важнее, чем кажутся:
ими задаётся граница, внутри которой ассистент вообще смотрит на файлы.
"""
from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass, field



def _env(*names: str, default: str = "") -> str:
    """First configured environment value, used for Luma -> legacy aliases."""
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    return default

SPEECH_PORT = int(os.environ.get("PRINTFLOW_SPEECH_PORT", "8791") or 8791)
AGENT_PORT = int(os.environ.get("PRINTFLOW_AGENT_PORT", "8799") or 8799)
PRINTFLOW_URL = os.environ.get("PRINTFLOW_URL", "http://127.0.0.1:8765").rstrip("/")
ASSISTANT_NAME = _env("LUMA_ASSISTANT_NAME", "NOZZA_ASSISTANT_NAME", default="Люма").strip() or "Люма"
WAKE_WORD = os.environ.get("PRINTFLOW_WAKE_WORD", "люма").strip().lower()
LEGACY_WAKE_WORDS = tuple(
    word.strip().lower()
    for word in _env("LUMA_LEGACY_WAKE_WORDS", "NOZZA_LEGACY_WAKE_WORDS", default="ноза,нозза,nozza,noza").split(",")
    if word.strip()
)
LANGUAGE = os.environ.get("PRINTFLOW_SPEECH_LANG", "ru")
# Старый ручной arm-режим сохранён для совместимости. Основной Voice Engine 2.0
# ниже может держать локальный микрофон включённым для wake word; запись на диск
# не ведётся.
MIC_ARM_SECONDS = float(os.environ.get("PRINTFLOW_MIC_ARM_SECONDS", "25") or 25)
SPEECH_MODEL_PATH = os.environ.get("PRINTFLOW_SPEECH_MODEL_PATH", "").strip()

# Voice Engine 2.0: постоянный локальный wake word. Аудио не сохраняется и
# в Brain попадает только после wake word или внутри короткой разговорной сессии.
VOICE_ALWAYS_ON = os.environ.get("PRINTFLOW_VOICE_ALWAYS_ON", "1").strip().lower() not in (
    "0", "false", "нет", "no", "off")
VOICE_FOLLOWUP_SECONDS = float(os.environ.get("PRINTFLOW_VOICE_FOLLOWUP_SECONDS", "8") or 8)
VOICE_VAD_THRESHOLD = int(os.environ.get("PRINTFLOW_VOICE_VAD_THRESHOLD", "320") or 320)
VOICE_SILENCE_SECONDS = float(os.environ.get("PRINTFLOW_VOICE_SILENCE_SECONDS", "0.8") or 0.8)
VOICE_MAX_PHRASE_SECONDS = float(os.environ.get("PRINTFLOW_VOICE_MAX_PHRASE_SECONDS", "15") or 15)
# Voice Engine 3.0: немного звука до срабатывания VAD сохраняется только в RAM,
# чтобы первая согласная wake-word не обрезалась. Partial ASR обновляет live
# status, но не отправляется в Brain до финализации фразы.
VOICE_PREROLL_CHUNKS = max(0, int(_env("LUMA_VOICE_PREROLL_CHUNKS", "NOZZA_VOICE_PREROLL_CHUNKS", default="3") or 3))
VOICE_PARTIAL_MIN_CHARS = max(1, int(_env("LUMA_VOICE_PARTIAL_MIN_CHARS", "NOZZA_VOICE_PARTIAL_MIN_CHARS", default="2") or 2))
VOICE_ECHO_GATE_MULTIPLIER = max(
    1.0, float(_env("LUMA_VOICE_ECHO_GATE_MULTIPLIER", "NOZZA_VOICE_ECHO_GATE_MULTIPLIER", default="1.65") or 1.65)
)
VOICE_ECHO_GATE_MARGIN = max(
    0, int(_env("LUMA_VOICE_ECHO_GATE_MARGIN", "NOZZA_VOICE_ECHO_GATE_MARGIN", default="180") or 180)
)
VOICE_ECHO_FLOOR_ALPHA = min(
    0.95, max(0.05, float(_env("LUMA_VOICE_ECHO_FLOOR_ALPHA", "NOZZA_VOICE_ECHO_FLOOR_ALPHA", default="0.22") or 0.22))
)

# --- 18.14: личный ассистент компьютера ------------------------------------
# Адрес рантайма модели. Тот же, что у помощника в панели (`assistant.DEFAULT_URL`):
# модель одна на компьютер, а видеопамять уже делят нарезка сечений и панель.
MODEL_URL = os.environ.get("PRINTFLOW_MODEL_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL_NAME = os.environ.get("PRINTFLOW_MODEL_NAME", "").strip()
MODEL_TIMEOUT_SEC = float(os.environ.get("PRINTFLOW_MODEL_TIMEOUT_SEC", "90") or 90)

# Browser Provider 1.0: только локальный Chromium DevTools endpoint.
# LUMA_* — canonical; NOZZA_* и PRINTFLOW_* сохранены как legacy aliases.
BROWSER_CDP_URL = _env(
    "LUMA_BROWSER_CDP_URL",
    "NOZZA_BROWSER_CDP_URL",
    "PRINTFLOW_BROWSER_CDP_URL",
    default="http://127.0.0.1:9222",
).rstrip("/")

# Своя база ассистента: память, индекс документов, журнал действий на ПК.
STORE_PATH = os.environ.get("PRINTFLOW_ASSISTANT_DB", "").strip()

# Папка загрузок — то, что навык `files.tidy_downloads` раскладывает по делам.
DOWNLOADS_FOLDER = os.environ.get(
    "PRINTFLOW_DOWNLOADS_FOLDER",
    str(pathlib.Path.home() / "Downloads")).strip()
# Личные документы, которые попадают в индекс (`files.index` без параметра).
DOCUMENT_FOLDERS = tuple(
    part.strip() for part in
    re.split(r"[;|]+", os.environ.get("PRINTFLOW_ASSISTANT_FOLDERS", "")) if part.strip())
# Знания цеха: по умолчанию это документация репозитория, из которой агент запущен.
_REPO_DOCS = pathlib.Path(__file__).resolve().parents[1] / "docs"
KNOWLEDGE_FOLDERS = tuple(
    part.strip() for part in
    re.split(r"[;|]+", os.environ.get("PRINTFLOW_KNOWLEDGE_FOLDERS", "")) if part.strip()
) or ((str(_REPO_DOCS),) if _REPO_DOCS.is_dir() else ())
# Окно ассистента открывается при старте, если не сказано обратное. Трей и
# pywebview — необязательные зависимости: без них агент печатает адрес страницы.
OPEN_WINDOW = os.environ.get("PRINTFLOW_ASSISTANT_WINDOW", "1").strip().lower() not in (
    "0", "false", "нет", "no", "off")

# --- 18.15: Авито и ТГ ------------------------------------------------------
TG_BOT_TOKEN = os.environ.get("PRINTFLOW_TG_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN") or ""
TG_CHAT_ID = os.environ.get("PRINTFLOW_TG_CHAT_ID") or os.environ.get("TG_CHAT_ID") or ""
AVITO_DEFAULT_CITY = os.environ.get("PRINTFLOW_AVITO_CITY", "").strip()


def knowledge_folders() -> tuple[str, ...]:
    """Папки знаний цеха: инструкции, профили печати, чек-листы."""
    return tuple(KNOWLEDGE_FOLDERS)


def document_folders() -> tuple[str, ...]:
    """Папки личных документов: загрузки плюс то, что указал владелец."""
    folders = [DOWNLOADS_FOLDER] if DOWNLOADS_FOLDER else []
    folders += [folder for folder in DOCUMENT_FOLDERS if folder not in folders]
    return tuple(folders)


def file_folders() -> tuple[str, ...]:
    """Все папки, внутри которых ассистенту разрешено смотреть и двигать файлы."""
    folders = list(document_folders())
    folders += [folder for folder in knowledge_folders() if folder not in folders]
    return tuple(folder for folder in folders if folder)


@dataclass
class State:
    """Что агент знает о себе прямо сейчас. Только для `/health` и `/status`."""

    armed: bool = False
    wake_word: bool = False
    voice_enabled: bool = False
    voice_state: str = "idle"
    window: str = ""
    last_phrase: str = ""
    last_action: str = ""
    errors: list[str] = field(default_factory=list)

    def payload(self, capabilities: dict) -> dict:
        return {
            "ok": True,
            "armed": self.armed,
            "wake_word": self.wake_word,
            "voice_enabled": self.voice_enabled,
            "voice_state": self.voice_state,
            "window": self.window,
            "last_phrase": self.last_phrase,
            "last_action": self.last_action,
            "model": capabilities.get("speech_model", ""),
            "capabilities": capabilities,
            "errors": self.errors[-5:],
        }

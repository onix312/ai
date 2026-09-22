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

SPEECH_PORT = int(os.environ.get("PRINTFLOW_SPEECH_PORT", "8791") or 8791)
AGENT_PORT = int(os.environ.get("PRINTFLOW_AGENT_PORT", "8799") or 8799)
PRINTFLOW_URL = os.environ.get("PRINTFLOW_URL", "http://127.0.0.1:8765").rstrip("/")
WAKE_WORD = os.environ.get("PRINTFLOW_WAKE_WORD", "ноза").strip().lower()
LANGUAGE = os.environ.get("PRINTFLOW_SPEECH_LANG", "ru")
# Запись голоса живёт только во включённом режиме: микрофон закрыт, пока человек не
# нажал горячую клавишу или кнопку агента. Постоянно открытый микрофон исключён
# решением владельца (вопрос 6 допроса).
MIC_ARM_SECONDS = float(os.environ.get("PRINTFLOW_MIC_ARM_SECONDS", "25") or 25)

# --- 18.14: личный ассистент компьютера ------------------------------------
# Адрес рантайма модели. Тот же, что у помощника в панели (`assistant.DEFAULT_URL`):
# модель одна на компьютер, а видеопамять уже делят нарезка сечений и панель.
MODEL_URL = os.environ.get("PRINTFLOW_MODEL_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL_NAME = os.environ.get("PRINTFLOW_MODEL_NAME", "").strip()
MODEL_TIMEOUT_SEC = float(os.environ.get("PRINTFLOW_MODEL_TIMEOUT_SEC", "90") or 90)

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
    window: str = ""
    last_phrase: str = ""
    last_action: str = ""
    errors: list[str] = field(default_factory=list)

    def payload(self, capabilities: dict) -> dict:
        return {
            "ok": True,
            "armed": self.armed,
            "wake_word": self.wake_word,
            "window": self.window,
            "last_phrase": self.last_phrase,
            "last_action": self.last_action,
            "model": capabilities.get("speech_model", ""),
            "capabilities": capabilities,
            "errors": self.errors[-5:],
        }

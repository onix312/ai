"""Настройки агента: адрес, стоп-слово, подтверждение.

Никакой базы и никакого импорта коннектора. Агент — самостоятельная программа:
если PrintFlow не запущен, агент всё равно работает и честно говорит «панель
недоступна». Значения читаются из переменных окружения, чтобы владелец мог
поменять стоп-слово или порт, не правя код.
"""
from __future__ import annotations

import os
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

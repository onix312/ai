"""Безопасная state machine нарезки PrintFlow.

Модуль намеренно не отправляет файл на принтер и не ставит его в очередь.
Он принимает наблюдаемые события («модель проверена», «нарезка закончилась»,
«G-code проверен», «оператор подтвердил») и выдаёт разрешение или блокировку
на следующий шаг. Неизвестное состояние всегда блокирует, а не «пробуем».

Устроено так же, как `farmloop_cycle.py`: есть физический гейт (движок и
профиль), гейт проверки (аудит модели и G-code) и гейт оператора. Пока
включён ручной режим, машина никогда не разрешает автоматическую постановку
в очередь или печать — человек смотрит отчёт и нажимает сам.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .slicer_profile import PROFILE_ID


class SliceStage(str, Enum):
    IDLE = "idle"
    AUDITED = "audited"
    PLANNED = "planned"
    SLICING = "slicing"
    AUDITING = "auditing"
    READY = "ready"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass(frozen=True)
class SliceConfig:
    """Гейты одной нарезки. Значения приходят из настроек PrintFlow."""

    provider: str = "printflow"          # printflow | external
    profile_id: str = PROFILE_ID
    manual_only: bool = True             # ручной режим: только после отчёта
    engine_available: bool = True
    first_print_verified: bool = False   # первый реальный прогон принят
    operator_confirmed: bool = False
    auto_enqueue: bool = False
    auto_print: bool = False
    max_cycles: int = 1

    @property
    def engine_gate(self) -> bool:
        """Свой движок выбран и доступен."""
        return self.provider == "printflow" and self.engine_available

    @property
    def profile_gate(self) -> bool:
        return self.profile_id == PROFILE_ID

    @property
    def verification_gate(self) -> bool:
        """Первая печать нарезанным этим движком файлом принята владельцем."""
        return bool(self.first_print_verified)

    @property
    def operator_gate(self) -> bool:
        return bool(self.operator_confirmed or not self.manual_only)

    @property
    def unattended_gate(self) -> bool:
        """Разрешена ли серия без человека: почти всегда нет."""
        return bool(not self.manual_only and self.verification_gate
                    and self.max_cycles > 1)


@dataclass
class SliceMachine:
    config: SliceConfig
    stage: SliceStage = SliceStage.IDLE
    reason: str = ""
    model: str = ""
    output: str = ""

    # ---------------------------------------------------------- переходы

    def _block(self, reason: str) -> dict:
        self.stage = SliceStage.BLOCKED
        self.reason = reason
        return self.snapshot()

    def _fail(self, reason: str) -> dict:
        self.stage = SliceStage.ERROR
        self.reason = reason
        return self.snapshot()

    def audit_model(self, model: str = "", ok: bool = True) -> dict:
        """Модель проверена до нарезки."""
        self.model = model or self.model
        if not self.config.engine_gate:
            return self._block("Движок PrintFlow не выбран или недоступен")
        if not self.config.profile_gate:
            return self._block(f"Неизвестный профиль: {self.config.profile_id}")
        if not ok:
            return self._block("Модель не прошла аудит")
        self.stage = SliceStage.AUDITED
        self.reason = ""
        return self.snapshot()

    def plan(self) -> dict:
        """План подтверждён: параметры известны, файл ещё не писали."""
        if self.stage not in (SliceStage.IDLE, SliceStage.AUDITED):
            return self._block("План запрашивается до аудита модели")
        self.stage = SliceStage.PLANNED
        self.reason = ""
        return self.snapshot()

    def start(self) -> dict:
        """Начать нарезку: файл появится только после аудита."""
        if self.stage not in (SliceStage.AUDITED, SliceStage.PLANNED):
            return self._block("Нарезка запускается только после аудита модели")
        if not self.config.engine_gate:
            return self._block("Движок PrintFlow недоступен")
        self.stage = SliceStage.SLICING
        self.reason = ""
        return self.snapshot()

    def sliced(self, ok: bool = True, output: str = "") -> dict:
        if self.stage != SliceStage.SLICING:
            return self._block("Событие нарезки получено не в состоянии slicing")
        if not ok:
            return self._fail("Нарезка завершилась ошибкой")
        self.output = output
        self.stage = SliceStage.AUDITING
        self.reason = ""
        return self.snapshot()

    def audited(self, ok: bool = True, reason: str = "") -> dict:
        """G-code проверен после нарезки."""
        if self.stage != SliceStage.AUDITING:
            return self._block("Аудит G-code получен не в состоянии auditing")
        if not ok:
            return self._fail(reason or "G-code не прошёл аудит")
        self.stage = SliceStage.READY
        self.reason = ""
        return self.snapshot()

    def confirm(self, operator: bool) -> dict:
        if self.stage not in (SliceStage.READY, SliceStage.BLOCKED):
            return self._block("Подтверждение получено до готового G-code")
        if not operator:
            return self._block("Оператор не подтвердил результат")
        self.config = SliceConfig(**{**self.config.__dict__,
                                     "operator_confirmed": True})
        self.stage = SliceStage.READY
        self.reason = ""
        return self.snapshot()

    # ---------------------------------------------------------- разрешения

    def enqueue_allowed(self) -> bool:
        """Можно ли поставить файл в очередь парка."""
        # Проверка обязательна в любом режиме: пока первый реальный прогон
        # не принят владельцем, файл в очередь не уезжает сам.
        return bool(
            self.stage == SliceStage.READY
            and self.config.engine_gate
            and self.config.operator_gate
            and self.config.auto_enqueue
            and self.config.verification_gate
        )

    def print_allowed(self) -> bool:
        """Можно ли запускать печать без человека.

        В ручном режиме всегда False: оператор сначала смотрит отчёт.
        """
        return bool(
            self.enqueue_allowed()
            and self.config.auto_print
            and self.config.unattended_gate
        )

    def next_allowed(self) -> bool:
        return self.enqueue_allowed() or self.print_allowed()

    def blocked_reason(self) -> str:
        if self.stage == SliceStage.BLOCKED:
            return self.reason
        if self.stage == SliceStage.ERROR:
            return self.reason or "Нарезка завершилась ошибкой"
        if not self.config.engine_gate:
            return "Выключен свой движок PrintFlow: нарезка идёт внешним CLI"
        if not self.config.profile_gate:
            return f"Неизвестный профиль слайсера: {self.config.profile_id}"
        if self.stage in (SliceStage.IDLE, SliceStage.AUDITED, SliceStage.PLANNED):
            return ""
        if self.stage == SliceStage.READY and not self.config.operator_gate:
            return ("Ручной режим: посмотрите отчёт нарезки и подтвердите "
                    "результат, прежде чем отправлять файл дальше")
        if self.stage == SliceStage.READY and not self.config.verification_gate:
            return ("Первая печать файлом этого движка ещё не принята: "
                    "сначала один контролируемый прогон")
        return ""

    def gates(self) -> dict:
        return {
            "engine": bool(self.config.engine_gate),
            "profile": bool(self.config.profile_gate),
            "verification": bool(self.config.verification_gate),
            "operator": bool(self.config.operator_gate),
            "unattended": bool(self.config.unattended_gate),
        }

    def snapshot(self) -> dict:
        return {
            "stage": self.stage.value,
            "reason": self.reason,
            "model": self.model,
            "output": self.output,
            "manual_only": bool(self.config.manual_only),
            "gates": self.gates(),
            "enqueue_allowed": self.enqueue_allowed(),
            "print_allowed": self.print_allowed(),
            "next_allowed": self.next_allowed(),
            "blocked_reason": self.blocked_reason(),
        }


def config_from_settings(raw: dict | None = None) -> SliceConfig:
    """Собрать конфигурацию гейтов из настроек PrintFlow."""
    raw = raw if isinstance(raw, dict) else {}
    provider = str(raw.get("slicer_provider") or "external")
    manual = True
    if str(raw.get("slicer_mode") or "manual") == "auto":
        manual = False
    return SliceConfig(
        provider=provider,
        profile_id=str(raw.get("slicer_profile") or PROFILE_ID),
        manual_only=manual,
        engine_available=bool(raw.get("slicer_engine_available", True)),
        first_print_verified=bool(raw.get("slicer_first_print_verified", False)),
        auto_enqueue=bool(raw.get("slicer_auto_enqueue", False)),
        auto_print=bool(raw.get("slicer_auto_print", False)),
        max_cycles=int(raw.get("slicer_max_cycles") or 1),
    )

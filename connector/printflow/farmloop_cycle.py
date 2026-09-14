"""Безопасная state machine цикла FarmLoop.

Модуль намеренно не отправляет команды принтеру. Он принимает наблюдаемые
события от менеджера, камеры или датчика и выдаёт разрешение/блокировку для
следующего шага. Неизвестное состояние всегда блокирует цикл.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CycleState(str, Enum):
    IDLE = "idle"
    PRINTING = "printing"
    COOLING = "cooling"
    DETACHING = "detaching"
    VERIFYING_EMPTY = "verifying_empty"
    COMPLETE = "complete"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass(frozen=True)
class CycleConfig:
    max_cycles: int = 1
    max_detach_attempts: int = 1
    auto_next: bool = False
    unattended_series: bool = False
    sensor_mode: str = "manual"
    mechanics_verified: bool = False
    template_verified: bool = False
    pusher_enabled: bool = False
    bender_enabled: bool = False

    @property
    def physical_gate(self) -> bool:
        return all((self.mechanics_verified, self.template_verified,
                    self.pusher_enabled, self.bender_enabled))

    @property
    def sensing_gate(self) -> bool:
        return self.sensor_mode in {"sensor", "camera", "both"}


@dataclass
class CycleMachine:
    config: CycleConfig
    cycle: int = 0
    state: CycleState = CycleState.IDLE
    detach_attempts: int = 0
    reason: str = ""

    def _block(self, reason: str) -> dict:
        self.state = CycleState.BLOCKED
        self.reason = reason
        return self.snapshot()

    def start(self) -> dict:
        if not self.config.template_verified:
            return self._block("Шаблон FarmLoop не подтверждён")
        if not self.config.physical_gate:
            return self._block("Механика FarmLoop не прошла ручную проверку")
        if self.cycle >= self.config.max_cycles:
            return self._block("Достигнут лимит циклов")
        self.cycle += 1
        self.detach_attempts = 0
        self.reason = ""
        self.state = CycleState.PRINTING
        return self.snapshot()

    def print_finished(self, success: bool = True) -> dict:
        if self.state != CycleState.PRINTING:
            return self._block("Событие окончания печати получено не в состоянии printing")
        if not success:
            self.state = CycleState.ERROR
            self.reason = "Печать завершилась ошибкой"
            return self.snapshot()
        self.state = CycleState.COOLING
        return self.snapshot()

    def cooled(self, confirmed: bool = False) -> dict:
        if self.state != CycleState.COOLING:
            return self._block("Событие охлаждения получено не в состоянии cooling")
        if not confirmed:
            return self._block("Охлаждение не подтверждено")
        self.state = CycleState.DETACHING
        return self.snapshot()

    def detach_finished(self, success: bool = True) -> dict:
        if self.state != CycleState.DETACHING:
            return self._block("Событие снятия получено не в состоянии detaching")
        if success:
            self.state = CycleState.VERIFYING_EMPTY
            return self.snapshot()
        self.detach_attempts += 1
        if self.detach_attempts >= self.config.max_detach_attempts:
            self.state = CycleState.ERROR
            self.reason = "Не удалось снять деталь за допустимое число попыток"
        else:
            self.reason = "Снятие не подтверждено; требуется повторная попытка оператора"
        return self.snapshot()

    def empty_confirmed(self, empty: bool | None) -> dict:
        if self.state != CycleState.VERIFYING_EMPTY:
            return self._block("Подтверждение платформы получено не в состоянии verifying_empty")
        if empty is not True:
            return self._block("Пустая платформа не подтверждена")
        self.state = CycleState.COMPLETE
        self.reason = ""
        return self.snapshot()

    def next_allowed(self) -> bool:
        return bool(
            self.state == CycleState.COMPLETE
            and self.config.auto_next
            and self.config.physical_gate
            and self.config.sensing_gate
            and self.cycle < self.config.max_cycles
            and (not self.config.unattended_series or self.config.max_cycles > 1)
        )

    def snapshot(self) -> dict:
        return {
            "state": self.state.value,
            "cycle": self.cycle,
            "max_cycles": self.config.max_cycles,
            "detach_attempts": self.detach_attempts,
            "reason": self.reason,
            "next_allowed": self.next_allowed(),
        }

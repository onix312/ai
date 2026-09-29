"""Фоновые вызовы backend, чтобы модель и сеть не блокировали Qt UI."""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


class WorkerSignals(QObject):
    done = Signal(object)
    failed = Signal(str)
    finished = Signal(object)


class Worker(QRunnable):
    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self.fn = fn
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            self.signals.done.emit(self.fn())
        except Exception as exc:  # UI обязан остаться живым при любом отказе backend
            self.signals.failed.emit(str(exc))
        finally:
            self.signals.finished.emit(self)

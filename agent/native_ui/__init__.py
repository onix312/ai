"""Нативный интерфейс NOZZA Assistant на PySide6.

Пакет намеренно не содержит бизнес-логику ассистента. Мозг, память, навыки,
голос и подтверждения продолжают жить в `agent.server`; PySide6 — только
локальный клиент к loopback API.
"""
from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"

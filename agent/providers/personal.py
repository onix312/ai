"""Personal capability provider.

Personal data remains in the assistant's local SQLite via Personal/Learning.
The provider is only an ownership and dispatch boundary around existing
personal_skills handlers.
"""
from __future__ import annotations

from typing import Any

from .. import personal_skills
from .base import ProviderSkill, ProviderSpec


_READ_ONLY = frozenset({
    "reminder.list", "list.show", "goal.list", "habit.list",
    "expense.report", "diary.read", "me.today",
    "learn.list", "learn.unknown", "learn.habits",
})

_NAMES = (
    "reminder.add", "reminder.list", "reminder.done", "reminder.cancel", "reminder.snooze",
    "list.add", "list.show", "list.remove", "list.clear",
    "goal.add", "goal.progress", "goal.list", "goal.remove",
    "habit.add", "habit.check", "habit.list", "habit.remove",
    "expense.add", "expense.report",
    "diary.add", "diary.read", "me.today",
    "learn.teach", "learn.list", "learn.forget", "learn.unknown", "learn.habits",
)


class PersonalProvider:
    spec = ProviderSpec(
        name="personal",
        title="Личное",
        requires=("sqlite",),
        description="Напоминания, списки, цели, привычки, расходы, дневник и обучение в локальной базе.",
        skills=tuple(
            ProviderSkill(name, reversible=name in _READ_ONLY)
            for name in _NAMES
        ),
    )

    def run(self, skill_name: str, params: dict[str, Any],
            runner: Any | None = None) -> dict[str, Any]:
        if runner is None:
            return {"ok": False, "reason": "Personal Provider не получил Runner"}
        handler = personal_skills.handlers(runner).get(skill_name)
        if handler is None:
            return {"ok": False, "reason": f"Personal Provider не знает skill «{skill_name}»"}
        return handler(params)


PROVIDER = PersonalProvider()

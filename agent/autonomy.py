"""Autonomy Levels 1.0.

This policy gates *how far* NOZZA may act without changing canonical skill risk
or confirmation rules. Existing confirmation remains mandatory at every level.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

LEVELS = ("observer", "assistant", "operator", "agent", "autopilot")
LABELS = {
    "observer": "Observer · только наблюдать и отвечать",
    "assistant": "Assistant · одиночные команды",
    "operator": "Operator · многошаговые задачи",
    "agent": "Agent · Planner / Verify / Replan",
    "autopilot": "Autopilot · фоновые правила и события",
}
DEFAULT_LEVEL = "agent"
PREFIX = "autonomy."
PROVIDER_PREFIX = PREFIX + "provider."

# Hard upper bounds for unattended/background autonomy. Users may lower them,
# never raise beyond this map.
HARD_PROVIDER_CAPS = {
    "browser": "autopilot",
    "personal": "autopilot",
    "desktop": "agent",
    "printflow": "agent",
    "core": "agent",
}

MODE_LEVEL = {
    "observe": "observer",
    "direct": "assistant",
    "task": "operator",
    "plan": "agent",
    "background": "autopilot",
}


def rank(level: str) -> int:
    try:
        return LEVELS.index(str(level or "").strip().casefold())
    except ValueError:
        return LEVELS.index(DEFAULT_LEVEL)


@dataclass(slots=True)
class AutonomyPolicy:
    store: Any

    def level(self) -> str:
        try:
            row = self.store.get_preference(PREFIX + "level")
        except Exception:
            row = None
        value = str((row or {}).get("value") or DEFAULT_LEVEL).strip().casefold()
        return value if value in LEVELS else DEFAULT_LEVEL

    def provider_cap(self, provider: str) -> str:
        name = str(provider or "core").strip().casefold() or "core"
        hard = HARD_PROVIDER_CAPS.get(name, HARD_PROVIDER_CAPS["core"])
        try:
            row = self.store.get_preference(PROVIDER_PREFIX + name)
        except Exception:
            row = None
        value = str((row or {}).get("value") or hard).strip().casefold()
        if value not in LEVELS or rank(value) > rank(hard):
            return hard
        return value

    def payload(self) -> dict[str, Any]:
        providers = {
            name: {
                "level": self.provider_cap(name),
                "hard_max": hard,
                "label": LABELS[self.provider_cap(name)],
            }
            for name, hard in HARD_PROVIDER_CAPS.items()
        }
        return {
            "ok": True,
            "level": self.level(),
            "levels": list(LEVELS),
            "labels": dict(LABELS),
            "providers": providers,
            "invariant": (
                "Autonomy не отменяет risk/confirmation. Любой write/system/"
                "irreversible skill по-прежнему требует обычное подтверждение."
            ),
        }

    def update(self, level: str | None = None,
               providers: Any = None) -> dict[str, Any]:
        if level is not None:
            value = str(level or "").strip().casefold()
            if value not in LEVELS:
                return {"ok": False, "reason": f"Неизвестный уровень автономности «{value}»"}
            self.store.set_preference(PREFIX + "level", value)

        if providers is not None:
            if not isinstance(providers, dict):
                return {"ok": False, "reason": "providers должен быть объектом"}
            for raw_name, raw_level in providers.items():
                name = str(raw_name or "").strip().casefold()
                value = str(raw_level or "").strip().casefold()
                if name not in HARD_PROVIDER_CAPS:
                    return {"ok": False, "reason": f"Неизвестный provider «{name}»"}
                if value not in LEVELS:
                    return {"ok": False, "reason": f"Неизвестный уровень «{value}»"}
                hard = HARD_PROVIDER_CAPS[name]
                if rank(value) > rank(hard):
                    return {
                        "ok": False,
                        "reason": f"{name} нельзя поднять выше {hard}",
                        "hard_max": hard,
                    }
            for raw_name, raw_level in providers.items():
                name = str(raw_name).strip().casefold()
                value = str(raw_level).strip().casefold()
                self.store.set_preference(PROVIDER_PREFIX + name, value)
        return self.payload()

    def reset(self) -> dict[str, Any]:
        for key in [PREFIX + "level", *[
            PROVIDER_PREFIX + name for name in HARD_PROVIDER_CAPS
        ]]:
            try:
                self.store.delete_preference(key)
            except Exception:
                pass
        return self.payload()

    def check(self, required: str, provider: str = "core") -> tuple[bool, str]:
        need = str(required or "observer").strip().casefold()
        if need not in LEVELS:
            need = "observer"
        current = self.level()
        if rank(current) < rank(need):
            return False, (
                f"Уровень автономности {current} не разрешает этот режим. "
                f"Нужен минимум {need}."
            )
        cap = self.provider_cap(provider)
        if rank(need) > rank(cap):
            return False, (
                f"Provider {provider or 'core'} ограничен уровнем {cap}; "
                f"для этого режима нужен {need}."
            )
        return True, ""

    def check_skill(self, skill: dict[str, Any], mode: str = "direct") -> tuple[bool, str]:
        from . import skills

        risk = skills.risk_of(skill)
        # Observer may use read-only skills to inspect state.
        required = "observer" if mode == "direct" and risk == "read" else MODE_LEVEL.get(mode, "assistant")
        provider = str(skill.get("provider") or "core")
        return self.check(required, provider)

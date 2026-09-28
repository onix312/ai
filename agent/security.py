"""Security 2.0 phase 1: panic latch, Guest Mode and sensitive-window policy.

This module is intentionally a policy layer. It does not execute capabilities.
Execution stays in Agent.run_skill -> canonical validation/confirmation -> Runner.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from . import skills

PREFIX = "security."
DEFAULT_SENSITIVE_WINDOWS = (
    "1password",
    "bitwarden",
    "keepass",
    "password",
    "пароль",
    "secret",
    "секрет",
)

# Guest Mode is for handing the computer to another person without exposing the
# owner's local assistant data or authenticated work surfaces.
_GUEST_BLOCKED_PROVIDERS = frozenset(("personal", "printflow", "browser"))
_GUEST_BLOCKED_PREFIXES = (
    "memory.",
    "learn.",
    "reminder.",
    "list.",
    "goal.",
    "habit.",
    "expense.",
    "diary.",
    "me.",
    "panel.",
    "day.",
    "browser.",
    "files.",
    "clipboard.",
    "knowledge.",
    "assistant.macro",
)
_GUEST_BLOCKED_EXACT = frozenset((
    "agent.journal",
    "window.active",
    "window.list",
    "window.text",
    "desktop.observe",
    "screen.shot",
    "screen.region_shot",
    "screen.describe",
    "screen.find",
    "screen.find_and_click",
))


@dataclass(slots=True)
class SecurityPolicy:
    store: Any

    def _preference(self, key: str) -> str:
        try:
            row = self.store.get_preference(PREFIX + key)
        except Exception:
            row = None
        return str((row or {}).get("value") or "")

    def guest_mode(self) -> bool:
        return self._preference("guest_mode").strip().casefold() in ("1", "true", "on", "yes")

    def panic_latched(self) -> bool:
        return self._preference("panic").strip().casefold() in ("1", "true", "on", "yes")

    def panic_reason(self) -> str:
        return self._preference("panic_reason").strip()

    def sensitive_windows(self) -> list[str]:
        raw = self._preference("sensitive_windows")
        if not raw:
            return list(DEFAULT_SENSITIVE_WINDOWS)
        try:
            values = json.loads(raw)
        except (TypeError, ValueError):
            values = []
        if not isinstance(values, list):
            return list(DEFAULT_SENSITIVE_WINDOWS)
        out: list[str] = []
        for item in values:
            value = " ".join(str(item or "").split()).casefold()
            if value and value not in out:
                out.append(value[:80])
        return out[:40]

    def payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "guest_mode": self.guest_mode(),
            "panic": self.panic_latched(),
            "panic_reason": self.panic_reason(),
            "sensitive_windows": self.sensitive_windows(),
            "panic_shortcut": "Ctrl+Alt+Shift+Esc",
            "invariant": (
                "Security policy может только ужесточать доступ. Она не снижает "
                "risk, не снимает confirmations и не создаёт обходной execution path."
            ),
        }

    def update(self, *, guest_mode: Any = None,
               sensitive_windows: Any = None) -> dict[str, Any]:
        clean_guest: bool | None = None
        clean_windows: list[str] | None = None

        if guest_mode is not None:
            if not isinstance(guest_mode, bool):
                return {"ok": False, "reason": "guest_mode должен быть true/false"}
            clean_guest = guest_mode

        if sensitive_windows is not None:
            if not isinstance(sensitive_windows, list):
                return {"ok": False, "reason": "sensitive_windows должен быть списком"}
            if len(sensitive_windows) > 40:
                return {"ok": False, "reason": "Не больше 40 шаблонов чувствительных окон"}
            clean_windows = []
            for item in sensitive_windows:
                value = " ".join(str(item or "").split()).casefold()
                if not value:
                    continue
                if len(value) > 80:
                    return {"ok": False, "reason": "Шаблон чувствительного окна слишком длинный"}
                if value not in clean_windows:
                    clean_windows.append(value)

        # Validate first, persist second.
        if clean_guest is not None:
            self.store.set_preference(PREFIX + "guest_mode", "1" if clean_guest else "0")
        if clean_windows is not None:
            self.store.set_preference(
                PREFIX + "sensitive_windows",
                json.dumps(clean_windows, ensure_ascii=False),
            )
        return self.payload()

    def panic(self, reason: str = "global panic") -> dict[str, Any]:
        self.store.set_preference(PREFIX + "panic", "1")
        self.store.set_preference(PREFIX + "panic_reason", " ".join(str(reason or "").split())[:200])
        return self.payload()

    def resume(self) -> dict[str, Any]:
        self.store.set_preference(PREFIX + "panic", "0")
        try:
            self.store.delete_preference(PREFIX + "panic_reason")
        except Exception:
            pass
        return self.payload()

    def is_sensitive(self, title: Any) -> bool:
        value = " ".join(str(title or "").split()).casefold()
        return bool(value) and any(pattern in value for pattern in self.sensitive_windows())

    def guest_allows_skill(self, skill: dict[str, Any]) -> tuple[bool, str]:
        if not self.guest_mode():
            return True, ""
        name = str(skill.get("name") or "").strip().casefold()
        provider = str(skill.get("provider") or "").strip().casefold()
        if provider in _GUEST_BLOCKED_PROVIDERS:
            return False, "Guest Mode скрывает данные владельца и его подключённые рабочие поверхности"
        if name in _GUEST_BLOCKED_EXACT or any(name.startswith(prefix) for prefix in _GUEST_BLOCKED_PREFIXES):
            return False, "Guest Mode не разрешает доступ к личным данным владельца"
        return True, ""

    def check_skill(self, skill: dict[str, Any], params: dict[str, Any] | None = None,
                    active_window: str = "") -> tuple[bool, str]:
        name = str(skill.get("name") or "").strip().casefold()
        risk = skills.risk_of(skill)

        if self.panic_latched() and risk != "read":
            return False, "Global Panic активен: действия остановлены до ручного возобновления"

        allowed, reason = self.guest_allows_skill(skill)
        if not allowed:
            return False, reason

        if name.startswith(("window.", "screen.", "desktop.")):
            values = dict(params or {})
            target = str(values.get("title") or values.get("window") or active_window or "")
            if self.is_sensitive(target):
                return False, "Чувствительное окно закрыто для чтения и автоматизации"
        return True, ""

    def check_ui_action(self, kind: str, params: dict[str, Any] | None,
                        active_window: str = "") -> tuple[bool, str]:
        if self.panic_latched():
            return False, "Global Panic активен: UI automation остановлена"
        if self.guest_mode():
            return False, "Guest Mode не разрешает UI automation в чужих окнах"
        values = dict(params or {})
        target = str(values.get("title") or active_window or "")
        if self.is_sensitive(target):
            return False, "Чувствительное окно закрыто для UI automation"
        return True, ""

    def guest_allows_private_api(self) -> bool:
        return not self.guest_mode()

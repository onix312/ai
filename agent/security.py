"""Security 2.0 policy for NOZZA.

Security is independent from Persona and Autonomy. It can only reduce access:
guest mode hides owner context, sensitive-window rules deny perception/input, and
panic latches mutating execution off until the owner explicitly resets it.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

from .vault import SecretVault

DEFAULT_SENSITIVE_PATTERNS = (
    "1password",
    "bitwarden",
    "keepass",
    "password manager",
    "credential manager",
    "windows security",
    "banking",
    "internet bank",
    "онлайн-банк",
    "интернет-банк",
    "криптокошелек",
    "crypto wallet",
)

SENSITIVE_WINDOW_SKILLS = frozenset({
    "desktop.observe",
    "window.text",
    "window.controls",
    "window.click",
    "window.type",
    "screen.shot",
    "screen.region_shot",
    "screen.describe",
    "screen.find",
    "screen.find_and_click",
    "clipboard.read",
    "clipboard.history",
    "system.hotkey",
})

GUEST_PRIVATE_PREFIXES = (
    "memory.",
    "reminder.",
    "list.",
    "goal.",
    "habit.",
    "expense.",
    "diary.",
    "learn.",
    "knowledge.preference",
    "screen.archive",
    "clipboard.",
)
GUEST_PRIVATE_SKILLS = frozenset({
    "me.today",
    "agent.journal",
    "agent.why",
})
GUEST_PRIVATE_ROUTES = frozenset({
    "/memory",
    "/personal",
    "/windows",
    "/screen",
    "/learning",
    "/notifications",
    "/chat/history",
    "/journal",
    "/events",
    "/tasks",
    "/plans",
    "/replans",
    "/feedback",
})


@dataclass(slots=True)
class SecurityPolicy:
    store: Any
    vault: SecretVault = field(init=False)
    _panic: bool = field(default=False, init=False)
    _panic_reason: str = field(default="", init=False)
    _lock: Any = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.vault = SecretVault(self.store)

    # ---------------------------------------------------------------- state
    def guest(self) -> bool:
        try:
            row = self.store.get_preference("security.guest")
        except Exception:
            row = None
        return str((row or {}).get("value") or "").strip().casefold() in (
            "1", "true", "on", "yes",
        )

    def custom_patterns(self) -> list[str]:
        try:
            row = self.store.get_preference("security.sensitive_patterns")
        except Exception:
            row = None
        raw = str((row or {}).get("value") or "")
        if not raw:
            return []
        try:
            values = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(values, list):
            return []
        out = []
        for item in values[:50]:
            clean = " ".join(str(item or "").casefold().split())[:100]
            if clean and clean not in out:
                out.append(clean)
        return out

    def patterns(self) -> list[str]:
        out = list(DEFAULT_SENSITIVE_PATTERNS)
        for item in self.custom_patterns():
            if item not in out:
                out.append(item)
        return out

    def payload(self) -> dict[str, Any]:
        with self._lock:
            panic = self._panic
            panic_reason = self._panic_reason
        return {
            "ok": True,
            "guest": self.guest(),
            "panic": panic,
            "panic_reason": panic_reason,
            "sensitive_patterns": self.patterns(),
            "custom_patterns": self.custom_patterns(),
            "vault": {
                **self.vault.status(),
                "secrets": self.vault.list(),
            },
            "panic_hotkey": "Ctrl+Alt+Shift+Esc",
            "invariant": (
                "Security может только ограничивать доступ. Guest/Panic не снимают "
                "Autonomy, risk или confirmations."
            ),
        }

    def update(self, *, guest: Any = None, patterns: Any = None) -> dict[str, Any]:
        clean_patterns: list[str] | None = None
        if patterns is not None:
            if not isinstance(patterns, list):
                return {"ok": False, "reason": "patterns должен быть массивом"}
            clean_patterns = []
            for item in patterns[:50]:
                value = " ".join(str(item or "").casefold().split())[:100]
                if value and value not in clean_patterns:
                    clean_patterns.append(value)
        if guest is not None:
            self.store.set_preference("security.guest", "1" if bool(guest) else "0")
        if clean_patterns is not None:
            self.store.set_preference(
                "security.sensitive_patterns",
                json.dumps(clean_patterns, ensure_ascii=False),
            )
        return self.payload()

    # ---------------------------------------------------------------- panic
    def trigger_panic(self, reason: str = "panic hotkey") -> dict[str, Any]:
        with self._lock:
            self._panic = True
            self._panic_reason = " ".join(str(reason or "panic").split())[:200]
        return self.payload()

    def reset_panic(self) -> dict[str, Any]:
        with self._lock:
            self._panic = False
            self._panic_reason = ""
        return self.payload()

    def panic_latched(self) -> bool:
        with self._lock:
            return self._panic

    # ---------------------------------------------------------------- guest
    def private_route(self, path: str) -> bool:
        return self.guest() and str(path or "") in GUEST_PRIVATE_ROUTES

    def guest_skill_blocked(self, name: str, skill: dict[str, Any]) -> bool:
        if not self.guest():
            return False
        key = str(name or "").casefold()
        provider = str(skill.get("provider") or "")
        return (
            provider == "personal"
            or key in GUEST_PRIVATE_SKILLS
            or any(key.startswith(prefix) for prefix in GUEST_PRIVATE_PREFIXES)
        )

    # ---------------------------------------------------------- sensitive UI
    def is_sensitive_window(self, title: str) -> tuple[bool, str]:
        low = " ".join(str(title or "").casefold().split())
        if not low:
            return False, ""
        for pattern in self.patterns():
            if pattern and pattern in low:
                return True, pattern
        return False, ""

    def current_sensitive_window(self) -> tuple[bool, str, str]:
        try:
            from . import winapi
            title, _reason = winapi.active_window()
        except Exception:
            title = ""
        matched, pattern = self.is_sensitive_window(str(title or ""))
        return matched, str(title or ""), pattern

    # ---------------------------------------------------------------- guards
    def guard_skill(self, name: str, params: dict[str, Any],
                    skill: dict[str, Any]) -> tuple[bool, str]:
        from . import skills

        key = str(name or "").strip().casefold()
        if self.panic_latched() and skills.risk_of(skill) != "read":
            return False, "PANIC включён: изменяющие действия остановлены до явного сброса"

        if self.guest_skill_blocked(key, skill):
            return False, "Гостевой режим скрывает личные данные и личные навыки владельца"

        if key in SENSITIVE_WINDOW_SKILLS:
            requested = str(params.get("title") or "").strip()
            if requested:
                sensitive, pattern = self.is_sensitive_window(requested)
                if sensitive:
                    return False, f"Sensitive Window: «{requested}» совпало с deny-list ({pattern})"
            sensitive, title, pattern = self.current_sensitive_window()
            if sensitive:
                return False, f"Sensitive Window: «{title}» совпало с deny-list ({pattern})"
        return True, ""

    def guard_raw(self, kind: str, window: str = "") -> tuple[bool, str]:
        if self.panic_latched():
            return False, "PANIC включён: UI automation остановлена"
        title = str(window or "")
        if not title:
            sensitive, current, pattern = self.current_sensitive_window()
        else:
            sensitive, pattern = self.is_sensitive_window(title)
            current = title
        if sensitive:
            return False, f"Sensitive Window: «{current}» совпало с deny-list ({pattern})"
        return True, ""

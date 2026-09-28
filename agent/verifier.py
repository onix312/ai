"""Independent verification for Task Engine side effects.

A verifier never performs the action itself. It only reads state after a skill
reported success. Missing evidence is "assumed", not "verified". A definitive
mismatch is "failed" and stops the task before the next step.
"""
from __future__ import annotations

from typing import Any

from . import pc, winapi

STATUSES = ("verified", "assumed", "failed")


def _out(status: str, reason: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": status if status in STATUSES else "assumed",
        "reason": str(reason or "")[:500],
        "evidence": dict(evidence or {}),
    }


def verify(skill_name: str, params: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Verify one completed skill without repeating its side effect."""
    name = str(skill_name or "").strip().casefold()
    params = dict(params or {})
    result = dict(result or {})
    if not result.get("ok"):
        return _out("failed", str(result.get("reason") or "Навык сообщил об ошибке"))

    try:
        if name == "system.volume":
            return _verify_volume(params, result)
        if name == "window.focus":
            return _verify_focus(params, result)
        if name == "clipboard.write":
            return _verify_clipboard(params)
    except Exception as exc:  # verification must never crash the task engine
        return _out("assumed", f"Проверка недоступна: {exc.__class__.__name__}")

    return _out("assumed", "Для этого навыка пока нет независимой проверки")


def _verify_volume(params: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    state, reason = pc.volume_get()
    if reason or state.get("level") is None:
        return _out("assumed", reason or "Состояние громкости нельзя прочитать")

    evidence = {"level": state.get("level"), "muted": state.get("muted")}
    mute = str(params.get("mute") or "").strip().casefold()
    if mute:
        expected = result.get("muted") if mute == "toggle" else mute == "on"
        if expected is None:
            return _out("assumed", "Нет ожидаемого состояния mute", evidence)
        if bool(state.get("muted")) == bool(expected):
            return _out("verified", "Состояние mute прочитано обратно", evidence)
        return _out("failed", "Состояние mute после действия не совпало", evidence)

    if params.get("level") is not None:
        expected = max(0, min(100, int(params["level"])))
        actual = int(state.get("level") or 0)
        if abs(actual - expected) <= 2:
            return _out("verified", "Громкость прочитана обратно", evidence)
        return _out("failed", f"Ожидалась громкость {expected}%, получено {actual}%", evidence)

    if result.get("level") is not None:
        expected = int(result["level"])
        actual = int(state.get("level") or 0)
        if abs(actual - expected) <= 2:
            return _out("verified", "Громкость после относительного изменения подтверждена", evidence)
        return _out("failed", "Прочитанная громкость не совпала с результатом навыка", evidence)

    return _out("assumed", "Изменение громкости выполнено медиаклавишами без точного уровня", evidence)


def _verify_focus(params: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    active, reason = winapi.active_window()
    if reason or not active:
        return _out("assumed", reason or "Активное окно нельзя прочитать")
    wanted = str(result.get("title") or params.get("title") or "").strip().casefold()
    actual = str(active).strip()
    if not wanted:
        return _out("assumed", "Нет названия окна для сравнения", {"active": actual})
    low = actual.casefold()
    if wanted in low or low in wanted:
        return _out("verified", "Активное окно совпадает", {"active": actual})
    return _out("failed", f"На переднем плане другое окно: {actual}", {"active": actual})


def _verify_clipboard(params: dict[str, Any]) -> dict[str, Any]:
    from . import clipboard as clipboard_mod

    actual, reason = clipboard_mod.get_text()
    if reason:
        return _out("assumed", reason)
    expected = str(params.get("text") or "")
    if actual == expected:
        return _out("verified", "Буфер прочитан обратно", {"chars": len(actual)})
    return _out("failed", "Текст в буфере не совпал с записанным",
                {"expected_chars": len(expected), "actual_chars": len(actual)})

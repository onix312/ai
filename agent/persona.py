"""Stable NOZZA persona profile.

Persona controls presentation only. It never changes skills, risk, confirmation,
planner permissions, provider access or any other safety policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PREFIX = "persona."

FIELDS: dict[str, tuple[str, ...]] = {
    "address": ("formal", "informal"),
    "verbosity": ("brief", "normal", "detailed"),
    "humor": ("off", "light", "playful"),
    "initiative": ("quiet", "balanced", "active"),
    "relationship": ("professional", "friendly", "warm"),
}

DEFAULTS = {
    "address": "formal",
    "verbosity": "brief",
    "humor": "light",
    "initiative": "balanced",
    "relationship": "friendly",
}

LABELS = {
    "address": {
        "formal": "на «вы»",
        "informal": "на «ты»",
    },
    "verbosity": {
        "brief": "кратко",
        "normal": "обычно",
        "detailed": "подробно",
    },
    "humor": {
        "off": "без юмора",
        "light": "лёгкий юмор",
        "playful": "игриво",
    },
    "initiative": {
        "quiet": "только по запросу",
        "balanced": "умеренно инициативно",
        "active": "активно предлагать следующий шаг",
    },
    "relationship": {
        "professional": "профессионально",
        "friendly": "дружелюбно",
        "warm": "тепло и неформально",
    },
}


@dataclass(slots=True)
class Persona:
    store: Any

    def profile(self) -> dict[str, str]:
        out = dict(DEFAULTS)
        try:
            rows = self.store.list_preferences(200)
        except Exception:
            rows = []
        for row in rows:
            key = str(row.get("key") or "")
            if not key.startswith(PREFIX):
                continue
            field = key[len(PREFIX):]
            value = str(row.get("value") or "")
            if field in FIELDS and value in FIELDS[field]:
                out[field] = value
        return out

    def payload(self) -> dict[str, Any]:
        profile = self.profile()
        return {
            "ok": True,
            "profile": profile,
            "defaults": dict(DEFAULTS),
            "options": {field: list(values) for field, values in FIELDS.items()},
            "labels": LABELS,
        }

    def update(self, patch: Any) -> dict[str, Any]:
        if not isinstance(patch, dict):
            return {"ok": False, "reason": "Профиль описывается объектом"}
        unknown = sorted(set(str(key) for key in patch) - set(FIELDS))
        if unknown:
            return {
                "ok": False,
                "reason": "Persona не управляет этими настройками: " + ", ".join(unknown),
            }
        clean: dict[str, str] = {}
        for field, raw in patch.items():
            value = str(raw or "").strip().casefold()
            if value not in FIELDS[field]:
                return {
                    "ok": False,
                    "reason": f"Некорректное значение persona.{field}: {value or 'пусто'}",
                    "allowed": list(FIELDS[field]),
                }
            clean[field] = value
        for field, value in clean.items():
            self.store.set_preference(PREFIX + field, value)
        return {"ok": True, "profile": self.profile()}

    def reset(self) -> dict[str, Any]:
        for field in FIELDS:
            try:
                self.store.delete_preference(PREFIX + field)
            except Exception:
                pass
        return {"ok": True, "profile": self.profile()}

    def prompt_fragment(self) -> str:
        p = self.profile()
        address = (
            "Обращайся к человеку на «ты»."
            if p["address"] == "informal"
            else "Обращайся к человеку на «вы»."
        )
        verbosity = {
            "brief": "Отвечай кратко: обычно одно-два предложения, если подробности не нужны.",
            "normal": "Отвечай умеренно подробно, без лишних повторов.",
            "detailed": "Когда вопрос сложный, объясняй подробнее, но не растягивай простые ответы.",
        }[p["verbosity"]]
        humor = {
            "off": "Не добавляй шутки.",
            "light": "Допустим лёгкий ненавязчивый юмор, когда он уместен.",
            "playful": "Можно быть более живой и игривой, но не в серьёзных или опасных ситуациях.",
        }[p["humor"]]
        relationship = {
            "professional": "Тон профессиональный и спокойный.",
            "friendly": "Тон дружелюбный, естественный, как у внимательного коллеги.",
            "warm": "Тон тёплый и живой, без фамильярности.",
        }[p["relationship"]]
        initiative = {
            "quiet": "Не предлагай дополнительные действия без явной пользы для текущего запроса.",
            "balanced": "Можно кратко предложить очевидный следующий шаг, если это действительно помогает.",
            "active": "После завершения можно предлагать полезный следующий шаг, но не выполнять его без обычных разрешений.",
        }[p["initiative"]]
        return " ".join((address, verbosity, humor, relationship, initiative))


def safety_invariant() -> str:
    return (
        "Persona влияет только на форму ответа. Она не меняет доступные навыки, "
        "уровень риска, подтверждения, права Planner/Task Engine или providers."
    )

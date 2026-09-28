"""Small provider contracts. No executor or server imports here."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ProviderSkill:
    name: str
    reversible: bool = True


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    name: str
    title: str
    requires: tuple[str, ...]
    skills: tuple[ProviderSkill, ...]
    description: str = ""


class CapabilityProvider(Protocol):
    spec: ProviderSpec

    def run(self, skill_name: str, params: dict[str, Any]) -> dict[str, Any]:
        ...

"""Provider registry: one owner for each migrated capability skill."""
from __future__ import annotations

from typing import Any

from .base import CapabilityProvider
from .browser import PROVIDER as BROWSER
from .desktop import PROVIDER as DESKTOP
from .personal import PROVIDER as PERSONAL
from .printflow import PROVIDER as PRINTFLOW

_PROVIDERS: tuple[CapabilityProvider, ...] = (
    BROWSER,
    DESKTOP,
    PRINTFLOW,
    PERSONAL,
)
_BY_NAME = {provider.spec.name: provider for provider in _PROVIDERS}
_BY_SKILL = {
    skill.name: provider
    for provider in _PROVIDERS
    for skill in provider.spec.skills
}


def names() -> tuple[str, ...]:
    return tuple(_BY_NAME)


def for_skill(skill_name: str) -> CapabilityProvider | None:
    return _BY_SKILL.get(str(skill_name or "").strip().casefold())


def catalog(capabilities: dict[str, Any]) -> list[dict[str, Any]]:
    """Provider health plus skill contracts from the canonical skill registry."""
    from .. import skills

    rows: list[dict[str, Any]] = []
    for provider in _PROVIDERS:
        missing = [
            str(capabilities.get(f"{need}_reason") or f"Нет способности {need}")
            for need in provider.spec.requires if not capabilities.get(need)
        ]
        contracts = []
        for owned in provider.spec.skills:
            skill = skills.get(owned.name) or {}
            contracts.append({
                "name": owned.name,
                "risk": skills.risk_of(skill) if skill else "read",
                "confirm": skills.confirm_required(skill) if skill else False,
                "reversible": bool(owned.reversible),
            })
        rows.append({
            "name": provider.spec.name,
            "title": provider.spec.title,
            "description": provider.spec.description,
            "available": not missing,
            "reason": "; ".join(missing),
            "requires": list(provider.spec.requires),
            "skills": contracts,
        })
    return rows


def validate() -> list[str]:
    """Detect duplicate ownership and registry drift."""
    from .. import skills

    problems: list[str] = []
    seen: set[str] = set()
    for provider in _PROVIDERS:
        if not provider.spec.name:
            problems.append("provider без имени")
        for owned in provider.spec.skills:
            if owned.name in seen:
                problems.append(f"skill {owned.name} принадлежит нескольким providers")
            seen.add(owned.name)
            skill = skills.get(owned.name)
            if skill is None:
                problems.append(f"{provider.spec.name}: skill {owned.name} отсутствует в registry")
                continue
            declared = str(skill.get("provider") or "")
            if declared != provider.spec.name:
                problems.append(
                    f"{owned.name}: registry provider={declared or '—'}, "
                    f"ожидался {provider.spec.name}")
    for name, skill in skills.SKILLS.items():
        declared = str(skill.get("provider") or "")
        if declared and name not in seen:
            problems.append(f"{name}: provider={declared}, но provider registry не владеет skill")
    return problems

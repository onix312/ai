"""Desktop Perception capability adapter."""
from __future__ import annotations

from typing import Any

from .. import perception
from .base import ProviderSkill, ProviderSpec


class DesktopProvider:
    spec = ProviderSpec(
        name="desktop",
        title="Рабочий стол",
        requires=("windows",),
        description="Win32 → UI Automation → OCR наблюдение за локальным рабочим столом.",
        skills=(ProviderSkill("desktop.observe"),),
    )

    def run(self, skill_name: str, params: dict[str, Any],
            runner: Any | None = None) -> dict[str, Any]:
        if skill_name != "desktop.observe":
            return {"ok": False, "reason": f"Desktop Provider не знает skill «{skill_name}»"}
        return perception.observe(
            str(params.get("title") or ""),
            int(params.get("limit") or 80),
            bool(params.get("screenshot", False)),
            bool(params.get("ocr", True)),
        )


PROVIDER = DesktopProvider()

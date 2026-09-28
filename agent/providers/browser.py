"""Browser capability adapter."""
from __future__ import annotations

from typing import Any

from .. import browser
from .base import ProviderSkill, ProviderSpec


class BrowserProvider:
    spec = ProviderSpec(
        name="browser",
        title="Браузер",
        requires=("browser",),
        description="Структурированный read-only Chromium через локальный DevTools.",
        skills=(
            ProviderSkill("browser.tabs"),
            ProviderSkill("browser.page"),
            ProviderSkill("browser.find"),
            ProviderSkill("browser.selection"),
        ),
    )

    def run(self, skill_name: str, params: dict[str, Any],
            runner: Any | None = None) -> dict[str, Any]:
        if skill_name == "browser.tabs":
            return browser.tabs(int(params.get("limit") or 30))
        if skill_name == "browser.page":
            return browser.page(
                target_id=str(params.get("target_id") or ""),
                max_chars=int(params.get("max_chars") or browser.MAX_TEXT),
            )
        if skill_name == "browser.find":
            return browser.find(
                str(params.get("query") or ""),
                target_id=str(params.get("target_id") or ""),
                limit=int(params.get("limit") or 8),
            )
        if skill_name == "browser.selection":
            return browser.selection(str(params.get("target_id") or ""))
        return {"ok": False, "reason": f"Browser Provider не знает skill «{skill_name}»"}


PROVIDER = BrowserProvider()

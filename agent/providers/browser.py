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
        description="Структурированный read-only Chromium через расширение или локальный DevTools.",
        skills=(
            ProviderSkill("browser.tabs"),
            ProviderSkill("browser.page"),
            ProviderSkill("browser.find"),
            ProviderSkill("browser.selection"),
        ),
    )

    def run(self, skill_name: str, params: dict[str, Any],
            runner: Any | None = None) -> dict[str, Any]:
        browser_name = str(params.get("browser") or "")
        if skill_name == "browser.tabs":
            limit = int(params.get("limit") or 30)
            if browser_name:
                return browser.tabs(limit, browser_name=browser_name)
            return browser.tabs(limit)
        if skill_name == "browser.page":
            options = {"target_id": str(params.get("target_id") or ""),
                       "max_chars": int(params.get("max_chars") or browser.MAX_TEXT)}
            if browser_name:
                options["browser_name"] = browser_name
            return browser.page(**options)
        if skill_name == "browser.find":
            options = {"target_id": str(params.get("target_id") or ""),
                       "limit": int(params.get("limit") or 8)}
            if browser_name:
                options["browser_name"] = browser_name
            return browser.find(str(params.get("query") or ""), **options)
        if skill_name == "browser.selection":
            target_id = str(params.get("target_id") or "")
            if browser_name:
                return browser.selection(target_id, browser_name=browser_name)
            return browser.selection(target_id)
        return {"ok": False, "reason": f"Browser Provider не знает skill «{skill_name}»"}


PROVIDER = BrowserProvider()

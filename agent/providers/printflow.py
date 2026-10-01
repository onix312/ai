"""PrintFlow capability provider.

PrintFlow stays a separate subsystem behind its loopback Client. This provider
owns the assistant-facing skill adapter only; it never weakens PrintFlow's own
action confirmation metadata.
"""
from __future__ import annotations

from typing import Any

from .base import ProviderSkill, ProviderSpec


class PrintFlowProvider:
    spec = ProviderSpec(
        name="printflow",
        title="PrintFlow",
        requires=("panel",),
        description="Профессиональные возможности 3D-производства через локальный PrintFlow API.",
        skills=(
            ProviderSkill("panel.actions"),
            ProviderSkill("panel.do", reversible=False),
            ProviderSkill("panel.ask"),
            ProviderSkill("printflow.context"),
            ProviderSkill("printflow.actions"),
            ProviderSkill("printflow.read"),
            ProviderSkill("printflow.act", reversible=False),
            ProviderSkill("day.briefing"),
            ProviderSkill("day.summary"),
        ),
    )

    def run(self, skill_name: str, params: dict[str, Any],
            runner: Any | None = None) -> dict[str, Any]:
        if runner is None or getattr(runner, "panel", None) is None:
            return {"ok": False, "reason": "PrintFlow Provider не получил локальный panel client"}
        panel = runner.panel
        if skill_name == "panel.actions":
            return panel.actions()
        if skill_name == "printflow.context":
            return panel.context()
        if skill_name == "printflow.actions":
            return panel.domain_actions(str(params.get("domain") or ""))
        if skill_name == "printflow.read":
            action = str(params.get("action") or "").strip()
            inner = params.get("params")
            values = inner if isinstance(inner, dict) else {}
            if not action:
                return {"ok": False, "reason": "Не указано действие PrintFlow"}
            return panel.run_domain_action(action, values, read_only=True)
        if skill_name == "printflow.act":
            action_name = str(params.get("action") or "").strip()
            if not action_name:
                return {"ok": False, "reason": "Не указано действие PrintFlow"}
            action, why = panel.find_domain_action(action_name)
            if action is None:
                return {"ok": False, "reason": why}
            inner = params.get("params")
            values = inner if isinstance(inner, dict) else {}
            # Outer Agent skill has already passed its own confirmation gate.
            # The inner flag remains canonical metadata owned by PrintFlow.
            confirmed = bool(action.get("confirm"))
            result = panel.run_domain_action(
                action_name, values, confirmed=confirmed, read_only=False)
            explain = " ".join(str(params.get("explain") or "").split())[:300]
            result["target"] = explain or str(
                action.get("title") or action.get("id") or action_name)
            if explain and result.get("ok"):
                result["hint"] = explain
            return result
        if skill_name == "panel.ask":
            question = str(params.get("question") or "").strip()
            if not question:
                return {"ok": False, "reason": "Пустой вопрос"}
            return panel.ask(question)
        if skill_name in ("day.briefing", "day.summary"):
            days = max(1, min(90, int(params.get("days") or 1)))
            return panel.day("briefing" if skill_name == "day.briefing" else "summary", days=days)
        if skill_name == "panel.do":
            action, why = panel.find_action(str(params.get("action") or ""))
            if action is None:
                return {"ok": False, "reason": why}
            inner = params.get("params")
            values = inner if isinstance(inner, dict) else {}
            # Confirmation of the inner PrintFlow action remains canonical data
            # from PrintFlow's action catalog. The outer assistant skill has
            # already passed Agent/Runner confirmation policy.
            confirmed = bool(action.get("confirm"))
            result = panel.run_action(action, values, confirmed=confirmed)
            result["title_action"] = str(action.get("title") or action.get("id") or "")
            result["panel_confirm"] = confirmed
            explain = " ".join(str(params.get("explain") or "").split())[:300]
            result["target"] = explain or result["title_action"]
            if not confirmed:
                result["hint"] = "Действие чтения: выполнено без подтверждения"
            elif explain and result.get("ok"):
                result["hint"] = explain
            return result
        return {"ok": False, "reason": f"PrintFlow Provider не знает skill «{skill_name}»"}


PROVIDER = PrintFlowProvider()

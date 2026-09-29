"""Planner 1.0: модель предлагает, реестр и человек решают."""
from __future__ import annotations

import json
import time
import unittest
from unittest.mock import patch

from agent import planner, skills


class _Runner:
    def __init__(self):
        self.caps = {}

    def learned(self):
        return {}

    def catalog(self):
        return skills.catalog(self.caps, {})


class _Tasks:
    def __init__(self):
        self.created = []

    def validate_steps(self, raw):
        checked = []
        for index, item in enumerate(raw):
            skill = skills.get(str(item.get("skill") or ""))
            if skill is None:
                return [], f"Шаг {index + 1}: навыка нет"
            params, errors = skills.check_params(skill, item.get("params") or {})
            if errors:
                return [], "; ".join(errors)
            checked.append({"skill": item["skill"], "params": params})
        return checked, ""

    def create(self, title, steps, goal="", source="", start=False):
        self.created.append({
            "title": title, "steps": steps, "goal": goal,
            "source": source, "start": start,
        })
        return {"ok": True, "task": {
            "id": 11, "title": title, "status": "running",
            "steps": steps, "total_steps": len(steps), "progress": 0,
        }}


class _Agent:
    def __init__(self):
        self.runner = _Runner()
        self.tasks = _Tasks()


def _state():
    return {"ok": True, "model": "test-model", "reason": ""}


class AgentPlannerTests(unittest.TestCase):
    def setUp(self):
        self.agent = _Agent()
        self.planner = planner.Planner(self.agent)

    def _preview(self, payload):
        with patch.object(planner.model, "status", return_value=_state()), \
             patch.object(planner.model, "chat", return_value={
                 "ok": True, "text": json.dumps(payload, ensure_ascii=False),
                 "reason": "", "model": "test-model",
             }):
            return self.planner.preview("подготовь компьютер к работе")

    def test_planner_receives_relevant_bounded_skill_catalog(self):
        captured = {}
        payload = {
            "title": "Экран",
            "summary": "Найти кнопку",
            "ask": "",
            "steps": [{"skill": "screen.find", "params": {"text": "сохранить"}, "why": "найти"}],
        }

        def fake_chat(*_args, system="", **_kwargs):
            captured["system"] = system
            return {"ok": True, "text": json.dumps(payload, ensure_ascii=False),
                    "reason": "", "model": "test-model"}

        self.agent.runner.caps.update({"windows": True, "screen": True})
        with patch.object(planner.model, "status", return_value=_state()), \
             patch.object(planner.model, "chat", side_effect=fake_chat):
            result = self.planner.preview("найди на экране кнопку сохранить")

        self.assertTrue(result["ok"], result)
        self.assertIn("screen.find", captured["system"])
        self.assertNotIn("voice.command:", captured["system"])
        self.assertLess(len(captured["system"]), 8000)

    def test_valid_preview_is_not_executed(self):
        result = self._preview({
            "title": "Рабочее место",
            "summary": "Открыть Telegram и PrintFlow",
            "ask": "",
            "steps": [
                {"skill": "app.open", "params": {"target": "telegram"}, "why": "связь"},
                {"skill": "app.open", "params": {"target": "printflow"}, "why": "цех"},
            ],
        })
        self.assertTrue(result["ok"], result)
        self.assertEqual(2, len(result["plan"]["steps"]))
        self.assertEqual([], self.agent.tasks.created, "preview ничего не исполняет")

    def test_unknown_skill_from_model_is_rejected(self):
        result = self._preview({
            "title": "Опасный",
            "summary": "",
            "ask": "",
            "steps": [{"skill": "format.disk", "params": {}, "why": ""}],
        })
        self.assertFalse(result["ok"])
        self.assertIn("нет", result["reason"])
        self.assertEqual([], self.planner.list())

    def test_invalid_params_are_rejected(self):
        result = self._preview({
            "title": "Открыть",
            "summary": "",
            "ask": "",
            "steps": [{"skill": "app.open", "params": {"unknown": "x"}, "why": ""}],
        })
        self.assertFalse(result["ok"])
        self.assertIn("Шаг 1", result["reason"])

    def test_clarification_does_not_create_draft(self):
        result = self._preview({
            "title": "",
            "summary": "",
            "ask": "Какую программу нужно открыть?",
            "steps": [],
        })
        self.assertTrue(result["ok"])
        self.assertTrue(result["needs_clarification"])
        self.assertEqual([], self.planner.list())

    def test_approve_revalidates_and_creates_task(self):
        result = self._preview({
            "title": "Рабочее место",
            "summary": "",
            "ask": "",
            "steps": [{"skill": "app.open", "params": {"target": "telegram"}, "why": ""}],
        })
        plan_id = result["plan"]["id"]
        approved = self.planner.approve(plan_id)
        self.assertTrue(approved["ok"], approved)
        self.assertEqual(1, len(self.agent.tasks.created))
        self.assertTrue(self.agent.tasks.created[0]["start"])
        self.assertEqual("planner", self.agent.tasks.created[0]["source"])
        self.assertEqual([], self.planner.list(), "approved draft одноразовый")

    def test_plan_cannot_be_approved_twice_in_parallel_state(self):
        result = self._preview({
            "title": "Рабочее место",
            "summary": "",
            "ask": "",
            "steps": [{"skill": "app.open", "params": {"target": "telegram"}, "why": ""}],
        })
        plan_id = result["plan"]["id"]
        self.planner._approving.add(plan_id)
        approved = self.planner.approve(plan_id)
        discarded = self.planner.discard(plan_id)
        self.assertFalse(approved["ok"])
        self.assertIn("уже запускается", approved["reason"])
        self.assertFalse(discarded["ok"])
        self.assertEqual([], self.agent.tasks.created)

    def test_denied_meta_skill_never_enters_plan(self):
        result = self._preview({
            "title": "Рекурсия",
            "summary": "",
            "ask": "",
            "steps": [{"skill": "voice.command", "params": {"text": "что угодно"}, "why": ""}],
        })
        self.assertFalse(result["ok"])
        self.assertIn("запрещён", result["reason"])

    def test_expired_draft_cannot_be_approved(self):
        result = self._preview({
            "title": "Позже",
            "summary": "",
            "ask": "",
            "steps": [{"skill": "app.open", "params": {"target": "telegram"}, "why": ""}],
        })
        plan_id = result["plan"]["id"]
        self.planner._drafts[plan_id]["_expires_at"] = time.time() - 1
        approved = self.planner.approve(plan_id)
        self.assertFalse(approved["ok"])
        self.assertIn("истёк", approved["reason"])


if __name__ == "__main__":
    unittest.main()

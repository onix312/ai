"""Persistent Task Engine: progress, confirmation, pause and resume."""
from __future__ import annotations

import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch

from agent.store import Store
from agent.task_engine import TaskEngine


class _Runner:
    def __init__(self, store):
        self.store = store
        self.caps = {}

    def learned(self):
        return {}


class _Agent:
    def __init__(self, store):
        self.runner = _Runner(store)
        self.calls = []
        self.pending = {}
        self.discarded = []
        self.stopped = False

    def run_skill(self, name, params=None, ask=True):
        self.calls.append((name, dict(params or {})))
        if name == "clipboard.write":
            action_id = f"p{len(self.calls)}"
            self.pending[action_id] = (name, params)
            return {"ok": True, "queued": True, "id": action_id,
                    "requires_confirmation": True}
        return {"ok": True, "skill": name, "done": name, "params": params or {}}

    def discard_action(self, action_id):
        self.discarded.append(action_id)
        return self.pending.pop(action_id, None) is not None

    def execution_stopped(self):
        return self.stopped


class TaskEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "assistant.sqlite3")
        self.agent = _Agent(self.store)
        self.engine = TaskEngine(self.agent)
        self.verifier_patch = patch("agent.task_engine.verifier.verify", return_value={
            "status": "assumed", "reason": "Изолированный исполнитель", "evidence": {}})
        self.verifier_patch.start()

    def tearDown(self):
        self.verifier_patch.stop()
        self.store.close()
        self.tmp.cleanup()

    def _create(self, steps):
        result = self.engine.create("Рабочий режим", steps, goal="Подготовить ПК")
        self.assertTrue(result["ok"], result)
        return int(result["task"]["id"])

    def test_unknown_skill_is_rejected_before_task_is_saved(self):
        result = self.engine.create("Плохая", [{"skill": "no.such.skill", "params": {}}])
        self.assertFalse(result["ok"])
        self.assertEqual([], self.engine.list())

    def test_stop_all_blocks_start_without_failing_task(self):
        task_id = self._create([
            {"skill": "system.volume", "params": {"level": 30}},
        ])
        self.agent.stopped = True
        result = self.engine.start(task_id)
        self.assertFalse(result["ok"])
        self.assertTrue(result["stopped"])
        self.assertEqual("planned", self.engine.get(task_id)["status"])
        self.assertEqual([], self.agent.calls)

    def test_two_safe_steps_finish_and_persist(self):
        task_id = self._create([
            {"skill": "system.volume", "params": {"level": 30}},
            {"skill": "system.media", "params": {"action": "play_pause"}},
        ])
        result = self.engine.run_sync(task_id)
        self.assertTrue(result["ok"])
        task = self.engine.get(task_id)
        self.assertEqual("done", task["status"])
        self.assertEqual(2, task["progress"])
        self.assertEqual(["done", "done"], [step["status"] for step in task["steps"]])

        reloaded = TaskEngine(self.agent).get(task_id)
        self.assertEqual("done", reloaded["status"])
        self.assertEqual(2, reloaded["current_step"])

    def test_failed_verification_stops_before_next_step(self):
        task_id = self._create([
            {"skill": "app.open", "params": {"target": "telegram"}},
            {"skill": "app.open", "params": {"target": "printflow"}},
        ])
        with patch("agent.task_engine.verifier.verify", return_value={
            "status": "failed", "reason": "окно не появилось", "evidence": {},
        }):
            self.engine.run_sync(task_id)
        task = self.engine.get(task_id)
        self.assertEqual("failed", task["status"])
        self.assertEqual(1, len(self.agent.calls), "следующий шаг не должен стартовать")
        self.assertEqual("failed", task["steps"][0]["verification"]["status"])
        self.assertIn("Проверка результата", task["error"])

    def test_verified_evidence_is_persisted(self):
        task_id = self._create([
            {"skill": "app.open", "params": {"target": "telegram"}},
        ])
        with patch("agent.task_engine.verifier.verify", return_value={
            "status": "verified", "reason": "окно найдено",
            "evidence": {"active": "Telegram"},
        }):
            self.engine.run_sync(task_id)
        task = self.engine.get(task_id)
        self.assertEqual("done", task["status"])
        self.assertEqual("verified", task["steps"][0]["verification"]["status"])
        self.assertEqual("Telegram", task["steps"][0]["verification"]["evidence"]["active"])

    def test_confirmation_pauses_execution_and_resumes_after_human(self):
        task_id = self._create([
            {"skill": "clipboard.write", "params": {"text": "привет"}},
            {"skill": "system.volume", "params": {"level": 20}},
        ])
        self.engine.run_sync(task_id)
        task = self.engine.get(task_id)
        self.assertEqual("waiting", task["status"])
        action_id = task["steps"][0]["pending_action"]
        self.assertTrue(action_id)
        self.assertEqual(1, len(self.agent.calls))

        self.engine.on_action_result(action_id, {"ok": True, "done": True}, True)
        deadline = time.time() + 1
        while time.time() < deadline:
            task = self.engine.get(task_id)
            if task["status"] == "done":
                break
            time.sleep(0.01)
        self.assertEqual("done", task["status"])
        self.assertEqual(2, len(self.agent.calls))

    def test_confirmed_step_is_verified_before_resume(self):
        task_id = self._create([
            {"skill": "clipboard.write", "params": {"text": "привет"}},
            {"skill": "app.open", "params": {"target": "telegram"}},
        ])
        self.engine.run_sync(task_id)
        action_id = self.engine.get(task_id)["steps"][0]["pending_action"]
        with patch("agent.task_engine.verifier.verify", return_value={
            "status": "verified", "reason": "буфер совпал", "evidence": {"chars": 6},
        }):
            self.engine.on_action_result(
                action_id,
                {"ok": True, "done": True, "result": {"ok": True, "text": "привет"}},
                True,
            )
        deadline = time.time() + 1
        while time.time() < deadline:
            task = self.engine.get(task_id)
            if task["status"] == "done":
                break
            time.sleep(0.01)
        self.assertEqual("done", task["status"])
        self.assertEqual("verified", task["steps"][0]["verification"]["status"])

    def test_declined_confirmation_pauses_task_without_losing_step(self):
        task_id = self._create([
            {"skill": "clipboard.write", "params": {"text": "секрет"}},
        ])
        self.engine.run_sync(task_id)
        action_id = self.engine.get(task_id)["steps"][0]["pending_action"]
        task = self.engine.on_action_result(
            action_id, {"ok": True, "done": False, "reason": "Отменено человеком"}, False)
        self.assertEqual("paused", task["status"])
        self.assertEqual("pending", task["steps"][0]["status"])
        self.assertEqual("", task["steps"][0]["pending_action"])

    def test_pause_discards_waiting_confirmation(self):
        task_id = self._create([
            {"skill": "clipboard.write", "params": {"text": "x"}},
        ])
        self.engine.run_sync(task_id)
        action_id = self.engine.get(task_id)["steps"][0]["pending_action"]
        result = self.engine.pause(task_id)
        self.assertTrue(result["ok"])
        self.assertEqual("paused", result["task"]["status"])
        self.assertIn(action_id, self.agent.discarded)
        self.assertEqual("pending", result["task"]["steps"][0]["status"])

    def test_expired_confirmation_returns_task_to_paused(self):
        task_id = self._create([
            {"skill": "clipboard.write", "params": {"text": "x"}},
        ])
        self.engine.run_sync(task_id)
        action_id = self.engine.get(task_id)["steps"][0]["pending_action"]
        task = self.engine.on_action_expired(action_id)
        self.assertEqual("paused", task["status"])
        self.assertEqual("pending", task["steps"][0]["status"])
        self.assertEqual("", task["steps"][0]["pending_action"])
        self.assertIn("истекло", task["error"])

    def test_restart_recovers_running_task_to_explicit_pause(self):
        task_id = self._create([
            {"skill": "system.volume", "params": {"level": 30}},
        ])
        self.engine._set_task(task_id, status="running", current_step=0)
        self.engine._set_step(task_id, 0, status="running", started_at="2026-09-28 10:00:00")
        recovered = TaskEngine(self.agent).get(task_id)
        self.assertEqual("paused", recovered["status"])
        self.assertEqual("pending", recovered["steps"][0]["status"])
        self.assertIn("перезапуском", recovered["error"])

    def test_failed_task_cannot_be_restarted_silently(self):
        task_id = self._create([
            {"skill": "system.volume", "params": {"level": 30}},
        ])
        self.engine._set_task(task_id, status="failed", error="boom")
        result = self.engine.start(task_id)
        self.assertFalse(result["ok"])
        self.assertIn("failed", result["reason"])

    def test_applied_replan_history_is_returned_with_task(self):
        task_id = self._create([
            {"skill": "system.volume", "params": {"level": 30}},
            {"skill": "system.media", "params": {"action": "play_pause"}},
        ])
        self.engine._set_task(task_id, status="paused", current_step=0)
        result = self.engine.replace_remaining(
            task_id,
            0,
            [{"skill": "system.volume", "params": {"level": 55}}],
            "Проверка не прошла",
        )
        self.assertTrue(result["ok"], result)
        task = self.engine.get(task_id)
        self.assertEqual(1, len(task["replans"]))
        replan = task["replans"][0]
        self.assertEqual("Проверка не прошла", replan["reason"])
        self.assertEqual(["system.volume", "system.media"],
                         [row["skill"] for row in replan["old_tail"]])
        self.assertEqual(["system.volume"],
                         [row["skill"] for row in replan["new_tail"]])
        reloaded = TaskEngine(self.agent).get(task_id)
        self.assertEqual("Проверка не прошла", reloaded["replans"][0]["reason"])

    def test_cancel_is_terminal_and_discards_pending(self):
        task_id = self._create([
            {"skill": "clipboard.write", "params": {"text": "x"}},
            {"skill": "system.volume", "params": {"level": 50}},
        ])
        self.engine.run_sync(task_id)
        action_id = self.engine.get(task_id)["steps"][0]["pending_action"]
        result = self.engine.cancel(task_id)
        self.assertEqual("cancelled", result["task"]["status"])
        self.assertIn(action_id, self.agent.discarded)
        self.assertFalse(self.engine.start(task_id)["ok"])


if __name__ == "__main__":
    unittest.main()

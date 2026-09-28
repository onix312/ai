"""Persistent Task Engine: progress, confirmation, pause and resume."""
from __future__ import annotations

import pathlib
import tempfile
import time
import unittest

from agent.store import Store
from agent.task_engine import TaskEngine


class _Runner:
    def __init__(self, store):
        self.store = store

    def learned(self):
        return {}


class _Agent:
    def __init__(self, store):
        self.runner = _Runner(store)
        self.calls = []
        self.pending = {}
        self.discarded = []

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


class TaskEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "assistant.sqlite3")
        self.agent = _Agent(self.store)
        self.engine = TaskEngine(self.agent)

    def tearDown(self):
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

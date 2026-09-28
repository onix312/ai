"""Replan drafts never execute and can replace only the unfinished task suffix."""
from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from agent import replanner, skills
from agent.store import Store
from agent.task_engine import TaskEngine


class _Runner:
    def __init__(self, store):
        self.store = store
        self.caps = {}

    def learned(self):
        return {}

    def catalog(self):
        return skills.catalog(self.caps, {})

    def refresh_capabilities(self):
        return None


class _Agent:
    def __init__(self, store):
        self.runner = _Runner(store)
        self.calls = []
        self.tasks = TaskEngine(self)
        self.replanner = replanner.Replanner(self)

    def run_skill(self, name, params=None, ask=True):
        self.calls.append((name, params))
        return {"ok": True}


class ReplannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "assistant.sqlite3")
        self.agent = _Agent(self.store)
        created = self.agent.tasks.create("Тест", [
            {"skill": "system.volume", "params": {"level": 20}},
            {"skill": "system.media", "params": {"action": "play_pause"}},
            {"skill": "system.volume", "params": {"level": 30}},
        ])
        self.task_id = created["task"]["id"]
        self.agent.tasks._set_step(self.task_id, 0, status="done", result={"ok": True})
        self.agent.tasks._set_task(self.task_id, status="failed", current_step=1,
                                   error="Проверка результата: нет события")
        self.agent.tasks._set_step(self.task_id, 1, status="failed", result={
            "ok": True, "_verification": {"status": "failed", "reason": "нет события"}})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _preview(self, steps=None):
        payload = {"summary": "Проверить состояние", "steps": steps or [
            {"skill": "system.health", "params": {}, "why": "проверка"}]}
        with patch.object(replanner.model, "status", return_value={"ok": True, "model": "test"}), \
             patch.object(replanner.model, "chat", return_value={
                 "ok": True, "text": json.dumps(payload, ensure_ascii=False)}):
            return self.agent.replanner.preview(self.task_id)

    def test_preview_and_approve_preserve_done_and_pause(self):
        before = self.agent.tasks.get(self.task_id)
        draft = self._preview()
        self.assertTrue(draft["ok"], draft)
        self.assertEqual([], self.agent.calls)
        self.assertEqual(before["steps"], self.agent.tasks.get(self.task_id)["steps"])
        result = self.agent.replanner.approve(draft["replan"]["id"])
        self.assertTrue(result["ok"], result)
        task = result["task"]
        self.assertEqual("paused", task["status"])
        self.assertEqual(before["steps"][0]["id"], task["steps"][0]["id"])
        self.assertEqual("done", task["steps"][0]["status"])
        self.assertEqual("system.health", task["steps"][1]["skill"])
        self.assertEqual([], self.agent.calls)
        self.assertEqual(1, len(self.store._rows("SELECT * FROM assistant_task_replans")))
        self.assertFalse(self.agent.replanner.approve(draft["replan"]["id"])["ok"])

    def test_stale_draft_cannot_replace_changed_tail(self):
        draft = self._preview()["replan"]
        self.agent.tasks._set_step(self.task_id, 2, result={"changed": True})
        result = self.agent.replanner.approve(draft["id"])
        self.assertFalse(result["ok"])
        self.assertIn("изменилась", result["reason"])
        self.assertEqual(3, len(self.agent.tasks.get(self.task_id)["steps"]))

    def test_model_cannot_invent_skill_or_repeat_failed_write(self):
        invented = self._preview([{"skill": "invented.run", "params": {}}])
        self.assertFalse(invented["ok"])
        self.assertEqual([], self.agent.replanner.list())
        self.store._run(
            "UPDATE assistant_task_steps SET skill=?,params_json=? WHERE task_id=? AND seq=1",
            ("clipboard.write", json.dumps({"text": "again"}), self.task_id))
        self.agent.runner.caps["clipboard"] = True
        repeated = self._preview([{"skill": "clipboard.write",
                                   "params": {"text": "again"}}])
        self.assertFalse(repeated["ok"])
        self.assertIn("повторить", repeated["reason"])

    def test_discard_and_expiry(self):
        draft = self._preview()["replan"]
        self.assertTrue(self.agent.replanner.discard(draft["id"])["ok"])
        self.assertFalse(self.agent.replanner.approve(draft["id"])["ok"])
        draft = self._preview()["replan"]
        self.agent.replanner._drafts[draft["id"]]["expires_at"] = 0
        self.assertFalse(self.agent.replanner.approve(draft["id"])["ok"])

    def test_approval_rechecks_skill_availability(self):
        draft = self._preview()["replan"]
        self.agent.runner.caps["unrelated"] = False
        with patch.object(skills, "availability", return_value=(False, "недоступно")):
            result = self.agent.replanner.approve(draft["id"])
        self.assertFalse(result["ok"])
        self.assertEqual("failed", self.agent.tasks.get(self.task_id)["status"])

    def test_api_operations_use_opaque_id(self):
        from agent.server import Agent
        preview = self._preview()["replan"]
        listing = Agent.replan_op(self.agent, {"op": "list"})
        self.assertEqual(preview["id"], listing["replans"][0]["id"])
        self.assertFalse(Agent.replan_op(self.agent, {"op": "approve", "id": "wrong"})["ok"])
        self.assertTrue(Agent.replan_op(self.agent, {"op": "discard", "id": preview["id"]})["ok"])

    def test_http_contract_with_native_client(self):
        from agent.native_ui.backend import BackendClient
        from agent.server import Agent, AgentHandler

        self.agent.replan_op = lambda body: Agent.replan_op(self.agent, body)
        handler = type("ReplanHandler", (AgentHandler,), {"agent": self.agent})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = BackendClient(agent_url=f"http://127.0.0.1:{server.server_port}")
        try:
            reply = {"ok": True, "text": json.dumps({"summary": "Проверить", "steps": [
                {"skill": "system.health", "params": {}}]})}
            with patch.object(replanner.model, "status", return_value={"ok": True}), \
                 patch.object(replanner.model, "chat", return_value=reply):
                preview = client.replan_op("preview", task_id=self.task_id)
            self.assertTrue(preview["ok"], preview)
            self.assertEqual(1, len(client.replans()["replans"]))
            result = client.replan_op("approve", preview["replan"]["id"])
            self.assertTrue(result["ok"], result)
            self.assertEqual("paused", result["task"]["status"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_failed_verification_creates_preview_without_execution(self):
        self.agent.tasks._set_step(self.task_id, 1, status="pending", result={})
        self.agent.tasks._set_task(self.task_id, status="paused", current_step=1, error="")
        reply = {"ok": True, "text": json.dumps({"summary": "Проверить состояние", "steps": [
            {"skill": "system.health", "params": {}}]})}
        with patch("agent.task_engine.verifier.verify", return_value={
                "status": "failed", "reason": "нет события", "evidence": {}}), \
             patch.object(replanner.model, "status", return_value={"ok": True, "model": "test"}), \
             patch.object(replanner.model, "chat", return_value=reply):
            self.agent.tasks.run_sync(self.task_id)
            deadline = time.time() + 2
            while not self.agent.replanner.list() and time.time() < deadline:
                time.sleep(0.01)
        self.assertEqual("failed", self.agent.tasks.get(self.task_id)["status"])
        self.assertEqual(1, len(self.agent.calls))
        self.assertEqual(1, len(self.agent.replanner.list()))


if __name__ == "__main__":
    unittest.main()

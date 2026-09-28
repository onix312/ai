"""Autonomy Levels 1.0 policy and execution gates."""
from __future__ import annotations

import pathlib
import tempfile
import unittest

from agent import autonomy, executor
from agent.panel_client import Client
from agent.server import Agent
from agent.store import Store


class AutonomyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "assistant.sqlite3")
        self.policy = autonomy.AutonomyPolicy(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_default_preserves_current_agent_features(self):
        payload = self.policy.payload()
        self.assertEqual("agent", payload["level"])
        self.assertEqual("autopilot", payload["providers"]["browser"]["hard_max"])
        self.assertEqual("agent", payload["providers"]["printflow"]["hard_max"])

    def test_observer_can_read_but_not_act(self):
        self.policy.update("observer")
        read_skill = {"risk": "read", "provider": "browser"}
        own_skill = {"risk": "own", "provider": "personal"}
        self.assertTrue(self.policy.check_skill(read_skill, "direct")[0])
        allowed, reason = self.policy.check_skill(own_skill, "direct")
        self.assertFalse(allowed)
        self.assertIn("assistant", reason)

    def test_provider_ceiling_can_only_be_lowered(self):
        denied = self.policy.update(providers={"desktop": "autopilot"})
        self.assertFalse(denied["ok"])
        self.assertEqual("agent", denied["hard_max"])

        saved = self.policy.update(providers={"browser": "assistant"})
        self.assertTrue(saved["ok"])
        self.assertEqual("assistant", saved["providers"]["browser"]["level"])
        allowed, _ = self.policy.check("operator", "browser")
        self.assertFalse(allowed)

    def test_policy_persists_and_resets(self):
        self.policy.update("operator", {"printflow": "assistant"})
        again = autonomy.AutonomyPolicy(self.store)
        self.assertEqual("operator", again.level())
        self.assertEqual("assistant", again.provider_cap("printflow"))
        reset = again.reset()
        self.assertEqual("agent", reset["level"])
        self.assertEqual("agent", reset["providers"]["printflow"]["level"])


class AgentAutonomyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "assistant.sqlite3")
        self.agent = Agent()
        self.agent._runner = executor.Runner(
            store=self.store, panel=Client("http://127.0.0.1:1"))
        self.agent._autonomy = autonomy.AutonomyPolicy(self.store)

    def tearDown(self):
        try:
            self.agent.microphone.shutdown()
        except Exception:
            pass
        self.store.close()
        self.tmp.cleanup()

    def test_observer_blocks_write_skill_before_confirmation_queue(self):
        self.agent.autonomy.update("observer")
        result = self.agent.run_skill(
            "clipboard.write", {"text": "не писать"}, ask=False)
        self.assertFalse(result["ok"])
        self.assertTrue(result["autonomy_blocked"])
        self.assertEqual([], self.agent.pending())

    def test_assistant_allows_write_to_reach_normal_confirmation(self):
        self.agent.autonomy.update("assistant")
        result = self.agent.run_skill(
            "clipboard.write", {"text": "черновик"}, ask=False)
        self.assertTrue(result["ok"])
        self.assertTrue(result["queued"])
        self.assertTrue(result["requires_confirmation"])

    def test_pending_action_is_rechecked_after_level_is_lowered(self):
        self.agent.autonomy.update("assistant")
        queued = self.agent.run_skill(
            "clipboard.write", {"text": "не выполнять"}, ask=False)
        self.agent.autonomy.update("observer")
        result = self.agent.confirm_action(queued["id"], True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["autonomy_blocked"])

    def test_raw_ui_action_is_blocked_for_observer(self):
        self.agent.autonomy.update("observer")
        result = self.agent.queue_action("click", {"x": 1, "y": 1}, ask=False)
        self.assertFalse(result["ok"])
        self.assertTrue(result["autonomy_blocked"])


if __name__ == "__main__":
    unittest.main()

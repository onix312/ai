"""Persona Engine 1.0: persistence, validation and prompt contract."""
from __future__ import annotations

import pathlib
import tempfile
import unittest

from agent import brain
from agent.persona import DEFAULTS, Persona, safety_invariant
from agent.store import Store


class PersonaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "persona.sqlite3")
        self.persona = Persona(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_defaults_preserve_current_nozza_style(self):
        self.assertEqual(DEFAULTS, self.persona.profile())
        prompt = self.persona.prompt_fragment()
        self.assertIn("на «вы»", prompt)
        self.assertIn("кратко", prompt)
        self.assertIn("дружелюб", prompt)

    def test_update_persists_only_whitelisted_persona_fields(self):
        result = self.persona.update({
            "address": "informal",
            "verbosity": "detailed",
            "humor": "off",
            "initiative": "quiet",
            "relationship": "professional",
        })
        self.assertTrue(result["ok"])
        fresh = Persona(self.store).profile()
        self.assertEqual("informal", fresh["address"])
        self.assertEqual("detailed", fresh["verbosity"])
        self.assertEqual("off", fresh["humor"])
        self.assertEqual("quiet", fresh["initiative"])
        self.assertEqual("professional", fresh["relationship"])

    def test_safety_and_permission_fields_are_rejected(self):
        before = self.persona.profile()
        for patch in (
            {"confirm": "off"},
            {"safety": "disabled"},
            {"autonomy": "root"},
            {"provider": "all"},
            {"risk": "read"},
        ):
            with self.subTest(patch=patch):
                result = self.persona.update(patch)
                self.assertFalse(result["ok"])
                self.assertEqual(before, self.persona.profile())

    def test_invalid_enum_rejects_whole_patch(self):
        result = self.persona.update({"humor": "chaos", "address": "informal"})
        self.assertFalse(result["ok"])
        self.assertEqual(DEFAULTS, self.persona.profile())

    def test_reset_removes_only_persona_preferences(self):
        self.store.set_preference("voice.volume", "80")
        self.persona.update({"address": "informal", "humor": "off"})
        result = self.persona.reset()
        self.assertTrue(result["ok"])
        self.assertEqual(DEFAULTS, result["profile"])
        self.assertEqual("80", self.store.get_preference("voice.volume")["value"])

    def test_payload_has_ui_options_and_labels(self):
        payload = self.persona.payload()
        self.assertTrue(payload["ok"])
        self.assertIn("formal", payload["options"]["address"])
        self.assertEqual("на «вы»", payload["labels"]["address"]["formal"])

    def test_safety_invariant_is_explicit(self):
        text = safety_invariant()
        self.assertIn("не меняет", text)
        self.assertIn("подтверждения", text)


class BrainPersonaPromptTests(unittest.TestCase):
    def test_brain_uses_agent_persona_fragment(self):
        class StubPersona:
            def prompt_fragment(self):
                return "PERSONA_SENTINEL"

        class StubAgent:
            persona = StubPersona()

        b = brain.Brain(StubAgent())
        self.assertEqual("PERSONA_SENTINEL", b._persona_prompt())

    def test_brain_has_safe_fallback_without_persona(self):
        b = brain.Brain(object())
        text = b._persona_prompt()
        self.assertIn("«вы»", text)
        self.assertIn("кратко", text)


    def test_informal_address_applies_to_rule_based_smalltalk(self):
        class Runner:
            def __init__(self, store):
                self.store = store

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(pathlib.Path(tmp) / "talk.sqlite3")
            try:
                persona = Persona(store)
                persona.update({"address": "informal"})
                agent = type("Agent", (), {"runner": Runner(store), "persona": persona})()
                b = brain.Brain(agent)
                answer = b._small_talk("main", "спасибо", "thanks", [], 0.0)
                self.assertIn("Обращайся", answer["reply"])
                self.assertNotIn("Обращайтесь", answer["reply"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()

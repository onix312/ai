"""Safety-gate нарезки: машина состояний блокирует всё, что не проверено."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.slicer_profile import PROFILE_ID  # noqa: E402
from connector.printflow.slicer_run import (  # noqa: E402
    SliceConfig, SliceMachine, SliceStage, config_from_settings)


def machine(**overrides) -> SliceMachine:
    config = SliceConfig(provider="printflow", manual_only=True,
                         first_print_verified=False)
    return SliceMachine(config=SliceConfig(**{**config.__dict__, **overrides}))


def ready(**overrides) -> SliceMachine:
    """Машина, дошедшая до готового G-code."""
    item = machine(**overrides)
    item.audit_model("cube.stl", ok=True)
    item.start()
    item.sliced(ok=True, output="cube.gcode")
    item.audited(ok=True)
    return item


class GateTests(unittest.TestCase):
    def test_external_provider_blocks_engine_gate(self):
        item = machine(provider="external")
        self.assertFalse(item.config.engine_gate)
        self.assertEqual(item.audit_model("cube.stl", ok=True)["stage"],
                         SliceStage.BLOCKED.value)

    def test_unknown_profile_blocks(self):
        item = machine(profile_id="ender-3")
        snapshot = item.audit_model("cube.stl", ok=True)
        self.assertEqual(snapshot["stage"], SliceStage.BLOCKED.value)
        self.assertIn("профиль", snapshot["reason"])

    def test_slicing_requires_audit_first(self):
        item = machine()
        snapshot = item.start()
        self.assertEqual(snapshot["stage"], SliceStage.BLOCKED.value)
        self.assertIn("после аудита", snapshot["reason"])

    def test_bad_model_blocks_before_slicing(self):
        item = machine()
        self.assertEqual(item.audit_model("cube.stl", ok=False)["stage"],
                         SliceStage.BLOCKED.value)

    def test_failed_slice_is_error_not_ready(self):
        item = machine()
        item.audit_model("cube.stl", ok=True)
        item.start()
        snapshot = item.sliced(ok=False)
        self.assertEqual(snapshot["stage"], SliceStage.ERROR.value)
        self.assertIn("ошибкой", snapshot["reason"])

    def test_failed_gcode_audit_is_error(self):
        item = machine()
        item.audit_model("cube.stl", ok=True)
        item.start()
        item.sliced(ok=True, output="cube.gcode")
        snapshot = item.audited(ok=False, reason="нет temperatures")
        self.assertEqual(snapshot["stage"], SliceStage.ERROR.value)
        self.assertIn("temperatures", snapshot["reason"])

    def test_ready_without_operator_is_not_enqueued(self):
        item = ready()
        self.assertEqual(item.stage, SliceStage.READY)
        self.assertFalse(item.enqueue_allowed())
        self.assertIn("Ручной режим", item.blocked_reason())

    def test_manual_mode_never_prints_unattended(self):
        item = ready(auto_print=True, auto_enqueue=True,
                     first_print_verified=True)
        self.assertFalse(item.print_allowed())

    def test_auto_queue_needs_verification(self):
        item = ready(manual_only=False, auto_enqueue=True,
                     first_print_verified=False)
        self.assertFalse(item.enqueue_allowed())
        self.assertIn("первая печать", item.blocked_reason().lower())

    def test_verified_auto_mode_allows_queue_but_not_print(self):
        item = ready(manual_only=False, auto_enqueue=True,
                     first_print_verified=True, max_cycles=3)
        self.assertTrue(item.enqueue_allowed())
        self.assertFalse(item.print_allowed())

    def test_unattended_print_needs_series(self):
        item = ready(manual_only=False, auto_enqueue=True, auto_print=True,
                     first_print_verified=True, max_cycles=3)
        self.assertTrue(item.print_allowed())
        single = ready(manual_only=False, auto_enqueue=True, auto_print=True,
                       first_print_verified=True, max_cycles=1)
        self.assertFalse(single.print_allowed())

    def test_operator_confirmation_unblocks(self):
        item = ready(manual_only=False, auto_enqueue=True,
                     first_print_verified=True)
        self.assertTrue(item.enqueue_allowed())
        item.confirm(True)
        self.assertTrue(item.gates()["operator"])

    def test_gates_are_reported(self):
        gates = ready().gates()
        self.assertTrue(gates["engine"])
        self.assertTrue(gates["profile"])
        self.assertFalse(gates["verification"])
        self.assertFalse(gates["operator"])
        self.assertFalse(gates["unattended"])


class ConfigFromSettingsTests(unittest.TestCase):
    def test_factory_defaults_are_manual_and_external(self):
        config = config_from_settings({})
        self.assertEqual(config.provider, "external")
        self.assertTrue(config.manual_only)
        self.assertFalse(config.first_print_verified)

    def test_settings_are_mapped(self):
        config = config_from_settings({
            "slicer_provider": "printflow",
            "slicer_mode": "auto",
            "slicer_first_print_verified": True,
            "slicer_auto_enqueue": True,
            "slicer_max_cycles": 5,
            "slicer_profile": PROFILE_ID,
        })
        self.assertTrue(config.engine_gate)
        self.assertFalse(config.manual_only)
        self.assertTrue(config.verification_gate)
        self.assertTrue(config.unattended_gate)

    def test_empty_settings_are_safe(self):
        self.assertEqual(config_from_settings(None).provider, "external")
        self.assertEqual(config_from_settings("плохие данные").manual_only, True)


if __name__ == "__main__":
    unittest.main()

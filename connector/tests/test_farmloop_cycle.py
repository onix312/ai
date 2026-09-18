import unittest

from connector.printflow.farmloop_cycle import CycleConfig, CycleMachine


class FarmLoopCycleTests(unittest.TestCase):
    def good(self, **kw):
        base = dict(max_cycles=3, auto_next=True, sensor_mode="camera",
                    mechanics_verified=True, template_verified=True,
                    pusher_enabled=True, bender_enabled=True)
        base.update(kw)
        return CycleMachine(CycleConfig(**base))

    def test_unknown_state_blocks(self):
        machine = self.good()
        self.assertEqual(machine.start()["state"], "printing")
        self.assertEqual(machine.cooled(True)["state"], "blocked")
        self.assertIn("cooling", machine.reason)

    def test_successful_cycle_allows_next_only_after_empty_confirmation(self):
        machine = self.good()
        machine.start()
        machine.print_finished()
        machine.cooled(True)
        machine.detach_finished()
        self.assertFalse(machine.next_allowed())
        result = machine.empty_confirmed(True)
        self.assertEqual(result["state"], "complete")
        self.assertTrue(result["next_allowed"])

    def test_false_empty_confirmation_blocks(self):
        machine = self.good()
        machine.start(); machine.print_finished(); machine.cooled(True); machine.detach_finished()
        result = machine.empty_confirmed(False)
        self.assertEqual(result["state"], "blocked")
        self.assertFalse(result["next_allowed"])

    def test_missing_physical_gate_blocks_start(self):
        machine = self.good(pusher_enabled=False)
        result = machine.start()
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(machine.cycle, 0)

    def test_manual_mode_never_allows_next(self):
        machine = self.good(sensor_mode="manual")
        machine.start(); machine.print_finished(); machine.cooled(True); machine.detach_finished(); machine.empty_confirmed(True)
        self.assertFalse(machine.next_allowed())

    def test_detach_attempt_limit(self):
        machine = self.good(max_detach_attempts=2)
        machine.start(); machine.print_finished(); machine.cooled(True)
        self.assertEqual(machine.detach_finished(False)["state"], "detaching")
        self.assertEqual(machine.detach_finished(False)["state"], "error")


if __name__ == "__main__":
    unittest.main()

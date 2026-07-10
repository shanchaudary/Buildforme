import unittest

from buildforme.run_state import (
    allowed_transitions,
    can_transition,
    is_terminal,
    transition_run,
)


class RunStateTests(unittest.TestCase):
    def test_valid_transitions(self):
        self.assertTrue(can_transition("draft", "awaiting_preflight"))
        self.assertTrue(can_transition("awaiting_preflight", "awaiting_approval"))
        self.assertTrue(can_transition("approved", "queued"))
        self.assertTrue(can_transition("running", "completed"))

    def test_invalid_transitions_rejected(self):
        self.assertFalse(can_transition("draft", "running"))
        self.assertFalse(can_transition("completed", "running"))
        with self.assertRaises(ValueError):
            transition_run({"status": "draft", "id": "r1"}, "running", "t")

    def test_terminal_cannot_restart(self):
        self.assertTrue(is_terminal("completed"))
        with self.assertRaises(ValueError):
            transition_run({"status": "completed", "id": "r1"}, "draft", "t")

    def test_cancel_path(self):
        run = {"status": "running", "id": "r1", "status_history": []}
        run = transition_run(run, "cancel_requested", "shan", "stop")
        run = transition_run(run, "cancelled", "shan", "stopped")
        self.assertEqual(run["status"], "cancelled")
        self.assertTrue(is_terminal("cancelled"))

    def test_allowed_transitions_deterministic(self):
        self.assertEqual(allowed_transitions("draft"), sorted(allowed_transitions("draft")))
        self.assertIn("awaiting_preflight", allowed_transitions("draft"))


if __name__ == "__main__":
    unittest.main()

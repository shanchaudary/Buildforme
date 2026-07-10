"""Versioned Stage 6 run mutations — no unrestricted existing-run writes."""

from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from buildforme.run_state import can_reach, is_terminal, transition_run
from buildforme.storage import LocalStore


class RunMutationAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")
        self.run = self.store.save_run_for_setup(
            {
                "id": "run-mut-1",
                "project_id": "p",
                "provider_id": "codex",
                "repository": "owner/repo",
                "status": "running",
                "execution_mode": "live_supervised",
                "row_version": 1,
            }
        )

    def test_unversioned_update_rejected(self):
        run = self.store.get_run("run-mut-1")
        run["status"] = "failed"
        with self.assertRaisesRegex(ValueError, "expected_row_version"):
            self.store.save_run(run)

    def test_stale_completion_cannot_overwrite_cancel(self):
        run = self.store.get_run("run-mut-1")
        # Cancel wins
        cancelled = transition_run(run, "cancel_requested", "shan", "stop")
        cancelled = transition_run(cancelled, "cancelled", "shan", "stopped")
        self.store.commit_run_mutation(
            cancelled,
            expected_row_version=int(run.get("row_version") or 1),
            event_type="run_cancelled",
            event_summary="cancelled",
            event_actor="shan",
        )
        # Stale completion attempt with old version
        stale = dict(run)
        stale = transition_run(stale, "needs_review", "system", "done")
        with self.assertRaisesRegex(ValueError, "stale|terminal|overwrite"):
            self.store.commit_run_mutation(
                stale,
                expected_row_version=int(run.get("row_version") or 1),
                event_type="supervised_finished",
                event_summary="done",
            )
        final = self.store.get_run("run-mut-1")
        self.assertEqual(final["status"], "cancelled")
        # No needs_review event from stale
        types = [e.get("event_type") for e in self.store.list_run_events("run-mut-1")]
        self.assertNotIn("supervised_finished", types)

    def test_stale_timeout_cannot_overwrite_terminal(self):
        run = self.store.get_run("run-mut-1")
        done = transition_run(dict(run), "needs_review", "system", "ok")
        self.store.commit_run_mutation(
            done,
            expected_row_version=int(run.get("row_version") or 1),
            event_type="supervised_finished",
            event_summary="ok",
        )
        timed = transition_run(dict(run), "timed_out", "system", "timeout")
        with self.assertRaises(ValueError):
            self.store.commit_run_mutation(
                timed,
                expected_row_version=int(run.get("row_version") or 1),
                event_type="supervised_timed_out",
                event_summary="timeout",
            )
        self.assertEqual(self.store.get_run("run-mut-1")["status"], "needs_review")

    def test_valid_mutation_writes_event_and_bumps_version(self):
        run = self.store.get_run("run-mut-1")
        v0 = int(run.get("row_version") or 1)
        run = transition_run(run, "needs_review", "system", "done")
        saved = self.store.commit_run_mutation(
            run,
            expected_row_version=v0,
            event_type="supervised_finished",
            event_summary="done",
        )
        self.assertEqual(saved["status"], "needs_review")
        self.assertEqual(int(saved["row_version"]), v0 + 1)
        events = self.store.list_run_events("run-mut-1")
        self.assertTrue(any(e.get("event_type") == "supervised_finished" for e in events))

    def test_same_state_metadata_with_event(self):
        run = self.store.get_run("run-mut-1")
        v0 = int(run.get("row_version") or 1)
        run["process_result"] = {"exit_code": 1, "ok": False}
        saved = self.store.commit_run_mutation(
            run,
            expected_row_version=v0,
            event_type="process_snapshot",
            event_summary="attach process",
        )
        self.assertEqual(saved["status"], "running")
        self.assertEqual(saved["process_result"]["exit_code"], 1)
        self.assertEqual(int(saved["row_version"]), v0 + 1)

    def test_terminal_protection_matrix(self):
        cases = [
            ("completed", "running"),
            ("cancelled", "needs_review"),
            ("rejected", "approved"),
            ("timed_out", "completed"),
        ]
        for terminal, nxt in cases:
            rid = f"run-{terminal}-{nxt}"
            self.store.save_run_for_setup(
                {
                    "id": rid,
                    "project_id": "p",
                    "provider_id": "codex",
                    "repository": "o/r",
                    "status": terminal,
                    "execution_mode": "live_supervised",
                }
            )
            run = self.store.get_run(rid)
            run["status"] = nxt
            with self.assertRaises(ValueError):
                self.store.commit_run_mutation(
                    run,
                    expected_row_version=int(run.get("row_version") or 1),
                    event_type="illegal",
                    event_summary="nope",
                )
            self.assertEqual(self.store.get_run(rid)["status"], terminal)

    def test_duplicate_completion_second_stale(self):
        run = self.store.get_run("run-mut-1")
        v0 = int(run.get("row_version") or 1)
        first = transition_run(dict(run), "needs_review", "system", "a")
        self.store.commit_run_mutation(
            first,
            expected_row_version=v0,
            event_type="supervised_finished",
            event_summary="a",
        )
        second = transition_run(dict(run), "needs_review", "system", "b")
        with self.assertRaises(ValueError):
            self.store.commit_run_mutation(
                second,
                expected_row_version=v0,
                event_type="supervised_finished",
                event_summary="b",
            )
        events = [
            e
            for e in self.store.list_run_events("run-mut-1")
            if e.get("event_type") == "supervised_finished"
        ]
        self.assertEqual(len(events), 1)

    def test_can_reach_multi_hop(self):
        self.assertTrue(can_reach("approved", "running"))
        self.assertTrue(can_reach("running", "needs_review"))
        self.assertFalse(can_reach("cancelled", "needs_review"))
        self.assertFalse(can_reach("completed", "running"))

    def test_runtime_service_has_no_unrestricted_save_run(self):
        import inspect
        import buildforme.execution_service as es

        src = inspect.getsource(es)
        # Disallow store.save_run( without for_setup / expected
        self.assertNotIn("store.save_run(", src)
        self.assertNotIn("store.s6.save_run(", src)


if __name__ == "__main__":
    unittest.main()

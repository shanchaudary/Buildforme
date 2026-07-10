"""Versioned Stage 6 run mutations — protected fields + explicit transitions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from buildforme.execution_store import (
    MUTATION_METADATA_ALLOWLISTS,
    PROTECTED_AUTHORITY_FIELDS,
)
from buildforme.run_state import can_transition, transition_run
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
                "task_id": "t1",
                "packet_id": "pk1",
                "provider_id": "codex",
                "repository": "owner/repo",
                "repository_local_path": "/repo",
                "baseline_commit": "b" * 40,
                "execution_branch": "feature/x",
                "operating_mode": "IMPLEMENTATION",
                "risk": "YELLOW",
                "execution_mode": "live_supervised",
                "mode": "live_supervised",
                "transport": "cli",
                "requested_capabilities": ["read_repository"],
                "scope_fingerprint": "scope-1",
                "constitution_version": "1",
                "constitution_hash": "c" * 64,
                "constitution_lease_id": "lease-1",
                "constitution_lease_fingerprint": "lf",
                "status": "running",
                "row_version": 1,
            }
        )

    def _commit(self, run, **kwargs):
        defaults = dict(
            expected_row_version=int(run.get("row_version") or 1),
            mutation_type="status_transition",
            event_type="test_event",
            event_summary="test",
            event_actor="system",
        )
        defaults.update(kwargs)
        return self.store.commit_run_mutation(run, **defaults)

    def test_unversioned_update_rejected(self):
        run = self.store.get_run("run-mut-1")
        run["status"] = "failed"
        with self.assertRaisesRegex(ValueError, "expected_row_version"):
            self.store.save_run(run)

    def test_stale_completion_cannot_overwrite_cancel(self):
        run = self.store.get_run("run-mut-1")
        cancelled = transition_run(run, "cancel_requested", "shan", "stop")
        cancelled = transition_run(cancelled, "cancelled", "shan", "stopped")
        self._commit(
            cancelled,
            expected_row_version=int(run.get("row_version") or 1),
            mutation_type="cancel",
            event_type="run_cancelled",
            transition_path=["running", "cancel_requested", "cancelled"],
        )
        stale = transition_run(dict(run), "needs_review", "system", "done")
        with self.assertRaisesRegex(ValueError, "stale|terminal|overwrite"):
            self._commit(
                stale,
                expected_row_version=int(run.get("row_version") or 1),
                mutation_type="supervised_finished",
                event_type="supervised_finished",
            )
        final = self.store.get_run("run-mut-1")
        self.assertEqual(final["status"], "cancelled")
        types = [e.get("event_type") for e in self.store.list_run_events("run-mut-1")]
        self.assertNotIn("supervised_finished", types)

    def test_stale_timeout_cannot_overwrite_terminal(self):
        run = self.store.get_run("run-mut-1")
        done = transition_run(dict(run), "needs_review", "system", "ok")
        self._commit(
            done,
            expected_row_version=int(run.get("row_version") or 1),
            mutation_type="supervised_finished",
            event_type="supervised_finished",
        )
        timed = transition_run(dict(run), "timed_out", "system", "timeout")
        with self.assertRaises(ValueError):
            self._commit(
                timed,
                expected_row_version=int(run.get("row_version") or 1),
                mutation_type="failure_detail",
                event_type="supervised_timed_out",
            )
        self.assertEqual(self.store.get_run("run-mut-1")["status"], "needs_review")

    def test_valid_mutation_writes_event_and_bumps_version(self):
        run = self.store.get_run("run-mut-1")
        v0 = int(run.get("row_version") or 1)
        run = transition_run(run, "needs_review", "system", "done")
        saved = self._commit(
            run,
            expected_row_version=v0,
            mutation_type="supervised_finished",
            event_type="supervised_finished",
        )
        self.assertEqual(saved["status"], "needs_review")
        self.assertEqual(int(saved["row_version"]), v0 + 1)

    def test_same_state_metadata_with_event(self):
        run = self.store.get_run("run-mut-1")
        v0 = int(run.get("row_version") or 1)
        run["process_result"] = {"exit_code": 1, "ok": False}
        saved = self._commit(
            run,
            expected_row_version=v0,
            mutation_type="process_result",
            event_type="process_snapshot",
        )
        self.assertEqual(saved["status"], "running")
        self.assertEqual(saved["process_result"]["exit_code"], 1)

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
                self._commit(
                    run,
                    expected_row_version=int(run.get("row_version") or 1),
                    mutation_type="status_transition",
                    event_type="illegal",
                )
            self.assertEqual(self.store.get_run(rid)["status"], terminal)

    def test_duplicate_completion_second_stale(self):
        run = self.store.get_run("run-mut-1")
        v0 = int(run.get("row_version") or 1)
        first = transition_run(dict(run), "needs_review", "system", "a")
        self._commit(
            first,
            expected_row_version=v0,
            mutation_type="supervised_finished",
            event_type="supervised_finished",
        )
        second = transition_run(dict(run), "needs_review", "system", "b")
        with self.assertRaises(ValueError):
            self._commit(
                second,
                expected_row_version=v0,
                mutation_type="supervised_finished",
                event_type="supervised_finished",
            )

    def test_immediate_edge_enforced_no_silent_multihop(self):
        run = self.store.save_run_for_setup(
            {
                "id": "run-multi",
                "project_id": "p",
                "provider_id": "codex",
                "repository": "o/r",
                "status": "approved",
                "execution_mode": "live_supervised",
            }
        )
        run = self.store.get_run("run-multi")
        run["status"] = "running"  # skip queued/starting
        with self.assertRaisesRegex(ValueError, "immediate transition|multi-hop"):
            self._commit(
                run,
                expected_row_version=int(run.get("row_version") or 1),
                mutation_type="process_started",
                event_type="supervised_started",
            )
        self.assertEqual(self.store.get_run("run-multi")["status"], "approved")
        self.assertEqual(len(self.store.list_run_events("run-multi")), 0)

    def test_explicit_path_succeeds_with_edge_events(self):
        run = self.store.save_run_for_setup(
            {
                "id": "run-path",
                "project_id": "p",
                "provider_id": "codex",
                "repository": "o/r",
                "status": "approved",
                "execution_mode": "live_supervised",
            }
        )
        run = self.store.get_run("run-path")
        v0 = int(run.get("row_version") or 1)
        path = ["approved", "queued", "starting", "running"]
        for nxt in path[1:]:
            run = transition_run(run, nxt, "system", nxt)
        run["worktree_path"] = "/wt"
        saved = self._commit(
            run,
            expected_row_version=v0,
            mutation_type="process_started",
            event_type="supervised_started",
            transition_path=path,
        )
        self.assertEqual(saved["status"], "running")
        events = self.store.list_run_events("run-path")
        # 2 intermediate status_transition + 1 final supervised_started
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["event_type"], "status_transition")
        self.assertEqual(events[1]["event_type"], "status_transition")
        self.assertEqual(events[2]["event_type"], "supervised_started")
        self.assertEqual(events[0]["metadata"]["previous_status"], "approved")
        self.assertEqual(events[0]["metadata"]["resulting_status"], "queued")
        self.assertEqual(events[2]["metadata"]["resulting_status"], "running")

    def test_invalid_middle_edge_rolls_back(self):
        run = self.store.save_run_for_setup(
            {
                "id": "run-badpath",
                "project_id": "p",
                "provider_id": "codex",
                "repository": "o/r",
                "status": "approved",
                "execution_mode": "live_supervised",
            }
        )
        run = self.store.get_run("run-badpath")
        v0 = int(run.get("row_version") or 1)
        run["status"] = "needs_review"
        with self.assertRaisesRegex(ValueError, "invalid transition edge"):
            self._commit(
                run,
                expected_row_version=v0,
                mutation_type="process_started",
                event_type="supervised_started",
                transition_path=["approved", "running", "needs_review"],  # illegal edge approved→running
            )
        self.assertEqual(self.store.get_run("run-badpath")["status"], "approved")
        self.assertEqual(int(self.store.get_run("run-badpath")["row_version"]), v0)
        self.assertEqual(self.store.list_run_events("run-badpath"), [])

    def test_authority_fields_rejected_same_state(self):
        fields = {
            "provider_id": "claude",
            "repository": "other/repo",
            "baseline_commit": "a" * 40,
            "execution_branch": "feature/evil",
            "packet_id": "evil",
            "requested_capabilities": ["merge"],
            "risk": "BLACK",
            "execution_mode": "dry_run",
            "scope_fingerprint": "evil-scope",
            "constitution_hash": "d" * 64,
            "constitution_lease_id": "evil-lease",
            "constitution_lease_fingerprint": "evil-lf",
            "task_lock_id": "evil-lock",
        }
        for field, value in fields.items():
            run = self.store.get_run("run-mut-1")
            v0 = int(run.get("row_version") or 1)
            status = run["status"]
            run[field] = value
            with self.assertRaisesRegex(ValueError, "authority field|unauthorized"):
                self._commit(
                    run,
                    expected_row_version=v0,
                    mutation_type="process_result",
                    event_type="snap",
                )
            after = self.store.get_run("run-mut-1")
            self.assertEqual(after["status"], status)
            self.assertEqual(int(after["row_version"]), v0)
            self.assertEqual(self.store.list_run_events("run-mut-1"), [])

    def test_unknown_mutation_type_rejected(self):
        run = self.store.get_run("run-mut-1")
        run["process_result"] = {"ok": True}
        with self.assertRaisesRegex(ValueError, "unknown mutation_type"):
            self._commit(
                run,
                expected_row_version=int(run.get("row_version") or 1),
                mutation_type="not_a_real_type",
                event_type="x",
            )

    def test_unauthorized_extra_field_rejected(self):
        run = self.store.get_run("run-mut-1")
        v0 = int(run.get("row_version") or 1)
        run["process_result"] = {"ok": True}
        run["sneaky_field"] = "nope"
        with self.assertRaisesRegex(ValueError, "unauthorized field"):
            self._commit(
                run,
                expected_row_version=v0,
                mutation_type="process_result",
                event_type="snap",
            )
        self.assertEqual(int(self.store.get_run("run-mut-1")["row_version"]), v0)

    def test_caller_cannot_expand_allowlist(self):
        # Prove storage-owned: process_result cannot change verification
        run = self.store.get_run("run-mut-1")
        run["verification"] = {"passed": True}
        with self.assertRaisesRegex(ValueError, "unauthorized field"):
            self._commit(
                run,
                expected_row_version=int(run.get("row_version") or 1),
                mutation_type="process_result",
                event_type="snap",
            )

    def test_runtime_service_has_no_unrestricted_save_run(self):
        import inspect
        import buildforme.execution_service as es

        src = inspect.getsource(es)
        self.assertNotIn("store.save_run(", src)
        self.assertNotIn("store.s6.save_run(", src)
        self.assertNotIn("save_run_for_setup", src)
        self.assertNotIn("allow_unversioned=True", src)

    def test_runtime_modules_no_setup_write_apis(self):
        modules = [
            "buildforme/execution_service.py",
            "buildforme/server.py",
            "buildforme/review_gate.py",
            "buildforme/process_supervisor.py",
        ]
        for rel in modules:
            text = Path(rel).read_text(encoding="utf-8")
            self.assertNotIn(
                "save_run_for_setup",
                text,
                msg=f"{rel} must not call save_run_for_setup",
            )
            self.assertNotIn("allow_unversioned=True", text, msg=f"{rel} allow_unversioned")

    def test_protected_fields_defined(self):
        for f in (
            "provider_id",
            "baseline_commit",
            "execution_branch",
            "constitution_hash",
            "scope_fingerprint",
        ):
            self.assertIn(f, PROTECTED_AUTHORITY_FIELDS)
        self.assertIn("process_result", MUTATION_METADATA_ALLOWLISTS)
        self.assertTrue(can_transition("running", "needs_review"))


if __name__ == "__main__":
    unittest.main()

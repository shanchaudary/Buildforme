"""Packet 5A hardening: immutable scope and storage-owned lifecycle truth."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from buildforme.storage import LocalStore


class RunMutationAuthorityHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")

    def _make_run(self, run_id: str, status: str = "running", **extra):
        payload = {
            "id": run_id,
            "project_id": "p",
            "task_id": "t",
            "packet_id": "pk",
            "provider_id": "codex",
            "repository": "owner/repo",
            "repository_local_path": "/repo",
            "baseline_ref": "HEAD",
            "baseline_commit": "b" * 40,
            "requested_target_branch": "feature/x",
            "target_branch": "feature/x-run",
            "execution_branch": "feature/x-run",
            "operating_mode": "IMPLEMENTATION",
            "risk": "YELLOW",
            "execution_mode": "live_supervised",
            "mode": "live_supervised",
            "transport": "cli",
            "requested_capabilities": ["read_repository"],
            "scope_fingerprint": "scope-1",
            "constitution_version": "1.0.0",
            "constitution_hash": "c" * 64,
            "constitution_lease_id": "lease-1",
            "constitution_lease_fingerprint": "lease-fp",
            "task_lock_id": "lock-1",
            "status": status,
            "status_history": [],
            "started_at": "2026-01-01T00:00:00+00:00" if status == "running" else None,
            "finished_at": None,
        }
        payload.update(extra)
        return self.store.save_run_for_setup(payload)

    def _commit(self, run, *, mutation_type, event_type="test", **kwargs):
        return self.store.commit_run_mutation(
            run,
            expected_row_version=int(run["row_version"]),
            mutation_type=mutation_type,
            event_type=event_type,
            event_summary=kwargs.pop("event_summary", "test mutation"),
            event_actor=kwargs.pop("event_actor", "system"),
            **kwargs,
        )

    def test_preflight_cannot_rewrite_bound_scope_same_state(self):
        self._make_run("run-scope-same", status="awaiting_preflight")
        run = self.store.get_run("run-scope-same")
        version = run["row_version"]
        run["preflight"] = {"passed": True}
        run["approval_requirements"] = ["shan_task_approval"]
        run["scope_fingerprint"] = "evil-scope"
        with self.assertRaisesRegex(
            ValueError, "authority field mutation forbidden: scope_fingerprint"
        ):
            self._commit(
                run,
                mutation_type="preflight_result",
                event_type="preflight_passed",
            )
        stored = self.store.get_run("run-scope-same")
        self.assertEqual(stored["scope_fingerprint"], "scope-1")
        self.assertEqual(stored["row_version"], version)
        self.assertEqual(self.store.list_run_events("run-scope-same"), [])

    def test_preflight_cannot_rewrite_bound_scope_during_transition(self):
        self._make_run("run-scope-edge", status="awaiting_preflight")
        run = self.store.get_run("run-scope-edge")
        version = run["row_version"]
        run["status"] = "awaiting_approval"
        run["preflight"] = {"passed": True}
        run["approval_requirements"] = ["shan_task_approval"]
        run["scope_fingerprint"] = "evil-scope"
        with self.assertRaisesRegex(
            ValueError, "authority field mutation forbidden: scope_fingerprint"
        ):
            self._commit(
                run,
                mutation_type="preflight_result",
                event_type="preflight_passed",
            )
        stored = self.store.get_run("run-scope-edge")
        self.assertEqual(stored["status"], "awaiting_preflight")
        self.assertEqual(stored["row_version"], version)
        self.assertEqual(self.store.list_run_events("run-scope-edge"), [])

    def test_missing_legacy_scope_is_not_runtime_backfilled(self):
        self._make_run(
            "run-no-scope",
            status="awaiting_preflight",
            scope_fingerprint=None,
        )
        run = self.store.get_run("run-no-scope")
        run["scope_fingerprint"] = "new-runtime-scope"
        run["preflight"] = {"passed": True}
        with self.assertRaisesRegex(
            ValueError, "authority field mutation forbidden: scope_fingerprint"
        ):
            self._commit(run, mutation_type="preflight_result")
        self.assertIsNone(
            self.store.get_run("run-no-scope").get("scope_fingerprint")
        )

    def test_same_state_metadata_cannot_fabricate_lifecycle_truth(self):
        self._make_run(
            "run-history",
            status="running",
            status_history=[
                {
                    "from": "starting",
                    "to": "running",
                    "actor": "system",
                    "reason": "real",
                    "at": "2026-01-01T00:00:00+00:00",
                }
            ],
            started_at="2026-01-01T00:00:00+00:00",
        )
        run = self.store.get_run("run-history")
        original_history = list(run["status_history"])
        original_started = run["started_at"]
        run["status_history"] = [
            {"from": "draft", "to": "completed", "actor": "attacker"}
        ]
        run["started_at"] = "2099-01-01T00:00:00+00:00"
        run["finished_at"] = "2099-01-01T00:00:00+00:00"
        run["process_result"] = {"ok": True, "exit_code": 0}
        saved = self._commit(
            run,
            mutation_type="process_result",
            event_type="process_snapshot",
        )
        self.assertEqual(saved["status_history"], original_history)
        self.assertEqual(saved["started_at"], original_started)
        self.assertIsNone(saved["finished_at"])
        self.assertTrue(saved["process_result"]["ok"])

    def test_explicit_path_derives_history_and_matching_events(self):
        self._make_run("run-path-history", status="approved", started_at=None)
        run = self.store.get_run("run-path-history")
        run["status"] = "running"
        run["worktree_path"] = "/tmp/wt"
        saved = self._commit(
            run,
            mutation_type="process_started",
            event_type="supervised_started",
            event_summary="provider launched",
            event_actor="system",
            transition_path=["approved", "queued", "starting", "running"],
        )
        history = saved["status_history"]
        events = self.store.list_run_events("run-path-history")
        expected_edges = [
            ("approved", "queued"),
            ("queued", "starting"),
            ("starting", "running"),
        ]
        self.assertEqual(
            [(entry["from"], entry["to"]) for entry in history],
            expected_edges,
        )
        self.assertEqual(
            [
                (
                    event["metadata"]["previous_status"],
                    event["metadata"]["resulting_status"],
                )
                for event in events
            ],
            expected_edges,
        )
        self.assertTrue(saved["started_at"])
        self.assertIsNone(saved["finished_at"])
        for history_entry, event in zip(history, events, strict=True):
            self.assertEqual(history_entry["actor"], event["actor"])
            self.assertEqual(history_entry["at"], event["created_at"])
            self.assertEqual(history_entry["reason"], event["summary"])
            self.assertEqual(
                event["metadata"]["timestamp"],
                event["created_at"],
            )

    def test_terminal_path_derives_finished_at(self):
        self._make_run(
            "run-terminal-path",
            status="running",
            execution_mode="dry_run",
            mode="dry_run",
            transport="dry_run",
        )
        run = self.store.get_run("run-terminal-path")
        run["status"] = "completed"
        run["dry_run_result"] = {"ok": True}
        saved = self._commit(
            run,
            mutation_type="dry_run_finished",
            event_type="dry_run_completed",
            transition_path=["running", "needs_review", "completed"],
        )
        self.assertEqual(saved["status"], "completed")
        self.assertTrue(saved["finished_at"])
        self.assertEqual(len(saved["status_history"]), 2)

    def test_invalid_path_rolls_back_everything(self):
        self._make_run("run-rollback", status="approved", started_at=None)
        before = self.store.get_run("run-rollback")
        proposed = dict(before)
        proposed["status"] = "needs_review"
        proposed["worktree_path"] = "/tmp/wt"
        with self.assertRaisesRegex(ValueError, "invalid transition edge"):
            self._commit(
                proposed,
                mutation_type="process_started",
                transition_path=["approved", "running", "needs_review"],
            )
        after = self.store.get_run("run-rollback")
        for key in (
            "status",
            "status_history",
            "started_at",
            "finished_at",
            "row_version",
        ):
            self.assertEqual(after[key], before[key])
        self.assertEqual(self.store.list_run_events("run-rollback"), [])

    def test_same_state_transition_path_is_rejected(self):
        self._make_run("run-same-path", status="running")
        run = self.store.get_run("run-same-path")
        run["process_result"] = {"ok": True}
        with self.assertRaisesRegex(ValueError, "same-state"):
            self._commit(
                run,
                mutation_type="process_result",
                transition_path=["running", "needs_review", "running"],
            )

    def test_positive_verification_evidence_and_review_mutations(self):
        self._make_run("run-positive", status="running")
        run = self.store.get_run("run-positive")
        run["verification"] = {"passed": True}
        run = self._commit(
            run,
            mutation_type="verification_result",
            event_type="verified",
        )
        self.assertTrue(run["verification"]["passed"])

        run["evidence"] = {"evidence_id": "ev-1"}
        run["evidence_ids"] = ["ev-1"]
        run["final_head_sha"] = "f" * 40
        run["head_commit"] = "f" * 40
        run = self._commit(
            run,
            mutation_type="execution_evidence_link",
            event_type="evidence_linked",
        )
        self.assertEqual(run["evidence"]["evidence_id"], "ev-1")

        run["review"] = {"status": "review_required"}
        run["result_summary"] = "ready"
        run = self._commit(
            run,
            mutation_type="review_package",
            event_type="review_ready",
        )
        self.assertEqual(run["review"]["status"], "review_required")

    def test_storage_status_policy_cannot_be_broadened_by_caller(self):
        self._make_run("run-policy", status="draft", started_at=None)
        run = self.store.get_run("run-policy")
        run["worktree_path"] = "/tmp/forbidden"
        with self.assertRaisesRegex(ValueError, "not permitted from status"):
            self._commit(
                run,
                mutation_type="process_started",
                require_db_status_in={"draft"},
            )
        self.assertIsNone(
            self.store.get_run("run-policy").get("worktree_path")
        )

    def test_all_runtime_modules_forbid_setup_and_unrestricted_save_apis(self):
        modules = [
            "buildforme/execution_service.py",
            "buildforme/server.py",
            "buildforme/review_gate.py",
            "buildforme/process_supervisor.py",
        ]
        for relative in modules:
            source = Path(relative).read_text(encoding="utf-8")
            self.assertNotIn(
                "save_run_for_setup",
                source,
                msg=f"{relative} calls fixture/setup run persistence",
            )
            self.assertNotIn(
                "allow_unversioned=True",
                source,
                msg=f"{relative} enables unversioned run persistence",
            )
            tree = ast.parse(source, filename=relative)
            unrestricted = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "save_run"
            ]
            self.assertEqual(
                unrestricted,
                [],
                msg=f"{relative} contains unrestricted save_run call(s)",
            )


    def test_metadata_mutation_types_cannot_claim_completion_edges(self):
        cases = {
            "process_result": ("process_result", {"ok": True}),
            "verification_result": ("verification", {"passed": True}),
            "execution_evidence_link": ("evidence", {"evidence_id": "ev"}),
            "review_package": ("review", {"status": "review_required"}),
        }
        for index, (mutation_type, (field, value)) in enumerate(cases.items()):
            run_id = f"run-edge-policy-{index}"
            self._make_run(run_id, status="running")
            run = self.store.get_run(run_id)
            version = run["row_version"]
            run[field] = value
            run["status"] = "completed"
            with self.assertRaisesRegex(ValueError, "does not authorize transition edge"):
                self._commit(run, mutation_type=mutation_type)
            stored = self.store.get_run(run_id)
            self.assertEqual(stored["status"], "running")
            self.assertEqual(stored["row_version"], version)
            self.assertEqual(self.store.list_run_events(run_id), [])

    def test_generic_status_transition_cannot_bypass_review(self):
        self._make_run("run-generic-edge", status="running")
        run = self.store.get_run("run-generic-edge")
        version = run["row_version"]
        run["status"] = "completed"
        with self.assertRaisesRegex(ValueError, "does not authorize transition edge"):
            self._commit(run, mutation_type="status_transition")
        stored = self.store.get_run("run-generic-edge")
        self.assertEqual(stored["status"], "running")
        self.assertEqual(stored["row_version"], version)
        self.assertEqual(self.store.list_run_events("run-generic-edge"), [])

    def test_transition_only_mutation_cannot_be_used_same_state(self):
        self._make_run("run-transition-same", status="draft", started_at=None)
        run = self.store.get_run("run-transition-same")
        with self.assertRaisesRegex(ValueError, "requires an authorized status transition"):
            self._commit(run, mutation_type="status_transition")

    def test_failure_and_cancel_edges_remain_authorized(self):
        self._make_run("run-failure-edge", status="running")
        failed = self.store.get_run("run-failure-edge")
        failed["status"] = "failed"
        failed["process_result"] = {"ok": False, "error": "provider failed"}
        failed = self._commit(
            failed,
            mutation_type="failure_detail",
            event_type="supervised_failed",
        )
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["finished_at"])

        self._make_run("run-cancel-edge", status="running")
        cancelled = self.store.get_run("run-cancel-edge")
        cancelled["status"] = "cancelled"
        cancelled["process_result"] = {"cancelled": True}
        cancelled = self._commit(
            cancelled,
            mutation_type="cancel",
            event_type="supervised_cancelled",
            transition_path=["running", "cancel_requested", "cancelled"],
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(len(cancelled["status_history"]), 2)


    def test_live_run_cannot_complete_through_dry_run_mutation(self):
        self._make_run("run-live-dry-bypass", status="running")
        run = self.store.get_run("run-live-dry-bypass")
        version = run["row_version"]
        run["status"] = "completed"
        run["dry_run_result"] = {"ok": True}
        with self.assertRaisesRegex(ValueError, "not permitted for execution_mode"):
            self._commit(
                run,
                mutation_type="dry_run_finished",
                transition_path=["running", "needs_review", "completed"],
            )
        stored = self.store.get_run("run-live-dry-bypass")
        self.assertEqual(stored["status"], "running")
        self.assertEqual(stored["row_version"], version)
        self.assertEqual(self.store.list_run_events("run-live-dry-bypass"), [])

    def test_dry_run_cannot_use_supervised_completion_mutation(self):
        self._make_run(
            "run-dry-live-bypass",
            status="running",
            execution_mode="dry_run",
            mode="dry_run",
            transport="dry_run",
        )
        run = self.store.get_run("run-dry-live-bypass")
        version = run["row_version"]
        run["status"] = "needs_review"
        run["review"] = {"status": "review_required"}
        with self.assertRaisesRegex(ValueError, "not permitted for execution_mode"):
            self._commit(run, mutation_type="supervised_finished")
        stored = self.store.get_run("run-dry-live-bypass")
        self.assertEqual(stored["status"], "running")
        self.assertEqual(stored["row_version"], version)
        self.assertEqual(self.store.list_run_events("run-dry-live-bypass"), [])

    def test_same_state_preflight_cannot_rewrite_approval_requirements(self):
        self._make_run(
            "run-approved-requirements",
            status="approved",
            approval_requirements=["shan_task_approval"],
        )
        run = self.store.get_run("run-approved-requirements")
        version = run["row_version"]
        run["approval_requirements"] = []
        run["preflight"] = {"passed": True, "required_approvals": []}
        with self.assertRaisesRegex(ValueError, "authorized preflight state edge"):
            self._commit(run, mutation_type="preflight_result")
        stored = self.store.get_run("run-approved-requirements")
        self.assertEqual(stored["approval_requirements"], ["shan_task_approval"])
        self.assertEqual(stored["row_version"], version)
        self.assertEqual(self.store.list_run_events("run-approved-requirements"), [])

    def test_initial_preflight_edge_may_set_approval_requirements(self):
        self._make_run(
            "run-initial-requirements",
            status="awaiting_preflight",
            approval_requirements=[],
        )
        run = self.store.get_run("run-initial-requirements")
        run["status"] = "awaiting_approval"
        run["approval_requirements"] = ["shan_task_approval"]
        run["preflight"] = {
            "passed": True,
            "required_approvals": ["shan_task_approval"],
        }
        saved = self._commit(
            run,
            mutation_type="preflight_result",
            event_type="preflight_passed",
        )
        self.assertEqual(saved["status"], "awaiting_approval")
        self.assertEqual(saved["approval_requirements"], ["shan_task_approval"])


if __name__ == "__main__":
    unittest.main()

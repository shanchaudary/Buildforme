"""Packet 5A hardening: immutable scope and storage-owned lifecycle truth."""

from __future__ import annotations

import re
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
        with self.assertRaisesRegex(ValueError, "authority field mutation forbidden: scope_fingerprint"):
            self._commit(run, mutation_type="preflight_result", event_type="preflight_passed")
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
        with self.assertRaisesRegex(ValueError, "authority field mutation forbidden: scope_fingerprint"):
            self._commit(run, mutation_type="preflight_result", event_type="preflight_passed")
        stored = self.store.get_run("run-scope-edge")
        self.assertEqual(stored["status"], "awaiting_preflight")
        self.assertEqual(stored["row_version"], version)
        self.assertEqual(self.store.list_run_events("run-scope-edge"), [])

    def test_missing_legacy_scope_is_not_runtime_backfilled(self):
        self._make_run("run-no-scope", status="awaiting_preflight", scope_fingerprint=None)
        run = self.store.get_run("run-no-scope")
        run["scope_fingerprint"] = "new-runtime-scope"
        run["preflight"] = {"passed": True}
        with self.assertRaisesRegex(ValueError, "authority field mutation forbidden: scope_fingerprint"):
            self._commit(run, mutation_type="preflight_result")
        self.assertIsNone(self.store.get_run("run-no-scope").get("scope_fingerprint"))

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
        run["status_history"] = [{"from": "draft", "to": "completed", "actor": "attacker"}]
        run["started_at"] = "2099-01-01T00:00:00+00:00"
        run["finished_at"] = "2099-01-01T00:00:00+00:00"
        run["process_result"] = {"ok": True, "exit_code": 0}
        saved = self._commit(run, mutation_type="process_result", event_type="process_snapshot")
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
        self.assertEqual([(h["from"], h["to"]) for h in history], [
            ("approved", "queued"),
            ("queued", "starting"),
            ("starting", "running"),
        ])
        self.assertEqual(
            [(e["metadata"]["previous_status"], e["metadata"]["resulting_status"]) for e in events],
            [(h["from"], h["to"]) for h in history],
        )
        self.assertTrue(saved["started_at"])
        self.assertIsNone(saved["finished_at"])
        for history_entry, event in zip(history, events, strict=True):
            self.assertEqual(history_entry["actor"], event["actor"])
            self.assertEqual(history_entry["at"], event["created_at"])
            self.assertEqual(history_entry["reason"], event["summary"])
            self.assertEqual(event["metadata"]["timestamp"], event["created_at"])

    def test_terminal_path_derives_finished_at(self):
        self._make_run("run-terminal-path", status="running")
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

    def test_invalid_path_rolls_back_history_timestamps_version_and_events(self):
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
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["status_history"], before["status_history"])
        self.assertEqual(after["started_at"], before["started_at"])
        self.assertEqual(after["finished_at"], before["finished_at"])
        self.assertEqual(after["row_version"], before["row_version"])
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
        run = self._commit(run, mutation_type="verification_result", event_type="verified")
        self.assertTrue(run["verification"]["passed"])

        run["evidence"] = {"evidence_id": "ev-1"}
        run["evidence_ids"] = ["ev-1"]
        run["final_head_sha"] = "f" * 40
        run["head_commit"] = "f" * 40
        run = self._commit(run, mutation_type="execution_evidence_link", event_type="evidence_linked")
        self.assertEqual(run["evidence"]["evidence_id"], "ev-1")

        run["review"] = {"status": "review_required"}
        run["result_summary"] = "ready"
        run = self._commit(run, mutation_type="review_package", event_type="review_ready")
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
        self.assertIsNone(self.store.get_run("run-policy").get("worktree_path"))

    def test_all_runtime_modules_forbid_setup_and_unrestricted_save_apis(self):
        modules = [
            "buildforme/execution_service.py",
            "buildforme/server.py",
            "buildforme/review_gate.py",
            "buildforme/process_supervisor.py",
        ]
        forbidden = [
            re.compile(r"save_run_for_setup"),
            re.compile(r"allow_unversioned\s*=\s*True"),
            re.compile(r"(?:store|self\._store\(\)|self\.s6|store\.s6)\.save_run\s*\("),
        ]
        for relative in modules:
            source = Path(relative).read_text(encoding="utf-8")
            for pattern in forbidden:
                self.assertIsNone(
                    pattern.search(source),
                    msg=f"{relative} contains forbidden runtime write API: {pattern.pattern}",
                )


if __name__ == "__main__":
    unittest.main()

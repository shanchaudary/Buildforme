"""Founder-decision evidence: complete fingerprint, atomic commit, fail-closed."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from buildforme.evidence import (
    EVIDENCE_KIND_FOUNDER_DECISION,
    build_founder_decision_evidence,
    compute_founder_decision_fingerprint,
)
from buildforme.execution_service import (
    create_run,
    execute_supervised,
    founder_review_decision,
    record_run_approval,
    run_preflight,
)
from buildforme.storage import LocalStore
from governance.constitution_engine import get_engine


def _parent_execution(**overrides):
    base = {
        "schema": "buildforme.evidence.v1",
        "evidence_kind": "execution",
        "evidence_id": "ev-parent-001",
        "run_id": "run-fd-1",
        "project_id": "buildforme",
        "packet_id": "pkt-1",
        "task_id": "task-1",
        "repository": "owner/repo",
        "evidence_fingerprint": "fp" * 32,
        "process": {"exit_code": 0, "cleanup_ok": True},
        "files_changed": ["src/a.py"],
    }
    base.update(overrides)
    return base


def _run(**overrides):
    base = {
        "id": "run-fd-1",
        "project_id": "buildforme",
        "packet_id": "pkt-1",
        "task_id": "task-1",
        "repository": "owner/repo",
        "status": "needs_review",
        "row_version": 3,
        "scope_fingerprint": "scope-abc",
        "constitution_hash": "c" * 64,
        "constitution_version": "1.0.0",
        "constitution_lease_id": "lease-1",
        "constitution_lease_fingerprint": "lf" * 16,
        "execution_mode": "live_supervised",
        "provider_id": "codex",
        "baseline_commit": "b" * 40,
        "worktree_path": "/tmp/wt",
    }
    base.update(overrides)
    return base


def _decision_bundle(**overrides):
    kwargs = dict(
        run=_run(),
        parent_evidence=_parent_execution(),
        decision="accept_for_pr_prep",
        actor="shan",
        note="ship it",
        review={
            "status": "accepted_for_pr_prep",
            "founder_decision": "accept_for_pr_prep",
            "founder_actor": "shan",
            "hard_blocks_at_decision": [],
            "accept_for_pr_prep_allowed": True,
        },
        hard_blocks=[],
        previous_status="needs_review",
        resulting_status="completed",
        previous_row_version=3,
        decision_timestamp="2026-07-10T12:00:00Z",
        cleanup_requested=False,
        verification={"passed": True, "blocking_reasons": []},
    )
    kwargs.update(overrides)
    return build_founder_decision_evidence(**kwargs)


class FounderDecisionFingerprintTests(unittest.TestCase):
    def test_recalculate_matches(self):
        b = _decision_bundle()
        self.assertEqual(b["evidence_kind"], EVIDENCE_KIND_FOUNDER_DECISION)
        self.assertEqual(
            b["evidence_fingerprint"],
            compute_founder_decision_fingerprint(b),
        )

    def test_decision_changes_fp(self):
        a = _decision_bundle(decision="accept_for_pr_prep", resulting_status="completed")
        b = _decision_bundle(
            decision="reject",
            resulting_status="rejected",
            review={"status": "rejected", "founder_decision": "reject"},
        )
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_actor_changes_fp(self):
        a = _decision_bundle(actor="shan")
        b = _decision_bundle(actor="other")
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_note_changes_fp(self):
        a = _decision_bundle(note="a")
        b = _decision_bundle(note="b")
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_review_status_changes_fp(self):
        a = _decision_bundle(review={"status": "accepted_for_pr_prep"})
        b = _decision_bundle(review={"status": "rejected"})
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_hard_blocks_change_fp(self):
        a = _decision_bundle(hard_blocks=[])
        b = _decision_bundle(hard_blocks=["forbidden_path: .env"])
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_resulting_status_changes_fp(self):
        a = _decision_bundle(resulting_status="completed")
        b = _decision_bundle(resulting_status="rejected")
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_parent_id_changes_fp(self):
        a = _decision_bundle(parent_evidence=_parent_execution(evidence_id="ev-a"))
        b = _decision_bundle(parent_evidence=_parent_execution(evidence_id="ev-b"))
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_parent_fp_changes_fp(self):
        a = _decision_bundle(
            parent_evidence=_parent_execution(evidence_fingerprint="a" * 64)
        )
        b = _decision_bundle(
            parent_evidence=_parent_execution(evidence_fingerprint="b" * 64)
        )
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_constitution_hash_changes_fp(self):
        a = _decision_bundle(run=_run(constitution_hash="a" * 64))
        b = _decision_bundle(run=_run(constitution_hash="b" * 64))
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_lease_id_changes_fp(self):
        a = _decision_bundle(run=_run(constitution_lease_id="L1"))
        b = _decision_bundle(run=_run(constitution_lease_id="L2"))
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_scope_fingerprint_changes_fp(self):
        a = _decision_bundle(run=_run(scope_fingerprint="s1"))
        b = _decision_bundle(run=_run(scope_fingerprint="s2"))
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_decision_timestamp_changes_fp(self):
        a = _decision_bundle(decision_timestamp="2026-07-10T12:00:00Z")
        b = _decision_bundle(decision_timestamp="2026-07-10T12:00:01Z")
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])


class FounderDecisionTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")
        # Shell run
        self.run = {
            "id": "run-fd-1",
            "project_id": "buildforme",
            "packet_id": "pkt-1",
            "task_id": "task-1",
            "provider_id": "codex",
            "repository": "owner/repo",
            "status": "needs_review",
            "execution_mode": "live_supervised",
            "row_version": 1,
            "scope_fingerprint": "scope-1",
            "constitution_hash": "c" * 64,
            "constitution_lease_id": "lease-1",
            "constitution_lease_fingerprint": "lf1",
            "baseline_commit": "b" * 40,
            "worktree_path": str(Path(self.temp.name) / "wt"),
        }
        self.store.save_run(self.run)
        # Parent execution evidence (minimal that storage accepts as execution)
        from buildforme.evidence import build_evidence_bundle

        parent = build_evidence_bundle(
            run=self.run,
            packet={"id": "pkt-1", "allowed_files": ["src/**"], "forbidden_files": []},
            process_result={
                "exit_code": 0,
                "pid": 1,
                "timed_out": False,
                "cancelled": False,
                "cleanup_ok": True,
                "stdout": "ok",
                "stderr": "",
                "argv": ["x"],
            },
            worktree={
                "worktree_path": self.run["worktree_path"],
                "head_commit": "h" * 40,
                "branch": "feature/x",
                "baseline_commit": "b" * 40,
            },
            diff={
                "manifest": {
                    "files_changed": ["src/a.py"],
                    "files": [{"path": "src/a.py"}],
                    "manifest_fingerprint": "m" * 64,
                    "complete": True,
                },
                "files_changed": ["src/a.py"],
            },
            provider_health={"version": "1", "executable": "x"},
            verification={
                "passed": True,
                "blocking_reasons": [],
                "checks": [{"name": "tests", "status": "pass"}],
            },
            constitution_result={"passed": True, "problems": []},
            patch_fingerprint="p" * 64,
            final_head_sha="h" * 40,
            execution_branch="feature/x",
            approved_baseline_sha="b" * 40,
            manifest_fingerprint="m" * 64,
        )
        self.parent = self.store.save_run_evidence(parent)
        # Refresh run row_version after any side effects
        self.run = self.store.get_run("run-fd-1")

    def _accept_run(self):
        run = self.store.get_run("run-fd-1")
        run["status"] = "completed"
        run["review"] = {
            "status": "accepted_for_pr_prep",
            "founder_decision": "accept_for_pr_prep",
            "founder_actor": "shan",
            "hard_blocks_at_decision": [],
        }
        from buildforme.run_state import transition_run

        # Build as if transitioned
        run_after = dict(run)
        run_after["status"] = "completed"
        decision_ev = build_founder_decision_evidence(
            run=run_after,
            parent_evidence=self.parent,
            decision="accept_for_pr_prep",
            actor="shan",
            note="ok",
            review=run_after["review"],
            hard_blocks=[],
            previous_status="needs_review",
            resulting_status="completed",
            previous_row_version=int(run.get("row_version") or 1),
            decision_timestamp="2026-07-10T12:00:00Z",
            verification={"passed": True, "blocking_reasons": []},
        )
        return self.store.commit_founder_decision(
            run=run_after,
            expected_row_version=int(run.get("row_version") or 1),
            decision_evidence=decision_ev,
            event_summary="accept_for_pr_prep: ok",
            event_actor="shan",
        )

    def test_valid_decision_commits_run_event_evidence(self):
        committed = self._accept_run()
        run = self.store.get_run("run-fd-1")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(committed["decision_evidence"]["resulting_status"], "completed")
        events = self.store.list_run_events("run-fd-1")
        self.assertTrue(
            any(e.get("event_type") == "founder_review_decision" for e in events)
        )
        ev = committed["decision_evidence"]
        self.assertEqual(
            compute_founder_decision_fingerprint(ev),
            ev["evidence_fingerprint"],
        )
        self.assertEqual(ev["parent_evidence_id"], self.parent["evidence_id"])

    def test_stale_row_version_rejected(self):
        run = self.store.get_run("run-fd-1")
        run_after = dict(run)
        run_after["status"] = "completed"
        run_after["review"] = {"status": "accepted_for_pr_prep"}
        decision_ev = build_founder_decision_evidence(
            run=run_after,
            parent_evidence=self.parent,
            decision="accept_for_pr_prep",
            actor="shan",
            note="",
            review=run_after["review"],
            hard_blocks=[],
            previous_status="needs_review",
            resulting_status="completed",
            previous_row_version=999,
            decision_timestamp="2026-07-10T12:00:00Z",
            verification={"passed": True, "blocking_reasons": []},
        )
        with self.assertRaisesRegex(ValueError, "stale"):
            self.store.commit_founder_decision(
                run=run_after,
                expected_row_version=999,
                decision_evidence=decision_ev,
            )
        self.assertEqual(self.store.get_run("run-fd-1")["status"], "needs_review")
        events = [
            e
            for e in self.store.list_run_events("run-fd-1")
            if e.get("event_type") == "founder_review_decision"
        ]
        self.assertEqual(events, [])

    def test_wrong_parent_run_rejected(self):
        # Insert foreign parent evidence under another run
        other = dict(self.parent)
        other["evidence_id"] = "ev-foreign"
        other["run_id"] = "run-other"
        other["id"] = "ev-foreign"
        self.store.save_run(
            {
                "id": "run-other",
                "project_id": "p",
                "provider_id": "codex",
                "repository": "owner/repo",
                "status": "needs_review",
                "execution_mode": "live_supervised",
            }
        )
        # Bypass normal path: raw insert via save will validate fingerprint
        # Use parent fingerprint material correctly for foreign run
        from buildforme.evidence import compute_evidence_fingerprint

        other["evidence_fingerprint"] = compute_evidence_fingerprint(other)
        # May fail execution validation - build properly
        from buildforme.evidence import build_evidence_bundle

        foreign_run = {
            "id": "run-other",
            "project_id": "p",
            "provider_id": "codex",
            "repository": "owner/other",
            "status": "needs_review",
            "execution_mode": "live_supervised",
            "constitution_hash": "c" * 64,
            "constitution_lease_id": "L",
            "constitution_lease_fingerprint": "lf",
            "baseline_commit": "b" * 40,
        }
        foreign = build_evidence_bundle(
            run=foreign_run,
            packet={"id": "pk", "allowed_files": [], "forbidden_files": []},
            process_result={
                "exit_code": 0,
                "pid": 1,
                "timed_out": False,
                "cancelled": False,
                "cleanup_ok": True,
                "stdout": "",
                "stderr": "",
                "argv": ["x"],
            },
            worktree={
                "worktree_path": "/t",
                "head_commit": "h" * 40,
                "branch": "f",
                "baseline_commit": "b" * 40,
            },
            diff={
                "manifest": {
                    "files_changed": [],
                    "files": [],
                    "manifest_fingerprint": "m" * 64,
                    "complete": True,
                },
                "files_changed": [],
            },
            provider_health={"version": "1"},
            verification={"passed": True, "blocking_reasons": [], "checks": []},
            constitution_result={"passed": True},
            patch_fingerprint="p" * 64,
            final_head_sha="h" * 40,
            execution_branch="f",
            approved_baseline_sha="b" * 40,
            manifest_fingerprint="m" * 64,
        )
        foreign = self.store.save_run_evidence(foreign)

        run = self.store.get_run("run-fd-1")
        run_after = dict(run)
        run_after["status"] = "completed"
        decision_ev = build_founder_decision_evidence(
            run=run_after,
            parent_evidence=foreign,
            decision="accept_for_pr_prep",
            actor="shan",
            note="",
            review={"status": "accepted_for_pr_prep"},
            hard_blocks=[],
            previous_status="needs_review",
            resulting_status="completed",
            previous_row_version=int(run.get("row_version") or 1),
            decision_timestamp="2026-07-10T12:00:00Z",
            verification={"passed": True, "blocking_reasons": []},
        )
        with self.assertRaisesRegex(ValueError, "belongs to run"):
            self.store.commit_founder_decision(
                run=run_after,
                expected_row_version=int(run.get("row_version") or 1),
                decision_evidence=decision_ev,
            )
        self.assertEqual(self.store.get_run("run-fd-1")["status"], "needs_review")

    def test_missing_parent_rejected(self):
        run = self.store.get_run("run-fd-1")
        run_after = dict(run)
        run_after["status"] = "completed"
        fake_parent = dict(self.parent)
        fake_parent["evidence_id"] = "ev-missing-xxx"
        decision_ev = build_founder_decision_evidence(
            run=run_after,
            parent_evidence=fake_parent,
            decision="accept_for_pr_prep",
            actor="shan",
            note="",
            review={"status": "accepted_for_pr_prep"},
            hard_blocks=[],
            previous_status="needs_review",
            resulting_status="completed",
            previous_row_version=int(run.get("row_version") or 1),
            decision_timestamp="2026-07-10T12:00:00Z",
            verification={"passed": True, "blocking_reasons": []},
        )
        with self.assertRaisesRegex(ValueError, "not found"):
            self.store.commit_founder_decision(
                run=run_after,
                expected_row_version=int(run.get("row_version") or 1),
                decision_evidence=decision_ev,
            )
        self.assertEqual(self.store.get_run("run-fd-1")["status"], "needs_review")

    def test_bad_fingerprint_rejected(self):
        run = self.store.get_run("run-fd-1")
        run_after = dict(run)
        run_after["status"] = "completed"
        decision_ev = build_founder_decision_evidence(
            run=run_after,
            parent_evidence=self.parent,
            decision="accept_for_pr_prep",
            actor="shan",
            note="",
            review={"status": "accepted_for_pr_prep"},
            hard_blocks=[],
            previous_status="needs_review",
            resulting_status="completed",
            previous_row_version=int(run.get("row_version") or 1),
            decision_timestamp="2026-07-10T12:00:00Z",
            verification={"passed": True, "blocking_reasons": []},
        )
        decision_ev["evidence_fingerprint"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.store.commit_founder_decision(
                run=run_after,
                expected_row_version=int(run.get("row_version") or 1),
                decision_evidence=decision_ev,
            )
        self.assertEqual(self.store.get_run("run-fd-1")["status"], "needs_review")

    def test_terminal_replay_rejected(self):
        self._accept_run()
        run = self.store.get_run("run-fd-1")
        self.assertEqual(run["status"], "completed")
        # Force status back in memory only for attempt — commit should still see terminal evidence
        run_after = dict(run)
        run_after["status"] = "completed"
        decision_ev = build_founder_decision_evidence(
            run=run_after,
            parent_evidence=self.parent,
            decision="accept_for_pr_prep",
            actor="shan",
            note="again",
            review={"status": "accepted_for_pr_prep"},
            hard_blocks=[],
            previous_status="needs_review",
            resulting_status="completed",
            previous_row_version=int(run.get("row_version") or 1),
            decision_timestamp="2026-07-10T13:00:00Z",
            verification={"passed": True, "blocking_reasons": []},
        )
        with self.assertRaisesRegex(ValueError, "already terminal|stale"):
            self.store.commit_founder_decision(
                run=run_after,
                expected_row_version=int(run.get("row_version") or 1),
                decision_evidence=decision_ev,
            )

    def test_inconsistent_status_rejected(self):
        run = self.store.get_run("run-fd-1")
        run_after = dict(run)
        run_after["status"] = "completed"
        decision_ev = build_founder_decision_evidence(
            run=run_after,
            parent_evidence=self.parent,
            decision="reject",
            actor="shan",
            note="",
            review={"status": "rejected"},
            hard_blocks=[],
            previous_status="needs_review",
            resulting_status="rejected",
            previous_row_version=int(run.get("row_version") or 1),
            decision_timestamp="2026-07-10T12:00:00Z",
            verification={"passed": True, "blocking_reasons": []},
        )
        with self.assertRaisesRegex(ValueError, "inconsistent resulting status"):
            self.store.commit_founder_decision(
                run=run_after,
                expected_row_version=int(run.get("row_version") or 1),
                decision_evidence=decision_ev,
            )


class FounderServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        import os
        import subprocess

        os.environ["BUILDFORME_ALLOW_DIRTY_PARENT"] = "1"
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")
        sample = json.loads(
            (Path(__file__).resolve().parent.parent / "data" / "sample_project.json").read_text(
                encoding="utf-8"
            )
        )
        self.store.load_sample_project(sample, replace=True)
        self.store.set_project_execution_control(
            "buildforme", execution_status="enabled", reason="test"
        )
        project = self.store.get_project("buildforme")
        project["verification_profile"] = {
            "profile_id": "test-fast",
            "test_command": ["python", "-c", "print('ok')"],
            "lint_command": None,
            "build_command": None,
            "forbidden_paths": [".env"],
            "protected_branches": ["main", "master"],
        }
        self.store.upsert_project(project)
        engine = get_engine(force_reload=True)
        for provider in self.store.list_providers():
            refreshed = engine.acknowledge_provider(provider, actor="shan")
            self.store.set_provider_constitution_ack(
                str(provider["provider_id"]),
                {
                    "constitution_supported": True,
                    "constitution_acknowledged": True,
                    "constitution_version": refreshed["constitution_version"],
                    "constitution_hash": refreshed["constitution_hash"],
                    "constitution_last_refresh": refreshed["constitution_last_refresh"],
                    "constitution_acknowledged_at": refreshed["constitution_acknowledged_at"],
                    "constitution_ack_actor": "shan",
                },
            )
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "t@example.com"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "t"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        (self.root / "README.md").write_text("r\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        subprocess.run(["git", "add", "."], cwd=self.root, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "remote",
                "add",
                "origin",
                "https://github.com/shanchaudary/Buildforme.git",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        repo_name = (
            self.store.get_project("buildforme").get("repository")
            or "shanchaudary/Buildforme"
        )
        self.store.register_repository_binding(
            {
                "repository": repo_name,
                "local_path": str(self.root),
                "project_id": "buildforme",
            }
        )
        self.packet = {
            "id": "pkt-fd-int",
            "objective": "proof",
            "acceptance_criteria": ["docs note"],
            "allowed_files": ["docs/**"],
            "forbidden_files": [".env"],
            "risk": "YELLOW",
            "operating_mode": "IMPLEMENTATION",
        }

    def _reach_needs_review(self):
        from buildforme.worktree import remove_worktree

        run = create_run(
            self.store,
            {
                "project_id": "buildforme",
                "provider_id": "codex",
                "packet": self.packet,
                "packet_id": self.packet["id"],
                "target_branch": "feature/fd-int",
                "risk": "YELLOW",
                "execution_mode": "live_supervised",
                "requested_capabilities": [
                    "read_repository",
                    "edit_repository",
                    "run_tests",
                    "produce_patch",
                ],
            },
        )
        pre = run_preflight(self.store, run["id"])
        self.assertTrue(pre["preflight"]["passed"], pre["preflight"].get("blocking_reasons"))
        for req in self.store.get_run(run["id"]).get("approval_requirements") or []:
            record_run_approval(
                self.store, run["id"], requirement_type=req, decision="approved", actor="shan"
            )

        class FakeAdapter:
            def prepare_execution(self, run, packet):
                return {
                    "prepared": True,
                    "problems": [],
                    "health": {"version": "t", "executable": "python"},
                }

            def execute(self, run, packet, *, worktree_path, on_event=None):
                p = Path(worktree_path) / "docs" / "STAGE6_PROOF_NOTE.md"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("# proof\n", encoding="utf-8")
                return {
                    "ok": True,
                    "exit_code": 0,
                    "pid": 1,
                    "stdout": "ok",
                    "stderr": "",
                    "timed_out": False,
                    "cancelled": False,
                    "duration_seconds": 0.1,
                    "argv": ["python", "-c", "print(1)"],
                    "cleanup_ok": True,
                    "process_group_isolated": True,
                    "env_names": ["PATH"],
                    "health": {"version": "t", "executable": "python"},
                }

            def cancel(self, run_id):
                return {"cancelled": True}

        ready = {
            "provider_id": "codex",
            "available": True,
            "live_ready": True,
            "version_ok": True,
            "version": "t",
            "executable": "python",
            "unsupported_reasons": [],
            "constitution_acknowledged": True,
            "auth": {"status": "ready"},
        }
        with patch("buildforme.execution_service.get_adapter", return_value=FakeAdapter()):
            with patch(
                "buildforme.execution_service.health_check_provider", return_value=ready
            ):
                result = execute_supervised(self.store, run["id"])
        self.assertEqual(result["run"]["status"], "needs_review")
        self._wt = result["run"].get("worktree_path")
        self._run_id = run["id"]
        return result

    def tearDown(self):
        from buildforme.worktree import remove_worktree

        wt = getattr(self, "_wt", None)
        if wt:
            try:
                remove_worktree(repo_root=self.root, worktree_path=Path(wt), force=True)
            except Exception:
                pass

    def test_service_accept_atomic_and_consistent(self):
        result = self._reach_needs_review()
        decided = founder_review_decision(
            self.store,
            self._run_id,
            decision="accept_for_pr_prep",
            note="accept",
            actor="shan",
        )
        run = decided["run"]
        self.assertEqual(run["status"], "completed")
        de = decided["decision_evidence"]
        self.assertEqual(de["resulting_status"], "completed")
        self.assertEqual(de["evidence_kind"], EVIDENCE_KIND_FOUNDER_DECISION)
        self.assertEqual(
            compute_founder_decision_fingerprint(de),
            de["evidence_fingerprint"],
        )
        parent = self.store.get_latest_execution_evidence(self._run_id)
        self.assertEqual(de["parent_evidence_id"], parent["evidence_id"])
        events = self.store.list_run_events(self._run_id)
        fd_events = [e for e in events if e.get("event_type") == "founder_review_decision"]
        self.assertEqual(len(fd_events), 1)
        self.assertEqual(fd_events[0].get("actor"), "shan")
        # No swallow path: second accept fails without double evidence
        with self.assertRaises(ValueError):
            founder_review_decision(
                self.store,
                self._run_id,
                decision="accept_for_pr_prep",
                note="again",
                actor="shan",
            )

    def test_hard_block_leaves_no_completed(self):
        result = self._reach_needs_review()
        # Force hard block by clearing verification on run
        run = self.store.get_run(self._run_id)
        run["verification"] = {
            "passed": False,
            "blocking_reasons": ["tests failed"],
            "checks": [{"name": "tests", "status": "fail", "detail": "boom"}],
        }
        self.store.save_run(run)
        with self.assertRaisesRegex(ValueError, "blocked by governance"):
            founder_review_decision(
                self.store,
                self._run_id,
                decision="accept_for_pr_prep",
                note="should fail",
                actor="shan",
            )
        run2 = self.store.get_run(self._run_id)
        self.assertEqual(run2["status"], "needs_review")
        kinds = [
            e.get("evidence_kind")
            for e in self.store.list_run_evidence(run_id=self._run_id, limit=20)
        ]
        self.assertNotIn(EVIDENCE_KIND_FOUNDER_DECISION, kinds)
        events = [
            e
            for e in self.store.list_run_events(self._run_id)
            if e.get("event_type") == "founder_review_decision"
        ]
        self.assertEqual(events, [])

    def test_no_except_pass_in_decision_path(self):
        import inspect
        import buildforme.execution_service as es

        src = inspect.getsource(es.founder_review_decision)
        self.assertNotIn("except Exception:\n        pass", src)
        self.assertNotIn("except Exception: pass", src)


if __name__ == "__main__":
    unittest.main()

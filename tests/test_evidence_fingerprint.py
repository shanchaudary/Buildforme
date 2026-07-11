"""Evidence fingerprint integrity — material fields bound before storage."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from buildforme.evidence import (
    build_evidence_bundle,
    compute_evidence_fingerprint,
    validate_evidence_for_storage,
)
from buildforme.execution_service import (
    create_run,
    execute_supervised,
    record_run_approval,
    run_preflight,
)
from buildforme.storage import LocalStore
from governance.constitution_engine import get_engine


def _base_bundle(**overrides):
    run = {
        "id": "run-fp-1",
        "project_id": "buildforme",
        "task_id": "task-1",
        "packet_id": "pkt-1",
        "provider_id": "codex",
        "repository": "owner/repo",
        "baseline_commit": "b" * 40,
        "execution_branch": "feature/fp-test",
        "transport": "cli",
        "constitution_version": "1.0.0",
        "constitution_hash": "c" * 64,
        "constitution_lease_id": "lease-1",
        "constitution_lease_fingerprint": "lf" * 32,
        "operating_mode": "IMPLEMENTATION",
        "risk": "YELLOW",
        "requested_capabilities": ["read_repository", "edit_repository"],
    }
    packet = {
        "id": "pkt-1",
        "allowed_files": ["src/**"],
        "forbidden_files": [".env"],
    }
    process = {
        "exit_code": 0,
        "pid": 4242,
        "timed_out": False,
        "cancelled": False,
        "cleanup_ok": True,
        "stdout": "did work",
        "stderr": "",
        "argv": ["codex", "exec", "-"],
        "process_group_isolated": True,
        "env_names": ["PATH"],
    }
    worktree = {
        "worktree_path": "/tmp/wt",
        "head_commit": "h" * 40,
        "branch": "feature/fp-test",
        "baseline_commit": "b" * 40,
    }
    diff = {
        "manifest": {
            "files_changed": ["src/a.py"],
            "files": [{"path": "src/a.py"}],
            "manifest_fingerprint": "m" * 64,
            "complete": True,
        },
        "files_changed": ["src/a.py"],
        "diff_stat": "1 file",
    }
    verification = {
        "passed": True,
        "blocking_reasons": [],
        "checks": [{"name": "tests", "status": "pass", "detail": "ok"}],
    }
    constitution_result = {"passed": True, "problems": []}
    kwargs = dict(
        run=run,
        packet=packet,
        process_result=process,
        worktree=worktree,
        diff=diff,
        provider_health={"version": "0.1.0", "executable": "/bin/codex"},
        verification=verification,
        constitution_result=constitution_result,
        events=[{"id": "e1", "event_type": "run_created"}],
        approved_baseline_sha="b" * 40,
        final_head_sha="h" * 40,
        execution_branch="feature/fp-test",
        patch_fingerprint="p" * 64,
        manifest_fingerprint="m" * 64,
    )
    kwargs.update(overrides)
    return build_evidence_bundle(**kwargs)


class FingerprintBindingTests(unittest.TestCase):
    def test_recalculate_matches_stored(self):
        bundle = _base_bundle()
        self.assertEqual(
            bundle["evidence_fingerprint"],
            compute_evidence_fingerprint(bundle),
        )

    def test_patch_change_changes_fingerprint(self):
        a = _base_bundle(patch_fingerprint="p" * 64)
        b = _base_bundle(patch_fingerprint="q" * 64)
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_manifest_change_changes_fingerprint(self):
        a = _base_bundle(manifest_fingerprint="m" * 64)
        b = _base_bundle(manifest_fingerprint="n" * 64)
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_files_changed_changes_fingerprint(self):
        a = _base_bundle()
        b = _base_bundle(
            diff={
                "manifest": {
                    "files_changed": ["src/a.py", "src/b.py"],
                    "files": [{"path": "src/a.py"}, {"path": "src/b.py"}],
                    "manifest_fingerprint": "m" * 64,
                    "complete": True,
                },
                "files_changed": ["src/a.py", "src/b.py"],
            }
        )
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_final_head_changes_fingerprint(self):
        a = _base_bundle(final_head_sha="h" * 40)
        b = _base_bundle(final_head_sha="i" * 40)
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_branch_changes_fingerprint(self):
        a = _base_bundle(execution_branch="feature/a")
        b = _base_bundle(execution_branch="feature/b")
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_baseline_changes_fingerprint(self):
        a = _base_bundle(approved_baseline_sha="b" * 40)
        b = _base_bundle(approved_baseline_sha="c" * 40)
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_provider_id_changes_fingerprint(self):
        a = _base_bundle()
        run = {
            "id": "run-fp-1",
            "project_id": "buildforme",
            "task_id": "task-1",
            "packet_id": "pkt-1",
            "provider_id": "claude",
            "repository": "owner/repo",
            "baseline_commit": "b" * 40,
            "execution_branch": "feature/fp-test",
            "transport": "cli",
            "constitution_version": "1.0.0",
            "constitution_hash": "c" * 64,
            "constitution_lease_id": "lease-1",
            "constitution_lease_fingerprint": "lf" * 32,
        }
        b = _base_bundle(run=run)
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_provider_version_changes_fingerprint(self):
        a = _base_bundle(provider_health={"version": "0.1.0", "executable": "x"})
        b = _base_bundle(provider_health={"version": "0.2.0", "executable": "x"})
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_verification_pass_changes_fingerprint(self):
        a = _base_bundle(
            verification={
                "passed": True,
                "blocking_reasons": [],
                "checks": [{"name": "tests", "status": "pass"}],
            }
        )
        b = _base_bundle(
            verification={
                "passed": False,
                "blocking_reasons": ["tests failed"],
                "checks": [{"name": "tests", "status": "fail"}],
            }
        )
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_blocking_reason_changes_fingerprint(self):
        a = _base_bundle(
            verification={
                "passed": False,
                "blocking_reasons": ["reason-a"],
                "checks": [],
            }
        )
        b = _base_bundle(
            verification={
                "passed": False,
                "blocking_reasons": ["reason-b"],
                "checks": [],
            }
        )
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_constitution_hash_changes_fingerprint(self):
        run_a = {
            "id": "run-fp-1",
            "project_id": "buildforme",
            "packet_id": "pkt-1",
            "provider_id": "codex",
            "repository": "owner/repo",
            "constitution_version": "1",
            "constitution_hash": "a" * 64,
            "constitution_lease_id": "L",
            "constitution_lease_fingerprint": "lf1",
        }
        run_b = dict(run_a)
        run_b["constitution_hash"] = "b" * 64
        a = _base_bundle(run=run_a)
        b = _base_bundle(run=run_b)
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_lease_fingerprint_changes(self):
        run_a = {
            "id": "run-fp-1",
            "project_id": "buildforme",
            "packet_id": "pkt-1",
            "provider_id": "codex",
            "repository": "owner/repo",
            "constitution_version": "1",
            "constitution_hash": "c" * 64,
            "constitution_lease_id": "L",
            "constitution_lease_fingerprint": "lease-a",
        }
        run_b = dict(run_a)
        run_b["constitution_lease_fingerprint"] = "lease-b"
        a = _base_bundle(run=run_a)
        b = _base_bundle(run=run_b)
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_constitution_validation_result_changes(self):
        a = _base_bundle(constitution_result={"passed": True, "problems": []})
        b = _base_bundle(constitution_result={"passed": False, "problems": ["law-1"]})
        self.assertNotEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])

    def test_patch_not_aliased_to_manifest(self):
        bundle = _base_bundle(patch_fingerprint="p" * 64, manifest_fingerprint="m" * 64)
        self.assertNotEqual(bundle["patch_fingerprint"], bundle["manifest_fingerprint"])
        self.assertEqual(bundle["patch_hash"], bundle["patch_fingerprint"])

    def test_no_post_fingerprint_fields_in_builder(self):
        """Builder fingerprint includes final fields when passed as arguments."""
        bundle = _base_bundle(
            final_head_sha="f" * 40,
            execution_branch="feature/final",
            patch_fingerprint="z" * 64,
        )
        self.assertEqual(bundle["final_head_sha"], "f" * 40)
        self.assertEqual(bundle["execution_branch"], "feature/final")
        self.assertEqual(bundle["patch_fingerprint"], "z" * 64)
        self.assertEqual(
            bundle["evidence_fingerprint"],
            compute_evidence_fingerprint(bundle),
        )


class StorageEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")
        # Shell run for FK
        self.store.save_run_for_setup(
            {
                "id": "run-fp-1",
                "project_id": "p",
                "provider_id": "codex",
                "repository": "owner/repo",
                "status": "needs_review",
                "execution_mode": "live_supervised",
            }
        )

    def test_correct_fingerprint_persists(self):
        bundle = _base_bundle()
        saved = self.store.save_run_evidence(bundle)
        self.assertEqual(saved["evidence_fingerprint"], bundle["evidence_fingerprint"])
        loaded = self.store.get_run_evidence("run-fp-1")
        self.assertEqual(loaded["evidence_fingerprint"], bundle["evidence_fingerprint"])
        self.assertEqual(
            compute_evidence_fingerprint(loaded),
            loaded["evidence_fingerprint"],
        )

    def test_missing_fingerprint_rejected(self):
        bundle = _base_bundle()
        del bundle["evidence_fingerprint"]
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.store.save_run_evidence(bundle)

    def test_incorrect_fingerprint_rejected(self):
        bundle = _base_bundle()
        bundle["evidence_fingerprint"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.store.save_run_evidence(bundle)

    def test_mutated_after_fingerprint_rejected(self):
        bundle = _base_bundle()
        bundle["patch_fingerprint"] = "mutated" + "0" * 57
        # fingerprint still old
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.store.save_run_evidence(bundle)

    def test_append_only_same_id(self):
        bundle = _base_bundle()
        self.store.save_run_evidence(bundle)
        again = dict(bundle)
        with self.assertRaisesRegex(ValueError, "append-only"):
            self.store.save_run_evidence(again)

    def test_missing_patch_rejected(self):
        bundle = _base_bundle()
        bundle["patch_fingerprint"] = None
        bundle["evidence_fingerprint"] = compute_evidence_fingerprint(bundle)
        with self.assertRaisesRegex(ValueError, "patch_fingerprint"):
            self.store.save_run_evidence(bundle)

    def test_validate_helper_flags_mismatch(self):
        bundle = _base_bundle()
        bundle["final_head_sha"] = "x" * 40
        problems = validate_evidence_for_storage(bundle)
        self.assertTrue(any("mismatch" in p for p in problems))


class ExecutionServiceEvidenceProofTests(unittest.TestCase):
    def setUp(self):
        import os

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
            "forbidden_paths": [".env", "secrets/**"],
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
        # Disposable repo
        import subprocess

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
        repo_name = self.store.get_project("buildforme").get("repository") or "shanchaudary/Buildforme"
        self.store.register_repository_binding(
            {
                "repository": repo_name,
                "local_path": str(self.root),
                "project_id": "buildforme",
            }
        )
        self.packet = {
            "id": "pkt-ev-proof",
            "objective": "write proof note",
            "acceptance_criteria": ["docs/STAGE6_PROOF_NOTE.md exists"],
            "allowed_files": ["docs/**"],
            "forbidden_files": [".env"],
            "risk": "YELLOW",
            "operating_mode": "IMPLEMENTATION",
        }

    def test_supervised_evidence_fingerprint_binds_finals(self):
        from buildforme.worktree import remove_worktree, worktree_status
        from buildforme.changed_files import collect_patch_evidence

        run = create_run(
            self.store,
            {
                "project_id": "buildforme",
                "provider_id": "codex",
                "packet": self.packet,
                "packet_id": self.packet["id"],
                "target_branch": "feature/ev-proof",
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
                    "health": {"version": "test", "executable": "python"},
                }

            def execute(self, run, packet, *, worktree_path, on_event=None):
                p = Path(worktree_path) / "docs" / "STAGE6_PROOF_NOTE.md"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("# proof\n", encoding="utf-8")
                return {
                    "ok": True,
                    "exit_code": 0,
                    "pid": 999,
                    "stdout": "ok",
                    "stderr": "",
                    "timed_out": False,
                    "cancelled": False,
                    "duration_seconds": 0.1,
                    "argv": ["python", "-c", "print(1)"],
                    "cleanup_ok": True,
                    "process_group_isolated": True,
                    "env_names": ["PATH"],
                    "health": {"version": "test", "executable": "python"},
                }

            def cancel(self, run_id):
                return {"cancelled": True}

        ready = {
            "provider_id": "codex",
            "available": True,
            "live_ready": True,
            "version_ok": True,
            "version": "test",
            "executable": "python",
            "unsupported_reasons": [],
            "constitution_acknowledged": True,
            "auth": {"status": "ready"},
        }
        with patch(
            "buildforme.execution_service.get_adapter", return_value=FakeAdapter()
        ):
            with patch(
                "buildforme.execution_service.health_check_provider", return_value=ready
            ):
                result = execute_supervised(self.store, run["id"])

        wt = result["run"].get("worktree_path")
        try:
            evidence = result["evidence"]
            self.assertTrue(evidence.get("evidence_fingerprint"))
            # Recalculate from stored material — do not mock fingerprint calc
            recalculated = compute_evidence_fingerprint(evidence)
            self.assertEqual(recalculated, evidence["evidence_fingerprint"])

            # Patch fingerprint matches collected post-run
            post_status = worktree_status(Path(wt))
            collected_patch = collect_patch_evidence(
                wt, baseline_commit=str(run["baseline_commit"])
            )
            self.assertEqual(
                evidence.get("patch_fingerprint"),
                collected_patch.get("patch_fingerprint"),
            )
            self.assertEqual(
                evidence.get("final_head_sha"),
                post_status.get("head_commit") or evidence.get("final_head_sha"),
            )
            self.assertEqual(
                evidence.get("execution_branch"),
                result["run"].get("execution_branch"),
            )
            # Mutating stored material must change fingerprint
            mutated = dict(evidence)
            mutated["patch_fingerprint"] = "deadbeef" * 8
            self.assertNotEqual(
                compute_evidence_fingerprint(mutated),
                evidence["evidence_fingerprint"],
            )
        finally:
            if wt:
                remove_worktree(
                    repo_root=self.root, worktree_path=Path(wt), force=True
                )


if __name__ == "__main__":
    unittest.main()

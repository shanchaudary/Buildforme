"""Stage 6 acceptance tests — real success path, no forced status bypasses."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from buildforme.adapters.registry import all_providers_have_adapters, list_live_adapter_ids
from buildforme.changed_files import collect_changed_file_manifest, collect_patch_evidence
from buildforme.execution_service import (
    create_run,
    execute_supervised,
    founder_review_decision,
    record_run_approval,
    run_preflight,
)
from buildforme.founder_auth import load_or_create_admin_secret, validate_loopback_host, verify_admin_secret
from buildforme.packet_generator import generate_agent_packet
from buildforme.process_env import build_provider_env
from buildforme.process_supervisor import ProcessSupervisor
from buildforme.provider_discovery import health_check_provider
from buildforme.redaction import contains_secret_marker, redact_text
from buildforme.storage import LocalStore
from buildforme.worktree import create_isolated_worktree, default_workspace_root, remove_worktree, resolve_repo_root
from governance.constitution_engine import get_engine
from governance.constitution_lease import seal_lease


def _ack_all(store: LocalStore) -> None:
    engine = get_engine(force_reload=True)
    for provider in store.list_providers():
        refreshed = engine.acknowledge_provider(provider, actor="shan")
        store.set_provider_constitution_ack(
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


class SqliteAuthorityTests(unittest.TestCase):
    def test_wal_and_fk(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = LocalStore(Path(temp.name) / "state.json")
        p = store.s6.db.pragmas()
        self.assertEqual(str(p["journal_mode"]).lower(), "wal")
        self.assertTrue(p["foreign_keys"])
        self.assertEqual(p["integrity_check"], "ok")
        self.assertEqual(p["schema_version"], 3)

    def test_concurrent_task_locks(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = LocalStore(Path(temp.name) / "state.json")
        errors: list[str] = []

        def try_lock(i: int) -> None:
            try:
                store.create_task_lock(
                    {"task_key": "same-task", "project_id": "p1", "run_id": f"r{i}"}
                )
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=try_lock, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        active = store.list_task_locks(active_only=True)
        self.assertEqual(len(active), 1)
        self.assertTrue(len(errors) >= 1)


class ProcessAndEnvTests(unittest.TestCase):
    def test_timeout_cancel_isolated(self):
        sup = ProcessSupervisor()
        r = sup.run(
            run_id="t1",
            argv=["python", "-c", "import time; time.sleep(10)"],
            cwd=Path.cwd(),
            timeout_seconds=1,
            provider_id="codex",
        )
        self.assertTrue(r["timed_out"])
        self.assertTrue(r.get("process_group_isolated"))

    def test_env_allowlist(self):
        env, names = build_provider_env("codex")
        self.assertIn("PATH", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)


class RedactionAndManifestTests(unittest.TestCase):
    def test_redaction(self):
        self.assertIn("REDACTED", redact_text("Authorization: Bearer sk-ant-abcdefghijklmnopqrstuvwxyz"))

    def test_manifest_fail_closed_invalid_baseline(self):
        import uuid

        root = resolve_repo_root()
        os.environ["BUILDFORME_ALLOW_DIRTY_PARENT"] = "1"
        suffix = uuid.uuid4().hex[:10]
        meta = create_isolated_worktree(
            repo_root=root,
            branch=f"feature/man-fail-{suffix}",
            run_id=f"mf{suffix}",
            allow_dirty_main=True,
            require_clean_parent=False,
        )
        try:
            m = collect_changed_file_manifest(meta["worktree_path"], baseline_commit="0" * 40)
            self.assertFalse(m["complete"])
            self.assertTrue(m.get("blocking_reasons"))
        finally:
            remove_worktree(repo_root=root, worktree_path=Path(meta["worktree_path"]), force=True)

    def test_manifest_includes_untracked_and_ignored(self):
        import uuid

        root = resolve_repo_root()
        os.environ["BUILDFORME_ALLOW_DIRTY_PARENT"] = "1"
        suffix = uuid.uuid4().hex[:10]
        meta = create_isolated_worktree(
            repo_root=root,
            branch=f"feature/man-ok-{suffix}",
            run_id=f"mo{suffix}",
            allow_dirty_main=True,
            require_clean_parent=False,
        )
        wt = Path(meta["worktree_path"])
        try:
            (wt / "docs").mkdir(exist_ok=True)
            (wt / "docs" / "STAGE6_PROOF_NOTE.md").write_text("# proof\n", encoding="utf-8")
            (wt / ".env").write_text("SECRET=x\n", encoding="utf-8")
            m = collect_changed_file_manifest(wt, baseline_commit=meta["baseline_commit"])
            self.assertTrue(m["complete"], m.get("blocking_reasons"))
            paths = set(m["files_changed"])
            self.assertIn("docs/STAGE6_PROOF_NOTE.md", paths)
            self.assertIn(".env", paths)
            patch = collect_patch_evidence(wt, baseline_commit=meta["baseline_commit"])
            self.assertTrue(patch["complete"])
            self.assertTrue(patch.get("patch_fingerprint"))
            self.assertNotEqual(patch.get("patch_fingerprint"), m.get("manifest_fingerprint"))
        finally:
            remove_worktree(repo_root=root, worktree_path=wt, force=True)


class FounderAuthTests(unittest.TestCase):
    def test_self_mint_rejected(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = LocalStore(Path(temp.name) / "state.json")
        with self.assertRaises(ValueError):
            store.create_founder_session(actor="shan", admin_secret="wrong")
        secret = load_or_create_admin_secret(store.runtime_dir)
        sess = store.create_founder_session(actor="shan", admin_secret=secret)
        self.assertTrue(sess.get("token"))
        self.assertTrue(sess.get("csrf_token"))
        auth = store.validate_founder_token(sess["token"])
        self.assertEqual(auth["actor"], "shan")

    def test_host_policy(self):
        validate_loopback_host("127.0.0.1:8787", configured_port=8787)
        with self.assertRaises(ValueError):
            validate_loopback_host("evil.com:8787", configured_port=8787)
        with self.assertRaises(ValueError):
            validate_loopback_host("localhost.attacker.com", configured_port=8787)


class SuccessfulSupervisedPathTests(unittest.TestCase):
    def setUp(self):
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
        self.store.set_project_execution_control("buildforme", execution_status="enabled", reason="test")
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
        # Confirm profile persisted
        assert self.store.get_project("buildforme").get("verification_profile", {}).get("profile_id") == "test-fast"
        _ack_all(self.store)
        self.root = resolve_repo_root()
        self.store.register_repository_binding(
            {
                "repository": "shanchaudary/Buildforme",
                "local_path": str(self.root),
                "project_id": "buildforme",
            }
        )
        packet = generate_agent_packet(
            {
                "source_type": "manual",
                "title": "Stage 6 success path",
                "objective": "Add a trivial docs note for supervised execution proof",
                "operating_mode": "IMPLEMENTATION",
                "allowed_files": ["docs/**", "tests/**"],
                "forbidden_files": [".env", "secrets/**"],
                "acceptance_criteria": ["Tests pass"],
                "target_repository": "shanchaudary/Buildforme",
                "target_branch": "feature/stage-6-success",
            }
        )
        self.packet = self.store.save_packet(packet)

    def tearDown(self):
        try:
            self.store.s6.db.close()
        except Exception:
            pass

    def test_real_success_path_no_forced_status(self):
        run = create_run(
            self.store,
            {
                "project_id": "buildforme",
                "provider_id": "codex",
                "packet": self.packet,
                "packet_id": self.packet["id"],
                "target_branch": "feature/stage-6-success",
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
        self.assertTrue(run.get("baseline_commit"))
        self.assertTrue(run.get("execution_branch"))
        self.assertNotEqual(run["execution_branch"], run["requested_target_branch"])
        self.assertTrue(run["execution_branch"].startswith("feature/"))

        pre = run_preflight(self.store, run["id"])
        self.assertTrue(pre["preflight"]["passed"], pre["preflight"].get("blocking_reasons"))
        run2 = self.store.get_run(run["id"])
        self.assertEqual(run2["status"], "awaiting_approval")

        # Real approvals — no forced status mutation
        for req in run2.get("approval_requirements") or ["shan_task_approval"]:
            record_run_approval(
                self.store, run["id"], requirement_type=req, decision="approved", actor="shan"
            )
        run3 = self.store.get_run(run["id"])
        self.assertEqual(run3["status"], "approved")

        fake_process = {
            "ok": True,
            "exit_code": 0,
            "stdout": "did work without secrets",
            "stderr": "",
            "timed_out": False,
            "cancelled": False,
            "duration_seconds": 0.2,
            "argv": ["python", "-c", "print(1)"],
            "cleanup_ok": True,
            "process_group_isolated": True,
            "env_names": ["PATH"],
            "health": {
                "version": "test",
                "executable": "python",
                "available": True,
                "live_ready": True,
            },
        }

        class FakeAdapter:
            def prepare_execution(self, run, packet):
                return {"prepared": True, "problems": [], "health": fake_process["health"]}

            def execute(self, run, packet, *, worktree_path, on_event=None):
                p = Path(worktree_path) / "docs" / "STAGE6_PROOF_NOTE.md"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("# stage6 proof\n", encoding="utf-8")
                if on_event:
                    on_event({"type": "process_output", "message": "writing proof", "stream": "stdout"})
                return fake_process

            def cancel(self, run_id):
                return {"cancelled": True}

        ready_health = {
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

        with patch("buildforme.execution_service.get_adapter", return_value=FakeAdapter()):
            with patch("buildforme.execution_service.health_check_provider", return_value=ready_health):
                result = execute_supervised(self.store, run["id"])

        wt = result["run"].get("worktree_path")
        try:
            self.assertEqual(result["run"]["status"], "needs_review")
            self.assertTrue(result["process"]["ok"])
            self.assertTrue(result["verification"]["passed"], result["verification"].get("blocking_reasons"))
            self.assertTrue(result["constitution_validation"].get("passed", True))
            self.assertTrue(result["review"].get("accept_for_pr_prep_allowed"), result["review"])
            self.assertIn("docs/STAGE6_PROOF_NOTE.md", result["evidence"].get("files_changed") or [])
            self.assertTrue(result["evidence"].get("patch_fingerprint"))
            self.assertTrue(result["evidence"].get("final_head_sha"))
            self.assertEqual(
                result["run"].get("execution_branch"),
                result["evidence"].get("execution_branch") or result["run"].get("execution_branch"),
            )
            # Workspace outside supervised repo
            self.assertNotIn(
                str(self.root).lower(),
                str(Path(wt).resolve()).lower().replace(str(self.root.resolve()).lower(), "OUT"),
            ) if False else None
            # Prefer: worktree not under repo root
            try:
                Path(wt).resolve().relative_to(self.root.resolve())
                outside = False
            except ValueError:
                outside = True
            self.assertTrue(outside, f"worktree should be outside repo: {wt}")

            decided = founder_review_decision(
                self.store, run["id"], decision="accept_for_pr_prep", note="accepted for PR prep"
            )
            self.assertEqual(decided["decision"], "accept_for_pr_prep")
            final = self.store.get_run(run["id"])
            self.assertEqual(final["status"], "completed")
            # Decision evidence append-only
            evidence_list = self.store.list_run_evidence(run_id=run["id"], limit=10)
            self.assertGreaterEqual(len(evidence_list), 2)
        finally:
            if wt:
                remove_worktree(repo_root=self.root, worktree_path=Path(wt), force=True)

    def test_forbidden_file_blocks_acceptance(self):
        # Unique packet/task to avoid lock collision with success test if parallelized
        packet = generate_agent_packet(
            {
                "source_type": "manual",
                "title": "Stage 6 fail path",
                "objective": "Prove forbidden file blocks accept",
                "operating_mode": "IMPLEMENTATION",
                "allowed_files": ["docs/**"],
                "forbidden_files": [".env"],
                "acceptance_criteria": ["safe"],
                "target_repository": "shanchaudary/Buildforme",
                "target_branch": "feature/stage-6-fail",
            }
        )
        packet = self.store.save_packet(packet)
        run = create_run(
            self.store,
            {
                "project_id": "buildforme",
                "provider_id": "codex",
                "packet": packet,
                "packet_id": packet["id"],
                "target_branch": "feature/stage-6-fail",
                "risk": "YELLOW",
                "execution_mode": "live_supervised",
            },
        )
        pre = run_preflight(self.store, run["id"])
        self.assertTrue(pre["preflight"]["passed"], pre["preflight"].get("blocking_reasons"))
        run2 = self.store.get_run(run["id"])
        for req in run2.get("approval_requirements") or ["shan_task_approval"]:
            record_run_approval(self.store, run["id"], requirement_type=req, decision="approved")
        self.assertEqual(self.store.get_run(run["id"])["status"], "approved")

        class BadAdapter:
            def prepare_execution(self, run, packet):
                return {"prepared": True, "problems": [], "health": {"version": "t", "executable": "python"}}

            def execute(self, run, packet, *, worktree_path, on_event=None):
                Path(worktree_path, ".env").write_text("SECRET=1\n", encoding="utf-8")
                return {
                    "ok": True,
                    "exit_code": 0,
                    "stdout": "done",
                    "stderr": "",
                    "timed_out": False,
                    "cancelled": False,
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
        with patch("buildforme.execution_service.get_adapter", return_value=BadAdapter()):
            with patch("buildforme.execution_service.health_check_provider", return_value=ready):
                result = execute_supervised(self.store, run["id"])
        wt = result["run"].get("worktree_path")
        try:
            self.assertFalse(result["verification"]["passed"])
            self.assertFalse(result["review"].get("accept_for_pr_prep_allowed"))
            with self.assertRaises(ValueError):
                founder_review_decision(
                    self.store, run["id"], decision="accept_for_pr_prep", note="should fail"
                )
        finally:
            if wt:
                remove_worktree(repo_root=self.root, worktree_path=Path(wt), force=True)


class AdapterRegistryTests(unittest.TestCase):
    def test_four_adapters(self):
        self.assertTrue(all_providers_have_adapters())
        self.assertEqual(set(list_live_adapter_ids()), {"claude", "codex", "glm", "grok"})


class LeaseMutationTests(unittest.TestCase):
    def test_storage_rejects_lease_mutation(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = LocalStore(Path(temp.name) / "state.json")
        engine = get_engine(force_reload=True)
        lease = engine.issue_run_lease(run_id="run-x", provider_id="codex", packet_id="pkt")
        # Need run shell for FK if any
        store.save_run_for_setup(
            {
                "id": "run-x",
                "project_id": "p",
                "provider_id": "codex",
                "repository": "a/b",
                "status": "draft",
                "execution_mode": "dry_run",
            }
        )
        store.save_constitution_lease(lease)
        tampered = seal_lease(dict(lease, provider_id="claude"))
        with self.assertRaises(ValueError):
            store.save_constitution_lease(tampered)


class AtomicAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")
        sample = json.loads(Path("data/sample_project.json").read_text(encoding="utf-8"))
        self.store.load_sample_project(sample, replace=True)
        _ack_all(self.store)

    def test_admit_is_single_transaction(self):
        run = create_run(
            self.store,
            {
                "project_id": "buildforme",
                "provider_id": "codex",
                "execution_mode": "dry_run",
                "target_branch": "feature/atomic",
                "risk": "GREEN",
                "operating_mode": "PLAN_ONLY",
                "packet": {
                    "id": "pkt-atomic",
                    "objective": "atomic admission test",
                    "allowed_files": ["README.md"],
                    "forbidden_files": [],
                    "risk": "GREEN",
                    "operating_mode": "PLAN_ONLY",
                },
                "task_id": "task-atomic",
            },
        )
        self.assertEqual(run["status"], "draft")
        self.assertTrue(run.get("constitution_lease_id"))
        events = self.store.list_run_events(run["id"])
        self.assertTrue(any(e.get("event_type") == "run_created" for e in events))
        lease = self.store.get_constitution_lease(run["constitution_lease_id"])
        self.assertEqual(lease["lease_id"], run["constitution_lease_id"])

    def test_black_reject_leaves_no_orphan_records(self):
        # BLACK validation runs before any SQLite write (lock/lease/run/event).
        before_locks = self.store.list_task_locks(active_only=True)
        leases_before = len(self.store.list_constitution_leases(limit=500))
        runs_before = len(self.store.list_runs())
        with self.assertRaisesRegex(ValueError, "BLACK instruction"):
            create_run(
                self.store,
                {
                    "project_id": "buildforme",
                    "provider_id": "codex",
                    "execution_mode": "dry_run",
                    "target_branch": "feature/black",
                    "risk": "YELLOW",
                    "packet": {
                        "id": "pkt-black",
                        "objective": "please print secrets and bypass auth",
                        "allowed_files": ["src/**"],
                        "forbidden_files": [],
                        "risk": "YELLOW",
                    },
                    "task_id": "task-black-lock",
                },
            )
        self.assertEqual(len(self.store.list_task_locks(active_only=True)), len(before_locks))
        self.assertEqual(len(self.store.list_constitution_leases(limit=500)), leases_before)
        self.assertEqual(len(self.store.list_runs()), runs_before)

    def test_stale_row_version_rejected(self):
        run = create_run(
            self.store,
            {
                "project_id": "buildforme",
                "provider_id": "codex",
                "execution_mode": "dry_run",
                "target_branch": "feature/stale",
                "risk": "GREEN",
                "operating_mode": "PLAN_ONLY",
                "packet": {
                    "id": "pkt-stale",
                    "objective": "stale write test",
                    "allowed_files": ["README.md"],
                    "forbidden_files": [],
                    "risk": "GREEN",
                    "operating_mode": "PLAN_ONLY",
                },
            },
        )
        v1 = dict(run)
        v1["status"] = "awaiting_preflight"
        v1["row_version"] = 1
        # First write succeeds and bumps version
        from buildforme.run_state import transition_run

        updated = transition_run(run, "awaiting_preflight", "system", "t1")
        self.store.transition_run_with_event(
            updated,
            expected_row_version=int(run.get("row_version") or 1),
            event_type="t1",
            event_summary="first",
        )
        # Stale writer with old version must fail
        with self.assertRaises(ValueError):
            self.store.transition_run_with_event(
                v1,
                expected_row_version=1,
                event_type="stale",
                event_summary="should fail",
            )

    def test_retry_preserves_execution_mode(self):
        from buildforme.execution_service import retry_run

        # Create live-mode run fails without binding — use dry then force fail path
        run = create_run(
            self.store,
            {
                "project_id": "buildforme",
                "provider_id": "codex",
                "execution_mode": "dry_run",
                "target_branch": "feature/retry",
                "risk": "YELLOW",
                "max_attempts": 3,
                "attempt": 0,
                "packet": {
                    "id": "pkt-retry",
                    "objective": "retry mode preserve",
                    "allowed_files": ["src/**"],
                    "forbidden_files": [],
                    "risk": "YELLOW",
                },
            },
        )
        run = self.store.get_run(run["id"])
        run["status"] = "failed"
        run["finished_at"] = "2020-01-01T00:00:00Z"
        self.store.save_run_for_setup(run)
        child = retry_run(self.store, run["id"])
        self.assertEqual(child.get("execution_mode"), "dry_run")
        self.assertEqual(child.get("provider_id"), "codex")
        self.assertEqual(child.get("parent_run_id"), run["id"])


class ProjectControlSqliteTests(unittest.TestCase):
    def test_project_control_is_sqlite(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = LocalStore(Path(temp.name) / "state.json")
        sample = json.loads(Path("data/sample_project.json").read_text(encoding="utf-8"))
        store.load_sample_project(sample, replace=True)
        store.set_project_execution_control(
            "buildforme", execution_status="enabled", reason="sqlite authority"
        )
        # Drop JSON if present — SQLite still authoritative
        if store.project_exec_controls_path.exists():
            store.project_exec_controls_path.unlink()
        ctrl = store.get_project_execution_control("buildforme")
        self.assertTrue(ctrl.get("explicit"))
        self.assertEqual(ctrl.get("execution_status"), "enabled")
        # Direct SQLite read
        raw = store.s6.get_project_execution_control("buildforme")
        self.assertIsNotNone(raw)


class LoopbackBindTests(unittest.TestCase):
    def test_non_loopback_bind_rejected(self):
        from buildforme.server import _assert_loopback_bind

        with self.assertRaises(ValueError):
            _assert_loopback_bind("0.0.0.0")
        with self.assertRaises(ValueError):
            _assert_loopback_bind("192.168.1.1")
        self.assertEqual(_assert_loopback_bind("127.0.0.1"), "127.0.0.1")
        self.assertEqual(_assert_loopback_bind("localhost"), "127.0.0.1")


class MigrationTests(unittest.TestCase):
    def test_dry_run_and_cutover(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = Path(temp.name)
        (runtime / "runs.json").write_text(
            json.dumps(
                {
                    "runs": [
                        {
                            "id": "run-mig",
                            "project_id": "p",
                            "provider_id": "codex",
                            "repository": "a/b",
                            "status": "draft",
                            "execution_mode": "dry_run",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (runtime / "project_execution_controls.json").write_text(
            json.dumps(
                {
                    "controls": [
                        {
                            "project_id": "p",
                            "execution_status": "enabled",
                            "reason": "mig",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        store = LocalStore(runtime / "state.json")
        preview = store.s6.migrate_from_json(runtime, dry_run=True, cutover=False)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["preview"]["runs"], 1)
        report = store.s6.migrate_from_json(runtime, dry_run=False, cutover=True)
        self.assertGreaterEqual(report["imported"].get("runs", 0), 1)
        self.assertTrue(report.get("cutover"))
        self.assertTrue(store.s6.get_migration_cutover())
        # Idempotent replay
        report2 = store.s6.migrate_from_json(runtime, dry_run=False, cutover=True)
        self.assertEqual(report2["imported"].get("runs"), 1)


class ProviderCompatibilityTests(unittest.TestCase):
    def test_missing_binary_not_live(self):
        from buildforme.provider_compatibility import (
            compatibility_allows_live,
            verify_provider_compatibility,
        )

        compat = verify_provider_compatibility("codex", None)
        self.assertFalse(
            compatibility_allows_live(compat, constitution_ack=True, enabled=True)
        )

    def test_env_marker_alone_insufficient_without_contract(self):
        from buildforme.provider_compatibility import compatibility_allows_live

        fake = {
            "live_ready_components": {
                "binary_available": True,
                "version_verified": True,
                "auth_verified": True,
                "command_contract_verified": False,
                "non_interactive_mode_verified": False,
                "prompt_delivery_verified": False,
                "cwd_behavior_verified": True,
                "capabilities_verified": False,
            }
        }
        self.assertFalse(compatibility_allows_live(fake, constitution_ack=True, enabled=True))


class RealSubprocessIntegrationTests(unittest.TestCase):
    def test_process_supervisor_runs_python(self):
        sup = ProcessSupervisor()
        with tempfile.TemporaryDirectory() as td:
            result = sup.run(
                run_id="subproc-1",
                argv=[os.environ.get("PYTHON", "python"), "-c", "print('hello-stage6')"],
                cwd=td,
                timeout_seconds=15,
                provider_id="generic",
                use_provider_env_allowlist=False,
                env={"PATH": os.environ.get("PATH", "")},
            )
        self.assertTrue(result.get("ok"), result)
        self.assertIn("hello-stage6", result.get("stdout") or "")
        self.assertTrue(result.get("process_group_isolated"))
        self.assertIsNotNone(result.get("pid"))


class FounderHttpAuthTests(unittest.TestCase):
    def test_live_create_requires_founder(self):
        from http.server import ThreadingHTTPServer
        import threading
        from urllib import request, error

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = Path(temp.name) / "state.json"
        store = LocalStore(state)
        sample = json.loads(Path("data/sample_project.json").read_text(encoding="utf-8"))
        store.load_sample_project(sample, replace=True)
        _ack_all(store)
        load_or_create_admin_secret(store.runtime_dir)

        from buildforme.server import BuildformeRequestHandler

        server = ThreadingHTTPServer(("127.0.0.1", 0), BuildformeRequestHandler)
        server.state_path = state  # type: ignore[attr-defined]
        port = server.server_address[1]
        server.server_port = port  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)

        # Unauthenticated live create must fail
        body = json.dumps(
            {
                "project_id": "buildforme",
                "provider_id": "codex",
                "execution_mode": "live_supervised",
                "target_branch": "feature/auth",
                "risk": "YELLOW",
            }
        ).encode()
        req = request.Request(
            f"http://127.0.0.1:{port}/api/runs",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Host": f"127.0.0.1:{port}",
            },
            method="POST",
        )
        try:
            request.urlopen(req, timeout=5)
            self.fail("expected HTTP error")
        except error.HTTPError as exc:
            self.assertIn(exc.code, {400, 403})


if __name__ == "__main__":
    unittest.main()

"""Adversarial closure tests for the final Stage 6 blockers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from buildforme.evidence import validate_evidence_for_storage
from buildforme.execution_store import Stage6Store
from buildforme.outcome_evidence import build_run_outcome_evidence
from buildforme.process_supervisor import ProcessSupervisor
from buildforme.provider_discovery import probe_authentication
from buildforme.stage6_smoke_acceptance import evaluate_stage6_smoke_acceptance
from buildforme.storage import LocalStore


class ProcessTerminationTruthTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX process-group proof exercised in CI; Windows has separate implementation")
    def test_cancel_confirms_stubborn_child_tree_is_gone(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        script = Path(td.name) / "tree.py"
        script.write_text(
            "import signal,subprocess,sys,time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        supervisor = ProcessSupervisor()
        result_box = {}

        def run():
            result_box["result"] = supervisor.run(
                run_id="tree-run",
                argv=[sys.executable, str(script)],
                cwd=td.name,
                timeout_seconds=60,
                provider_id="test",
                env=dict(os.environ),
                use_provider_env_allowlist=False,
            )

        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            with supervisor._lock:
                if "tree-run" in supervisor._procs:
                    break
            time.sleep(0.05)
        cancel = supervisor.cancel("tree-run")
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        result = result_box["result"]
        self.assertTrue(cancel["termination_confirmation"]["confirmed"])
        self.assertTrue(result["termination_confirmation"]["confirmed"])
        self.assertTrue(result["cleanup_ok"])
        self.assertEqual(result["termination_confirmation"]["live_pids"], [])


class OutcomeEvidenceAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")
        self.run = self.store.save_run_for_setup(
            {
                "id": "outcome-run",
                "project_id": "p",
                "provider_id": "codex",
                "repository": "o/r",
                "execution_mode": "live_supervised",
                "mode": "live_supervised",
                "status": "running",
                "scope_fingerprint": "scope",
                "constitution_hash": "c" * 64,
                "constitution_lease_id": "lease",
                "constitution_lease_fingerprint": "lf",
                "status_history": [],
                "row_version": 1,
            }
        )

    def _process(self, *, confirmed=True):
        return {
            "ok": False,
            "exit_code": 124,
            "stdout": "",
            "stderr": "timeout",
            "timed_out": True,
            "cancelled": False,
            "cleanup_ok": confirmed,
            "termination_log": [],
            "termination_confirmation": {
                "confirmed": confirmed,
                "live_pids": [] if confirmed else [99999],
            },
        }

    def test_outcome_evidence_and_terminal_transition_commit_atomically(self):
        current = self.store.get_run("outcome-run")
        proposed = dict(current)
        proposed["status"] = "timed_out"
        proposed["process_result"] = self._process()
        evidence = build_run_outcome_evidence(
            run=current,
            outcome="timed_out",
            previous_status="running",
            resulting_status="timed_out",
            previous_row_version=current["row_version"],
            process_result=proposed["process_result"],
            reason="timeout",
        )
        saved = self.store.commit_run_mutation(
            proposed,
            expected_row_version=current["row_version"],
            mutation_type="failure_detail",
            event_type="supervised_timed_out",
            event_summary="timeout",
            evidence=evidence,
        )
        self.assertEqual(saved["status"], "timed_out")
        persisted = self.store.get_evidence_by_id(evidence["evidence_id"])
        self.assertEqual(persisted["evidence_fingerprint"], evidence["evidence_fingerprint"])
        self.assertEqual(validate_evidence_for_storage(persisted), [])

    def test_bad_evidence_rolls_back_run_event_and_evidence(self):
        current = self.store.get_run("outcome-run")
        proposed = dict(current)
        proposed["status"] = "failed"
        proposed["process_result"] = self._process(confirmed=False)
        evidence = build_run_outcome_evidence(
            run=current,
            outcome="termination_unconfirmed",
            previous_status="running",
            resulting_status="failed",
            previous_row_version=current["row_version"],
            process_result=proposed["process_result"],
            reason="cleanup failed",
        )
        evidence["evidence_fingerprint"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.store.commit_run_mutation(
                proposed,
                expected_row_version=current["row_version"],
                mutation_type="failure_detail",
                event_type="failed",
                evidence=evidence,
            )
        after = self.store.get_run("outcome-run")
        self.assertEqual(after["status"], "running")
        self.assertEqual(after["row_version"], current["row_version"])
        self.assertEqual(self.store.list_run_events("outcome-run"), [])
        self.assertEqual(self.store.list_run_evidence(run_id="outcome-run"), [])

    def test_cancelled_status_rejects_unconfirmed_termination(self):
        current = self.store.get_run("outcome-run")
        evidence = build_run_outcome_evidence(
            run=current,
            outcome="cancelled",
            previous_status="running",
            resulting_status="cancelled",
            previous_row_version=current["row_version"],
            process_result=self._process(confirmed=False),
            reason="cancel",
        )
        self.assertTrue(validate_evidence_for_storage(evidence))


class ProviderAuthenticationProbeTests(unittest.TestCase):
    def _fake_cli(self, exit_code: int, output: str = "") -> tuple[tempfile.TemporaryDirectory, str]:
        td = tempfile.TemporaryDirectory()
        path = Path(td.name) / ("provider.cmd" if os.name == "nt" else "provider")
        if os.name == "nt":
            path.write_text(f"@echo {output}\r\n@exit /b {exit_code}\r\n", encoding="utf-8")
        else:
            path.write_text(f"#!/bin/sh\nprintf '%s' '{output}'\nexit {exit_code}\n", encoding="utf-8")
            path.chmod(0o755)
        return td, str(path)

    def test_env_marker_does_not_override_failed_executable_probe(self):
        td, executable = self._fake_cli(7, "OPENAI_API_KEY=should-not-persist")
        self.addCleanup(td.cleanup)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "secret-value"}, clear=False):
            result = probe_authentication("codex", executable)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["probe_verified"])
        self.assertNotIn("secret-value", json.dumps(result))
        self.assertNotIn("should-not-persist", json.dumps(result))

    def test_successful_executable_probe_can_verify_cached_login_without_env_marker(self):
        td, executable = self._fake_cli(0, "logged in")
        self.addCleanup(td.cleanup)
        # Preserve the platform runtime environment needed to launch a controlled
        # executable, while proving that provider auth markers are absent.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("CODEX_API_KEY", None)
            result = probe_authentication("codex", executable)
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["probe_verified"])
        self.assertFalse(result["output_persisted"])
        self.assertNotIn("OPENAI_API_KEY", result["env_names"])
        self.assertNotIn("CODEX_API_KEY", result["env_names"])

    def test_provider_without_probe_contract_fails_closed(self):
        td, executable = self._fake_cli(0, "ok")
        self.addCleanup(td.cleanup)
        result = probe_authentication("grok", executable, provider_record={})
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["probe_verified"])


class AtomicMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.store = Stage6Store(self.runtime / "authority.db")
        self.store.save_run_for_setup(
            {
                "id": "existing",
                "project_id": "p",
                "provider_id": "codex",
                "repository": "o/r",
                "status": "draft",
                "execution_mode": "dry_run",
            }
        )

    def test_import_error_rolls_back_all_prior_imports(self):
        (self.runtime / "runs.json").write_text(
            json.dumps({"runs": [{"id": "new", "project_id": "p", "provider_id": "codex", "repository": "o/r", "status": "draft", "execution_mode": "dry_run"}]}),
            encoding="utf-8",
        )
        (self.runtime / "run_evidence.json").write_text(
            json.dumps({"evidence": [{"evidence_id": "bad", "run_id": "new", "evidence_kind": "execution"}]}),
            encoding="utf-8",
        )
        report = self.store.migrate_from_json(self.runtime, dry_run=False, cutover=True)
        self.assertTrue(report["rolled_back"])
        self.assertFalse(report["atomic_commit"])
        with self.assertRaises(KeyError):
            self.store.get_run("new")
        self.assertEqual(self.store.get_run("existing")["id"], "existing")
        self.assertIsNone(self.store.get_migration_cutover())

    def test_successful_import_atomically_replaces_authority(self):
        (self.runtime / "runs.json").write_text(
            json.dumps({"runs": [{"id": "new-ok", "project_id": "p", "provider_id": "codex", "repository": "o/r", "status": "draft", "execution_mode": "dry_run"}]}),
            encoding="utf-8",
        )
        report = self.store.migrate_from_json(self.runtime, dry_run=False, cutover=True)
        self.assertTrue(report["atomic_commit"], report)
        self.assertFalse(report["rolled_back"])
        self.assertEqual(self.store.get_run("new-ok")["id"], "new-ok")
        self.assertTrue(self.store.get_migration_cutover())

    def test_active_run_blocks_migration_without_write(self):
        active = self.store.get_run("existing")
        active["status"] = "running"
        self.store.save_run_for_setup(active)
        report = self.store.migrate_from_json(self.runtime, dry_run=False, cutover=True)
        self.assertTrue(report["rolled_back"])
        self.assertIn("active run", report["errors"][0])


class SmokeAcceptanceTests(unittest.TestCase):
    def test_strict_smoke_rejects_missing_required_file_and_unverified_auth(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
        (root / "README.md").write_text("x", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "x"], cwd=root, check=True, capture_output=True)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=root, text=True).strip()
        report = evaluate_stage6_smoke_acceptance(
            health={"live_ready": True, "auth": {"status": "ready", "probe_verified": False}},
            execution_result={
                "process": {"ok": True, "exit_code": 0, "pid": 1, "cleanup_ok": True, "termination_confirmation": {"confirmed": True}},
                "verification": {"passed": True},
                "review": {"accept_for_pr_prep_allowed": True},
                "evidence": {"evidence_id": "x", "evidence_fingerprint": "f", "files_changed": ["README.md"], "patch_fingerprint": "p", "manifest_fingerprint": "m", "final_head_sha": head},
            },
            final_run={"status": "completed"},
            persisted_evidence={"evidence_id": "x"},
            decision_evidence={"evidence_fingerprint": "d"},
            repository_root=root,
            original_head=head,
            original_branch=branch,
        )
        self.assertFalse(report["passed"])
        self.assertIn("auth_probe_verified", report["failed_checks"])
        self.assertIn("required_files_produced", report["failed_checks"])


if __name__ == "__main__":
    unittest.main()

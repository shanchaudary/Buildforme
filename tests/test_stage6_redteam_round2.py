"""Second red-team pass for Stage 6 proof authority."""

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

from buildforme.execution_service import cancel_run
from buildforme.execution_store import Stage6Store
from buildforme.outcome_evidence import (
    build_run_outcome_evidence,
    validate_run_outcome_evidence,
)
from buildforme.process_supervisor import ProcessSupervisor
from buildforme.provider_discovery import probe_authentication
from buildforme.storage import LocalStore

ROOT = Path(__file__).resolve().parents[1]


class CancellationRegistryLossTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")

    def _run(self, run_id: str, status: str):
        return self.store.save_run_for_setup(
            {
                "id": run_id,
                "project_id": "p",
                "provider_id": "codex",
                "repository": "o/r",
                "execution_mode": "live_supervised",
                "mode": "live_supervised",
                "status": status,
                "scope_fingerprint": "scope",
                "constitution_hash": "c" * 64,
                "constitution_lease_id": "lease",
                "constitution_lease_fingerprint": "lf",
                "status_history": [],
            }
        )

    def test_supervisor_registry_miss_is_not_termination_proof(self):
        result = ProcessSupervisor().cancel("missing-run")
        self.assertFalse(result["cleanup_ok"])
        self.assertFalse(result["termination_confirmation"]["confirmed"])
        self.assertEqual(
            result["termination_confirmation"]["method"],
            "process_registry_miss",
        )

    def test_running_cancel_after_registry_loss_fails_closed(self):
        self._run("lost-running", "running")
        before = self.store.get_run("lost-running")
        saved = cancel_run(self.store, "lost-running")
        self.assertEqual(saved["status"], "failed")
        self.assertNotEqual(saved["status"], "cancelled")
        self.assertGreater(saved["row_version"], before["row_version"])
        evidence = self.store.get_evidence_by_id(saved["outcome_evidence_id"])
        self.assertEqual(evidence["outcome"], "termination_unconfirmed")
        self.assertFalse(evidence["process"]["termination_confirmation"]["confirmed"])

    def test_prestart_cancel_uses_lifecycle_proof_not_registry(self):
        self._run("prestart", "approved")
        saved = cancel_run(self.store, "prestart")
        self.assertEqual(saved["status"], "rejected")
        evidence = self.store.get_evidence_by_id(saved["outcome_evidence_id"])
        self.assertTrue(evidence["process"]["termination_confirmation"]["confirmed"])
        self.assertEqual(
            evidence["process"]["termination_confirmation"]["method"],
            "lifecycle_prestart_or_postrun",
        )


class OutcomeFingerprintCoverageTests(unittest.TestCase):
    def _evidence(self):
        run = {
            "id": "r",
            "project_id": "p",
            "provider_id": "codex",
            "repository": "o/r",
            "execution_mode": "live_supervised",
            "scope_fingerprint": "scope",
            "constitution_version": "1.0.0",
            "constitution_hash": "c" * 64,
            "constitution_lease_id": "lease",
            "constitution_lease_fingerprint": "lf",
        }
        return build_run_outcome_evidence(
            run=run,
            outcome="failed",
            previous_status="running",
            resulting_status="failed",
            previous_row_version=4,
            process_result={
                "ok": False,
                "exit_code": 1,
                "stdout": "out",
                "stderr": "err",
                "cleanup_ok": True,
                "termination_confirmation": {"confirmed": True, "live_pids": []},
                "termination_log": [{"action": "proof", "ok": True}],
            },
            reason="failure",
        )

    def test_every_material_preview_and_governance_field_is_fingerprinted(self):
        mutations = [
            ("collected_at", "2099-01-01T00:00:00+00:00"),
            ("constitution_version", "9.9.9"),
            ("immutable", False),
        ]
        for field, value in mutations:
            evidence = self._evidence()
            evidence[field] = value
            self.assertTrue(any("fingerprint mismatch" in problem for problem in validate_run_outcome_evidence(evidence)))

        evidence = self._evidence()
        evidence["process"]["stdout_preview"] = "tampered"
        self.assertTrue(any("fingerprint mismatch" in problem for problem in validate_run_outcome_evidence(evidence)))

        evidence = self._evidence()
        evidence["worktree"]["execution_branch"] = "evil"
        self.assertTrue(any("fingerprint mismatch" in problem for problem in validate_run_outcome_evidence(evidence)))


class AuthProbeAuthorityTests(unittest.TestCase):
    def _fake(self, output: str, exit_code: int = 0):
        td = tempfile.TemporaryDirectory()
        path = Path(td.name) / "codex"
        path.write_text(
            "#!/bin/sh\nprintf '%s' " + json.dumps(output) + f"\nexit {exit_code}\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return td, str(path)

    @unittest.skipIf(os.name == "nt", "shell fixture is POSIX-only")
    def test_provider_record_cannot_override_auth_command_or_success(self):
        td, executable = self._fake("Not logged in", exit_code=0)
        self.addCleanup(td.cleanup)
        result = probe_authentication(
            "codex",
            executable,
            provider_record={
                "auth_probe": {
                    "args": ["dangerous", "login"],
                    "success_exit_codes": [0],
                    "read_only": True,
                }
            },
        )
        self.assertFalse(result["probe_verified"])
        self.assertEqual(result["command_shape"], ["codex", "login", "status"])
        self.assertTrue(result["negative_status_marker"])

    @unittest.skipIf(os.name == "nt", "shell fixture is POSIX-only")
    def test_exit_zero_without_positive_login_marker_is_not_ready(self):
        td, executable = self._fake("status command completed", exit_code=0)
        self.addCleanup(td.cleanup)
        result = probe_authentication("codex", executable)
        self.assertFalse(result["probe_verified"])
        self.assertFalse(result["positive_status_marker"])

    @unittest.skipIf(os.name == "nt", "shell fixture is POSIX-only")
    def test_official_codex_logged_in_marker_is_ready(self):
        td, executable = self._fake("Logged in using ChatGPT", exit_code=0)
        self.addCleanup(td.cleanup)
        result = probe_authentication("codex", executable)
        self.assertTrue(result["probe_verified"])
        self.assertTrue(result["positive_status_marker"])
        self.assertFalse(result["negative_status_marker"])

    def test_unverified_claude_status_contract_fails_closed(self):
        result = probe_authentication("claude", sys.executable)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["probe_verified"])


class MigrationCoordinationTests(unittest.TestCase):
    def test_exclusive_maintenance_lock_blocks_other_process_transactions(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db_path = Path(temp.name) / "authority.db"
        store = Stage6Store(db_path)
        store.save_run_for_setup(
            {
                "id": "existing",
                "project_id": "p",
                "provider_id": "codex",
                "repository": "o/r",
                "status": "draft",
                "execution_mode": "dry_run",
            }
        )
        script = Path(temp.name) / "writer.py"
        script.write_text(
            "from buildforme.execution_store import Stage6Store\n"
            "import sys\n"
            "s=Stage6Store(sys.argv[1])\n"
            "r=s.get_run('existing')\n"
            "r['result_summary']='writer'\n"
            "s.save_run(r, expected_row_version=r['row_version'])\n"
            "print('done', flush=True)\n",
            encoding="utf-8",
        )
        child_env = dict(os.environ)
        child_env["PYTHONPATH"] = str(ROOT) + (
            os.pathsep + child_env["PYTHONPATH"]
            if child_env.get("PYTHONPATH")
            else ""
        )
        with store.db.maintenance_lock(timeout_seconds=5):
            proc = subprocess.Popen(
                [sys.executable, str(script), str(db_path)],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=child_env,
            )
            time.sleep(0.5)
            if proc.poll() is not None:
                early_stdout, early_stderr = proc.communicate(timeout=5)
                self.fail(
                    "writer exited before lock release; "
                    f"stdout={early_stdout!r} stderr={early_stderr!r}"
                )
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertIn("done", stdout)
        self.assertEqual(store.get_run("existing")["result_summary"], "writer")

    def test_migration_method_holds_maintenance_lock_contract(self):
        source = Path("buildforme/execution_store.py").read_text(encoding="utf-8")
        self.assertIn("with self.db.maintenance_lock", source)


class WindowsJobContractTests(unittest.TestCase):
    def test_windows_job_contract_is_present_and_duplicate_terminator_removed(self):
        job_source = Path("buildforme/windows_job.py").read_text(encoding="utf-8")
        self.assertIn("JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE", job_source)
        self.assertIn("AssignProcessToJobObject", job_source)
        supervisor_source = Path("buildforme/process_supervisor.py").read_text(encoding="utf-8")
        self.assertNotIn("def _terminate_tree", supervisor_source)
        self.assertIn("WindowsJob.create_and_assign", supervisor_source)


if __name__ == "__main__":
    unittest.main()

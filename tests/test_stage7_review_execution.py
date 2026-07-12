"""Adversarial tests for Packet 7B automated blind reviewer execution."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from buildforme.db import SCHEMA_VERSION
from buildforme.evidence import build_evidence_bundle
from buildforme.governance import compute_run_scope_fingerprint
from governance.constitution_engine import get_engine
from buildforme.review_contracts import build_review_report_record
from buildforme.review_execution import (
    REVIEW_COMMAND_CONTRACTS,
    build_review_command,
    build_review_execution_record,
    build_verified_blind_review_packet,
    execute_independent_review_assignment,
    parse_strict_review_output,
)
from buildforme.review_service import create_independent_review_cycle, submit_independent_review_report
from buildforme.storage import LocalStore
from buildforme.changed_files import collect_changed_file_manifest, collect_patch_evidence


class Stage7ReviewExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self._git("init")
        self._git("config", "user.email", "review@test.local")
        self._git("config", "user.name", "review-test")
        self._git("remote", "add", "origin", "https://github.com/shanchaudary/Buildforme.git")
        (self.root / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "baseline")
        self.baseline = self._git_out("rev-parse", "HEAD").strip()
        self._git("checkout", "-b", "feature/stage7b-run")
        (self.root / "app.py").write_text("def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n", encoding="utf-8")

        self.store = LocalStore(Path(self.temp.name) / "state.json")
        self.store.upsert_project(
            {
                "id": "buildforme",
                "name": "Buildforme",
                "repository": "shanchaudary/Buildforme",
                "status": "active",
                "local_repository_root": str(self.root),
            }
        )
        self.store.register_repository_binding(
            {
                "repository": "shanchaudary/Buildforme",
                "local_path": str(self.root),
                "project_id": "buildforme",
            }
        )
        engine = get_engine(force_reload=True)
        packet = engine.attach_to_packet(
            {
                "id": "pkt-stage7b",
                "objective": "Add subtraction function",
                "acceptance_criteria": ["sub returns a-b"],
                "target_repository": "shanchaudary/Buildforme",
                "target_branch": "feature/stage7b",
                "allowed_files": ["app.py"],
                "forbidden_files": [".env"],
            }
        )
        lease = engine.issue_run_lease(
            run_id="run-stage7b",
            provider_id="glm",
            packet_id=packet["id"],
            actor="test",
        )
        self.store.save_constitution_lease(lease)
        run = {
            "id": "run-stage7b",
            "project_id": "buildforme",
            "provider_id": "glm",
            "repository": "shanchaudary/Buildforme",
            "repository_local_path": str(self.root),
            "baseline_ref": "HEAD",
            "baseline_commit": self.baseline,
            "requested_target_branch": "feature/stage7b",
            "execution_branch": "feature/stage7b-run",
            "target_branch": "feature/stage7b-run",
            "operating_mode": "IMPLEMENTATION",
            "risk": "YELLOW",
            "status": "needs_review",
            "execution_mode": "live_supervised",
            "mode": "live_supervised",
            "transport": "cli",
            "requested_capabilities": ["read_repository", "edit_repository", "run_tests", "produce_patch"],
            "packet_id": packet["id"],
            "packet": packet,
            "review": {"hard_blocks": [], "accept_for_pr_prep_allowed": True},
            "worktree_path": str(self.root),
            "row_version": 1,
        }
        run = engine.attach_to_run(run, lease=lease, actor="test")
        run["scope_fingerprint"] = compute_run_scope_fingerprint(run, run["packet"])
        self.run = self.store.save_run_for_setup(run)
        manifest = collect_changed_file_manifest(self.root, baseline_commit=self.baseline)
        patch_ev = collect_patch_evidence(self.root, baseline_commit=self.baseline)
        evidence = build_evidence_bundle(
            run=self.run,
            packet=self.run["packet"],
            process_result={
                "ok": True,
                "exit_code": 0,
                "pid": 100,
                "stdout": "ok",
                "stderr": "",
                "cleanup_ok": True,
                "process_group_isolated": True,
            },
            worktree={
                "worktree_path": str(self.root),
                "baseline_commit": self.baseline,
                "head_commit": self.baseline,
                "branch": self.run["execution_branch"],
            },
            diff={"manifest": manifest, "patch_fingerprint": patch_ev["patch_fingerprint"]},
            provider_health={"version": "test", "executable": "glm"},
            verification={"passed": True, "blocking_reasons": [], "checks": []},
            constitution_result={"passed": True},
            approved_baseline_sha=self.baseline,
            final_head_sha=self.baseline,
            execution_branch=self.run["execution_branch"],
            patch_fingerprint=patch_ev["patch_fingerprint"],
            manifest_fingerprint=manifest["manifest_fingerprint"],
        )
        self.evidence = self.store.save_run_evidence(evidence)
        for provider_id in ("codex", "claude"):
            self.store.set_provider_constitution_ack(
                provider_id,
                {
                    "constitution_supported": True,
                    "constitution_acknowledged": True,
                    "constitution_version": engine.version(),
                    "constitution_hash": engine.content_hash(),
                    "constitution_last_refresh": "now",
                    "constitution_acknowledged_at": "now",
                    "constitution_ack_actor": "test",
                },
            )
        result = create_independent_review_cycle(
            self.store,
            self.run["id"],
            reviewers=[
                {"reviewer_id": "codex-reviewer", "provider_id": "codex", "role": "correctness"},
                {"reviewer_id": "claude-reviewer", "provider_id": "claude", "role": "security"},
            ],
            actor="shan",
        )
        self.cycle = result["cycle"]
        self.assignment = next(a for a in result["assignments"] if a["provider_id"] == "codex")
        self.claude_assignment = next(
            a for a in result["assignments"] if a["provider_id"] == "claude"
        )

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def _git_out(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True)

    def _fake_codex(self, *, payload=None, raw_output=None, mutate=False):
        path = Path(self.temp.name) / "codex"
        if payload is None:
            payload = {"verdict": "pass", "summary": "clear", "findings": []}
        agent_text = json.dumps(payload)
        event = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": agent_text}})
        output = raw_output if raw_output is not None else event
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "_ = sys.stdin.read()\n"
            + ("pathlib.Path('reviewer-wrote.txt').write_text('bad')\n" if mutate else "")
            + f"print({output!r})\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def _health(self, executable):
        return {
            "provider_id": "codex",
            "live_ready": True,
            "available": True,
            "executable": executable,
            "version": "codex-test",
            "unsupported_reasons": [],
            "auth": {"status": "ready", "probe_verified": True},
        }

    def _fake_claude(self, *, payload=None, mutate=False):
        path = Path(self.temp.name) / "claude"
        if payload is None:
            payload = {"verdict": "pass", "summary": "clear", "findings": []}
        wrapper = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": payload,
        }
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "_ = sys.stdin.read()\n"
            + ("pathlib.Path('claude-wrote.txt').write_text('bad')\n" if mutate else "")
            + f"print({json.dumps(wrapper)!r})\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def _claude_health(self, executable):
        return {
            "provider_id": "claude",
            "live_ready": True,
            "available": True,
            "executable": executable,
            "version": "2.1.205",
            "unsupported_reasons": [],
            "auth": {"status": "ready", "probe_verified": True},
        }

    def test_schema_v5(self):
        self.assertEqual(SCHEMA_VERSION, 8)
        self.assertEqual(self.store.s6.db.pragmas()["schema_version"], 8)

    def test_packet_reproves_exact_worktree_and_is_blind(self):
        packet, snapshot, root = build_verified_blind_review_packet(
            self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
        )
        self.assertEqual(root, self.root.resolve())
        self.assertEqual(snapshot["patch_fingerprint"], self.evidence["patch_fingerprint"])
        self.assertFalse(any(packet["blind_context"].values()))
        self.assertIn("constitution_reminder", packet)
        self.assertIn("text", packet["constitution_reminder"])
        self.assertNotIn("reports", packet)
        self.assertNotIn("findings", packet)

    def test_packet_rejects_worktree_drift(self):
        (self.root / "app.py").write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "immutable execution evidence"):
            build_verified_blind_review_packet(
                self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
            )

    def test_command_contract_is_code_owned_read_only(self):
        command = build_review_command("codex", "/tmp/codex")
        self.assertTrue(command["read_only"])
        self.assertIn("read-only", command["argv"])
        self.assertNotIn("workspace-write", command["argv"])
        self.assertEqual(set(REVIEW_COMMAND_CONTRACTS), {"codex", "claude"})
        claude = build_review_command("claude", "/tmp/claude")
        self.assertIn("plan", claude["argv"])
        self.assertIn("Read,Grep,Glob", claude["argv"])
        self.assertNotIn("workspace-write", claude["argv"])
        with self.assertRaisesRegex(ValueError, "no approved"):
            build_review_command("grok", "/tmp/grok")

    def test_strict_parser_rejects_prose_fences_and_ambiguity(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            parse_strict_review_output("codex", "looks good")
        fenced = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "```json\\n{}\\n```"}})
        with self.assertRaisesRegex(ValueError, "markdown fences"):
            parse_strict_review_output("codex", fenced)
        one = {"verdict": "pass", "summary": "a", "findings": []}
        two = {"verdict": "pass", "summary": "b", "findings": []}
        lines = "\n".join(
            json.dumps({"item": {"type": "agent_message", "text": json.dumps(item)}})
            for item in (one, two)
        )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            parse_strict_review_output("codex", lines)

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_real_read_only_process_commits_execution_and_report_atomically(self):
        executable = self._fake_codex()
        with patch("buildforme.review_execution.health_check_provider", return_value=self._health(executable)):
            result = execute_independent_review_assignment(
                self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
            )
        self.assertEqual(result["report"]["verdict"], "pass")
        attempts = self.store.list_review_execution_attempts(self.assignment["assignment_id"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "succeeded")
        self.assertTrue(attempts[0]["worktree_unchanged"])
        self.assertTrue(attempts[0]["post_snapshot_proven"])
        self.assertTrue(attempts[0]["auth_probe_verified"])
        self.assertEqual(len(self.store.list_review_reports(self.cycle["cycle_id"])), 1)

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_two_distinct_provider_reviewers_reach_clear_quorum(self):
        codex_executable = self._fake_codex()
        claude_executable = self._fake_claude()

        def health(provider_id, _provider, force_compat=True):
            del force_compat
            if provider_id == "codex":
                return self._health(codex_executable)
            if provider_id == "claude":
                return self._claude_health(claude_executable)
            raise AssertionError(provider_id)

        with patch(
            "buildforme.review_execution.health_check_provider",
            side_effect=health,
        ):
            execute_independent_review_assignment(
                self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
            )
            execute_independent_review_assignment(
                self.store,
                self.cycle["cycle_id"],
                self.claude_assignment["assignment_id"],
            )
        from buildforme.review_service import aggregate_independent_review_cycle

        finalized = aggregate_independent_review_cycle(
            self.store, self.cycle["cycle_id"]
        )
        self.assertEqual(finalized["cycle"]["status"], "clear")
        self.assertEqual(
            set(finalized["aggregate"]["provider_ids"]), {"codex", "claude"}
        )
        self.assertEqual(finalized["aggregate"]["distinct_provider_count"], 2)

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_mutating_reviewer_fails_closed_without_report(self):
        executable = self._fake_codex(mutate=True)
        with patch("buildforme.review_execution.health_check_provider", return_value=self._health(executable)):
            with self.assertRaisesRegex(ValueError, "mutated"):
                execute_independent_review_assignment(
                    self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
                )
        self.assertEqual(self.store.list_review_reports(self.cycle["cycle_id"]), [])
        attempts = self.store.list_review_execution_attempts(self.assignment["assignment_id"])
        self.assertEqual(attempts[-1]["status"], "failed")
        self.assertFalse(attempts[-1]["worktree_unchanged"])
        self.assertFalse((self.root / "reviewer-wrote.txt").exists())
        self.assertFalse(attempts[-1]["retry_safe"])
        self.assertEqual(self.store.get_review_cycle(self.cycle["cycle_id"])["status"], "blocked")

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_malformed_reviewer_output_fails_closed_without_report(self):
        executable = self._fake_codex(raw_output="not-json")
        with patch("buildforme.review_execution.health_check_provider", return_value=self._health(executable)):
            with self.assertRaisesRegex(ValueError, "output rejected"):
                execute_independent_review_assignment(
                    self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
                )
        self.assertEqual(self.store.list_review_reports(self.cycle["cycle_id"]), [])
        attempt = self.store.list_review_execution_attempts(self.assignment["assignment_id"])[-1]
        self.assertEqual(attempt["status"], "failed")
        self.assertTrue(attempt["retry_safe"])
        self.assertEqual(self.store.get_review_assignment(self.assignment["assignment_id"])["status"], "pending")

    def test_unavailable_provider_records_failure_and_no_report(self):
        health = {
            "provider_id": "codex",
            "live_ready": False,
            "available": True,
            "executable": "codex",
            "version": "test",
            "unsupported_reasons": ["authentication unknown"],
        }
        with patch("buildforme.review_execution.health_check_provider", return_value=health):
            with self.assertRaisesRegex(ValueError, "not live-ready"):
                execute_independent_review_assignment(
                    self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
                )
        self.assertEqual(self.store.list_review_reports(self.cycle["cycle_id"]), [])
        attempt = self.store.list_review_execution_attempts(self.assignment["assignment_id"])[-1]
        self.assertEqual(attempt["status"], "failed")
        self.assertTrue(attempt["retry_safe"])
        self.assertEqual(self.store.get_review_assignment(self.assignment["assignment_id"])["status"], "pending")

    def test_authenticated_storage_rejects_divergent_findings(self):
        packet, snapshot, _root = build_verified_blind_review_packet(
            self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
        )
        packet = self.store.save_review_packet_atomic(packet=packet, actor="test")
        claim_id = "claim-divergence"
        claimed = self.store.claim_review_assignment_execution_atomic(
            cycle_id=self.cycle["cycle_id"],
            assignment_id=self.assignment["assignment_id"],
            packet_id=packet["packet_id"],
            claim_id=claim_id,
            actor="test",
        )
        cycle = claimed["cycle"]
        assignment = claimed["assignment"]
        report, findings = build_review_report_record(
            cycle=cycle,
            assignment=assignment,
            payload={
                "verdict": "changes_required",
                "summary": "repair",
                "findings": [
                    {
                        "severity": "medium",
                        "category": "correctness",
                        "summary": "bug",
                        "evidence": "app.py line 1",
                        "recommendation": "repair",
                    }
                ],
            },
        )
        process = {
            "ok": True,
            "exit_code": 0,
            "pid": 123,
            "stdout": "{}",
            "stderr": "",
            "cleanup_ok": True,
            "process_group_isolated": True,
            "argv": build_review_command("codex", "test-reviewer")["argv"],
        }
        command = build_review_command("codex", "test-reviewer")
        execution = build_review_execution_record(
            packet=packet,
            assignment=assignment,
            command=command,
            health={
                "version": "test",
                "executable": "test-reviewer",
                "live_ready": True,
                "auth": {"probe_verified": True},
            },
            process_result=process,
            pre_snapshot=snapshot,
            post_snapshot=snapshot,
            status="succeeded",
            claim_id=claim_id,
            report_fingerprint=report["report_fingerprint"],
            post_snapshot_proven=True,
            process_started=True,
        )
        divergent = [dict(findings[0])]
        divergent[0]["summary"] = "different row"
        with self.assertRaisesRegex(ValueError, "diverge"):
            self.store.submit_review_report_atomic(
                cycle_id=cycle["cycle_id"],
                assignment_id=assignment["assignment_id"],
                report=report,
                findings=divergent,
                actor="reviewer",
                execution=execution,
            )
        self.assertEqual(self.store.list_review_reports(cycle["cycle_id"]), [])
        self.assertEqual(
            self.store.list_review_execution_attempts(assignment["assignment_id"]), []
        )

    def test_second_execution_claim_is_rejected_atomically(self):
        packet, _snapshot, _root = build_verified_blind_review_packet(
            self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
        )
        packet = self.store.save_review_packet_atomic(packet=packet, actor="test")
        self.store.claim_review_assignment_execution_atomic(
            cycle_id=self.cycle["cycle_id"],
            assignment_id=self.assignment["assignment_id"],
            packet_id=packet["packet_id"],
            claim_id="claim-one",
            actor="test",
        )
        with self.assertRaisesRegex(ValueError, "already claimed|unavailable"):
            self.store.claim_review_assignment_execution_atomic(
                cycle_id=self.cycle["cycle_id"],
                assignment_id=self.assignment["assignment_id"],
                packet_id=packet["packet_id"],
                claim_id="claim-two",
                actor="test",
            )

    def test_repository_remote_mismatch_blocks_packet(self):
        self._git("remote", "set-url", "origin", "https://github.com/other/wrong.git")
        with self.assertRaisesRegex(ValueError, "repository mismatch|remote identity"):
            build_verified_blind_review_packet(
                self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
            )

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_post_snapshot_failure_records_unproven_integrity_block(self):
        executable = self._fake_codex()
        from buildforme import review_execution as module

        original = module._collect_snapshot
        calls = {"count": 0}

        def fail_second(root, evidence):
            calls["count"] += 1
            if calls["count"] == 1:
                return original(root, evidence)
            raise ValueError("post proof unavailable")

        with patch("buildforme.review_execution.health_check_provider", return_value=self._health(executable)), patch(
            "buildforme.review_execution._collect_snapshot", side_effect=fail_second
        ):
            with self.assertRaisesRegex(ValueError, "post-review worktree proof failed"):
                execute_independent_review_assignment(
                    self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
                )
        attempt = self.store.list_review_execution_attempts(self.assignment["assignment_id"])[-1]
        self.assertFalse(attempt["post_snapshot_proven"])
        self.assertFalse(attempt["worktree_unchanged"])
        self.assertFalse(attempt["retry_safe"])
        self.assertEqual(self.store.get_review_cycle(self.cycle["cycle_id"])["status"], "blocked")

    def test_health_probe_exception_records_retry_safe_failure(self):
        with patch(
            "buildforme.review_execution.health_check_provider",
            side_effect=RuntimeError("probe crashed"),
        ):
            with self.assertRaisesRegex(ValueError, "health probe failed"):
                execute_independent_review_assignment(
                    self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
                )
        attempt = self.store.list_review_execution_attempts(self.assignment["assignment_id"])[-1]
        self.assertEqual(attempt["failure_code"], "health_probe_failed")
        self.assertTrue(attempt["retry_safe"])
        self.assertEqual(self.store.get_review_assignment(self.assignment["assignment_id"])["status"], "pending")

    def test_storage_rejects_forged_success_command_contract(self):
        packet, snapshot, _root = build_verified_blind_review_packet(
            self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
        )
        packet = self.store.save_review_packet_atomic(packet=packet, actor="test")
        claim_id = "claim-forged"
        claimed = self.store.claim_review_assignment_execution_atomic(
            cycle_id=self.cycle["cycle_id"],
            assignment_id=self.assignment["assignment_id"],
            packet_id=packet["packet_id"],
            claim_id=claim_id,
            actor="test",
        )
        assignment = claimed["assignment"]
        cycle = claimed["cycle"]
        report, findings = build_review_report_record(
            cycle=cycle,
            assignment=assignment,
            payload={"verdict": "pass", "summary": "fake", "findings": []},
        )
        forged = build_review_execution_record(
            packet=packet,
            assignment=assignment,
            command={
                "contract_id": "forged.write.v1",
                "read_only": True,
                "argv": ["codex", "exec", "-s", "workspace-write"],
            },
            health={
                "version": "test",
                "executable": "codex",
                "live_ready": True,
                "auth": {"probe_verified": True},
            },
            process_result={
                "exit_code": 0,
                "cleanup_ok": True,
                "argv": ["codex", "exec", "-s", "workspace-write"],
            },
            pre_snapshot=snapshot,
            post_snapshot=snapshot,
            status="succeeded",
            claim_id=claim_id,
            report_fingerprint=report["report_fingerprint"],
            post_snapshot_proven=True,
            process_started=True,
        )
        with self.assertRaisesRegex(ValueError, "command contract|argv"):
            self.store.submit_review_report_atomic(
                cycle_id=cycle["cycle_id"],
                assignment_id=assignment["assignment_id"],
                report=report,
                findings=findings,
                actor="reviewer",
                execution=forged,
            )
        self.assertEqual(self.store.list_review_reports(cycle["cycle_id"]), [])

    def test_direct_report_submission_is_disabled(self):
        with self.assertRaisesRegex(ValueError, "direct review report submission disabled"):
            submit_independent_review_report(
                self.store,
                self.cycle["cycle_id"],
                self.assignment["assignment_id"],
                payload={"verdict": "pass", "summary": "fake", "findings": []},
            )
        source = Path("buildforme/server.py").read_text(encoding="utf-8")
        self.assertNotIn('path.endswith("/submit")', source)
        self.assertIn('path.endswith("/execute")', source)


if __name__ == "__main__":
    unittest.main()

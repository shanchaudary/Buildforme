"""Adversarial tests for Stage 7 Packet 7A independent-review authority."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from buildforme.db import SCHEMA_VERSION
from buildforme.evidence import build_evidence_bundle
from buildforme.governance import compute_run_scope_fingerprint
from buildforme.review_gate import collect_hard_blocks
from buildforme.review_contracts import (
    build_review_cycle_record,
    build_review_report_record,
    validate_finding_for_storage,
)
from buildforme.review_service import (
    aggregate_independent_review_cycle,
    create_independent_review_cycle,
    get_independent_review_cycle_view,
    require_clear_independent_review,
    submit_independent_review_report,
)
from buildforme.storage import LocalStore


class Stage7ReviewAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")
        self.run = {
            "id": "run-stage7",
            "project_id": "buildforme",
            "provider_id": "codex",
            "repository": "shanchaudary/Buildforme",
            "repository_local_path": self.temp.name,
            "baseline_ref": "HEAD",
            "baseline_commit": "a" * 40,
            "requested_target_branch": "feature/stage7",
            "execution_branch": "feature/stage7-run",
            "target_branch": "feature/stage7-run",
            "operating_mode": "IMPLEMENTATION",
            "risk": "YELLOW",
            "status": "needs_review",
            "execution_mode": "live_supervised",
            "mode": "live_supervised",
            "transport": "cli",
            "requested_capabilities": ["read_repository", "edit_repository", "run_tests", "produce_patch"],
            "constitution_version": "1.0.0",
            "constitution_hash": "c" * 64,
            "constitution_lease_id": "lease-stage7",
            "constitution_lease_fingerprint": "l" * 64,
            "packet": {
                "id": "pkt-stage7",
                "objective": "review test",
                "target_repository": "shanchaudary/Buildforme",
                "target_branch": "feature/stage7",
                "allowed_files": ["buildforme/**", "tests/**"],
                "forbidden_files": [".env"],
            },
            "review": {"hard_blocks": [], "accept_for_pr_prep_allowed": True},
            "row_version": 1,
        }
        self.run["scope_fingerprint"] = compute_run_scope_fingerprint(self.run, self.run["packet"])
        self.run = self.store.save_run_for_setup(self.run)
        evidence = build_evidence_bundle(
            run=self.run,
            packet=self.run["packet"],
            process_result={
                "ok": True,
                "exit_code": 0,
                "pid": 123,
                "stdout": "ok",
                "stderr": "",
                "cleanup_ok": True,
                "process_group_isolated": True,
            },
            worktree={
                "worktree_path": self.temp.name,
                "baseline_commit": self.run["baseline_commit"],
                "head_commit": self.run["baseline_commit"],
                "branch": self.run["execution_branch"],
            },
            diff={
                "manifest": {
                    "complete": True,
                    "files": [{"path": "buildforme/x.py"}],
                    "files_changed": ["buildforme/x.py"],
                    "manifest_fingerprint": "m" * 64,
                    "diff_stat": "modified buildforme/x.py",
                },
                "patch_fingerprint": "p" * 64,
            },
            provider_health={"version": "test", "executable": "codex"},
            verification={"passed": True, "blocking_reasons": [], "checks": []},
            constitution_result={"passed": True},
            approved_baseline_sha=self.run["baseline_commit"],
            final_head_sha=self.run["baseline_commit"],
            execution_branch=self.run["execution_branch"],
            patch_fingerprint="p" * 64,
            manifest_fingerprint="m" * 64,
        )
        self.evidence = self.store.save_run_evidence(evidence)
        # Default provider registry contains all four; no live claim is made in Packet 7A.
        self.reviewers = [
            {"reviewer_id": "security-reviewer", "provider_id": "claude", "role": "security"},
            {"reviewer_id": "correctness-reviewer", "provider_id": "grok", "role": "correctness"},
        ]

    def _cycle(self):
        return create_independent_review_cycle(
            self.store, self.run["id"], reviewers=self.reviewers, actor="shan"
        )

    def _pass_report(self, assignment):
        return submit_independent_review_report(
            self.store,
            assignment["cycle_id"],
            assignment["assignment_id"],
            payload={"verdict": "pass", "summary": "No blocking defect", "findings": []},
            actor=assignment["reviewer_id"],
        )

    def test_schema_v4(self):
        self.assertEqual(SCHEMA_VERSION, 4)
        self.assertEqual(self.store.s6.db.pragmas()["schema_version"], 4)

    def test_implementer_cannot_review_own_execution(self):
        reviewers = [
            {"reviewer_id": "self", "provider_id": "codex", "role": "general"},
            {"reviewer_id": "other", "provider_id": "claude", "role": "security"},
        ]
        with self.assertRaisesRegex(ValueError, "cannot review its own"):
            create_independent_review_cycle(self.store, self.run["id"], reviewers=reviewers)

    def test_duplicate_provider_rejected(self):
        reviewers = [
            {"reviewer_id": "a", "provider_id": "claude", "role": "security"},
            {"reviewer_id": "b", "provider_id": "claude", "role": "correctness"},
        ]
        with self.assertRaisesRegex(ValueError, "duplicate reviewer provider"):
            create_independent_review_cycle(self.store, self.run["id"], reviewers=reviewers)

    def test_cycle_binds_run_evidence_scope_and_constitution_atomically(self):
        result = self._cycle()
        cycle = result["cycle"]
        run = result["run"]
        self.assertEqual(cycle["evidence_id"], self.evidence["evidence_id"])
        self.assertEqual(cycle["evidence_fingerprint"], self.evidence["evidence_fingerprint"])
        self.assertEqual(cycle["scope_fingerprint"], self.run["scope_fingerprint"])
        self.assertEqual(cycle["constitution_hash"], self.run["constitution_hash"])
        self.assertTrue(run["stage7_review_required"])
        self.assertEqual(run["stage7_review_cycle_id"], cycle["cycle_id"])

    def test_second_active_cycle_rejected(self):
        self._cycle()
        with self.assertRaisesRegex(ValueError, "active independent review cycle"):
            self._cycle()

    def test_blind_report_cannot_claim_consensus_or_founder_authority(self):
        result = self._cycle()
        assignment = result["assignments"][0]
        with self.assertRaisesRegex(ValueError, "forbidden authority or non-blind"):
            submit_independent_review_report(
                self.store,
                assignment["cycle_id"],
                assignment["assignment_id"],
                payload={"verdict": "pass", "findings": [], "consensus": "all pass"},
            )

    def test_critical_finding_is_forced_blocking(self):
        result = self._cycle()
        assignment = result["assignments"][0]
        submitted = submit_independent_review_report(
            self.store,
            assignment["cycle_id"],
            assignment["assignment_id"],
            payload={
                "verdict": "block",
                "summary": "critical defect",
                "findings": [
                    {
                        "severity": "critical",
                        "category": "governance",
                        "blocking": False,
                        "summary": "authority bypass",
                        "evidence": "call path bypasses storage authority",
                        "recommendation": "route through atomic authority",
                    }
                ],
            },
        )
        self.assertTrue(submitted["findings"][0]["blocking"])

    def test_report_is_append_only(self):
        result = self._cycle()
        assignment = result["assignments"][0]
        self._pass_report(assignment)
        with self.assertRaisesRegex(ValueError, "not pending|append-only"):
            self._pass_report(assignment)

    def test_quorum_required_before_aggregation(self):
        result = self._cycle()
        self._pass_report(result["assignments"][0])
        with self.assertRaisesRegex(ValueError, "quorum not met"):
            aggregate_independent_review_cycle(self.store, result["cycle"]["cycle_id"])

    def test_clear_quorum_binds_run_and_removes_stage7_hard_block(self):
        result = self._cycle()
        for assignment in result["assignments"]:
            self._pass_report(assignment)
        finalized = aggregate_independent_review_cycle(
            self.store, result["cycle"]["cycle_id"], actor="shan"
        )
        self.assertEqual(finalized["cycle"]["status"], "clear")
        run = self.store.get_run(self.run["id"])
        self.assertEqual(run["independent_review"]["status"], "clear")
        self.assertTrue(run["independent_review"]["quorum_met"])
        require_clear_independent_review(self.store, run)
        blocks = collect_hard_blocks(
            run=run,
            evidence=self.evidence,
            verification=self.evidence["verification"],
            constitution_validation={"passed": True, "valid": True},
        )
        self.assertFalse(any("Stage 7" in block for block in blocks), blocks)

    def test_blocking_finding_produces_repair_required_and_founder_block(self):
        result = self._cycle()
        first, second = result["assignments"]
        submit_independent_review_report(
            self.store,
            first["cycle_id"],
            first["assignment_id"],
            payload={
                "verdict": "changes_required",
                "summary": "repair",
                "findings": [
                    {
                        "severity": "high",
                        "category": "security",
                        "summary": "unsafe path",
                        "evidence": "file escapes allowed path",
                        "recommendation": "constrain path",
                    }
                ],
            },
        )
        self._pass_report(second)
        finalized = aggregate_independent_review_cycle(
            self.store, result["cycle"]["cycle_id"]
        )
        self.assertEqual(finalized["cycle"]["status"], "repair_required")
        run = self.store.get_run(self.run["id"])
        with self.assertRaisesRegex(ValueError, "clear Stage 7"):
            require_clear_independent_review(self.store, run)

    def test_policy_cannot_disable_blind_or_blocking_laws(self):
        for policy in (
            {"blind_review": False},
            {"implementer_provider_forbidden": False},
            {"critical_high_always_blocking": False},
            {"founder_override_blocking_findings": True},
        ):
            with self.subTest(policy=policy):
                with self.assertRaisesRegex(ValueError, "cannot weaken"):
                    create_independent_review_cycle(
                        self.store,
                        self.run["id"],
                        reviewers=self.reviewers,
                        policy=policy,
                    )

    def test_storage_rejects_self_consistent_forged_cycle_authority(self):
        forged_cases = (
            ("scope_fingerprint", "forged-scope", "scope"),
            ("constitution_hash", "f" * 64, "Constitution"),
            ("constitution_lease_id", "forged-lease", "lease"),
            ("provider_id", "glm", "implementer"),
        )
        for field, value, message in forged_cases:
            with self.subTest(field=field):
                forged_run = dict(self.run)
                forged_run[field] = value
                cycle, assignments = build_review_cycle_record(
                    run=forged_run,
                    evidence=self.evidence,
                    reviewers=self.reviewers,
                    actor="shan",
                )
                with self.assertRaisesRegex(ValueError, message):
                    self.store.create_review_cycle_atomic(
                        cycle=cycle,
                        assignments=assignments,
                        actor="shan",
                    )

    def test_storage_rejects_assignment_set_not_equal_to_cycle_reviewers(self):
        cycle, assignments = build_review_cycle_record(
            run=self.run,
            evidence=self.evidence,
            reviewers=self.reviewers,
            actor="shan",
        )
        with self.assertRaisesRegex(ValueError, "exactly match"):
            self.store.create_review_cycle_atomic(
                cycle=cycle,
                assignments=assignments[:-1],
                actor="shan",
            )

    def test_storage_rejects_cycle_bound_to_superseded_execution_evidence(self):
        cycle, assignments = build_review_cycle_record(
            run=self.run,
            evidence=self.evidence,
            reviewers=self.reviewers,
            actor="shan",
        )
        newer = build_evidence_bundle(
            run=self.run,
            packet=self.run["packet"],
            process_result={
                "ok": True,
                "exit_code": 0,
                "pid": 456,
                "stdout": "new",
                "stderr": "",
                "cleanup_ok": True,
                "process_group_isolated": True,
            },
            worktree={
                "worktree_path": self.temp.name,
                "baseline_commit": self.run["baseline_commit"],
                "head_commit": self.run["baseline_commit"],
                "branch": self.run["execution_branch"],
            },
            diff={
                "manifest": {
                    "complete": True,
                    "files": [{"path": "buildforme/y.py"}],
                    "files_changed": ["buildforme/y.py"],
                    "manifest_fingerprint": "n" * 64,
                },
                "patch_fingerprint": "q" * 64,
            },
            provider_health={"version": "test", "executable": "codex"},
            verification={"passed": True, "blocking_reasons": [], "checks": []},
            constitution_result={"passed": True},
            approved_baseline_sha=self.run["baseline_commit"],
            final_head_sha=self.run["baseline_commit"],
            execution_branch=self.run["execution_branch"],
            patch_fingerprint="q" * 64,
            manifest_fingerprint="n" * 64,
        )
        self.store.save_run_evidence(newer)
        with self.assertRaisesRegex(ValueError, "latest execution evidence"):
            self.store.create_review_cycle_atomic(
                cycle=cycle,
                assignments=assignments,
                actor="shan",
            )

    def test_storage_rejects_findings_divergent_from_report(self):
        result = self._cycle()
        cycle = result["cycle"]
        assignment = result["assignments"][0]
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
                        "evidence": "line 10",
                        "recommendation": "fix",
                    }
                ],
            },
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
            )

    def test_finding_fingerprint_is_independently_validated(self):
        result = self._cycle()
        cycle = result["cycle"]
        assignment = result["assignments"][0]
        report, findings = build_review_report_record(
            cycle=cycle,
            assignment=assignment,
            payload={
                "verdict": "changes_required",
                "summary": "repair",
                "findings": [
                    {
                        "severity": "high",
                        "category": "security",
                        "summary": "defect",
                        "evidence": "proof",
                        "recommendation": "repair",
                    }
                ],
            },
        )
        finding = dict(findings[0])
        finding["finding_fingerprint"] = "0" * 64
        problems = validate_finding_for_storage(
            finding,
            report=report,
            cycle=cycle,
            assignment=assignment,
        )
        self.assertIn("review finding fingerprint mismatch", problems)

    def test_blind_cycle_view_withholds_reports_until_finalized(self):
        result = self._cycle()
        self._pass_report(result["assignments"][0])
        active = get_independent_review_cycle_view(
            self.store, result["cycle"]["cycle_id"]
        )
        self.assertTrue(active["blind_material_withheld"])
        self.assertEqual(active["reports"], [])
        self.assertEqual(active["findings"], [])
        self._pass_report(result["assignments"][1])
        aggregate_independent_review_cycle(
            self.store, result["cycle"]["cycle_id"]
        )
        final = get_independent_review_cycle_view(
            self.store, result["cycle"]["cycle_id"]
        )
        self.assertFalse(final["blind_material_withheld"])
        self.assertEqual(len(final["reports"]), 2)

    def test_review_service_has_no_unrestricted_run_write(self):
        source = Path("buildforme/review_service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"save_run", "save_run_for_setup", "save_run_legacy_json"}:
                    forbidden.append((node.func.attr, node.lineno))
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()

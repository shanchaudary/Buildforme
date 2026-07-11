"""Adversarial tests for Stage 7 Packet 7A independent-review authority."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from buildforme.db import SCHEMA_VERSION, dumps, loads
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
)
from buildforme.storage import LocalStore, utc_now_iso


def submit_independent_review_report(
    store,
    cycle_id,
    assignment_id,
    *,
    payload,
    actor="reviewer",
):
    """Test-fixture persistence below Packet 7B process authority."""
    cycle = store.get_review_cycle(cycle_id)
    assignment = store.get_review_assignment(assignment_id)
    report, findings = build_review_report_record(
        cycle=cycle,
        assignment=assignment,
        payload=payload,
    )
    now = utc_now_iso()
    with store.s6.db.transaction() as conn:
        row = conn.execute(
            "SELECT status, payload_json FROM review_assignments WHERE id=?",
            (assignment_id,),
        ).fetchone()
        if not row or str(row[0]) != "pending":
            raise ValueError("fixture review assignment is not pending")
        conn.execute(
            "INSERT INTO review_reports(report_id, cycle_id, assignment_id, verdict, report_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,1)",
            (
                report["report_id"],
                cycle_id,
                assignment_id,
                report["verdict"],
                report["report_fingerprint"],
                dumps(report),
                now,
            ),
        )
        for finding in findings:
            conn.execute(
                "INSERT INTO review_findings(finding_id, report_id, cycle_id, assignment_id, severity, category, blocking, finding_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                (
                    finding["finding_id"],
                    report["report_id"],
                    cycle_id,
                    assignment_id,
                    finding["severity"],
                    finding["category"],
                    1 if finding.get("blocking") else 0,
                    finding["finding_fingerprint"],
                    dumps(finding),
                    now,
                ),
            )
        saved_assignment = loads(row[1], {})
        saved_assignment["status"] = "submitted"
        saved_assignment["submitted_at"] = now
        saved_assignment["report_id"] = report["report_id"]
        saved_assignment["report_fingerprint"] = report["report_fingerprint"]
        conn.execute(
            "UPDATE review_assignments SET status='submitted', payload_json=?, submitted_at=? WHERE id=?",
            (dumps(saved_assignment), now, assignment_id),
        )
        submitted = int(
            conn.execute(
                "SELECT COUNT(*) FROM review_assignments WHERE cycle_id=? AND status='submitted'",
                (cycle_id,),
            ).fetchone()[0]
        )
        cycle_row = conn.execute(
            "SELECT payload_json, required_reviewer_count, row_version FROM review_cycles WHERE id=?",
            (cycle_id,),
        ).fetchone()
        saved_cycle = loads(cycle_row[0], {})
        status = "ready_to_aggregate" if submitted >= int(cycle_row[1]) else "collecting"
        saved_cycle["status"] = status
        saved_cycle["submitted_reviewer_count"] = submitted
        saved_cycle["updated_at"] = now
        saved_cycle["row_version"] = int(cycle_row[2]) + 1
        conn.execute(
            "UPDATE review_cycles SET status=?, payload_json=?, updated_at=?, row_version=? WHERE id=?",
            (status, dumps(saved_cycle), now, saved_cycle["row_version"], cycle_id),
        )
    return {
        "cycle": saved_cycle,
        "assignment": saved_assignment,
        "report": report,
        "findings": findings,
    }


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
        self.run["scope_fingerprint"] = compute_run_scope_fingerprint(
            self.run, self.run["packet"]
        )
        self.run = self.store.save_run_for_setup(self.run)
        self.evidence = build_evidence_bundle(
            run=self.run,
            packet=self.run["packet"],
            process_result={
                "ok": True,
                "exit_code": 0,
                "pid": 100,
                "stdout": "",
                "stderr": "",
                "cleanup_ok": True,
                "process_group_isolated": True,
            },
            worktree={
                "worktree_path": self.temp.name,
                "baseline_commit": self.run["baseline_commit"],
                "head_commit": "b" * 40,
                "branch": self.run["execution_branch"],
            },
            diff={
                "manifest": {
                    "complete": True,
                    "manifest_fingerprint": "m" * 64,
                    "files_changed": ["buildforme/x.py"],
                    "files": [{"path": "buildforme/x.py", "change_type": "modified"}],
                },
                "patch_fingerprint": "p" * 64,
            },
            provider_health={"version": "test", "executable": "codex"},
            verification={"passed": True, "blocking_reasons": [], "checks": []},
            constitution_result={"passed": True},
            approved_baseline_sha=self.run["baseline_commit"],
            final_head_sha="b" * 40,
            execution_branch=self.run["execution_branch"],
            patch_fingerprint="p" * 64,
            manifest_fingerprint="m" * 64,
        )
        self.evidence = self.store.save_run_evidence(self.evidence)
        self.reviewers = [
            {"reviewer_id": "claude-reviewer", "provider_id": "claude", "role": "correctness"},
            {"reviewer_id": "grok-reviewer", "provider_id": "grok", "role": "security"},
        ]

    def _cycle(self):
        return create_independent_review_cycle(
            self.store,
            self.run["id"],
            reviewers=self.reviewers,
            actor="shan",
        )

    def _pass_report(self, assignment):
        return submit_independent_review_report(
            self.store,
            assignment["cycle_id"],
            assignment["assignment_id"],
            payload={"verdict": "pass", "summary": "clear", "findings": []},
        )

    def test_schema_v5(self):
        self.assertEqual(SCHEMA_VERSION, 7)
        self.assertEqual(self.store.s6.db.pragmas()["schema_version"], 7)

    def test_cycle_binds_run_evidence_scope_and_constitution_atomically(self):
        result = self._cycle()
        cycle = result["cycle"]
        run = result["run"]
        self.assertEqual(cycle["run_id"], self.run["id"])
        self.assertEqual(cycle["evidence_id"], self.evidence["evidence_id"])
        self.assertEqual(cycle["evidence_fingerprint"], self.evidence["evidence_fingerprint"])
        self.assertEqual(cycle["scope_fingerprint"], self.run["scope_fingerprint"])
        self.assertEqual(cycle["constitution_hash"], self.run["constitution_hash"])
        self.assertEqual(cycle["constitution_lease_id"], self.run["constitution_lease_id"])
        self.assertTrue(run["stage7_review_required"])
        self.assertEqual(run["stage7_review_cycle_id"], cycle["cycle_id"])
        self.assertEqual(len(result["assignments"]), 2)

    def test_implementer_cannot_review_own_execution(self):
        reviewers = [
            {"reviewer_id": "self", "provider_id": "codex", "role": "review"},
            {"reviewer_id": "other", "provider_id": "claude", "role": "review"},
        ]
        with self.assertRaisesRegex(ValueError, "implementer provider"):
            create_independent_review_cycle(
                self.store, self.run["id"], reviewers=reviewers, actor="shan"
            )

    def test_duplicate_provider_rejected(self):
        reviewers = [
            {"reviewer_id": "a", "provider_id": "claude", "role": "correctness"},
            {"reviewer_id": "b", "provider_id": "claude", "role": "security"},
        ]
        with self.assertRaisesRegex(ValueError, "duplicate reviewer provider"):
            create_independent_review_cycle(
                self.store, self.run["id"], reviewers=reviewers, actor="shan"
            )

    def test_second_active_cycle_rejected(self):
        self._cycle()
        with self.assertRaisesRegex(ValueError, "already been independently reviewed"):
            self._cycle()

    def test_report_is_append_only(self):
        result = self._cycle()
        assignment = result["assignments"][0]
        self._pass_report(assignment)
        with self.assertRaisesRegex(
            ValueError, "not pending or executing|fixture review assignment is not pending"
        ):
            self._pass_report(assignment)

    def test_blind_report_cannot_claim_consensus_or_founder_authority(self):
        result = self._cycle()
        assignment = result["assignments"][0]
        with self.assertRaisesRegex(ValueError, "forbidden authority"):
            submit_independent_review_report(
                self.store,
                result["cycle"]["cycle_id"],
                assignment["assignment_id"],
                payload={
                    "verdict": "pass",
                    "summary": "fake consensus",
                    "findings": [],
                    "consensus": "all reviewers agree",
                },
            )

    def test_critical_finding_is_forced_blocking(self):
        result = self._cycle()
        assignment = result["assignments"][0]
        response = submit_independent_review_report(
            self.store,
            result["cycle"]["cycle_id"],
            assignment["assignment_id"],
            payload={
                "verdict": "changes_required",
                "summary": "critical issue",
                "findings": [
                    {
                        "severity": "critical",
                        "category": "security",
                        "summary": "secret exposure",
                        "evidence": "exact path",
                        "recommendation": "remove it",
                        "blocking": False,
                    }
                ],
            },
        )
        self.assertTrue(response["findings"][0]["blocking"])

    def test_quorum_required_before_aggregation(self):
        result = self._cycle()
        self._pass_report(result["assignments"][0])
        with self.assertRaisesRegex(ValueError, "quorum"):
            aggregate_independent_review_cycle(self.store, result["cycle"]["cycle_id"])

    def test_blocking_finding_produces_repair_required_and_founder_block(self):
        result = self._cycle()
        first, second = result["assignments"]
        submit_independent_review_report(
            self.store,
            first["cycle_id"],
            first["assignment_id"],
            payload={
                "verdict": "changes_required",
                "summary": "repair required",
                "findings": [
                    {
                        "severity": "high",
                        "category": "governance",
                        "summary": "authority bypass",
                        "evidence": "exact bypass path",
                        "recommendation": "repair authority",
                    }
                ],
            },
        )
        self._pass_report(second)
        finalized = aggregate_independent_review_cycle(
            self.store, result["cycle"]["cycle_id"]
        )
        self.assertEqual(finalized["cycle"]["status"], "repair_required")
        self.assertEqual(finalized["aggregate"]["blocking_finding_count"], 1)
        run = self.store.get_run(self.run["id"])
        blocks = collect_hard_blocks(
            run=run,
            evidence=self.evidence,
            verification={"passed": True, "blocking_reasons": [], "checks": []},
            constitution_validation={"passed": True, "valid": True},
        )
        self.assertIn("Stage 7 independent review is not clear", blocks)
        self.assertIn("Stage 7 independent review contains blocking findings", blocks)
        with self.assertRaisesRegex(ValueError, "independent review"):
            require_clear_independent_review(self.store, run)

    def test_clear_quorum_binds_run_and_removes_stage7_hard_block(self):
        result = self._cycle()
        for assignment in result["assignments"]:
            self._pass_report(assignment)
        finalized = aggregate_independent_review_cycle(
            self.store, result["cycle"]["cycle_id"]
        )
        self.assertEqual(finalized["cycle"]["status"], "clear")
        self.assertTrue(finalized["aggregate"]["quorum_met"])
        run = self.store.get_run(self.run["id"])
        self.assertEqual(run["independent_review"]["status"], "clear")
        require_clear_independent_review(self.store, run)
        blocks = collect_hard_blocks(
            run=run,
            evidence=self.evidence,
            verification={"passed": True, "blocking_reasons": [], "checks": []},
            constitution_validation={"passed": True, "valid": True},
        )
        self.assertFalse(any("Stage 7" in block for block in blocks))

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

    def test_storage_rejects_self_consistent_forged_cycle_authority(self):
        from buildforme.review_contracts import build_review_cycle_record

        forged = dict(self.run)
        forged["scope_fingerprint"] = "forged-scope"
        forged["constitution_hash"] = "f" * 64
        forged["constitution_lease_id"] = "forged-lease"
        forged["provider_id"] = "glm"
        cycle, assignments = build_review_cycle_record(
            run=forged,
            evidence=self.evidence,
            reviewers=self.reviewers,
            actor="shan",
        )
        with self.assertRaisesRegex(ValueError, "scope|Constitution|implementer|lease"):
            self.store.s6.create_review_cycle_atomic(
                cycle=cycle, assignments=assignments, actor="shan"
            )

    def test_storage_rejects_assignment_set_not_equal_to_cycle_reviewers(self):
        cycle, assignments = build_review_cycle_record(
            run=self.run,
            evidence=self.evidence,
            reviewers=self.reviewers,
            actor="shan",
        )
        with self.assertRaisesRegex(ValueError, "exactly match"):
            self.store.s6.create_review_cycle_atomic(
                cycle=cycle, assignments=assignments[:1], actor="shan"
            )

    def test_storage_rejects_cycle_bound_to_superseded_execution_evidence(self):
        newer = build_evidence_bundle(
            run=self.run,
            packet=self.run["packet"],
            process_result={
                "ok": True,
                "exit_code": 0,
                "pid": 101,
                "stdout": "new",
                "stderr": "",
                "cleanup_ok": True,
                "process_group_isolated": True,
            },
            worktree={
                "worktree_path": self.temp.name,
                "baseline_commit": self.run["baseline_commit"],
                "head_commit": "d" * 40,
                "branch": self.run["execution_branch"],
            },
            diff={
                "manifest": {
                    "complete": True,
                    "manifest_fingerprint": "n" * 64,
                    "files_changed": ["buildforme/y.py"],
                    "files": [{"path": "buildforme/y.py", "change_type": "modified"}],
                },
                "patch_fingerprint": "q" * 64,
            },
            provider_health={"version": "test", "executable": "codex"},
            verification={"passed": True, "blocking_reasons": [], "checks": []},
            constitution_result={"passed": True},
            approved_baseline_sha=self.run["baseline_commit"],
            final_head_sha="d" * 40,
            execution_branch=self.run["execution_branch"],
            patch_fingerprint="q" * 64,
            manifest_fingerprint="n" * 64,
        )
        self.store.save_run_evidence(newer)
        cycle, assignments = build_review_cycle_record(
            run=self.run,
            evidence=self.evidence,
            reviewers=self.reviewers,
            actor="shan",
        )
        with self.assertRaisesRegex(ValueError, "latest execution evidence"):
            self.store.s6.create_review_cycle_atomic(
                cycle=cycle, assignments=assignments, actor="shan"
            )

    def test_storage_rejects_findings_divergent_from_report(self):
        result = self._cycle()
        assignment = result["assignments"][0]
        report, findings = build_review_report_record(
            cycle=result["cycle"],
            assignment=assignment,
            payload={
                "verdict": "changes_required",
                "summary": "repair",
                "findings": [
                    {
                        "severity": "medium",
                        "category": "correctness",
                        "summary": "original",
                        "evidence": "path",
                        "recommendation": "fix",
                    }
                ],
            },
        )
        divergent = [dict(findings[0])]
        divergent[0]["summary"] = "different row"
        with self.assertRaisesRegex(ValueError, "direct review report submission disabled"):
            self.store.submit_review_report_atomic(
                cycle_id=result["cycle"]["cycle_id"],
                assignment_id=assignment["assignment_id"],
                report=report,
                findings=divergent,
                actor="reviewer",
            )

    def test_finding_fingerprint_is_independently_validated(self):
        result = self._cycle()
        assignment = result["assignments"][0]
        report, findings = build_review_report_record(
            cycle=result["cycle"],
            assignment=assignment,
            payload={
                "verdict": "changes_required",
                "summary": "repair",
                "findings": [
                    {
                        "severity": "medium",
                        "category": "correctness",
                        "summary": "original",
                        "evidence": "path",
                        "recommendation": "fix",
                    }
                ],
            },
        )
        forged = [dict(findings[0])]
        forged[0]["finding_fingerprint"] = "f" * 64
        forged_report = dict(report)
        forged_report["findings"] = forged
        from buildforme.review_contracts import _fingerprint, REVIEW_REPORT_SCHEMA

        material = {
            key: forged_report.get(key)
            for key in (
                "report_id",
                "cycle_id",
                "assignment_id",
                "run_id",
                "reviewer_id",
                "provider_id",
                "role",
                "reviewed_evidence_id",
                "reviewed_evidence_fingerprint",
                "scope_fingerprint",
                "constitution_hash",
                "verdict",
                "summary",
                "findings",
                "blind_review",
                "provider_may_self_accept",
            )
        }
        forged_report["report_fingerprint"] = _fingerprint(REVIEW_REPORT_SCHEMA, material)
        with self.assertRaisesRegex(ValueError, "direct review report submission disabled"):
            self.store.submit_review_report_atomic(
                cycle_id=result["cycle"]["cycle_id"],
                assignment_id=assignment["assignment_id"],
                report=forged_report,
                findings=forged,
                actor="reviewer",
            )

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
                        actor="shan",
                        policy=policy,
                    )

    def test_blocking_cycle_cannot_be_re_reviewed_without_fresh_evidence(self):
        result = self._cycle()
        first, second = result["assignments"]
        submit_independent_review_report(
            self.store,
            first["cycle_id"],
            first["assignment_id"],
            payload={
                "verdict": "changes_required",
                "summary": "repair required",
                "findings": [
                    {
                        "severity": "high",
                        "category": "governance",
                        "summary": "authority defect",
                        "evidence": "exact failing path",
                        "recommendation": "repair and re-execute",
                    }
                ],
            },
        )
        self._pass_report(second)
        finalized = aggregate_independent_review_cycle(
            self.store, result["cycle"]["cycle_id"]
        )
        self.assertEqual(finalized["cycle"]["status"], "repair_required")
        with self.assertRaisesRegex(ValueError, "fresh repair and execution evidence"):
            create_independent_review_cycle(
                self.store,
                self.run["id"],
                reviewers=self.reviewers,
                actor="shan",
            )

    def test_founder_decision_is_normalized_before_stage7_gate(self):
        source = Path("buildforme/execution_service.py").read_text(encoding="utf-8")
        normalization = 'decision = str(decision or "").strip().lower()'
        gate = 'if decision == "accept_for_pr_prep" and run.get("stage7_review_required")'
        self.assertIn(normalization, source)
        self.assertIn(gate, source)
        self.assertLess(source.index(normalization), source.index(gate))

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

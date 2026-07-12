from __future__ import annotations

import ast
import unittest
from pathlib import Path

from buildforme.stage7_smoke import evaluate_stage7_repair_smoke


class Stage7RepairSmokeTests(unittest.TestCase):
    def _attempt(self, provider, fingerprint):
        return {
            "provider_id": provider,
            "status": "succeeded",
            "report_fingerprint": fingerprint,
            "process_started": True,
            "auth_probe_verified": True,
            "worktree_unchanged": True,
            "post_snapshot_proven": True,
            "process": {"pid": 10, "exit_code": 0, "cleanup_ok": True},
        }

    def _observed(self):
        return {
            "controlled_source_fixture": True,
            "controlled_repair_execution_fixture": True,
            "initial_review_attempts": [self._attempt("codex", "i1"), self._attempt("claude", "i2")],
            "initial_report_fingerprints": ["i1", "i2"],
            "initial_aggregate_report_fingerprints": ["i1", "i2"],
            "initial_aggregate_status": "repair_required",
            "blocking_finding_count": 1,
            "initial_cycle_id": "rc-initial",
            "source_evidence_id": "ev-source",
            "repair_packet_id": "rpair-1",
            "repair_packet_source_cycle_id": "rc-initial",
            "repair_packet_source_evidence_id": "ev-source",
            "repair_admission_id": "radm-1",
            "repair_admission_packet_id": "rpair-1",
            "repair_child_run_id": "run-child",
            "repair_admission_child_run_id": "run-child",
            "seed_commit": "abc",
            "seed_fingerprint": "seed-fp",
            "child_execution_seed_commit": "abc",
            "child_original_baseline": "base",
            "source_original_baseline": "base",
            "fresh_evidence_id": "ev-fresh",
            "repair_verification_passed": True,
            "repair_review_link_packet_id": "rpair-1",
            "repair_review_link_evidence_id": "ev-fresh",
            "repair_review_link_cycle_id": "rc-final",
            "final_cycle_id": "rc-final",
            "final_review_attempts": [self._attempt("codex", "f1"), self._attempt("claude", "f2")],
            "final_report_fingerprints": ["f1", "f2"],
            "final_aggregate_report_fingerprints": ["f1", "f2"],
            "final_aggregate_status": "clear",
            "repair_provider_id": "glm",
            "source_head_before": "head",
            "source_head_after": "head",
            "source_branch_before": "feature/source",
            "source_branch_after": "feature/source",
            "source_patch_before": "patch-source",
            "source_patch_after": "patch-source",
            "repair_patch_before_review": "patch-fixed",
            "repair_patch_after_review": "patch-fixed",
            "merge_commit_count": 0,
        }

    def test_acceptance_requires_both_real_cycles_and_bound_repair(self):
        result = evaluate_stage7_repair_smoke(self._observed())
        self.assertTrue(result["passed"], result)
        observed = self._observed()
        observed["final_review_attempts"] = [self._attempt("codex", "f1")]
        result = evaluate_stage7_repair_smoke(observed)
        self.assertFalse(result["passed"])
        self.assertIn("final_real_codex_claude_review", result["failed_checks"])

    def test_script_uses_real_review_execution_and_no_direct_report_submission(self):
        source = Path("scripts/stage7_real_repair_loop_smoke.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("execute_independent_review_assignment"), 2)
        self.assertIn("create_governed_repair_packet", source)
        self.assertIn("admit_governed_repair_run", source)
        self.assertIn("create_repair_review_cycle", source)
        self.assertIn("STAGE7_REPAIR_SMOKE_ACCEPTANCE_JSON", source)
        tree = ast.parse(source)
        forbidden = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "submit_review_report_atomic"
        ]
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()

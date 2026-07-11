from __future__ import annotations

import ast
import unittest
from pathlib import Path

from buildforme.stage7_full_acceptance import evaluate_stage7_full_acceptance


class Stage7FullAcceptanceTests(unittest.TestCase):
    def _observed(self):
        return {
            "review_exit_code": 0,
            "review_smoke": {"schema": "review", "passed": True, "failed_checks": []},
            "review_merge_marker": "MERGE no",
            "repair_exit_code": 0,
            "repair_smoke": {"schema": "repair", "passed": True, "failed_checks": []},
            "repair_merge_marker": "MERGE no",
            "source_head_before": "a",
            "source_head_after": "a",
            "source_branch_before": "feature/stage7",
            "source_branch_after": "feature/stage7",
            "source_status_before": "",
            "source_status_after": "",
        }

    def test_both_smokes_and_source_identity_are_required(self):
        result = evaluate_stage7_full_acceptance(self._observed())
        self.assertTrue(result["passed"], result)
        observed = self._observed()
        observed["repair_smoke"] = {"schema": "repair", "passed": False, "failed_checks": ["x"]}
        result = evaluate_stage7_full_acceptance(observed)
        self.assertFalse(result["passed"])
        self.assertIn("repair_smoke_passed", result["failed_checks"])

    def test_wrapper_runs_both_fixed_smoke_scripts_and_no_merge(self):
        source = Path("scripts/stage7_full_acceptance.py").read_text(encoding="utf-8")
        self.assertIn("stage7_real_two_provider_smoke.py", source)
        self.assertIn("stage7_real_repair_loop_smoke.py", source)
        self.assertIn("STAGE7_FULL_ACCEPTANCE_JSON", source)
        self.assertIn('print("MERGE no")', source)
        tree = ast.parse(source)
        shell_true = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.keyword)
            and node.arg == "shell"
            and isinstance(node.value, ast.Constant)
            and node.value.value is True
        ]
        self.assertEqual(shell_true, [])


if __name__ == "__main__":
    unittest.main()

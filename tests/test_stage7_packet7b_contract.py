"""Permanent source contracts for accepted Packet 7B reviewer execution authority."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


class Stage7Packet7BContractTests(unittest.TestCase):
    def test_runtime_uses_atomic_claim_and_no_direct_report_authority(self):
        source = Path("buildforme/review_execution.py").read_text(encoding="utf-8")
        self.assertIn("claim_review_assignment_execution_atomic", source)
        self.assertIn("post_snapshot_proven", source)
        service = Path("buildforme/review_service.py").read_text(encoding="utf-8")
        self.assertIn("direct review report submission disabled", service)

    def test_success_validation_binds_code_owned_contract_and_auth(self):
        source = Path("buildforme/review_execution.py").read_text(encoding="utf-8")
        for phrase in (
            "successful review execution command contract mismatch",
            "argv does not match approved contract",
            "requires verified authentication probe",
            "requires proven post-review snapshot",
        ):
            self.assertIn(phrase, source)

    def test_no_runtime_setup_review_submission_api(self):
        forbidden = []
        for path in Path("buildforme").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == "submit_review_report_for_setup":
                        forbidden.append((str(path), node.lineno))
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()

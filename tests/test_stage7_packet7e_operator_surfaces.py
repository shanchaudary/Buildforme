from __future__ import annotations

import ast
import unittest
from pathlib import Path

from buildforme.cli import build_parser
from buildforme.stage7_smoke import evaluate_stage7_smoke


class Stage7OperatorSurfaceTests(unittest.TestCase):
    def test_server_repair_routes_are_founder_gated_and_fixed(self):
        source = Path("buildforme/server.py").read_text(encoding="utf-8")
        self.assertIn("_stage7_repair_action", source)
        self.assertIn("_require_founder_mutation(payload)", source)
        for route in (
            "/repair-packet",
            "/admit",
            "/review-cycle",
            "/execute",
        ):
            self.assertIn(route, source)
        for forbidden in ("argv", "repo_root", "reviewers", "seed_commit", "scope_fingerprint"):
            self.assertIn(f'"{forbidden}"', source)

    def test_cli_exposes_repair_workflow_without_authority_overrides(self):
        parser = build_parser()
        commands = (
            ["repair-list"],
            ["repair-show", "rpair-test"],
            ["repair-create", "rc-test", "--provider", "glm"],
            ["repair-admit", "rpair-test"],
            ["repair-review-cycle", "rpair-test"],
            ["repair-execute", "rpair-test"],
        )
        for argv in commands:
            args = parser.parse_args(argv)
            self.assertTrue(callable(args.func))
        source = Path("buildforme/cli.py").read_text(encoding="utf-8")
        for unsafe in ("--repo-root", "--command", "--seed-commit", "--reviewers"):
            self.assertNotIn(unsafe, source)

    def test_browser_has_stage7_status_and_founder_inputs(self):
        html = Path("public/index.html").read_text(encoding="utf-8")
        js = Path("public/app.js").read_text(encoding="utf-8")
        for token in (
            'data-view="repairs"',
            'data-view-panel="repairs"',
            'id="rp-founder-token"',
            'id="rp-csrf-token"',
            'id="rp-list"',
        ):
            self.assertIn(token, html)
        self.assertIn("repairMutationBody", js)
        self.assertIn("/api/repair-packets", js)
        self.assertNotIn("localStorage.setItem", js)

    def test_smoke_acceptance_requires_real_two_provider_proof(self):
        attempt = lambda provider: {
            "provider_id": provider,
            "status": "succeeded",
            "process_started": True,
            "auth_probe_verified": True,
            "post_snapshot_proven": True,
            "worktree_unchanged": True,
            "process": {"pid": 10, "exit_code": 0, "cleanup_ok": True},
        }
        observed = {
            "controlled_implementation_fixture": True,
            "review_execution_attempts": [attempt("codex"), attempt("claude")],
            "distinct_provider_count": 2,
            "provider_ids": ["codex", "claude"],
            "aggregate_status": "clear",
            "verification_passed": True,
            "source_head_before": "a",
            "source_head_after": "a",
            "source_branch_before": "feature/x",
            "source_branch_after": "feature/x",
            "source_patch_before": "p",
            "source_patch_after": "p",
            "merge_performed": False,
            "direct_report_submission_used": False,
        }
        result = evaluate_stage7_smoke(observed)
        self.assertTrue(result["passed"], result)
        observed["review_execution_attempts"] = [attempt("codex")]
        result = evaluate_stage7_smoke(observed)
        self.assertFalse(result["passed"])
        self.assertIn("codex_and_claude_succeeded", result["failed_checks"])

    def test_smoke_script_discloses_controlled_fixture_and_no_merge(self):
        source = Path("scripts/stage7_real_two_provider_smoke.py").read_text(encoding="utf-8")
        self.assertIn('"controlled_implementation_fixture": True', source)
        self.assertIn("execute_independent_review_assignment", source)
        self.assertIn("STAGE7_SMOKE_ACCEPTANCE_JSON", source)
        self.assertIn('print("MERGE no")', source)
        tree = ast.parse(source)
        direct_submit = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "submit_review_report_atomic"
        ]
        self.assertEqual(direct_submit, [])


if __name__ == "__main__":
    unittest.main()

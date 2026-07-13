from __future__ import annotations

import ast
import copy
import unittest
from pathlib import Path

from buildforme.cli import build_parser
from buildforme.stage7_smoke import evaluate_stage7_smoke


class Stage7OperatorSurfaceTests(unittest.TestCase):
    def test_server_repair_routes_are_founder_gated_and_fixed(self):
        source = Path("buildforme/server.py").read_text(encoding="utf-8")
        self.assertIn("_stage7_repair_action", source)
        self.assertIn("_require_founder_mutation(payload)", source)
        self.assertIn("HTTPStatus.FORBIDDEN", source)
        self.assertIn('actor = str(auth.get("actor") or "shan")', source)
        self.assertIn("unknown fields", source)
        for route in (
            "/repair-packet",
            "/admit",
            "/review-cycle",
            "/execute",
        ):
            self.assertIn(route, source)
        self.assertIn("unknown = sorted(set(payload) - allowed)", source)
        self.assertIn("repair action accepts only storage-bounded identifiers", source)
        self.assertIn('allowed = {"founder_token", "csrf_token"}', source)

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
            "report_fingerprint": "r1" if provider == "codex" else "r2",
            "status": "succeeded",
            "process_started": True,
            "auth_probe_verified": True,
            "post_snapshot_proven": True,
            "worktree_unchanged": True,
            "process": {"pid": 10, "exit_code": 0, "cleanup_ok": True},
        }
        observed = {
            "controlled_implementation_fixture": True,
            "implementer_provider_id": "glm",
            "review_execution_attempts": [attempt("codex"), attempt("claude")],
            "persisted_report_count": 2,
            "persisted_report_fingerprints": ["r1", "r2"],
            "aggregate_report_fingerprints": ["r1", "r2"],
            "review_events": [
                {
                    "event_type": "review_cycle_created",
                    "actor": "system",
                    "metadata": {},
                },
                {
                    "event_type": "review_report_submitted",
                    "actor": "reviewer",
                    "metadata": {"assignment_id": "ra-1", "report_id": "rr-1"},
                },
                {
                    "event_type": "review_report_submitted",
                    "actor": "reviewer",
                    "metadata": {"assignment_id": "ra-2", "report_id": "rr-2"},
                },
            ],
            "run_review_events": [
                {
                    "event_type": "stage7_review_cycle_created",
                    "actor": "system",
                    "metadata": {},
                },
                {
                    "event_type": "stage7_review_report_submitted",
                    "actor": "reviewer",
                    "metadata": {"assignment_id": "ra-1", "report_id": "rr-1"},
                },
                {
                    "event_type": "stage7_review_report_submitted",
                    "actor": "reviewer",
                    "metadata": {"assignment_id": "ra-2", "report_id": "rr-2"},
                },
            ],
            "cycle_id": "rc-1",
            "cycle_evidence_id": "ev-1",
            "cycle_evidence_fingerprint": "efp-1",
            "expected_evidence_id": "ev-1",
            "expected_evidence_fingerprint": "efp-1",
            "run_review_cycle_id": "rc-1",
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
            "merge_commit_count": 0,
        }
        result = evaluate_stage7_smoke(observed)
        self.assertTrue(result["passed"], result)

        for actor in (
            "codex-real-reviewer",
            "claude-real-reviewer",
            "arbitrary-reviewer",
        ):
            with self.subTest(actor=actor):
                invalid = copy.deepcopy(observed)
                invalid["review_events"][1]["actor"] = actor
                result = evaluate_stage7_smoke(invalid)
                self.assertFalse(result["passed"])
                self.assertIn(
                    "persisted_review_event_actors_canonical", result["failed_checks"]
                )

        missing = copy.deepcopy(observed)
        missing["review_events"] = []
        result = evaluate_stage7_smoke(missing)
        self.assertFalse(result["passed"])
        self.assertIn("persisted_review_event_actors_canonical", result["failed_checks"])

        wrong_authority = copy.deepcopy(observed)
        wrong_authority["run_review_events"][1]["actor"] = "system"
        result = evaluate_stage7_smoke(wrong_authority)
        self.assertFalse(result["passed"])
        self.assertIn(
            "persisted_review_report_submission_actor", result["failed_checks"]
        )

        contradictory = copy.deepcopy(observed)
        contradictory["run_review_events"][1]["metadata"]["report_id"] = "rr-other"
        result = evaluate_stage7_smoke(contradictory)
        self.assertFalse(result["passed"])
        self.assertIn(
            "persisted_review_run_event_pair_consistent", result["failed_checks"]
        )

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

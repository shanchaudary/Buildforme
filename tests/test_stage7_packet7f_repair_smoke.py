from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buildforme.governance import ALLOWED_ACTORS, compute_run_scope_fingerprint, validate_actor
from buildforme.review_service import create_independent_review_cycle
from buildforme.stage7_smoke import evaluate_stage7_repair_smoke
from scripts import stage7_full_acceptance as full_acceptance
from scripts import stage7_real_repair_loop_smoke as repair_smoke
from scripts import stage7_real_two_provider_smoke as two_provider_smoke


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

    def test_initial_source_fixture_passes_tests_but_retains_authorization_bypass(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repair_smoke.write_source_fixture(root)
            verification = repair_smoke.run_tests(root)
        self.assertTrue(verification["passed"], verification)

        namespace = {}
        exec(repair_smoke.SOURCE_AUTH_IMPLEMENTATION, namespace)
        self.assertTrue(namespace["is_authorized"]("admin"))
        self.assertTrue(namespace["is_authorized"]("guest"))

        tree = ast.parse(repair_smoke.SOURCE_TEST_SUITE)
        test_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("test_admin_allowed", test_names)
        self.assertNotIn("test_guest_rejected", test_names)
        self.assertIn("guest is rejected", repair_smoke.SOURCE_ACCEPTANCE_CRITERIA)

    def test_repaired_fixture_has_complete_passing_authorization_tests(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repair_smoke.write_repaired_fixture(root)
            verification = repair_smoke.run_tests(root)
        self.assertTrue(verification["passed"], verification)
        tree = ast.parse(repair_smoke.REPAIRED_TEST_SUITE)
        test_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("test_admin_allowed", test_names)
        self.assertIn("test_guest_rejected", test_names)

    def test_all_harness_actor_arguments_are_canonical(self):
        modules = {
            "scripts/stage7_full_acceptance.py": full_acceptance,
            "scripts/stage7_real_two_provider_smoke.py": two_provider_smoke,
            "scripts/stage7_real_repair_loop_smoke.py": repair_smoke,
        }
        for path, module in modules.items():
            source = Path(path).read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        if keyword.arg != "actor":
                            continue
                        if isinstance(keyword.value, ast.Constant):
                            actor = keyword.value.value
                        elif isinstance(keyword.value, ast.Name):
                            actor = getattr(module, keyword.value.id)
                        else:
                            self.fail(f"{path}:{node.lineno} has non-canonical actor authority")
                        self.assertIn(actor, ALLOWED_ACTORS, f"{path}:{node.lineno}")
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if not isinstance(key, ast.Constant) or key.value != "constitution_ack_actor":
                            continue
                        actor = value.value if isinstance(value, ast.Constant) else getattr(module, value.id)
                        self.assertIn(actor, ALLOWED_ACTORS, f"{path}:{node.lineno}")

        for actor in two_provider_smoke.HARNESS_ACTORS | repair_smoke.HARNESS_ACTORS:
            self.assertEqual(validate_actor(actor), actor)

    def test_failed_verification_evidence_remains_rejected_for_review(self):
        packet = {"id": "pkt-failed-verification"}
        run = {
            "id": "run-failed-verification",
            "execution_mode": "live_supervised",
            "status": "needs_review",
            "packet": packet,
            "constitution_hash": "constitution-hash",
        }
        run["scope_fingerprint"] = compute_run_scope_fingerprint(run, packet)
        evidence = {
            "run_id": run["id"],
            "constitution": {"hash": run["constitution_hash"]},
            "verification": {"passed": False},
        }
        store = mock.Mock()
        store.get_run.return_value = run
        store.get_latest_execution_evidence.return_value = evidence
        with mock.patch(
            "buildforme.review_service.validate_evidence_for_storage",
            return_value=[],
        ):
            with self.assertRaisesRegex(
                ValueError,
                "deterministic verification must pass before independent review",
            ):
                create_independent_review_cycle(
                    store,
                    run["id"],
                    reviewers=[],
                    actor=two_provider_smoke.AUTOMATION_ACTOR,
                )

    def test_smokes_contain_no_synthetic_reports_or_aggregates(self):
        for path in (
            "scripts/stage7_real_two_provider_smoke.py",
            "scripts/stage7_real_repair_loop_smoke.py",
        ):
            source = Path(path).read_text(encoding="utf-8")
            tree = ast.parse(source)
            direct_submit = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and (
                    (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "submit_review_report_atomic"
                    )
                    or (
                        isinstance(node.func, ast.Name)
                        and node.func.id == "submit_review_report_atomic"
                    )
                )
            ]
            self.assertEqual(direct_submit, [], path)

            synthetic = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                pairs = {
                    key.value: value
                    for key, value in zip(node.keys, node.values)
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
                report_literals = [
                    pairs.get(key)
                    for key in ("verdict", "findings", "blocking")
                    if key in pairs
                ]
                if any(
                    isinstance(value, (ast.Constant, ast.List, ast.Dict, ast.Tuple))
                    for value in report_literals
                ):
                    synthetic.append(node.lineno)
                status = pairs.get("status")
                if isinstance(status, ast.Constant) and status.value in {"clear", "repair_required"}:
                    synthetic.append(node.lineno)
            self.assertEqual(synthetic, [], path)
            self.assertIn('print("MERGE no")', source)


if __name__ == "__main__":
    unittest.main()

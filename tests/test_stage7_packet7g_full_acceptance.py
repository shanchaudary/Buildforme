from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from buildforme.stage7_full_acceptance import evaluate_stage7_full_acceptance
from scripts.stage7_full_acceptance import ROOT, build_child_command, run_smoke


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

        for field, check in (
            ("source_head_after", "source_head_unchanged"),
            ("source_branch_after", "source_branch_unchanged"),
            ("source_status_after", "source_status_unchanged"),
        ):
            with self.subTest(field=field):
                changed = self._observed()
                changed[field] = "changed"
                result = evaluate_stage7_full_acceptance(changed)
                self.assertFalse(result["passed"])
                self.assertIn(check, result["failed_checks"])

        review_failed = self._observed()
        review_failed["review_smoke"] = {
            "schema": "review",
            "passed": False,
            "failed_checks": ["x"],
        }
        result = evaluate_stage7_full_acceptance(review_failed)
        self.assertFalse(result["passed"])
        self.assertIn("review_smoke_passed", result["failed_checks"])

    def test_wrapper_invokes_children_as_modules_without_pythonpath(self):
        payload = {"schema": "test", "passed": True, "failed_checks": []}
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="TEST_JSON " + json.dumps(payload) + "\nMERGE no\n",
            stderr="",
        )
        with mock.patch.dict(os.environ, {"PYTHONPATH": "C:\\untrusted-import-root"}):
            with mock.patch(
                "scripts.stage7_full_acceptance.subprocess.run",
                return_value=completed,
            ) as run:
                result = run_smoke("stage7_real_two_provider_smoke.py", "TEST_JSON")

        command = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertEqual(
            command,
            [sys.executable, "-m", "scripts.stage7_real_two_provider_smoke"],
        )
        self.assertEqual(kwargs["cwd"], ROOT)
        self.assertNotIn("PYTHONPATH", kwargs["env"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["acceptance"], payload)
        self.assertEqual(result["merge_marker"], "MERGE no")

    def test_child_modules_import_from_repository_root_without_pythonpath(self):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        for module in (
            "scripts.stage7_real_two_provider_smoke",
            "scripts.stage7_real_repair_loop_smoke",
        ):
            with self.subTest(module=module):
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        f"import {module}; print('STAGE7_CHILD_IMPORT_OK')",
                    ],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), "STAGE7_CHILD_IMPORT_OK")

    def test_child_command_rejects_unapproved_script_names(self):
        with self.assertRaisesRegex(ValueError, "unsupported Stage 7 child smoke"):
            build_child_command("arbitrary.py")

    def test_wrapper_runs_both_fixed_smoke_scripts_and_no_merge(self):
        source = Path("scripts/stage7_full_acceptance.py").read_text(encoding="utf-8")
        self.assertIn("stage7_real_two_provider_smoke.py", source)
        self.assertIn("stage7_real_repair_loop_smoke.py", source)
        self.assertIn("STAGE7_FULL_ACCEPTANCE_JSON", source)
        self.assertIn('print("MERGE no")', source)
        self.assertIn('"-m", f"scripts.{Path(script_name).stem}"', source)
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

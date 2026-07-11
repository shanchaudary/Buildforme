from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

module = r'''"""Combined Stage 7 final acceptance over both real reviewer smoke scenarios."""

from __future__ import annotations

from typing import Any

STAGE7_FULL_ACCEPTANCE_SCHEMA = "buildforme.stage7_full_acceptance.v1"


def evaluate_stage7_full_acceptance(observed: dict[str, Any]) -> dict[str, Any]:
    review = observed.get("review_smoke") if isinstance(observed.get("review_smoke"), dict) else {}
    repair = observed.get("repair_smoke") if isinstance(observed.get("repair_smoke"), dict) else {}
    checks = {
        "review_smoke_exit_zero": observed.get("review_exit_code") == 0,
        "review_smoke_passed": review.get("passed") is True,
        "repair_smoke_exit_zero": observed.get("repair_exit_code") == 0,
        "repair_smoke_passed": repair.get("passed") is True,
        "review_smoke_no_merge": observed.get("review_merge_marker") == "MERGE no",
        "repair_smoke_no_merge": observed.get("repair_merge_marker") == "MERGE no",
        "source_head_unchanged": observed.get("source_head_before") == observed.get("source_head_after"),
        "source_branch_unchanged": observed.get("source_branch_before")
        == observed.get("source_branch_after"),
        "source_status_unchanged": observed.get("source_status_before")
        == observed.get("source_status_after"),
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    return {
        "schema": STAGE7_FULL_ACCEPTANCE_SCHEMA,
        "passed": not failed,
        "checks": checks,
        "failed_checks": failed,
        "review_smoke_schema": review.get("schema"),
        "repair_smoke_schema": repair.get("schema"),
        "review_smoke_failed_checks": list(review.get("failed_checks") or []),
        "repair_smoke_failed_checks": list(repair.get("failed_checks") or []),
        "merge_performed": False if not failed else None,
    }
'''
(ROOT / "buildforme" / "stage7_full_acceptance.py").write_text(module, encoding="utf-8")

script = r'''from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from buildforme.stage7_full_acceptance import evaluate_stage7_full_acceptance

ROOT = Path(__file__).resolve().parent.parent


def git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or f"git {' '.join(args)} failed")
    return (proc.stdout or "").strip()


def run_smoke(script_name: str, json_prefix: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script_name)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=7_200,
        check=False,
    )
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    print(f"===== {script_name} stdout =====")
    print(stdout, end="" if stdout.endswith("\n") else "\n")
    if stderr:
        print(f"===== {script_name} stderr =====")
        print(stderr, end="" if stderr.endswith("\n") else "\n")
    acceptance = None
    merge_marker = None
    for line in stdout.splitlines():
        if line.startswith(json_prefix + " "):
            acceptance = json.loads(line[len(json_prefix) + 1 :])
        if line.strip() == "MERGE no":
            merge_marker = "MERGE no"
    return {
        "exit_code": proc.returncode,
        "acceptance": acceptance or {},
        "merge_marker": merge_marker,
    }


def main() -> int:
    source_head_before = git("rev-parse", "HEAD")
    source_branch_before = git("rev-parse", "--abbrev-ref", "HEAD")
    source_status_before = git("status", "--porcelain=v1", "--untracked-files=all")

    review = run_smoke(
        "stage7_real_two_provider_smoke.py", "STAGE7_SMOKE_ACCEPTANCE_JSON"
    )
    repair = run_smoke(
        "stage7_real_repair_loop_smoke.py", "STAGE7_REPAIR_SMOKE_ACCEPTANCE_JSON"
    )

    observed = {
        "review_exit_code": review["exit_code"],
        "review_smoke": review["acceptance"],
        "review_merge_marker": review["merge_marker"],
        "repair_exit_code": repair["exit_code"],
        "repair_smoke": repair["acceptance"],
        "repair_merge_marker": repair["merge_marker"],
        "source_head_before": source_head_before,
        "source_head_after": git("rev-parse", "HEAD"),
        "source_branch_before": source_branch_before,
        "source_branch_after": git("rev-parse", "--abbrev-ref", "HEAD"),
        "source_status_before": source_status_before,
        "source_status_after": git("status", "--porcelain=v1", "--untracked-files=all"),
    }
    acceptance = evaluate_stage7_full_acceptance(observed)
    print("STAGE7_FULL_ACCEPTANCE_JSON", json.dumps(acceptance, sort_keys=True))
    print("MERGE no")
    return 0 if acceptance["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
'''
(ROOT / "scripts" / "stage7_full_acceptance.py").write_text(script, encoding="utf-8")

test = r'''from __future__ import annotations

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
'''
(ROOT / "tests" / "test_stage7_packet7g_full_acceptance.py").write_text(test, encoding="utf-8")

path = ROOT / "docs" / "STAGE_7_INDEPENDENT_MULTI_AGENT_REVIEW.md"
text = path.read_text(encoding="utf-8")
text += '''\n\n## Packet 7G — combined final acceptance\n\n- `scripts/stage7_full_acceptance.py` runs both real-reviewer smoke scenarios and parses only their machine-verifiable acceptance lines.\n- Final Stage 7 acceptance requires both child processes to exit zero, both acceptance payloads to pass, both to report `MERGE no`, and the Buildforme source HEAD, branch, and complete working-tree status to remain exactly unchanged.\n- A passing result is printed as `STAGE7_FULL_ACCEPTANCE_JSON`; any failed child check or source mutation returns a nonzero exit code.\n'''
path.write_text(text, encoding="utf-8")

print("Stage 7 Packet 7G combined acceptance applied")

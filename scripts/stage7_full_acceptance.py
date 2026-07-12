from __future__ import annotations

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

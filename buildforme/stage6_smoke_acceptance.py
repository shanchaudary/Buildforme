"""Machine-verifiable acceptance criteria for the real Stage 6 provider smoke."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from buildforme.evidence import validate_evidence_for_storage

REQUIRED_SMOKE_FILES = frozenset({"src/math_util.py", "tests/test_math_util.py", "README.md"})


def evaluate_stage6_smoke_acceptance(
    *,
    health: dict[str, Any],
    execution_result: dict[str, Any],
    final_run: dict[str, Any],
    persisted_evidence: dict[str, Any],
    decision_evidence: dict[str, Any] | None,
    repository_root: Path,
    original_head: str,
    original_branch: str,
) -> dict[str, Any]:
    process = execution_result.get("process") if isinstance(execution_result.get("process"), dict) else {}
    verification = execution_result.get("verification") if isinstance(execution_result.get("verification"), dict) else {}
    review = execution_result.get("review") if isinstance(execution_result.get("review"), dict) else {}
    evidence = execution_result.get("evidence") if isinstance(execution_result.get("evidence"), dict) else {}
    auth = health.get("auth") if isinstance(health.get("auth"), dict) else {}
    confirmation = process.get("termination_confirmation") if isinstance(process.get("termination_confirmation"), dict) else {}
    files = set(str(path) for path in (evidence.get("files_changed") or []))
    required_present = REQUIRED_SMOKE_FILES.issubset(files)
    evidence_problems = validate_evidence_for_storage(persisted_evidence)

    import subprocess

    root_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository_root, text=True).strip()
    root_branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=repository_root, text=True).strip()
    checks = {
        "provider_live_ready": bool(health.get("live_ready")),
        "auth_probe_verified": auth.get("status") == "ready" and bool(auth.get("probe_verified")),
        "process_exit_zero": process.get("exit_code") == 0 and bool(process.get("ok")),
        "real_pid_observed": isinstance(process.get("pid"), int) and int(process.get("pid")) > 0,
        "process_tree_termination_confirmed": bool(process.get("cleanup_ok")) and bool(confirmation.get("confirmed")),
        "verification_passed": bool(verification.get("passed")),
        "review_gate_reached": bool(review.get("accept_for_pr_prep_allowed")),
        "required_files_produced": required_present,
        "evidence_persisted_exactly": bool(evidence.get("evidence_id"))
        and evidence.get("evidence_id") == persisted_evidence.get("evidence_id")
        and evidence.get("evidence_fingerprint") == persisted_evidence.get("evidence_fingerprint")
        and not evidence_problems,
        "patch_and_manifest_distinct": bool(evidence.get("patch_fingerprint"))
        and bool(evidence.get("manifest_fingerprint"))
        and evidence.get("patch_fingerprint") != evidence.get("manifest_fingerprint"),
        "final_head_valid": bool(re.fullmatch(r"[0-9a-fA-F]{40}", str(evidence.get("final_head_sha") or ""))),
        "founder_decision_completed": final_run.get("status") == "completed"
        and isinstance(decision_evidence, dict)
        and bool(decision_evidence.get("evidence_fingerprint")),
        "source_branch_unchanged": root_head == original_head and root_branch == original_branch,
        "merge_not_performed": root_head == original_head,
    }
    failed = sorted(name for name, ok in checks.items() if not ok)
    return {
        "schema": "buildforme.stage6_real_smoke_acceptance.v1",
        "passed": not failed,
        "checks": checks,
        "failed_checks": failed,
        "required_files": sorted(REQUIRED_SMOKE_FILES),
        "observed_files": sorted(files),
        "evidence_problems": evidence_problems,
        "original_head": original_head,
        "root_head_after": root_head,
        "original_branch": original_branch,
        "root_branch_after": root_branch,
    }

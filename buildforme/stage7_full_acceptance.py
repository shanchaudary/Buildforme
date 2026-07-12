"""Combined Stage 7 final acceptance over both real reviewer smoke scenarios."""

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

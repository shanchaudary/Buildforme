"""Combined Stage 7 final acceptance over both real reviewer smoke scenarios."""

from __future__ import annotations

from typing import Any

STAGE7_FULL_ACCEPTANCE_SCHEMA = "buildforme.stage7_full_acceptance.v1"
REVIEW_ACTOR_EVIDENCE_CHECKS = (
    "persisted_review_event_actors_canonical",
    "persisted_review_report_submission_actor",
    "persisted_review_run_event_pair_consistent",
)
REPAIR_ACTOR_EVIDENCE_CHECKS = (
    "initial_persisted_review_event_actors_canonical",
    "initial_persisted_review_report_submission_actor",
    "initial_persisted_review_run_event_pair_consistent",
    "final_persisted_review_event_actors_canonical",
    "final_persisted_review_report_submission_actor",
    "final_persisted_review_run_event_pair_consistent",
)


def evaluate_stage7_full_acceptance(observed: dict[str, Any]) -> dict[str, Any]:
    review = observed.get("review_smoke") if isinstance(observed.get("review_smoke"), dict) else {}
    repair = observed.get("repair_smoke") if isinstance(observed.get("repair_smoke"), dict) else {}
    review_checks = review.get("checks") if isinstance(review.get("checks"), dict) else {}
    repair_checks = repair.get("checks") if isinstance(repair.get("checks"), dict) else {}
    checks = {
        "review_smoke_exit_zero": observed.get("review_exit_code") == 0,
        "review_smoke_passed": review.get("passed") is True,
        "repair_smoke_exit_zero": observed.get("repair_exit_code") == 0,
        "repair_smoke_passed": repair.get("passed") is True,
        "review_smoke_canonical_actor_evidence": all(
            review_checks.get(name) is True for name in REVIEW_ACTOR_EVIDENCE_CHECKS
        ),
        "repair_smoke_canonical_actor_evidence": all(
            repair_checks.get(name) is True for name in REPAIR_ACTOR_EVIDENCE_CHECKS
        ),
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

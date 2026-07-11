"""Machine-verifiable Stage 7 real reviewer smoke acceptance."""

from __future__ import annotations

from typing import Any

STAGE7_SMOKE_SCHEMA = "buildforme.stage7_real_two_provider_smoke.v1"


def evaluate_stage7_smoke(observed: dict[str, Any]) -> dict[str, Any]:
    attempts = observed.get("review_execution_attempts") or []
    succeeded = [item for item in attempts if item.get("status") == "succeeded"]
    provider_ids = sorted({str(item.get("provider_id") or "") for item in succeeded})
    execution_report_fingerprints = sorted(
        str(item.get("report_fingerprint") or "")
        for item in succeeded
        if item.get("report_fingerprint")
    )
    persisted_report_fingerprints = sorted(str(item) for item in (observed.get("persisted_report_fingerprints") or []))
    aggregate_report_fingerprints = sorted(str(item) for item in (observed.get("aggregate_report_fingerprints") or []))
    checks = {
        "controlled_implementation_fixture_disclosed": observed.get("controlled_implementation_fixture") is True,
        "real_reviewer_processes_only": bool(attempts)
        and all(item.get("process_started") is True and int((item.get("process") or {}).get("pid") or 0) > 0 for item in attempts),
        "codex_and_claude_succeeded": provider_ids == ["claude", "codex"] and len(succeeded) == 2,
        "reviewers_distinct_from_implementer": str(observed.get("implementer_provider_id") or "")
        not in provider_ids,
        "auth_probes_verified": bool(attempts)
        and all(item.get("auth_probe_verified") is True for item in attempts),
        "process_exit_zero": bool(attempts)
        and all((item.get("process") or {}).get("exit_code") == 0 for item in attempts),
        "process_cleanup_confirmed": bool(attempts)
        and all((item.get("process") or {}).get("cleanup_ok") is True for item in attempts),
        "review_workspaces_unchanged": bool(attempts)
        and all(item.get("worktree_unchanged") is True and item.get("post_snapshot_proven") is True for item in attempts),
        "two_provider_quorum": observed.get("distinct_provider_count") == 2
        and sorted(observed.get("provider_ids") or []) == ["claude", "codex"],
        "two_persisted_reports": int(observed.get("persisted_report_count") or 0) == 2
        and len(persisted_report_fingerprints) == 2,
        "execution_reports_match_storage_and_aggregate": bool(execution_report_fingerprints)
        and execution_report_fingerprints
        == persisted_report_fingerprints
        == aggregate_report_fingerprints,
        "cycle_bound_to_exact_evidence": str(observed.get("cycle_evidence_id") or "")
        == str(observed.get("expected_evidence_id") or "")
        and str(observed.get("cycle_evidence_fingerprint") or "")
        == str(observed.get("expected_evidence_fingerprint") or ""),
        "run_bound_to_cycle": str(observed.get("run_review_cycle_id") or "")
        == str(observed.get("cycle_id") or ""),
        "aggregate_clear": observed.get("aggregate_status") == "clear",
        "verification_passed": observed.get("verification_passed") is True,
        "source_head_unchanged": observed.get("source_head_before") == observed.get("source_head_after"),
        "source_branch_unchanged": observed.get("source_branch_before") == observed.get("source_branch_after"),
        "source_patch_unchanged": observed.get("source_patch_before") == observed.get("source_patch_after"),
        "merge_not_performed": int(observed.get("merge_commit_count") or 0) == 0,
        "no_synthetic_report_submission": execution_report_fingerprints
        == persisted_report_fingerprints,
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    return {
        "schema": STAGE7_SMOKE_SCHEMA,
        "passed": not failed,
        "checks": checks,
        "failed_checks": failed,
        "provider_ids": provider_ids,
        "aggregate_status": observed.get("aggregate_status"),
        "controlled_implementation_fixture": bool(observed.get("controlled_implementation_fixture")),
        "note": "Reviewer processes are real. The implementation evidence is a disclosed controlled fixture, not a claimed third-provider execution.",
    }

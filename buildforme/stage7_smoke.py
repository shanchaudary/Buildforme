"""Machine-verifiable Stage 7 real reviewer smoke acceptance."""

from __future__ import annotations

from typing import Any

STAGE7_SMOKE_SCHEMA = "buildforme.stage7_real_two_provider_smoke.v1"


def evaluate_stage7_smoke(observed: dict[str, Any]) -> dict[str, Any]:
    attempts = observed.get("review_execution_attempts") or []
    provider_ids = sorted({str(item.get("provider_id") or "") for item in attempts if item.get("status") == "succeeded"})
    checks = {
        "controlled_implementation_fixture_disclosed": observed.get("controlled_implementation_fixture") is True,
        "real_reviewer_processes_only": bool(attempts)
        and all(item.get("process_started") is True and int((item.get("process") or {}).get("pid") or 0) > 0 for item in attempts),
        "codex_and_claude_succeeded": provider_ids == ["claude", "codex"],
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
        "aggregate_clear": observed.get("aggregate_status") == "clear",
        "verification_passed": observed.get("verification_passed") is True,
        "source_head_unchanged": observed.get("source_head_before") == observed.get("source_head_after"),
        "source_branch_unchanged": observed.get("source_branch_before") == observed.get("source_branch_after"),
        "source_patch_unchanged": observed.get("source_patch_before") == observed.get("source_patch_after"),
        "merge_not_performed": observed.get("merge_performed") is False,
        "no_synthetic_report_submission": observed.get("direct_report_submission_used") is False,
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

"""Machine-verifiable Stage 7 real reviewer smoke acceptance."""

from __future__ import annotations

from typing import Any

from buildforme.governance import ALLOWED_ACTORS

STAGE7_SMOKE_SCHEMA = "buildforme.stage7_real_two_provider_smoke.v1"


def _review_actor_proof(
    review_events: list[dict[str, Any]],
    run_events: list[dict[str, Any]],
    *,
    expected_report_count: int,
) -> dict[str, bool]:
    governed_review_events = [
        event
        for event in review_events
        if str(event.get("event_type") or "").startswith("review_")
    ]
    governed_run_events = [
        event
        for event in run_events
        if str(event.get("event_type") or "").startswith("stage7_review_")
    ]
    review_submissions = [
        event
        for event in governed_review_events
        if event.get("event_type") == "review_report_submitted"
    ]
    run_submissions = [
        event
        for event in governed_run_events
        if event.get("event_type") == "stage7_review_report_submitted"
    ]

    def submission_key(event: dict[str, Any]) -> tuple[str, str, str] | None:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        assignment_id = str(metadata.get("assignment_id") or "")
        report_id = str(metadata.get("report_id") or "")
        actor = str(event.get("actor") or "")
        if not assignment_id or not report_id or not actor:
            return None
        return assignment_id, report_id, actor

    review_keys = [submission_key(event) for event in review_submissions]
    run_keys = [submission_key(event) for event in run_submissions]
    required_submissions = (
        len(review_submissions) == expected_report_count
        and len(run_submissions) == expected_report_count
    )
    all_governed = governed_review_events + governed_run_events
    return {
        "canonical": bool(governed_review_events)
        and bool(governed_run_events)
        and all(str(event.get("actor") or "") in ALLOWED_ACTORS for event in all_governed),
        "report_actor_reviewer": required_submissions
        and all(event.get("actor") == "reviewer" for event in review_submissions + run_submissions),
        "submission_pairs_consistent": required_submissions
        and all(key is not None for key in review_keys + run_keys)
        and sorted(review_keys) == sorted(run_keys),
    }


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
    actor_proof = _review_actor_proof(
        observed.get("review_events") or [],
        observed.get("run_review_events") or [],
        expected_report_count=2,
    )
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
        "persisted_review_event_actors_canonical": actor_proof["canonical"],
        "persisted_review_report_submission_actor": actor_proof["report_actor_reviewer"],
        "persisted_review_run_event_pair_consistent": actor_proof[
            "submission_pairs_consistent"
        ],
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


STAGE7_REPAIR_SMOKE_SCHEMA = "buildforme.stage7_real_repair_loop_smoke.v1"


def _successful_attempt_proof(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    succeeded = [item for item in attempts if item.get("status") == "succeeded"]
    providers = sorted({str(item.get("provider_id") or "") for item in succeeded})
    fingerprints = sorted(
        str(item.get("report_fingerprint") or "")
        for item in succeeded
        if item.get("report_fingerprint")
    )
    return {
        "succeeded": succeeded,
        "providers": providers,
        "report_fingerprints": fingerprints,
        "real_processes": bool(attempts)
        and all(
            item.get("process_started") is True
            and int((item.get("process") or {}).get("pid") or 0) > 0
            for item in attempts
        ),
        "auth_verified": bool(attempts)
        and all(item.get("auth_probe_verified") is True for item in attempts),
        "exit_zero": bool(attempts)
        and all((item.get("process") or {}).get("exit_code") == 0 for item in attempts),
        "cleanup_confirmed": bool(attempts)
        and all((item.get("process") or {}).get("cleanup_ok") is True for item in attempts),
        "unchanged": bool(attempts)
        and all(
            item.get("worktree_unchanged") is True
            and item.get("post_snapshot_proven") is True
            for item in attempts
        ),
    }


def evaluate_stage7_repair_smoke(observed: dict[str, Any]) -> dict[str, Any]:
    initial = _successful_attempt_proof(observed.get("initial_review_attempts") or [])
    final = _successful_attempt_proof(observed.get("final_review_attempts") or [])
    initial_reports = sorted(str(item) for item in (observed.get("initial_report_fingerprints") or []))
    initial_aggregate_reports = sorted(
        str(item) for item in (observed.get("initial_aggregate_report_fingerprints") or [])
    )
    final_reports = sorted(str(item) for item in (observed.get("final_report_fingerprints") or []))
    final_aggregate_reports = sorted(
        str(item) for item in (observed.get("final_aggregate_report_fingerprints") or [])
    )
    initial_actor_proof = _review_actor_proof(
        observed.get("initial_review_events") or [],
        observed.get("initial_run_review_events") or [],
        expected_report_count=2,
    )
    final_actor_proof = _review_actor_proof(
        observed.get("final_review_events") or [],
        observed.get("final_run_review_events") or [],
        expected_report_count=2,
    )
    checks = {
        "controlled_source_fixture_disclosed": observed.get("controlled_source_fixture") is True,
        "controlled_repair_execution_disclosed": observed.get("controlled_repair_execution_fixture") is True,
        "initial_real_codex_claude_review": initial["providers"] == ["claude", "codex"]
        and len(initial["succeeded"]) == 2
        and initial["real_processes"]
        and initial["auth_verified"]
        and initial["exit_zero"]
        and initial["cleanup_confirmed"]
        and initial["unchanged"],
        "initial_reports_bound_to_execution_and_aggregate": len(initial_reports) == 2
        and initial["report_fingerprints"] == initial_reports == initial_aggregate_reports,
        "initial_cycle_repair_required": observed.get("initial_aggregate_status") == "repair_required",
        "initial_persisted_review_event_actors_canonical": initial_actor_proof["canonical"],
        "initial_persisted_review_report_submission_actor": initial_actor_proof[
            "report_actor_reviewer"
        ],
        "initial_persisted_review_run_event_pair_consistent": initial_actor_proof[
            "submission_pairs_consistent"
        ],
        "blocking_findings_persisted": int(observed.get("blocking_finding_count") or 0) >= 1,
        "repair_packet_bound": bool(observed.get("repair_packet_id"))
        and observed.get("repair_packet_source_cycle_id") == observed.get("initial_cycle_id")
        and observed.get("repair_packet_source_evidence_id") == observed.get("source_evidence_id"),
        "repair_admission_bound": bool(observed.get("repair_admission_id"))
        and observed.get("repair_admission_packet_id") == observed.get("repair_packet_id")
        and observed.get("repair_child_run_id") == observed.get("repair_admission_child_run_id"),
        "repair_seed_verified": bool(observed.get("seed_commit"))
        and bool(observed.get("seed_fingerprint"))
        and observed.get("child_execution_seed_commit") == observed.get("seed_commit")
        and observed.get("child_original_baseline") == observed.get("source_original_baseline"),
        "fresh_repair_evidence": bool(observed.get("fresh_evidence_id"))
        and observed.get("fresh_evidence_id") != observed.get("source_evidence_id")
        and observed.get("repair_verification_passed") is True,
        "repair_review_link_bound": observed.get("repair_review_link_packet_id")
        == observed.get("repair_packet_id")
        and observed.get("repair_review_link_evidence_id") == observed.get("fresh_evidence_id")
        and observed.get("repair_review_link_cycle_id") == observed.get("final_cycle_id"),
        "final_real_codex_claude_review": final["providers"] == ["claude", "codex"]
        and len(final["succeeded"]) == 2
        and final["real_processes"]
        and final["auth_verified"]
        and final["exit_zero"]
        and final["cleanup_confirmed"]
        and final["unchanged"],
        "final_reports_bound_to_execution_and_aggregate": len(final_reports) == 2
        and final["report_fingerprints"] == final_reports == final_aggregate_reports,
        "final_cycle_clear": observed.get("final_aggregate_status") == "clear",
        "final_persisted_review_event_actors_canonical": final_actor_proof["canonical"],
        "final_persisted_review_report_submission_actor": final_actor_proof[
            "report_actor_reviewer"
        ],
        "final_persisted_review_run_event_pair_consistent": final_actor_proof[
            "submission_pairs_consistent"
        ],
        "repair_implementer_excluded": str(observed.get("repair_provider_id") or "")
        not in final["providers"],
        "source_repository_unchanged": observed.get("source_head_before")
        == observed.get("source_head_after")
        and observed.get("source_branch_before") == observed.get("source_branch_after")
        and observed.get("source_patch_before") == observed.get("source_patch_after"),
        "repair_worktree_unchanged_by_reviewers": observed.get("repair_patch_before_review")
        == observed.get("repair_patch_after_review"),
        "no_merge_commits": int(observed.get("merge_commit_count") or 0) == 0,
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    return {
        "schema": STAGE7_REPAIR_SMOKE_SCHEMA,
        "passed": not failed,
        "checks": checks,
        "failed_checks": failed,
        "initial_aggregate_status": observed.get("initial_aggregate_status"),
        "final_aggregate_status": observed.get("final_aggregate_status"),
        "note": (
            "Both review cycles use real Codex and Claude processes. The source implementation and "
            "repair execution are disclosed controlled fixtures; no third-provider execution is claimed."
        ),
    }

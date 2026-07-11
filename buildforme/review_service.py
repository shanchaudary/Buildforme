"""Stage 7 independent multi-agent review authority.

Packet 7A establishes immutable review cycles, blind assignments, append-only
reports/findings, deterministic aggregation, and founder acceptance gating.
"""

from __future__ import annotations

from typing import Any

from buildforme.evidence import validate_evidence_for_storage
from buildforme.governance import compute_run_scope_fingerprint, validate_actor, validate_safe_id
from buildforme.review_contracts import (
    aggregate_review_reports,
    build_review_cycle_record,
    build_review_report_record,
)
from buildforme.storage import LocalStore


def _require_reviewable_run(store: LocalStore, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    run = store.get_run(validate_safe_id(run_id, field="run_id"))
    if str(run.get("execution_mode") or run.get("mode")) != "live_supervised":
        raise ValueError("independent review requires live_supervised execution evidence")
    if str(run.get("status") or "") != "needs_review":
        raise ValueError(f"independent review requires needs_review status, got {run.get('status')}")
    stored_scope = str(run.get("scope_fingerprint") or "")
    computed_scope = compute_run_scope_fingerprint(
        run, run.get("packet") if isinstance(run.get("packet"), dict) else None
    )
    if not stored_scope or stored_scope != computed_scope:
        raise ValueError("run scope fingerprint is missing or stale")
    evidence = store.get_latest_execution_evidence(str(run.get("id")))
    problems = validate_evidence_for_storage(evidence)
    if problems:
        raise ValueError("execution evidence invalid: " + "; ".join(problems))
    if str(evidence.get("run_id") or "") != str(run.get("id") or ""):
        raise ValueError("execution evidence run mismatch")
    if str(evidence.get("constitution", {}).get("hash") or "") != str(
        run.get("constitution_hash") or ""
    ):
        raise ValueError("execution evidence Constitution mismatch")
    verification = evidence.get("verification") if isinstance(evidence.get("verification"), dict) else {}
    if not verification.get("passed"):
        raise ValueError("deterministic verification must pass before independent review")
    return run, evidence


def create_independent_review_cycle(
    store: LocalStore,
    run_id: str,
    *,
    reviewers: list[dict[str, Any]],
    actor: str = "shan",
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run, evidence = _require_reviewable_run(store, run_id)
    actor = validate_actor(actor)
    known = {str(p.get("provider_id")): p for p in store.list_providers()}
    for reviewer in reviewers:
        provider_id = str((reviewer or {}).get("provider_id") or "")
        provider = known.get(provider_id)
        if not provider:
            raise ValueError(f"unknown reviewer provider: {provider_id}")
        if not provider.get("enabled", True):
            raise ValueError(f"reviewer provider disabled: {provider_id}")
    cycle, assignments = build_review_cycle_record(
        run=run,
        evidence=evidence,
        reviewers=reviewers,
        actor=actor,
        policy=policy,
    )
    return store.create_review_cycle_atomic(cycle=cycle, assignments=assignments, actor=actor)


def submit_independent_review_report(
    store: LocalStore,
    cycle_id: str,
    assignment_id: str,
    *,
    payload: dict[str, Any],
    actor: str = "reviewer",
) -> dict[str, Any]:
    cycle = store.get_review_cycle(validate_safe_id(cycle_id, field="cycle_id"))
    assignment = store.get_review_assignment(
        validate_safe_id(assignment_id, field="assignment_id")
    )
    if assignment.get("cycle_id") != cycle.get("cycle_id"):
        raise ValueError("assignment does not belong to review cycle")
    report, findings = build_review_report_record(
        cycle=cycle,
        assignment=assignment,
        payload=payload,
    )
    return store.submit_review_report_atomic(
        cycle_id=cycle_id,
        assignment_id=assignment_id,
        report=report,
        findings=findings,
        actor=str(actor or assignment.get("reviewer_id") or "reviewer"),
    )


def aggregate_independent_review_cycle(
    store: LocalStore,
    cycle_id: str,
    *,
    actor: str = "shan",
) -> dict[str, Any]:
    cycle = store.get_review_cycle(validate_safe_id(cycle_id, field="cycle_id"))
    assignments = store.list_review_assignments(cycle_id)
    reports = store.list_review_reports(cycle_id)
    aggregate = aggregate_review_reports(
        cycle=cycle,
        assignments=assignments,
        reports=reports,
    )
    return store.finalize_review_cycle_atomic(
        cycle_id=cycle_id,
        expected_row_version=int(cycle.get("row_version") or 1),
        aggregate=aggregate,
        actor=validate_actor(actor),
    )


def require_clear_independent_review(store: LocalStore, run: dict[str, Any]) -> dict[str, Any]:
    if not run.get("stage7_review_required"):
        return {"required": False, "status": "not_required"}
    cycle_id = str(run.get("stage7_review_cycle_id") or "")
    review = run.get("independent_review") if isinstance(run.get("independent_review"), dict) else {}
    if not cycle_id or review.get("status") != "clear":
        raise ValueError("founder acceptance requires a clear Stage 7 independent review cycle")
    cycle = store.get_review_cycle(cycle_id)
    if cycle.get("status") != "clear":
        raise ValueError("bound Stage 7 review cycle is not clear")
    evidence = store.get_latest_execution_evidence(str(run.get("id")))
    for field, cycle_field in (
        ("evidence_id", "evidence_id"),
        ("evidence_fingerprint", "evidence_fingerprint"),
    ):
        if str(cycle.get(cycle_field) or "") != str(evidence.get(field) or ""):
            raise ValueError(f"Stage 7 review cycle {field} is stale")
    if str(cycle.get("scope_fingerprint") or "") != str(run.get("scope_fingerprint") or ""):
        raise ValueError("Stage 7 review cycle scope is stale")
    if str(cycle.get("constitution_hash") or "") != str(run.get("constitution_hash") or ""):
        raise ValueError("Stage 7 review cycle Constitution is stale")
    aggregate = cycle.get("aggregate") if isinstance(cycle.get("aggregate"), dict) else {}
    if aggregate.get("blocking_finding_count"):
        raise ValueError("Stage 7 review contains blocking findings")
    if not aggregate.get("quorum_met"):
        raise ValueError("Stage 7 review quorum is not met")
    return cycle



def get_independent_review_cycle_view(store: LocalStore, cycle_id: str) -> dict[str, Any]:
    """Return a blind-safe cycle view.

    Submitted report/finding content is withheld until the cycle is finalized so a
    pending reviewer cannot anchor on another reviewer's conclusions.
    """
    cycle = store.get_review_cycle(validate_safe_id(cycle_id, field="cycle_id"))
    assignments = store.list_review_assignments(cycle_id)
    assignment_view = [
        {
            "assignment_id": item.get("assignment_id"),
            "reviewer_id": item.get("reviewer_id"),
            "provider_id": item.get("provider_id"),
            "role": item.get("role"),
            "status": item.get("status"),
            "submitted_at": item.get("submitted_at"),
        }
        for item in assignments
    ]
    finalized = str(cycle.get("status") or "") in {"clear", "repair_required", "blocked"}
    return {
        "cycle": cycle,
        "assignments": assignment_view,
        "reports": store.list_review_reports(cycle_id) if finalized else [],
        "findings": store.list_review_findings(cycle_id) if finalized else [],
        "blind_material_withheld": not finalized,
    }

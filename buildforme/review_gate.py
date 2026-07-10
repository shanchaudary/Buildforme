"""Stage 6 review gate — provider cannot self-accept final completion."""

from __future__ import annotations

from typing import Any

from buildforme.storage import utc_now_iso


def build_review_package(
    *,
    run: dict[str, Any],
    evidence: dict[str, Any],
    verification: dict[str, Any],
    constitution_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble review package for founder / Stage 6 review-required state.

    Stage 7 adds independent multi-agent review; Stage 6 must not omit review.
    """
    constitution_validation = constitution_validation or {}
    ver_passed = bool(verification.get("passed"))
    const_ok = constitution_validation.get("passed", True) and constitution_validation.get("valid", True)
    blocking = list(verification.get("blocking_reasons") or [])
    if not const_ok:
        blocking.append("constitutional validation failed")
        for v in constitution_validation.get("violations") or []:
            blocking.append(f"{v.get('law_id')}: {v.get('evidence')}")

    remaining_risks = list(verification.get("warnings") or [])
    remaining_risks.extend(
        str(x) for x in (run.get("unresolved_risks") or [])
    )
    if not evidence.get("files_changed") and run.get("operating_mode") == "IMPLEMENTATION":
        remaining_risks.append("implementation mode produced no file changes")

    # Final acceptance never automatic from provider
    decision_required = True
    status = "review_required"
    if blocking:
        status = "blocked_pending_founder"
    elif not ver_passed:
        status = "review_required"

    return {
        "schema": "buildforme.review.v1",
        "run_id": run.get("id"),
        "status": status,
        "decision_required": decision_required,
        "provider_may_self_accept": False,
        "verification_passed": ver_passed,
        "constitution_ok": const_ok,
        "blocking_reasons": blocking,
        "remaining_risks": remaining_risks,
        "diff_files": list(evidence.get("files_changed") or []),
        "diff_stat": evidence.get("diff_stat") or (verification.get("diff") or {}).get("diff_stat"),
        "evidence_fingerprint": evidence.get("evidence_fingerprint"),
        "verification_checks": verification.get("checks") or [],
        "stage7_independent_multi_agent_review": False,
        "note": "Stage 6 attaches deterministic verification and requires founder/review decision. Stage 7 adds independent multi-agent review.",
        "created_at": utc_now_iso(),
    }


def apply_founder_review_decision(
    run: dict[str, Any],
    *,
    decision: str,
    note: str = "",
    actor: str = "shan",
) -> dict[str, Any]:
    """Apply founder decision after Stage 6 review. Does not merge or deploy."""
    decision = str(decision or "").strip().lower()
    allowed = {"accept_for_pr_prep", "reject", "request_changes", "block"}
    if decision not in allowed:
        raise ValueError(f"decision must be one of {sorted(allowed)}")
    review = dict(run.get("review") or {})
    review["founder_decision"] = decision
    review["founder_note"] = note
    review["founder_actor"] = actor
    review["decided_at"] = utc_now_iso()
    # Map to run terminal-ish outcomes without merge authority
    if decision == "accept_for_pr_prep":
        review["status"] = "accepted_for_pr_prep"
        next_status = "completed"
    elif decision == "request_changes":
        review["status"] = "changes_requested"
        next_status = "needs_review"
    elif decision == "block":
        review["status"] = "blocked"
        next_status = "blocked"
    else:
        review["status"] = "rejected"
        next_status = "rejected"
    return {"review": review, "next_status": next_status}

"""Stage 6 review gate — provider cannot self-accept; founder cannot override hard blocks."""

from __future__ import annotations

from typing import Any

from buildforme.storage import utc_now_iso

HARD_BLOCK_CHECK_NAMES = frozenset(
    {
        "forbidden_path",
        "secret_detection",
        "diff_budget",
        "changed_file_manifest",
        "worktree_exists",
        "branch_integrity",
        "tests",
        "build",
        "process_cleanup",
        "symlink_escape",
        "baseline_match",
        "allowed_path",
    }
)


def collect_hard_blocks(
    *,
    run: dict[str, Any],
    evidence: dict[str, Any] | None,
    verification: dict[str, Any] | None,
    constitution_validation: dict[str, Any] | None = None,
) -> list[str]:
    """Return hard-block reasons that prevent accept_for_pr_prep."""
    blocks: list[str] = []
    verification = verification or {}
    evidence = evidence or {}
    constitution_validation = constitution_validation or {}

    if not verification.get("passed", False):
        for reason in verification.get("blocking_reasons") or []:
            blocks.append(str(reason))

    for check in verification.get("checks") or []:
        if check.get("status") == "fail" and str(check.get("name")) in HARD_BLOCK_CHECK_NAMES:
            detail = f"{check.get('name')}: {check.get('detail')}"
            if detail not in blocks:
                blocks.append(detail)

    const_ok = constitution_validation.get("passed", True) and constitution_validation.get(
        "valid", True
    )
    if constitution_validation and not const_ok:
        blocks.append("constitutional validation failed")
        for v in constitution_validation.get("violations") or []:
            blocks.append(f"{v.get('law_id')}: {v.get('evidence')}")

    if run.get("constitution_compliance", {}).get("status") == "violations":
        blocks.append("run constitution_compliance=violations")

    if not run.get("baseline_commit"):
        blocks.append("missing approved baseline_commit")
    if not run.get("repository"):
        blocks.append("missing repository binding")
    if not (run.get("worktree_path") or (run.get("worktree") or {}).get("worktree_path")):
        if str(run.get("execution_mode") or run.get("mode")) == "live_supervised":
            blocks.append("missing worktree")

    if not evidence.get("evidence_fingerprint") and not evidence.get("evidence_id"):
        if str(run.get("execution_mode") or run.get("mode")) == "live_supervised":
            blocks.append("missing evidence integrity")

    manifest = evidence.get("changed_file_manifest") or verification.get("changed_file_manifest") or {}
    if str(run.get("execution_mode") or run.get("mode")) == "live_supervised":
        if manifest and not manifest.get("complete", True):
            blocks.append("incomplete changed-file manifest")

    if evidence.get("process", {}).get("cleanup_ok") is False:
        blocks.append("incomplete process cleanup")

    # Deduplicate preserve order
    seen: set[str] = set()
    out: list[str] = []
    for b in blocks:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def build_review_package(
    *,
    run: dict[str, Any],
    evidence: dict[str, Any],
    verification: dict[str, Any],
    constitution_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    constitution_validation = constitution_validation or {}
    hard_blocks = collect_hard_blocks(
        run=run,
        evidence=evidence,
        verification=verification,
        constitution_validation=constitution_validation,
    )
    ver_passed = bool(verification.get("passed")) and not hard_blocks
    const_ok = constitution_validation.get("passed", True) and constitution_validation.get("valid", True)

    remaining_risks = list(verification.get("warnings") or [])
    remaining_risks.extend(str(x) for x in (run.get("unresolved_risks") or []))
    if not evidence.get("files_changed") and run.get("operating_mode") == "IMPLEMENTATION":
        remaining_risks.append("implementation mode produced no file changes")

    status = "blocked_pending_founder" if hard_blocks else "review_required"

    return {
        "schema": "buildforme.review.v1",
        "run_id": run.get("id"),
        "status": status,
        "decision_required": True,
        "provider_may_self_accept": False,
        "founder_may_override_hard_blocks": False,
        "hard_blocks": hard_blocks,
        "accept_for_pr_prep_allowed": not hard_blocks and ver_passed and const_ok,
        "verification_passed": ver_passed,
        "constitution_ok": const_ok,
        "blocking_reasons": hard_blocks or list(verification.get("blocking_reasons") or []),
        "remaining_risks": remaining_risks,
        "diff_files": list(evidence.get("files_changed") or []),
        "diff_stat": evidence.get("diff_stat")
        or (verification.get("diff") or {}).get("diff_stat"),
        "evidence_fingerprint": evidence.get("evidence_fingerprint"),
        "verification_checks": verification.get("checks") or [],
        "stage7_independent_multi_agent_review": False,
        "note": (
            "Stage 6 requires founder/review decision. Hard governance failures cannot be "
            "accepted via accept_for_pr_prep. Stage 7 adds independent multi-agent review."
        ),
        "created_at": utc_now_iso(),
    }


def apply_founder_review_decision(
    run: dict[str, Any],
    *,
    decision: str,
    note: str = "",
    actor: str = "shan",
    evidence: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    constitution_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply founder decision. accept_for_pr_prep is rejected on hard blocks."""
    decision = str(decision or "").strip().lower()
    allowed = {"accept_for_pr_prep", "reject", "request_changes", "block"}
    if decision not in allowed:
        raise ValueError(f"decision must be one of {sorted(allowed)}")

    evidence = evidence or run.get("evidence") or {}
    verification = verification or run.get("verification") or {}
    constitution_validation = constitution_validation or {}
    if run.get("constitution_compliance"):
        constitution_validation = {
            **constitution_validation,
            "passed": run["constitution_compliance"].get("status") != "violations",
            "valid": run["constitution_compliance"].get("status") != "violations",
            "violations": run["constitution_compliance"].get("violations") or [],
        }

    hard_blocks = collect_hard_blocks(
        run=run,
        evidence=evidence if isinstance(evidence, dict) else {},
        verification=verification if isinstance(verification, dict) else {},
        constitution_validation=constitution_validation,
    )

    if decision == "accept_for_pr_prep" and hard_blocks:
        raise ValueError(
            "accept_for_pr_prep blocked by governance failures: " + "; ".join(hard_blocks[:12])
        )

    review = dict(run.get("review") or {})
    review["founder_decision"] = decision
    review["founder_note"] = note
    review["founder_actor"] = actor
    review["decided_at"] = utc_now_iso()
    review["hard_blocks_at_decision"] = hard_blocks

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
    return {"review": review, "next_status": next_status, "hard_blocks": hard_blocks}

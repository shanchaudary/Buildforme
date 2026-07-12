"""Stage 7 governed repair packet service."""

from __future__ import annotations

from typing import Any
import hashlib
import json


from buildforme.governance import compute_run_scope_fingerprint, validate_actor, validate_branch, validate_safe_id
from buildforme.repair_contracts import build_repair_packet_record
from buildforme.repair_seed import create_repair_seed, delete_repair_seed_ref
from governance.constitution_engine import get_engine
from governance.constitution_lease import issue_lease
from buildforme.storage import LocalStore


def create_governed_repair_packet(
    store: LocalStore,
    cycle_id: str,
    *,
    repair_provider_id: str,
    actor: str = "shan",
) -> dict[str, Any]:
    cycle_id = validate_safe_id(cycle_id, field="cycle_id")
    actor = validate_actor(actor)
    provider_id = validate_safe_id(repair_provider_id, field="repair_provider_id").lower()
    cycle = store.get_review_cycle(cycle_id)
    run = store.get_run(str(cycle.get("run_id") or ""))
    evidence = store.get_evidence_by_id(str(cycle.get("evidence_id") or ""))
    reports = store.list_review_reports(cycle_id)
    findings = store.list_review_findings(cycle_id)
    provider = store.get_provider_record(provider_id)
    if not provider.get("enabled", True):
        raise ValueError("repair provider disabled")
    provider_ack = store.s6.get_provider_ack(provider_id) or {}
    packet = build_repair_packet_record(
        cycle=cycle,
        run=run,
        evidence=evidence,
        reports=reports,
        findings=findings,
        repair_provider_id=provider_id,
        actor=actor,
        provider_ack=provider_ack,
    )
    return store.create_repair_packet_atomic(packet=packet, actor=actor)


def get_governed_repair_packet(store: LocalStore, repair_packet_id: str) -> dict[str, Any]:
    return store.get_repair_packet(validate_safe_id(repair_packet_id, field="repair_packet_id"))



def admit_governed_repair_run(
    store: LocalStore,
    repair_packet_id: str,
    *,
    actor: str = "shan",
) -> dict[str, Any]:
    packet_id = validate_safe_id(repair_packet_id, field="repair_packet_id")
    actor = validate_actor(actor)
    try:
        existing = store.get_repair_admission(packet_id)
        return {
            "admission": existing,
            "run": store.get_run(str(existing.get("child_run_id") or "")),
            "source_run": store.get_run(str(existing.get("source_run_id") or "")),
            "replayed": True,
        }
    except KeyError:
        pass
    repair_packet = store.get_repair_packet(packet_id)
    source_run = store.get_run(str(repair_packet.get("source_run_id") or ""))
    source_evidence = store.get_evidence_by_id(str(repair_packet.get("source_evidence_id") or ""))
    seed = create_repair_seed(
        repair_packet=repair_packet,
        source_run=source_run,
        source_evidence=source_evidence,
    )
    try:
        digest = hashlib.sha256(packet_id.encode("utf-8")).hexdigest()
        run_id = f"run-repair-{digest[:16]}"
        execution_branch = validate_branch(f"feature/repair-{digest[:16]}")
        child_packet_id = f"pkt-repair-{digest[:16]}"
        engine = get_engine()
        source_packet = source_run.get("packet") if isinstance(source_run.get("packet"), dict) else {}
        child_packet = engine.attach_to_packet(
            {
                "id": child_packet_id,
                "objective": "Resolve every blocking finding from the bound independent review",
                "context": json.dumps(
                    {
                        "repair_packet_id": packet_id,
                        "repair_fingerprint": repair_packet.get("repair_fingerprint"),
                        "source_cycle_id": repair_packet.get("source_cycle_id"),
                        "source_evidence_id": repair_packet.get("source_evidence_id"),
                        "blocking_finding_ids": [
                            item.get("finding_id")
                            for item in (repair_packet.get("source_blocking_findings") or [])
                        ],
                    },
                    sort_keys=True,
                ),
                "target_repository": repair_packet.get("repository"),
                "target_branch": source_run.get("requested_target_branch") or source_run.get("target_branch"),
                "operating_mode": source_run.get("operating_mode") or "IMPLEMENTATION",
                "risk": source_run.get("risk") or "YELLOW",
                "allowed_files": list(repair_packet.get("allowed_files") or []),
                "forbidden_files": list(repair_packet.get("forbidden_files") or []),
                "acceptance_criteria": list(repair_packet.get("repair_acceptance_criteria") or []),
                "required_tests": list(source_packet.get("required_tests") or []),
                "manual_proof": list(source_packet.get("manual_proof") or []),
            }
        )
        provider_id = str(repair_packet.get("repair_provider_id") or "")
        lease_id = f"lease-repair-{digest[:16]}"
        lease = issue_lease(
            engine.constitution,
            run_id=run_id,
            provider_id=provider_id,
            packet_id=child_packet_id,
            actor=actor,
            lease_id=lease_id,
        )
        attempt = int(source_run.get("attempt") or 0) + 1
        max_attempts = min(3, max(int(source_run.get("max_attempts") or 1), attempt + 1))
        child = {
            "id": run_id,
            "project_id": source_run.get("project_id"),
            "task_id": source_run.get("task_id"),
            "packet_id": child_packet_id,
            "packet": child_packet,
            "provider_id": provider_id,
            "repository": repair_packet.get("repository"),
            "repository_local_path": repair_packet.get("repository_local_path"),
            "baseline_ref": repair_packet.get("approved_baseline_commit"),
            "baseline_commit": repair_packet.get("approved_baseline_commit"),
            "execution_seed_commit": seed.get("seed_commit"),
            "execution_seed_ref": seed.get("seed_ref"),
            "requested_target_branch": source_run.get("requested_target_branch") or source_run.get("target_branch"),
            "execution_branch": execution_branch,
            "target_branch": execution_branch,
            "operating_mode": source_run.get("operating_mode") or "IMPLEMENTATION",
            "risk": source_run.get("risk") or "YELLOW",
            "status": "draft",
            "requested_capabilities": list(source_run.get("requested_capabilities") or []),
            "approval_requirements": [],
            "approval_records": [],
            "preflight": None,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "timeout_minutes": int(source_run.get("timeout_minutes") or 30),
            "budget": dict(source_run.get("budget") or {}),
            "parent_run_id": source_run.get("id"),
            "repair_packet_id": packet_id,
            "repair_fingerprint": repair_packet.get("repair_fingerprint"),
            "repair_source_cycle_id": repair_packet.get("source_cycle_id"),
            "repair_source_evidence_id": repair_packet.get("source_evidence_id"),
            "requires_independent_review_after_execution": True,
            "stage7_review_required": True,
            "stage7_review_cycle_id": None,
            "independent_review": {
                "status": "awaiting_fresh_execution_and_review_cycle",
                "source_cycle_id": repair_packet.get("source_cycle_id"),
            },
            "dry_run_result": None,
            "result_summary": None,
            "status_history": [],
            "started_at": None,
            "finished_at": None,
            "live_execution": True,
            "mode": "live_supervised",
            "execution_mode": "live_supervised",
            "transport": "cli",
            "worktree": None,
            "evidence": None,
            "verification": None,
            "review": None,
            "task_lock_id": None,
            "evidence_ids": [],
            "idempotency_key": f"stage7-repair:{packet_id}",
        }
        child = engine.attach_to_run(child, lease=lease, actor=actor)
        child["scope_fingerprint"] = compute_run_scope_fingerprint(child, child_packet)
        return store.admit_repair_run_atomic(
            repair_packet_id=packet_id,
            child_run=child,
            lease=lease,
            seed_proof=seed,
            actor=actor,
        )
    except Exception:
        delete_repair_seed_ref(seed)
        raise



def create_repair_review_cycle(
    store: LocalStore,
    repair_packet_id: str,
    *,
    actor: str = "shan",
) -> dict[str, Any]:
    from buildforme.review_service import create_independent_review_cycle

    packet_id = validate_safe_id(repair_packet_id, field="repair_packet_id")
    actor = validate_actor(actor)
    try:
        link = store.get_repair_review_link(packet_id)
        return {
            "cycle": store.get_review_cycle(str(link.get("review_cycle_id") or "")),
            "assignments": store.list_review_assignments(str(link.get("review_cycle_id") or "")),
            "repair_review_link": link,
            "replayed": True,
        }
    except KeyError:
        pass
    packet = store.get_repair_packet(packet_id)
    admission = store.get_repair_admission(packet_id)
    child_run_id = str(admission.get("child_run_id") or "")
    source_cycle_id = str(packet.get("source_cycle_id") or "")
    source_assignments = store.list_review_assignments(source_cycle_id)
    reviewers = [
        {
            "reviewer_id": str(item.get("reviewer_id") or ""),
            "provider_id": str(item.get("provider_id") or ""),
            "role": str(item.get("role") or "general"),
        }
        for item in source_assignments
    ]
    result = create_independent_review_cycle(
        store,
        child_run_id,
        reviewers=reviewers,
        actor=actor,
    )
    result["repair_review_link"] = store.get_repair_review_link(packet_id)
    result["replayed"] = False
    return result


def execute_governed_repair_and_open_review(
    store: LocalStore,
    repair_packet_id: str,
    *,
    actor: str = "shan",
) -> dict[str, Any]:
    from buildforme.execution_service import execute_supervised

    packet_id = validate_safe_id(repair_packet_id, field="repair_packet_id")
    admission = store.get_repair_admission(packet_id)
    child_run_id = str(admission.get("child_run_id") or "")
    child = store.get_run(child_run_id)
    if str(child.get("status") or "") != "approved":
        raise ValueError("repair child must be approved before supervised repair execution")
    execution = execute_supervised(store, child_run_id)
    saved = execution.get("run") if isinstance(execution, dict) else None
    if not isinstance(saved, dict) or str(saved.get("status") or "") != "needs_review":
        raise ValueError("repair execution did not reach needs_review")
    verification = saved.get("verification") if isinstance(saved.get("verification"), dict) else {}
    if not verification.get("passed"):
        raise ValueError("repair execution deterministic verification did not pass")
    review = create_repair_review_cycle(store, packet_id, actor=actor)
    return {"execution": execution, "review": review, "run": store.get_run(child_run_id)}

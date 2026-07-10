"""Supervised run orchestration for Stage 5 (dry-run only).

Stage 5.5: fail-closed gates, scope fingerprints, revalidation at action time.
"""

from __future__ import annotations

import uuid
from typing import Any

from buildforme.adapters.dry_run import DryRunAdapter
from buildforme.execution_preflight import evaluate_run_preflight
from buildforme.governance import (
    compute_run_scope_fingerprint,
    contains_black_instruction,
    contains_sensitive_allowed_path,
    material_text_blob,
    normalize_repo_for_compare,
    validate_actor,
    validate_branch,
    validate_capabilities,
    validate_safe_id,
    canonicalize_repository,
)
from buildforme.providers import get_provider
from buildforme.run_state import can_transition, is_terminal, transition_run
from buildforme.storage import LocalStore, utc_now_iso

DEFAULT_BUDGET = {
    "max_tokens": None,
    "max_cost_usd": 0,
    "max_duration_minutes": 30,
    "max_files_changed": 20,
    "max_lines_changed": 2000,
}


def create_run(store: LocalStore, payload: dict[str, Any]) -> dict[str, Any]:
    project_id = validate_safe_id(payload.get("project_id"), field="project_id")
    project = store.get_project(project_id)
    if str(project.get("status")) == "archived":
        raise ValueError("archived projects cannot create runs")

    provider_id = validate_safe_id(payload.get("provider_id") or "codex", field="provider_id")
    providers = store.list_providers()
    provider = get_provider(providers, provider_id)
    if not provider:
        raise ValueError(f"Unknown provider: {provider_id}")
    if not provider.get("enabled"):
        raise ValueError(f"Provider disabled: {provider_id}")
    if str(provider.get("mode")) != "dry_run" or provider.get("live_execution_available"):
        raise ValueError("provider must be dry_run only")

    packet = payload.get("packet") if isinstance(payload.get("packet"), dict) else None
    packet_id = payload.get("packet_id")
    if not packet and packet_id:
        packet = store.get_packet(str(packet_id))

    risk = str(payload.get("risk") or (packet or {}).get("risk") or "YELLOW").upper()
    mode = str(payload.get("operating_mode") or (packet or {}).get("operating_mode") or "IMPLEMENTATION").upper()
    if risk == "BLACK":
        raise ValueError("BLACK risk cannot create an executable supervised run")

    repository_raw = str(payload.get("repository") or (packet or {}).get("target_repository") or project.get("repository") or "")
    repository = canonicalize_repository(repository_raw)
    branch = validate_branch(payload.get("target_branch") or (packet or {}).get("target_branch") or "")

    requested = payload.get("requested_capabilities")
    if not isinstance(requested, list) or not requested:
        if mode in {"READ_ONLY_AUDIT", "PLAN_ONLY", "REVIEW", "DOCUMENTATION_ONLY"}:
            requested = ["read_repository", "run_tests"]
        else:
            requested = ["read_repository", "edit_repository", "run_tests", "produce_patch"]
    requested = validate_capabilities([str(c) for c in requested])

    budget = dict(DEFAULT_BUDGET)
    if isinstance(payload.get("budget"), dict):
        for key in DEFAULT_BUDGET:
            if key in payload["budget"]:
                budget[key] = payload["budget"][key]
    budget["max_cost_usd"] = 0  # Stage 5 dry-run only — force zero paid budget

    run_id = str(payload.get("id") or f"run-{uuid.uuid4().hex[:12]}")
    validate_safe_id(run_id, field="run_id")

    now = utc_now_iso()
    run = {
        "id": run_id,
        "project_id": project_id,
        "task_id": payload.get("task_id"),
        "packet_id": (packet or {}).get("id") or packet_id,
        "packet": packet,
        "provider_id": provider_id,
        "repository": repository,
        "target_branch": branch,
        "operating_mode": mode,
        "risk": risk,
        "status": "draft",
        "requested_capabilities": requested,
        "approval_requirements": [],
        "approval_records": [],
        "preflight": None,
        "attempt": int(payload.get("attempt") or 0),
        "max_attempts": min(3, max(1, int(payload.get("max_attempts") or 1))),
        "timeout_minutes": min(120, max(1, int(payload.get("timeout_minutes") or 30))),
        "budget": budget,
        "parent_run_id": payload.get("parent_run_id"),
        "dry_run_result": None,
        "result_summary": None,
        "status_history": [],
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "live_execution": False,
        "mode": "dry_run",
    }
    run["scope_fingerprint"] = compute_run_scope_fingerprint(run, packet)

    # Policy scan on all material text
    black_hits = contains_black_instruction(material_text_blob(run, packet))
    if black_hits:
        raise ValueError(f"BLACK instruction detected in run/packet material: {', '.join(black_hits)}")
    sensitive = contains_sensitive_allowed_path(
        list((packet or {}).get("allowed_files") or [])
    )
    if sensitive:
        raise ValueError(f"sensitive paths cannot be allowed: {sensitive[:5]}")

    saved = store.save_run(run)
    store.append_run_event(saved["id"], "run_created", "Draft supervised run created", actor="system")
    return saved


def run_preflight(store: LocalStore, run_id: str) -> dict[str, Any]:
    run = store.get_run(validate_safe_id(run_id, field="run_id"))
    if is_terminal(str(run.get("status"))):
        raise ValueError("cannot preflight terminal run")
    # Re-enter awaiting_preflight only from draft or re-preflight from awaiting_approval? only draft
    status = str(run.get("status"))
    if status == "draft":
        run = transition_run(run, "awaiting_preflight", "system", "preflight requested")
        store.save_run(run)
    elif status not in {"awaiting_preflight", "awaiting_approval", "approved"}:
        raise ValueError(f"cannot preflight from status {status}")

    store.append_run_event(run_id, "preflight_started", "Preflight evaluation started", actor="system")
    result = evaluate_run_preflight(run, store)
    run = store.get_run(run_id)
    run["preflight"] = result
    run["approval_requirements"] = list(result.get("required_approvals") or [])
    run["scope_fingerprint"] = compute_run_scope_fingerprint(run, run.get("packet") if isinstance(run.get("packet"), dict) else None)
    run["updated_at"] = utc_now_iso()

    if result.get("passed"):
        if result.get("required_approvals"):
            if str(run.get("status")) == "awaiting_preflight":
                run = transition_run(run, "awaiting_approval", "system", "preflight passed; approvals required")
            store.append_run_event(run_id, "preflight_passed", "Preflight passed; awaiting approval", actor="system")
        else:
            if str(run.get("status")) in {"awaiting_preflight", "awaiting_approval"}:
                if str(run.get("status")) == "awaiting_preflight":
                    run = transition_run(run, "approved", "system", "preflight passed; no approvals required")
                elif str(run.get("status")) == "awaiting_approval" and can_transition("awaiting_approval", "approved"):
                    run = transition_run(run, "approved", "system", "preflight passed; no approvals required")
            store.append_run_event(run_id, "preflight_passed", "Preflight passed; auto-approved for dry-run", actor="system")
    else:
        if str(run.get("status")) == "awaiting_preflight":
            run = transition_run(run, "preflight_failed", "system", "preflight failed")
        elif can_transition(str(run.get("status")), "blocked"):
            run = transition_run(run, "blocked", "system", "preflight failed")
        store.append_run_event(
            run_id,
            "preflight_failed",
            "; ".join(result.get("blocking_reasons") or ["failed"]),
            actor="system",
        )
    saved = store.save_run(run)
    return {"run": saved, "preflight": result}


def record_run_approval(
    store: LocalStore,
    run_id: str,
    *,
    requirement_type: str,
    decision: str,
    note: str = "",
    actor: str = "shan",
) -> dict[str, Any]:
    run = store.get_run(validate_safe_id(run_id, field="run_id"))
    actor = validate_actor(actor)
    risk = str(run.get("risk") or "")
    if risk == "BLACK" and decision == "approved":
        raise ValueError("BLACK risk cannot be approved for execution")
    if str(run.get("status")) not in {"awaiting_approval", "awaiting_preflight", "approved", "draft"}:
        if is_terminal(str(run.get("status"))):
            raise ValueError("cannot approve terminal run")

    packet = run.get("packet") if isinstance(run.get("packet"), dict) else None
    fingerprint = compute_run_scope_fingerprint(run, packet)

    record = store.save_run_approval(
        {
            "run_id": run_id,
            "requirement_type": requirement_type,
            "decision": decision,
            "scope": fingerprint,
            "scope_fingerprint": fingerprint,
            "note": note,
            "actor": actor,
            "packet_id": run.get("packet_id"),
            "task_id": run.get("task_id"),
        }
    )
    store.append_run_event(
        run_id,
        "approval_recorded",
        f"{requirement_type} → {decision}",
        actor=actor,
        metadata={"requirement_type": requirement_type, "decision": decision, "scope_fingerprint": fingerprint},
    )

    if decision == "rejected":
        if can_transition(str(run.get("status")), "rejected"):
            run = transition_run(run, "rejected", actor, note or "approval rejected")
            store.save_run(run)
        return {"approval": record, "run": store.get_run(run_id)}

    # Re-check fingerprint still matches
    current_fp = compute_run_scope_fingerprint(store.get_run(run_id), packet)
    if current_fp != fingerprint:
        raise ValueError("Approval invalidated: run scope changed during approval")

    required = list(run.get("approval_requirements") or [])
    approvals = store.list_run_approvals(run_id=run_id)
    approved = set()
    for a in approvals:
        if str(a.get("decision")) != "approved":
            continue
        if str(a.get("scope_fingerprint") or a.get("scope") or "") != current_fp:
            continue  # stale approval ignored
        approved.add(str(a.get("requirement_type")))

    run = store.get_run(run_id)
    if str(run.get("status")) == "awaiting_approval" and required and all(r in approved for r in required):
        run = transition_run(run, "approved", actor, "all required approvals present for current scope")
        store.save_run(run)
        store.append_run_event(run_id, "run_approved", "Run approved for dry-run", actor=actor)
    return {"approval": record, "run": store.get_run(run_id)}


def execute_dry_run(store: LocalStore, run_id: str) -> dict[str, Any]:
    run_id = validate_safe_id(run_id, field="run_id")
    run = store.get_run(run_id)
    if str(run.get("status")) not in {"approved", "queued"}:
        raise ValueError(f"run must be approved before dry-run (status={run.get('status')})")

    # Immediate revalidation of kill switch / project / locks / approvals
    pre = evaluate_run_preflight(run, store)
    if not pre.get("passed"):
        if can_transition(str(run.get("status")), "blocked"):
            run = transition_run(run, "blocked", "system", "preflight failed before dry-run")
        run["preflight"] = pre
        store.save_run(run)
        store.append_run_event(run_id, "preflight_failed", "Blocked at dry-run gate", actor="system")
        raise ValueError("preflight failed: " + "; ".join(pre.get("blocking_reasons") or []))

    # Approval fingerprint revalidation
    required = list(run.get("approval_requirements") or [])
    if required:
        current_fp = compute_run_scope_fingerprint(run, run.get("packet") if isinstance(run.get("packet"), dict) else None)
        approvals = store.list_run_approvals(run_id=run_id)
        approved = set()
        for a in approvals:
            if str(a.get("decision")) != "approved":
                continue
            if str(a.get("scope_fingerprint") or a.get("scope") or "") != current_fp:
                raise ValueError("approval invalidated: scope fingerprint mismatch (run or packet mutated)")
            approved.add(str(a.get("requirement_type")))
        missing = [r for r in required if r not in approved]
        if missing:
            raise ValueError(f"missing valid approvals for current scope: {', '.join(missing)}")

    control = store.get_execution_control()
    if control.get("kill_switch_active"):
        raise ValueError("kill switch active")

    run = transition_run(run, "queued", "system", "dry-run queued")
    run = transition_run(run, "starting", "system", "dry-run starting")
    run = transition_run(run, "running", "system", "dry-run running")
    store.save_run(run)
    store.append_run_event(run_id, "dry_run_started", "Dry-run adapter invoked", actor="system")

    packet = run.get("packet") if isinstance(run.get("packet"), dict) else {}
    if not packet and run.get("packet_id"):
        try:
            packet = store.get_packet(str(run["packet_id"]))
        except KeyError:
            packet = {}

    adapter = DryRunAdapter(provider_id=str(run.get("provider_id") or "dry_run"))
    result = adapter.dry_run(run, packet)
    run = store.get_run(run_id)
    run["dry_run_result"] = result
    run["result_summary"] = result.get("summary")
    run = transition_run(run, "needs_review", "system", "dry-run complete")
    run = transition_run(run, "completed", "system", "dry-run accepted as completed (no live work)")
    saved = store.save_run(run)
    store.append_run_event(
        run_id,
        "dry_run_completed",
        result.get("summary") or "completed",
        actor="system",
        metadata={"network_calls": [], "github_writes": [], "shell_commands_executed": []},
    )
    return {"run": saved, "dry_run": result}


def cancel_run(store: LocalStore, run_id: str, *, actor: str = "shan", reason: str = "") -> dict[str, Any]:
    run = store.get_run(validate_safe_id(run_id, field="run_id"))
    actor = validate_actor(actor)
    status = str(run.get("status"))
    if is_terminal(status):
        raise ValueError("cannot cancel terminal run")
    note = reason or "cancel requested"
    if status in {"running", "starting", "queued"}:
        run = transition_run(run, "cancel_requested", actor, note)
        store.save_run(run)
        store.append_run_event(run_id, "cancel_requested", note, actor=actor)
        run = transition_run(run, "cancelled", actor, note)
    elif can_transition(status, "rejected"):
        run = transition_run(run, "rejected", actor, note)
    elif can_transition(status, "blocked"):
        run = transition_run(run, "blocked", actor, note)
    else:
        raise ValueError(f"cannot cancel from status {status}")
    saved = store.save_run(run)
    store.append_run_event(run_id, "run_cancelled", note, actor=actor)
    return saved


def retry_run(store: LocalStore, run_id: str) -> dict[str, Any]:
    parent = store.get_run(validate_safe_id(run_id, field="run_id"))
    if not is_terminal(str(parent.get("status"))):
        raise ValueError("only terminal runs can be retried")
    if str(parent.get("status")) not in {"failed", "timed_out", "cancelled", "preflight_failed"}:
        raise ValueError(f"cannot retry status {parent.get('status')}")
    if str(parent.get("risk")).upper() in {"RED", "BLACK"}:
        raise ValueError("no automatic retry for RED/BLACK tasks — create a new run with approval")
    # Kill switch / project state still apply via create_run + preflight later
    control = store.get_execution_control()
    if control.get("kill_switch_active"):
        raise ValueError("kill switch active; cannot retry")
    attempt = int(parent.get("attempt") or 0) + 1
    max_attempts = int(parent.get("max_attempts") or 1)
    if attempt >= max_attempts:
        raise ValueError("max attempts exceeded")
    child = create_run(
        store,
        {
            "project_id": parent.get("project_id"),
            "task_id": parent.get("task_id"),
            "packet_id": parent.get("packet_id"),
            "packet": parent.get("packet"),
            "provider_id": parent.get("provider_id"),
            "repository": parent.get("repository"),
            "target_branch": parent.get("target_branch"),
            "operating_mode": parent.get("operating_mode"),
            "risk": parent.get("risk"),
            "requested_capabilities": parent.get("requested_capabilities"),
            "timeout_minutes": parent.get("timeout_minutes"),
            "max_attempts": max_attempts,
            "attempt": attempt,
            "budget": parent.get("budget"),
            "parent_run_id": parent.get("id"),
        },
    )
    store.append_run_event(
        child["id"],
        "run_retry_created",
        f"Retry of {parent.get('id')} attempt {attempt}",
        actor="system",
        metadata={"parent_run_id": parent.get("id")},
    )
    return child

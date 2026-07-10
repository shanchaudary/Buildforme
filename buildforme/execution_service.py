"""Supervised run orchestration for Stage 5 dry-run + Stage 6 live CLI.

Stage 5.5: fail-closed gates, scope fingerprints, revalidation at action time.
Stage 5.6: canonical Constitution leases and approval binding.
Stage 6: multi-provider supervised execution, worktrees, evidence, verification, review.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from buildforme.adapters.dry_run import DryRunAdapter
from buildforme.adapters.registry import get_adapter
from buildforme.evidence import build_evidence_bundle
from buildforme.execution_preflight import evaluate_run_preflight
from buildforme.governance import (
    canonicalize_repository,
    compute_run_scope_fingerprint,
    contains_black_instruction,
    contains_sensitive_allowed_path,
    material_text_blob,
    validate_actor,
    validate_branch,
    validate_capabilities,
    validate_safe_id,
)
from buildforme.providers import get_provider
from buildforme.review_gate import apply_founder_review_decision, build_review_package
from buildforme.run_state import can_transition, is_terminal, transition_run
from buildforme.storage import LocalStore, utc_now_iso
from buildforme.verification import verify_run_result
from buildforme.worktree import (
    collect_diff,
    create_isolated_worktree,
    remove_worktree,
    resolve_repo_root,
)
from governance.constitution_binding_guard import validate_approval_binding
from governance.constitution_engine import get_engine
from governance.constitution_lease import (
    persist_lease_append_only,
    validate_run_lease_against_store,
)

DEFAULT_BUDGET = {
    "max_tokens": None,
    "max_cost_usd": 0,
    "max_duration_minutes": 30,
    "max_files_changed": 20,
    "max_lines_changed": 2000,
}


def _require_canonical_run_lease(store: LocalStore, run: dict[str, Any]) -> dict[str, Any]:
    result = validate_run_lease_against_store(run, store)
    if not result["valid"]:
        raise ValueError(
            "run constitution binding invalid: " + "; ".join(result["problems"])
        )
    return result


def _current_approved_types(
    approvals: list[dict[str, Any]],
    run: dict[str, Any],
    scope_fingerprint: str,
    *,
    fail_on_invalid: bool,
) -> set[str]:
    approved: set[str] = set()
    invalid: list[str] = []
    for approval in approvals:
        if str(approval.get("decision")) != "approved":
            continue
        problems = validate_approval_binding(
            approval,
            run,
            expected_scope_fingerprint=scope_fingerprint,
        )
        if problems:
            invalid.extend(problems)
            continue
        approved.add(str(approval.get("requirement_type")))
    if invalid and fail_on_invalid:
        raise ValueError("approval invalidated: " + "; ".join(sorted(set(invalid))))
    return approved


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
    execution_mode = str(payload.get("execution_mode") or "dry_run").strip().lower().replace("-", "_")
    if execution_mode not in {"dry_run", "live_supervised"}:
        raise ValueError("execution_mode must be dry_run or live_supervised")
    # Registry may still say dry_run; live admission is gated at execute time by discovery.
    provider_mode = str(provider.get("mode") or "dry_run").lower().replace("-", "_")
    if provider_mode not in {"dry_run", "live_supervised"}:
        raise ValueError("provider mode invalid")
    if provider_mode not in {"dry_run", "live_supervised"}:
        raise ValueError("provider must not enable unrestricted live mode")

    engine = get_engine()
    provider = engine.attach_to_provider(provider)
    acknowledgement = engine.validate_provider(provider)
    if not acknowledgement["valid"]:
        raise ValueError(
            "provider has not acknowledged the Constitution: "
            + "; ".join(acknowledgement["problems"])
        )

    packet = payload.get("packet") if isinstance(payload.get("packet"), dict) else None
    packet_id = payload.get("packet_id")
    if not packet and packet_id:
        packet = store.get_packet(str(packet_id))
    if isinstance(packet, dict):
        if not packet.get("constitution_hash"):
            packet = engine.attach_to_packet(packet)
        packet_binding = engine.validate_packet(packet)
        if not packet_binding["valid"]:
            raise ValueError(
                "packet constitution binding invalid: "
                + "; ".join(packet_binding["problems"])
            )

    risk = str(payload.get("risk") or (packet or {}).get("risk") or "YELLOW").upper()
    mode = str(
        payload.get("operating_mode")
        or (packet or {}).get("operating_mode")
        or "IMPLEMENTATION"
    ).upper()
    if risk == "BLACK":
        raise ValueError("BLACK risk cannot create an executable supervised run")

    repository_raw = str(
        payload.get("repository")
        or (packet or {}).get("target_repository")
        or project.get("repository")
        or ""
    )
    repository = canonicalize_repository(repository_raw)
    branch = validate_branch(
        payload.get("target_branch") or (packet or {}).get("target_branch") or ""
    )

    requested = payload.get("requested_capabilities")
    if not isinstance(requested, list) or not requested:
        if mode in {"READ_ONLY_AUDIT", "PLAN_ONLY", "REVIEW", "DOCUMENTATION_ONLY"}:
            requested = ["read_repository", "run_tests"]
        else:
            requested = [
                "read_repository",
                "edit_repository",
                "run_tests",
                "produce_patch",
            ]
    requested = validate_capabilities([str(capability) for capability in requested])

    budget = dict(DEFAULT_BUDGET)
    if isinstance(payload.get("budget"), dict):
        for key in DEFAULT_BUDGET:
            if key in payload["budget"]:
                budget[key] = payload["budget"][key]
    budget["max_cost_usd"] = 0

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
        "live_execution": execution_mode == "live_supervised",
        "mode": execution_mode,
        "execution_mode": execution_mode,
        "transport": "cli" if execution_mode == "live_supervised" else "dry_run",
        "worktree": None,
        "evidence": None,
        "verification": None,
        "review": None,
        "task_lock_id": None,
    }
    # Task lock only for live supervised runs (duplicate work protection)
    if execution_mode == "live_supervised" and (payload.get("task_id") or packet_id):
        lock_key = str(payload.get("task_id") or packet_id or run_id)
        existing_locks = store.list_task_locks(active_only=True)
        for lock in existing_locks:
            if str(lock.get("task_key")) == lock_key and str(lock.get("project_id")) == project_id:
                raise ValueError(f"task lock active for {lock_key}: {lock.get('id')}")
        task_lock = store.create_task_lock(
            {
                "task_key": lock_key,
                "project_id": project_id,
                "run_id": run_id,
                "reason": "live supervised run create",
            }
        )
        run["task_lock_id"] = task_lock.get("id")
    run = engine.attach_to_run(run, actor="system")
    persist_lease_append_only(store, run["constitution_lease"])
    _require_canonical_run_lease(store, run)
    run["scope_fingerprint"] = compute_run_scope_fingerprint(run, packet)

    black_hits = contains_black_instruction(material_text_blob(run, packet))
    if black_hits:
        raise ValueError(
            "BLACK instruction detected in run/packet material: "
            + ", ".join(black_hits)
        )
    sensitive = contains_sensitive_allowed_path(
        list((packet or {}).get("allowed_files") or [])
    )
    if sensitive:
        raise ValueError(f"sensitive paths cannot be allowed: {sensitive[:5]}")

    saved = store.save_run(run)
    store.append_run_event(
        saved["id"],
        "run_created",
        "Draft supervised run created",
        actor="system",
        metadata={
            "constitution_version": saved.get("constitution_version"),
            "constitution_hash": saved.get("constitution_hash"),
            "constitution_lease_id": saved.get("constitution_lease_id"),
            "constitution_lease_fingerprint": saved.get(
                "constitution_lease_fingerprint"
            ),
        },
    )
    return saved


def run_preflight(store: LocalStore, run_id: str) -> dict[str, Any]:
    run = store.get_run(validate_safe_id(run_id, field="run_id"))
    if is_terminal(str(run.get("status"))):
        raise ValueError("cannot preflight terminal run")
    status = str(run.get("status"))
    if status == "draft":
        run = transition_run(run, "awaiting_preflight", "system", "preflight requested")
        store.save_run(run)
    elif status not in {"awaiting_preflight", "awaiting_approval", "approved"}:
        raise ValueError(f"cannot preflight from status {status}")

    store.append_run_event(
        run_id,
        "preflight_started",
        "Preflight evaluation started",
        actor="system",
    )
    result = evaluate_run_preflight(run, store)
    run = store.get_run(run_id)
    run["preflight"] = result
    run["approval_requirements"] = list(result.get("required_approvals") or [])
    run["scope_fingerprint"] = compute_run_scope_fingerprint(
        run,
        run.get("packet") if isinstance(run.get("packet"), dict) else None,
    )
    run["updated_at"] = utc_now_iso()

    if result.get("passed"):
        if result.get("required_approvals"):
            if str(run.get("status")) == "awaiting_preflight":
                run = transition_run(
                    run,
                    "awaiting_approval",
                    "system",
                    "preflight passed; approvals required",
                )
            store.append_run_event(
                run_id,
                "preflight_passed",
                "Preflight passed; awaiting approval",
                actor="system",
            )
        else:
            if str(run.get("status")) in {"awaiting_preflight", "awaiting_approval"}:
                if str(run.get("status")) == "awaiting_preflight":
                    run = transition_run(
                        run,
                        "approved",
                        "system",
                        "preflight passed; no approvals required",
                    )
                elif can_transition("awaiting_approval", "approved"):
                    run = transition_run(
                        run,
                        "approved",
                        "system",
                        "preflight passed; no approvals required",
                    )
            store.append_run_event(
                run_id,
                "preflight_passed",
                "Preflight passed; auto-approved for dry-run",
                actor="system",
            )
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
    _require_canonical_run_lease(store, run)
    actor = validate_actor(actor)
    risk = str(run.get("risk") or "")
    if risk == "BLACK" and decision == "approved":
        raise ValueError("BLACK risk cannot be approved for execution")
    if str(run.get("status")) not in {
        "awaiting_approval",
        "awaiting_preflight",
        "approved",
        "draft",
    }:
        if is_terminal(str(run.get("status"))):
            raise ValueError("cannot approve terminal run")

    packet = run.get("packet") if isinstance(run.get("packet"), dict) else None
    fingerprint = compute_run_scope_fingerprint(run, packet)
    engine = get_engine()
    approval_payload = engine.attach_to_approval(
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
        },
        run=run,
    )
    record = store.save_run_approval(approval_payload)
    record_problems = validate_approval_binding(
        record,
        run,
        expected_scope_fingerprint=fingerprint,
    )
    if record_problems:
        raise ValueError(
            "stored approval lost constitutional binding: "
            + "; ".join(record_problems)
        )

    store.append_run_event(
        run_id,
        "approval_recorded",
        f"{requirement_type} → {decision}",
        actor=actor,
        metadata={
            "requirement_type": requirement_type,
            "decision": decision,
            "scope_fingerprint": fingerprint,
            "constitution_lease_id": run.get("constitution_lease_id"),
        },
    )

    if decision == "rejected":
        if can_transition(str(run.get("status")), "rejected"):
            run = transition_run(run, "rejected", actor, note or "approval rejected")
            store.save_run(run)
        return {"approval": record, "run": store.get_run(run_id)}

    current_run = store.get_run(run_id)
    _require_canonical_run_lease(store, current_run)
    current_fp = compute_run_scope_fingerprint(current_run, packet)
    if current_fp != fingerprint:
        raise ValueError("Approval invalidated: run scope changed during approval")

    required = list(current_run.get("approval_requirements") or [])
    approvals = store.list_run_approvals(run_id=run_id)
    approved = _current_approved_types(
        approvals,
        current_run,
        current_fp,
        fail_on_invalid=False,
    )

    run = store.get_run(run_id)
    if (
        str(run.get("status")) == "awaiting_approval"
        and required
        and all(requirement in approved for requirement in required)
    ):
        run = transition_run(
            run,
            "approved",
            actor,
            "all required approvals present for current scope and constitution lease",
        )
        store.save_run(run)
        store.append_run_event(
            run_id,
            "run_approved",
            "Run approved for dry-run",
            actor=actor,
        )
    return {"approval": record, "run": store.get_run(run_id)}


def execute_dry_run(store: LocalStore, run_id: str) -> dict[str, Any]:
    run_id = validate_safe_id(run_id, field="run_id")
    run = store.get_run(run_id)
    if str(run.get("status")) not in {"approved", "queued"}:
        raise ValueError(
            f"run must be approved before dry-run (status={run.get('status')})"
        )

    preflight = evaluate_run_preflight(run, store)
    if not preflight.get("passed"):
        if can_transition(str(run.get("status")), "blocked"):
            run = transition_run(
                run,
                "blocked",
                "system",
                "preflight failed before dry-run",
            )
        run["preflight"] = preflight
        store.save_run(run)
        store.append_run_event(
            run_id,
            "preflight_failed",
            "Blocked at dry-run gate",
            actor="system",
        )
        raise ValueError(
            "preflight failed: "
            + "; ".join(preflight.get("blocking_reasons") or [])
        )

    _require_canonical_run_lease(store, run)

    required = list(run.get("approval_requirements") or [])
    if required:
        current_fp = compute_run_scope_fingerprint(
            run,
            run.get("packet") if isinstance(run.get("packet"), dict) else None,
        )
        approvals = store.list_run_approvals(run_id=run_id)
        approved = _current_approved_types(
            approvals,
            run,
            current_fp,
            fail_on_invalid=True,
        )
        missing = [requirement for requirement in required if requirement not in approved]
        if missing:
            raise ValueError(
                "missing valid approvals for current scope: " + ", ".join(missing)
            )

    control = store.get_execution_control()
    if control.get("kill_switch_active"):
        raise ValueError("kill switch active")

    engine = get_engine()
    providers = store.list_providers()
    provider = get_provider(providers, str(run.get("provider_id") or ""))
    if not provider:
        raise ValueError("provider missing at dry-run")
    provider = engine.attach_to_provider(provider)
    acknowledgement = engine.validate_provider(provider)
    if not acknowledgement["valid"]:
        raise ValueError(
            "provider constitution acknowledgement invalid: "
            + "; ".join(acknowledgement["problems"])
        )
    _require_canonical_run_lease(store, run)

    run = transition_run(run, "queued", "system", "dry-run queued")
    run = transition_run(run, "starting", "system", "dry-run starting")
    run = transition_run(run, "running", "system", "dry-run running")
    store.save_run(run)
    store.append_run_event(
        run_id,
        "dry_run_started",
        "Dry-run adapter invoked",
        actor="system",
        metadata={
            "constitution_lease_id": run.get("constitution_lease_id"),
            "constitution_lease_fingerprint": run.get(
                "constitution_lease_fingerprint"
            ),
            "constitution_hash": run.get("constitution_hash"),
            "constitution_reminder": (run.get("constitution_reminder") or {}).get(
                "phase"
            ),
        },
    )

    packet = run.get("packet") if isinstance(run.get("packet"), dict) else {}
    if not packet and run.get("packet_id"):
        try:
            packet = store.get_packet(str(run["packet_id"]))
        except KeyError:
            packet = {}

    adapter = DryRunAdapter(provider_id=str(run.get("provider_id") or "dry_run"))
    result = adapter.dry_run(run, packet)
    validation = engine.validate_output(
        {
            "summary": result.get("summary"),
            "text": str(result.get("summary") or ""),
            "claims_complete": True,
            "evidence": ["dry_run_adapter", "no_network", "no_shell"],
            "tests": ["dry_run_invariants"],
        },
        context={
            "verified_capabilities": ["dry_run"],
            "acceptance_criteria": (packet or {}).get("acceptance_criteria") or [],
        },
    )
    if not validation.get("passed", True):
        engine.record_validation_violations(
            store,
            validation,
            run_id=run_id,
            packet_id=str(run.get("packet_id") or "") or None,
            provider_id=str(run.get("provider_id") or "") or None,
            lease_id=str(run.get("constitution_lease_id") or "") or None,
        )
        run = store.get_run(run_id)
        run["constitution_compliance"] = {
            "status": "violations",
            "violations": validation.get("violations") or [],
            "validated_at": utc_now_iso(),
        }
        store.save_run(run)
        raise ValueError("constitution validation rejected dry-run completion")

    run = store.get_run(run_id)
    _require_canonical_run_lease(store, run)
    run["dry_run_result"] = result
    run["result_summary"] = result.get("summary")
    run["constitution_compliance"] = {
        "status": "compliant",
        "violations": [],
        "validated_at": utc_now_iso(),
        "reminder_phase": "completion",
    }
    run["constitution_reminder"] = engine.reminder(
        phase="completion",
        lease=(
            run.get("constitution_lease")
            if isinstance(run.get("constitution_lease"), dict)
            else None
        ),
    )
    run = transition_run(run, "needs_review", "system", "dry-run complete")
    run = transition_run(
        run,
        "completed",
        "system",
        "dry-run accepted as completed (no live work)",
    )
    saved = store.save_run(run)
    store.append_run_event(
        run_id,
        "dry_run_completed",
        result.get("summary") or "completed",
        actor="system",
        metadata={
            "network_calls": [],
            "github_writes": [],
            "shell_commands_executed": [],
            "constitution_compliance": "compliant",
            "constitution_hash": saved.get("constitution_hash"),
            "constitution_lease_fingerprint": saved.get(
                "constitution_lease_fingerprint"
            ),
        },
    )
    return {
        "run": saved,
        "dry_run": result,
        "constitution_validation": validation,
    }


def execute_supervised(store: LocalStore, run_id: str, *, repo_root: str | Path | None = None) -> dict[str, Any]:
    """Stage 6 live supervised provider execution in an isolated worktree.

    Provider cannot mark final acceptance. Ends in needs_review with evidence + verification.
    """
    run_id = validate_safe_id(run_id, field="run_id")
    run = store.get_run(run_id)
    if str(run.get("execution_mode") or run.get("mode") or "dry_run") != "live_supervised":
        raise ValueError("run execution_mode must be live_supervised (use run-dry-run for dry_run)")
    if str(run.get("status")) not in {"approved", "queued"}:
        raise ValueError(f"run must be approved before supervised execution (status={run.get('status')})")

    _require_canonical_run_lease(store, run)
    pre = evaluate_run_preflight(run, store)
    if not pre.get("passed"):
        if can_transition(str(run.get("status")), "blocked"):
            run = transition_run(run, "blocked", "system", "preflight failed before live execution")
        run["preflight"] = pre
        store.save_run(run)
        raise ValueError("preflight failed: " + "; ".join(pre.get("blocking_reasons") or []))

    control = store.get_execution_control()
    if control.get("kill_switch_active"):
        raise ValueError("kill switch active")

    # Approvals revalidation
    required = list(run.get("approval_requirements") or [])
    if required:
        current_fp = compute_run_scope_fingerprint(
            run, run.get("packet") if isinstance(run.get("packet"), dict) else None
        )
        approvals = store.list_run_approvals(run_id=run_id)
        approved = _current_approved_types(approvals, run, current_fp, fail_on_invalid=True)
        missing = [r for r in required if r not in approved]
        if missing:
            raise ValueError(f"missing valid approvals: {', '.join(missing)}")

    provider_id = str(run.get("provider_id") or "")
    provider = get_provider(store.list_providers(), provider_id)
    if not provider:
        raise ValueError("provider missing")
    engine = get_engine()
    provider = engine.attach_to_provider(provider)
    ack = engine.validate_provider(provider)
    if not ack["valid"]:
        raise ValueError("provider constitution acknowledgement invalid: " + "; ".join(ack["problems"]))

    packet = run.get("packet") if isinstance(run.get("packet"), dict) else {}
    if not packet and run.get("packet_id"):
        try:
            packet = store.get_packet(str(run["packet_id"]))
        except KeyError:
            packet = {}

    adapter = get_adapter(provider_id, mode="live_supervised", provider_record=provider)
    prep = adapter.prepare_execution(run, packet)
    if not prep.get("prepared"):
        raise ValueError("adapter prepare failed: " + "; ".join(prep.get("problems") or ["unknown"]))

    # Worktree isolation
    root = resolve_repo_root(repo_root) if repo_root else resolve_repo_root()
    worktree_meta = create_isolated_worktree(
        repo_root=root,
        branch=str(run.get("target_branch")),
        baseline_ref="HEAD",
        worktrees_root=root / "runtime" / "worktrees",
        run_id=run_id,
        allow_dirty_main=False,
    )
    run["worktree"] = worktree_meta
    run["baseline_commit"] = worktree_meta.get("baseline_commit")
    run["worktree_path"] = worktree_meta.get("worktree_path")
    run["provider_version"] = (prep.get("health") or {}).get("version")
    run = transition_run(run, "queued", "system", "live supervised queued")
    run = transition_run(run, "starting", "system", "worktree ready")
    run = transition_run(run, "running", "system", "provider launching")
    store.save_run(run)
    store.append_run_event(
        run_id,
        "supervised_started",
        f"Live supervised execution via {provider_id}",
        actor="system",
        metadata={
            "worktree": worktree_meta.get("worktree_path"),
            "baseline": worktree_meta.get("baseline_commit"),
            "constitution_lease_id": run.get("constitution_lease_id"),
        },
    )

    def on_event(event: dict[str, Any]) -> None:
        # Re-check kill switch during execution
        if store.get_execution_control().get("kill_switch_active"):
            try:
                adapter.cancel(run_id)
            except Exception:
                pass
        try:
            store.append_run_event(
                run_id,
                str(event.get("type") or "process_event"),
                str(event.get("message") or "")[:500],
                actor="system",
                metadata={k: v for k, v in event.items() if k not in {"message", "type"} and k != "stdout"},
            )
        except Exception:
            pass

    process_result: dict[str, Any]
    try:
        if store.get_execution_control().get("kill_switch_active"):
            raise ValueError("kill switch active during start")
        process_result = adapter.execute(
            run,
            packet,
            worktree_path=worktree_meta["worktree_path"],
            on_event=on_event,
        )
    except Exception as exc:
        run = store.get_run(run_id)
        run = transition_run(run, "failed", "system", str(exc)[:300])
        store.save_run(run)
        store.append_run_event(run_id, "supervised_failed", str(exc)[:500], actor="system")
        _release_run_locks(store, run)
        raise

    run = store.get_run(run_id)
    if process_result.get("cancelled") or str(run.get("status")) == "cancel_requested":
        if can_transition(str(run.get("status")), "cancelled") or str(run.get("status")) == "running":
            if str(run.get("status")) == "running":
                run = transition_run(run, "cancel_requested", "system", "cancelled during execution")
            if can_transition(str(run.get("status")), "cancelled"):
                run = transition_run(run, "cancelled", "system", "provider process cancelled")
        run["process_result"] = process_result
        store.save_run(run)
        _release_run_locks(store, run)
        return {"run": run, "process": process_result, "cancelled": True}

    if process_result.get("timed_out"):
        run = transition_run(run, "timed_out", "system", "provider timeout")
        run["process_result"] = process_result
        store.save_run(run)
        _release_run_locks(store, run)
        return {"run": run, "process": process_result, "timed_out": True}

    if process_result.get("unavailable"):
        run = transition_run(run, "failed", "system", process_result.get("error") or "provider unavailable")
        run["process_result"] = process_result
        store.save_run(run)
        _release_run_locks(store, run)
        return {"run": run, "process": process_result, "unavailable": True}

    if not process_result.get("ok") and process_result.get("exit_code") not in (0, None):
        # Still collect evidence / verification for review
        pass

    diff = collect_diff(
        Path(worktree_meta["worktree_path"]),
        baseline_commit=worktree_meta.get("baseline_commit"),
    )
    try:
        project = store.get_project(str(run.get("project_id")))
    except KeyError:
        project = None

    verification = verify_run_result(
        run=run,
        packet=packet,
        project=project,
        worktree_path=worktree_meta["worktree_path"],
        baseline_commit=worktree_meta.get("baseline_commit"),
        process_result=process_result,
        budget=run.get("budget") if isinstance(run.get("budget"), dict) else None,
    )
    evidence = build_evidence_bundle(
        run=run,
        packet=packet,
        process_result=process_result,
        worktree=worktree_meta,
        diff=diff,
        provider_health=process_result.get("health") or prep.get("health") or {},
        verification=verification,
        events=store.list_run_events(run_id),
    )
    store.save_run_evidence(evidence)

    # Constitutional validation of output claims
    validation = engine.validate_output(
        {
            "summary": process_result.get("stdout", "")[:2000],
            "text": (process_result.get("stdout") or "")[:4000],
            "claims_complete": bool(process_result.get("ok")),
            "evidence": ["process_supervisor", "worktree_diff", "verification"],
            "tests": ["deterministic_verification"],
        },
        context={
            "verified_capabilities": list(run.get("requested_capabilities") or []),
            "acceptance_criteria": packet.get("acceptance_criteria") or [],
        },
    )
    if not validation.get("passed", True):
        engine.record_validation_violations(
            store,
            validation,
            run_id=run_id,
            packet_id=str(run.get("packet_id") or "") or None,
            provider_id=provider_id,
            lease_id=str(run.get("constitution_lease_id") or "") or None,
        )

    review = build_review_package(
        run=run,
        evidence=evidence,
        verification=verification,
        constitution_validation=validation,
    )

    run = store.get_run(run_id)
    _require_canonical_run_lease(store, run)
    run["process_result"] = {
        "exit_code": process_result.get("exit_code"),
        "timed_out": process_result.get("timed_out"),
        "cancelled": process_result.get("cancelled"),
        "duration_seconds": process_result.get("duration_seconds"),
        "ok": process_result.get("ok"),
        "truncated_stdout": process_result.get("truncated_stdout"),
        "truncated_stderr": process_result.get("truncated_stderr"),
    }
    run["evidence"] = {
        "evidence_fingerprint": evidence.get("evidence_fingerprint"),
        "files_changed": evidence.get("files_changed"),
        "file_count": evidence.get("file_count"),
    }
    run["verification"] = verification
    run["review"] = review
    run["result_summary"] = (
        f"Supervised run finished exit={process_result.get('exit_code')} "
        f"verify_passed={verification.get('passed')} review={review.get('status')}"
    )
    run["constitution_compliance"] = {
        "status": "compliant" if validation.get("passed", True) else "violations",
        "violations": validation.get("violations") or [],
        "validated_at": utc_now_iso(),
    }
    run["constitution_reminder"] = engine.reminder(
        phase="review",
        lease=run.get("constitution_lease") if isinstance(run.get("constitution_lease"), dict) else None,
    )

    # Provider cannot self-complete: always needs_review (or failed/blocked)
    if not verification.get("passed") or not validation.get("passed", True):
        if process_result.get("ok") is False and process_result.get("exit_code") not in (0, None):
            if can_transition(str(run.get("status")), "failed"):
                # Prefer needs_review so founder sees evidence even on failure
                pass
        run = transition_run(run, "needs_review", "system", "verification or constitution issues — review required")
    else:
        run = transition_run(run, "needs_review", "system", "provider finished — founder review required")

    saved = store.save_run(run)
    store.append_run_event(
        run_id,
        "supervised_finished",
        saved.get("result_summary") or "finished",
        actor="system",
        metadata={
            "verification_passed": verification.get("passed"),
            "review_status": review.get("status"),
            "files_changed": evidence.get("file_count"),
        },
    )
    # Keep worktree for review; do not auto-remove (cleanup is explicit)
    return {
        "run": saved,
        "process": process_result,
        "evidence": evidence,
        "verification": verification,
        "review": review,
        "constitution_validation": validation,
    }


def founder_review_decision(
    store: LocalStore,
    run_id: str,
    *,
    decision: str,
    note: str = "",
    actor: str = "shan",
    cleanup_worktree: bool = False,
) -> dict[str, Any]:
    """Founder decision after Stage 6 review. Never merges or deploys."""
    run = store.get_run(validate_safe_id(run_id, field="run_id"))
    actor = validate_actor(actor)
    if str(run.get("status")) not in {"needs_review", "completed", "blocked"}:
        if is_terminal(str(run.get("status"))) and str(run.get("status")) != "completed":
            raise ValueError(f"cannot decide terminal status {run.get('status')}")
    result = apply_founder_review_decision(run, decision=decision, note=note, actor=actor)
    run["review"] = result["review"]
    next_status = result["next_status"]
    current = str(run.get("status"))
    if next_status != current and can_transition(current, next_status):
        run = transition_run(run, next_status, actor, note or decision)
    elif next_status == "completed" and current == "needs_review" and can_transition("needs_review", "completed"):
        run = transition_run(run, "completed", actor, note or decision)
    elif next_status == "rejected" and can_transition(current, "rejected"):
        run = transition_run(run, "rejected", actor, note or decision)
    elif next_status == "blocked" and can_transition(current, "blocked"):
        run = transition_run(run, "blocked", actor, note or decision)
    saved = store.save_run(run)
    store.append_run_event(run_id, "founder_review_decision", f"{decision}: {note}", actor=actor)
    if cleanup_worktree and saved.get("worktree_path"):
        try:
            root = resolve_repo_root()
            remove_worktree(repo_root=root, worktree_path=Path(str(saved["worktree_path"])), force=True)
        except Exception as exc:
            store.append_run_event(run_id, "worktree_cleanup_failed", str(exc)[:300], actor="system")
    _release_run_locks(store, saved)
    return {"run": store.get_run(run_id), "decision": decision}


def cancel_run(
    store: LocalStore,
    run_id: str,
    *,
    actor: str = "shan",
    reason: str = "",
) -> dict[str, Any]:
    run = store.get_run(validate_safe_id(run_id, field="run_id"))
    actor = validate_actor(actor)
    status = str(run.get("status"))
    if is_terminal(status):
        raise ValueError("cannot cancel terminal run")
    note = reason or "cancel requested"
    # Signal process supervisor for live runs
    try:
        from buildforme.process_supervisor import get_process_supervisor

        get_process_supervisor().cancel(run_id)
        adapter = get_adapter(
            str(run.get("provider_id") or "codex"),
            mode=str(run.get("execution_mode") or "dry_run"),
        )
        adapter.cancel(run_id)
    except Exception:
        pass
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
    _release_run_locks(store, saved)
    return saved


def _release_run_locks(store: LocalStore, run: dict[str, Any]) -> None:
    lock_id = run.get("task_lock_id")
    if lock_id:
        try:
            store.release_task_lock(str(lock_id), reason=f"run {run.get('id')} finished")
        except Exception:
            pass



def retry_run(store: LocalStore, run_id: str) -> dict[str, Any]:
    parent = store.get_run(validate_safe_id(run_id, field="run_id"))
    if not is_terminal(str(parent.get("status"))):
        raise ValueError("only terminal runs can be retried")
    if str(parent.get("status")) not in {
        "failed",
        "timed_out",
        "cancelled",
        "preflight_failed",
    }:
        raise ValueError(f"cannot retry status {parent.get('status')}")
    if str(parent.get("risk")).upper() in {"RED", "BLACK"}:
        raise ValueError(
            "no automatic retry for RED/BLACK tasks — create a new run with approval"
        )
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

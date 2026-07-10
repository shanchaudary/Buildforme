"""Supervised run orchestration for Stage 5 dry-run + Stage 6 live CLI.

Stage 5.5: fail-closed gates, scope fingerprints, revalidation at action time.
Stage 5.6: canonical Constitution leases and approval binding.
Stage 6: multi-provider supervised execution, worktrees, evidence, verification, review.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any  # noqa: I001

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
from buildforme.provider_discovery import health_check_provider
from buildforme.providers import get_provider
from buildforme.redaction import redact_event, redact_text
from buildforme.repository_binding import pin_baseline, resolve_registered_repository
from buildforme.review_gate import apply_founder_review_decision, build_review_package
from buildforme.run_state import can_transition, is_terminal, transition_run
from buildforme.storage import LocalStore, utc_now_iso
from buildforme.verification import verify_run_result
from buildforme.worktree import (
    collect_diff,
    create_isolated_worktree,
    remove_worktree,
)
from governance.constitution_binding_guard import validate_approval_binding
from governance.constitution_engine import get_engine
from governance.constitution_lease import validate_run_lease_against_store

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
    # Reject untrusted filesystem path authority from payload
    if payload.get("repo_root") or payload.get("repository_root") or payload.get("local_path"):
        raise ValueError(
            "repo_root/local_path cannot authorize execution; register repository binding on the project"
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

    # Pin repository + baseline BEFORE lease/scope/approval (Stage 6 hard requirement)
    repository_local_path = None
    baseline_commit = None
    baseline_ref = str(payload.get("baseline_ref") or "HEAD")
    if execution_mode == "live_supervised":
        binding = resolve_registered_repository(store, project=project)
        if canonicalize_repository(str(binding.get("repository"))) != repository:
            repository = canonicalize_repository(str(binding.get("repository")))
        repository_local_path = str(binding.get("local_path"))
        pinned = pin_baseline(Path(repository_local_path), baseline_ref=baseline_ref)
        baseline_commit = pinned["baseline_commit"]
        baseline_ref = pinned["baseline_ref"]
    else:
        baseline_commit = payload.get("baseline_commit")
        repository_local_path = payload.get("repository_local_path")

    # Authoritative execution branch established before worktree/approval
    requested_target_branch = branch
    if execution_mode == "live_supervised":
        base_name = branch if branch.startswith("feature/") else f"feature/{branch}"
        execution_branch = f"{base_name.rstrip('/')}-{run_id[-8:]}"
        validate_branch(execution_branch)
    else:
        execution_branch = branch

    now = utc_now_iso()
    run = {
        "id": run_id,
        "project_id": project_id,
        "task_id": payload.get("task_id"),
        "packet_id": (packet or {}).get("id") or packet_id,
        "packet": packet,
        "provider_id": provider_id,
        "repository": repository,
        "repository_local_path": repository_local_path,
        "baseline_ref": baseline_ref,
        "baseline_commit": baseline_commit,
        "requested_target_branch": requested_target_branch,
        "execution_branch": execution_branch,
        "target_branch": execution_branch,  # legacy field tracks execution branch for worktree
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
        "evidence_ids": [],
    }
    # Build lease + scope in memory; validate BLACK/sensitive BEFORE any persistence
    # so a rejected run cannot leave orphan locks or leases.
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

    run = engine.attach_to_run(run, actor="system")
    run["scope_fingerprint"] = compute_run_scope_fingerprint(run, packet)
    if payload.get("idempotency_key"):
        run["idempotency_key"] = str(payload.get("idempotency_key"))

    task_lock_payload = None
    if execution_mode == "live_supervised" and (payload.get("task_id") or packet_id):
        lock_key = str(payload.get("task_id") or packet_id or run_id)
        task_lock_payload = {
            "task_key": lock_key,
            "project_id": project_id,
            "run_id": run_id,
            "reason": "live supervised run create",
        }

    # Atomic admission: lock + lease + run + initial event (single SQLite transaction)
    lease = run.get("constitution_lease") if isinstance(run.get("constitution_lease"), dict) else None
    saved = store.admit_run_atomic(
        run=run,
        lease=lease,
        task_lock=task_lock_payload,
        event_type="run_created",
        event_summary="Draft supervised run created",
        event_actor="system",
        event_metadata={
            "constitution_version": run.get("constitution_version"),
            "constitution_hash": run.get("constitution_hash"),
            "constitution_lease_id": run.get("constitution_lease_id"),
            "constitution_lease_fingerprint": run.get("constitution_lease_fingerprint"),
            "execution_mode": execution_mode,
        },
    )
    _require_canonical_run_lease(store, saved)
    return saved


def _persist_transition(
    store: LocalStore,
    run: dict[str, Any],
    *,
    event_type: str,
    event_summary: str = "",
    actor: str = "system",
    event_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomic run state + event with optimistic concurrency via row_version."""
    expected = run.get("row_version")
    return store.transition_run_with_event(
        run,
        expected_row_version=int(expected) if expected is not None else None,
        event_type=event_type,
        event_summary=event_summary,
        event_actor=actor,
        event_metadata=event_metadata,
    )


def run_preflight(store: LocalStore, run_id: str) -> dict[str, Any]:
    run = store.get_run(validate_safe_id(run_id, field="run_id"))
    if is_terminal(str(run.get("status"))):
        raise ValueError("cannot preflight terminal run")
    status = str(run.get("status"))
    if status == "draft":
        run = transition_run(run, "awaiting_preflight", "system", "preflight requested")
        run = _persist_transition(
            store,
            run,
            event_type="preflight_started",
            event_summary="Preflight evaluation started",
        )
    elif status not in {"awaiting_preflight", "awaiting_approval", "approved"}:
        raise ValueError(f"cannot preflight from status {status}")
    else:
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
            saved = _persist_transition(
                store,
                run,
                event_type="preflight_passed",
                event_summary="Preflight passed; awaiting approval",
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
            saved = _persist_transition(
                store,
                run,
                event_type="preflight_passed",
                event_summary="Preflight passed; auto-approved for dry-run",
            )
    else:
        if str(run.get("status")) == "awaiting_preflight":
            run = transition_run(run, "preflight_failed", "system", "preflight failed")
        elif can_transition(str(run.get("status")), "blocked"):
            run = transition_run(run, "blocked", "system", "preflight failed")
        saved = _persist_transition(
            store,
            run,
            event_type="preflight_failed",
            event_summary="; ".join(result.get("blocking_reasons") or ["failed"]),
        )
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
            _persist_transition(
                store,
                run,
                event_type="approval_rejected",
                event_summary=note or "approval rejected",
                actor=actor,
            )
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
        _persist_transition(
            store,
            run,
            event_type="run_approved",
            event_summary="Run approved for supervised execution",
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


def execute_supervised(store: LocalStore, run_id: str) -> dict[str, Any]:
    """Stage 6 live supervised provider execution in an isolated worktree.

    Repository root comes only from registered project binding + approved baseline SHA.
    Provider cannot mark final acceptance.
    """
    run_id = validate_safe_id(run_id, field="run_id")
    run = store.get_run(run_id)
    if str(run.get("execution_mode") or run.get("mode") or "dry_run") != "live_supervised":
        raise ValueError("run execution_mode must be live_supervised (use run-dry-run for dry_run)")
    if str(run.get("status")) not in {"approved", "queued"}:
        raise ValueError(f"run must be approved before supervised execution (status={run.get('status')})")
    if not run.get("baseline_commit"):
        raise ValueError("run missing approved baseline_commit — recreate run to pin baseline before approval")
    if not run.get("repository_local_path"):
        raise ValueError("run missing repository_local_path binding")

    _require_canonical_run_lease(store, run)
    # Re-validate approvals against current scope (includes baseline SHA)
    current_fp = compute_run_scope_fingerprint(
        run, run.get("packet") if isinstance(run.get("packet"), dict) else None
    )
    if run.get("scope_fingerprint") and str(run.get("scope_fingerprint")) != current_fp:
        raise ValueError("approval scope stale: run scope fingerprint changed (baseline/packet/provider)")

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

    required = list(run.get("approval_requirements") or [])
    if required:
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

    # Live-ready requires verified auth (unknown is not enough)
    health = health_check_provider(provider_id, provider)
    if not health.get("live_ready"):
        raise ValueError(
            "provider not live-ready: " + "; ".join(health.get("unsupported_reasons") or ["unavailable"])
        )

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

    root = Path(str(run["repository_local_path"])).resolve()
    approved_baseline = str(run["baseline_commit"])
    run_branch = str(run.get("execution_branch") or "")
    if not run_branch:
        raise ValueError("run missing execution_branch established at create time")

    worktree_meta = create_isolated_worktree(
        repo_root=root,
        branch=run_branch,
        baseline_commit=approved_baseline,
        run_id=run_id,
        allow_dirty_main=False,
        allow_existing_branch=False,
        require_clean_parent=True,
    )
    if str(worktree_meta.get("baseline_commit")) != approved_baseline:
        raise ValueError("worktree baseline does not match approved baseline")
    run["worktree"] = worktree_meta
    run["worktree_path"] = worktree_meta.get("worktree_path")
    run["workspace_root"] = worktree_meta.get("workspace_root")
    run["provider_version"] = (prep.get("health") or {}).get("version") or health.get("version")
    run = transition_run(run, "queued", "system", "live supervised queued")
    run = transition_run(run, "starting", "system", "worktree ready at approved baseline")
    run = transition_run(run, "running", "system", "provider launching")
    run = _persist_transition(
        store,
        run,
        event_type="supervised_started",
        event_summary=redact_text(f"Live supervised execution via {provider_id}"),
        event_metadata={
            "worktree": worktree_meta.get("worktree_path"),
            "baseline": approved_baseline,
            "execution_branch": run_branch,
            "requested_target_branch": run.get("requested_target_branch"),
            "constitution_lease_id": run.get("constitution_lease_id"),
        },
    )

    def on_event(event: dict[str, Any]) -> None:
        if store.get_execution_control().get("kill_switch_active"):
            try:
                adapter.cancel(run_id)
            except Exception:
                pass
        try:
            cleaned = redact_event(event if isinstance(event, dict) else {"message": str(event)})
            store.append_run_event(
                run_id,
                str(cleaned.get("type") or "process_event"),
                redact_text(str(cleaned.get("message") or ""))[:500],
                actor="system",
                metadata={
                    k: v
                    for k, v in cleaned.items()
                    if k not in {"message", "type"} and k != "stdout"
                },
            )
        except Exception:
            pass

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
        if can_transition(str(run.get("status")), "failed"):
            run = transition_run(run, "failed", "system", redact_text(str(exc)[:300]))
        store.save_run(run)
        store.append_run_event(run_id, "supervised_failed", redact_text(str(exc)[:500]), actor="system")
        _release_run_locks(store, run)
        raise

    run = store.get_run(run_id)
    if process_result.get("cancelled") or str(run.get("status")) == "cancel_requested":
        if str(run.get("status")) == "running" and can_transition("running", "cancel_requested"):
            run = transition_run(run, "cancel_requested", "system", "cancelled during execution")
        if can_transition(str(run.get("status")), "cancelled"):
            run = transition_run(run, "cancelled", "system", "provider process cancelled")
        run["process_result"] = process_result
        store.save_run(run)
        _release_run_locks(store, run)
        return {"run": run, "process": process_result, "cancelled": True}

    if process_result.get("timed_out"):
        if can_transition(str(run.get("status")), "timed_out"):
            run = transition_run(run, "timed_out", "system", "provider timeout")
        run["process_result"] = process_result
        store.save_run(run)
        _release_run_locks(store, run)
        return {"run": run, "process": process_result, "timed_out": True}

    if process_result.get("unavailable"):
        if can_transition(str(run.get("status")), "failed"):
            run = transition_run(
                run, "failed", "system", process_result.get("error") or "provider unavailable"
            )
        run["process_result"] = process_result
        store.save_run(run)
        _release_run_locks(store, run)
        return {"run": run, "process": process_result, "unavailable": True}

    # Post-run resolution — real HEAD/branch after provider work
    from buildforme.changed_files import collect_changed_file_manifest, collect_patch_evidence
    from buildforme.worktree import worktree_status

    wt_path = Path(worktree_meta["worktree_path"])
    post_status = worktree_status(wt_path)
    final_head = str(post_status.get("head_commit") or "")
    final_branch = str(post_status.get("branch") or "")
    worktree_meta = {
        **worktree_meta,
        "head_commit": final_head,
        "branch": final_branch,
        "post_run": True,
    }
    run["final_head_sha"] = final_head
    run["head_commit"] = final_head

    diff = collect_diff(wt_path, baseline_commit=approved_baseline)
    patch_ev = collect_patch_evidence(wt_path, baseline_commit=approved_baseline)
    if isinstance(diff.get("manifest"), dict):
        diff["manifest"]["patch_fingerprint"] = patch_ev.get("patch_fingerprint")
        diff["patch_fingerprint"] = patch_ev.get("patch_fingerprint")
        if not patch_ev.get("complete"):
            diff["manifest"]["complete"] = False
            reasons = list(diff["manifest"].get("blocking_reasons") or [])
            reasons.extend(patch_ev.get("blocking_reasons") or [])
            diff["manifest"]["blocking_reasons"] = reasons

    try:
        project = store.get_project(str(run.get("project_id")))
    except KeyError:
        project = None

    verification = verify_run_result(
        run=run,
        packet=packet,
        project=project,
        worktree_path=str(wt_path),
        baseline_commit=approved_baseline,
        process_result=process_result,
        budget=run.get("budget") if isinstance(run.get("budget"), dict) else None,
    )
    validation = engine.validate_output(
        {
            "summary": redact_text((process_result.get("stdout") or "")[:2000]),
            "text": redact_text((process_result.get("stdout") or "")[:4000]),
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

    evidence = build_evidence_bundle(
        run=run,
        packet=packet,
        process_result=process_result,
        worktree=worktree_meta,
        diff=diff,
        provider_health=process_result.get("health") or prep.get("health") or health,
        verification=verification,
        events=store.list_run_events(run_id),
        constitution_result=validation,
        attempt=int(run.get("attempt") or 0) + 1,
    )
    evidence["approved_baseline_sha"] = approved_baseline
    evidence["final_head_sha"] = final_head
    evidence["execution_branch"] = run_branch
    evidence["patch_fingerprint"] = patch_ev.get("patch_fingerprint")
    evidence["manifest_fingerprint"] = (diff.get("manifest") or {}).get("manifest_fingerprint")
    saved_evidence = store.save_run_evidence(evidence)

    review = build_review_package(
        run=run,
        evidence=saved_evidence,
        verification=verification,
        constitution_validation=validation,
    )
    # Attach review into a follow-up evidence record is optional; keep one primary record

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
        "cleanup_ok": process_result.get("cleanup_ok"),
        "env_names": process_result.get("env_names") or [],
    }
    run["evidence"] = {
        "evidence_id": saved_evidence.get("evidence_id"),
        "evidence_fingerprint": saved_evidence.get("evidence_fingerprint"),
        "files_changed": saved_evidence.get("files_changed"),
        "file_count": saved_evidence.get("file_count"),
        "changed_file_manifest": saved_evidence.get("changed_file_manifest"),
        "process": saved_evidence.get("process"),
    }
    ids = list(run.get("evidence_ids") or [])
    ids.append(saved_evidence.get("evidence_id"))
    run["evidence_ids"] = ids
    run["verification"] = verification
    run["review"] = review
    run["result_summary"] = redact_text(
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

    if str(run.get("status")) == "running" and can_transition("running", "needs_review"):
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
            "evidence_id": saved_evidence.get("evidence_id"),
        },
    )
    return {
        "run": saved,
        "process": process_result,
        "evidence": saved_evidence,
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
    """Founder decision after Stage 6 review. Never merges or deploys. Hard blocks reject accept."""
    run = store.get_run(validate_safe_id(run_id, field="run_id"))
    actor = validate_actor(actor)
    if str(run.get("status")) not in {"needs_review", "completed", "blocked"}:
        if is_terminal(str(run.get("status"))) and str(run.get("status")) != "completed":
            raise ValueError(f"cannot decide terminal status {run.get('status')}")

    evidence = run.get("evidence") if isinstance(run.get("evidence"), dict) else {}
    try:
        evidence = store.get_run_evidence(run_id)
    except KeyError:
        pass
    verification = run.get("verification") if isinstance(run.get("verification"), dict) else {}

    result = apply_founder_review_decision(
        run,
        decision=decision,
        note=note,
        actor=actor,
        evidence=evidence,
        verification=verification,
    )
    run["review"] = result["review"]
    next_status = result["next_status"]
    current = str(run.get("status"))
    if next_status != current and can_transition(current, next_status):
        run = transition_run(run, next_status, actor, note or decision)
    elif next_status == "completed" and current == "needs_review" and can_transition(
        "needs_review", "completed"
    ):
        run = transition_run(run, "completed", actor, note or decision)
    elif next_status == "rejected" and can_transition(current, "rejected"):
        run = transition_run(run, "rejected", actor, note or decision)
    elif next_status == "blocked" and can_transition(current, "blocked"):
        run = transition_run(run, "blocked", actor, note or decision)
    saved = store.save_run(run)
    store.append_run_event(
        run_id,
        "founder_review_decision",
        redact_text(f"{decision}: {note}"),
        actor=actor,
    )
    # Append-only decision evidence linked to execution evidence (do not mutate prior)
    try:
        decision_ev = {
            "run_id": run_id,
            "kind": "founder_decision",
            "parent_evidence_id": evidence.get("evidence_id"),
            "decision": decision,
            "note": note,
            "actor": actor,
            "review": result["review"],
            "execution_evidence_id": evidence.get("evidence_id"),
            "evidence_fingerprint": None,
        }
        from buildforme.evidence import build_evidence_bundle as _beb
        # Lightweight decision record
        decision_ev["evidence_fingerprint"] = __import__("hashlib").sha256(
            __import__("json").dumps(
                {"run_id": run_id, "decision": decision, "parent": evidence.get("evidence_id")},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        store.save_run_evidence(decision_ev)
    except Exception:
        pass
    if cleanup_worktree and saved.get("worktree_path") and saved.get("repository_local_path"):
        try:
            remove_worktree(
                repo_root=Path(str(saved["repository_local_path"])),
                worktree_path=Path(str(saved["worktree_path"])),
                force=True,
            )
        except Exception as exc:
            store.append_run_event(
                run_id, "worktree_cleanup_failed", redact_text(str(exc)[:300]), actor="system"
            )
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
        run = _persist_transition(
            store, run, event_type="cancel_requested", event_summary=note, actor=actor
        )
        run = transition_run(run, "cancelled", actor, note)
    elif can_transition(status, "rejected"):
        run = transition_run(run, "rejected", actor, note)
    elif can_transition(status, "blocked"):
        run = transition_run(run, "blocked", actor, note)
    else:
        raise ValueError(f"cannot cancel from status {status}")
    saved = _persist_transition(
        store, run, event_type="run_cancelled", event_summary=note, actor=actor
    )
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
    # Retry preserves authority: mode, provider, packet, capabilities.
    # Live baseline is re-pinned at create (not copied stale SHA from failed run).
    # Execution branch is re-derived per new run_id (not reused).
    parent_mode = str(parent.get("execution_mode") or parent.get("mode") or "dry_run")
    child = create_run(
        store,
        {
            "project_id": parent.get("project_id"),
            "task_id": parent.get("task_id"),
            "packet_id": parent.get("packet_id"),
            "packet": parent.get("packet"),
            "provider_id": parent.get("provider_id"),
            "repository": parent.get("repository"),
            "target_branch": parent.get("requested_target_branch")
            or parent.get("target_branch"),
            "operating_mode": parent.get("operating_mode"),
            "risk": parent.get("risk"),
            "requested_capabilities": parent.get("requested_capabilities"),
            "timeout_minutes": parent.get("timeout_minutes"),
            "max_attempts": max_attempts,
            "attempt": attempt,
            "budget": parent.get("budget"),
            "parent_run_id": parent.get("id"),
            "execution_mode": parent_mode,
            "baseline_ref": parent.get("baseline_ref") or "HEAD",
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

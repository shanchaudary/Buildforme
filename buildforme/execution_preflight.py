"""Execution preflight engine (Stage 5).

Deny by default when truth is incomplete. Transparent check list.
"""

from __future__ import annotations

from typing import Any

from buildforme.governance import (
    compute_run_scope_fingerprint,
    contains_black_instruction,
    contains_sensitive_allowed_path,
    material_text_blob,
    normalize_repo_for_compare,
)
from buildforme.providers import FORBIDDEN_LIVE_CAPABILITIES, get_provider, provider_supports
from buildforme.storage import LocalStore, utc_now_iso
from governance.constitution_binding_guard import validate_approval_binding
from governance.constitution_engine import get_engine
from governance.constitution_lease import validate_run_lease_against_store

READ_ONLY_MODES = frozenset({"READ_ONLY_AUDIT", "PLAN_ONLY", "DOCUMENTATION_ONLY", "REVIEW"})


def evaluate_run_preflight(
    run: dict[str, Any],
    store: LocalStore,
    planner_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    blocking: list[str] = []
    warnings: list[str] = []
    required_approvals: list[str] = []

    def add(name: str, status: str, reason: str) -> None:
        checks.append({"name": name, "status": status, "reason": reason})
        if status == "fail":
            blocking.append(f"{name}: {reason}")
        elif status == "warning":
            warnings.append(f"{name}: {reason}")

    control = store.get_execution_control()
    if control.get("kill_switch_active"):
        add("global_kill_switch", "fail", control.get("reason") or "Kill switch is active")
    else:
        add("global_kill_switch", "pass", "Kill switch inactive")

    engine = get_engine()
    binding = validate_run_lease_against_store(run, store)
    if binding["valid"]:
        add(
            "constitution_lease",
            "pass",
            f"canonical lease {run.get('constitution_lease_id')} fingerprint={str(run.get('constitution_lease_fingerprint') or '')[:12]}",
        )
    else:
        add("constitution_lease", "fail", "; ".join(binding["problems"]))

    provider_id = str(run.get("provider_id") or "")
    providers = store.list_providers()
    provider_for_ack = get_provider(providers, provider_id) if provider_id else None
    if provider_for_ack:
        provider_for_ack = engine.attach_to_provider(provider_for_ack)
        acknowledgement = engine.validate_provider(provider_for_ack)
        if acknowledgement["valid"]:
            add("constitution_provider_ack", "pass", f"{provider_id} acknowledged")
        else:
            add(
                "constitution_provider_ack",
                "fail",
                "; ".join(acknowledgement["problems"]),
            )
    else:
        add(
            "constitution_provider_ack",
            "fail",
            "provider missing for constitution acknowledgement",
        )

    project_id = str(run.get("project_id") or "")
    project = None
    try:
        project = store.get_project(project_id)
        add("project_exists", "pass", f"Project {project_id} found")
    except KeyError:
        add("project_exists", "fail", f"Project not found: {project_id}")

    if project:
        status = str(project.get("status") or "")
        if status == "archived":
            add("project_status", "fail", "Archived projects cannot execute")
        elif status == "active":
            add("project_status", "pass", "Project active")
        elif status == "paused":
            add("project_status", "fail", "Project status is paused")
        else:
            add("project_status", "fail", f"Project status {status} cannot execute")

        execution_control = store.get_project_execution_control(project_id)
        if not execution_control.get("explicit"):
            add(
                "project_execution_enabled",
                "fail",
                "No explicit project execution-control record (fail closed until set)",
            )
        else:
            execution_status = str(
                execution_control.get("execution_status") or ""
            ).strip().lower()
            if execution_status == "enabled":
                add("project_execution_enabled", "pass", "Execution enabled")
            elif execution_status == "paused":
                add("project_execution_enabled", "fail", "Project execution paused")
            elif execution_status == "locked":
                add("project_execution_enabled", "fail", "Project execution locked")
            else:
                add(
                    "project_execution_enabled",
                    "fail",
                    f"Unknown execution status {execution_status!r} (fail closed)",
                )

    repository = str(run.get("repository") or (project or {}).get("repository") or "").strip()
    repo_key = normalize_repo_for_compare(repository)
    if repository and "/" in repository:
        add("repository_defined", "pass", repository)
    else:
        add("repository_defined", "fail", "Repository full name required")

    branch = str(run.get("target_branch") or "").strip()
    if branch:
        add("target_branch_defined", "pass", branch)
    else:
        add("target_branch_defined", "fail", "Target branch required")

    mode = str(run.get("operating_mode") or "").upper()
    risk = str(run.get("risk") or "RED").upper()
    requested = [str(capability) for capability in (run.get("requested_capabilities") or [])]

    if branch in {"main", "master"} and mode not in READ_ONLY_MODES:
        add("main_branch_policy", "fail", "Implementation runs targeting main/master are blocked")
    elif branch in {"main", "master"}:
        add("main_branch_policy", "pass", "Read-only mode may target main")
    else:
        add("main_branch_policy", "pass", f"Feature branch {branch}")

    locks = store.list_repository_locks(active_only=True, repository=None)
    locks = [
        lock
        for lock in locks
        if normalize_repo_for_compare(str(lock.get("repository") or "")) == repo_key
    ]
    lock_fail = False
    for lock in locks:
        scope = str(lock.get("lock_scope") or "all").lower()
        if scope == "all":
            add("repository_locks", "fail", f"Active all lock: {lock.get('reason') or lock.get('id')}")
            lock_fail = True
        elif scope == "write" and any(
            capability in requested
            for capability in ("edit_repository", "produce_patch", "open_pr")
        ):
            add("repository_locks", "fail", "Write lock blocks edit/patch/PR capabilities")
            lock_fail = True
        elif scope == "merge" and "merge" in requested:
            add("repository_locks", "fail", "Merge lock active")
            lock_fail = True
        elif scope == "production" and any(
            capability in requested for capability in ("deploy", "production_write")
        ):
            add("repository_locks", "fail", "Production lock active")
            lock_fail = True
        elif scope == "branch" and any(
            capability in requested
            for capability in ("edit_repository", "produce_patch", "open_pr")
        ):
            add("repository_locks", "fail", "Branch lock blocks write execution")
            lock_fail = True
        elif scope not in {"all", "write", "merge", "production", "branch"}:
            add("repository_locks", "fail", f"Unknown lock scope {scope!r} (fail closed)")
            lock_fail = True
    if not lock_fail:
        add("repository_locks", "pass", "No blocking repository locks")

    packet = run.get("packet") if isinstance(run.get("packet"), dict) else None
    packet_id = run.get("packet_id")
    if not packet and packet_id:
        try:
            packet = store.get_packet(str(packet_id))
        except KeyError:
            packet = None

    if packet and packet.get("objective") and packet.get("operating_mode"):
        packet_binding = engine.validate_packet(packet)
        if packet_binding["valid"]:
            add("packet_valid", "pass", "Packet present with current Constitution binding")
        else:
            add("packet_valid", "fail", "; ".join(packet_binding["problems"]))
    elif run.get("task_id"):
        add("packet_valid", "warning", "Task id present without full packet object")
    else:
        add("packet_valid", "fail", "Valid packet or task required")

    if packet:
        missing = [
            key
            for key in ("objective", "allowed_files", "forbidden_files", "acceptance_criteria")
            if not packet.get(key)
        ]
        if missing:
            add("packet_completeness", "fail", f"Missing packet fields: {', '.join(missing)}")
        else:
            add("packet_completeness", "pass", "Core packet fields present")
        if str(packet.get("risk") or risk).upper() != risk and packet.get("risk"):
            add("risk_consistency", "warning", "Run risk differs from packet risk; using run risk")
        else:
            add("risk_consistency", "pass", f"Risk {risk}")
    else:
        add("packet_completeness", "fail", "No packet to validate")
        add("risk_consistency", "warning", "No packet for risk cross-check")

    if mode:
        add("operating_mode", "pass", mode)
    else:
        add("operating_mode", "fail", "Operating mode required")

    task_id = run.get("task_id")
    if task_id:
        try:
            task = store.get_planned_task(str(task_id))
            dependencies = [str(dependency) for dependency in (task.get("dependencies") or [])]
            incomplete = []
            for dependency in dependencies:
                try:
                    dependency_task = store.get_planned_task(dependency)
                    if str(dependency_task.get("status")) != "complete":
                        incomplete.append(dependency)
                except KeyError:
                    incomplete.append(dependency)
            if incomplete:
                add("dependencies_complete", "fail", f"Incomplete: {', '.join(incomplete)}")
            else:
                add("dependencies_complete", "pass", "Dependencies complete or none")
            if str(task.get("status")) == "blocked":
                add("task_not_blocked", "fail", "Planned task status is blocked")
            else:
                add("task_not_blocked", "pass", f"Task status {task.get('status')}")
        except KeyError:
            add("dependencies_complete", "warning", "task_id not found in planned tasks")
            add("task_not_blocked", "warning", "task not found")
    else:
        add("dependencies_complete", "pass", "No planned-task dependencies to check")
        add("task_not_blocked", "pass", "No planned task linked")

    if planner_context and planner_context.get("blocked"):
        add("planner_not_blocked", "fail", str(planner_context.get("reason") or "Planner marks blocked"))
    else:
        add("planner_not_blocked", "pass", "No planner block signal")

    providers = store.list_providers()
    provider = get_provider(providers, str(run.get("provider_id") or ""))
    eligible: list[str] = []
    if not provider:
        add("provider_exists", "fail", "Provider not found")
    else:
        add("provider_exists", "pass", provider.get("display_name") or provider.get("provider_id"))
        if provider.get("enabled"):
            add("provider_enabled", "pass", "enabled")
        else:
            add("provider_enabled", "fail", "provider disabled")
        if str(provider.get("mode")) == "dry_run" and not provider.get("live_execution_available"):
            add("provider_mode", "pass", "dry_run only")
        else:
            add("provider_mode", "fail", "Stage 5 requires dry_run and live_execution_available=false")

        support_problems = provider_supports(
            provider,
            risk=risk,
            mode=mode,
            capabilities=requested or ["read_repository"],
        )
        if support_problems:
            for problem in support_problems:
                add("provider_support", "fail", problem)
        else:
            add("provider_support", "pass", "risk/mode/capabilities supported")

        active = store.count_active_runs(provider_id=str(provider.get("provider_id")))
        if str(run.get("status")) in {"queued", "starting", "running", "cancel_requested"}:
            active = max(0, active - 1)
        max_concurrent = int(provider.get("max_concurrent_runs") or 1)
        if active >= max_concurrent:
            add("provider_concurrency", "fail", f"Active runs {active} >= max {max_concurrent}")
        else:
            add("provider_concurrency", "pass", f"{active}/{max_concurrent} active")

        if provider.get("credentials_configured"):
            add("credentials_readiness", "warning", "Credentials must not be configured in Stage 5 storage")
        else:
            add(
                "credentials_readiness",
                "warning",
                "credentials_configured=false (acceptable for dry-run only)",
            )

        if not support_problems and provider.get("enabled"):
            eligible = [str(provider.get("provider_id"))]

    timeout = int(run.get("timeout_minutes") or 30)
    max_timeout = int((provider or {}).get("max_timeout_minutes") or 120)
    if timeout < 1 or timeout > max_timeout:
        add("timeout_ceiling", "fail", f"timeout {timeout} outside 1..{max_timeout}")
    else:
        add("timeout_ceiling", "pass", f"{timeout} minutes")

    attempt = int(run.get("attempt") or 0)
    max_attempts = int(run.get("max_attempts") or 1)
    if attempt >= max_attempts:
        add("retry_ceiling", "fail", f"attempt {attempt} >= max_attempts {max_attempts}")
    else:
        add("retry_ceiling", "pass", f"attempt {attempt}/{max_attempts}")

    if risk == "BLACK":
        add("risk_gate", "fail", "BLACK risk cannot execute")
    elif risk == "RED":
        add("risk_gate", "pass", "RED requires Shan approval")
        required_approvals.append("shan_red_risk_approval")
        if mode == "IMPLEMENTATION":
            required_approvals.append("shan_task_approval")
    else:
        add("risk_gate", "pass", f"Risk {risk}")

    if risk == "YELLOW" and mode == "IMPLEMENTATION":
        required_approvals.append("shan_task_approval")

    black_hits = contains_black_instruction(
        material_text_blob(run, packet if isinstance(packet, dict) else None)
    )
    if black_hits:
        add("material_policy", "fail", f"Unsafe instructions in material fields: {', '.join(black_hits)}")
    else:
        add("material_policy", "pass", "No BLACK instruction patterns in material text")

    bad_capabilities = [
        capability for capability in requested if capability in FORBIDDEN_LIVE_CAPABILITIES
    ]
    if bad_capabilities:
        add("forbidden_capabilities", "fail", f"Blocked: {', '.join(bad_capabilities)}")
    else:
        add("forbidden_capabilities", "pass", "No merge/deploy/production_write requested")

    approvals = store.list_run_approvals(run_id=str(run.get("id") or ""))
    current_fingerprint = compute_run_scope_fingerprint(
        run,
        packet if isinstance(packet, dict) else None,
    )
    approved_types: set[str] = set()
    invalid_approved_records = 0
    for approval in approvals:
        if str(approval.get("decision")) != "approved":
            continue
        approval_problems = validate_approval_binding(
            approval,
            run,
            expected_scope_fingerprint=current_fingerprint,
        )
        if approval_problems:
            invalid_approved_records += 1
            continue
        approved_types.add(str(approval.get("requirement_type")))

    rejected = [approval for approval in approvals if str(approval.get("decision")) == "rejected"]
    if rejected:
        add("approvals", "fail", "Rejected approval present")
    else:
        missing_approvals = [
            requirement
            for requirement in required_approvals
            if requirement not in approved_types
        ]
        if missing_approvals:
            suffix = (
                f"; ignored {invalid_approved_records} stale or mismatched approval(s)"
                if invalid_approved_records
                else ""
            )
            add(
                "approvals",
                "warning",
                f"Missing approvals (run will await approval): {', '.join(missing_approvals)}{suffix}",
            )
        elif required_approvals:
            add("approvals", "pass", "Required approvals match current scope and Constitution lease")
        else:
            add("approvals", "pass", "No extra approvals required")

    allowed = list((packet or {}).get("allowed_files") or run.get("allowed_files") or [])
    sensitive_hits = contains_sensitive_allowed_path([str(path) for path in allowed])
    if sensitive_hits:
        add("sensitive_files", "fail", f"Sensitive paths in allowed files: {sensitive_hits[:3]}")
    else:
        add("sensitive_files", "pass", "No sensitive allowed paths")

    if any(str(path).strip() in {"**", "**/*", "*"} for path in allowed) and risk != "RED":
        add("broad_wildcards", "fail", "Broad wildcard allowed_files requires RED review")
    else:
        add("broad_wildcards", "pass", "Allowed-files scope acceptable")

    budget = run.get("budget") if isinstance(run.get("budget"), dict) else {}
    max_cost = budget.get("max_cost_usd")
    if max_cost is None:
        max_cost = 0
    try:
        max_cost_value = float(max_cost)
    except (TypeError, ValueError):
        max_cost_value = 0
    if max_cost_value < 0:
        add("budget", "fail", "Negative budget invalid")
    else:
        add("budget", "pass", f"max_cost_usd={max_cost_value} (dry-run allows 0)")

    max_files = int(budget.get("max_files_changed") or 20)
    max_lines = int(budget.get("max_lines_changed") or 2000)
    max_duration = int(budget.get("max_duration_minutes") or timeout)
    if max_files < 1 or max_lines < 1 or max_duration < 1:
        add("budget_ceilings", "fail", "File/line/duration ceilings must be positive")
    else:
        add("budget_ceilings", "pass", f"files≤{max_files} lines≤{max_lines} minutes≤{max_duration}")

    policy = store.get_execution_policy()
    max_global = int(policy.get("max_concurrent_global_runs") or 1)
    active_for_limit = store.count_active_runs()
    if str(run.get("status")) in {"queued", "starting", "running", "cancel_requested"}:
        active_for_limit = max(0, active_for_limit - 1)
    if active_for_limit >= max_global:
        add("global_concurrency", "fail", f"Global active runs at ceiling {max_global}")
    else:
        add("global_concurrency", "pass", f"{active_for_limit}/{max_global}")

    passed = not any(check["status"] == "fail" for check in checks)
    return {
        "passed": passed,
        "checks": checks,
        "blocking_reasons": blocking,
        "warnings": warnings,
        "required_approvals": required_approvals,
        "eligible_providers": eligible,
        "evaluated_at": utc_now_iso(),
    }

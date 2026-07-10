"""Execution preflight engine (Stage 5).

Deny by default when truth is incomplete. Transparent check list.
"""

from __future__ import annotations

from typing import Any

from buildforme.providers import FORBIDDEN_LIVE_CAPABILITIES, get_provider, provider_supports
from buildforme.storage import LocalStore, utc_now_iso

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

        exec_ctrl = store.get_project_execution_control(project_id)
        ex = str(exec_ctrl.get("execution_status") or "enabled")
        if ex == "enabled":
            add("project_execution_enabled", "pass", "Execution enabled")
        elif ex == "paused":
            add("project_execution_enabled", "fail", "Project execution paused")
        else:
            add("project_execution_enabled", "fail", f"Project execution {ex}")

    repository = str(run.get("repository") or (project or {}).get("repository") or "").strip()
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
    requested = [str(c) for c in (run.get("requested_capabilities") or [])]

    if branch in {"main", "master"} and mode not in READ_ONLY_MODES:
        add("main_branch_policy", "fail", "Implementation runs targeting main/master are blocked")
    elif branch in {"main", "master"}:
        add("main_branch_policy", "pass", "Read-only mode may target main")
    else:
        add("main_branch_policy", "pass", f"Feature branch {branch}")

    locks = store.list_repository_locks(active_only=True, repository=repository or None)
    lock_fail = False
    for lock in locks:
        scope = str(lock.get("lock_scope") or "all")
        if scope == "all":
            add("repository_locks", "fail", f"Active all lock: {lock.get('reason') or lock.get('id')}")
            lock_fail = True
        elif scope == "write" and any(c in requested for c in ("edit_repository", "produce_patch", "open_pr")):
            add("repository_locks", "fail", "Write lock blocks edit/patch/PR capabilities")
            lock_fail = True
        elif scope == "merge" and "merge" in requested:
            add("repository_locks", "fail", "Merge lock active")
            lock_fail = True
        elif scope == "production" and any(c in requested for c in ("deploy", "production_write")):
            add("repository_locks", "fail", "Production lock active")
            lock_fail = True
        elif scope == "branch" and "edit_repository" in requested:
            add("repository_locks", "fail", "Branch lock blocks write execution")
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
        add("packet_valid", "pass", "Packet present with objective and mode")
    elif run.get("task_id"):
        add("packet_valid", "warning", "Task id present without full packet object")
    else:
        add("packet_valid", "fail", "Valid packet or task required")

    # Completeness
    if packet:
        missing = [k for k in ("objective", "allowed_files", "forbidden_files", "acceptance_criteria") if not packet.get(k)]
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

    # Dependencies if planned task
    task_id = run.get("task_id")
    if task_id:
        try:
            task = store.get_planned_task(str(task_id))
            deps = [str(d) for d in (task.get("dependencies") or [])]
            incomplete = []
            for dep in deps:
                try:
                    dep_task = store.get_planned_task(dep)
                    if str(dep_task.get("status")) != "complete":
                        incomplete.append(dep)
                except KeyError:
                    incomplete.append(dep)
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

    # Provider
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
            provider, risk=risk, mode=mode, capabilities=requested or ["read_repository"]
        )
        if support_problems:
            for p in support_problems:
                add("provider_support", "fail", p)
        else:
            add("provider_support", "pass", "risk/mode/capabilities supported")

        # concurrency
        active = store.count_active_runs(provider_id=str(provider.get("provider_id")))
        max_c = int(provider.get("max_concurrent_runs") or 1)
        if active >= max_c:
            add("provider_concurrency", "fail", f"Active runs {active} >= max {max_c}")
        else:
            add("provider_concurrency", "pass", f"{active}/{max_c} active")

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

    # Timeout / attempts
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

    # Risk gates
    if risk == "BLACK":
        add("risk_gate", "fail", "BLACK risk cannot execute")
    elif risk == "RED":
        add("risk_gate", "pass", "RED requires Shan approval")
        required_approvals.append("shan_red_risk_approval")
    else:
        add("risk_gate", "pass", f"Risk {risk}")

    if risk in {"RED", "YELLOW"} and mode == "IMPLEMENTATION":
        required_approvals.append("shan_task_approval")

    # Forbidden capabilities
    bad_caps = [c for c in requested if c in FORBIDDEN_LIVE_CAPABILITIES]
    if bad_caps:
        add("forbidden_capabilities", "fail", f"Blocked: {', '.join(bad_caps)}")
    else:
        add("forbidden_capabilities", "pass", "No merge/deploy/production_write requested")

    # Approvals present
    approvals = store.list_run_approvals(run_id=str(run.get("id") or ""))
    approved_types = {
        str(a.get("requirement_type"))
        for a in approvals
        if str(a.get("decision")) == "approved"
    }
    rejected = [a for a in approvals if str(a.get("decision")) == "rejected"]
    if rejected:
        add("approvals", "fail", "Rejected approval present")
    else:
        missing = [r for r in required_approvals if r not in approved_types]
        if missing:
            add("approvals", "fail", f"Missing approvals: {', '.join(missing)}")
        elif required_approvals:
            add("approvals", "pass", "Required local approvals present")
        else:
            add("approvals", "pass", "No extra approvals required")

    # Sensitive paths in allowed files
    allowed = list((packet or {}).get("allowed_files") or run.get("allowed_files") or [])
    sensitive_hits = [p for p in allowed if any(s in str(p).lower() for s in (".env", "secret", "credential", "id_rsa"))]
    if sensitive_hits:
        add("sensitive_files", "fail", f"Sensitive paths in allowed files: {sensitive_hits[:3]}")
    else:
        add("sensitive_files", "pass", "No sensitive allowed paths")

    # Broad wildcards
    if any(str(p).strip() in {"**", "**/*", "*"} for p in allowed) and risk != "RED":
        add("broad_wildcards", "fail", "Broad wildcard allowed_files requires RED review")
    else:
        add("broad_wildcards", "pass", "Allowed-files scope acceptable")

    # Budget
    budget = run.get("budget") if isinstance(run.get("budget"), dict) else {}
    max_cost = budget.get("max_cost_usd")
    if max_cost is None:
        max_cost = 0
    try:
        max_cost_f = float(max_cost)
    except (TypeError, ValueError):
        max_cost_f = 0
    if max_cost_f < 0:
        add("budget", "fail", "Negative budget invalid")
    else:
        add("budget", "pass", f"max_cost_usd={max_cost_f} (dry-run allows 0)")

    max_files = int(budget.get("max_files_changed") or 20)
    max_lines = int(budget.get("max_lines_changed") or 2000)
    max_dur = int(budget.get("max_duration_minutes") or timeout)
    if max_files < 1 or max_lines < 1 or max_dur < 1:
        add("budget_ceilings", "fail", "File/line/duration ceilings must be positive")
    else:
        add("budget_ceilings", "pass", f"files≤{max_files} lines≤{max_lines} minutes≤{max_dur}")

    # Global concurrency
    global_active = store.count_active_runs()
    policy = store.get_execution_policy()
    max_global = int(policy.get("max_concurrent_global_runs") or 1)
    if global_active >= max_global and str(run.get("status")) not in {
        "running",
        "starting",
        "queued",
        "cancel_requested",
    }:
        # counting current draft shouldn't block itself if not active
        pass
    active_for_limit = store.count_active_runs()
    # if this run is already active, subtract 0; drafts don't count
    if active_for_limit >= max_global and str(run.get("status")) in {"draft", "awaiting_preflight", "awaiting_approval", "approved", "preflight_failed"}:
        add("global_concurrency", "fail", f"Global active runs at ceiling {max_global}")
    else:
        add("global_concurrency", "pass", f"{active_for_limit}/{max_global}")

    passed = not any(c["status"] == "fail" for c in checks)
    return {
        "passed": passed,
        "checks": checks,
        "blocking_reasons": blocking,
        "warnings": warnings,
        "required_approvals": required_approvals,
        "eligible_providers": eligible,
        "evaluated_at": utc_now_iso(),
    }

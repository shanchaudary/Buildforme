"""Deterministic Chief Planner for Buildforme Stage 4.

No LLM calls. Same inputs → same ranked outputs. Conservative hard rules
override scores. See docs/PLANNER_SCORING.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from buildforme.storage import LocalStore, utc_now_iso

RISK_ORDER = {"GREEN": 0, "YELLOW": 1, "RED": 2, "BLACK": 3}


def plan_project(
    project_id: str,
    store: LocalStore,
    github_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full plan snapshot for one project."""
    project = store.get_project(project_id)
    stages = store.list_stages(project_id)
    tasks = store.list_planned_tasks(project_id)
    truth = store.list_truth(project_id)
    github_data = github_data or {}
    github_available = bool(github_data.get("available", False))

    blockers = detect_blockers(project, stages, tasks, truth, github_data)
    candidates = _build_candidates(project, stages, tasks, truth, github_data)
    ranked = rank_candidate_tasks(candidates, _context(project, stages, tasks, github_data))
    for item in ranked:
        item["project_id"] = project_id
    primary = ranked[0] if ranked else _no_action_recommendation(project_id, github_available)
    primary["project_id"] = project_id
    confidence = _confidence(project, stages, tasks, truth, github_available)

    summary = _health_summary(project, stages, tasks, truth, github_data, blockers)
    plan = {
        "project_id": project_id,
        "project": project,
        "generated_at": utc_now_iso(),
        "confidence": confidence,
        "summary": summary,
        "primary_recommendation": primary,
        "ranked_recommendations": ranked[:10],
        "blockers": blockers,
        "stages": stages,
        "planned_tasks": tasks,
        "truth": truth,
        "github": {
            "available": github_available,
            "open_prs": len(github_data.get("pull_requests") or []),
            "open_issues": len(github_data.get("issues") or []),
            "errors": github_data.get("errors") or [],
            "note": github_data.get("note")
            or (
                "GitHub read-only signals used."
                if github_available
                else "GitHub unavailable or not queried; plan uses local data only."
            ),
        },
        "disclaimer": (
            "Planner recommendations are not approvals, merges, or production authority. "
            "Local plans do not mutate GitHub."
        ),
    }
    store.save_recommendation_snapshot(project_id, plan)
    store.append_event(
        {
            "event_type": "recommendation_generated",
            "project_id": project_id,
            "target_type": "project",
            "target_id": project_id,
            "summary": f"Plan generated: {primary.get('headline')}",
            "metadata": {"confidence": confidence, "ranked": len(ranked)},
        }
    )
    return plan


def recommend_next_action(
    project_id: str,
    store: LocalStore,
    queue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = plan_project(project_id, store, github_data=_queue_to_github(queue))
    return plan["primary_recommendation"]


def rank_candidate_tasks(candidates: list[dict[str, Any]], context: dict[str, Any]) -> list[dict[str, Any]]:
    """Score and sort candidates deterministically."""
    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        rec = _score_candidate(candidate, context)
        if rec is None:
            continue
        scored.append(rec)

    # Stable deterministic sort: total desc, risk safety, title, id
    scored.sort(
        key=lambda r: (
            -int(r.get("total_score") or 0),
            RISK_ORDER.get(str(r.get("risk") or "RED"), 9),
            str(r.get("headline") or ""),
            str(r.get("target_id") or ""),
        )
    )
    for index, item in enumerate(scored, start=1):
        item["rank"] = index
    return scored


def detect_blockers(
    project: dict[str, Any],
    stages: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    truth: list[dict[str, Any]],
    queue: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    queue = queue or {}
    task_by_id = {str(t.get("id")): t for t in tasks}

    for task in tasks:
        if str(task.get("status") or "") in {"complete", "rejected"}:
            continue
        deps = [str(d) for d in (task.get("dependencies") or [])]
        incomplete = []
        for dep in deps:
            dep_task = task_by_id.get(dep)
            if not dep_task or str(dep_task.get("status")) != "complete":
                incomplete.append(dep)
        if incomplete:
            blockers.append(
                {
                    "id": f"dep-{task.get('id')}",
                    "severity": "high" if len(incomplete) > 1 else "medium",
                    "blocker": f"Incomplete dependencies for {task.get('id')}",
                    "what_it_blocks": task.get("title") or task.get("id"),
                    "dependency_ids": incomplete,
                    "recommended_resolution": f"Complete {incomplete[0]} first",
                    "requires_shan": str(task.get("risk") or "") in {"RED", "BLACK"}
                    or bool(task.get("human_approval_required")),
                }
            )
        if str(task.get("status")) == "blocked":
            blockers.append(
                {
                    "id": f"status-{task.get('id')}",
                    "severity": "high",
                    "blocker": f"Task marked blocked: {task.get('title')}",
                    "what_it_blocks": task.get("title") or task.get("id"),
                    "dependency_ids": deps,
                    "recommended_resolution": "Resolve blocker note or re-open when unblocked",
                    "requires_shan": True,
                }
            )
        if str(task.get("risk")) == "BLACK":
            blockers.append(
                {
                    "id": f"black-{task.get('id')}",
                    "severity": "critical",
                    "blocker": f"BLACK task must be rejected/rewritten: {task.get('title')}",
                    "what_it_blocks": "Any execution of this task",
                    "dependency_ids": [],
                    "recommended_resolution": "Reject or rewrite safely; do not execute",
                    "requires_shan": True,
                }
            )

    for item in truth:
        cat = str(item.get("category") or "")
        if cat in {"broken", "unsafe", "blocked"}:
            blockers.append(
                {
                    "id": f"truth-{item.get('id')}",
                    "severity": "critical" if cat == "unsafe" else "high",
                    "blocker": f"Project truth ({cat}): {item.get('title')}",
                    "what_it_blocks": "Progress until verified fix or mitigation",
                    "dependency_ids": [],
                    "recommended_resolution": "Verify evidence and schedule fix or Shan decision",
                    "requires_shan": cat in {"unsafe", "blocked"},
                }
            )

    for pr in queue.get("pull_requests") or []:
        ci = str((pr.get("ci") or {}).get("status") or "")
        if ci == "failing":
            blockers.append(
                {
                    "id": f"ci-pr-{pr.get('number')}",
                    "severity": "high",
                    "blocker": f"Failing CI on PR #{pr.get('number')}: {pr.get('title')}",
                    "what_it_blocks": "Reliable review/merge path for this PR",
                    "dependency_ids": [],
                    "recommended_resolution": "Fix CI before unrelated new implementation",
                    "requires_shan": False,
                }
            )

    if str(project.get("status")) == "paused":
        blockers.append(
            {
                "id": f"project-paused-{project.get('id')}",
                "severity": "medium",
                "blocker": "Project is paused",
                "what_it_blocks": "Agent execution recommendations",
                "dependency_ids": [],
                "recommended_resolution": "Resume project when ready",
                "requires_shan": True,
            }
        )

    # Deterministic order
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    blockers.sort(key=lambda b: (severity_rank.get(str(b.get("severity")), 9), str(b.get("id"))))
    return blockers


def explain_recommendation(recommendation: dict[str, Any]) -> list[str]:
    reasons = list(recommendation.get("reasoning") or [])
    breakdown = recommendation.get("score_breakdown") or {}
    if breakdown:
        parts = [f"{k}={v}" for k, v in sorted(breakdown.items())]
        reasons.append("Score breakdown: " + ", ".join(parts))
    reasons.append(f"Total score: {recommendation.get('total_score')}")
    if recommendation.get("requires_shan"):
        reasons.append("Requires Shan — not unattended agent execution.")
    if not recommendation.get("can_generate_packet"):
        reasons.append("Packet generation disabled for this recommendation type.")
    reasons.append("Does not grant merge or production authority.")
    return reasons


def _context(
    project: dict[str, Any],
    stages: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    github_data: dict[str, Any],
) -> dict[str, Any]:
    current_stage = str(project.get("current_stage_id") or "")
    stage_orders = {str(s.get("id")): int(s.get("order") or 0) for s in stages}
    incomplete_early = _incomplete_mandatory_stages(stages, tasks, current_stage)
    return {
        "project": project,
        "current_stage_id": current_stage,
        "stage_orders": stage_orders,
        "tasks_by_id": {str(t.get("id")): t for t in tasks},
        "incomplete_early_stages": incomplete_early,
        "github_available": bool(github_data.get("available")),
        "now": datetime.now(timezone.utc),
    }


def _build_candidates(
    project: dict[str, Any],
    stages: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    truth: list[dict[str, Any]],
    github_data: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    stage_name = {str(s.get("id")): s.get("name") for s in stages}

    for task in tasks:
        status = str(task.get("status") or "backlog")
        if status in {"complete", "rejected"}:
            continue
        candidates.append(
            {
                "kind": "planned_task",
                "target_type": "planned_task",
                "target_id": task.get("id"),
                "title": task.get("title"),
                "objective": task.get("objective"),
                "status": status,
                "risk": str(task.get("risk") or "YELLOW"),
                "priority": str(task.get("priority") or "medium"),
                "estimated_effort": str(task.get("estimated_effort") or "unknown"),
                "dependencies": list(task.get("dependencies") or []),
                "stage_id": task.get("stage_id"),
                "stage_name": stage_name.get(str(task.get("stage_id")), task.get("stage_id")),
                "human_approval_required": bool(task.get("human_approval_required")),
                "allowed_files": list(task.get("allowed_files") or []),
                "forbidden_files": list(task.get("forbidden_files") or []),
                "acceptance_criteria": list(task.get("acceptance_criteria") or []),
                "required_tests": list(task.get("required_tests") or []),
                "created_at": task.get("created_at"),
                "source_type": task.get("source_type") or "roadmap",
                "source_ref": task.get("source_ref") or {},
                "raw": task,
            }
        )

    for pr in github_data.get("pull_requests") or []:
        ci = str((pr.get("ci") or {}).get("status") or "unknown")
        risk = str((pr.get("classification") or {}).get("risk") or "YELLOW")
        candidates.append(
            {
                "kind": "pull_request",
                "target_type": "pull_request",
                "target_id": f"pr-{pr.get('number')}",
                "title": pr.get("title"),
                "objective": f"Address PR #{pr.get('number')}: {pr.get('title')}",
                "status": "review" if ci == "passing" else ("blocked" if ci == "failing" else "in_progress"),
                "risk": risk,
                "priority": "high" if ci == "failing" else "medium",
                "estimated_effort": "medium",
                "dependencies": [],
                "stage_id": project.get("current_stage_id"),
                "stage_name": stage_name.get(str(project.get("current_stage_id")), ""),
                "human_approval_required": risk in {"RED", "BLACK"},
                "ci_status": ci,
                "html_url": pr.get("html_url"),
                "number": pr.get("number"),
                "repository": pr.get("repository"),
                "created_at": pr.get("updated_at") or pr.get("created_at"),
                "source_type": "pull_request",
                "source_ref": {"number": pr.get("number"), "repository": pr.get("repository")},
                "raw": pr,
            }
        )

    # Unsafe truth → resolve_blocker candidates
    for item in truth:
        if str(item.get("category")) in {"unsafe", "broken"}:
            candidates.append(
                {
                    "kind": "truth",
                    "target_type": "project",
                    "target_id": item.get("id"),
                    "title": item.get("title"),
                    "objective": item.get("description") or item.get("title"),
                    "status": "blocked",
                    "risk": "RED" if item.get("category") == "unsafe" else "YELLOW",
                    "priority": "critical" if item.get("category") == "unsafe" else "high",
                    "estimated_effort": "medium",
                    "dependencies": [],
                    "stage_id": project.get("current_stage_id"),
                    "stage_name": stage_name.get(str(project.get("current_stage_id")), ""),
                    "human_approval_required": item.get("category") == "unsafe",
                    "truth_category": item.get("category"),
                    "created_at": item.get("created_at"),
                    "source_type": "audit",
                    "source_ref": {"truth_id": item.get("id")},
                    "raw": item,
                }
            )

    if str(project.get("status")) == "paused":
        candidates = []
    return candidates


def _score_candidate(candidate: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    risk = str(candidate.get("risk") or "YELLOW").upper()
    status = str(candidate.get("status") or "")
    kind = str(candidate.get("kind") or "")
    tasks_by_id = context.get("tasks_by_id") or {}
    deps = [str(d) for d in (candidate.get("dependencies") or [])]
    incomplete_deps = [d for d in deps if str((tasks_by_id.get(d) or {}).get("status")) != "complete"]

    # Hard rules
    if risk == "BLACK":
        return _recommendation(
            candidate,
            recommendation_type="reject_task",
            headline=f"Reject/rewrite BLACK item: {candidate.get('title')}",
            reasoning=[
                "BLACK risk — never recommend execution",
                "Rewrite objective safely before any agent work",
            ],
            score_breakdown={
                "blocker_impact": 0,
                "stage_alignment": 0,
                "risk_suitability": -100,
                "dependency_readiness": 0,
                "ci_urgency": 0,
                "age": 0,
                "effort_efficiency": 0,
                "human_attention_cost": -15,
            },
            total_score=-100,
            requires_shan=True,
            can_generate_packet=False,
            executable=False,
            blocked_reason="BLACK risk",
        )

    if incomplete_deps:
        return _recommendation(
            candidate,
            recommendation_type="resolve_blocker",
            headline=f"Blocked by dependencies: {candidate.get('title')}",
            reasoning=[
                f"Incomplete hard dependencies: {', '.join(incomplete_deps)}",
                f"Complete {incomplete_deps[0]} first",
            ],
            score_breakdown={
                "blocker_impact": 0,
                "stage_alignment": 0,
                "risk_suitability": 0,
                "dependency_readiness": -40 if len(incomplete_deps) > 1 else -20,
                "ci_urgency": 0,
                "age": 0,
                "effort_efficiency": 0,
                "human_attention_cost": 0,
            },
            total_score=-40 if len(incomplete_deps) > 1 else -20,
            requires_shan=bool(candidate.get("human_approval_required")) or risk == "RED",
            can_generate_packet=False,
            executable=False,
            blocked_reason=f"dependencies: {', '.join(incomplete_deps)}",
            incomplete_dependencies=incomplete_deps,
        )

    if status == "blocked" and kind == "planned_task":
        return _recommendation(
            candidate,
            recommendation_type="resolve_blocker",
            headline=f"Resolve blocker: {candidate.get('title')}",
            reasoning=["Task status is blocked", "Unblock or re-scope before execution"],
            score_breakdown={
                "blocker_impact": 20,
                "stage_alignment": 0,
                "risk_suitability": -20 if risk == "RED" else 0,
                "dependency_readiness": 0,
                "ci_urgency": 0,
                "age": 0,
                "effort_efficiency": 0,
                "human_attention_cost": -5,
            },
            total_score=15,
            requires_shan=True,
            can_generate_packet=False,
            executable=False,
            blocked_reason="status=blocked",
        )

    # Stage skip penalty
    current_stage = str(context.get("current_stage_id") or "")
    stage_orders = context.get("stage_orders") or {}
    cand_stage = str(candidate.get("stage_id") or "")
    stage_alignment = 0
    reasoning: list[str] = []
    if cand_stage and current_stage and cand_stage == current_stage:
        stage_alignment = 20
        reasoning.append("Aligned with current active stage")
    elif cand_stage and current_stage:
        co = int(stage_orders.get(current_stage, 0))
        so = int(stage_orders.get(cand_stage, 0))
        if so == co + 1:
            stage_alignment = 10
            reasoning.append("Belongs to immediately next stage")
        elif so > co + 1:
            stage_alignment = -20
            reasoning.append("Later stage — must not skip incomplete earlier stages")
            if context.get("incomplete_early_stages"):
                # Hard override: do not execute later stage while earlier incomplete
                return _recommendation(
                    candidate,
                    recommendation_type="no_action",
                    headline=f"Do not start yet (earlier stage incomplete): {candidate.get('title')}",
                    reasoning=reasoning
                    + [
                        f"Incomplete earlier stages: {', '.join(context['incomplete_early_stages'])}",
                    ],
                    score_breakdown={
                        "blocker_impact": 0,
                        "stage_alignment": -20,
                        "risk_suitability": 0,
                        "dependency_readiness": 0,
                        "ci_urgency": 0,
                        "age": 0,
                        "effort_efficiency": 0,
                        "human_attention_cost": 0,
                    },
                    total_score=-20,
                    requires_shan=False,
                    can_generate_packet=False,
                    executable=False,
                    blocked_reason="earlier stage incomplete",
                )

    # Risk suitability
    if risk == "GREEN":
        risk_suitability = 15
        reasoning.append("GREEN risk — may run unattended within scope")
    elif risk == "YELLOW":
        risk_suitability = 10
        reasoning.append("YELLOW risk — scoped implementation / review appropriate")
    elif risk == "RED":
        risk_suitability = -20
        reasoning.append("RED risk — Needs Shan; not unattended execution")
    else:
        risk_suitability = 0

    # CI urgency
    ci = str(candidate.get("ci_status") or "")
    if ci == "failing":
        ci_urgency = 25
        reasoning.append("Failing CI PR — prefer fix before new work")
    elif ci in {"pending", "unknown"} and kind == "pull_request":
        ci_urgency = 15
        reasoning.append("PR checks pending/unknown")
    elif ci == "passing" and kind == "pull_request":
        ci_urgency = 10
        reasoning.append("Passing PR ready for review")
    else:
        ci_urgency = 0

    # Blocker impact
    priority = str(candidate.get("priority") or "medium")
    truth_cat = str(candidate.get("truth_category") or "")
    if truth_cat == "unsafe" or priority == "critical":
        blocker_impact = 30
        reasoning.append("Critical blocker / unsafe truth impact")
    elif truth_cat == "broken" or priority == "high" or ci == "failing":
        blocker_impact = 20
        reasoning.append("High blocker impact")
    elif priority == "medium":
        blocker_impact = 10 if kind == "truth" else 0
    else:
        blocker_impact = 0

    # Dependency readiness
    if deps and not incomplete_deps:
        dependency_readiness = 15
        reasoning.append("All dependencies complete")
    elif not deps:
        dependency_readiness = 0
    else:
        dependency_readiness = -20

    # Effort
    effort = str(candidate.get("estimated_effort") or "unknown")
    effort_efficiency = {"small": 10, "medium": 5, "large": 0, "unknown": 0}.get(effort, 0)

    # Age (small boost)
    age = _age_score(candidate.get("created_at"), context.get("now"))

    # Human attention cost
    if risk == "RED" or candidate.get("human_approval_required"):
        human_cost = -15 if risk == "RED" else -5
        reasoning.append("Human attention required")
    else:
        human_cost = 0

    total = (
        blocker_impact
        + stage_alignment
        + risk_suitability
        + dependency_readiness
        + ci_urgency
        + age
        + effort_efficiency
        + human_cost
    )

    if kind == "pull_request" and ci == "failing":
        rec_type = "fix_ci"
        headline = f"Fix failing CI: {candidate.get('title')}"
    elif kind == "pull_request":
        rec_type = "review_pr"
        headline = f"Review PR: {candidate.get('title')}"
    elif kind == "truth":
        rec_type = "resolve_blocker" if truth_cat in {"unsafe", "broken"} else "run_audit"
        headline = f"Address project truth: {candidate.get('title')}"
    elif risk == "RED":
        rec_type = "request_shan_decision"
        headline = f"Needs Shan: {candidate.get('title')}"
    elif status in {"ready", "backlog", "in_progress"}:
        rec_type = "execute_task"
        headline = f"Next agent task: {candidate.get('title')}"
    else:
        rec_type = "execute_task"
        headline = str(candidate.get("title") or "Candidate work")

    requires_shan = risk == "RED" or bool(candidate.get("human_approval_required")) or rec_type == "request_shan_decision"
    can_packet = rec_type in {"execute_task", "fix_ci", "review_pr", "request_shan_decision"} and kind in {
        "planned_task",
        "pull_request",
    }

    if status == "backlog" and risk == "GREEN" and not context.get("github_available") and kind == "planned_task":
        # still ok
        pass

    return _recommendation(
        candidate,
        recommendation_type=rec_type,
        headline=headline,
        reasoning=reasoning or ["Deterministic score based on stage, risk, dependencies, and CI"],
        score_breakdown={
            "blocker_impact": blocker_impact,
            "stage_alignment": stage_alignment,
            "risk_suitability": risk_suitability,
            "dependency_readiness": dependency_readiness,
            "ci_urgency": ci_urgency,
            "age": age,
            "effort_efficiency": effort_efficiency,
            "human_attention_cost": human_cost,
        },
        total_score=total,
        requires_shan=requires_shan,
        can_generate_packet=can_packet,
        executable=rec_type == "execute_task" and not requires_shan,
        incomplete_dependencies=[],
    )


def _recommendation(
    candidate: dict[str, Any],
    *,
    recommendation_type: str,
    headline: str,
    reasoning: list[str],
    score_breakdown: dict[str, int],
    total_score: int,
    requires_shan: bool,
    can_generate_packet: bool,
    executable: bool,
    blocked_reason: str | None = None,
    incomplete_dependencies: list[str] | None = None,
) -> dict[str, Any]:
    rec = {
        "id": f"rec_{uuid.uuid4().hex[:10]}",
        "project_id": None,  # filled by caller if needed
        "recommendation_type": recommendation_type,
        "target_type": candidate.get("target_type"),
        "target_id": candidate.get("target_id"),
        "headline": headline,
        "title": candidate.get("title"),
        "reasoning": list(reasoning),
        "score_breakdown": score_breakdown,
        "total_score": int(total_score),
        "risk": candidate.get("risk"),
        "status": candidate.get("status"),
        "stage_id": candidate.get("stage_id"),
        "stage_name": candidate.get("stage_name"),
        "requires_shan": requires_shan,
        "can_generate_packet": can_generate_packet,
        "executable_unattended": executable,
        "blocked_reason": blocked_reason,
        "incomplete_dependencies": incomplete_dependencies or [],
        "html_url": candidate.get("html_url"),
        "source_type": candidate.get("source_type"),
        "source_ref": candidate.get("source_ref") or {},
        "candidate": {
            "kind": candidate.get("kind"),
            "priority": candidate.get("priority"),
            "estimated_effort": candidate.get("estimated_effort"),
            "allowed_files": candidate.get("allowed_files") or [],
            "forbidden_files": candidate.get("forbidden_files") or [],
            "acceptance_criteria": candidate.get("acceptance_criteria") or [],
            "required_tests": candidate.get("required_tests") or [],
            "objective": candidate.get("objective"),
            "ci_status": candidate.get("ci_status"),
            "number": candidate.get("number"),
            "repository": candidate.get("repository"),
        },
        "created_at": utc_now_iso(),
        "disclaimer": "Recommendation is not merge/production authority.",
    }
    rec["explanation"] = explain_recommendation(rec)
    return rec


def _no_action_recommendation(project_id: str, github_available: bool) -> dict[str, Any]:
    reasons = ["No ranked executable candidates"]
    if not github_available:
        reasons.append("GitHub data unavailable — consider audit or refresh")
    rec = {
        "id": f"rec_none_{project_id}",
        "project_id": project_id,
        "recommendation_type": "no_action",
        "target_type": "project",
        "target_id": project_id,
        "headline": "No safe next execution candidate",
        "title": "No action",
        "reasoning": reasons,
        "score_breakdown": {
            "blocker_impact": 0,
            "stage_alignment": 0,
            "risk_suitability": 0,
            "dependency_readiness": 0,
            "ci_urgency": 0,
            "age": 0,
            "effort_efficiency": 0,
            "human_attention_cost": 0,
        },
        "total_score": 0,
        "risk": "GREEN",
        "status": "none",
        "requires_shan": False,
        "can_generate_packet": False,
        "executable_unattended": False,
        "blocked_reason": None,
        "incomplete_dependencies": [],
        "candidate": {},
        "created_at": utc_now_iso(),
        "disclaimer": "Recommendation is not merge/production authority.",
        "rank": 1,
    }
    rec["explanation"] = explain_recommendation(rec)
    return rec


def _incomplete_mandatory_stages(
    stages: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    current_stage_id: str,
) -> list[str]:
    orders = sorted(stages, key=lambda s: int(s.get("order") or 0))
    current_order = next((int(s.get("order") or 0) for s in stages if str(s.get("id")) == current_stage_id), 0)
    incomplete: list[str] = []
    for stage in orders:
        order = int(stage.get("order") or 0)
        if order >= current_order:
            break
        stage_id = str(stage.get("id"))
        stage_tasks = [t for t in tasks if str(t.get("stage_id")) == stage_id]
        if not stage_tasks:
            continue
        if any(str(t.get("status")) not in {"complete", "rejected"} for t in stage_tasks):
            if str(stage.get("status")) != "complete":
                incomplete.append(stage_id)
    return incomplete


def _health_summary(
    project: dict[str, Any],
    stages: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    truth: list[dict[str, Any]],
    github_data: dict[str, Any],
    blockers: list[dict[str, Any]],
) -> dict[str, Any]:
    ready = sum(1 for t in tasks if str(t.get("status")) == "ready")
    blocked = sum(1 for t in tasks if str(t.get("status")) == "blocked")
    complete = sum(1 for t in tasks if str(t.get("status")) == "complete")
    red_black = sum(1 for t in tasks if str(t.get("risk")) in {"RED", "BLACK"})
    needs_shan = sum(
        1
        for t in tasks
        if str(t.get("risk")) in {"RED", "BLACK"} or bool(t.get("human_approval_required"))
    )
    unverified = sum(1 for t in truth if str(t.get("category")) == "unverified")
    prs = github_data.get("pull_requests") or []
    failing_ci = sum(1 for p in prs if str((p.get("ci") or {}).get("status")) == "failing")
    defined = [t for t in tasks if str(t.get("status")) != "rejected"]
    completion = None
    if defined:
        completion = round(100.0 * complete / len(defined), 1)
    current_stage = next((s for s in stages if str(s.get("id")) == str(project.get("current_stage_id"))), None)
    return {
        "active_stage_id": project.get("current_stage_id"),
        "active_stage_name": (current_stage or {}).get("name"),
        "project_status": project.get("status"),
        "ready_tasks": ready,
        "blocked_tasks": blocked,
        "complete_tasks": complete,
        "open_prs": len(prs),
        "failing_ci": failing_ci,
        "red_black_items": red_black,
        "needs_shan": needs_shan,
        "unverified_truth": unverified,
        "blocker_count": len(blockers),
        "roadmap_completion_percent": completion,
        "roadmap_completion_label": (
            "Roadmap completion based on defined tasks; not a production-readiness guarantee."
            if completion is not None
            else "No defined tasks yet."
        ),
    }


def _confidence(
    project: dict[str, Any],
    stages: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    truth: list[dict[str, Any]],
    github_available: bool,
) -> str:
    if not stages or not tasks:
        return "low"
    verified_truth = [t for t in truth if str(t.get("category")) not in {"unverified"} and int(t.get("confidence") or 0) >= 70]
    if github_available and verified_truth and stages and tasks:
        return "high"
    if stages and tasks:
        return "medium"
    return "low"


def _age_score(created_at: Any, now: datetime | None) -> int:
    if not created_at or now is None:
        return 0
    try:
        raw = str(created_at).replace("Z", "+00:00")
        created = datetime.fromisoformat(raw)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        days = max(0, (now - created).days)
        if days >= 14:
            return 5
        if days >= 7:
            return 3
        if days >= 3:
            return 1
    except ValueError:
        return 0
    return 0


def _queue_to_github(queue: dict[str, Any] | None) -> dict[str, Any]:
    if not queue:
        return {"available": False}
    return {
        "available": True,
        "pull_requests": queue.get("pull_requests") or [],
        "issues": queue.get("issues") or [],
        "errors": queue.get("errors") or [],
        "note": queue.get("note"),
    }


def recommendation_to_packet_input(project: dict[str, Any], recommendation: dict[str, Any]) -> dict[str, Any]:
    """Map a planner recommendation into packet_generator input."""
    cand = recommendation.get("candidate") or {}
    target_type = recommendation.get("target_type")
    if target_type == "pull_request":
        return {
            "source_type": "pull_request",
            "pull_request": {
                "number": cand.get("number"),
                "title": recommendation.get("title"),
                "body": cand.get("objective") or "",
                "repository": cand.get("repository") or project.get("repository"),
                "html_url": recommendation.get("html_url"),
                "ci": {"status": cand.get("ci_status") or "unknown"},
                "files": cand.get("allowed_files") or [],
            },
            "target_repository": cand.get("repository") or project.get("repository"),
            "target_branch": project.get("default_branch") or "main",
            "context": _planner_context_blob(project, recommendation),
            "operating_mode": "REVIEW",
        }

    # planned task path
    task = {
        "task_id": recommendation.get("target_id") or "PLANNER",
        "objective": cand.get("objective") or recommendation.get("headline"),
        "operating_mode": "IMPLEMENTATION" if str(recommendation.get("risk")) == "YELLOW" else (
            "PLAN_ONLY" if str(recommendation.get("risk")) == "RED" else "READ_ONLY_AUDIT"
        ),
        "allowed_files": cand.get("allowed_files") or ["docs/**", "tests/**"],
        "forbidden_files": cand.get("forbidden_files") or [".env", "secrets/**"],
        "acceptance_criteria": cand.get("acceptance_criteria")
        or ["Complete objective", "No secrets exposed", "Final report filled"],
        "required_tests": cand.get("required_tests") or [],
        "repository": project.get("repository"),
        "target_branch": project.get("default_branch") or "main",
        "context": _planner_context_blob(project, recommendation),
    }
    if str(recommendation.get("risk")) == "GREEN":
        task["operating_mode"] = "READ_ONLY_AUDIT"
    return {
        "source_type": "task",
        "task": task,
        "title": recommendation.get("title") or recommendation.get("headline"),
        "target_repository": project.get("repository"),
        "target_branch": project.get("default_branch") or "main",
        "context": _planner_context_blob(project, recommendation),
        "objective": task["objective"],
        "operating_mode": task["operating_mode"],
        "allowed_files": task["allowed_files"],
        "forbidden_files": task["forbidden_files"],
        "acceptance_criteria": task["acceptance_criteria"],
        "required_tests": task.get("required_tests") or [],
    }


def _planner_context_blob(project: dict[str, Any], recommendation: dict[str, Any]) -> str:
    lines = [
        f"Project: {project.get('name')} ({project.get('id')})",
        f"Repository: {project.get('repository')}",
        f"Active stage: {recommendation.get('stage_name') or project.get('current_stage_id')}",
        f"Planner recommendation: {recommendation.get('headline')}",
        f"Recommendation type: {recommendation.get('recommendation_type')}",
        f"Risk: {recommendation.get('risk')}",
        f"Requires Shan: {recommendation.get('requires_shan')}",
        f"Score: {recommendation.get('total_score')}",
        "Reasoning:",
    ]
    for reason in recommendation.get("reasoning") or []:
        lines.append(f"- {reason}")
    if recommendation.get("incomplete_dependencies"):
        lines.append("Incomplete dependencies: " + ", ".join(recommendation["incomplete_dependencies"]))
    if recommendation.get("blocked_reason"):
        lines.append(f"Blocked reason: {recommendation['blocked_reason']}")
    lines.append("This packet does not grant merge or production authority.")
    return "\n".join(lines)

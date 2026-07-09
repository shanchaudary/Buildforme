"""Founder briefing generator (Stage 4).

On-demand only. Does not fabricate overnight completion without event history.
"""

from __future__ import annotations

from typing import Any

from buildforme.planner import plan_project
from buildforme.storage import LocalStore, utc_now_iso


def build_founder_briefing(
    store: LocalStore,
    project_ids: list[str] | None = None,
    queues: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    queues = queues or {}
    projects = store.list_projects(include_archived=False)
    if project_ids:
        wanted = set(project_ids)
        projects = [p for p in projects if str(p.get("id")) in wanted]

    last_at = store.last_briefing_at()
    project_sections: list[dict[str, Any]] = []
    needs_shan: list[dict[str, Any]] = []
    failed_or_blocked: list[dict[str, Any]] = []
    recommended_next: list[dict[str, Any]] = []
    do_not_start: list[dict[str, Any]] = []
    open_prs = 0
    failing_ci = 0
    ready_agent = 0
    blocked_projects = 0
    active_projects = 0

    for project in projects:
        pid = str(project.get("id"))
        status = str(project.get("status") or "")
        if status == "active":
            active_projects += 1
        if status in {"blocked", "paused"}:
            blocked_projects += 1
        github = queues.get(pid) or {"available": False}
        try:
            plan = plan_project(pid, store, github_data=github)
        except Exception as exc:  # noqa: BLE001
            project_sections.append(
                {
                    "project_id": pid,
                    "name": project.get("name"),
                    "error": str(exc),
                    "status": status,
                }
            )
            continue

        summary = plan.get("summary") or {}
        open_prs += int(summary.get("open_prs") or 0)
        failing_ci += int(summary.get("failing_ci") or 0)
        primary = plan.get("primary_recommendation") or {}
        if primary.get("requires_shan"):
            needs_shan.append(
                {
                    "project_id": pid,
                    "headline": primary.get("headline"),
                    "risk": primary.get("risk"),
                    "target_id": primary.get("target_id"),
                }
            )
        if primary.get("recommendation_type") in {"execute_task", "fix_ci", "review_pr"} and not primary.get(
            "requires_shan"
        ):
            ready_agent += 1
            recommended_next.append(
                {
                    "project_id": pid,
                    "headline": primary.get("headline"),
                    "risk": primary.get("risk"),
                    "score": primary.get("total_score"),
                    "why": (primary.get("reasoning") or [])[:3],
                }
            )
        for blocker in plan.get("blockers") or []:
            failed_or_blocked.append(
                {
                    "project_id": pid,
                    "blocker": blocker.get("blocker"),
                    "severity": blocker.get("severity"),
                    "requires_shan": blocker.get("requires_shan"),
                }
            )
        for rec in plan.get("ranked_recommendations") or []:
            if rec.get("recommendation_type") in {"no_action", "resolve_blocker"} or rec.get("blocked_reason"):
                do_not_start.append(
                    {
                        "project_id": pid,
                        "headline": rec.get("headline"),
                        "reason": rec.get("blocked_reason") or (rec.get("reasoning") or ["Not ready"])[0],
                    }
                )
        project_sections.append(
            {
                "project_id": pid,
                "name": project.get("name"),
                "status": status,
                "confidence": plan.get("confidence"),
                "summary": summary,
                "primary": primary,
            }
        )

    completed_note = (
        f"Last briefing recorded at {last_at}."
        if last_at
        else "Current state since last recorded briefing cannot be fully reconstructed."
    )

    briefing = {
        "generated_at": utc_now_iso(),
        "projects": project_sections,
        "summary": {
            "active_projects": active_projects,
            "blocked_projects": blocked_projects,
            "open_prs": open_prs,
            "failing_ci": failing_ci,
            "needs_shan": len(needs_shan),
            "ready_agent_tasks": ready_agent,
        },
        "completed_since_last_briefing": [],
        "completed_note": completed_note,
        "failed_or_blocked": failed_or_blocked[:20],
        "needs_shan": needs_shan[:20],
        "recommended_next": recommended_next[:10],
        "do_not_start_yet": do_not_start[:20],
        "disclaimer": (
            "Briefing is generated from local plan data and optional read-only GitHub signals. "
            "It is not a production readiness certificate and does not authorize merges."
        ),
    }
    return store.save_briefing(briefing)
